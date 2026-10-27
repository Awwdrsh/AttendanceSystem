"""Port of apps/api/test/timesheets.test.ts at service level (status codes as the TS routes answered them)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hajiri.core.db import execute, one
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.timesheets import services as ts
from hajiri.web.csv import to_csv
from hajiri.workflow import service as wf

from .helpers import add_days, make_world, status, today_npt

DEAD = '00000000-0000-7000-8000-00000000dead'


@pytest.fixture
def w(db):
    return make_world()


# ================================================================ describe: clients, projects and activities

def step_catalogue(w):
    # only HR manages the catalogue
    assert status(ts.create_client, w.sita, {'name': 'Acme'}) == 403
    assert status(ts.create_project, w.ram, {'code': 'X', 'name': 'X'}) == 403
    c = ts.create_client(w.hr, {'name': 'Acme Hydro'})
    w.ids['client'] = c['id']
    assert status(ts.create_client, w.hr, {'name': 'acme hydro'}) == 409

    # creates projects with members, rates and budgets; validates codes and clients
    w.ids['projA'] = ts.create_project(w.hr, {'code': 'hydro-1', 'name': 'Hydro survey', 'clientId': w.ids['client'],
                                              'billRate': 2000, 'budgetHours': 10})
    w.ids['projPrivate'] = ts.create_project(w.hr, {'code': 'SECRET', 'name': 'Internal R&D', 'allMembers': False,
                                                    'memberIds': [str(w.ram_emp.id)], 'billable': False})
    assert status(ts.create_project, w.hr, {'code': 'bad code!', 'name': 'x'}) == 400
    assert status(ts.create_project, w.hr, {'code': 'HYDRO-1', 'name': 'dup'}) == 409
    assert status(ts.create_project, w.hr, {'code': 'NOPE', 'name': 'x', 'memberIds': [str(w.other_emp.id)], 'allMembers': False}) == 400
    assert status(ts.create_project, w.outsider, {'code': 'Z', 'name': 'z', 'clientId': w.ids['client']}) == 400
    w.ids['actGlobal'] = ts.create_activity(w.hr, {'name': 'Meetings'})['id']
    w.ids['actA'] = ts.create_activity(w.hr, {'name': 'Field work', 'projectId': w.ids['projA']})['id']

    # employees see only projects they may use; other companies see nothing
    mine = ts.list_projects(w.sita)
    assert [p['code'] for p in mine] == ['HYDRO-1']
    assert mine[0]['members'] == []
    assert len(ts.list_projects(w.ram)) == 2
    hr_all = ts.list_projects(w.hr, True)
    assert len(next(p for p in hr_all if p['id'] == w.ids['projPrivate'])['members']) == 1
    assert ts.list_projects(w.outsider) == []
    assert status(ts.update_project, w.outsider, w.ids['projA'], {'name': 'hijack'}) == 404
    assert len(ts.list_activities(w.sita, {'projectId': w.ids['projA']})) == 2


def test_catalogue(w):
    step_catalogue(w)


def test_catalogue_audit_and_client_archive(w):
    step_catalogue(w)
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE table_name = 'time_project' AND action = 'create'")['n'] == 2
    c = ts.update_client(w.hr, w.ids['client'], {'archived': True})
    assert c['archived'] is True
    assert [x['name'] for x in ts.list_clients(w.hr)] == []
    assert [x['name'] for x in ts.list_clients(w.hr, all_=True)] == ['Acme Hydro']
    assert ts.list_clients(w.hr, all_=True)[0]['active_projects'] == 1
    assert status(ts.update_client, w.outsider, w.ids['client'], {'name': 'x'}) == 404
    a = ts.update_activity(w.hr, w.ids['actGlobal'], {'archived': True})
    assert a['archived'] is True
    assert len(ts.list_activities(w.sita, {'projectId': w.ids['projA']})) == 1
    assert len(ts.list_activities(w.hr, {'all': '1'})) == 2
    assert status(ts.update_activity, w.outsider, w.ids['actA'], {'name': 'x'}) == 404
    assert status(ts.create_activity, w.hr, {'name': 'x', 'projectId': DEAD}) == 400


def test_project_update_members_and_archive(w):
    step_catalogue(w)
    ts.update_project(w.hr, w.ids['projPrivate'], {'memberIds': [str(w.sita_emp.id)], 'billRate': None, 'code': 'secret2'})
    p = next(x for x in ts.list_projects(w.hr, True) if x['id'] == w.ids['projPrivate'])
    assert p['code'] == 'SECRET2' and [m['id'] for m in p['members']] == [str(w.sita_emp.id)]
    assert {x['code'] for x in ts.list_projects(w.sita)} == {'HYDRO-1', 'SECRET2'}
    ts.update_project(w.hr, w.ids['projA'], {'status': 'archived'})
    assert {x['code'] for x in ts.list_projects(w.sita)} == {'SECRET2'}
    with pytest.raises(BadRequestError, match='Hydro survey is archived.'):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 30, 'projectId': w.ids['projA']})
    assert status(ts.update_project, w.hr, w.ids['projA'], {'clientId': DEAD}) == 400


# ================================================================ describe: manual entries

def step_manual(w):
    ids = w.ids
    # logs time with start/end or duration and defaults billable from the project
    a = ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '12:30', 'projectId': ids['projA'], 'activityId': ids['actA'],
                              'note': 'Site visit'})
    assert {k: a[k] for k in ('duration_min', 'billable', 'project_code', 'activity_name', 'source')} == {
        'duration_min': 210, 'billable': True, 'project_code': 'HYDRO-1', 'activity_name': 'Field work', 'source': 'manual'}
    b = ts.add_entry(w.sita, {'date': w.TUE, 'durationMin': 95, 'activityId': ids['actGlobal']})
    assert (b['duration_min'], b['billable'], b['started_at']) == (95, False, None)

    # rejects overlaps, future time, bad ranges, more than 24 h a day and projects you are not on
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'start': '12:00', 'end': '13:00'}) == 409
    assert status(ts.add_entry, w.sita, {'date': add_days(today_npt(), 1), 'durationMin': 30}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'start': '15:00', 'end': '14:00'}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'start': '15:00'}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.MON}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.TUE, 'durationMin': 1400}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'durationMin': 30, 'projectId': ids['projPrivate']}) == 403
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'durationMin': 30, 'projectId': ids['projA'], 'activityId': DEAD}) == 400

    # requires a project when the company says so
    assert status(ts.save_settings, w.hr, {'requireProject': True, 'roundingStep': 15, 'roundingMode': 'up'}) == 200
    assert status(ts.save_settings, w.sita, {'requireProject': False}) == 403
    assert status(ts.save_settings, w.hr, {'roundingStep': 7}) == 400
    assert status(ts.add_entry, w.sita, {'date': w.TUE, 'durationMin': 10}) == 400
    pol = ts.get_settings(w.sita)['policy']
    assert pol['requireProject'] is True and pol['roundingStep'] == 15

    # edits and deletes only your own entries
    e = ts.add_entry(w.sita, {'date': w.TUE, 'start': '14:00', 'end': '15:00', 'projectId': ids['projA']})
    eid = e['id']
    assert status(ts.update_entry, w.ram, eid, {'note': 'x'}) == 404
    assert status(ts.delete_entry, w.hr, eid) == 403
    p = ts.update_entry(w.sita, eid, {'start': '14:00', 'end': '16:00', 'note': 'longer'})
    assert (p['duration_min'], p['note']) == (120, 'longer')
    assert status(ts.update_entry, w.sita, eid, {'projectId': ids['projPrivate']}) == 403
    assert status(ts.delete_entry, w.sita, eid) == 200

    # lets a manager read their team and HR everyone, but not others
    q = {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'employeeId': str(w.sita_emp.id)}
    assert len(ts.list_entries(w.ram, q)) > 0
    assert status(ts.list_entries, w.hr, q) == 200
    assert status(ts.list_entries, w.hari, q) == 403
    assert status(ts.list_entries, w.outsider, q) == 404
    assert status(ts.week, w.other, {'weekStart': w.WEEK, 'employeeId': str(w.sita_emp.id)}) == 403
    assert status(ts.list_entries, w.hr, {'from': w.WEEK, 'to': w.WEEK}) == 403   # HR user without an employee record


def test_manual_entries(w):
    step_catalogue(w)
    step_manual(w)


def test_entry_messages(w):
    step_catalogue(w)
    with pytest.raises(ConflictError, match='This time overlaps another entry or your running timer.'):
        ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '10:00'})
        ts.add_entry(w.sita, {'date': w.MON, 'start': '09:30', 'end': '10:30'})
    with pytest.raises(BadRequestError, match='You cannot log time for a future day.'):
        ts.add_entry(w.sita, {'date': add_days(today_npt(), 1), 'durationMin': 30})
    with pytest.raises(BadRequestError, match='The end time must be after the start time.'):
        ts.add_entry(w.sita, {'date': w.MON, 'start': '15:00', 'end': '14:00'})
    with pytest.raises(BadRequestError, match='Give both a start and an end time, or just a duration.'):
        ts.add_entry(w.sita, {'date': w.MON, 'start': '15:00'})
    with pytest.raises(BadRequestError, match='Give a start and end time or a duration.'):
        ts.add_entry(w.sita, {'date': w.MON})
    with pytest.raises(BadRequestError, match='A day cannot have more than 24 hours of time.'):
        ts.add_entry(w.sita, {'date': w.TUE, 'durationMin': 1400})
        ts.add_entry(w.sita, {'date': w.TUE, 'durationMin': 100})
    with pytest.raises(ForbiddenError, match='You are not a member of Internal R&D.'):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 30, 'projectId': w.ids['projPrivate']})
    with pytest.raises(BadRequestError, match='That activity does not belong to this project.'):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 30, 'projectId': w.ids['projA'], 'activityId': DEAD})
    with pytest.raises(BadRequestError, match='That activity is not available without a project.'):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 30, 'activityId': w.ids['actA']})
    with pytest.raises(ForbiddenError, match='Only employees can track time.'):
        ts.add_entry(w.hr, {'date': w.MON, 'durationMin': 30})
    with pytest.raises(BadRequestError, match='from must be before to'):
        ts.list_entries(w.sita, {'from': w.TUE, 'to': w.MON})


def test_editing_a_duration_entry_changing_project_resets_activity_and_billable(w):
    step_catalogue(w)
    e = ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 30, 'activityId': w.ids['actGlobal']})
    assert e['billable'] is False
    p = ts.update_entry(w.sita, e['id'], {'projectId': w.ids['projA']})
    # The project changed, so the activity is kept only when sent (here it was not; it is cleared) and billable follows it.
    assert p['project_id'] == w.ids['projA'] and p['activity_id'] is None and p['billable'] is True
    p = ts.update_entry(w.sita, e['id'], {'durationMin': 45, 'billable': False})
    assert (p['duration_min'], p['billable']) == (45, False)


# ================================================================ describe: timer

def step_timer(w):
    ts.save_settings(w.hr, {'requireProject': False})
    s = ts.start_timer(w.hari, {'note': 'Inbox'})
    assert (s['source'], s['duration_min'], s['note']) == ('timer', None, 'Inbox')
    assert status(ts.start_timer, w.hari, {}) == 409
    assert ts.timer_state(w.hari)['running']['id'] == s['id']
    # Nothing may be logged after a running timer started.
    now = datetime.now(timezone.utc) + timedelta(minutes=345)
    if now.hour >= 1:
        start = now - timedelta(minutes=50)
        end = now - timedelta(minutes=1)
        if start.date() == end.date():
            execute("UPDATE time_entry SET started_at = now() - interval '55 minutes' WHERE id = %s", [s['id']])
            assert status(ts.add_entry, w.hari, {'date': today_npt(), 'start': start.strftime('%H:%M'), 'end': end.strftime('%H:%M')}) == 409
    stop = ts.stop_timer(w.hari, {'note': 'Inbox zero'})
    assert stop['duration_min'] >= 1
    assert stop['note'] == 'Inbox zero'
    assert status(ts.stop_timer, w.hari, {}) == 409
    r = ts.resume_timer(w.hari)
    assert r['note'] == 'Inbox zero'
    ts.stop_timer(w.hari, {})

    # auto-stops a forgotten timer at the company limit and tells the employee
    s = ts.start_timer(w.hari, {})
    execute('DELETE FROM time_entry WHERE employee_id = %s AND id <> %s', [w.hari_emp.id, s['id']])
    execute("UPDATE time_entry SET started_at = now() - interval '13 hours' WHERE id = %s", [s['id']])
    assert ts.timer_state(w.hari)['running'] is None
    row = one('SELECT duration_min, auto_stopped FROM time_entry WHERE id = %s', [s['id']])
    assert row == {'duration_min': 720, 'auto_stopped': True}
    n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'time.timer_auto_stopped' AND employee_id = %s",
            [w.hari_emp.id])
    assert n['n'] == 1
    assert ts.auto_stop_stale_timers() == 0

    # employees without an employee record cannot track time; turning tracking off blocks it
    assert status(ts.start_timer, w.hr, {}) == 403
    ts.save_settings(w.hr, {'enabled': False})
    assert status(ts.start_timer, w.hari, {}) == 403
    assert status(ts.add_entry, w.hari, {'date': w.MON, 'durationMin': 5}) == 403
    ts.save_settings(w.hr, {'enabled': True, 'roundingStep': 15, 'roundingMode': 'up'})


def test_timer(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)


def test_timer_messages_and_resume_with_nothing(w):
    step_catalogue(w)
    with pytest.raises(BadRequestError, match='There is nothing to resume yet. Start a new timer.'):
        ts.resume_timer(w.hari)
    with pytest.raises(ConflictError, match='No timer is running.'):
        ts.stop_timer(w.hari)
    ts.start_timer(w.hari, {'projectId': w.ids['projA']})
    with pytest.raises(ConflictError, match='A timer is already running. Stop it first.'):
        ts.start_timer(w.hari)
    t = ts.timer_state(w.hari)
    assert t['running']['project_code'] == 'HYDRO-1' and t['running']['billable'] is True
    assert t['last'] is None and t['todayMin'] == 0
    running_id = t['running']['id']
    with pytest.raises(BadRequestError, match='Stop the timer before changing its times.'):
        ts.update_entry(w.hari, running_id, {'durationMin': 30})
    # What a running timer is for can still change.
    e = ts.update_entry(w.hari, running_id, {'projectId': None, 'note': 'Calls'})
    assert e['project_id'] is None and e['note'] == 'Calls' and e['duration_min'] is None
    ts.save_settings(w.hr, {'enabled': False})
    with pytest.raises(ForbiddenError, match='Time tracking is turned off for your company.'):
        ts.start_timer(w.sita)


# ================================================================ describe: weekly timesheets

def step_weekly(w):
    sita_id = str(w.sita_emp.id)
    # shows the week with rounding and the attendance comparison
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash)
               VALUES (%s, %s, 1, 'Present', 480, 'h')""", [sita_id, w.MON])
    wk = ts.week(w.sita, {'weekStart': w.MON})
    assert wk['weekStart'] == w.WEEK
    mon = next(d for d in wk['days'] if d['date'] == w.MON)
    assert {k: mon[k] for k in ('trackedMin', 'roundedMin', 'attendanceMin', 'diffMin', 'flag')} == {
        'trackedMin': 210, 'roundedMin': 210, 'attendanceMin': 480, 'diffMin': -270, 'flag': 'under'}
    tue = next(d for d in wk['days'] if d['date'] == w.TUE)
    assert {k: tue[k] for k in ('trackedMin', 'roundedMin', 'flag')} == {'trackedMin': 95, 'roundedMin': 105, 'flag': 'no_attendance'}
    assert wk['totals']['billableMin'] == 210
    assert ts.round_entry(95, {'roundingStep': 15, 'roundingMode': 'up'}) == 105

    # submits a week, locks it, and lets the employee withdraw
    assert status(ts.submit_timesheet, w.sita, {'weekStart': w.MON}) == 400
    assert status(ts.submit_timesheet, w.sita, {'weekStart': add_days(w.WEEK, -7)}) == 400   # empty week
    ts_id = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK, 'note': 'Site week'})
    assert status(ts.submit_timesheet, w.sita, {'weekStart': w.WEEK}) == 409
    assert status(ts.add_entry, w.sita, {'date': w.TUE, 'durationMin': 10, 'projectId': w.ids['projA']}) == 409
    assert status(ts.withdraw_timesheet, w.sita, ts_id) == 200
    assert status(ts.withdraw_timesheet, w.sita, ts_id) == 409
    assert status(ts.add_entry, w.sita, {'date': w.TUE, 'start': '17:00', 'end': '17:30', 'projectId': w.ids['projA']}) == 200

    # the manager rejects with a reason, the employee fixes and resubmits, the manager approves
    ts_id = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    item = next(i for i in wf.inbox_for(w.ram) if i['objectId'] == ts_id)
    assert item['objectType'] == 'timesheet'
    s = item['summary']
    assert (s['weekStart'], s['totalMin'], s['attendanceMin']) == (w.WEEK, 335, 480)
    assert next((i for i in wf.inbox_for(w.hari) if i['objectId'] == ts_id), None) is None
    assert status(wf.decide, w.ram, item['instanceId'], 'reject', reason='Add Tuesday project') == 200
    after = ts.week(w.sita, {'weekStart': w.WEEK})
    assert (after['timesheet']['status'], after['timesheet']['decision_comment']) == ('rejected', 'Add Tuesday project')
    note = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'timesheet.rejected' AND employee_id = %s", [sita_id])
    assert note['n'] > 0
    # Returned weeks are open again.
    assert status(ts.add_entry, w.sita, {'date': w.TUE, 'start': '18:00', 'end': '18:30', 'projectId': w.ids['projA']}) == 200
    again = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    item2 = next(i for i in wf.inbox_for(w.ram) if i['objectId'] == again)
    assert status(wf.decide, w.ram, item2['instanceId'], 'approve') == 200
    final = ts.week(w.sita, {'weekStart': w.WEEK})
    assert final['timesheet']['status'] == 'approved'
    assert len(final['history']) == 2
    assert status(ts.add_entry, w.sita, {'date': w.MON, 'durationMin': 5, 'projectId': w.ids['projA']}) == 409
    assert status(ts.withdraw_timesheet, w.sita, again) == 409

    # lists timesheets by scope and shows details only to those who manage the person
    team = ts.list_timesheets(w.ram)
    assert len(team) >= 3
    assert 'diff_min' in team[0]
    assert status(ts.list_timesheets, w.ram, {'scope': 'all'}) == 403
    assert ts.list_timesheets(w.hari) == []
    assert len(ts.list_timesheets(w.hr, {'scope': 'all'})) >= 3
    assert ts.list_timesheets(w.outsider, {'scope': 'all'}) == []
    tid = team[0]['id']
    d = ts.timesheet_detail(w.ram, tid)
    assert d['employee']['name'] == 'Sita Sharma'
    assert len(d['days']) == 7
    assert status(ts.timesheet_detail, w.hari, tid) == 403
    assert status(ts.timesheet_detail, w.outsider, tid) == 404
    tw = ts.team_week(w.ram, {'weekStart': w.WEEK})
    assert [p['name_en'] for p in tw['people']] == ['Sita Sharma']
    assert tw['people'][0]['status'] == 'approved'
    w.ids['approved'] = again


def test_weekly_timesheets(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)
    step_weekly(w)


def test_weekly_messages_notifications_and_audit(w):
    step_catalogue(w)
    ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 15})
    with pytest.raises(BadRequestError, match='weekStart must be a Sunday'):
        ts.submit_timesheet(w.sita, {'weekStart': w.MON})
    with pytest.raises(BadRequestError, match='You cannot submit a week that has not started.'):
        ts.submit_timesheet(w.sita, {'weekStart': ts.week_start_of(add_days(today_npt(), 7))})
    with pytest.raises(BadRequestError, match='There is no time logged in this week.'):
        ts.submit_timesheet(w.sita, {'weekStart': add_days(w.WEEK, -7)})
    tid = ts.submit_timesheet(w.sita, {'weekStart': w.WEEK})
    pending = one("SELECT title, body, user_id FROM notification_outbox WHERE template = 'timesheet.pending' AND channel = 'in_app'")
    assert pending['title'] == 'Timesheet to approve'
    assert pending['body'] == f"Sita Sharma submitted 0.3 h for {ts.bs_range(w.WEEK, add_days(w.WEEK, 6))}."
    assert str(pending['user_id']) == w.ram.id
    with pytest.raises(ConflictError, match="This week's timesheet is waiting for approval. Withdraw it to make changes."):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 5})
    with pytest.raises(NotFoundError, match='timesheet not found'):
        ts.withdraw_timesheet(w.hari, tid)
    inst = wf.instance_for_object('timesheet', tid)
    wf.decide(w.ram, inst['id'], 'approve', reason='Thanks')
    n = one("SELECT title, body, payload FROM notification_outbox WHERE template = 'timesheet.approved' AND channel = 'in_app'")
    assert n['title'] == 'Timesheet approved'
    assert n['body'] == f"Your timesheet for {ts.bs_range(w.WEEK, add_days(w.WEEK, 6))} was approved: Thanks"
    assert n['payload'] == {'timesheetId': tid, 'weekStart': w.WEEK}
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'timesheet.approved' AND channel = 'push'")['n'] == 1
    with pytest.raises(ConflictError, match="This week's timesheet is approved and locked."):
        ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 5})
    with pytest.raises(ConflictError, match=r'Only a timesheet waiting for approval can be withdrawn \(it is approved\).'):
        ts.withdraw_timesheet(w.sita, tid)
    actions = [r['action'] for r in __import__('hajiri.core.db', fromlist=['rows']).rows(
        "SELECT action FROM audit_log WHERE table_name = 'timesheet' ORDER BY id")]
    assert actions == ['submit', 'approve']   # the workflow engine audits the decision on the object
    row = one('SELECT status, decided_by::text AS by, decision_comment FROM timesheet WHERE id = %s', [tid])
    assert row == {'status': 'approved', 'by': w.ram.id, 'decision_comment': 'Thanks'}
    with pytest.raises(ConflictError, match='timesheet already approved'):
        ts.apply_timesheet_decision(tid, 'approved', wf.DecisionContext(w.ram.id, None))


def test_returned_notification_without_reason_and_running_timer_blocks_submit(w):
    step_catalogue(w)
    ts.save_settings(w.hr, {'requireProject': False})
    s = ts.start_timer(w.hari)
    execute('UPDATE time_entry SET local_date = %s, started_at = %s WHERE id = %s',
            [w.MON, datetime.fromisoformat(f'{w.MON}T04:00:00+00:00'), s['id']])
    with pytest.raises(ConflictError, match='Stop your running timer before submitting this week.'):
        ts.submit_timesheet(w.hari, {'weekStart': w.WEEK})
    assert ts.week(w.hari, {'weekStart': w.WEEK})['running'] is False   # the forgotten timer was stopped on read
    tid = ts.submit_timesheet(w.hari, {'weekStart': w.WEEK})
    hr_inst = wf.instance_for_object('timesheet', tid)
    # Hari has no manager: HR decides. The engine needs a reason to reject; the handler words a missing one.
    assert hr_inst['employee_id'] == str(w.hari_emp.id)
    from django.db import transaction
    with transaction.atomic():
        ts.apply_timesheet_decision(tid, 'rejected', wf.DecisionContext(w.hr.id, None))
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'timesheet.rejected' AND channel = 'in_app'")
    assert n['title'] == 'Timesheet returned'
    assert n['body'] == f"Your timesheet for {ts.bs_range(w.WEEK, add_days(w.WEEK, 6))} was returned for changes."


# ================================================================ describe: reports

def step_reports(w):
    # summarises by project with rounding, billable amount and budget burn
    r = ts.report_summary(w.hr, {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'groupBy': 'project'})
    hydro = next(x for x in r['rows'] if x['code'] == 'HYDRO-1')
    # 210 + 30 + 30 minutes, each already on a 15-minute boundary.
    assert {k: hydro[k] for k in ('hours', 'billable_hours', 'billable_amount', 'budget_hours', 'budget_used_pct')} == {
        'hours': 4.5, 'billable_hours': 4.5, 'billable_amount': 9000, 'budget_hours': 10, 'budget_used_pct': 45}
    none = next(x for x in r['rows'] if x['key'] == 'none')
    assert (none['hours'], none['billable_hours']) == (1.75, 0)

    # groups by employee, client, activity and day and is scoped
    for g in ('employee', 'client', 'activity', 'day'):
        assert status(ts.report_summary, w.hr, {'from': w.WEEK, 'to': today_npt(), 'groupBy': g}) == 200
    own = ts.report_summary(w.hari, {'from': w.WEEK, 'to': today_npt(), 'groupBy': 'employee'})
    assert [x['label'] for x in own['rows']] == ['Hari Karki']
    team = ts.report_summary(w.ram, {'from': w.WEEK, 'to': today_npt(), 'groupBy': 'employee'})
    assert [x['label'] for x in team['rows']] == ['Sita Sharma']
    assert status(ts.report_summary, w.ram, {'from': w.WEEK, 'to': today_npt(), 'scope': 'all'}) == 403
    assert status(ts.report_summary, w.hr, {'from': today_npt(), 'to': w.WEEK}) == 400
    assert ts.report_summary(w.outsider, {'from': w.WEEK, 'to': today_npt(), 'scope': 'all'})['rows'] == []

    # exports CSV with formula cells neutralised
    ts.add_entry(w.hari, {'date': w.MON, 'durationMin': 15, 'note': '=HYPERLINK("x")'})
    e = ts.report_entries(w.hr, {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'format': 'csv'})
    text = to_csv(e['columns'], e['rows'])
    assert '"\'=HYPERLINK(""x"")"' in text
    assert 'Hours (rounded)' in text
    s = ts.report_summary(w.hr, {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'format': 'csv'})
    assert 'Budget used %' in to_csv(s['columns'], s['rows'])


def test_reports(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)
    step_weekly(w)
    step_reports(w)


def test_report_range_limit_and_entry_times(w):
    step_catalogue(w)
    with pytest.raises(BadRequestError, match='Pick a range of at most 400 days.'):
        ts.report_summary(w.hr, {'from': '2025-01-01', 'to': '2026-02-10'})
    ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '10:20', 'projectId': w.ids['projA']})
    e = ts.report_entries(w.hr, {'from': w.WEEK, 'to': add_days(w.WEEK, 6)})
    assert e['rows'][0] | {} == {'date': w.MON, 'emp_code': 'E001', 'name': 'Sita Sharma', 'client': 'Acme Hydro',
                                 'project': 'Hydro survey', 'activity': '', 'start': '09:00', 'end': '10:20', 'minutes': 80,
                                 'hours': 1.33, 'billable': 'Yes', 'note': ''}
    assert e['filename'] == f'time-entries-{w.WEEK}-to-{add_days(w.WEEK, 6)}'
    s = ts.report_summary(w.hr, {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'groupBy': 'client'})
    assert [c['header'] for c in s['columns']] == ['Client', 'Hours', 'Billable hours', 'Non-billable hours',
                                                    'Billable amount (NPR)', 'Entries']
    assert s['rows'][0]['label'] == 'Acme Hydro' and s['totals'] == {'hours': 1.33, 'billableHours': 1.33, 'billableAmount': 2666.67}


# ================================================================ describe: daily jobs

def test_daily_jobs(w):
    step_catalogue(w)
    this_week = ts.week_start_of(today_npt())
    friday = add_days(this_week, 5)
    at = datetime.fromisoformat(f'{friday}T06:00:00+00:00')    # noon in Nepal
    execute("INSERT INTO time_entry (company_id, employee_id, local_date, duration_min, source) VALUES (%s, %s, %s, 30, 'manual')",
            [w.company.id, w.hari_emp.id, this_week])
    n1 = ts.friday_timesheet_reminders(at)
    assert n1 >= 1
    assert ts.friday_timesheet_reminders(at) == 0
    assert ts.friday_timesheet_reminders(datetime.fromisoformat(f'{add_days(this_week, 4)}T06:00:00+00:00')) == 0
    from hajiri.worker.registry import daily_jobs
    job = next(j for j in daily_jobs() if j.name == 'timesheets')
    assert job.fn(at) == {'autoStopped': 0, 'reminded': 0}
    n = one("SELECT title, body, payload FROM notification_outbox WHERE template = 'timesheet.reminder' AND channel = 'push'")
    assert n['title'] == 'Submit your timesheet'
    assert n['payload'] == {'weekStart': this_week}
    assert n['body'].startswith(f"Your timesheet for {ts.bs_range(this_week, add_days(this_week, 6))} has not been submitted yet.")


def test_friday_reminder_respects_policy(w):
    step_catalogue(w)
    this_week = ts.week_start_of(today_npt())
    at = datetime.fromisoformat(f'{add_days(this_week, 5)}T06:00:00+00:00')
    execute("INSERT INTO time_entry (company_id, employee_id, local_date, duration_min, source) VALUES (%s, %s, %s, 30, 'manual')",
            [w.company.id, w.hari_emp.id, this_week])
    ts.save_settings(w.hr, {'fridayReminder': False})
    assert ts.friday_timesheet_reminders(at) == 0


def test_rounding_and_helpers():
    assert ts.week_start_of('2026-10-31') == '2026-10-25'
    assert ts.is_sunday('2026-10-25') and not ts.is_sunday('2026-10-26')
    assert ts.round_entry(95, {'roundingStep': 0, 'roundingMode': 'up'}) == 95
    assert ts.round_entry(8, {'roundingStep': 5, 'roundingMode': 'nearest'}) == 10
    assert ts.round_entry(8, {'roundingStep': 5, 'roundingMode': 'down'}) == 5
    assert ts.to_fixed(15 / 60, 1) == '0.3'          # JS toFixed: an exact tie goes up
    assert ts.minutes_from_ms(10) == 1 and ts.minutes_from_ms(90_000) == 2
