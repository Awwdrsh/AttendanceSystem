"""Engage pages and HTMX fragments through the Django test client (the TS route cases as the browser now makes them)."""
import json
from datetime import date, timedelta

from engines.bs_calendar import BsDate, ad_to_bs, bs_to_ad
from hajiri.core.db import execute, one, rows
from hajiri.engage import services as S
from tests.web.factories import htmx, hx_triggers, set_features_off, toasts

from .conftest import client_for


def add_days(ad, n):
    return (date.fromisoformat(ad) + timedelta(days=n)).isoformat()


def test_feed_page_shows_actions_by_role(world):
    w = world
    hr = client_for(w.hr).get('/engage')
    assert hr.status_code == 200
    body = hr.content.decode()
    assert 'Kudos settings' in body and 'ti-speakerphone' in body and 'Give kudos' not in body  # HR account has no employee
    sita = client_for(w.sita).get('/engage').content.decode()
    assert 'Give kudos' in sita and 'ti-speakerphone" aria-hidden="true"></i>Announce' not in sita and 'Kudos settings' not in sita
    assert 'Say thanks, catch up on company news' in sita
    ram = client_for(w.ram).get('/engage').content.decode()
    assert 'ti-speakerphone" aria-hidden="true"></i>Announce' in ram


def test_hr_posts_an_announcement_and_people_see_it(world):
    w = world
    c = client_for(w.hr)
    r = c.post('/_/engage/announcements/new', {'title': 'Dashain holidays', 'body': 'Office closed.', 'pinned': '1',
                                                'requires_ack': '1'}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == 'Posted. 7 people were notified.'
    assert 'hj:refresh-engage-feed' in hx_triggers(r)
    sita = client_for(w.sita)
    page = sita.get('/engage').content.decode()
    assert 'Dashain holidays' in page and 'I have read this' in page and 'Pinned' in page
    ann = one('SELECT id FROM announcement')['id']
    r = sita.post(f'/_/engage/announcements/{ann}/ack', **htmx())
    assert r.status_code == 200 and b'You confirmed you have read this.' in r.content
    r = c.get(f'/_/engage/announcements/{ann}/acks', **htmx())
    assert r.status_code == 200 and b'1 of 7 confirmed' in r.content
    r = client_for(w.ram).get(f'/_/engage/announcements/{ann}/acks', **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only the author or HR can see confirmations'
    r = client_for(w.ram).post(f'/_/engage/announcements/{ann}/pin', {'pinned': '0'}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only the author or HR can edit this'
    r = c.post(f'/_/engage/announcements/{ann}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Announcement removed.'


def test_manager_form_and_refusals(world):
    w = world
    ram = client_for(w.ram)
    form = ram.get('/_/engage/announcements/new', **htmx()).content.decode()
    assert 'This goes to the people who report to you.' in form and 'A branch' not in form
    r = ram.post('/_/engage/announcements/new', {'title': 'Lunch', 'body': 'Friday', 'audience_kind': 'all'}, **htmx())
    assert r.status_code == 403 and toasts(r)[0] == {'message': 'managers post announcements to their own team', 'tone': 'err',
                                                     'title': 'Not posted'}
    r = ram.post('/_/engage/announcements/new', {'title': 'Lunch', 'body': 'Friday'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Posted. 2 people were notified.'
    r = client_for(w.sita).get('/_/engage/announcements/new', **htmx())
    assert r.status_code == 403


def test_kudos_flow_reactions_and_comments(world):
    w = world
    sita = client_for(w.sita)
    form = sita.get('/_/engage/kudos/new', **htmx()).content.decode()
    assert 'Ram Thapa' in form and 'Sita Sharma' not in form and 'Ownership' in form
    r = sita.post('/_/engage/kudos/new', {'recipient_ids': [w.sita_id], 'message': 'Me!'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'You cannot give kudos to yourself.'
    assert toasts(r)[0]['title'] == 'Kudos not sent'
    r = sita.post('/_/engage/kudos/new', {'recipient_ids': [w.ram_id, w.extra[0].id], 'message': 'Thanks for the release help'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Kudos sent. They have been notified.'
    assert {'hj:refresh-engage-feed', 'hj:refresh-engage-leaderboard'} <= set(hx_triggers(r))
    k = str(one('SELECT id FROM kudos')['id'])
    ram = client_for(w.ram)
    r = ram.post('/_/engage/react', {'target_type': 'kudos', 'target_id': k, 'emoji': 'clap'}, **htmx())
    assert r.status_code == 200 and 'aria-pressed="true"' in r.content.decode() and 'Applause, 1' in r.content.decode()
    r = ram.post('/_/engage/react', {'target_type': 'kudos', 'target_id': k, 'emoji': 'poop'}, **htmx())
    assert r.status_code == 400
    r = ram.post('/_/engage/comments', {'target_type': 'kudos', 'target_id': k, 'body': 'Well deserved!'}, **htmx())
    assert r.status_code == 200 and b'Well deserved!' in r.content and b'1 comment' in r.content
    r = sita.get(f'/_/engage/comments?target_type=kudos&target_id={k}', **htmx())
    assert b'Well deserved!' in r.content and b'Remove comment' not in r.content
    cid = one('SELECT id FROM engage_comment')['id']
    r = sita.post(f'/_/engage/comments/{cid}/delete', **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'you can only remove your own comments'
    r = client_for(w.foreign_hr).get(f'/_/engage/comments?target_type=kudos&target_id={k}', **htmx())
    assert r.status_code == 404
    feed = client_for(w.extra[3].user).get('/engage').content.decode()
    assert 'Thanks for the release help' in feed and 'Kudos options' not in feed
    r = ram.post(f'/_/engage/kudos/{k}/delete', **htmx())
    assert r.status_code == 403
    r = sita.post(f'/_/engage/kudos/{k}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Kudos removed.'
    lb = ram.get('/_/engage/leaderboard', **htmx())
    assert b'No kudos yet this month' in lb.content


def test_values_and_limit_are_hr_only(world):
    w = world
    assert client_for(w.sita).get('/_/engage/values', **htmx()).status_code == 403
    hr = client_for(w.hr)
    r = hr.post('/_/engage/values/new', {'name': 'Speed', 'emoji': '⚡'}, **htmx())
    assert r.status_code == 200 and 'Speed' in r.content.decode()
    vid = one("SELECT id FROM engage_value WHERE name = 'Speed'")['id']
    r = hr.post(f'/_/engage/values/{vid}/toggle', **htmx())
    assert r.status_code == 200 and one('SELECT active FROM engage_value WHERE id = %s', [vid])['active'] is False
    r = hr.post('/_/engage/settings', {'kudos_monthly_limit': '2'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Saved.'
    assert one('SELECT kudos_monthly_limit FROM engage_setting')['kudos_monthly_limit'] == 2
    assert client_for(w.sita).post('/_/engage/settings', {'kudos_monthly_limit': '3'}, **htmx()).status_code == 403


def test_kudos_feature_off_hides_kudos(world):
    w = world
    set_features_off(w.co, 'kudos')
    page = client_for(w.sita).get('/engage').content.decode()
    assert 'Give kudos' not in page and 'Everything' not in page
    assert client_for(w.sita).get('/_/engage/kudos/new', **htmx()).status_code == 404
    assert client_for(w.sita).get('/_/engage/leaderboard', **htmx()).status_code == 404


def test_celebrations_panel_and_birthday_switch(world):
    w = world
    t = S.npt_today()
    bs = ad_to_bs(t)
    execute('UPDATE employee SET dob = %s, join_date = %s WHERE id = %s',
            [bs_to_ad(BsDate(bs.year - 30, bs.month, bs.day)), bs_to_ad(BsDate(bs.year - 3, bs.month, bs.day)), w.sita_id])
    sita = client_for(w.sita)
    p = sita.get('/_/engage/celebrations', **htmx()).content.decode()
    assert '3 years at the company · Today' in p and 'Show my birthday to colleagues' in p and 'Birthday' not in p
    r = sita.post('/_/engage/prefs', {'show_birthday': '1'}, **htmx())
    assert r.status_code == 204
    p = client_for(w.ram).get('/_/engage/celebrations', **htmx()).content.decode()
    assert 'Birthday · Today' in p and 'Send a wish' in p
    assert client_for(w.hr).post('/_/engage/prefs', {'show_birthday': '1'}, **htmx()).status_code == 403


def test_home_card(world):
    w = world
    sita = client_for(w.sita)
    assert sita.get('/_/engage/home', **htmx()).content == b''
    S.post_announcement(w.hr, {'title': 'Pinned news', 'body': 'Read me', 'pinned': True, 'requiresAck': True})
    card = sita.get('/_/engage/home', **htmx()).content.decode()
    assert 'Pinned news' in card and 'Please read' in card and 'Open Engage' in card


def survey_body(t, **kw):
    return {'title': 'Pulse', 'kind': 'survey', 'anonymous': '1', 'min_group': '3', 'audience_kind': 'all', 'opens_on': t,
            'closes_on': add_days(t, 7), 'recurrence': 'none',
            'questions_json': json.dumps([{'kind': 'rating', 'prompt': 'Ok?', 'options': [], 'required': True},
                                          {'kind': 'multi', 'prompt': 'Help?', 'options': ['Tools', ' Training ', ''], 'required': True}]),
            **kw}


def test_hr_builds_publishes_and_reads_a_survey(world):
    w = world
    t = S.npt_today()
    hr = client_for(w.hr)
    page = hr.get('/engage/surveys').content.decode()
    assert 'No surveys yet' in page and 'New survey or poll' in page
    assert b'Monthly pulse with eNPS' in hr.get('/_/engage/surveys/new', **htmx()).content
    r = hr.post('/_/engage/surveys/new', survey_body(t, kind='poll'), **htmx())
    assert r.status_code == 400 and 'a poll has exactly one choice question' in toasts(r)[0]['message']
    r = hr.post('/_/engage/surveys/new', survey_body(t), **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Saved as a draft. Publish it when you are ready.'
    sid = str(one('SELECT id FROM survey')['id'])
    assert [q['options'] for q in rows('SELECT options FROM survey_question WHERE survey_id = %s ORDER BY position', [sid])][1] == \
        ['Tools', 'Training']
    assert client_for(w.sita).get(f'/_/engage/surveys/{sid}/answer', **htmx()).status_code == 404
    assert b'Edit draft' in hr.get(f'/_/engage/surveys/{sid}/edit', **htmx()).content
    r = hr.post(f'/_/engage/surveys/{sid}/publish', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Published. People in the audience have been notified.'
    sita = client_for(w.sita)
    assert 'Pulse' in sita.get('/engage/surveys').content.decode()
    form = sita.get(f'/_/engage/surveys/{sid}/answer', **htmx()).content.decode()
    assert 'Anonymous. Your name is not stored' in form
    qs = S.get_survey(w.hr, sid)['questions']
    r = sita.post(f'/_/engage/surveys/{sid}/answer', {f"q-{qs[0]['id']}": '6'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Pick a rating from 1 to 5 for: Ok?'
    assert toasts(r)[0]['title'] == 'Not sent'
    r = sita.post(f'/_/engage/surveys/{sid}/answer', {f"q-{qs[0]['id']}": '4', f"q-{qs[1]['id']}": ['Tools']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Thanks, your answers are in.'
    assert 'Answered, thank you' in sita.get('/engage/surveys').content.decode()
    res = hr.get(f'/_/engage/surveys/{sid}/results', **htmx()).content.decode()
    assert 'Results appear once at least 3 people have answered' in res
    assert sita.get(f'/_/engage/surveys/{sid}/results', **htmx()).status_code == 404
    csv = hr.get(f'/_/engage/surveys/{sid}/results.csv')
    assert csv.status_code == 200 and csv['Content-Disposition'] == 'attachment; filename="survey-results.csv"'
    assert 'Results are hidden until at least 3 people answer.' in csv.content.decode()
    assert sita.get(f'/_/engage/surveys/{sid}/results.csv').status_code == 403
    r = hr.post(f'/_/engage/surveys/{sid}/extend', {'closes_on': add_days(t, 9)}, **htmx())
    assert r.status_code == 204
    r = hr.post(f'/_/engage/surveys/{sid}/delete', **htmx())
    assert r.status_code == 409
    r = hr.post(f'/_/engage/surveys/{sid}/close', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Closed.'


def test_poll_vote_shows_results(world):
    w = world
    t = S.npt_today()
    pid = S.create_survey(w.hr, {'kind': 'poll', 'title': 'Snacks', 'anonymous': False, 'opensOn': t, 'closesOn': t,
                                 'questions': [{'kind': 'single', 'prompt': 'Which?', 'options': ['Momo', 'Samosa']}]})['id']
    S.publish_survey(w.hr, pid)
    q = S.get_survey(w.hr, pid)['questions'][0]
    r = client_for(w.sita).post(f'/_/engage/surveys/{pid}/answer', {f"q-{q['id']}": 'Momo'}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Vote counted.'
    assert 'Results: Snacks' in r.content.decode() and 'Momo' in r.content.decode()


def test_surveys_page_needs_the_feature(world):
    set_features_off(world.co, 'surveys')
    r = client_for(world.sita).get('/engage/surveys')
    assert r.status_code == 302


def test_suggestion_box_and_hr_inbox(world):
    w = world
    sita = client_for(w.sita)
    assert b'Anonymous suggestion' in sita.get('/_/engage/suggestions/new', **htmx()).content
    r = sita.post('/_/engage/suggestions/new', {'category': 'workplace', 'body': 'Could we get a water filter?'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Sent anonymously. Thank you.'
    assert sita.get('/engage/suggestions').status_code in (302, 403)
    hr = client_for(w.hr)
    page = hr.get('/engage/suggestions').content.decode()
    assert 'Could we get a water filter?' in page and 'Workplace' in page
    sid = str(one('SELECT id FROM suggestion')['id'])
    r = hr.post(f'/_/engage/suggestions/{sid}/update', {'status': 'reviewed'}, **htmx())
    assert toasts(r)[0]['message'] == 'Marked as reviewed.'
    assert b'Reply to suggestion' in hr.get(f'/_/engage/suggestions/{sid}/reply', **htmx()).content
    r = hr.post(f'/_/engage/suggestions/{sid}/reply', {'reply': 'Ordered.', 'is_public': '1'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Reply saved.'
    assert 'No suggestions waiting' in hr.get('/engage/suggestions').content.decode()
    assert 'Ordered.' in hr.get('/engage/suggestions?status=answered').content.decode()
    box = client_for(w.ram).get('/_/engage/suggestion-box', **htmx()).content.decode()
    assert 'HR answered' in box and 'Ordered.' in box
    for i in range(5):
        client_for(w.ram).post('/_/engage/suggestions/new', {'body': f'Idea number {i}'}, **htmx())
    r = client_for(w.ram).post('/_/engage/suggestions/new', {'body': 'One idea too many'}, **htmx())
    assert r.status_code == 409


def test_nav_tabs(world):
    page = client_for(world.hr).get('/engage').content.decode()
    assert 'Surveys &amp; polls' in page and 'Suggestions' in page


def test_closing_day_form_and_daily_job_registered(world):
    w = world
    t = S.npt_today()
    sid = S.create_survey(w.hr, {'title': 'P', 'opensOn': t, 'closesOn': t, 'questions': [{'kind': 'text', 'prompt': 'Hi?'}]})['id']
    S.publish_survey(w.hr, sid)
    r = client_for(w.hr).get(f'/_/engage/surveys/{sid}/closing-day', **htmx())
    assert r.status_code == 200 and b'Change closing day' in r.content
    from hajiri.worker import registry
    registry.autodiscover()
    assert 'surveys' in [j.name for j in registry.daily_jobs()]
