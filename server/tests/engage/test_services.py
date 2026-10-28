"""Port of apps/api/test/engage.test.ts at the service level (the insights blocks belong to the insights slice)."""
from datetime import date, timedelta

import pytest

from engines.bs_calendar import BsDate, ad_to_bs, bs_to_ad
from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.engage import services as S
from hajiri.engage.surveys import check_answers, enps_of, run_survey_jobs

today = S.npt_today


def add_days(ad: str, n: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=n)).isoformat()


def ids(items):
    return [i['id'] for i in items]


def raises(exc, fn, *a, **kw):
    with pytest.raises(exc) as e:
        fn(*a, **kw)
    return e.value


# ================================================================ announcements

def post_all_hands(w):
    return S.post_announcement(w.hr, {'title': 'Dashain holidays', 'body': 'Office closed from 1 to 5 Kartik.', 'pinned': True,
                                      'requiresAck': True})


def test_hr_posts_to_everyone_people_are_notified_and_see_it(world):
    w = world
    r = post_all_hands(w)
    ann = r['id']
    assert r['notified'] >= 7
    item = next(i for i in S.feed(w.sita)['items'] if i['id'] == ann)
    assert item['type'] == 'announcement' and item['pinned'] and item['requiresAck'] and not item['acked'] and not item['canEdit']
    n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'engage.announcement' AND payload->>'announcementId' = %s", [ann])
    assert n['n'] >= 7
    assert S.home(w.sita)['announcement']['id'] == ann
    assert 'create' in [a['action'] for a in rows("SELECT action FROM audit_log WHERE row_id = %s", [ann])]


def test_tracks_who_confirmed_reading_only_author_or_hr_sees_list(world):
    w = world
    ann = post_all_hands(w)['id']
    assert S.ack_announcement(w.sita, ann) == {'ok': True}
    acks = S.announcement_acks(w.hr, ann)
    assert acks['acked'] == 1
    assert acks['people'][-1]['employeeId'] == w.sita_id
    e = raises(ForbiddenError, S.announcement_acks, w.ram, ann)
    assert e.message == 'only the author or HR can see confirmations'
    e = raises(ForbiddenError, S.edit_announcement, w.ram, ann, {'title': 'x'})
    assert e.message == 'only the author or HR can edit this'


def test_managers_post_only_to_their_own_team(world):
    w = world
    assert raises(ForbiddenError, S.post_announcement, w.ram, {'title': 'All hands', 'body': 'x'}).message == \
        'managers post announcements to their own team'
    assert raises(ForbiddenError, S.post_announcement, w.sita, {'title': 'Hi', 'body': 'x', 'audienceKind': 'reports'}).message == \
        'only HR and managers post announcements'
    r = S.post_announcement(w.ram, {'title': 'Team lunch', 'body': 'Friday 1 pm', 'audienceKind': 'reports', 'pinned': True})
    assert r['notified'] == 2  # Sita and Person 0
    mine = S.list_announcements(w.extra[0].user)
    assert r['id'] in ids(mine)
    assert next(a for a in mine if a['id'] == r['id'])['pinned'] is False  # only HR pins
    assert next(a for a in mine if a['id'] == r['id'])['audienceLabel'] == "Ram Thapa's team"
    assert r['id'] not in ids(S.list_announcements(w.extra[1].user))
    assert raises(NotFoundError, S.ack_announcement, w.extra[1].user, r['id']).message == 'announcement not found'


def test_department_audiences_reach_that_department_only_other_companies_never(world):
    w = world
    r = S.post_announcement(w.hr, {'title': 'Ops drill', 'body': 'Fire drill at 3', 'audienceKind': 'department', 'audienceId': w.dept_b})
    assert r['notified'] == 2
    assert r['id'] in ids(S.list_announcements(w.extra[4].user))
    assert r['id'] not in ids(S.list_announcements(w.sita))
    assert S.list_announcements(w.foreign_hr) == []
    e = raises(BadRequestError, S.post_announcement, w.foreign_hr, {'title': 'x', 'body': 'y', 'audienceKind': 'department',
                                                                     'audienceId': w.dept_b})
    assert e.message == 'pick the branch or department this is for'


def test_announcement_rules_end_date_ack_and_delete(world):
    w = world
    e = raises(BadRequestError, S.post_announcement, w.hr, {'title': 'x', 'body': 'y', 'expiresOn': add_days(today(), -1)})
    assert e.message == 'the end date has already passed'
    plain = S.post_announcement(w.hr, {'title': 'News', 'body': 'Hello'})['id']
    assert raises(BadRequestError, S.ack_announcement, w.sita, plain).message == 'this announcement does not ask for confirmation'
    assert raises(ForbiddenError, S.delete_announcement, w.sita, plain).message == 'only the author or HR can remove this'
    assert raises(ValidationError, S.edit_announcement, w.hr, plain, {'audienceKind': 'all'})
    S.edit_announcement(w.hr, plain, {'pinned': True, 'expiresOn': add_days(today(), 3)})
    a = one('SELECT pinned, expires_on, popup FROM announcement WHERE id = %s', [plain])
    assert a['pinned'] is True and a['expires_on'].isoformat() == add_days(today(), 3)
    S.delete_announcement(w.hr, plain)
    assert plain not in ids(S.list_announcements(w.sita))
    assert raises(NotFoundError, S.delete_announcement, w.hr, plain).message == 'announcement not found'


def test_popup_is_hr_only(world):
    w = world
    r = S.post_announcement(w.ram, {'title': 'Team', 'body': 'x', 'audienceKind': 'reports', 'popup': True,
                                    'popupUntil': add_days(today(), 2)})
    a = one('SELECT popup, popup_until FROM announcement WHERE id = %s', [r['id']])
    assert a['popup'] is False and a['popup_until'] is None
    r = S.post_announcement(w.hr, {'title': 'All', 'body': 'x', 'popup': True, 'popupUntil': add_days(today(), 2)})
    a = one('SELECT popup, popup_until FROM announcement WHERE id = %s', [r['id']])
    assert a['popup'] is True and a['popup_until'].isoformat() == add_days(today(), 2)


def test_hr_lists_expired_and_other_teams_with_all(world):
    w = world
    team = S.post_announcement(w.ram, {'title': 'Team lunch', 'body': 'x', 'audienceKind': 'reports'})['id']
    execute("UPDATE announcement SET expires_on = (now() AT TIME ZONE 'Asia/Kathmandu')::date - 1 WHERE id = %s", [team])
    assert team not in ids(S.list_announcements(w.hr))
    assert team in ids(S.list_announcements(w.hr, {'all': '1'}))
    assert team not in ids(S.list_announcements(w.sita, {'all': '1'}))


# ================================================================ kudos

def give_first(w):
    values = S.list_values(w.sita)['values']
    return values, S.give_kudos(w.sita, {'recipientIds': [w.ram_id, w.extra[0].id], 'valueId': values[0]['id'],
                                         'message': 'Thanks for the release help'})['id']


def test_kudos_needs_a_colleague_never_yourself_and_notifies(world):
    w = world
    values = S.list_values(w.sita)['values']
    assert len(values) >= 5
    assert raises(BadRequestError, S.give_kudos, w.sita, {'recipientIds': [w.sita_id], 'message': 'Me!'}).message == \
        'You cannot give kudos to yourself.'
    k = S.give_kudos(w.sita, {'recipientIds': [w.ram_id, w.extra[0].id], 'valueId': values[0]['id'],
                              'message': 'Thanks for the release help'})['id']
    n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'engage.kudos' AND channel = 'in_app'")
    assert n['n'] == 2
    push = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'engage.kudos' AND channel = 'push'")
    assert push['n'] == 2
    first = S.list_kudos(w.extra[3].user)[0]
    assert first['id'] == k and first['canDelete'] is False and first['giver']['employeeId'] == w.sita_id
    assert len(first['recipients']) == 2
    assert raises(BadRequestError, S.give_kudos, w.sita, {'recipientIds': [w.ram_id, '00000000-0000-7000-8000-00000000ffff'],
                                                          'message': 'Hello there'}).message == 'Pick colleagues from your company.'
    assert raises(ForbiddenError, S.give_kudos, w.hr, {'recipientIds': [w.ram_id], 'message': 'From HR acct'}).message == \
        'only employees can give kudos'
    assert raises(ValidationError, S.give_kudos, w.sita, {'recipientIds': [w.ram_id], 'message': 'hi'})


def test_reactions_toggle_and_comments_notify_people_involved(world):
    w = world
    _, k = give_first(w)
    on = S.react(w.ram, {'targetType': 'kudos', 'targetId': k, 'emoji': 'clap'})
    assert on['reacted'] is True and on['reactions'] == {'clap': 1} and on['mine'] == ['clap']
    off = S.react(w.ram, {'targetType': 'kudos', 'targetId': k, 'emoji': 'clap'})
    assert off['reacted'] is False and off['reactions'] == {}
    raises(ValidationError, S.react, w.ram, {'targetType': 'kudos', 'targetId': k, 'emoji': 'poop'})
    c = S.add_comment(w.extra[2].user, {'targetType': 'kudos', 'targetId': k, 'body': 'Well deserved!'})
    notes = rows("SELECT user_id, title, body FROM notification_outbox WHERE template = 'engage.comment'")
    assert {str(n['user_id']) for n in notes} == {w.sita.id, w.ram.id, w.extra[0].user.id}
    assert notes[0]['title'] == 'New comment on kudos'
    assert notes[0]['body'] == f'{w.extra[2].user.name}: Well deserved!'
    lst = S.list_comments(w.sita, {'targetType': 'kudos', 'targetId': k})
    assert lst[0]['body'] == 'Well deserved!' and lst[0]['canDelete'] is False
    assert raises(ForbiddenError, S.delete_comment, w.sita, c['id']).message == 'you can only remove your own comments'
    assert S.delete_comment(w.extra[2].user, c['id'])['ok'] is True
    assert raises(NotFoundError, S.list_comments, w.foreign_hr, {'targetType': 'kudos', 'targetId': k}).message == 'not found'


def test_hr_removing_someone_elses_comment_is_audited(world):
    w = world
    _, k = give_first(w)
    c = S.add_comment(w.ram, {'targetType': 'kudos', 'targetId': k, 'body': 'Nice'})
    S.delete_comment(w.hr, c['id'])
    assert [a['action'] for a in rows('SELECT action FROM audit_log WHERE row_id = %s', [c['id']])] == ['delete']
    assert raises(NotFoundError, S.delete_comment, w.hr, c['id']).message == 'comment not found'


def test_enforces_the_monthly_limit_and_leaderboard_counts_the_month(world):
    w = world
    give_first(w)
    assert raises(ForbiddenError, S.save_settings, w.sita, {'kudosMonthlyLimit': 2}).message == 'requires role hr'
    assert S.save_settings(w.hr, {'kudosMonthlyLimit': 2}) == {'ok': True}
    S.give_kudos(w.sita, {'recipientIds': [w.extra[1].id], 'message': 'Second one'})
    e = raises(ConflictError, S.give_kudos, w.sita, {'recipientIds': [w.extra[1].id], 'message': 'Third one'})
    assert e.message == 'You have given all 2 kudos for this month. You can give more next month.'
    lb = S.leaderboard(w.ram)
    assert lb['total'] == 2
    assert lb['given'][0]['employeeId'] == w.sita_id and lb['given'][0]['count'] == 2
    assert lb['received'][0]['count'] == 1
    assert S.list_values(w.hr)['kudosMonthlyLimit'] == 2
    S.save_settings(w.hr, {'kudosMonthlyLimit': None})
    assert S.list_values(w.hr)['kudosMonthlyLimit'] is None
    assert raises(BadRequestError, S.leaderboard, w.ram, {'bsYear': '2100', 'bsMonth': '12'}).message == \
        'that month is outside the supported calendar'


def test_only_the_giver_or_hr_removes_kudos(world):
    w = world
    _, k = give_first(w)
    assert raises(ForbiddenError, S.delete_kudos, w.ram, k).message == 'only the giver or HR can remove kudos'
    assert S.delete_kudos(w.hr, k) == {'ok': True}
    assert raises(NotFoundError, S.delete_kudos, w.hr, k).message == 'kudos not found'


def test_values_are_managed_by_hr(world):
    w = world
    assert raises(ForbiddenError, S.create_value, w.sita, {'name': 'Speed'}).message == 'requires role hr'
    v = S.create_value(w.hr, {'name': 'Speed', 'emoji': '⚡'})['id']
    assert S.update_value(w.hr, v, {'active': False}) == {'ok': True}
    assert raises(BadRequestError, S.give_kudos, w.ram, {'recipientIds': [w.sita_id], 'valueId': v, 'message': 'Fast work'}).message == \
        'Pick one of the company values.'
    assert raises(NotFoundError, S.update_value, w.foreign_hr, v, {'active': True}).message == 'value not found'


def test_kudos_filtered_by_employee(world):
    w = world
    _, k = give_first(w)
    assert ids(S.list_kudos(w.sita, {'employeeId': w.extra[0].id})) == [k]
    assert ids(S.list_kudos(w.sita, {'employeeId': w.extra[4].id})) == []


# ================================================================ celebrations

def test_birthdays_only_for_people_who_opted_in_anniversaries_and_joiners_always(world):
    w = world
    t = today()
    bs = ad_to_bs(t)
    dob = bs_to_ad(BsDate(bs.year - 30, bs.month, bs.day))
    joined = bs_to_ad(BsDate(bs.year - 3, bs.month, bs.day))
    execute('UPDATE employee SET dob = %s, join_date = %s WHERE id = %s', [dob, joined, w.sita_id])
    execute('UPDATE employee SET join_date = %s WHERE id = %s', [add_days(t, -3), w.extra[4].id])
    c = S.celebrations_for(w.ram)
    assert not [x for x in c if x['kind'] == 'birthday']
    ann = next(x for x in c if x['kind'] == 'anniversary' and x['employeeId'] == w.sita_id)
    assert ann['years'] == 3 and ann['daysAway'] == 0
    assert [x for x in c if x['kind'] == 'joined' and x['employeeId'] == w.extra[4].id]
    e = raises(NotFoundError, S.add_comment, w.ram, {'targetType': 'celebration', 'targetId': f'birthday:{w.sita_id}:{bs.year}',
                                                     'body': 'Happy birthday'})
    assert e.message == 'not found'

    assert S.get_prefs(w.sita) == {'showBirthday': False, 'hasBirthday': True}
    assert S.save_prefs(w.sita, {'showBirthday': True}) == {'ok': True}
    c = S.celebrations_for(w.ram)
    bday = next(x for x in c if x['kind'] == 'birthday')
    assert bday['employeeId'] == w.sita_id and bday['daysAway'] == 0
    assert bday['years'] is None  # age is never shown
    S.add_comment(w.ram, {'targetType': 'celebration', 'targetId': bday['targetId'], 'body': 'Happy birthday!'})
    wish = one("SELECT user_id, title FROM notification_outbox WHERE template = 'engage.comment'")
    assert str(wish['user_id']) == w.sita.id and wish['title'] == 'Someone sent you a wish'
    kinds = [x['kind'] for x in S.home(w.extra[2].user)['celebrations']]
    assert 'birthday' in kinds and 'anniversary' in kinds
    assert next(x for x in S.celebrations_for(w.sita) if x['kind'] == 'birthday')['isMe'] is True


def test_prefs_need_an_employee(world):
    assert S.get_prefs(world.hr) == {'showBirthday': False, 'hasBirthday': False}
    assert raises(ForbiddenError, S.save_prefs, world.hr, {'showBirthday': True}).message == 'only employees have celebration settings'


def test_celebration_target_ids_are_checked(world):
    w = world
    assert not S.celebration_target_ok(w.company_id, 'joined:nonsense')
    assert S.celebration_target_ok(w.company_id, f'joined:{w.sita_id}')
    assert not S.celebration_target_ok(str(w.other.id), f'joined:{w.sita_id}')
    assert not S.celebration_target_ok(w.company_id, f'birthday:{w.sita_id}:2083')


# ================================================================ surveys and polls

QUESTIONS = [
    {'kind': 'enps', 'prompt': 'How likely are you to recommend CoreValley as a place to work?'},
    {'kind': 'rating', 'prompt': 'I have what I need to do my job well.'},
    {'kind': 'multi', 'prompt': 'What would help most?', 'options': ['Training', 'Tools', 'Flexibility']},
    {'kind': 'text', 'prompt': 'Anything else?', 'required': False},
]


def make_pulse(w):
    sid = S.create_survey(w.hr, {'title': 'Pulse', 'anonymous': True, 'minGroup': 3, 'opensOn': today(),
                                 'closesOn': add_days(today(), 7), 'questions': QUESTIONS})['id']
    return sid


def answer(q, enps, rating, text=None):
    out = [{'questionId': q[0]['id'], 'value': enps}, {'questionId': q[1]['id'], 'value': rating},
           {'questionId': q[2]['id'], 'choices': ['Tools']}]
    if text:
        out.append({'questionId': q[3]['id'], 'text': text})
    return {'answers': out}


def test_only_hr_creates_surveys_and_the_shape_is_validated(world):
    w = world
    t = today()
    assert raises(ForbiddenError, S.create_survey, w.sita, {'title': 'x', 'opensOn': t, 'closesOn': t, 'questions': QUESTIONS}).message == \
        'requires role hr'
    e = raises(ValidationError, S.create_survey, w.hr, {'kind': 'poll', 'title': 'x', 'opensOn': t, 'closesOn': t, 'questions': QUESTIONS})
    assert any(i['message'] == 'a poll has exactly one choice question' for i in e.details)
    e = raises(ValidationError, S.create_survey, w.hr, {'title': 'x', 'opensOn': t, 'closesOn': t,
                                                        'questions': [{'kind': 'single', 'prompt': 'Pick', 'options': ['Only one']}]})
    assert e.details == [{'path': 'questions.0.options', 'message': 'choice questions need at least two options'}]
    e = raises(ValidationError, S.create_survey, w.hr, {'title': 'x', 'opensOn': t, 'closesOn': add_days(t, -1), 'questions': QUESTIONS})
    assert e.details == [{'path': 'closesOn', 'message': 'the closing day must be on or after the opening day'}]
    sid = make_pulse(w)
    # A draft is invisible to employees.
    assert raises(NotFoundError, S.get_survey, w.sita, sid).message == 'survey not found'
    pub = S.publish_survey(w.hr, sid)
    assert pub['notified'] >= 7
    assert raises(ValidationError, S.update_survey, w.hr, sid, {'title': 'Changed', 'opensOn': t, 'closesOn': t, 'questions': QUESTIONS})
    assert raises(ConflictError, S.publish_survey, w.hr, sid).message == 'this survey is already published'
    note = one("SELECT title, body FROM notification_outbox WHERE template = 'engage.survey_open' LIMIT 1")
    assert note['title'] == 'New survey' and note['body'].startswith('Pulse (anonymous). Open until ')


def test_anonymous_answers_are_not_linked_and_results_hidden_below_minimum(world):
    w = world
    sid = make_pulse(w)
    S.publish_survey(w.hr, sid)
    s = S.get_survey(w.sita, sid)
    q = s['questions']
    assert raises(BadRequestError, S.respond, w.sita, sid, {'answers': [{'questionId': q[0]['id'], 'value': 11}]}).message == \
        'Pick a score from 0 to 10 for: How likely are you to recommend CoreValley as a place to work?'
    assert raises(BadRequestError, S.respond, w.sita, sid, {'answers': [{'questionId': q[0]['id'], 'value': 9}]}).message == \
        'Please answer: I have what I need to do my job well.'  # required missing
    assert S.respond(w.sita, sid, answer(q, 10, 5, 'Great team')) == {'ok': True}
    assert raises(ConflictError, S.respond, w.sita, sid, answer(q, 10, 5)).message == 'You have already answered this.'
    S.respond(w.ram, sid, answer(q, 3, 2, 'More tools please'))
    res = S.results(w.hr, sid)['results']
    assert res['responses'] == 2 and res['hidden'] is True and res['questions'] == []
    assert raises(NotFoundError, S.results, w.sita, sid).message == 'results are not available to you'
    S.respond(w.extra[0].user, sid, answer(q, 8, 4))
    res = S.results(w.hr, sid)['results']
    assert res['responses'] == 3 and res['hidden'] is True and res['hiddenUntilClose'] is True and res['questions'] == []
    execute("UPDATE survey SET status = 'closed' WHERE id = %s", [sid])  # results of an anonymous survey show once it has closed
    res = S.results(w.hr, sid)['results']
    assert res['hidden'] is False
    enps = res['questions'][0]
    assert enps['enps'] == {'score': 0, 'promoters': 1, 'passives': 1, 'detractors': 1}
    assert res['questions'][1]['average'] == pytest.approx(3.67, abs=0.005)
    assert res['questions'][2]['distribution'] == [{'label': 'Training', 'count': 0}, {'label': 'Tools', 'count': 3},
                                                   {'label': 'Flexibility', 'count': 0}]
    texts = res['questions'][3]['texts']
    assert len(texts) == 2
    for t in texts:
        assert list(t.keys()) == ['text']
    # All three are in Engineering, which reaches the minimum; a single group gives no breakdown.
    assert enps['breakdown'] == []
    stored = rows('SELECT employee_id, respondent_key FROM survey_response WHERE survey_id = %s', [sid])
    assert all(r['employee_id'] is None for r in stored)
    assert not any(w.sita_id in r['respondent_key'] for r in stored)
    csv = S.results_csv(w.hr, sid)
    assert 'More tools please' in csv
    assert 'Sita' not in csv
    assert csv.startswith('﻿Survey,Pulse\r\n')
    execute("UPDATE survey SET status = 'published' WHERE id = %s", [sid])
    lst = S.open_surveys_for(w.sita)
    assert next(x for x in lst if x['id'] == sid)['answered'] is True
    assert raises(NotFoundError, S.results, w.foreign_hr, sid).message == 'survey not found'


def test_poll_shows_results_to_voters_and_audiences_are_respected(world):
    w = world
    t = today()
    poll = S.create_survey(w.hr, {'kind': 'poll', 'title': 'Friday snacks', 'anonymous': False, 'audienceKind': 'department',
                                  'audienceId': w.dept_b, 'opensOn': t, 'closesOn': t,
                                  'questions': [{'kind': 'single', 'prompt': 'Which snack?', 'options': ['Momo', 'Samosa']}]})['id']
    S.publish_survey(w.hr, poll)
    q = S.get_survey(w.extra[3].user, poll)['questions'][0]
    raises(NotFoundError, S.get_survey, w.sita, poll)
    assert raises(NotFoundError, S.respond, w.sita, poll, {'answers': [{'questionId': q['id'], 'choices': ['Momo']}]}).message == \
        'this survey is not open to you'
    raises(NotFoundError, S.results, w.extra[3].user, poll)
    v = S.respond(w.extra[3].user, poll, {'answers': [{'questionId': q['id'], 'choices': ['Momo']}]})
    assert v['results']['questions'][0]['distribution'][0] == {'label': 'Momo', 'count': 1}
    assert S.results(w.extra[3].user, poll)['results']['responses'] == 1
    assert raises(BadRequestError, S.respond, w.extra[4].user, poll, {'answers': [{'questionId': q['id'], 'choices': ['Pizza']}]}).message == \
        'Pick one option for: Which snack?'
    stored = rows('SELECT employee_id FROM survey_response WHERE survey_id = %s', [poll])
    assert str(stored[0]['employee_id']) == w.extra[3].id  # not anonymous: the name is kept


def test_daily_job_closes_repeats_a_recurring_pulse_and_reminds(world):
    w = world
    t = today()
    sid = S.create_survey(w.hr, {'title': 'Weekly pulse', 'recurrence': 'weekly', 'opensOn': add_days(t, -6), 'closesOn': add_days(t, 1),
                                 'questions': [{'kind': 'rating', 'prompt': 'How was your week?'}]})['id']
    S.publish_survey(w.hr, sid)
    q = S.get_survey(w.sita, sid)['questions'][0]
    S.respond(w.sita, sid, {'answers': [{'questionId': q['id'], 'value': 4}]})
    first = run_survey_jobs(t)
    assert first['reminded'] == 6  # 7 in the audience, Sita already answered
    rem = one("SELECT title, body FROM notification_outbox WHERE template = 'engage.survey_reminder' LIMIT 1")
    assert rem == {'title': 'Survey closes tomorrow', 'body': 'You have not answered "Weekly pulse" yet. It takes a minute.'}
    again = run_survey_jobs(t)
    assert again['reminded'] == 0
    later = run_survey_jobs(add_days(t, 2))
    assert later['closed'] >= 1
    assert later['started'] == 1
    nxt = rows('SELECT id, opens_on, closes_on, status, series_id FROM survey WHERE series_id = %s', [sid])
    assert nxt[0]['status'] == 'published'
    assert nxt[0]['opens_on'].isoformat() == add_days(t, 1) and nxt[0]['closes_on'].isoformat() == add_days(t, 8)
    assert one('SELECT count(*)::int AS n FROM survey_question WHERE survey_id = %s', [nxt[0]['id']])['n'] == 1
    assert later['opened'] >= 1


def test_closing_by_hand_stops_the_series_drafts_can_be_deleted(world):
    w = world
    t = today()
    sid = make_pulse(w)
    S.publish_survey(w.hr, sid)
    q = S.get_survey(w.sita, sid)['questions']
    for u in (w.sita, w.ram, w.extra[0].user):
        S.respond(u, sid, answer(q, 8, 4))
    d = S.create_survey(w.hr, {'title': 'Draft', 'opensOn': t, 'closesOn': t, 'questions': [{'kind': 'text', 'prompt': 'Hi?'}]})['id']
    assert S.delete_survey(w.hr, d) == {'ok': True}
    assert S.close_survey(w.hr, sid) == {'ok': True}
    assert raises(ConflictError, S.delete_survey, w.hr, sid).message == 'only drafts can be deleted; close a published survey instead'
    assert raises(ConflictError, S.close_survey, w.hr, sid).message == 'only an open survey can be closed'
    assert raises(ConflictError, S.update_survey, w.hr, sid, {'closesOn': t}).message == 'this survey is closed'
    m = next(s for s in S.manage_surveys(w.hr) if s['id'] == sid)
    assert m['status'] == 'closed' and m['responses'] == 3 and m['recurrence'] == 'none'
    assert raises(ForbiddenError, S.manage_surveys, w.ram).message == 'requires role hr'


def test_published_survey_moves_only_its_closing_day(world):
    w = world
    t = today()
    sid = make_pulse(w)
    S.publish_survey(w.hr, sid)
    assert S.update_survey(w.hr, sid, {'closesOn': add_days(t, 10)}) == {'ok': True}
    assert one('SELECT closes_on FROM survey WHERE id = %s', [sid])['closes_on'].isoformat() == add_days(t, 10)
    assert raises(BadRequestError, S.update_survey, w.hr, sid, {'closesOn': add_days(t, -1)}).message == \
        'the new closing day must be today or later'
    raises(ValidationError, S.update_survey, w.hr, sid, {'closesOn': t, 'title': 'x'})


def test_draft_edit_replaces_questions_and_past_close_cannot_publish(world):
    w = world
    t = today()
    sid = make_pulse(w)
    S.update_survey(w.hr, sid, {'title': 'Pulse 2', 'opensOn': t, 'closesOn': t, 'questions': [{'kind': 'rating', 'prompt': 'Ok?'}]})
    full = S.get_survey(w.hr, sid)
    assert full['survey']['title'] == 'Pulse 2' and [q['prompt'] for q in full['questions']] == ['Ok?']
    execute('UPDATE survey SET opens_on = %s, closes_on = %s WHERE id = %s', [add_days(t, -3), add_days(t, -1), sid])
    assert raises(BadRequestError, S.publish_survey, w.hr, sid).message == 'the closing day has passed; change the dates first'


def test_check_answers_and_enps_rules():
    qs = [{'id': 'a', 'kind': 'single', 'prompt': 'One', 'options': ['x', 'y'], 'required': True},
          {'id': 'b', 'kind': 'multi', 'prompt': 'Many', 'options': ['x', 'y'], 'required': False}]
    with pytest.raises(BadRequestError, match='Each question can be answered once.'):
        check_answers(qs, [{'questionId': 'a', 'choices': ['x']}, {'questionId': 'a', 'choices': ['y']}])
    with pytest.raises(BadRequestError, match='An answer refers to a question that is not in this survey.'):
        check_answers(qs, [{'questionId': 'z', 'choices': ['x']}])
    assert check_answers(qs, [{'questionId': 'a', 'choices': ['x']}, {'questionId': 'b', 'choices': ['y', 'y', 'x']}]) == [
        {'questionId': 'a', 'value': None, 'choices': ['x'], 'text': None},
        {'questionId': 'b', 'value': None, 'choices': ['y', 'x'], 'text': None}]
    with pytest.raises(BadRequestError, match='Pick from the listed options for: Many'):
        check_answers(qs, [{'questionId': 'a', 'choices': ['x']}, {'questionId': 'b', 'choices': ['q']}])
    assert enps_of([]) is None
    assert enps_of([10, 9, 0]) == {'score': 33, 'promoters': 2, 'passives': 0, 'detractors': 1}


def test_department_breakdown_pools_small_groups(world):
    w = world
    t = today()
    sid = S.create_survey(w.hr, {'title': 'Named', 'anonymous': False, 'minGroup': 3, 'opensOn': t, 'closesOn': t,
                                 'questions': [{'kind': 'rating', 'prompt': 'Ok?'}, {'kind': 'text', 'prompt': 'Why?', 'required': False}]})['id']
    S.publish_survey(w.hr, sid)
    q = S.get_survey(w.hr, sid)['questions']
    for u, v in ((w.sita, 5), (w.ram, 4), (w.extra[0].user, 3), (w.extra[1].user, 2)):
        S.respond(u, sid, {'answers': [{'questionId': q[0]['id'], 'value': v}]})
    S.respond(w.extra[3].user, sid, {'answers': [{'questionId': q[0]['id'], 'value': 1}, {'questionId': q[1]['id'], 'text': 'Busy'}]})
    res = S.results(w.hr, sid)['results']
    # Engineering (4) is shown; Operations (1) is below the minimum and too small to pool on its own.
    assert res['questions'][0]['breakdown'] == []
    assert res['questions'][1]['texts'] == [{'text': 'Busy', 'name': 'Person 3'}]


# ================================================================ suggestion box

def test_suggestion_box_stores_no_author_hr_replies_and_shares(world):
    w = world
    assert S.suggest(w.sita, {'category': 'workplace', 'body': 'Could we get a water filter on the second floor?'}) == {'ok': True}
    names = [c['column_name'] for c in rows("SELECT column_name FROM information_schema.columns WHERE table_name = 'suggestion'")]
    assert 'employee_id' not in names and 'created_at' not in names
    assert raises(ForbiddenError, S.list_suggestions, w.sita).message == 'requires role hr'
    lst = S.list_suggestions(w.hr)
    assert len(lst) == 1
    sid = lst[0]['id']
    assert S.update_suggestion(w.hr, sid, {'reply': 'Ordered, arriving next week.', 'isPublic': True}) == {'ok': True}
    pub = S.public_suggestions(w.ram)
    assert pub[0]['reply'] == 'Ordered, arriving next week.'
    assert S.list_suggestions(w.hr, {'status': 'answered'})[0]['status'] == 'answered'
    for i in range(5):
        S.suggest(w.ram, {'body': f'Idea number {i}'})
    assert raises(ConflictError, S.suggest, w.ram, {'body': 'One idea too many'}).message == \
        'You have sent several suggestions today. Please try again tomorrow.'
    assert S.public_suggestions(w.foreign_hr) == []
    assert raises(NotFoundError, S.update_suggestion, w.foreign_hr, sid, {'status': 'archived'}).message == 'suggestion not found'
