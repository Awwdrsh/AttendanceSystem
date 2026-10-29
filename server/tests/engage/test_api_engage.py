"""Port of apps/api/test/engage.test.ts at the HTTP level: the React app's /api/v1/engage JSON API (DRF), with the
same paths, bodies, answers and status codes (the insights blocks belong to the insights slice)."""
import json
from datetime import date, timedelta

import pytest
from django.test import Client

from engines.bs_calendar import BsDate, ad_to_bs, bs_to_ad
from hajiri.core import auth
from hajiri.db.models import Employee, NotificationOutbox, Survey, SurveyResponse
from hajiri.engage import services as S
from hajiri.engage.surveys import run_survey_jobs
from tests.web.factories import PW, bearer, set_features_off

from .conftest import client_for

A = '/api/v1/engage'


def add_days(ad: str, n: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=n)).isoformat()


class Api:
    """A signed-in JSON client (cookie session, like the React app)."""

    def __init__(self, user):
        self.c = client_for(user)

    def get(self, path, query=None):
        return self.c.get(A + path, query or {})

    def _send(self, method, path, body):
        return getattr(self.c, method)(A + path, json.dumps({} if body is None else body), content_type='application/json')

    def post(self, path, body=None):
        return self._send('post', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)

    def delete(self, path):
        return self.c.delete(A + path)


@pytest.fixture
def w(world):
    world.api = {k: Api(getattr(world, k)) for k in ('hr', 'sita', 'ram', 'foreign_hr')}
    world.x = [Api(e.user) for e in world.extra]
    return world


def ids(rows):
    return [r['id'] for r in rows]


# ================================================================ announcements

def test_hr_posts_to_everyone_people_are_notified_and_see_it_in_their_feed(w):
    hr, sita, ram = w.api['hr'], w.api['sita'], w.api['ram']
    r = hr.post('/announcements', {'title': 'Dashain holidays', 'body': 'Office closed from 1 to 5 Kartik.', 'pinned': True,
                                   'requiresAck': True})
    assert r.status_code == 201
    ann = r.json()['id']
    assert r.json()['notified'] >= 7
    feed = sita.get('/feed').json()
    item = next(i for i in feed['items'] if i['id'] == ann)
    assert item['type'] == 'announcement' and item['pinned'] and item['requiresAck'] and item['acked'] is False
    assert item['canEdit'] is False and item['createdAt'].endswith('Z')
    assert NotificationOutbox.objects.filter(template='engage.announcement', payload__announcementId=ann).count() >= 7
    assert sita.get('/home').json()['announcement']['id'] == ann

    # tracks who confirmed reading; only the author or HR sees the list
    assert sita.post(f'/announcements/{ann}/ack').status_code == 200
    acks = hr.get(f'/announcements/{ann}/acks')
    assert acks.status_code == 200
    body = acks.json()
    assert set(body) == {'audience', 'acked', 'people'}
    assert body['acked'] == 1 and body['people'][-1]['employeeId'] == w.sita_id
    r = ram.get(f'/announcements/{ann}/acks')
    assert r.status_code == 403 and r.json() == {'error': 'only the author or HR can see confirmations'}
    assert ram.patch(f'/announcements/{ann}', {'title': 'x'}).status_code == 403
    assert hr.patch(f'/announcements/{ann}', {'title': 'Dashain'}).json() == {'ok': True}
    r = hr.patch(f'/announcements/{ann}', {'title': 'x', 'nope': 1})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert hr.get('/announcements', {'all': '1'}).json()['announcements'][0]['title'] == 'Dashain'
    # a malformed id is a validation error, as uuid.parse was
    assert hr.post('/announcements/not-a-uuid/ack').status_code == 400
    assert hr.delete(f'/announcements/{ann}').json() == {'ok': True}
    assert sita.post(f'/announcements/{ann}/ack').status_code == 404


def test_managers_post_only_to_their_own_team_and_only_that_team_sees_it(w):
    ram, sita = w.api['ram'], w.api['sita']
    assert ram.post('/announcements', {'title': 'All hands', 'body': 'x'}).status_code == 403
    assert sita.post('/announcements', {'title': 'Hi', 'body': 'x', 'audienceKind': 'reports'}).status_code == 403
    r = ram.post('/announcements', {'title': 'Team lunch', 'body': 'Friday 1 pm', 'audienceKind': 'reports', 'pinned': True})
    assert r.status_code == 201 and r.json()['notified'] == 2     # Sita and Person 0
    aid = r.json()['id']
    mine = w.x[0].get('/announcements').json()['announcements']
    assert aid in ids(mine)
    assert next(a for a in mine if a['id'] == aid)['pinned'] is False     # only HR pins
    assert aid not in ids(w.x[1].get('/announcements').json()['announcements'])
    assert w.x[1].post(f'/announcements/{aid}/ack').status_code == 404


def test_department_audiences_reach_that_department_only_other_companies_never_see_it(w):
    hr = w.api['hr']
    r = hr.post('/announcements', {'title': 'Ops drill', 'body': 'Fire drill at 3', 'audienceKind': 'department',
                                   'audienceId': w.dept_b})
    assert r.status_code == 201 and r.json()['notified'] == 2
    aid = r.json()['id']
    assert aid in ids(w.x[4].get('/announcements').json()['announcements'])
    assert aid not in ids(w.api['sita'].get('/announcements').json()['announcements'])
    foreign = w.api['foreign_hr']
    assert foreign.get('/announcements').json()['announcements'] == []
    r = foreign.post('/announcements', {'title': 'x', 'body': 'y', 'audienceKind': 'department', 'audienceId': w.dept_b})
    assert r.status_code == 400 and r.json() == {'error': 'pick the branch or department this is for'}
    assert foreign.patch(f'/announcements/{aid}', {'title': 'x'}).status_code == 404
    assert foreign.delete(f'/announcements/{aid}').status_code == 404
    assert foreign.get(f'/announcements/{aid}/acks').status_code == 404


# ================================================================ kudos

def test_kudos_reactions_comments_limit_leaderboard_and_values(w):
    hr, sita, ram, foreign = w.api['hr'], w.api['sita'], w.api['ram'], w.api['foreign_hr']
    values = sita.get('/values').json()
    assert len(values['values']) >= 5 and values['kudosMonthlyLimit'] is None
    assert set(values['values'][0]) == {'id', 'name', 'description', 'emoji', 'active', 'sortOrder'}
    r = sita.post('/kudos', {'recipientIds': [w.sita_id], 'message': 'Me!'})
    assert r.status_code == 400
    ok = sita.post('/kudos', {'recipientIds': [w.ram_id, w.extra[0].id], 'valueId': values['values'][0]['id'],
                              'message': 'Thanks for the release help'})
    assert ok.status_code == 201
    kid = ok.json()['id']
    assert NotificationOutbox.objects.filter(template='engage.kudos', channel='in_app').count() == 2
    feed = w.x[3].get('/kudos').json()['kudos']
    assert feed[0]['id'] == kid and feed[0]['canDelete'] is False and feed[0]['giver']['employeeId'] == w.sita_id
    assert len(feed[0]['recipients']) == 2
    assert w.x[3].get('/colleagues').json()['colleagues'][0].keys() == {'id', 'name', 'empCode', 'designation'}
    r = sita.post('/kudos', {'recipientIds': [w.ram_id, '00000000-0000-7000-8000-00000000ffff'], 'message': 'Hello there'})
    assert r.status_code == 400 and r.json() == {'error': 'Pick colleagues from your company.'}
    r = hr.post('/kudos', {'recipientIds': [w.ram_id], 'message': 'From HR acct'})
    assert r.status_code == 403 and r.json() == {'error': 'only employees can give kudos'}

    # reactions toggle and comments notify the people involved
    on = ram.post('/reactions', {'targetType': 'kudos', 'targetId': kid, 'emoji': 'clap'}).json()
    assert on['reacted'] is True and on['reactions'] == {'clap': 1} and on['mine'] == ['clap']
    off = ram.post('/reactions', {'targetType': 'kudos', 'targetId': kid, 'emoji': 'clap'}).json()
    assert off['reacted'] is False and off['reactions'] == {}
    r = ram.post('/reactions', {'targetType': 'kudos', 'targetId': kid, 'emoji': 'poop'})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'emoji'
    c = w.x[2].post('/comments', {'targetType': 'kudos', 'targetId': kid, 'body': 'Well deserved!'})
    assert c.status_code == 201 and set(c.json()) == {'id'}
    listed = sita.get('/comments', {'targetType': 'kudos', 'targetId': kid}).json()['comments']
    assert listed[0]['body'] == 'Well deserved!' and listed[0]['canDelete'] is False
    assert sita.delete(f"/comments/{c.json()['id']}").status_code == 403
    assert w.x[2].delete(f"/comments/{c.json()['id']}").json() == {'ok': True}
    r = foreign.get('/comments', {'targetType': 'kudos', 'targetId': kid})
    assert r.status_code == 404 and r.json() == {'error': 'not found'}
    assert foreign.post('/reactions', {'targetType': 'kudos', 'targetId': kid, 'emoji': 'clap'}).status_code == 404

    # enforces the monthly limit, and the leaderboard counts the month
    assert sita.put('/settings', {'kudosMonthlyLimit': 2}).status_code == 403
    assert hr.put('/settings', {'kudosMonthlyLimit': 2}).json() == {'ok': True}
    assert sita.post('/kudos', {'recipientIds': [w.extra[1].id], 'message': 'Second one'}).status_code == 201
    third = sita.post('/kudos', {'recipientIds': [w.extra[1].id], 'message': 'Third one'})
    assert third.status_code == 409
    lb = ram.get('/leaderboard').json()
    assert lb['total'] == 2 and lb['given'][0]['employeeId'] == w.sita_id and lb['given'][0]['count'] == 2
    assert ram.get('/leaderboard', {'bsMonth': '13'}).status_code == 400
    hr.put('/settings', {'kudosMonthlyLimit': None})

    # only the giver or HR removes kudos; another company does not find it
    assert foreign.delete(f'/kudos/{kid}').status_code == 404
    assert ram.delete(f'/kudos/{kid}').status_code == 403
    assert hr.delete(f'/kudos/{kid}').json() == {'ok': True}

    # values are managed by HR
    assert sita.post('/values', {'name': 'Speed'}).status_code == 403
    v = hr.post('/values', {'name': 'Speed', 'emoji': '⚡'})
    assert v.status_code == 201
    assert hr.patch(f"/values/{v.json()['id']}", {'active': False}).json() == {'ok': True}
    assert foreign.patch(f"/values/{v.json()['id']}", {'active': True}).status_code == 404
    r = ram.post('/kudos', {'recipientIds': [w.sita_id], 'valueId': v.json()['id'], 'message': 'Fast work'})
    assert r.status_code == 400 and r.json() == {'error': 'Pick one of the company values.'}


def test_kudos_feature_off(w):
    set_features_off(w.co, 'kudos')
    r = w.api['sita'].get('/kudos')
    assert r.status_code == 404 and r.json() == {'error': 'feature off', 'feature': 'kudos'}
    assert w.api['sita'].get('/leaderboard').json() == {'error': 'feature off', 'feature': 'kudos'}
    assert w.api['sita'].get('/feed').status_code == 200


# ================================================================ celebrations

def test_birthdays_only_for_people_who_opted_in_anniversaries_and_joiners_always(w):
    ram, sita = w.api['ram'], w.api['sita']
    today = S.npt_today()
    bs = ad_to_bs(today)
    dob = bs_to_ad(BsDate(bs.year - 30, bs.month, bs.day))
    joined = bs_to_ad(BsDate(bs.year - 3, bs.month, bs.day))
    Employee.objects.filter(pk=w.sita_id).update(dob=dob, join_date=joined)
    Employee.objects.filter(pk=w.extra[4].id).update(join_date=add_days(today, -3))
    c = ram.get('/celebrations').json()['celebrations']
    assert not [x for x in c if x['kind'] == 'birthday']
    ann = next(x for x in c if x['kind'] == 'anniversary' and x['employeeId'] == w.sita_id)
    assert ann['years'] == 3 and ann['daysAway'] == 0
    assert [x for x in c if x['kind'] == 'joined' and x['employeeId'] == w.extra[4].id]
    hidden = ram.post('/comments', {'targetType': 'celebration', 'targetId': f'birthday:{w.sita_id}:{bs.year}',
                                    'body': 'Happy birthday'})
    assert hidden.status_code == 404

    assert sita.get('/me/prefs').json() == {'showBirthday': False, 'hasBirthday': True}
    assert w.api['hr'].get('/me/prefs').json() == {'showBirthday': False, 'hasBirthday': False}
    assert w.api['hr'].put('/me/prefs', {'showBirthday': True}).status_code == 403
    assert sita.put('/me/prefs', {'showBirthday': True}).json() == {'ok': True}
    c = ram.get('/celebrations', {'days': 14}).json()['celebrations']
    bday = next(x for x in c if x['kind'] == 'birthday')
    assert bday['employeeId'] == w.sita_id and bday['daysAway'] == 0
    assert bday['years'] is None    # age is never shown
    assert ram.post('/comments', {'targetType': 'celebration', 'targetId': bday['targetId'],
                                  'body': 'Happy birthday!'}).status_code == 201
    home = w.x[2].get('/home').json()
    assert {'birthday', 'anniversary'} <= {x['kind'] for x in home['celebrations']}
    assert ram.get('/celebrations', {'days': 61}).status_code == 400
    # another company sees none of it
    assert w.api['foreign_hr'].get('/celebrations').json() == {'celebrations': []}


# ================================================================ surveys and polls

QUESTIONS = [
    {'kind': 'enps', 'prompt': 'How likely are you to recommend CoreValley as a place to work?'},
    {'kind': 'rating', 'prompt': 'I have what I need to do my job well.'},
    {'kind': 'multi', 'prompt': 'What would help most?', 'options': ['Training', 'Tools', 'Flexibility']},
    {'kind': 'text', 'prompt': 'Anything else?', 'required': False},
]


def test_surveys_end_to_end(w):
    hr, sita, ram, foreign = w.api['hr'], w.api['sita'], w.api['ram'], w.api['foreign_hr']
    today = S.npt_today()
    assert sita.post('/surveys', {'title': 'x', 'opensOn': today, 'closesOn': today, 'questions': QUESTIONS}).status_code == 403
    assert hr.post('/surveys', {'kind': 'poll', 'title': 'x', 'opensOn': today, 'closesOn': today,
                                'questions': QUESTIONS}).status_code == 400
    assert hr.post('/surveys', {'title': 'x', 'opensOn': today, 'closesOn': today,
                                'questions': [{'kind': 'single', 'prompt': 'Pick', 'options': ['Only one']}]}).status_code == 400
    r = hr.post('/surveys', {'title': 'Pulse', 'anonymous': True, 'minGroup': 3, 'opensOn': today,
                             'closesOn': add_days(today, 7), 'questions': QUESTIONS})
    assert r.status_code == 201
    sid = r.json()['id']
    # A draft is invisible to employees.
    assert sita.get(f'/surveys/{sid}').status_code == 404
    pub = hr.post(f'/surveys/{sid}/publish')
    assert pub.status_code == 200 and pub.json()['notified'] >= 7
    assert hr.post(f'/surveys/{sid}/publish').status_code == 409
    assert hr.put(f'/surveys/{sid}', {'title': 'Changed', 'opensOn': today, 'closesOn': today,
                                      'questions': QUESTIONS}).status_code == 400
    assert hr.put(f'/surveys/{sid}', {'closesOn': add_days(today, 8)}).json() == {'ok': True}

    # anonymous answers are not linked to anyone, and results stay hidden below the minimum group
    s = sita.get(f'/surveys/{sid}')
    assert s.status_code == 200 and s.json()['answered'] is False
    q = s.json()['questions']

    def answer(enps, rating, text=None):
        a = [{'questionId': q[0]['id'], 'value': enps}, {'questionId': q[1]['id'], 'value': rating},
             {'questionId': q[2]['id'], 'choices': ['Tools']}]
        if text:
            a.append({'questionId': q[3]['id'], 'text': text})
        return {'answers': a}

    assert sita.post(f'/surveys/{sid}/respond', {'answers': [{'questionId': q[0]['id'], 'value': 11}]}).status_code == 400
    assert sita.post(f'/surveys/{sid}/respond', {'answers': [{'questionId': q[0]['id'], 'value': 9}]}).status_code == 400
    r = sita.post(f'/surveys/{sid}/respond', answer(10, 5, 'Great team'))
    assert r.status_code == 201 and r.json() == {'ok': True}
    r = sita.post(f'/surveys/{sid}/respond', answer(10, 5))
    assert r.status_code == 409 and r.json() == {'error': 'You have already answered this.'}
    assert ram.post(f'/surveys/{sid}/respond', answer(3, 2, 'More tools please')).status_code == 201
    res = hr.get(f'/surveys/{sid}/results').json()
    assert res['results']['responses'] == 2 and res['results']['hidden'] is True and res['results']['questions'] == []
    assert sita.get(f'/surveys/{sid}/results').status_code == 404
    assert w.x[0].post(f'/surveys/{sid}/respond', answer(8, 4)).status_code == 201
    res = hr.get(f'/surveys/{sid}/results').json()['results']
    assert res['responses'] == 3 and res['hidden'] is True and res['hiddenUntilClose'] is True and res['questions'] == []
    Survey.objects.filter(pk=sid).update(status=Survey.Status.CLOSED)   # anonymous results show once it has closed
    res = hr.get(f'/surveys/{sid}/results').json()['results']
    assert res['hidden'] is False
    enps = res['questions'][0]
    assert enps['enps'] == {'score': 0, 'promoters': 1, 'passives': 1, 'detractors': 1}
    assert res['questions'][1]['average'] == pytest.approx(3.67, abs=0.01)
    assert res['questions'][2]['distribution'] == [{'label': 'Training', 'count': 0}, {'label': 'Tools', 'count': 3},
                                                   {'label': 'Flexibility', 'count': 0}]
    texts = res['questions'][3]['texts']
    assert len(texts) == 2 and all(list(t) == ['text'] for t in texts)
    assert enps['breakdown'] == []
    stored = list(SurveyResponse.objects.filter(survey_id=sid).values('employee_id', 'respondent_key'))
    assert all(r['employee_id'] is None for r in stored)
    assert not any(w.sita_id in r['respondent_key'] for r in stored)
    csv = hr.get(f'/surveys/{sid}/results.csv')
    assert csv.status_code == 200 and csv['Content-Type'].startswith('text/csv')
    assert 'attachment; filename="survey-results.csv"' == csv['Content-Disposition']
    text = csv.content.decode('utf-8')
    assert 'More tools please' in text and 'Sita' not in text
    assert sita.get(f'/surveys/{sid}/results.csv').status_code == 403
    assert foreign.get(f'/surveys/{sid}/results.csv').status_code == 404
    Survey.objects.filter(pk=sid).update(status=Survey.Status.PUBLISHED)
    listed = sita.get('/surveys').json()['surveys']
    assert next(x for x in listed if x['id'] == sid)['answered'] is True
    assert foreign.get(f'/surveys/{sid}/results').status_code == 404
    assert foreign.get(f'/surveys/{sid}').status_code == 404

    # a poll shows results to people who voted, and audiences are respected
    p = hr.post('/surveys', {'kind': 'poll', 'title': 'Friday snacks', 'anonymous': False, 'audienceKind': 'department',
                             'audienceId': w.dept_b, 'opensOn': today, 'closesOn': today,
                             'questions': [{'kind': 'single', 'prompt': 'Which snack?', 'options': ['Momo', 'Samosa']}]})
    poll = p.json()['id']
    hr.post(f'/surveys/{poll}/publish')
    pq = w.x[3].get(f'/surveys/{poll}').json()['questions'][0]
    assert sita.get(f'/surveys/{poll}').status_code == 404
    assert sita.post(f'/surveys/{poll}/respond', {'answers': [{'questionId': pq['id'], 'choices': ['Momo']}]}).status_code == 404
    assert w.x[3].get(f'/surveys/{poll}/results').status_code == 404
    v = w.x[3].post(f'/surveys/{poll}/respond', {'answers': [{'questionId': pq['id'], 'choices': ['Momo']}]})
    assert v.status_code == 201
    assert v.json()['results']['questions'][0]['distribution'][0] == {'label': 'Momo', 'count': 1}
    assert w.x[3].get(f'/surveys/{poll}/results').status_code == 200
    assert w.x[4].post(f'/surveys/{poll}/respond',
                       {'answers': [{'questionId': pq['id'], 'choices': ['Pizza']}]}).status_code == 400
    assert str(SurveyResponse.objects.get(survey_id=poll).employee_id) == w.extra[3].id   # not anonymous

    # closing by hand stops the series; drafts can be deleted
    d = hr.post('/surveys', {'title': 'Draft', 'opensOn': today, 'closesOn': today,
                             'questions': [{'kind': 'text', 'prompt': 'Hi?'}]})
    assert foreign.delete(f"/surveys/{d.json()['id']}").status_code == 404
    assert hr.delete(f"/surveys/{d.json()['id']}").json() == {'ok': True}
    assert hr.post(f'/surveys/{sid}/close').json() == {'ok': True}
    assert hr.post(f'/surveys/{sid}/close').status_code == 409
    assert hr.delete(f'/surveys/{sid}').status_code == 409
    manage = hr.get('/surveys/manage').json()['surveys']
    row = next(s for s in manage if s['id'] == sid)
    assert row['status'] == 'closed' and row['responses'] == 3 and row['questionCount'] == 4
    r = ram.get('/surveys/manage')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    assert foreign.get('/surveys/manage').json() == {'surveys': []}


def test_daily_job_reminds_people_who_have_not_answered(w):
    hr, sita = w.api['hr'], w.api['sita']
    today = S.npt_today()
    r = hr.post('/surveys', {'title': 'Weekly pulse', 'recurrence': 'weekly', 'opensOn': add_days(today, -6),
                             'closesOn': add_days(today, 1), 'questions': [{'kind': 'rating', 'prompt': 'How was your week?'}]})
    sid = r.json()['id']
    hr.post(f'/surveys/{sid}/publish')
    q = sita.get(f'/surveys/{sid}').json()['questions'][0]
    assert sita.post(f'/surveys/{sid}/respond', {'answers': [{'questionId': q['id'], 'value': 4}]}).status_code == 201
    assert run_survey_jobs(today)['reminded'] == 6     # 7 in the audience, Sita already answered
    assert run_survey_jobs(today)['reminded'] == 0
    later = run_survey_jobs(add_days(today, 2))
    assert later['closed'] >= 1 and later['started'] == 1
    nxt = Survey.objects.get(series_id=sid)
    assert nxt.status == 'published' and nxt.opens_on.isoformat() == add_days(today, 1)


def test_surveys_feature_off_and_home_card(w):
    today = S.npt_today()
    sid = w.api['hr'].post('/surveys', {'title': 'Pulse', 'opensOn': today, 'closesOn': today,
                                        'questions': [{'kind': 'rating', 'prompt': 'How?'}]}).json()['id']
    w.api['hr'].post(f'/surveys/{sid}/publish')
    home = w.api['sita'].get('/home').json()
    assert home['survey']['id'] == sid and home['surveysWaiting'] == 1
    set_features_off(w.co, 'surveys')
    r = w.api['sita'].get('/surveys')
    assert r.status_code == 404 and r.json() == {'error': 'feature off', 'feature': 'surveys'}
    assert w.api['hr'].get(f'/surveys/{sid}/results.csv').status_code == 404
    home = w.api['sita'].get('/home').json()
    assert home['survey'] is None and home['surveysWaiting'] == 0


# ================================================================ suggestion box

def test_suggestions_store_no_author_hr_replies_and_can_share_the_answer(w):
    hr, sita, ram, foreign = w.api['hr'], w.api['sita'], w.api['ram'], w.api['foreign_hr']
    r = sita.post('/suggestions', {'category': 'workplace', 'body': 'Could we get a water filter on the second floor?'})
    assert r.status_code == 201 and r.json() == {'ok': True}
    assert sita.get('/suggestions').status_code == 403
    listed = hr.get('/suggestions').json()['suggestions']
    assert len(listed) == 1
    assert set(listed[0]) == {'id', 'category', 'body', 'receivedOn', 'status', 'reply', 'isPublic', 'repliedAt'}
    sid = listed[0]['id']
    assert hr.get('/suggestions', {'status': 'nope'}).status_code == 400
    assert foreign.patch(f'/suggestions/{sid}', {'status': 'reviewed'}).status_code == 404
    assert hr.patch(f'/suggestions/{sid}', {'reply': 'Ordered, arriving next week.', 'isPublic': True}).json() == {'ok': True}
    assert hr.get('/suggestions', {'status': 'answered'}).json()['suggestions'][0]['id'] == sid
    pub = ram.get('/suggestions/public').json()['suggestions']
    assert pub[0]['reply'] == 'Ordered, arriving next week.'
    assert set(pub[0]) == {'id', 'category', 'body', 'receivedOn', 'reply', 'repliedAt'}
    for i in range(5):
        ram.post('/suggestions', {'body': f'Idea number {i}'})
    assert ram.post('/suggestions', {'body': 'One idea too many'}).status_code == 409
    assert foreign.get('/suggestions/public').json()['suggestions'] == []
    assert foreign.get('/suggestions').json()['suggestions'] == []
    set_features_off(w.co, 'suggestions')
    assert sita.get('/suggestions/public').json() == {'error': 'feature off', 'feature': 'suggestions'}


# ================================================================ auth

def test_signed_out_and_bearer_clients(w):
    assert Client().get(f'{A}/feed').status_code == 401
    token = auth.login(w.sita.email, PW).token
    r = Client().get(f'{A}/feed', **bearer(token))
    assert r.status_code == 200 and set(r.json()) == {'items', 'nextBefore'}
    # an unrouted method answers 404, as Express did
    assert w.api['sita'].c.delete(f'{A}/feed').status_code == 404


def test_cookie_writes_are_checked_against_the_origin_not_a_csrf_token(w):
    """The React app sends the session cookie with fetch() and no CSRF token, as it did against the TS API."""
    c = Client(enforce_csrf_checks=True)
    from tests.web.factories import sign_in
    sign_in(c, w.sita)
    body = json.dumps({'body': 'A quieter meeting room please'})
    r = c.post(f'{A}/suggestions', body, content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201
    r = c.post(f'{A}/suggestions', body, content_type='application/json', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
