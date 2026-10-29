"""
HTTP-level port of apps/api/test/timesheets.test.ts: every assertion of the TS suite replayed against /api/v1/time
(and the workflow inbox it decides through) with django.test.Client, plus the JSON shapes the React app reads
(frontend/src/api/time.js and pages/time/*) and company isolation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.timesheets import services as ts
from tests.support.sql import execute, one
from tests.web.factories import PW, bearer, sign_in

from .helpers import add_days, make_world, today_npt

API = '/api/v1/time'
DEAD = '00000000-0000-7000-8000-00000000dead'

ENTRY_KEYS = {'id', 'employee_id', 'local_date', 'started_at', 'ended_at', 'duration_min', 'project_id', 'project_name',
              'project_code', 'project_color', 'client_name', 'activity_id', 'activity_name', 'note', 'billable', 'source',
              'auto_stopped', 'created_at', 'updated_at'}
TS_KEYS = {'id', 'week_start', 'status', 'total_min', 'note', 'submitted_at', 'decided_at', 'decision_comment',
           'workflow_instance_id', 'decided_by_name'}


class Api:
    """A Bearer-token caller: api.get(path, query) / api.post(path, body) answer the response."""

    def __init__(self, user):
        self.c = Client()
        self.h = bearer(auth.login(user.email, PW).token)

    def get(self, path, q=None):
        return self.c.get(f'{API}{path}', q or {}, **self.h)

    def post(self, path, body=None):
        return self.c.post(f'{API}{path}', body or {}, content_type='application/json', **self.h)

    def put(self, path, body):
        return self.c.put(f'{API}{path}', body, content_type='application/json', **self.h)

    def patch(self, path, body):
        return self.c.patch(f'{API}{path}', body, content_type='application/json', **self.h)

    def delete(self, path):
        return self.c.delete(f'{API}{path}', **self.h)

    def wf(self, method, path, body=None):
        url = f'/api/v1/workflow{path}'
        if method == 'get':
            return self.c.get(url, **self.h)
        return self.c.post(url, body or {}, content_type='application/json', **self.h)


@pytest.fixture
def w(db):
    w = make_world()
    for k in ('hr', 'sita', 'ram', 'hari', 'other', 'outsider'):
        setattr(w, f'api_{k}', Api(getattr(w, k)))
    return w


# ================================================================ describe: clients, projects and activities

def step_catalogue(w):
    hr, sita, ram, outsider = w.api_hr, w.api_sita, w.api_ram, w.api_outsider
    # only HR manages the catalogue
    assert sita.post('/clients', {'name': 'Acme'}).status_code == 403
    assert ram.post('/projects', {'code': 'X', 'name': 'X'}).status_code == 403
    c = hr.post('/clients', {'name': 'Acme Hydro'})
    assert c.status_code == 201
    assert set(c.json()['client']) == {'id', 'name', 'notes', 'archived'}
    w.ids['client'] = c.json()['client']['id']
    assert hr.post('/clients', {'name': 'acme hydro'}).status_code == 409

    # creates projects with members, rates and budgets; validates codes and clients
    a = hr.post('/projects', {'code': 'hydro-1', 'name': 'Hydro survey', 'clientId': w.ids['client'], 'billRate': 2000,
                              'budgetHours': 10})
    assert a.status_code == 201 and set(a.json()) == {'id'}
    w.ids['projA'] = a.json()['id']
    p = hr.post('/projects', {'code': 'SECRET', 'name': 'Internal R&D', 'allMembers': False,
                              'memberIds': [str(w.ram_emp.id)], 'billable': False})
    assert p.status_code == 201
    w.ids['projPrivate'] = p.json()['id']
    assert hr.post('/projects', {'code': 'bad code!', 'name': 'x'}).status_code == 400
    assert hr.post('/projects', {'code': 'HYDRO-1', 'name': 'dup'}).status_code == 409
    assert hr.post('/projects', {'code': 'NOPE', 'name': 'x', 'memberIds': [str(w.other_emp.id)],
                                 'allMembers': False}).status_code == 400
    # another company's client id behaves as not found
    assert outsider.post('/projects', {'code': 'Z', 'name': 'z', 'clientId': w.ids['client']}).status_code == 400
    g = hr.post('/activities', {'name': 'Meetings'})
    w.ids['actGlobal'] = g.json()['activity']['id']
    s = hr.post('/activities', {'name': 'Field work', 'projectId': w.ids['projA']})
    assert s.status_code == 201
    assert set(s.json()['activity']) == {'id', 'name', 'project_id', 'archived'}
    w.ids['actA'] = s.json()['activity']['id']

    # employees see only projects they may use; other companies see nothing
    mine = sita.get('/projects').json()['projects']
    assert [x['code'] for x in mine] == ['HYDRO-1']
    assert mine[0]['members'] == []
    assert set(mine[0]) == {'id', 'code', 'name', 'color', 'billable', 'bill_rate', 'budget_hours', 'all_members', 'status',
                            'client_id', 'client_name', 'tracked_min', 'members'}
    assert len(ram.get('/projects').json()['projects']) == 2
    hr_all = hr.get('/projects', {'all': '1'}).json()['projects']
    members = next(x for x in hr_all if x['id'] == w.ids['projPrivate'])['members']
    assert len(members) == 1 and set(members[0]) == {'id', 'name', 'empCode'}
    assert outsider.get('/projects').json()['projects'] == []
    assert outsider.patch(f"/projects/{w.ids['projA']}", {'name': 'hijack'}).status_code == 404
    acts = sita.get('/activities', {'projectId': w.ids['projA']}).json()['activities']
    assert len(acts) == 2
    assert set(acts[0]) == {'id', 'name', 'project_id', 'project_name', 'archived'}


def test_catalogue(w):
    step_catalogue(w)


def test_catalogue_edits_and_isolation(w):
    step_catalogue(w)
    hr, outsider = w.api_hr, w.api_outsider
    r = hr.patch(f"/clients/{w.ids['client']}", {'archived': True})
    assert r.status_code == 200 and r.json()['client']['archived'] is True
    assert hr.get('/clients').json()['clients'] == []
    listed = hr.get('/clients', {'all': '1'}).json()['clients']
    assert set(listed[0]) == {'id', 'name', 'notes', 'archived', 'active_projects'} and listed[0]['active_projects'] == 1
    assert outsider.get('/clients', {'all': '1'}).json()['clients'] == []
    assert outsider.patch(f"/clients/{w.ids['client']}", {'name': 'x'}).status_code == 404
    assert outsider.patch(f"/activities/{w.ids['actA']}", {'name': 'x'}).status_code == 404
    assert outsider.get('/activities', {'all': '1'}).json()['activities'] == []
    assert hr.post('/activities', {'name': 'x', 'projectId': DEAD}).status_code == 400
    assert hr.patch(f'/projects/{DEAD}', {'name': 'x'}).status_code == 404
    r = hr.patch(f"/projects/{w.ids['projPrivate']}", {'memberIds': [str(w.sita_emp.id)], 'code': 'secret2'})
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert {x['code'] for x in w.api_sita.get('/projects').json()['projects']} == {'HYDRO-1', 'SECRET2'}
    # another company's employee cannot be made a member
    assert hr.patch(f"/projects/{w.ids['projA']}", {'memberIds': [str(w.other_emp.id)]}).status_code == 400


# ================================================================ describe: manual entries

def step_manual(w):
    ids, hr, sita, ram = w.ids, w.api_hr, w.api_sita, w.api_ram
    # logs time with start/end or duration and defaults billable from the project
    a = sita.post('/entries', {'date': w.MON, 'start': '09:00', 'end': '12:30', 'projectId': ids['projA'],
                               'activityId': ids['actA'], 'note': 'Site visit'})
    assert a.status_code == 201
    e = a.json()['entry']
    assert set(e) == ENTRY_KEYS
    assert {k: e[k] for k in ('duration_min', 'billable', 'project_code', 'activity_name', 'source')} == {
        'duration_min': 210, 'billable': True, 'project_code': 'HYDRO-1', 'activity_name': 'Field work', 'source': 'manual'}
    b = sita.post('/entries', {'date': w.TUE, 'durationMin': 95, 'activityId': ids['actGlobal']})
    assert b.status_code == 201
    assert (b.json()['entry']['duration_min'], b.json()['entry']['billable'], b.json()['entry']['started_at']) == (95, False, None)

    # rejects overlaps, future time, bad ranges, more than 24 h a day and projects you are not on
    assert sita.post('/entries', {'date': w.MON, 'start': '12:00', 'end': '13:00'}).status_code == 409
    assert sita.post('/entries', {'date': add_days(today_npt(), 1), 'durationMin': 30}).status_code == 400
    assert sita.post('/entries', {'date': w.MON, 'start': '15:00', 'end': '14:00'}).status_code == 400
    assert sita.post('/entries', {'date': w.MON, 'start': '15:00'}).status_code == 400
    assert sita.post('/entries', {'date': w.MON}).status_code == 400
    assert sita.post('/entries', {'date': w.TUE, 'durationMin': 1400}).status_code == 400
    r = sita.post('/entries', {'date': w.MON, 'durationMin': 30, 'projectId': ids['projPrivate']})
    assert r.status_code == 403 and r.json() == {'error': 'You are not a member of Internal R&D.'}
    assert sita.post('/entries', {'date': w.MON, 'durationMin': 30, 'projectId': ids['projA'],
                                  'activityId': DEAD}).status_code == 400

    # requires a project when the company says so
    put = hr.put('/settings', {'requireProject': True, 'roundingStep': 15, 'roundingMode': 'up'})
    assert put.status_code == 200
    assert sita.put('/settings', {'requireProject': False}).status_code == 403
    assert hr.put('/settings', {'roundingStep': 7}).status_code == 400
    assert sita.post('/entries', {'date': w.TUE, 'durationMin': 10}).status_code == 400
    pol = sita.get('/settings').json()['policy']
    assert pol['requireProject'] is True and pol['roundingStep'] == 15

    # edits and deletes only your own entries
    eid = sita.post('/entries', {'date': w.TUE, 'start': '14:00', 'end': '15:00', 'projectId': ids['projA']}).json()['entry']['id']
    assert ram.patch(f'/entries/{eid}', {'note': 'x'}).status_code == 404
    assert hr.delete(f'/entries/{eid}').status_code == 403
    p = sita.patch(f'/entries/{eid}', {'start': '14:00', 'end': '16:00', 'note': 'longer'})
    assert p.status_code == 200
    assert (p.json()['entry']['duration_min'], p.json()['entry']['note']) == (120, 'longer')
    assert sita.patch(f'/entries/{eid}', {'projectId': ids['projPrivate']}).status_code == 403
    d = sita.delete(f'/entries/{eid}')
    assert d.status_code == 200 and d.json() == {'ok': True}

    # lets a manager read their team and HR everyone, but not others
    q = {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'employeeId': str(w.sita_emp.id)}
    assert len(ram.get('/entries', q).json()['entries']) > 0
    assert hr.get('/entries', q).status_code == 200
    assert w.api_hari.get('/entries', q).status_code == 403
    assert w.api_outsider.get('/entries', q).status_code == 404
    assert w.api_other.get('/week', {'weekStart': w.WEEK, 'employeeId': str(w.sita_emp.id)}).status_code == 403
    assert hr.get('/entries', {'from': w.WEEK, 'to': w.WEEK}).status_code == 403   # HR user without an employee record


def test_manual_entries(w):
    step_catalogue(w)
    step_manual(w)


def test_entry_validation_and_error_shapes(w):
    step_catalogue(w)
    sita = w.api_sita
    r = sita.post('/entries', {'date': '2026-13-40', 'durationMin': 0})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert {i['path'] for i in r.json()['issues']} == {'date', 'durationMin'}
    r = sita.get('/entries', {'to': w.MON})
    assert r.status_code == 400 and r.json()['issues'] == [{'path': 'from', 'message': 'Required'}]
    r = sita.patch('/entries/not-a-uuid', {})
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'Invalid UUID'
    r = sita.post('/entries', {'date': w.MON, 'start': '15:00'})
    assert r.json() == {'error': 'Give both a start and an end time, or just a duration.'}
    # another company's entry behaves as not found
    eid = sita.post('/entries', {'date': w.MON, 'durationMin': 30}).json()['entry']['id']
    assert w.api_outsider.patch(f'/entries/{eid}', {'note': 'x'}).status_code == 404
    assert w.api_outsider.delete(f'/entries/{eid}').status_code == 404


# ================================================================ describe: timer

def step_timer(w):
    hr, hari = w.api_hr, w.api_hari
    hr.put('/settings', {'requireProject': False})
    s = hari.post('/timer/start', {'note': 'Inbox'})
    assert s.status_code == 201
    running = s.json()['running']
    assert set(running) == ENTRY_KEYS
    assert (running['source'], running['duration_min'], running['note']) == ('timer', None, 'Inbox')
    assert hari.post('/timer/start', {}).status_code == 409
    t = hari.get('/timer').json()
    assert set(t) == {'running', 'last', 'todayMin', 'serverTime'}
    assert t['running']['id'] == running['id']
    # Nothing may be logged after a running timer started.
    now = datetime.now(timezone.utc) + timedelta(minutes=345)
    if now.hour >= 1:
        start, end = now - timedelta(minutes=50), now - timedelta(minutes=1)
        if start.date() == end.date():
            execute("UPDATE time_entry SET started_at = now() - interval '55 minutes' WHERE id = %s", [running['id']])
            assert hari.post('/entries', {'date': today_npt(), 'start': start.strftime('%H:%M'),
                                          'end': end.strftime('%H:%M')}).status_code == 409
    stop = hari.post('/timer/stop', {'note': 'Inbox zero'})
    assert stop.status_code == 200
    assert stop.json()['entry']['duration_min'] >= 1
    assert stop.json()['entry']['note'] == 'Inbox zero'
    assert hari.post('/timer/stop', {}).status_code == 409
    r = hari.post('/timer/resume', {})
    assert r.status_code == 201
    assert r.json()['running']['note'] == 'Inbox zero'
    hari.post('/timer/stop', {})

    # auto-stops a forgotten timer at the company limit and tells the employee
    s = hari.post('/timer/start', {}).json()['running']
    execute('DELETE FROM time_entry WHERE employee_id = %s AND id <> %s', [w.hari_emp.id, s['id']])
    execute("UPDATE time_entry SET started_at = now() - interval '13 hours' WHERE id = %s", [s['id']])
    assert hari.get('/timer').json()['running'] is None
    assert one('SELECT duration_min, auto_stopped FROM time_entry WHERE id = %s', [s['id']]) == {
        'duration_min': 720, 'auto_stopped': True}
    n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'time.timer_auto_stopped' AND employee_id = %s",
            [w.hari_emp.id])
    assert n['n'] == 1
    assert ts.auto_stop_stale_timers() == 0

    # employees without an employee record cannot track time; turning tracking off blocks it
    assert hr.post('/timer/start', {}).status_code == 403
    hr.put('/settings', {'enabled': False})
    assert hari.post('/timer/start', {}).status_code == 403
    assert hari.post('/entries', {'date': w.MON, 'durationMin': 5}).status_code == 403
    hr.put('/settings', {'enabled': True, 'roundingStep': 15, 'roundingMode': 'up'})


def test_timer(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)


# ================================================================ describe: weekly timesheets

def step_weekly(w):
    sita, ram, hari, hr, outsider = w.api_sita, w.api_ram, w.api_hari, w.api_hr, w.api_outsider
    # shows the week with rounding and the attendance comparison
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash)
               VALUES (%s, %s, 1, 'Present', 480, 'h')""", [str(w.sita_emp.id), w.MON])
    r = sita.get('/week', {'weekStart': w.MON})
    assert r.status_code == 200
    wk = r.json()
    assert set(wk) == {'weekStart', 'weekEnd', 'days', 'totals', 'running', 'timesheet', 'history', 'rounding'}
    assert wk['weekStart'] == w.WEEK
    mon = next(d for d in wk['days'] if d['date'] == w.MON)
    assert set(mon) == {'date', 'entries', 'trackedMin', 'roundedMin', 'billableMin', 'attendanceMin', 'attendanceStatus',
                        'diffMin', 'flag'}
    assert {k: mon[k] for k in ('trackedMin', 'roundedMin', 'attendanceMin', 'diffMin', 'flag')} == {
        'trackedMin': 210, 'roundedMin': 210, 'attendanceMin': 480, 'diffMin': -270, 'flag': 'under'}
    tue = next(d for d in wk['days'] if d['date'] == w.TUE)
    assert {k: tue[k] for k in ('trackedMin', 'roundedMin', 'flag')} == {'trackedMin': 95, 'roundedMin': 105, 'flag': 'no_attendance'}
    assert wk['totals']['billableMin'] == 210
    assert set(wk['totals']) == {'trackedMin', 'roundedMin', 'billableMin', 'attendanceMin', 'flaggedDays'}
    assert wk['rounding'] == {'step': 15, 'mode': 'up'}

    # submits a week, locks it, and lets the employee withdraw
    assert sita.post('/timesheets', {'weekStart': w.MON}).status_code == 400
    assert sita.post('/timesheets', {'weekStart': add_days(w.WEEK, -7)}).status_code == 400   # empty week
    s = sita.post('/timesheets', {'weekStart': w.WEEK, 'note': 'Site week'})
    assert s.status_code == 201
    ts_id = s.json()['id']
    assert sita.post('/timesheets', {'weekStart': w.WEEK}).status_code == 409
    assert sita.post('/entries', {'date': w.TUE, 'durationMin': 10, 'projectId': w.ids['projA']}).status_code == 409
    # another company cannot withdraw it
    assert outsider.post(f'/timesheets/{ts_id}/withdraw').status_code == 404
    wd = sita.post(f'/timesheets/{ts_id}/withdraw')
    assert wd.status_code == 200 and wd.json() == {'ok': True}
    assert sita.post(f'/timesheets/{ts_id}/withdraw').status_code == 409
    assert sita.post('/entries', {'date': w.TUE, 'start': '17:00', 'end': '17:30', 'projectId': w.ids['projA']}).status_code == 201

    # the manager rejects with a reason, the employee fixes and resubmits, the manager approves
    s = sita.post('/timesheets', {'weekStart': w.WEEK})
    assert s.status_code == 201
    ts_id = s.json()['id']
    inbox = ram.wf('get', '/inbox')
    assert inbox.status_code == 200
    item = next(i for i in inbox.json()['items'] if i['objectId'] == ts_id)
    assert item['objectType'] == 'timesheet'
    assert {k: item['summary'][k] for k in ('weekStart', 'totalMin', 'attendanceMin')} == {
        'weekStart': w.WEEK, 'totalMin': 335, 'attendanceMin': 480}
    assert next((i for i in hari.wf('get', '/inbox').json()['items'] if i['objectId'] == ts_id), None) is None
    rej = ram.wf('post', f"/{item['instanceId']}/reject", {'reason': 'Add Tuesday project'})
    assert rej.status_code == 200
    after = sita.get('/week', {'weekStart': w.WEEK}).json()
    assert set(after['timesheet']) == TS_KEYS
    assert (after['timesheet']['status'], after['timesheet']['decision_comment']) == ('rejected', 'Add Tuesday project')
    note = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'timesheet.rejected' AND employee_id = %s",
               [str(w.sita_emp.id)])
    assert note['n'] > 0
    # Returned weeks are open again.
    assert sita.post('/entries', {'date': w.TUE, 'start': '18:00', 'end': '18:30', 'projectId': w.ids['projA']}).status_code == 201
    again = sita.post('/timesheets', {'weekStart': w.WEEK})
    assert again.status_code == 201
    again_id = again.json()['id']
    item2 = next(i for i in ram.wf('get', '/inbox').json()['items'] if i['objectId'] == again_id)
    assert ram.wf('post', f"/{item2['instanceId']}/approve", {}).status_code == 200
    final = sita.get('/week', {'weekStart': w.WEEK}).json()
    assert final['timesheet']['status'] == 'approved'
    assert len(final['history']) == 2
    assert sita.post('/entries', {'date': w.MON, 'durationMin': 5, 'projectId': w.ids['projA']}).status_code == 409
    assert sita.post(f'/timesheets/{again_id}/withdraw').status_code == 409

    # lists timesheets by scope and shows details only to those who manage the person
    team = ram.get('/timesheets').json()['timesheets']
    assert len(team) >= 3
    assert 'diff_min' in team[0]
    assert set(team[0]) == {'id', 'employee_id', 'emp_code', 'name_en', 'week_start', 'status', 'total_min', 'note',
                            'submitted_at', 'decided_at', 'decision_comment', 'workflow_instance_id', 'workflow_state',
                            'decided_by_name', 'attendance_min', 'billable_min', 'diff_min'}
    assert ram.get('/timesheets', {'scope': 'all'}).status_code == 403
    assert hari.get('/timesheets').json()['timesheets'] == []
    assert len(hr.get('/timesheets', {'scope': 'all'}).json()['timesheets']) >= 3
    assert outsider.get('/timesheets', {'scope': 'all'}).json()['timesheets'] == []
    tid = team[0]['id']
    d = ram.get(f'/timesheets/{tid}')
    assert d.status_code == 200
    body = d.json()
    assert body['employee'] == {'id': str(w.sita_emp.id), 'empCode': 'E001', 'name': 'Sita Sharma'}
    assert len(body['days']) == 7
    assert set(body['submission']) == TS_KEYS
    assert hari.get(f'/timesheets/{tid}').status_code == 403
    assert outsider.get(f'/timesheets/{tid}').status_code == 404
    tw = ram.get('/team-week', {'weekStart': w.WEEK}).json()
    assert set(tw) == {'weekStart', 'weekEnd', 'people'}
    assert [p['name_en'] for p in tw['people']] == ['Sita Sharma']
    assert set(tw['people'][0]) == {'employee_id', 'emp_code', 'name_en', 'tracked_min', 'attendance_min', 'timesheet_id', 'status'}
    assert tw['people'][0]['status'] == 'approved'
    assert [p['name_en'] for p in outsider.get('/team-week', {'weekStart': w.WEEK}).json()['people']] == ['Outsider']


def test_weekly_timesheets(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)
    step_weekly(w)


# ================================================================ describe: reports

def step_reports(w):
    hr, hari, ram, outsider = w.api_hr, w.api_hari, w.api_ram, w.api_outsider
    # summarises by project with rounding, billable amount and budget burn
    r = hr.get('/reports/summary', {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'groupBy': 'project'})
    assert r.status_code == 200
    assert set(r.json()) == {'columns', 'rows', 'totals', 'rounding'}
    hydro = next(x for x in r.json()['rows'] if x['code'] == 'HYDRO-1')
    # 210 + 30 + 30 minutes, each already on a 15-minute boundary.
    assert {k: hydro[k] for k in ('hours', 'billable_hours', 'billable_amount', 'budget_hours', 'budget_used_pct')} == {
        'hours': 4.5, 'billable_hours': 4.5, 'billable_amount': 9000, 'budget_hours': 10, 'budget_used_pct': 45}
    none = next(x for x in r.json()['rows'] if x['key'] == 'none')
    assert (none['hours'], none['billable_hours']) == (1.75, 0)

    # groups by employee, client, activity and day and is scoped
    for g in ('employee', 'client', 'activity', 'day'):
        assert hr.get('/reports/summary', {'from': w.WEEK, 'to': today_npt(), 'groupBy': g}).status_code == 200
    own = hari.get('/reports/summary', {'from': w.WEEK, 'to': today_npt(), 'groupBy': 'employee'}).json()
    assert [x['label'] for x in own['rows']] == ['Hari Karki']
    team = ram.get('/reports/summary', {'from': w.WEEK, 'to': today_npt(), 'groupBy': 'employee'}).json()
    assert [x['label'] for x in team['rows']] == ['Sita Sharma']
    assert ram.get('/reports/summary', {'from': w.WEEK, 'to': today_npt(), 'scope': 'all'}).status_code == 403
    assert hr.get('/reports/summary', {'from': today_npt(), 'to': w.WEEK}).status_code == 400
    assert outsider.get('/reports/summary', {'from': w.WEEK, 'to': today_npt(), 'scope': 'all'}).json()['rows'] == []
    assert outsider.get('/reports/entries', {'from': w.WEEK, 'to': today_npt(), 'scope': 'all'}).json()['rows'] == []

    # exports CSV with formula cells neutralised
    hari.post('/entries', {'date': w.MON, 'durationMin': 15, 'note': '=HYPERLINK("x")'})
    e = hr.get('/reports/entries', {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'format': 'csv'})
    assert e.status_code == 200
    assert 'text/csv' in e['Content-Type']
    text = e.content.decode('utf-8-sig')
    assert '"\'=HYPERLINK(""x"")"' in text
    assert 'Hours (rounded)' in text
    s = hr.get('/reports/summary', {'from': w.WEEK, 'to': add_days(w.WEEK, 6), 'format': 'csv'})
    assert 'Budget used %' in s.content.decode('utf-8-sig')
    assert s['Content-Disposition'] == f'attachment; filename="time-by-project-{w.WEEK}-to-{add_days(w.WEEK, 6)}.csv"'


def test_reports(w):
    step_catalogue(w)
    step_manual(w)
    step_timer(w)
    step_weekly(w)
    step_reports(w)


# ================================================================ the React app's cookie session

def test_react_cookie_session_uses_the_origin_check(w):
    """The React app sends the session cookie from fetch() without a CSRF token, as it did against the TS API:
    same-origin (or no Origin) posts pass, a foreign Origin is refused."""
    step_catalogue(w)
    c = Client(enforce_csrf_checks=True)
    sign_in(c, w.sita)
    assert c.get(f'{API}/timer').status_code == 200
    r = c.post(f'{API}/timer/start', {'projectId': w.ids['projA']}, content_type='application/json',
               HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201
    r = c.post(f'{API}/timer/stop', {}, content_type='application/json', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert c.post(f'{API}/timer/stop', {}, content_type='application/json').status_code == 200
