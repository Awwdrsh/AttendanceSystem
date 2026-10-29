"""Contract review of /api/v1/engage against the React app and the TS routes (65a9079 routes.ts / surveys.ts):
every response the app reads, with its exact key set and value types, the way the TS handlers built them."""
import re
from datetime import timedelta

from django.utils import timezone

from hajiri.db.models import Announcement, Employee, Kudos
from hajiri.engage import services as S

from .test_api_engage import QUESTIONS, add_days, w  # noqa: F401  (fixture)

ISO_TS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')

ANNOUNCEMENT_KEYS = {'type', 'id', 'createdAt', 'title', 'body', 'authorName', 'audienceKind', 'audienceId',
                     'audienceLabel', 'pinned', 'requiresAck', 'acked', 'expiresOn', 'popup', 'popupUntil',
                     'reactions', 'mine', 'comments', 'canEdit'}
KUDOS_KEYS = {'type', 'id', 'createdAt', 'message', 'giver', 'recipients', 'value', 'reactions', 'mine', 'comments',
              'canDelete'}
PERSON_KEYS = {'employeeId', 'name', 'designation'}
PUBLIC_SURVEY_KEYS = {'id', 'kind', 'title', 'description', 'anonymous', 'minGroup', 'audienceKind', 'audienceId',
                      'status', 'opensOn', 'closesOn', 'recurrence', 'seriesId', 'createdAt', 'publishedAt', 'closedAt'}


def test_feed_announcements_kudos_reactions_comments_and_acks_shapes(w):
    hr, sita, ram = w.api['hr'], w.api['sita'], w.api['ram']
    today = S.npt_today()
    a = hr.post('/announcements', {'title': 'Read me', 'body': 'Policy', 'requiresAck': True, 'pinned': True,
                                   'expiresOn': add_days(today, 3), 'popup': True, 'popupUntil': add_days(today, 2)})
    assert a.status_code == 201 and set(a.json()) == {'id', 'notified'} and isinstance(a.json()['notified'], int)
    aid = a.json()['id']
    values = sita.get('/values').json()
    assert set(values) == {'values', 'kudosMonthlyLimit'}
    k = sita.post('/kudos', {'recipientIds': [w.ram_id], 'valueId': values['values'][0]['id'], 'message': 'Thanks a lot'})
    assert k.status_code == 201 and set(k.json()) == {'id'}
    kid = k.json()['id']
    plain = sita.post('/kudos', {'recipientIds': [w.extra[0].id], 'message': 'No value tag'}).json()['id']
    # one test transaction gives every row the same now(): spread them out like separate requests would
    base = timezone.now()
    Announcement.objects.filter(pk=aid).update(created_at=base - timedelta(minutes=3))
    Kudos.objects.filter(pk=kid).update(created_at=base - timedelta(minutes=2))
    Kudos.objects.filter(pk=plain).update(created_at=base - timedelta(minutes=1))

    feed = ram.get('/feed', {'limit': 20}).json()
    assert set(feed) == {'items', 'nextBefore'} and feed['nextBefore'] is None
    by_id = {i['id']: i for i in feed['items']}
    ann = by_id[aid]
    assert set(ann) == ANNOUNCEMENT_KEYS
    assert ann['type'] == 'announcement' and ISO_TS.match(ann['createdAt'])
    assert ann['audienceKind'] == 'all' and ann['audienceId'] is None and ann['audienceLabel'] == 'Everyone'
    assert ann['authorName'] == 'HR Person' and ann['expiresOn'] == add_days(today, 3)
    assert ann['popup'] is True and ann['popupUntil'] == add_days(today, 2)
    assert ann['reactions'] == {} and ann['mine'] == [] and ann['comments'] == 0 and ann['canEdit'] is False
    kd = by_id[kid]
    assert set(kd) == KUDOS_KEYS and kd['type'] == 'kudos' and ISO_TS.match(kd['createdAt'])
    assert set(kd['giver']) == PERSON_KEYS and kd['giver']['employeeId'] == w.sita_id
    assert [set(r) for r in kd['recipients']] == [PERSON_KEYS] and kd['recipients'][0]['employeeId'] == w.ram_id
    assert set(kd['value']) == {'id', 'name', 'emoji'}
    assert by_id[plain]['value'] is None
    # newest first, across both kinds
    stamps = [i['createdAt'] for i in feed['items']]
    assert stamps == sorted(stamps, reverse=True)

    # paging: nextBefore is an ISO instant the app sends back as `before`
    page = ram.get('/feed', {'limit': 2}).json()
    assert len(page['items']) == 2 and page['nextBefore']
    rest = ram.get('/feed', {'before': page['nextBefore'], 'limit': 20}).json()
    assert {i['id'] for i in rest['items']}.isdisjoint({i['id'] for i in page['items']})
    assert len(rest['items']) + 2 == len(feed['items'])

    # GET /announcements and GET /kudos use the same item shapes and list wrappers
    assert set(ram.get('/announcements').json()) == {'announcements'}
    assert set(ram.get('/announcements').json()['announcements'][0]) == ANNOUNCEMENT_KEYS
    assert set(ram.get('/kudos').json()) == {'kudos'}
    assert set(ram.get('/kudos').json()['kudos'][0]) == KUDOS_KEYS

    # reactions: exactly {reacted, reactions, mine}
    r = ram.post('/reactions', {'targetType': 'announcement', 'targetId': aid, 'emoji': 'love'})
    assert r.status_code == 200 and r.json() == {'reacted': True, 'reactions': {'love': 1}, 'mine': ['love']}

    # comments
    c = sita.post('/comments', {'targetType': 'announcement', 'targetId': aid, 'body': 'Noted'})
    assert c.status_code == 201 and set(c.json()) == {'id'}
    listed = ram.get('/comments', {'targetType': 'announcement', 'targetId': aid}).json()
    assert set(listed) == {'comments'}
    assert set(listed['comments'][0]) == {'id', 'body', 'createdAt', 'name', 'canDelete'}
    assert listed['comments'][0]['name'] == 'Sita Sharma' and ISO_TS.match(listed['comments'][0]['createdAt'])
    assert ram.get('/feed').json()['items'][0]['comments'] >= 0
    assert next(i for i in ram.get('/feed').json()['items'] if i['id'] == aid)['comments'] == 1
    assert sita.delete(f"/comments/{c.json()['id']}").json() == {'ok': True}

    # acks list
    assert sita.post(f'/announcements/{aid}/ack').json() == {'ok': True}
    acks = hr.get(f'/announcements/{aid}/acks').json()
    assert set(acks) == {'audience', 'acked', 'people'} and acks['acked'] == 1
    assert all(set(p) == {'employeeId', 'name', 'empCode', 'ackedAt'} for p in acks['people'])
    done = next(p for p in acks['people'] if p['employeeId'] == w.sita_id)
    assert ISO_TS.match(done['ackedAt']) and done['empCode'] == 'E001'
    # not yet confirmed come first, with ackedAt null
    assert acks['people'][0]['ackedAt'] is None and acks['people'][-1]['employeeId'] == w.sita_id

    # the pin toggle the feed card sends
    assert hr.patch(f'/announcements/{aid}', {'pinned': False}).json() == {'ok': True}
    assert next(i for i in ram.get('/feed').json()['items'] if i['id'] == aid)['pinned'] is False


def test_leaderboard_colleagues_values_settings_shapes(w):
    hr, sita, ram = w.api['hr'], w.api['sita'], w.api['ram']
    sita.post('/kudos', {'recipientIds': [w.ram_id], 'message': 'Thanks a lot'})
    lb = ram.get('/leaderboard').json()
    assert set(lb) == {'bsYear', 'bsMonth', 'total', 'received', 'given', 'values'}
    assert isinstance(lb['bsYear'], int) and isinstance(lb['bsMonth'], int) and lb['total'] == 1
    assert lb['received'] == [{'employeeId': w.ram_id, 'name': 'Ram Thapa', 'designation': lb['received'][0]['designation'],
                               'count': 1}]
    assert set(lb['given'][0]) == PERSON_KEYS | {'count'}
    assert all(set(v) == {'id', 'name', 'emoji', 'count'} for v in lb['values'])
    col = sita.get('/colleagues').json()
    assert set(col) == {'colleagues'} and all(set(x) == {'id', 'name', 'empCode', 'designation'} for x in col['colleagues'])
    v = hr.post('/values', {'name': 'Care', 'emoji': '💙', 'description': None})
    assert v.status_code == 201 and set(v.json()) == {'id'}
    assert hr.put('/settings', {'kudosMonthlyLimit': None}).json() == {'ok': True}
    assert hr.put('/settings', {'kudosMonthlyLimit': 4}).json() == {'ok': True}
    assert sita.get('/values').json()['kudosMonthlyLimit'] == 4


def test_celebrations_prefs_and_home_shapes(w):
    ram = w.api['ram']
    today = S.npt_today()
    Employee.objects.filter(pk=w.extra[4].id).update(join_date=today)
    c = ram.get('/celebrations', {'days': 14}).json()
    assert set(c) == {'celebrations'}
    j = next(x for x in c['celebrations'] if x['employeeId'] == w.extra[4].id)
    assert set(j) == {'kind', 'targetId', 'employeeId', 'name', 'designation', 'date', 'bsLabel', 'years', 'daysAway',
                      'reactions', 'mine', 'comments', 'isMe'}
    assert j['kind'] == 'joined' and j['date'] == today and j['daysAway'] == 0 and j['years'] is None
    assert j['isMe'] is False and w.x[4].get('/celebrations').json()['celebrations'][0]['isMe'] is True
    assert set(ram.get('/me/prefs').json()) == {'showBirthday', 'hasBirthday'}
    assert ram.put('/me/prefs', {'showBirthday': True}).json() == {'ok': True}

    home = ram.get('/home').json()
    assert set(home) == {'announcement', 'celebrations', 'survey', 'surveysWaiting'}
    assert home['announcement'] is None and home['survey'] is None and home['surveysWaiting'] == 0
    assert home['celebrations'] == [{'kind': 'joined', 'name': 'Person 4', 'employeeId': w.extra[4].id, 'years': None,
                                     'targetId': f'joined:{w.extra[4].id}'}]
    hr = w.api['hr']
    hr.post('/announcements', {'title': 'Pinned', 'body': 'x', 'pinned': True})
    sid = hr.post('/surveys', {'kind': 'poll', 'title': 'Lunch?', 'opensOn': today, 'closesOn': add_days(today, 2),
                               'questions': [{'kind': 'single', 'prompt': 'Where', 'options': ['A', 'B']}]}).json()['id']
    hr.post(f'/surveys/{sid}/publish')
    home = ram.get('/home').json()
    assert set(home['announcement']) == ANNOUNCEMENT_KEYS
    assert home['survey'] == {'id': sid, 'title': 'Lunch?', 'kind': 'poll', 'closesOn': add_days(today, 2)}
    assert home['surveysWaiting'] == 1


def test_survey_shapes_from_builder_to_results(w):
    hr, sita = w.api['hr'], w.api['sita']
    today = S.npt_today()
    # the body exactly as SurveyBuilder sends it (the whole form, options trimmed and emptied for non-choice kinds)
    body = {'kind': 'survey', 'title': 'Pulse', 'description': None, 'anonymous': False, 'minGroup': 5,
            'audienceKind': 'all', 'audienceId': None, 'opensOn': today, 'closesOn': add_days(today, 7),
            'recurrence': 'none',
            'questions': [{'kind': 'enps', 'prompt': 'Recommend?', 'options': [], 'required': True},
                          {'kind': 'rating', 'prompt': 'Tools?', 'options': [], 'required': True},
                          {'kind': 'single', 'prompt': 'Best?', 'options': ['A', 'B'], 'required': True},
                          {'kind': 'text', 'prompt': 'Else?', 'options': [], 'required': False}]}
    r = hr.post('/surveys', body)
    assert r.status_code == 201 and set(r.json()) == {'id'}
    sid = r.json()['id']
    manage = hr.get('/surveys/manage').json()
    assert set(manage) == {'surveys'}
    row = manage['surveys'][0]
    assert set(row) == PUBLIC_SURVEY_KEYS | {'responses', 'questionCount'}
    assert row['status'] == 'draft' and row['responses'] == 0 and row['questionCount'] == 4
    assert row['opensOn'] == today and ISO_TS.match(row['createdAt']) and row['publishedAt'] is None

    one = hr.get(f'/surveys/{sid}').json()
    assert set(one) == {'survey', 'answered', 'questions'} and set(one['survey']) == PUBLIC_SURVEY_KEYS
    assert all(set(q) == {'id', 'kind', 'prompt', 'options', 'required'} for q in one['questions'])
    assert one['questions'][0]['options'] == [] and one['questions'][2]['options'] == ['A', 'B']
    # editing a draft sends the same full body back
    assert hr.put(f'/surveys/{sid}', {**body, 'title': 'Pulse 2'}).json() == {'ok': True}
    pub = hr.post(f'/surveys/{sid}/publish')
    assert pub.status_code == 200 and set(pub.json()) == {'notified'}

    mine = sita.get('/surveys').json()
    assert set(mine) == {'surveys'}
    assert set(mine['surveys'][0]) == {'id', 'kind', 'title', 'description', 'anonymous', 'opensOn', 'closesOn',
                                       'questionCount', 'answered'}
    qs = sita.get(f'/surveys/{sid}').json()['questions']
    answers = [{'questionId': qs[0]['id'], 'value': 9}, {'questionId': qs[1]['id'], 'value': 4},
               {'questionId': qs[2]['id'], 'choices': ['B']}, {'questionId': qs[3]['id'], 'text': 'More tea'}]
    resp = sita.post(f'/surveys/{sid}/respond', {'answers': answers})
    assert resp.status_code == 201 and resp.json() == {'ok': True}
    assert sita.get('/surveys').json()['surveys'][0]['answered'] is True

    res = hr.get(f'/surveys/{sid}/results').json()
    assert set(res) == {'survey', 'results'} and set(res['survey']) == PUBLIC_SURVEY_KEYS
    out = res['results']
    assert set(out) == {'responses', 'audience', 'hidden', 'minGroup', 'questions'}
    assert out['responses'] == 1 and out['hidden'] is False and out['minGroup'] == 5
    enps, rating, single, text = out['questions']
    base = {'questionId', 'kind', 'prompt', 'options', 'answered'}
    assert set(enps) == base | {'distribution', 'average', 'enps', 'breakdown'}
    assert enps['enps'] == {'score': 100, 'promoters': 1, 'passives': 0, 'detractors': 0}
    assert enps['average'] == 9 and isinstance(enps['average'], int) and len(enps['distribution']) == 11
    assert enps['distribution'][9] == {'label': '9', 'count': 1} and enps['breakdown'] == []
    assert set(rating) == base | {'distribution', 'average', 'breakdown'} and rating['average'] == 4
    assert set(single) == base | {'distribution'}
    assert single['distribution'] == [{'label': 'A', 'count': 0}, {'label': 'B', 'count': 1}]
    assert set(text) == base | {'texts'} and text['texts'] == [{'text': 'More tea', 'name': 'Sita Sharma'}]

    csv = hr.get(f'/surveys/{sid}/results.csv')
    assert csv.status_code == 200 and csv['Content-Type'].startswith('text/csv')
    assert csv['Content-Disposition'] == 'attachment; filename="survey-results.csv"'
    lines = csv.content.decode('utf-8').split('\r\n')
    assert lines[0] == '﻿Survey,Pulse 2' and lines[1] == 'Responses,1'
    assert 'Question,Type,Answer,Count,Average,eNPS,Name' in lines
    assert 'Recommend?,enps,Average,,9,100,' in lines and 'Else?,text,More tea,,,,Sita Sharma' in lines

    # extend the closing day of a published survey, then close it
    assert hr.put(f'/surveys/{sid}', {'closesOn': add_days(today, 9)}).json() == {'ok': True}
    assert hr.post(f'/surveys/{sid}/close').json() == {'ok': True}


def test_poll_vote_returns_results_and_hidden_results_shape(w):
    hr, sita = w.api['hr'], w.api['sita']
    today = S.npt_today()
    sid = hr.post('/surveys', {'kind': 'poll', 'title': 'Lunch?', 'opensOn': today, 'closesOn': add_days(today, 2),
                               'questions': [{'kind': 'single', 'prompt': 'Where', 'options': ['A', 'B']}]}).json()['id']
    hr.post(f'/surveys/{sid}/publish')
    qid = sita.get(f'/surveys/{sid}').json()['questions'][0]['id']
    r = sita.post(f'/surveys/{sid}/respond', {'answers': [{'questionId': qid, 'choices': ['A']}]})
    assert r.status_code == 201 and set(r.json()) == {'ok', 'results'}
    # anonymous poll below the minimum group: hidden, without hiddenUntilClose
    assert r.json()['results'] == {'responses': 1, 'audience': r.json()['results']['audience'], 'hidden': True,
                                   'minGroup': 5, 'questions': []}
    assert sita.get(f'/surveys/{sid}/results').status_code == 200
    # an anonymous survey still open says hiddenUntilClose once it has enough answers
    sid2 = hr.post('/surveys', {'title': 'Anon', 'minGroup': 3, 'opensOn': today, 'closesOn': add_days(today, 2),
                                'questions': [{'kind': 'rating', 'prompt': 'Ok?'}]}).json()['id']
    hr.post(f'/surveys/{sid2}/publish')
    for api in [sita, *w.x[:3]]:
        q2 = api.get(f'/surveys/{sid2}').json()['questions'][0]['id']
        assert api.post(f'/surveys/{sid2}/respond', {'answers': [{'questionId': q2, 'value': 3}]}).json() == {'ok': True}
    out = hr.get(f'/surveys/{sid2}/results').json()['results']
    assert out == {'responses': 4, 'audience': out['audience'], 'hidden': True, 'hiddenUntilClose': True,
                   'minGroup': 3, 'questions': []}


def test_suggestion_shapes(w):
    hr, sita = w.api['hr'], w.api['sita']
    r = sita.post('/suggestions', {'category': 'general', 'body': 'More plants please'})
    assert r.status_code == 201 and r.json() == {'ok': True}
    listed = hr.get('/suggestions', {}).json()
    assert set(listed) == {'suggestions'}
    s = listed['suggestions'][0]
    assert s['status'] == 'new' and s['reply'] is None and s['isPublic'] is False and s['repliedAt'] is None
    assert ISO_DAY.match(s['receivedOn'])
    assert hr.get('/suggestions', {'status': 'answered'}).json() == {'suggestions': []}
    assert hr.patch(f"/suggestions/{s['id']}", {'status': 'reviewed'}).json() == {'ok': True}
    assert hr.patch(f"/suggestions/{s['id']}", {'reply': 'Done', 'isPublic': True}).json() == {'ok': True}
    pub = sita.get('/suggestions/public').json()['suggestions'][0]
    assert pub['reply'] == 'Done' and ISO_TS.match(pub['repliedAt'])
