"""
Contract review of /api/v1/time against the React pages (frontend/src/pages/time/*, api/time.js): the exact bodies the
pages send, through the cookie session they use, and the value formats they read (numeric columns as the strings pg
returned, timestamps as JSON.stringify ISO strings, calendar dates as YYYY-MM-DD, ids as strings, nulls).
"""
from __future__ import annotations

import re

import pytest
from django.test import Client

from tests.support.sql import execute
from tests.web.factories import sign_in

from .helpers import add_days, make_world, today_npt

API = '/api/v1/time'
ISO_MS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
BRAND = '#12773a'


class Browser:
    """The React app: the session cookie, JSON bodies, and the browser's own Origin on writes."""

    def __init__(self, user):
        self.c = Client(enforce_csrf_checks=True)
        sign_in(self.c, user)
        self.h = {'HTTP_ORIGIN': 'http://testserver'}

    def get(self, path, q=None):
        return self.c.get(f'{API}{path}', q or {})

    def post(self, path, body=None):
        if body is None:   # api.post(path) with no body: no Content-Type, empty body
            return self.c.generic('POST', f'{API}{path}', b'', **self.h)
        return self.c.post(f'{API}{path}', body, content_type='application/json', **self.h)

    def put(self, path, body):
        return self.c.put(f'{API}{path}', body, content_type='application/json', **self.h)

    def patch(self, path, body):
        return self.c.patch(f'{API}{path}', body, content_type='application/json', **self.h)

    def delete(self, path):
        return self.c.delete(f'{API}{path}', **self.h)


@pytest.fixture
def w(db):
    w = make_world()
    w.hr_b, w.sita_b, w.ram_b, w.out_b = Browser(w.hr), Browser(w.sita), Browser(w.ram), Browser(w.outsider)
    return w


def _catalogue(w):
    hr = w.hr_b
    # ProjectsPage: Clients.add
    assert hr.post('/clients', {'name': 'Acme Hydro'}).status_code == 201
    client = hr.get('/clients', {'all': 1}).json()['clients'][0]
    # ProjectModal.save for a new project: the defaults the form starts with.
    r = hr.post('/projects', {'code': 'HYDRO-1', 'name': 'Hydro survey', 'clientId': client['id'], 'color': BRAND,
                              'billable': True, 'billRate': 1500.5, 'budgetHours': 10, 'allMembers': True,
                              'memberIds': [], 'status': 'active'})
    assert r.status_code == 201, r.json()
    w.ids['proj'] = r.json()['id']
    r = hr.post('/projects', {'code': 'INT', 'name': 'Internal', 'clientId': None, 'color': BRAND, 'billable': False,
                              'billRate': None, 'budgetHours': None, 'allMembers': False,
                              'memberIds': [str(w.sita_emp.id)], 'status': 'active'})
    assert r.status_code == 201, r.json()
    w.ids['int'] = r.json()['id']
    # Activities.add: projectId null (every project) or a project
    r = hr.post('/activities', {'name': 'Meetings', 'projectId': None})
    assert r.status_code == 201
    w.ids['act'] = r.json()['activity']['id']
    return client


def test_catalogue_values_the_projects_page_reads(w):
    _catalogue(w)
    client = w.hr_b.get('/clients').json()['clients'][0]
    assert client == {'id': client['id'], 'name': 'Acme Hydro', 'notes': None, 'archived': False, 'active_projects': 1}
    rows = w.hr_b.get('/projects', {'all': 1}).json()['projects']
    hydro = next(p for p in rows if p['code'] == 'HYDRO-1')
    # numeric columns come back as the strings pg returned ("1500.50"), as the page does Number(p.bill_rate)
    assert hydro['bill_rate'] == '1500.50' and hydro['budget_hours'] == '10.00'
    assert hydro['client_id'] == client['id'] and hydro['client_name'] == 'Acme Hydro'
    assert (hydro['color'], hydro['billable'], hydro['all_members'], hydro['status'], hydro['tracked_min']) == (
        BRAND, True, True, 'active', 0)
    internal = next(p for p in rows if p['code'] == 'INT')
    assert (internal['bill_rate'], internal['budget_hours'], internal['client_id'], internal['client_name']) == (
        None, None, None, None)
    assert internal['members'] == [{'id': str(w.sita_emp.id), 'name': 'Sita Sharma', 'empCode': 'E001'}]

    # ProjectModal.save on edit: the whole form back, built from the row as the page reads it
    p = internal
    body = {'code': p['code'], 'name': p['name'], 'clientId': p['client_id'], 'color': p['color'], 'billable': p['billable'],
            'billRate': None if p['bill_rate'] is None else float(p['bill_rate']),
            'budgetHours': None if p['budget_hours'] is None else float(p['budget_hours']),
            'allMembers': p['all_members'], 'memberIds': [m['id'] for m in p['members']], 'status': 'archived'}
    r = w.hr_b.patch(f"/projects/{p['id']}", body)
    assert r.status_code == 200 and r.json() == {'ok': True}
    # an archived project disappears from the pickers and stays in the HR list
    assert [x['code'] for x in w.sita_b.get('/projects').json()['projects']] == ['HYDRO-1']
    assert {x['code']: x['status'] for x in w.hr_b.get('/projects', {'all': 1}).json()['projects']} == {
        'HYDRO-1': 'active', 'INT': 'archived'}
    # clearing a rate and a budget (NumberInput emptied: null)
    r = w.hr_b.patch(f"/projects/{w.ids['proj']}", {'billRate': None, 'budgetHours': None, 'clientId': None,
                                                    'allMembers': True, 'memberIds': []})
    assert r.status_code == 200
    hydro = next(x for x in w.hr_b.get('/projects', {'all': 1}).json()['projects'] if x['code'] == 'HYDRO-1')
    assert (hydro['bill_rate'], hydro['budget_hours'], hydro['client_id']) == (None, None, None)

    # Clients.toggle / Activities.toggle
    r = w.hr_b.patch(f"/clients/{client['id']}", {'archived': True})
    assert r.status_code == 200 and r.json()['client'] == {'id': client['id'], 'name': 'Acme Hydro', 'notes': None,
                                                          'archived': True}
    acts = w.hr_b.get('/activities', {'all': 1}).json()['activities']
    assert acts == [{'id': w.ids['act'], 'name': 'Meetings', 'project_id': None, 'project_name': None, 'archived': False}]
    r = w.hr_b.patch(f"/activities/{w.ids['act']}", {'archived': True})
    assert r.json()['activity'] == {'id': w.ids['act'], 'name': 'Meetings', 'project_id': None, 'archived': True}
    assert w.sita_b.get('/activities').json()['activities'] == []
    # an employee's all=1 is ignored (TS: only HR sees archived ones)
    assert w.sita_b.get('/activities', {'all': 1}).json()['activities'] == []
    # another company's ids are not found
    assert w.out_b.patch(f"/clients/{client['id']}", {'archived': False}).status_code == 404
    assert w.out_b.patch(f"/projects/{w.ids['proj']}", {'name': 'x'}).status_code == 404
    assert w.out_b.get('/projects', {'all': 1}).json()['projects'] == []


def test_settings_round_trip_as_the_settings_card_saves_it(w):
    pol = w.hr_b.get('/settings').json()['policy']
    assert pol == {'enabled': True, 'requireProject': False, 'roundingStep': 0, 'roundingMode': 'nearest',
                   'fridayReminder': True, 'autoStopHours': 12}
    r = w.hr_b.put('/settings', {**pol, 'roundingStep': 15, 'roundingMode': 'up', 'autoStopHours': 8})
    assert r.status_code == 200 and r.json()['policy']['autoStopHours'] == 8
    assert w.sita_b.get('/settings').json()['policy']['roundingStep'] == 15
    # the other company keeps its own policy
    assert w.out_b.get('/settings').json()['policy']['roundingStep'] == 0


def test_timer_card_and_entry_modal_bodies(w):
    _catalogue(w)
    s = w.sita_b
    t = s.get('/timer').json()
    assert t['running'] is None and t['last'] is None and t['todayMin'] == 0
    assert ISO_MS.match(t['serverTime'])
    # TimerCard.start sends nulls for an empty picker and note
    r = s.post('/timer/start', {'projectId': None, 'activityId': None, 'note': None})
    assert r.status_code == 201, r.json()
    run = r.json()['running']
    assert ISO_MS.match(run['started_at']) and run['ended_at'] is None and DAY.match(run['local_date'])
    assert (run['project_id'], run['project_name'], run['project_color'], run['billable']) == (None, None, None, False)
    # EntryModal on a running timer sends only the work fields
    r = s.patch(f"/entries/{run['id']}", {'projectId': w.ids['proj'], 'activityId': w.ids['act'], 'note': None})
    assert r.status_code == 200, r.json()
    assert (r.json()['entry']['project_name'], r.json()['entry']['activity_name'], r.json()['entry']['billable']) == (
        'Hydro survey', 'Meetings', True)
    # a time change on a running timer is refused
    assert s.patch(f"/entries/{run['id']}", {'durationMin': 5}).status_code == 400
    # TimerCard.stop with a note of null (empty box)
    r = s.post('/timer/stop', {'note': None})
    assert r.status_code == 200
    stopped = r.json()['entry']
    assert stopped['note'] is None and ISO_MS.match(stopped['ended_at']) and stopped['duration_min'] >= 1
    t = s.get('/timer').json()
    assert t['last']['id'] == stopped['id'] and t['last']['project_name'] == 'Hydro survey'
    assert t['todayMin'] == stopped['duration_min']
    # TimerCard.resume posts with no body at all
    r = s.post('/timer/resume')
    assert r.status_code == 201 and r.json()['running']['project_id'] == w.ids['proj']
    assert s.post('/timer/stop', {}).status_code == 200

    # EntryModal.save, range mode and duration mode, with billable omitted (null in the form)
    r = s.post('/entries', {'date': w.MON, 'projectId': w.ids['proj'], 'activityId': None, 'note': None,
                            'start': '09:00', 'end': '10:15'})
    assert r.status_code == 201, r.json()
    e = r.json()['entry']
    assert (e['duration_min'], e['billable'], e['note'], e['local_date'], e['client_name']) == (75, True, None, w.MON, 'Acme Hydro')
    assert ISO_MS.match(e['started_at']) and e['started_at'] == f'{w.MON}T03:15:00.000Z'   # 09:00 NPT
    r = s.post('/entries', {'date': w.TUE, 'projectId': w.ids['int'], 'activityId': w.ids['act'], 'note': 'Notes',
                            'billable': True, 'durationMin': 45})
    assert r.status_code == 201, r.json()
    d = r.json()['entry']
    assert (d['started_at'], d['ended_at'], d['duration_min'], d['billable']) == (None, None, 45, True)
    # editing a finished entry sends the full body again (switching to duration mode clears the times)
    r = s.patch(f"/entries/{e['id']}", {'date': w.MON, 'projectId': w.ids['proj'], 'activityId': None, 'note': 'x',
                                         'durationMin': 30})
    assert r.status_code == 200
    assert (r.json()['entry']['started_at'], r.json()['entry']['duration_min']) == (None, 30)
    # the outsider cannot touch it
    assert w.out_b.patch(f"/entries/{e['id']}", {'note': 'x'}).status_code == 404
    assert w.out_b.delete(f"/entries/{e['id']}").status_code == 404


def test_my_timesheet_and_review_pages(w):
    _catalogue(w)
    s, ram, hr = w.sita_b, w.ram_b, w.hr_b
    s.post('/entries', {'date': w.MON, 'projectId': w.ids['proj'], 'start': '09:00', 'end': '12:00'})
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash)
               VALUES (%s, %s, 1, 'Present', 200, 'h')""", [str(w.sita_emp.id), w.MON])
    wk = s.get('/week', {'weekStart': w.WEEK}).json()
    assert (wk['weekStart'], wk['weekEnd'], wk['running'], wk['timesheet'], wk['history']) == (
        w.WEEK, add_days(w.WEEK, 6), False, None, [])
    mon = next(d for d in wk['days'] if d['date'] == w.MON)
    assert (mon['attendanceMin'], mon['attendanceStatus'], mon['diffMin'], mon['flag']) == (200, 'Present', -20, None)
    sun = wk['days'][0]
    assert (sun['attendanceMin'], sun['diffMin'], sun['flag'], sun['entries']) == (None, None, None, [])

    # MyTimesheetPage.submit: { weekStart } without a note when the box is empty
    r = s.post('/timesheets', {'weekStart': w.WEEK})
    assert r.status_code == 201
    tid = r.json()['id']
    ts = s.get('/week', {'weekStart': w.WEEK}).json()['timesheet']
    assert ts['id'] == tid and ts['status'] == 'submitted' and ts['week_start'] == w.WEEK
    assert ISO_MS.match(ts['submitted_at']) and ts['decided_at'] is None and ts['decided_by_name'] is None
    assert ts['total_min'] == 180 and ts['note'] is None and isinstance(ts['workflow_instance_id'], str)

    # ToApprove / History rows
    rows = ram.get('/timesheets', {'status': 'submitted'}).json()['timesheets']
    assert len(rows) == 1
    row = rows[0]
    assert (row['id'], row['name_en'], row['emp_code'], row['week_start'], row['total_min'], row['attendance_min'],
            row['diff_min'], row['billable_min'], row['workflow_state']) == (
        tid, 'Sita Sharma', 'E001', w.WEEK, 180, 200, -20, 180, 'pending')
    assert ISO_MS.match(row['submitted_at'])
    assert hr.get('/timesheets').json()['timesheets'][0]['id'] == tid
    assert w.out_b.get('/timesheets').json()['timesheets'] == []

    # ReviewDrawer
    d = ram.get(f'/timesheets/{tid}').json()
    assert d['employee'] == {'id': str(w.sita_emp.id), 'empCode': 'E001', 'name': 'Sita Sharma'}
    assert d['weekStart'] == w.WEEK and d['totals']['roundedMin'] == 180 and d['totals']['attendanceMin'] == 200
    assert d['submission']['id'] == tid and d['submission']['workflow_instance_id'] == ts['workflow_instance_id']

    # TeamWeek
    tw = ram.get('/team-week', {'weekStart': w.WEEK}).json()
    assert tw['people'] == [{'employee_id': str(w.sita_emp.id), 'emp_code': 'E001', 'name_en': 'Sita Sharma',
                             'tracked_min': 180, 'attendance_min': 200, 'timesheet_id': tid, 'status': 'submitted'}]
    # HR sees everyone, including people with no timesheet (null id and status)
    hr_people = {p['name_en']: p for p in hr.get('/team-week', {'weekStart': w.WEEK}).json()['people']}
    assert hr_people['Hari Karki']['timesheet_id'] is None and hr_people['Hari Karki']['status'] is None
    assert hr_people['Hari Karki']['tracked_min'] == 0 and hr_people['Hari Karki']['attendance_min'] == 0
    assert 'Outsider' not in hr_people

    # MyTimesheetPage.withdraw posts with no body
    r = s.post(f'/timesheets/{tid}/withdraw')
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert s.get('/week', {'weekStart': w.WEEK}).json()['timesheet']['status'] == 'cancelled'


def test_hours_report_values(w):
    _catalogue(w)
    w.sita_b.post('/entries', {'date': w.MON, 'projectId': w.ids['proj'], 'durationMin': 90})
    w.sita_b.post('/entries', {'date': w.TUE, 'durationMin': 20})
    q = {'from': w.WEEK, 'to': today_npt(), 'groupBy': 'project'}
    r = w.hr_b.get('/reports/summary', q).json()
    hydro = next(x for x in r['rows'] if x['code'] == 'HYDRO-1')
    assert hydro == {'key': w.ids['proj'], 'label': 'Hydro survey', 'code': 'HYDRO-1', 'entries': 1, 'tracked_hours': 1.5,
                     'hours': 1.5, 'billable_hours': 1.5, 'non_billable_hours': 0, 'billable_amount': 2250.75,
                     'budget_hours': 10, 'budget_used_pct': 15}
    none = next(x for x in r['rows'] if x['key'] == 'none')
    assert (none['label'], none['code'], none['budget_hours'], none['budget_used_pct'], none['billable_amount']) == (
        'No project', None, None, None, 0)
    assert r['totals'] == {'hours': 1.83, 'billableHours': 1.5, 'billableAmount': 2250.75}
    assert r['rounding'] == {'step': 0, 'mode': 'nearest'}
    assert [c['key'] for c in r['columns']] == ['label', 'code', 'hours', 'billable_hours', 'non_billable_hours',
                                               'billable_amount', 'budget_hours', 'budget_used_pct', 'entries']
    day = w.hr_b.get('/reports/summary', {**q, 'groupBy': 'day'}).json()['rows']
    assert [x['label'] for x in day] == [w.MON, w.TUE]
    # the downloads (api.download of the same URLs with format=csv)
    csv = w.hr_b.get('/reports/summary', {**q, 'format': 'csv'})
    assert csv.status_code == 200 and 'text/csv' in csv['Content-Type']
    ent = w.hr_b.get('/reports/entries', {'from': w.WEEK, 'to': today_npt(), 'format': 'csv'})
    assert ent.status_code == 200 and 'Minutes tracked' in ent.content.decode('utf-8-sig')
    # another company's report is empty
    assert w.out_b.get('/reports/summary', q).json()['rows'] == []


def _decide(ram, path, body):
    """wfApi.approve / wfApi.reject. The workflow views (another slice) still demand Django's CSRF token on cookie
    posts, which the React app never sends, so the decision itself goes with Ram's Bearer token here; the cookie path
    is pinned by test_workflow_decision_from_the_browser_cookie below."""
    from hajiri.core import auth
    from tests.web.factories import PW, bearer
    return Client().post(f'/api/v1/workflow{path}', body, content_type='application/json',
                         **bearer(auth.login(ram.user.email, PW).token))


def test_review_drawer_decisions_show_on_the_week_and_lists(w):
    """ReviewDrawer.decide: wfApi.approve(instanceId, {reason}) / wfApi.reject(instanceId, reason)."""
    _catalogue(w)
    s, ram = w.sita_b, w.ram_b
    ram.user = w.ram
    s.post('/entries', {'date': w.MON, 'projectId': w.ids['proj'], 'durationMin': 60})
    tid = s.post('/timesheets', {'weekStart': w.WEEK, 'note': 'Week one'}).json()['id']
    inst = ram.get(f'/timesheets/{tid}').json()['submission']['workflow_instance_id']
    r = _decide(ram, f'/{inst}/reject', {'reason': 'Split Monday'})
    assert r.status_code == 200, r.content
    sub = ram.get(f'/timesheets/{tid}').json()['submission']
    assert (sub['status'], sub['decision_comment'], sub['decided_by_name'], sub['note']) == (
        'rejected', 'Split Monday', 'Ram Thapa', 'Week one')
    assert ISO_MS.match(sub['decided_at'])
    tid2 = s.post('/timesheets', {'weekStart': w.WEEK}).json()['id']
    inst2 = ram.get(f'/timesheets/{tid2}').json()['submission']['workflow_instance_id']
    r = _decide(ram, f'/{inst2}/approve', {'reason': 'Thanks'})
    assert r.status_code == 200, r.content
    wk = s.get('/week', {'weekStart': w.WEEK}).json()
    assert (wk['timesheet']['status'], wk['timesheet']['decision_comment'], wk['timesheet']['decided_by_name']) == (
        'approved', 'Thanks', 'Ram Thapa')
    assert [h['status'] for h in wk['history']] == ['rejected']
    hist = ram.get('/timesheets').json()['timesheets']
    # (both rows were submitted in the test's one transaction, so now() ties and the order between them is not fixed)
    assert sorted((h['status'], h['decided_by_name']) for h in hist) == [('approved', 'Ram Thapa'), ('rejected', 'Ram Thapa')]
    # a duplicate project code answers the TS conflict body
    r = w.hr_b.post('/projects', {'code': 'hydro-1', 'name': 'Again'})
    assert r.status_code == 409 and r.json()['error'] == 'already exists'


def test_workflow_decision_from_the_browser_cookie(w):
    _catalogue(w)
    w.sita_b.post('/entries', {'date': w.MON, 'projectId': w.ids['proj'], 'durationMin': 60})
    tid = w.sita_b.post('/timesheets', {'weekStart': w.WEEK}).json()['id']
    inst = w.ram_b.get(f'/timesheets/{tid}').json()['submission']['workflow_instance_id']
    r = w.ram_b.c.post(f'/api/v1/workflow/{inst}/approve', {}, content_type='application/json', **w.ram_b.h)
    assert r.status_code == 200, r.content
