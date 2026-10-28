"""Pages and HTMX endpoints of the Growth and Performance hubs, through django.test.Client."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from tests.support.sql import one, rows
from hajiri.learning import services as lrn
from hajiri.performance import services as perf
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts

from .world import plus_days, today

pytestmark = pytest.mark.django_db


def client(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def test_growth_pages_render_for_an_employee(w):
    c = client(w.sita)
    for url, text in (('/me/growth', 'No goals yet'), ('/me/growth/reviews', 'No reviews yet'), ('/me/growth/one-on-ones', 'No 1-on-1s scheduled'),
                      ('/me/growth/feedback', 'No feedback yet'), ('/me/growth?view=company', 'No company goals yet')):
        r = c.get(url)
        assert r.status_code == 200, url
        assert text in r.content.decode(), url


def test_performance_hub_is_for_managers_and_hr(w):
    assert client(w.ram).get('/performance').status_code == 200
    assert client(w.hr).get('/performance?view=kpis').status_code == 200
    r = client(w.sita).get('/performance', **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'requires role manager or hr'
    for url in ('/performance/reviews', '/performance/learning'):
        assert client(w.ram).get(url).status_code == 200


def test_feature_switch_off_hides_the_pages(w):
    set_features_off(w.co, 'performance')
    c = client(w.sita)
    assert c.get('/me/growth').status_code == 302
    assert c.get('/_/performance/goals/new', **htmx()).status_code == 404


def test_employee_creates_a_goal_with_key_results_and_checks_in(w):
    c = client(w.sita)
    assert 'New goal' in c.get('/_/performance/goals/new', **htmx('hj-drawer-body')).content.decode()
    r = c.post('/_/performance/goals/new', {'scope': 'individual', 'title': 'Ship the attendance app', 'weight': '60',
                                            'kr_title': ['Screens done', 'Released'], 'kr_measure': ['number', 'boolean'],
                                            'kr_start': ['0', '0'], 'kr_target': ['10', '100'], 'kr_unit': ['', '']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Goal saved.'
    assert 'hj:refresh-perf-main' in hx_triggers(r)
    g = perf.list_goals(w.sita, {'scope': 'mine'})[0]
    assert g['title'] == 'Ship the attendance app' and len(g['key_results']) == 2
    page = c.get('/me/growth').content.decode()
    assert 'Ship the attendance app' in page and 'Average progress' in page
    kr = next(k for k in g['key_results'] if k['title'] == 'Released')
    r = c.post(f"/_/performance/goals/{g['id']}/checkin", {'key_result_id': kr['id'], 'done': 'on', 'done_touched': '1', 'confidence': '3', 'comment': 'Live'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Check-in saved.'
    assert 'Live' in r.content.decode() and 'On track' in r.content.decode()
    assert float(one('SELECT progress FROM perf_goal WHERE id = %s', [g['id']])['progress']) == 50
    # Validation errors come back as a toast, nothing is saved.
    r = c.post('/_/performance/goals/new', {'title': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['tone'] == 'err'


def test_goal_drawer_hidden_from_colleagues(w):
    gid = perf.create_goal(w.sita, {'title': 'Private'})
    assert client(w.hari).get(f'/_/performance/goals/{gid}', **htmx()).status_code == 404
    r = client(w.ram).get(f'/_/performance/goals/{gid}', **htmx())
    assert r.status_code == 200 and 'Check in' in r.content.decode()
    r = client(w.ram).post(f'/_/performance/goals/{gid}/status', {'status': 'completed'}, **htmx())
    assert toasts(r)[0]['message'] == 'Marked complete.'
    r = client(w.sita).post(f'/_/performance/goals/{gid}/cancel', **htmx())
    assert toasts(r)[0]['message'] == 'Goal cancelled.' and 'hj:close-modal' in hx_triggers(r)


def test_team_goals_journal_and_feedback_views(w):
    perf.create_goal(w.sita, {'title': 'Sita goal'})
    c = client(w.ram)
    page = c.get('/performance').content.decode()
    assert 'Sita goal' in page and 'No active goals: Gita Rai, Hari Karki' in page
    r = c.post('/_/performance/journal', {'employee_id': w.SITA, 'note_date': today(), 'tag': 'positive', 'note': 'Handled the outage calmly'}, **htmx())
    assert toasts(r)[0]['message'] == 'Note saved.'
    page = c.get(f'/performance?view=journal&person={w.SITA}').content.decode()
    assert 'Handled the outage calmly' in page and 'Went well' in page
    perf.give_feedback(w.hari, {'toEmployeeId': w.SITA, 'body': 'Great code reviews'})
    assert 'Great code reviews' in c.get(f'/performance?view=feedback&person={w.SITA}').content.decode()
    # The journal post is refused for a colleague.
    r = client(w.hari).post('/_/performance/journal', {'employee_id': w.SITA, 'note_date': today(), 'note': 'x'}, **htmx())
    assert r.status_code == 403


def test_kpi_library_and_apply(w):
    c = client(w.hr)
    r = c.post('/_/performance/kpis/new', {'title': 'Bugs fixed per month', 'designation': 'Engineer', 'measure': 'number', 'default_target': '20', 'active': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'KPI saved.'
    assert 'Bugs fixed per month' in c.get('/performance?view=kpis').content.decode()
    k = perf.list_kpis(w.sita)[0]
    s = client(w.sita)
    assert 'Bugs fixed per month' in s.get('/_/performance/kpis/apply', **htmx()).content.decode()
    r = s.post('/_/performance/kpis/apply', {'kpi_id': k['id'], 'weight': '40'}, **htmx())
    assert toasts(r)[0]['message'] == 'Goal added.'
    assert client(w.sita).post('/_/performance/kpis/new', {'title': 'x'}, **htmx()).status_code == 403


def test_feedback_give_ask_answer_through_pages(w):
    h = client(w.hari)
    assert 'Give feedback' in h.get('/_/performance/feedback/give', **htmx()).content.decode()
    r = h.post('/_/performance/feedback/give', {'to_employee_id': w.SITA, 'body': 'Great code reviews'}, **htmx())
    assert toasts(r)[0]['message'] == 'Feedback sent.'
    assert 'Great code reviews' in client(w.sita).get('/me/growth/feedback').content.decode()
    r = client(w.sita).post('/_/performance/feedback/ask', {'from_employee_ids': [w.GITA], 'prompt': 'How was the launch?'}, **htmx())
    assert toasts(r)[0]['message'] == 'Request sent.' and r.headers['HX-Redirect'].endswith('?box=requests')
    g = client(w.gita)
    page = g.get('/me/growth/feedback?box=requests').content.decode()
    assert 'asked you' in page and 'How was the launch?' in page
    fid = perf.feedback(w.gita, {'box': 'requests'})[0]['id']
    assert g.post(f'/_/performance/feedback/{fid}/answer', {'body': 'Smooth launch'}, **htmx()).status_code == 204
    r = g.post(f'/_/performance/feedback/{fid}/decline', **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'this request was already answered'


def test_one_on_one_schedule_notes_items_and_done(w):
    c = client(w.ram)
    assert 'Sita Sharma (Reports to you)' in c.get('/_/performance/one-on-ones/new', **htmx()).content.decode()
    day = (datetime.now(timezone.utc) + timedelta(minutes=345, days=2)).date().isoformat()
    r = c.post('/_/performance/one-on-ones/new', {'with_employee_id': w.SITA, 'date': day, 'time': '11:00', 'cadence': 'weekly', 'agenda': 'Career plan\n\nBudget'}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == '1-on-1 scheduled.' and r.headers['HX-Retarget'] == '#hj-drawer-body'
    assert 'Career plan' in r.content.decode()
    mid = perf.meetings(w.ram)['meetings'][0]['id']
    s = client(w.sita)
    r = s.post(f'/_/performance/one-on-ones/{mid}/update', {'action': 'notes', 'shared_notes': 'Agreed on Q2 focus', 'private_notes': 'Ask about training budget'}, **htmx())
    assert toasts(r)[0]['message'] == 'Notes saved.' and 'Ask about training budget' in r.content.decode()
    assert 'Ask about training budget' not in c.get(f'/_/performance/one-on-ones/{mid}', **htmx()).content.decode()
    r = s.post(f'/_/performance/one-on-ones/{mid}/items', {'kind': 'action', 'text': 'Draft plan', 'owner_employee_id': w.SITA}, **htmx())
    assert 'Draft plan' in r.content.decode()
    item = rows("SELECT id FROM perf_one_on_one_item WHERE kind = 'action'")[0]['id']
    s.post(f'/_/performance/one-on-one-items/{item}/toggle', {'done': 'on'}, **htmx())
    assert one('SELECT done FROM perf_one_on_one_item WHERE id = %s', [item])['done'] is True
    r = c.post(f'/_/performance/one-on-ones/{mid}/update', {'action': 'done'}, **htmx())
    assert toasts(r)[0]['message'] == 'Done. The next one is scheduled.'
    assert client(w.hari).get(f'/_/performance/one-on-ones/{mid}', **htmx()).status_code == 404
    assert '1-on-1s' in s.get('/me/growth/one-on-ones?show=past').content.decode()


def test_review_cycle_end_to_end_through_pages(w):
    perf.create_goal(w.sita, {'title': 'Ship the attendance app', 'weight': 60})
    perf.create_competency(w.hr, {'kind': 'skill', 'name': 'Problem solving'})
    course = lrn.create_course(w.hr, {'code': 'LEAD', 'title': 'Leadership basics', 'cost': '5000.00'})
    h = client(w.hr)
    assert 'New review cycle' in h.get('/_/performance/cycles/new', **htmx()).content.decode()
    r = h.post('/_/performance/cycles/new', {'name': 'FY 2083', 'period_start': '2026-01-01', 'period_end': plus_days(200), 'goals_weight': '60',
                                             'peer_enabled': 'on', 'peer_min': '2', 'self_due': plus_days(3),
                                             **{f'label_{i}': l for i, l in enumerate(['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding'], 1)}}, **htmx())
    assert toasts(r)[0]['message'] == 'Cycle saved.'
    cid = perf.list_cycles(w.hr)[0]['id']
    assert 'FY 2083' in h.get('/performance/reviews').content.decode()
    page = h.get(f'/performance/reviews?cycle={cid}').content.decode()
    assert 'Launch cycle' in page
    r = h.post(f'/_/performance/cycles/{cid}/launch', **htmx())
    assert toasts(r)[0]['message'] == 'Launched for 4 people.'
    # Deadlines can change after launch.
    r = h.post(f'/_/performance/cycles/{cid}/edit', {'name': 'FY 2083/84', 'self_due': plus_days(5), 'peer_min': '2'}, **htmx())
    assert r.status_code == 204, r.content
    s = client(w.sita)
    page = s.get('/me/growth/reviews').content.decode()
    assert 'FY 2083/84' in page and 'Self review to do' in page
    rid = perf.list_reviews(w.sita)[0]['id']
    drawer = s.get(f'/_/performance/reviews/{rid}', **htmx()).content.decode()
    assert 'Problem solving' in drawer and 'Save draft' in drawer and 'Print' not in drawer
    items = perf.review_detail(w.sita, rid)['items']
    data = {f"rating_{i['id']}": '4' for i in items} | {f"comment_{i['id']}": 'Did well' for i in items} | {'summary': 'Good year'}
    r = s.post(f'/_/performance/reviews/{rid}/save', {**data, 'submit': '1'}, **htmx())
    assert toasts(r)[0]['message'] == 'Review submitted.'
    r = s.post(f'/_/performance/reviews/{rid}/peers', {'employee_ids': [w.HARI, w.GITA]}, **htmx())
    assert toasts(r)[0]['message'] == 'Sent to your manager for approval.'
    m = client(w.ram)
    for p in perf.review_detail(w.ram, rid)['peers']['nominations']:
        r = m.post(f"/_/performance/peers/{p['id']}/decide", {'approve': '1'}, **htmx())
        assert toasts(r)[0]['message'] == 'Approved.'
    r = h.post(f'/_/performance/cycles/{cid}/advance', **htmx())
    assert toasts(r)[0]['message'] == 'Moved to Manager review.'
    mdata = {f"rating_{i['id']}": '5' if i['item_type'] == 'goal' else '3' for i in items}
    r = m.post(f'/_/performance/reviews/{rid}/save', {**mdata, 'summary': 'Strong delivery', 'increment_pct': '10', 'promotion': 'on',
                                                      'training_course_ids': [course], 'submit': '1'}, **htmx())
    assert toasts(r)[0]['message'] == 'Review submitted.', r.content
    assert float(one('SELECT manager_score FROM perf_review WHERE id = %s', [rid])['manager_score']) == 4.2
    h.post(f'/_/performance/cycles/{cid}/advance', **htmx())   # peer review
    for u, rating in ((w.hari, '4'), (w.gita, '5')):
        cl = client(u)
        assert 'Peer feedback requested' in cl.get('/me/growth/reviews').content.decode()
        pid = perf.peer_requests(u)[0]['id']
        assert 'Feedback for Sita Sharma' in cl.get(f'/_/performance/peers/{pid}/respond', **htmx()).content.decode()
        r = cl.post(f'/_/performance/peers/{pid}/respond', {'rating': rating, 'strengths': f'strength {rating}'}, **htmx())
        assert toasts(r)[0]['message'] == 'Thanks, feedback sent.'
    r = h.post(f'/_/performance/cycles/{cid}/advance', **htmx())
    assert toasts(r)[0]['message'] == 'Moved to Calibration.'
    page = h.get(f'/performance/reviews?cycle={cid}').content.decode()
    assert 'Rating distribution' in page and 'Release results' in page
    r = h.post(f'/_/performance/reviews/{rid}/calibrate', {'final_rating': '4', 'reason': 'adj'}, **htmx())
    assert r.status_code == 400
    r = h.post(f'/_/performance/reviews/{rid}/calibrate', {'final_rating': '4', 'reason': 'Bring in line with peers'}, **htmx())
    assert toasts(r)[0]['message'] == 'Final rating set.'
    h.post(f'/_/performance/cycles/{cid}/advance', **htmx())   # released
    drawer = s.get(f'/_/performance/reviews/{rid}', **htmx()).content.decode()
    assert 'From 2 colleagues (anonymous)' in drawer and 'I have read my review' in drawer and '+ strength 4' in drawer and 'Strengths: strength' not in drawer
    r = s.post(f'/_/performance/reviews/{rid}/acknowledge', {'comment': 'Thanks'}, **htmx())
    assert toasts(r)[0]['message'] == 'Thanks, acknowledged.'
    pr = s.get(f'/_/performance/reviews/{rid}/print')
    assert pr.status_code == 200 and 'Sita Sharma' in pr.content.decode() and 'Recommendation' not in pr.content.decode()
    assert 'Increment 10%' in m.get(f'/_/performance/reviews/{rid}/print').content.decode()
    assert client(w.hari).get(f'/_/performance/reviews/{rid}/print').status_code == 404
    assert rows('SELECT status, source FROM lrn_enrollment WHERE course_id = %s', [course]) == [{'status': 'enrolled', 'source': 'review'}]


def test_competencies_page_and_form(w):
    h = client(w.hr)
    r = h.post('/_/performance/competencies/new', {'kind': 'skill', 'name': 'Ownership', 'level_3': 'Owns routine work', 'designations': 'Engineer, Accountant', 'active': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Competency saved.'
    page = h.get('/performance/reviews?view=competencies').content.decode()
    assert 'Ownership' in page and 'Engineer, Accountant' in page
    comp = perf.list_competencies(w.hr, all_=True)[0]
    assert comp['indicators'] == [{'level': 3, 'text': 'Owns routine work'}]
    form = h.get(f"/_/performance/competencies/{comp['id']}/edit", **htmx()).content.decode()
    assert 'Owns routine work' in form and 'Type and press Enter' in form and '&quot;Engineer&quot;, &quot;Accountant&quot;' in form
    # The tag field posts one value per tag.
    r = h.post(f"/_/performance/competencies/{comp['id']}/edit", {'kind': 'skill', 'name': 'Ownership', 'designations': ['Engineer', 'Driver', 'Engineer'],
                                                                   'grades': ['G1'], 'active': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Competency saved.'
    comp = perf.get_competency(w.hr, comp['id'])
    assert comp['designations'] == ['Engineer', 'Driver'] and comp['grades'] == ['G1'] and comp['indicators'] == []
    assert client(w.ram).get('/_/performance/competencies/new', **htmx()).status_code == 403


# ---------------------------------------------------------------- audit fixes

def test_a_comment_only_checkin_leaves_a_done_yes_no_key_result_done(w):
    gid = perf.create_goal(w.sita, {'title': 'Ship', 'keyResults': [{'title': 'Released', 'measure': 'boolean', 'currentValue': 1}]})
    kr = perf.goal_detail(w.sita, gid)['goal']['key_results'][0]
    c = client(w.sita)
    # parts.tsx sends no value until the switch is touched; the unchecked box alone must not reset it.
    r = c.post(f'/_/performance/goals/{gid}/checkin', {'key_result_id': kr['id'], 'confidence': '3', 'comment': 'Still live'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200
    assert float(one('SELECT current_value FROM perf_key_result WHERE id = %s', [kr['id']])['current_value']) == 1
    assert one('SELECT value FROM perf_checkin WHERE goal_id = %s', [gid])['value'] is None
    # Touching the switch off does send 0.
    c.post(f'/_/performance/goals/{gid}/checkin', {'key_result_id': kr['id'], 'done_touched': '1', 'confidence': '2'}, **htmx('hj-drawer-body'))
    assert float(one('SELECT current_value FROM perf_key_result WHERE id = %s', [kr['id']])['current_value']) == 0
    drawer = c.get(f'/_/performance/goals/{gid}', **htmx('hj-drawer-body')).content.decode()
    assert 'name="done_touched"' in drawer and f":disabled=\"kr !== '{kr['id']}'\"" in drawer


def test_a_switched_off_tab_opens_the_next_tab_of_its_hub(w):
    set_features_off(w.co, 'performance')
    r = client(w.sita).get('/me/growth')
    assert r.status_code == 302 and r['Location'] == '/me/growth/learning'
    assert client(w.sita).get('/me/growth/reviews')['Location'] == '/me/growth/learning'
    r = client(w.ram).get('/performance')
    assert r.status_code == 302 and r['Location'] == '/performance/learning'
    assert client(w.ram).get('/performance/learning').status_code == 200
    # With both modules off there is nowhere left in the hub: home.
    set_features_off(w.co, 'performance', 'learning')
    assert client(w.sita).get('/me/growth')['Location'] == '/'


def test_empty_states_offer_the_next_step(w):
    s = client(w.sita)
    assert 'hx-get="/_/performance/goals/new"' in s.get('/me/growth').content.decode()
    assert 'hx-get="/_/performance/feedback/ask"' in s.get('/me/growth/feedback?box=given').content.decode()
    page = s.get('/me/growth/one-on-ones').content.decode()
    assert 'Schedule a 1-on-1' in page and 'hx-get="/_/performance/one-on-ones/new"' in page
    assert 'Set a goal' in client(w.ram).get(f'/performance?person={w.HARI}').content.decode()


def test_colleague_pickers_are_searchable_and_capped(w):
    s = client(w.sita)
    form = s.get('/_/performance/feedback/ask', **htmx()).content.decode()
    assert 'data-multi-pick' in form and 'name="from_employee_ids"' in form and 'max: 10' in form
    assert 'Hari Karki, Engineer' in form and 'Sita Sharma' not in form
    # The picker posts one value per pick, which the service reads as a list.
    r = s.post('/_/performance/feedback/ask', {'from_employee_ids': [w.HARI, w.GITA]}, **htmx())
    assert toasts(r)[0]['message'] == 'Request sent.'
    assert len(perf.feedback(w.sita, {'box': 'requests'})) == 2


def test_review_form_counts_unrated_items_and_pickers_in_the_drawer(w):
    perf.create_goal(w.sita, {'title': 'Ship the attendance app', 'weight': 60})
    perf.create_competency(w.hr, {'kind': 'skill', 'name': 'Problem solving'})
    lrn.create_course(w.hr, {'code': 'LEAD', 'title': 'Leadership basics'})
    cid = perf.create_cycle(w.hr, {'name': 'FY 2083', 'periodStart': '2026-01-01', 'periodEnd': plus_days(200), 'peerEnabled': True})
    perf.launch_cycle(w.hr, cid)
    rid = perf.list_reviews(w.sita)[0]['id']
    drawer = client(w.sita).get(f'/_/performance/reviews/{rid}', **htmx()).content.decode()
    # Two items, none rated: the submit button says so and stays off until both are rated (ReviewForm.tsx).
    assert 'Rate 2 more' in drawer and ':disabled="left > 0"' in drawer and drawer.count('<div data-rate-item>') == 2
    assert 'Suggest colleagues to review you' in drawer and 'data-multi-pick' in drawer
    items = perf.review_detail(w.sita, rid)['items']
    perf.save_self(w.sita, rid, {'items': [{'id': items[0]['id'], 'rating': 4}]})
    assert 'Rate 1 more' in client(w.sita).get(f'/_/performance/reviews/{rid}', **htmx()).content.decode()
    perf.save_self(w.sita, rid, {'items': [{'id': i['id'], 'rating': 4} for i in items], 'submit': True})
    perf.advance_cycle(w.hr, cid)
    mgr = client(w.ram).get(f'/_/performance/reviews/{rid}', **htmx()).content.decode()
    assert 'Training needs' in mgr and 'Leadership basics' in mgr and 'name="training_course_ids"' in mgr


def test_kpi_apply_shows_the_kpi_description_and_default_target(w):
    perf.create_kpi(w.hr, {'title': 'Bugs fixed per month', 'description': 'Closed tickets tagged bug', 'defaultTarget': 20})
    form = client(w.sita).get('/_/performance/kpis/apply', **htmx()).content.decode()
    assert 'Closed tickets tagged bug' in form and '&quot;t&quot;: &quot;20&quot;' in form


def test_peer_feedback_needs_a_rating_before_sending(w):
    cid = perf.create_cycle(w.hr, {'name': 'FY 2083', 'periodStart': '2026-01-01', 'periodEnd': plus_days(200), 'peerEnabled': True})
    perf.launch_cycle(w.hr, cid)
    rid = perf.list_reviews(w.sita)[0]['id']
    perf.nominate_peers(w.ram, rid, {'employeeIds': [w.HARI]})
    pid = perf.peer_requests(w.hari)[0]['id']
    form = client(w.hari).get(f'/_/performance/peers/{pid}/respond', **htmx()).content.decode()
    assert ':disabled="!r" disabled' in form and '4 · Exceeds expectations' in form
    r = client(w.hari).post(f'/_/performance/peers/{pid}/respond', {'strengths': 'x'}, **htmx())
    assert r.status_code == 400
