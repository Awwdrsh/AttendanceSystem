"""HTTP-level port of the TS tests that drove /api/v1/shifts/... (apps/api/test/shifts.test.ts, shifts-qa.test.ts and
the shifts cases of endpoints.test.ts, security-authz.test.ts, fixes-payroll-auth.test.ts, fixes-attendance.test.ts at
commit 65a9079): the paths, bodies, answers and status codes the React app relies on. Also the cookie session the React
app signs in with, and company isolation for every id-taking endpoint."""
from __future__ import annotations

import json
from datetime import date

import pytest
from django.db import connection
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import (
    Branch, Company, Employee, EmployeeAssignment, Holiday, Location, NotificationOutbox, RecomputeQueue, Roster, Shift,
    ShiftAssignment,
)
from tests.web.factories import bearer, make_employee

UNKNOWN = '00000000-0000-7000-8000-0000000000ff'


def _caller(client: Client, headers: dict):
    def call(method, path, body=None, **query):
        url = f'/api/v1/shifts{path}'
        if method == 'get':
            return client.get(url, query, **headers)
        if method == 'delete' and body is None:
            return client.delete(url, **headers)
        return getattr(client, method)(url, json.dumps(body if body is not None else {}), content_type='application/json',
                                       **headers)
    return call


@pytest.fixture
def api(demo, login_as):
    """api(roles, employee_id=None) -> call(method, path, body=None, **query) with a fresh Bearer session."""
    def make(roles, employee_id=None):
        return _caller(Client(), bearer(login_as(roles, employee_id).token))
    return make


def _report_to(employee_id: str, manager_id: str) -> None:
    EmployeeAssignment.objects.filter(employee_id=employee_id).update(manager_id=manager_id)


def _lock(employee_id: str, day: str) -> None:
    with connection.cursor() as cur:
        cur.execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at)
                       VALUES (%s, %s, 1, 'Present', 'h', now())""", [employee_id, day])


# ---------------------------------------------------------------- shifts.test.ts

def test_night_shift_crosses_midnight_and_bad_times_are_refused(api, demo):
    hr = api(['hr'])
    r = hr('post', '', {'code': 'NIGHT', 'nameEn': 'Night', 'start': '22:00', 'end': '06:00', 'breakMinutes': 30})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    night = next(s for s in hr('get', '').json()['shifts'] if s['code'] == 'NIGHT')
    assert night['crosses_midnight'] is True and night['assigned_employees'] == '0'   # count(*) as text, like pg int8
    assert night['start'] == '22:00:00' and night['end'] == '06:00:00' and night['break_minutes'] == 30
    assert hr('post', '', {'code': 'BAD', 'nameEn': 'Bad', 'start': '25:00', 'end': '06:00'}).status_code == 400


def test_assign_is_effective_dated_and_a_shift_in_use_is_not_deleted(api, demo):
    hr = api(['hr'])
    night = hr('post', '', {'code': 'NIGHT', 'nameEn': 'Night', 'start': '22:00', 'end': '06:00'}).json()['id']
    a = hr('post', '/assign', {'employeeId': demo['ram'], 'shiftId': night, 'from': '2026-10-01'})
    assert a.status_code == 201 and a.json() == {'ok': True}
    rows = sorted(ShiftAssignment.objects.filter(employee_id=demo['ram']), key=lambda x: x.valid.lower)
    assert len(rows) == 2
    assert str(rows[0].shift_id) == demo['shift_id'] and (rows[0].valid.lower, rows[0].valid.upper) == (date(2026, 1, 1), date(2026, 10, 1))
    d = hr('delete', f'/{night}')
    assert d.status_code == 409 and 'assigned to 1 employee' in d.json()['error']
    assert hr('post', '/assign', {'employeeId': UNKNOWN, 'shiftId': night, 'from': '2026-10-01'}).json() == {'error': 'employee or shift not found'}


def test_roster_upsert_queues_recompute_refuses_locked_days_and_copies_a_week(api, demo):
    hr = api(['hr'])
    night = hr('post', '', {'code': 'NIGHT', 'nameEn': 'Night', 'start': '22:00', 'end': '06:00'}).json()['id']
    RecomputeQueue.objects.filter(employee_id=demo['sita']).delete()
    r = hr('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-10-20', 'shiftId': night},
                                       {'employeeId': demo['sita'], 'localDate': '2026-10-21', 'shiftId': None}]})
    assert r.status_code == 200 and r.json() == {'upserted': 2}
    queued = sorted(d.isoformat() for d in RecomputeQueue.objects.filter(employee_id=demo['sita']).values_list('local_date', flat=True))
    assert queued == ['2026-10-20', '2026-10-21']
    _lock(demo['sita'], '2026-10-22')
    locked = hr('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-10-22', 'shiftId': night}]})
    assert locked.status_code == 409 and locked.json() == {'error': 'attendance for 2026-10-22 is locked by payroll'}
    assert hr('post', '/roster/copy-week', {'weekStart': '2026-10-25'}).json() == {'copied': 2, 'skippedLocked': 0}


def test_holiday_import_is_idempotent_and_the_calendar_marks_saturdays(api, demo):
    hr = api(['hr'])
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json() == {'inserted': 42, 'total': 42}
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json()['inserted'] == 0
    days = hr('get', '/calendar', **{'from': '2026-10-17', 'to': '2026-10-24'}).json()['days']
    sat = next(d for d in days if d['date'] == '2026-10-17')
    assert sat['weeklyOff'] is True and sat['bsDate'] == '2083-06-31' and sat['weekday'] == 6
    assert 'Dashain' in sat['holidays'][0]['name_en']
    assert set(sat['holidays'][0]) == {'local_date', 'name_en', 'name_ne', 'gender_scope', 'optional', 'branch_id'}
    h = hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'})
    assert h.status_code == 201 and h.json()['recomputeQueued'] == 2


def test_locations_crud(api, demo):
    hr = api(['hr'])
    c = hr('post', '/locations', {'name': 'HQ', 'lat': 27.7172, 'lng': 85.324, 'radiusM': 100})
    assert c.status_code == 201
    locs = hr('get', '/locations').json()['locations']
    assert len(locs) == 1 and set(locs[0]) == {'id', 'branch_id', 'name', 'lat', 'lng', 'radius_m'}
    assert hr('delete', f"/locations/{c.json()['id']}").status_code == 200


# ---------------------------------------------------------------- shifts-qa.test.ts

@pytest.fixture
def team(demo, api):
    """Sita reports to Ram (a manager); an unrelated manager with an employee record of their own."""
    _report_to(demo['sita'], demo['ram'])
    other = make_employee(Company.objects.get(pk=demo['company_id']), code='E903', name='Unrelated Manager')
    return {'hr': api(['hr']), 'ram': api(['manager'], demo['ram']), 'other': api(['manager'], str(other.id)),
            'sita': api(['employee'], demo['sita'])}


def test_roster_rows_must_use_shifts_of_the_same_company(team, demo):
    c3 = Company.objects.create(code='C3', name_en='C3')
    foreign = Shift.objects.create(company=c3, code='Z', name_en='Z', start_time='09:00', end_time='17:00')
    r = team['hr']('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': str(foreign.id)}]})
    assert r.status_code == 404 and r.json() == {'error': f'shift {foreign.id} not found'}
    assert not Roster.objects.filter(employee_id=demo['sita']).exists()


def test_a_manager_rosters_only_their_reports_and_sees_only_them(team, demo):
    cell = {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']}]}
    assert team['other']('put', '/roster', cell).status_code == 403
    assert team['ram']('put', '/roster', cell).status_code == 200
    week = {'from': '2026-11-01', 'to': '2026-11-07'}
    view = team['other']('get', '/roster', **week).json()
    assert demo['sita'] not in [e['id'] for e in view['employees']] and view['roster'] == []
    own = team['ram']('get', '/roster', **week).json()
    assert demo['sita'] in [e['id'] for e in own['employees']]
    assert set(own['employees'][0]) == {'id', 'emp_code', 'name_en', 'name_ne', 'weekly_off_days', 'default_shift_id'}
    assert own['roster'] == [{'employee_id': demo['sita'], 'local_date': '2026-11-02', 'shift_id': demo['shift_id']}]


def test_copy_week_skips_payroll_locked_target_days(team, demo):
    team['hr']('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']}]})
    _lock(demo['sita'], '2026-11-09')
    assert team['hr']('post', '/roster/copy-week', {'weekStart': '2026-11-08'}).json() == {'copied': 0, 'skippedLocked': 1}
    assert not Roster.objects.filter(employee_id=demo['sita'], local_date='2026-11-09').exists()


def test_duplicate_company_wide_holidays_are_refused_and_managers_cannot_add_holidays(team, demo):
    hr = team['hr']
    body = {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'}
    assert hr('post', '/holidays', body).status_code == 201
    dup = hr('post', '/holidays', body)
    assert dup.status_code == 409 and dup.json() == {'error': 'this holiday already exists for that date'}
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json()['inserted'] == 42
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json()['inserted'] == 0
    same_day = Holiday.objects.filter(company_id=demo['company_id'], local_date='2026-10-17')
    assert same_day.count() == 1 and same_day.get().bs_date == '2083-06-31'
    r = team['ram']('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'x'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr or attendance_admin'}


# ---------------------------------------------------------------- endpoints.test.ts

def test_holidays_filter_by_range_and_hr_edits_them(team, demo):
    hr, sita = team['hr'], team['sita']
    hid = hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'}).json()['id']
    listed = sita('get', '/holidays', **{'from': '2026-12-01', 'to': '2026-12-31'})
    assert listed.status_code == 200
    row = next(h for h in listed.json()['holidays'] if h['id'] == hid)
    assert row['name_en'] == 'Company anniversary' and row['optional'] is False and row['gender_scope'] == 'all'
    assert set(row) == {'id', 'branch_id', 'local_date', 'bs_date', 'name_en', 'name_ne', 'gender_scope', 'optional'}
    assert sita('get', '/holidays', **{'from': '2027-01-01', 'to': '2027-01-02'}).json()['holidays'] == []

    assert sita('patch', f'/holidays/{hid}', {'nameEn': 'x'}).status_code == 403
    RecomputeQueue.objects.all().delete()
    p = hr('patch', f'/holidays/{hid}', {'nameEn': 'Founders day', 'optional': True, 'localDate': '2026-12-30'})
    assert p.status_code == 200 and p.json() == {'ok': True}
    h = Holiday.objects.get(pk=hid)
    assert (h.name_en, h.optional, h.local_date.isoformat(), h.gender_scope) == ('Founders day', True, '2026-12-30', 'all')
    assert RecomputeQueue.objects.filter(local_date__in=['2026-12-30', '2026-12-31']).exists()

    assert sita('delete', f'/holidays/{hid}').status_code == 403
    assert hr('delete', f'/holidays/{hid}').status_code == 200
    assert hr('delete', f'/holidays/{hid}').json() == {'error': 'holiday not found'}
    assert hr('patch', f'/holidays/{hid}', {'nameEn': 'gone'}).status_code == 404


def test_patch_location_updates_a_geofence(team, demo):
    hr, sita = team['hr'], team['sita']
    lid = hr('post', '/locations', {'name': 'HQ', 'lat': 27.7, 'lng': 85.3}).json()['id']
    assert hr('patch', f'/locations/{lid}', {'radiusM': 300, 'name': 'Kathmandu HQ'}).status_code == 200
    loc = Location.objects.get(pk=lid)
    assert (loc.name, loc.radius_m) == ('Kathmandu HQ', 300)
    assert hr('patch', f'/locations/{lid}', {'radiusM': 5}).status_code == 400
    assert sita('patch', f'/locations/{lid}', {'radiusM': 300}).status_code == 403
    r = hr('patch', f'/locations/{UNKNOWN}', {'radiusM': 300})
    assert r.status_code == 404 and r.json() == {'error': 'location not found'}


def test_patch_shift_changes_rules_for_hr_only(team, demo):
    hr, sita = team['hr'], team['sita']
    p = hr('patch', f"/{demo['shift_id']}", {'graceInMin': 10, 'nameEn': 'General 10-18 (10 min grace)'})
    assert p.status_code == 200 and p.json()['ok'] is True and isinstance(p.json()['recomputeQueued'], int)
    s = Shift.objects.get(pk=demo['shift_id'])
    assert (s.grace_in_min, s.name_en) == (10, 'General 10-18 (10 min grace)')
    assert sita('patch', f"/{demo['shift_id']}", {'graceInMin': 0}).status_code == 403
    assert hr('patch', f'/{UNKNOWN}', {'graceInMin': 0}).status_code == 404
    bad = hr('patch', '/not-a-uuid', {'graceInMin': 0})
    assert bad.status_code == 400 and bad.json()['issues'][0]['message'] == 'Invalid UUID'


def test_delete_roster_day_and_publish_notifies_rostered_people(team, demo, login_as):
    hr, sita, ram = team['hr'], team['sita'], team['ram']
    up = hr('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']}]})
    assert up.status_code == 200
    week = {'from': '2026-11-01', 'to': '2026-11-07'}
    pub = hr('post', '/roster/publish', week)
    assert pub.status_code == 200 and pub.json() == {'notified': 1}
    assert NotificationOutbox.objects.filter(employee_id=demo['sita'], template='roster.published').exists()
    r = sita('post', '/roster/publish', week)
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr or attendance_admin or manager'}
    no_employee = _caller(Client(), bearer(login_as(['manager']).token))
    assert no_employee('post', '/roster/publish', week).json() == {'notified': 0}
    assert ram('post', '/roster/publish', week).json() == {'notified': 1}

    cell = {'employeeId': demo['sita'], 'localDate': '2026-11-02'}
    assert sita('delete', '/roster', cell).status_code == 403
    d = ram('delete', '/roster', cell)
    assert d.status_code == 200 and d.json() == {'deleted': 1}
    assert hr('delete', '/roster', cell).json() == {'deleted': 0}


# ---------------------------------------------------------------- security-authz, fixes-payroll-auth, fixes-attendance

def test_an_employee_reads_only_their_own_calendar_and_huge_ranges_are_refused(team, demo):
    q = {'from': '2026-10-01', 'to': '2026-10-07'}
    assert team['sita']('get', '/calendar', **q, employeeId=demo['sita']).status_code == 200
    assert team['sita']('get', '/calendar', **q, employeeId=demo['ram']).status_code == 403
    assert team['hr']('get', '/calendar', **q, employeeId=demo['ram']).status_code == 200
    r = team['sita']('get', '/calendar', **{'from': '0001-01-01', 'to': '9999-12-31'})
    assert r.status_code == 400 and r.json() == {'error': 'range must be 1 to 400 days'}
    missing = team['sita']('get', '/calendar', to='2026-10-07')
    assert missing.status_code == 400 and missing.json()['error'] == 'validation'


def test_new_shifts_default_to_a_six_hour_late_out_window(api, demo):
    r = api(['hr'])('post', '', {'code': 'LATE6', 'nameEn': 'Ten to six', 'start': '10:00', 'end': '18:00'})
    assert r.status_code == 201 and Shift.objects.get(pk=r.json()['id']).late_out_window_min == 360


@pytest.fixture
def other_company(demo):
    """Another company with a branch, an employee and a shift (security-authz.test.ts)."""
    c = Company.objects.create(code='OTHER', name_en='Other Co')
    branch = Branch.objects.create(company=c, code='OB', name_en='Other branch')
    emp = Employee.objects.create(company=c, emp_code='O001', name_en='Other person')
    shift = Shift.objects.create(company=c, code='OS', name_en='Other shift', start_time='09:00', end_time='17:00')
    hol = Holiday.objects.create(company=c, local_date='2026-12-31', name_en='Other day')
    loc = Location.objects.create(company=c, name='Other yard', lat='27.6', lng='85.3', radius_m=150)
    return {'company': c, 'branch': str(branch.id), 'employee': str(emp.id), 'shift': str(shift.id), 'holiday': str(hol.id),
            'location': str(loc.id)}


def test_another_companys_records_are_not_found(api, demo, other_company):
    hr, sita = api(['hr']), api(['employee'], demo['sita'])
    o = other_company
    assert hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Anniversary', 'branchId': o['branch']}).json() == {'error': 'branch not found'}
    assert hr('post', '/locations', {'name': 'HQ', 'lat': 27.7, 'lng': 85.3, 'branchId': o['branch']}).status_code == 404
    assert sita('get', '/calendar', **{'from': '2026-10-01', 'to': '2026-10-07'}, employeeId=o['employee']).status_code == 404
    for method, path, body in [
        ('get', f"/{o['shift']}", None), ('patch', f"/{o['shift']}", {'graceInMin': 1}), ('delete', f"/{o['shift']}", None),
        ('patch', f"/holidays/{o['holiday']}", {'nameEn': 'x'}), ('delete', f"/holidays/{o['holiday']}", None),
        ('patch', f"/locations/{o['location']}", {'name': 'x'}), ('delete', f"/locations/{o['location']}", None),
        ('post', '/assign', {'employeeId': o['employee'], 'shiftId': demo['shift_id'], 'from': '2026-11-01'}),
        ('post', '/assign', {'employeeId': demo['sita'], 'shiftId': o['shift'], 'from': '2026-11-01'}),
        ('put', '/roster', {'rows': [{'employeeId': o['employee'], 'localDate': '2026-11-02', 'shiftId': None}]}),
    ]:
        assert hr(method, path, body).status_code == 404, (method, path)
    # Nothing of the other company changed or leaked into the lists.
    assert Shift.objects.filter(pk=o['shift'], grace_in_min=0).exists() and Holiday.objects.filter(pk=o['holiday']).exists()
    assert Location.objects.filter(pk=o['location'], name='Other yard').exists()
    assert o['holiday'] not in [h['id'] for h in hr('get', '/holidays').json()['holidays']]
    assert o['location'] not in [x['id'] for x in hr('get', '/locations').json()['locations']]
    assert o['shift'] not in [s['id'] for s in hr('get', '').json()['shifts']]
    assert o['employee'] not in [e['id'] for e in hr('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'}).json()['employees']]


# ---------------------------------------------------------------- the React app's cookie session

def test_the_react_app_cookie_session_reaches_every_route(demo, login_as):
    login = login_as(['hr'])
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    call = _caller(c, {})
    assert call('get', '').status_code == 200
    sid = call('post', '', {'code': 'CK', 'nameEn': 'Cookie', 'start': '09:00', 'end': '17:00'}).json()['id']
    assert call('patch', f'/{sid}', {'nameEn': 'Cookie 2'}).json() == {'ok': True, 'recomputeQueued': 0}
    assert call('get', f'/{sid}').json()['shift']['name_en'] == 'Cookie 2'
    assert call('post', '/assign', {'employeeId': demo['sita'], 'shiftId': sid, 'from': '2027-01-01'}).status_code == 201
    assert call('get', '/holidays').status_code == 200
    assert call('get', '/locations').json() == {'locations': []}
    assert call('get', '/calendar', **{'from': '2026-11-01', 'to': '2026-11-07'}).status_code == 200
    assert call('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': None}]}).json() == {'upserted': 1}
    assert call('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'}).status_code == 200
    assert call('delete', '/roster', {'employeeId': demo['sita'], 'localDate': '2026-11-02'}).json() == {'deleted': 1}
    assert call('post', '/roster/copy-week', {'weekStart': '2026-11-08'}).json() == {'copied': 0, 'skippedLocked': 0}
    assert call('post', '/roster/publish', {'from': '2026-11-01', 'to': '2026-11-07'}).json() == {'notified': 0}
    assert Client().get('/api/v1/shifts/holidays').status_code == 401
