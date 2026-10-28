"""
Port of apps/api/test/endpoints.test.ts: one authorised call (status and shape) and one refused call for endpoints the
feature suites do not reach. Where a non-browser client still has the JSON route it is called over HTTP with the TS path;
where the route became a page, the call goes to the same service function and the page is checked for the refusal.
"""
import json

import pytest
from django.test import Client

from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from tests.web.factories import bearer, htmx

NOPE = '00000000-0000-7000-8000-0000000000ff'


@pytest.fixture
def w(demo, login_as):
    """hr, admin, sita (employee) and ram (manager and employee, Sita's manager), as the TS beforeAll."""
    x = {'hr': login_as(['hr']), 'admin': login_as(['admin']), 'sita': login_as(['employee'], demo['sita']),
         'ram': login_as(['manager', 'employee'], demo['ram']), 'demo': demo, 'login_as': login_as}
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return x


def get(who, path, **query):
    return Client().get(path, query, **bearer(who.token))


def send(method, who, path, body=None):
    c = Client()
    fn = getattr(c, method)
    return fn(path, data=json.dumps(body or {}), content_type='application/json', **bearer(who.token))


def page(who) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = who.token
    return c


# ---------------------------------------------------------------- health

def test_health_is_public_and_reports_the_database(db):
    r = Client().get('/health')
    assert r.status_code == 200 and r.json() == {'ok': True, 'db': 'up'}


# ---------------------------------------------------------------- auth and people

def test_users_list_for_hr_only(w):
    from hajiri.core.users import list_users
    users = list_users(w['hr'].user)
    assert len(users) >= 4 and 'password_hash' not in users[0]
    assert {'id', 'email', 'roles', 'status'} <= set(users[0])
    assert page(w['hr']).get('/settings/users').status_code == 200
    assert page(w['sita']).get('/settings/users').status_code == 403
    assert Client().get('/settings/users', HTTP_ACCEPT='application/json').status_code == 401
    with pytest.raises(ForbiddenError):
        list_users(w['sita'].user)


def test_assignment_history_for_self_manager_and_hr(w):
    from hajiri.employees.services import assignments
    d = w['demo']
    a = assignments(w['hr'].user, d['sita'])
    assert a[0]['employee_id'] == d['sita'] and a[0]['valid'] == '[2026-01-01,)'
    assert a[0]['branch_id'] == d['branch_id'] and a[0]['manager_id'] == d['ram']
    assert assignments(w['sita'].user, d['sita']) and assignments(w['ram'].user, d['sita'])
    with pytest.raises(ForbiddenError):
        assignments(w['sita'].user, d['ram'])
    # On the page: HR opens the employee; an employee cannot.
    assert page(w['hr']).get(f"/employees/{d['sita']}").status_code == 200
    assert page(w['sita']).get(f"/employees/{d['sita']}").status_code == 403


def test_raw_punches_for_an_employee_and_range(w):
    from hajiri.attendance.api import punches
    d = w['demo']
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
                                        direction, verify_mode, source, idempotency_key)
               VALUES (%s, 'DEMO-01', 'zkteco', '1', %s, '2026-09-01T04:20:00Z', '2026-09-01 10:05:00', '2026-09-01', 'unknown',
                       'fingerprint', 'zkteco', 'ep-1')""", [d['device_id'], d['sita']])
    p = punches(w['sita'].user, {'employeeId': d['sita'], 'from': '2026-09-01', 'to': '2026-09-02'})
    assert len(p) == 1 and str(p[0]['local_date']) == '2026-09-01' and p[0]['superseded'] is False
    with pytest.raises(ForbiddenError):
        punches(w['sita'].user, {'employeeId': d['ram'], 'from': '2026-09-01', 'to': '2026-09-02'})
    with pytest.raises(BadRequestError):
        punches(w['sita'].user, {'employeeId': d['sita']})


# ---------------------------------------------------------------- devices

def test_devices_with_health_counters(w):
    from hajiri.devices.services import list_devices
    d = w['demo']
    dev = next(x for x in list_devices(w['hr'].user) if str(x['id']) == d['device_id'])
    assert dev['serial_no'] == 'DEMO-01' and dev['offline'] is True
    assert int(dev['mapped_users']) == 2 and int(dev['queued_commands']) == 0
    with pytest.raises(ForbiddenError):
        list_devices(w['sita'].user)
    assert page(w['hr']).get('/settings/devices').status_code == 200
    assert page(w['sita']).get('/settings/devices').status_code == 403


def test_sync_users_queues_one_userinfo_per_mapped_employee(w):
    from hajiri.devices.services import sync_users
    d = w['demo']
    assert sync_users(w['hr'].user, d['device_id']) == {'queued': 2}
    cmds = [r['command'] for r in rows("SELECT command FROM device_command WHERE device_id = %s AND status = 'queued' ORDER BY command",
                                       [d['device_id']])]
    assert cmds[0].startswith('DATA UPDATE USERINFO PIN=1\tName=') and cmds[1].startswith('DATA UPDATE USERINFO PIN=2\tName=')
    assert sync_users(w['hr'].user, NOPE) == {'queued': 0}
    with pytest.raises(ForbiddenError):
        sync_users(w['sita'].user, d['device_id'])
    assert page(w['sita']).post(f"/_/devices/{d['device_id']}/sync-users", **htmx()).status_code == 403


# ---------------------------------------------------------------- shifts, holidays, locations and roster

def test_holidays_filter_patch_and_delete(w):
    from hajiri.shifts import services as sh
    hr, sita = w['hr'].user, w['sita'].user
    hid = sh.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'})['id']
    got = next(h for h in sh.holidays(sita, {'from': '2026-12-01', 'to': '2026-12-31'}) if str(h['id']) == str(hid))
    assert got['name_en'] == 'Company anniversary' and got['optional'] is False and got['gender_scope'] == 'all'
    assert sh.holidays(sita, {'from': '2027-01-01', 'to': '2027-01-02'}) == []
    with pytest.raises(ForbiddenError):
        sh.update_holiday(sita, hid, {'nameEn': 'x'})
    sh.update_holiday(hr, hid, {'nameEn': 'Founders day', 'optional': True, 'localDate': '2026-12-30'})
    row = one('SELECT name_en, optional, local_date::text, gender_scope FROM holiday WHERE id = %s', [hid])
    assert row == {'name_en': 'Founders day', 'optional': True, 'local_date': '2026-12-30', 'gender_scope': 'all'}
    assert one("SELECT count(*)::int AS n FROM recompute_queue WHERE local_date IN ('2026-12-30', '2026-12-31')")['n'] > 0
    with pytest.raises(ForbiddenError):
        sh.delete_holiday(sita, hid)
    sh.delete_holiday(hr, hid)
    with pytest.raises(NotFoundError):
        sh.delete_holiday(hr, hid)
    with pytest.raises(NotFoundError):
        sh.update_holiday(hr, hid, {'nameEn': 'gone'})
    assert page(w['sita']).get('/settings/holidays').status_code == 403


def test_location_geofence_update(w):
    from hajiri.shifts import services as sh
    hr, sita = w['hr'].user, w['sita'].user
    lid = sh.create_location(hr, {'name': 'HQ', 'lat': 27.7, 'lng': 85.3})['id']
    sh.update_location(hr, lid, {'radiusM': 300, 'name': 'Kathmandu HQ'})
    assert one('SELECT name, radius_m FROM location WHERE id = %s', [lid]) == {'name': 'Kathmandu HQ', 'radius_m': 300}
    with pytest.raises(ValidationError):
        sh.update_location(hr, lid, {'radiusM': 5})
    with pytest.raises(ForbiddenError):
        sh.update_location(sita, lid, {'radiusM': 300})
    with pytest.raises(NotFoundError):
        sh.update_location(hr, NOPE, {'radiusM': 300})


def test_shift_rules_patch_for_hr_only(w):
    from hajiri.shifts import services as sh
    d = w['demo']
    sh.update_shift(w['hr'].user, d['shift_id'], {'graceInMin': 10, 'nameEn': 'General 10-18 (10 min grace)'})
    assert one('SELECT grace_in_min, name_en FROM shift WHERE id = %s', [d['shift_id']]) == {'grace_in_min': 10, 'name_en': 'General 10-18 (10 min grace)'}
    with pytest.raises(ForbiddenError):
        sh.update_shift(w['sita'].user, d['shift_id'], {'graceInMin': 0})
    with pytest.raises(NotFoundError):
        sh.update_shift(w['hr'].user, NOPE, {'graceInMin': 0})
    assert page(w['sita']).get('/settings/shifts').status_code == 403


def test_roster_publish_and_clear(w):
    from hajiri.shifts import services as sh
    d = w['demo']
    hr, sita, ram = w['hr'].user, w['sita'].user, w['ram'].user
    sh.save_roster(hr, {'rows': [{'employeeId': d['sita'], 'localDate': '2026-11-02', 'shiftId': d['shift_id']}]})
    assert sh.publish_roster(hr, {'from': '2026-11-01', 'to': '2026-11-07'}) == {'notified': 1}
    with pytest.raises(ForbiddenError):
        sh.publish_roster(sita, {'from': '2026-11-01', 'to': '2026-11-07'})
    other_mgr = w['login_as'](['manager'], None).user
    assert sh.publish_roster(other_mgr, {'from': '2026-11-01', 'to': '2026-11-07'}) == {'notified': 0}
    assert sh.publish_roster(ram, {'from': '2026-11-01', 'to': '2026-11-07'}) == {'notified': 1}
    with pytest.raises(ForbiddenError):
        sh.clear_roster(sita, {'employeeId': d['sita'], 'localDate': '2026-11-02'})
    assert sh.clear_roster(ram, {'employeeId': d['sita'], 'localDate': '2026-11-02'}) == {'deleted': 1}
    assert sh.clear_roster(hr, {'employeeId': d['sita'], 'localDate': '2026-11-02'}) == {'deleted': 0}
    assert page(w['sita']).get('/attendance/rosters').status_code == 403


# ---------------------------------------------------------------- leave and overtime lists (JSON routes)

def test_leave_types_everyone_reads_hr_creates(w):
    lst = get(w['sita'], '/api/v1/leave/types')
    assert lst.status_code == 200 and len(lst.json()['types']) > 0
    t0 = lst.json()['types'][0]
    assert isinstance(t0['id'], str) and isinstance(t0['code'], str) and isinstance(t0['name_en'], str) and isinstance(t0['paid'], bool)
    c = send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'paid': False, 'maxConsecutiveDays': 10})
    assert c.status_code == 201
    mine = next(t for t in get(w['sita'], '/api/v1/leave/types').json()['types'] if t['id'] == c.json()['id'])
    assert (mine['code'], mine['paid'], mine['statutory']) == ('STUDY', False, False)
    assert send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Again'}).status_code == 409
    assert send('post', w['sita'], '/api/v1/leave/types', {'code': 'X', 'nameEn': 'X'}).status_code == 403


def test_leave_requests_scope_own_team_all(w):
    send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'paid': False, 'maxConsecutiveDays': 10})
    t = next(x for x in get(w['sita'], '/api/v1/leave/types').json()['types'] if x['code'] == 'STUDY')
    c = send('post', w['sita'], '/api/v1/leave/requests', {'leaveTypeId': t['id'], 'from': '2026-11-16', 'to': '2026-11-16', 'reason': 'exam'})
    assert c.status_code == 201, c.content
    own = get(w['sita'], '/api/v1/leave/requests')
    assert own.status_code == 200 and len(own.json()['requests']) == 1
    r0 = own.json()['requests'][0]
    assert (r0['employee_id'], r0['type_code'], r0['status']) == (w['demo']['sita'], 'STUDY', 'pending')
    assert c.json()['id'] in [x['id'] for x in get(w['ram'], '/api/v1/leave/requests').json()['requests']]
    assert get(w['hr'], '/api/v1/leave/requests', status='approved').json()['requests'] == []
    assert get(w['sita'], '/api/v1/leave/requests', scope='all').status_code == 403
    assert get(w['sita'], '/api/v1/leave/requests', employeeId=w['demo']['ram']).status_code == 403


def test_ot_requests_list_and_cancel(w):
    d = w['demo']
    execute('UPDATE employee SET ot_eligible = true WHERE id = %s', [d['sita']])
    c = send('post', w['hr'], '/api/v1/ot/requests', {'employeeId': d['sita'], 'localDate': '2026-11-03', 'requestedMin': 60, 'reason': 'release'})
    assert c.status_code == 201, c.content
    own = get(w['sita'], '/api/v1/ot/requests')
    r0 = own.json()['requests'][0]
    assert own.status_code == 200 and (r0['id'], r0['employee_id'], r0['requested_min'], r0['status']) == (c.json()['id'], d['sita'], 60, 'pending')
    assert get(w['sita'], '/api/v1/ot/requests', scope='all').status_code == 403
    assert send('post', w['sita'], f"/api/v1/ot/requests/{c.json()['id']}/cancel").status_code == 200
    assert one('SELECT status FROM ot_request WHERE id = %s', [c.json()['id']])['status'] == 'cancelled'
    assert send('post', w['sita'], f"/api/v1/ot/requests/{c.json()['id']}/cancel").status_code == 409
    assert send('post', w['sita'], f'/api/v1/ot/requests/{NOPE}/cancel').status_code == 404
    other = w['login_as'](['employee'], None)
    c2 = send('post', w['hr'], '/api/v1/ot/requests', {'employeeId': d['sita'], 'localDate': '2026-11-04', 'requestedMin': 30})
    assert send('post', other, f"/api/v1/ot/requests/{c2.json()['id']}/cancel").status_code == 403


# ---------------------------------------------------------------- settings (pages and services)

def test_branch_rename_and_org_units(w):
    from hajiri.company_settings import services as cs
    d = w['demo']
    b = cs.update_branch(w['admin'].user, d['branch_id'], {'nameEn': 'Kathmandu HQ'})
    assert (str(b['id']), b['code'], b['name_en']) == (d['branch_id'], 'HQ', 'Kathmandu HQ')
    with pytest.raises(ForbiddenError):
        cs.update_branch(w['sita'].user, d['branch_id'], {'nameEn': 'x'})
    with pytest.raises(NotFoundError):
        cs.update_branch(w['hr'].user, NOPE, {'nameEn': 'x'})
    r = page(w['hr']).post('/_/home/settings/units', {'kind': 'department', 'code': 'ENG', 'name': 'Engineering'}, **htmx())
    assert r.status_code == 200
    units = cs.list_org_units(w['hr'].user)
    assert [(u['kind'], u['code'], u['name_en']) for u in units] == [('department', 'ENG', 'Engineering')]
    assert page(w['sita']).get('/settings?tab=org').status_code == 403
    assert page(w['sita']).post('/_/home/settings/units', {'kind': 'team', 'code': 'T', 'name': 'T'}, **htmx()).status_code == 403


# ---------------------------------------------------------------- payroll setup and run lists (JSON routes)

def P(path):
    return f'/api/v1/payroll{path}'


def test_structures_and_versions(w):
    s = send('post', w['hr'], P('/structures'), {'code': 'EP', 'nameEn': 'Endpoint structure', 'components': [{'code': 'BASIC'}]})
    assert s.status_code == 201, s.content
    assert set(s.json()) == {'id', 'versionId'}
    v = send('post', w['hr'], P(f"/structures/{s.json()['id']}/versions"), {'components': [{'code': 'BASIC'}, {'code': 'GRADE'}]})
    assert v.status_code == 201 and v.json()['version'] == 2 and isinstance(v.json()['id'], str)
    lst = get(w['hr'], P('/structures')).json()['structures']
    assert [x['version'] for x in next(x for x in lst if x['id'] == s.json()['id'])['versions']] == [1, 2]
    assert send('post', w['hr'], P('/structures'), {'code': 'EP', 'nameEn': 'dup', 'components': [{'code': 'BASIC'}]}).status_code == 409
    assert send('post', w['hr'], P(f'/structures/{NOPE}/versions'), {'components': [{'code': 'BASIC'}]}).status_code == 404
    assert send('post', w['sita'], P('/structures'), {'code': 'NO', 'nameEn': 'no', 'components': [{'code': 'BASIC'}]}).status_code == 403


def test_loans_periods_runs_and_inputs(w):
    d = w['demo']
    loan = send('post', w['hr'], P('/loans'), {'employeeId': d['sita'], 'principal': '10000', 'emi': '2500', 'startBsYear': 2083, 'startBsMonth': 7})
    assert loan.status_code == 201, loan.content
    loans = get(w['hr'], P('/loans'), employeeId=d['sita'])
    l0 = loans.json()['loans'][0]
    assert loans.status_code == 200 and (l0['id'], l0['emp_code'], l0['installments']) == (loan.json()['id'], 'E001', 4)
    assert str(l0['repaid']) in ('0', '0.00')
    period = send('post', w['hr'], P('/periods'), {'bsYear': 2083, 'bsMonth': 7})
    assert period.status_code == 201
    pid = period.json()['period']['id']
    p0 = get(w['hr'], P('/periods')).json()['periods'][0]
    assert (p0['id'], p0['bs_year'], p0['bs_month']) == (pid, 2083, 7)
    run = send('post', w['hr'], P('/runs'), {'periodId': pid})
    assert run.status_code == 201
    r0 = get(w['hr'], P('/runs')).json()['runs'][0]
    assert (r0['id'], r0['bs_year'], r0['bs_month'], r0['run_type'], r0['status']) == (run.json()['id'], 2083, 7, 'regular', 'draft')
    comp = next(c for c in get(w['hr'], P('/components')).json()['components'] if c['type'] == 'earning' and c['active'])
    rid = run.json()['id']
    ins = send('post', w['hr'], P(f'/runs/{rid}/inputs'), {'inputs': [{'employeeId': d['sita'], 'componentCode': comp['code'], 'amount': '1500', 'note': 'one-off'}]})
    assert ins.status_code == 201
    got = get(w['hr'], P(f'/runs/{rid}/inputs')).json()['inputs']
    assert len(got) == 1 and (got[0]['employee_id'], got[0]['emp_code'], got[0]['component_code'], got[0]['note']) == (d['sita'], 'E001', comp['code'], 'one-off')
    assert float(got[0]['amount']) == 1500
    for path in ['/loans', '/periods', '/runs', f'/runs/{rid}/inputs']:
        assert get(w['sita'], P(path)).status_code == 403, path
    assert get(w['hr'], P(f'/runs/{NOPE}/inputs')).status_code == 404
    assert page(w['sita']).get('/payroll').status_code == 403


# ---------------------------------------------------------------- biometrics

def test_unmapped_templates_and_stats_for_hr_only(w):
    from hajiri.devices import biometrics as bio
    d = w['demo']
    execute("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (NULL, '99', 'fingerprint', 'zk_fp_v10', '\\x00'::bytea, 1, 'h', %s)""", [d['device_id']])
    u = bio.unmapped_templates(w['hr'].user)
    assert len(u) == 1 and (u[0]['modality'], u[0]['device_user_pin'], u[0]['device_sn']) == ('fingerprint', '99', 'DEMO-01')
    assert 'ciphertext' not in u[0]
    st = bio.stats(w['hr'].user)
    st = st['byModality'] if isinstance(st, dict) else st
    assert [{k: int(v) if k != 'modality' else v for k, v in x.items()} for x in st] == [{'modality': 'fingerprint', 'active': 1, 'shredded': 0, 'unmapped': 1}]
    with pytest.raises(ForbiddenError):
        bio.unmapped_templates(w['sita'].user)
    with pytest.raises(ForbiddenError):
        bio.stats(w['sita'].user)
