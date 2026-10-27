"""The time pages (apps/web/src/pages/time) through the Django test client."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core.db import one, rows
from hajiri.timesheets import services as ts
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts

from .helpers import add_days, make_world, today_npt


@pytest.fixture
def w(db):
    w = make_world()
    w.ids['client'] = ts.create_client(w.hr, {'name': 'Acme Hydro'})['id']
    w.ids['projA'] = ts.create_project(w.hr, {'code': 'HYDRO-1', 'name': 'Hydro survey', 'clientId': w.ids['client'], 'billRate': 2000,
                                              'budgetHours': 10})
    w.ids['actA'] = ts.create_activity(w.hr, {'name': 'Field work', 'projectId': w.ids['projA']})['id']
    w.ids['actGlobal'] = ts.create_activity(w.hr, {'name': 'Meetings'})['id']
    return w


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


# ---------------------------------------------------------------- my timesheet

def test_my_timesheet_page(w):
    c = client_for(w.sita)
    ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '12:30', 'projectId': w.ids['projA'], 'note': 'Site visit'})
    r = c.get('/me/timesheet')
    assert r.status_code == 200
    body = r.content.decode()
    assert 'Today: 0 m logged' in body and 'Start' in body and 'No time logged this week' in body
    r = c.get(f'/me/timesheet?week={w.WEEK}')
    body = r.content.decode()
    assert 'Hydro survey' in body and 'Site visit' in body and '3:30' in body and 'Submit week' in body
    assert 'Not submitted' in body and 'Billable' in body
    assert '3 h 30 m on' in body and 'for Hydro survey will be removed.' in body   # the delete confirmation
    # A week fragment for the refresh.
    r = c.get(f'/_/timesheets/week?week={w.MON}', **htmx())
    assert r.status_code == 200 and 'id="ts-week"' in r.content.decode()


def test_my_timesheet_when_turned_off_or_feature_off(w):
    c = client_for(w.sita)
    ts.save_settings(w.hr, {'enabled': False})
    assert 'Time tracking is turned off' in c.get('/me/timesheet').content.decode()
    assert c.get('/_/timesheets/home-timer', **htmx()).content == b''
    ts.save_settings(w.hr, {'enabled': True})
    set_features_off(w.company, 'time')
    r = c.get('/me/timesheet')
    assert r.status_code == 302 and r['Location'] == '/'
    assert c.post('/_/timesheets/timer/start', {}, **htmx()).status_code == 404


def test_timer_actions(w):
    c = client_for(w.hari)
    r = c.post('/_/timesheets/timer/start', {'note': 'Inbox', 'project_id': '', 'activity_id': ''}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Timer started'
    assert 'hj:refresh-timesheet-week' in hx_triggers(r)
    assert 'Tracking since' in r.content.decode() and 'Stop' in r.content.decode()
    r = c.post('/_/timesheets/timer/start', {}, **htmx())
    assert r.status_code == 409
    t = toasts(r)[0]
    assert (t['message'], t['title'], t['tone']) == ('A timer is already running. Stop it first.', 'Timer', 'err')
    r = c.post('/_/timesheets/timer/stop', {'note': 'Inbox zero'}, **htmx())
    assert toasts(r)[0]['message'] == 'Time saved'
    assert one('SELECT note FROM time_entry WHERE employee_id = %s', [w.hari_emp.id])['note'] == 'Inbox zero'
    r = c.post('/_/timesheets/timer/resume', {}, **htmx())
    assert toasts(r)[0]['message'] == 'Timer resumed'
    assert ts.timer_state(w.hari)['running']['note'] == 'Inbox zero'
    # The compact Home timer.
    r = c.get('/_/timesheets/home-timer', **htmx())
    assert 'Project timer' in r.content.decode() and 'Open timesheet' in r.content.decode()
    r = c.post('/_/timesheets/timer/stop', {'compact': '1'}, **htmx())
    assert 'name="compact"' in r.content.decode()


def test_timer_requires_project_when_policy_says_so(w):
    ts.save_settings(w.hr, {'requireProject': True})
    c = client_for(w.sita)
    body = c.get('/_/timesheets/timer', **htmx()).content.decode()
    assert 'Pick a project' in body and ':disabled="!project"' in body
    r = c.post('/_/timesheets/timer/start', {'project_id': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Pick a project for this time.'
    r = c.post('/_/timesheets/timer/start', {'project_id': w.ids['projA'], 'activity_id': w.ids['actA']}, **htmx())
    assert r.status_code == 200
    assert ts.timer_state(w.sita)['running']['activity_name'] == 'Field work'


def test_activity_options(w):
    c = client_for(w.sita)
    body = c.get(f"/_/timesheets/activity-options?project_id={w.ids['projA']}", **htmx()).content.decode()
    assert 'Field work' in body and 'Meetings' in body
    body = c.get('/_/timesheets/activity-options?project_id=', **htmx()).content.decode()
    assert 'Field work' not in body and 'Meetings' in body


def test_entry_add_edit_delete(w):
    c = client_for(w.sita)
    r = c.get(f'/_/timesheets/entries/new?date={w.MON}', **htmx())
    body = r.content.decode()
    assert r.status_code == 200 and 'data-modal-title="Add time"' in body and 'Hydro survey (HYDRO-1) · Acme Hydro' in body
    r = c.post('/_/timesheets/entries/new', {'date': w.MON, 'mode': 'range', 'start': '09:00', 'end': '12:30', 'project_id': w.ids['projA'],
                                            'activity_id': w.ids['actA'], 'note': 'Site visit', 'billable': ''}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Time added'
    ev = hx_triggers(r)
    assert 'hj:close-modal' in ev and 'hj:refresh-timesheet-week' in ev and 'hj:refresh-timesheet-timer' in ev
    e = rows('SELECT id::text AS id, duration_min, billable FROM time_entry WHERE employee_id = %s', [w.sita_emp.id])[0]
    assert (e['duration_min'], e['billable']) == (210, True)
    # A duration entry, not billable by choice.
    r = c.post('/_/timesheets/entries/new', {'date': w.TUE, 'mode': 'duration', 'duration_min': '95', 'start': '09:00', 'end': '10:00',
                                            'project_id': w.ids['projA'], 'billable': 'false'}, **htmx())
    assert r.status_code == 204
    d = one('SELECT duration_min, billable, started_at FROM time_entry WHERE employee_id = %s AND local_date = %s', [w.sita_emp.id, w.TUE])
    assert d == {'duration_min': 95, 'billable': False, 'started_at': None}
    # Errors are red toasts with the TS message.
    r = c.post('/_/timesheets/entries/new', {'date': w.MON, 'mode': 'range', 'start': '12:00', 'end': '13:00'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'This time overlaps another entry or your running timer.'
    assert toasts(r)[0]['title'] == 'Not saved'
    r = c.post('/_/timesheets/entries/new', {'date': add_days(today_npt(), 1), 'mode': 'duration', 'duration_min': '30'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'You cannot log time for a future day.'
    # Edit
    body = c.get(f"/_/timesheets/entries/{e['id']}/edit", **htmx()).content.decode()
    assert 'data-modal-title="Edit time"' in body and 'value="12:30"' in body and 'Site visit' in body
    r = c.post(f"/_/timesheets/entries/{e['id']}/edit", {'date': w.MON, 'mode': 'range', 'start': '09:00', 'end': '13:00',
                                                        'project_id': w.ids['projA'], 'activity_id': w.ids['actA'], 'note': 'longer',
                                                        'billable': 'true'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Entry updated'
    assert one('SELECT duration_min, note FROM time_entry WHERE id = %s', [e['id']]) == {'duration_min': 240, 'note': 'longer'}
    # Somebody else's entry is not found.
    other = client_for(w.ram)
    assert other.get(f"/_/timesheets/entries/{e['id']}/edit", **htmx()).status_code == 404
    assert other.post(f"/_/timesheets/entries/{e['id']}/delete", **htmx()).status_code == 404
    r = c.post(f"/_/timesheets/entries/{e['id']}/delete", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Entry deleted'
    assert one('SELECT count(*)::int AS n FROM time_entry WHERE id = %s', [e['id']])['n'] == 0


def test_editing_a_running_timer_changes_only_what_it_is_for(w):
    c = client_for(w.hari)
    run = ts.start_timer(w.hari, {})
    body = c.get(f"/_/timesheets/entries/{run['id']}/edit", **htmx()).content.decode()
    assert 'The timer is still running.' in body and 'name="start"' not in body
    r = c.post(f"/_/timesheets/entries/{run['id']}/edit", {'project_id': w.ids['projA'], 'activity_id': '', 'note': 'Survey'}, **htmx())
    assert r.status_code == 204
    t = ts.timer_state(w.hari)['running']
    assert (t['project_code'], t['note'], t['duration_min']) == ('HYDRO-1', 'Survey', None)


def test_submit_and_withdraw(w):
    c = client_for(w.sita)
    ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 95})
    body = c.get(f'/_/timesheets/submit?week={w.WEEK}', **htmx()).content.decode()
    assert 'Submit this week?' in body and 'goes to your manager. The week is locked until they decide.' in body
    assert '1 h 35 m for' in body
    assert '1 day differs from your attendance.' in body
    r = c.post('/_/timesheets/submit', {'week': add_days(w.WEEK, -7)}, **htmx())
    t = toasts(r)[0]
    assert r.status_code == 400 and (t['message'], t['title']) == ('There is no time logged in this week.', 'Not submitted')
    r = c.post('/_/timesheets/submit', {'week': w.WEEK, 'note': 'Site week'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Timesheet sent for approval'
    tid = one('SELECT id::text AS id, note FROM timesheet WHERE employee_id = %s', [w.sita_emp.id])
    assert tid['note'] == 'Site week'
    body = c.get(f'/me/timesheet?week={w.WEEK}').content.decode()
    assert 'Waiting for approval' in body and 'Withdraw' in body and 'Add time' not in body
    r = c.post(f"/_/timesheets/{tid['id']}/withdraw", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Timesheet withdrawn. You can edit the week again.'
    r = c.post(f"/_/timesheets/{tid['id']}/withdraw", **htmx())
    assert r.status_code == 409
    body = c.get(f'/me/timesheet?week={w.WEEK}').content.decode()
    assert 'Withdrawn' in body and 'Submit week' in body and 'Add time' in body


def test_returned_and_approved_week_banners(w):
    c = client_for(w.sita)
    ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 60})
    tid = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    from hajiri.workflow import service as wf
    wf.decide(w.ram, wf.instance_for_object('timesheet', tid)['id'], 'reject', reason='Add Tuesday project')
    body = c.get(f'/me/timesheet?week={w.WEEK}').content.decode()
    assert 'Returned by Ram Thapa' in body and 'Add Tuesday project' in body and 'Submit again' in body
    tid = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    wf.decide(w.ram, wf.instance_for_object('timesheet', tid)['id'], 'approve')
    body = c.get(f'/me/timesheet?week={w.WEEK}').content.decode()
    assert 'Approved by Ram Thapa. This week is locked.' in body


# ---------------------------------------------------------------- review

def _submitted(w) -> str:
    ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '12:30', 'projectId': w.ids['projA']})
    return ts.submit_timesheet(w.sita, {'weekStart': w.WEEK, 'note': 'Site week'})


def test_review_page_views(w):
    tid = _submitted(w)
    c = client_for(w.ram)
    body = c.get('/attendance/timesheets').content.decode()
    assert "Your team's project time, compared with their attendance." in body
    assert 'Sita Sharma' in body and '3:30' in body and f'/_/timesheets/{tid}' in body
    hr = client_for(w.hr)
    assert "Everyone's project time, compared with their attendance." in hr.get('/attendance/timesheets').content.decode()
    r = c.get(f'/attendance/timesheets?view=week&week={w.WEEK}', **htmx(target='ts-review'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Who has submitted' in body and 'Waiting for approval' in body and 'Not submitted' in body
    assert '<html' not in body
    body = c.get('/attendance/timesheets?view=history&status=approved', **htmx(target='ts-review')).content.decode()
    assert 'No timesheets yet' in body
    body = c.get('/attendance/timesheets?view=history', **htmx(target='ts-review')).content.decode()
    assert 'Sita Sharma' in body
    body = c.get(f'/attendance/timesheets?view=report&from={w.WEEK}&to={add_days(w.WEEK, 6)}').content.decode()
    assert 'Hydro survey' in body and '3.50' in body and 'NPR 7,000.00' in body and '35% of 10 h' in body
    body = hr.get(f'/attendance/timesheets?view=report&from={w.WEEK}&to={add_days(w.WEEK, 6)}&group_by=employee').content.decode()
    assert 'Person' in body and 'Sita Sharma' in body


def test_review_page_is_for_managers_and_attendance_roles(w):
    c = client_for(w.hari)
    r = c.get('/attendance/timesheets')
    assert r.status_code in (302, 403)
    assert client_for(w.hari).get('/_/timesheets/report.csv?from=2026-01-01&to=2026-01-31', **htmx()).status_code == 403


def test_review_drawer_and_decisions(w):
    tid = _submitted(w)
    c = client_for(w.ram)
    r = c.get(f'/_/timesheets/{tid}', **htmx(target='hj-drawer-body'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Sita Sharma · ' in body and 'Approve' in body and 'Return' in body and 'Site week' in body
    assert client_for(w.hari).get(f'/_/timesheets/{tid}', **htmx()).status_code == 403
    assert client_for(w.outsider).get(f'/_/timesheets/{tid}', **htmx()).status_code == 404
    r = c.post(f'/_/timesheets/{tid}/decide', {'decision': 'reject', 'reason': ''}, **htmx())
    t = toasts(r)[0]
    assert (t['message'], t['tone']) == ('Say what needs fixing so they can correct it.', 'info')
    r = c.post(f'/_/timesheets/{tid}/decide', {'decision': 'reject', 'reason': 'Add Tuesday project'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Timesheet returned'
    assert 'hj:refresh-timesheets-review' in hx_triggers(r)
    assert one('SELECT status, decision_comment FROM timesheet WHERE id = %s', [tid]) == {'status': 'rejected', 'decision_comment': 'Add Tuesday project'}
    body = c.get(f'/_/timesheets/{tid}', **htmx()).content.decode()
    assert 'Approve' not in body and 'Add Tuesday project' in body
    tid2 = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    r = c.post(f'/_/timesheets/{tid2}/decide', {'decision': 'approve', 'reason': ''}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Timesheet approved'
    assert one('SELECT status FROM timesheet WHERE id = %s', [tid2])['status'] == 'approved'


def test_hari_cannot_decide_someone_elses_week(w):
    tid = _submitted(w)
    r = client_for(w.hari).post(f'/_/timesheets/{tid}/decide', {'decision': 'approve'}, **htmx())
    assert r.status_code == 403


def test_report_csv_downloads(w):
    _submitted(w)
    ts.add_entry(w.hari, {'date': w.MON, 'durationMin': 15, 'note': '=HYPERLINK("x")'})
    c = client_for(w.hr)
    r = c.get(f'/_/timesheets/report.csv?kind=entries&from={w.WEEK}&to={add_days(w.WEEK, 6)}')
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    assert r['Content-Disposition'] == f'attachment; filename="time-entries-{w.WEEK}-to-{add_days(w.WEEK, 6)}.csv"'
    text = r.content.decode('utf-8-sig')
    assert '"\'=HYPERLINK(""x"")"' in text and 'Hours (rounded)' in text
    r = c.get(f'/_/timesheets/report.csv?kind=summary&from={w.WEEK}&to={add_days(w.WEEK, 6)}&group_by=project')
    text = r.content.decode('utf-8-sig')
    assert 'Budget used %' in text and 'HYDRO-1' in text
    assert r['Content-Disposition'] == f'attachment; filename="time-by-project-{w.WEEK}-to-{add_days(w.WEEK, 6)}.csv"'


# ---------------------------------------------------------------- catalogue

def test_projects_page_and_forms(w):
    c = client_for(w.hr)
    body = c.get('/attendance/projects').content.decode()
    assert 'Hydro survey' in body and 'NPR 2,000.00/h' in body and 'Everyone' in body and '0% of 10 h' in body
    assert client_for(w.ram).get('/attendance/projects').status_code in (302, 403)
    body = c.get('/_/timesheets/projects/new', **htmx()).content.decode()
    assert 'data-modal-title="New project"' in body and 'Create project' in body
    r = c.post('/_/timesheets/projects/new', {'name': 'Internal R&D', 'code': 'secret', 'client_id': '', 'color': '#0b7285',
                                             'bill_rate': '', 'budget_hours': '', 'member_ids': [str(w.ram_emp.id)]}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Project created'
    p = one("SELECT id::text AS id, code, billable, all_members, color FROM time_project WHERE name = 'Internal R&D'")
    assert p == {'id': p['id'], 'code': 'SECRET', 'billable': False, 'all_members': False, 'color': '#0b7285'}
    assert rows('SELECT employee_id::text AS e FROM time_project_member WHERE project_id = %s', [p['id']]) == [{'e': str(w.ram_emp.id)}]
    body = c.get(f"/_/timesheets/projects/{p['id']}/edit", **htmx()).content.decode()
    assert 'Edit Internal R&amp;D' in body and 'Archived' in body
    r = c.post(f"/_/timesheets/projects/{p['id']}/edit", {'name': 'Internal R&D', 'code': 'SECRET', 'all_members': 'true', 'billable': 'true',
                                                          'color': '#0b7285', 'archived': 'true', 'bill_rate': '1500', 'budget_hours': '20'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Project saved'
    p2 = one('SELECT status, all_members, bill_rate::text AS rate, budget_hours::text AS budget FROM time_project WHERE id = %s', [p['id']])
    assert p2 == {'status': 'archived', 'all_members': True, 'rate': '1500.00', 'budget': '20.00'}
    r = c.post('/_/timesheets/projects/new', {'name': 'dup', 'code': 'HYDRO-1'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'already exists'


def test_clients_and_activities(w):
    c = client_for(w.hr)
    body = c.get('/attendance/projects?view=clients', **htmx(target='ts-catalogue')).content.decode()
    assert 'Acme Hydro' in body and 'Archive' in body
    r = c.post('/_/timesheets/clients/new', {'name': 'Nepal Power'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Client added'
    assert 'hj:refresh-timesheets-catalogue' in hx_triggers(r)
    r = c.post(f"/_/timesheets/clients/{w.ids['client']}/archive", {'archived': 'true'}, **htmx())
    assert r.status_code == 204 and not toasts(r)
    assert one('SELECT archived FROM time_client WHERE id = %s', [w.ids['client']])['archived'] is True
    assert 'Restore' in c.get('/attendance/projects?view=clients').content.decode()
    body = c.get('/attendance/projects?view=activities').content.decode()
    assert 'Field work' in body and 'Every project' in body
    r = c.post('/_/timesheets/activities/new', {'name': 'Design', 'project_id': w.ids['projA']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Activity added'
    r = c.post(f"/_/timesheets/activities/{w.ids['actA']}/archive", {'archived': 'true'}, **htmx())
    assert r.status_code == 204
    assert one('SELECT archived FROM time_activity WHERE id = %s', [w.ids['actA']])['archived'] is True
    assert client_for(w.sita).post('/_/timesheets/clients/new', {'name': 'x'}, **htmx()).status_code == 403


# ---------------------------------------------------------------- settings card

def test_settings_card(w):
    c = client_for(w.hr)
    body = c.get('/_/timesheets/settings', **htmx()).content.decode()
    assert 'Stop forgotten timers after' in body and 'value="12"' in body
    r = c.post('/_/timesheets/settings', {'enabled': 'true', 'require_project': 'true', 'rounding_step': '15', 'rounding_mode': 'up',
                                         'auto_stop_hours': '40'}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Time tracking settings saved'
    assert ts.load_time_policy(str(w.company.id)) == {'enabled': True, 'requireProject': True, 'roundingStep': 15, 'roundingMode': 'up',
                                                      'fridayReminder': False, 'autoStopHours': 24}
    # The direction is kept while rounding is off (the select is disabled and not posted).
    c.post('/_/timesheets/settings', {'enabled': 'true', 'rounding_step': '0', 'rounding_mode_kept': 'up', 'auto_stop_hours': '8'}, **htmx())
    assert ts.load_time_policy(str(w.company.id))['roundingMode'] == 'up'
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'time.policy'")['n'] == 2
    assert client_for(w.sita).get('/_/timesheets/settings', **htmx()).status_code == 403


def test_nav_tabs(w):
    from hajiri.web import nav
    hr_items = {i.url_name for i in nav.all_items()}
    assert {'timesheets:mine', 'timesheets:review', 'timesheets:projects'} <= hr_items
    body = client_for(w.ram).get('/attendance/timesheets').content.decode()
    assert 'href="/attendance/timesheets"' in body


# ---------------------------------------------------------------- audit additions

def test_compact_timer_stop_keeps_the_running_note(w):
    """The Home timer has no note field; TimerCard sends the running timer's note back on Stop."""
    ts.start_timer(w.hari, {'note': 'Inbox'})
    c = client_for(w.hari)
    body = c.get('/_/timesheets/home-timer', **htmx()).content.decode()
    assert 'name="note" value="Inbox"' in body
    r = c.post('/_/timesheets/timer/stop', {'compact': '1', 'note': 'Inbox'}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Time saved'
    assert one('SELECT note FROM time_entry WHERE employee_id = %s', [w.hari_emp.id])['note'] == 'Inbox'


def test_stopped_timer_leaves_its_work_ready_for_the_next_start(w):
    """TimerCard keeps the project, activity and note of the timer it just stopped in the Start form."""
    ts.start_timer(w.sita, {'projectId': w.ids['projA'], 'activityId': w.ids['actA'], 'note': 'Survey'})
    c = client_for(w.sita)
    body = c.post('/_/timesheets/timer/stop', {'note': 'Survey done'}, **htmx()).content.decode()
    assert f'<option value="{w.ids["projA"]}" selected>' in body
    assert f'<option value="{w.ids["actA"]}" selected>Field work</option>' in body
    assert 'value="Survey done"' in body
    # A fresh load starts blank, with only the company-wide activities.
    body = c.get('/_/timesheets/timer', **htmx()).content.decode()
    assert ' selected>' not in body and 'Field work' not in body and 'Meetings' in body


def test_settings_card_is_for_the_attendance_area(w):
    from tests.web.factories import make_user
    admin = make_user(w.company, roles=['attendance_admin'])
    c = client_for(admin)
    assert c.get('/_/timesheets/settings', **htmx()).status_code == 200
    r = c.post('/_/timesheets/settings', {'enabled': 'true', 'rounding_step': '5', 'rounding_mode': 'down', 'friday_reminder': 'true',
                                         'auto_stop_hours': ''}, **htmx())
    assert r.status_code == 200
    assert ts.load_time_policy(str(w.company.id)) == {'enabled': True, 'requireProject': False, 'roundingStep': 5, 'roundingMode': 'down',
                                                      'fridayReminder': True, 'autoStopHours': 12}
    assert client_for(w.ram).post('/_/timesheets/settings', {}, **htmx()).status_code == 403


def test_project_form_colour_swatches_and_member_checklist(w):
    c = client_for(w.hr)
    body = c.get('/_/timesheets/projects/new', **htmx()).content.decode()
    for s in ('#12773a', '#0b7285', '#1971c2', '#6741d9', '#c2255c', '#e8590c', '#f08c00', '#495057'):
        assert f"@click=\"color = '{s}'\"" in body
    assert 'name="color"' in body and 'value="#12773a"' in body
    assert 'Sita Sharma (E001)' in body and 'name="member_ids"' in body and 'placeholder="Pick people"' in body
    pid = ts.create_project(w.hr, {'code': 'P2', 'name': 'Private', 'allMembers': False, 'memberIds': [str(w.sita_emp.id)], 'color': '#c2255c'})
    body = c.get(f'/_/timesheets/projects/{pid}/edit', **htmx()).content.decode()
    assert f'value="{w.sita_emp.id}" checked' in body and 'value="#c2255c"' in body
    r = c.post('/_/timesheets/projects/new', {'name': 'Bad colour', 'code': 'BC', 'color': 'red'}, **htmx())
    assert r.status_code == 400
