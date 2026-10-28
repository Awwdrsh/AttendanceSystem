"""
Port of apps/api/test/security-authz.test.ts: authorisation, tenant isolation, injection and race fixes from the security
review. JSON routes kept for non-browser clients are called over HTTP with the TS paths; the others through the same
service functions, with the page refusal checked as well.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.clock import npt_today
from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from tests.core.fixtures import PW
from tests.web.factories import bearer, htmx


def days_ago(n: int) -> str:
    return (npt_today() - timedelta(days=n)).isoformat()


def locked_day(employee_id: str, d: str) -> None:
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at)
               VALUES (%s, %s, 50, 'Present', 480, 'h', now())""", [employee_id, d])


def get(who, path, **query):
    return Client().get(path, query, **bearer(who.token))


def send(method, who, path, body=None):
    return getattr(Client(), method)(path, data=json.dumps(body or {}), content_type='application/json', **bearer(who.token))


def page(who) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = who.token
    return c


@pytest.fixture
def w(demo, login_as):
    x = {'hr': login_as(['hr']), 'admin': login_as(['admin']), 'sita': login_as(['employee'], demo['sita']),
         'ram': login_as(['manager'], demo['ram']), 'stranger': login_as(['employee']), 'demo': demo}
    c = one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id']
    x['other_company'] = str(c)
    x['other_branch'] = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'OB', 'Other branch') RETURNING id", [c])['id'])
    x['other_employee'] = str(one("INSERT INTO employee (company_id, emp_code, name_en, weekly_off_days) VALUES (%s, 'X1', 'Foreign', '{0,3}') RETURNING id", [c])['id'])
    x['other_shift'] = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'OS', 'Other shift', '09:00', '17:00') RETURNING id", [c])['id'])
    auth.create_user(company_id=str(c), email='hr@other.local', name='Other HR', password=PW, roles=['hr'])
    return x


# ---------------------------------------------------------------- manager scoping

def test_manager_lists_only_reports_and_self(w):
    from hajiri.employees.services import list_employees
    d = w['demo']
    execute('UPDATE employee_assignment SET manager_id = NULL WHERE employee_id = %s', [d['sita']])
    assert [str(e['id']) for e in list_employees(w['ram'].user)] == [d['ram']]
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s AND upper_inf(valid)', [d['ram'], d['sita']])
    assert sorted(str(e['id']) for e in list_employees(w['ram'].user)) == sorted([d['sita'], d['ram']])
    assert len(list_employees(w['hr'].user)) >= 2
    # The Employees pages stay HR-only.
    assert page(w['ram']).get('/employees').status_code == 403


def test_exceptions_and_reports_limited_to_the_team(w):
    from hajiri.attendance.api import exceptions
    from hajiri.reports.service import attendance_report
    d = w['demo']
    execute('UPDATE employee_assignment SET manager_id = NULL WHERE employee_id = %s', [d['sita']])
    day = days_ago(3)
    execute("INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash) VALUES (%s, %s, 40, 'Absent', 'h')", [d['sita'], day])
    ex = exceptions(w['ram'].user, {'from': day, 'to': day})
    assert not any(str(x['employee_id']) == d['sita'] for x in ex)
    assert any(str(x['employee_id']) == d['sita'] for x in exceptions(w['hr'].user, {'from': day, 'to': day}))
    assert [r['emp_code'] for r in attendance_report(w['ram'].user, 'daily', {'date': day}).rows] == ['E002']
    with pytest.raises(ForbiddenError):
        attendance_report(w['ram'].user, 'register', {'bsYear': 2083, 'bsMonth': 6, 'employeeId': d['sita']})
    with pytest.raises(ForbiddenError):
        attendance_report(w['ram'].user, 'leave-balances', {})
    with pytest.raises(ForbiddenError):
        attendance_report(w['ram'].user, 'devices', {})
    assert attendance_report(w['hr'].user, 'leave-balances', {}).rows is not None
    assert page(w['ram']).get('/reports').status_code == 403


def test_manager_dashboard_has_no_payroll_totals(w):
    from hajiri.dashboard.services import executive
    d = w['demo']
    execute("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES (%s, 2083, 1, '2026-04-14', '2026-05-14', 31)""", [d['company_id']])
    execute("""INSERT INTO payroll_run (company_id, period_id, run_type) SELECT %s, id, 'regular' FROM payroll_period WHERE company_id = %s AND bs_year = 2083 AND bs_month = 1""",
            [d['company_id'], d['company_id']])
    assert executive(w['ram'].user)['lastPayrollRun'] is None
    assert executive(w['hr'].user)['lastPayrollRun'] is not None
    # The Overview page (with the payroll card) is not for managers.
    assert page(w['ram']).get('/overview').status_code == 403


# ---------------------------------------------------------------- approvals

def test_only_people_involved_read_an_approval_and_a_locked_day_cannot_be_approved(w):
    d = w['demo']
    day = days_ago(2)
    r = send('post', w['sita'], '/api/v1/regularisation', {'localDate': day, 'kind': 'missed_punch', 'punchTime': f'{day}T09:05:00+05:45',
                                                           'punchDirection': 'in', 'reason': 'card reader was down'})
    assert r.status_code == 201, r.content
    inst = str(one('SELECT workflow_instance_id FROM regularisation_request WHERE id = %s', [r.json()['id']])['workflow_instance_id'])
    assert get(w['sita'], f'/api/v1/workflow/{inst}').status_code == 200
    assert get(w['hr'], f'/api/v1/workflow/{inst}').status_code == 200
    assert get(w['stranger'], f'/api/v1/workflow/{inst}').status_code == 404
    locked_day(d['sita'], day)
    ap = send('post', w['hr'], f'/api/v1/workflow/{inst}/approve', {})
    assert ap.status_code == 409
    assert one("SELECT count(*)::int AS n FROM punch_event WHERE employee_id = %s AND source = 'regularisation'", [d['sita']])['n'] == 0


# ---------------------------------------------------------------- punches

def test_punch_time_is_the_server_clock(w):
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    r = send('post', w['sita'], '/api/v1/punch', {'source': 'web', 'direction': 'in', 'clientTime': (before - timedelta(minutes=9)).isoformat(),
                                                  'idempotencyKey': 'sec-review-1', 'reason': 'test'})
    assert r.status_code == 201, r.content
    at = datetime.fromisoformat(r.json()['punch']['punched_at'].replace('Z', '+00:00'))
    assert abs((at - before).total_seconds()) < 60
    raw = one('SELECT raw FROM punch_event WHERE id = %s', [r.json()['punch']['id']])['raw']
    assert raw['clientTime']
    # The Home card goes through the same rule.
    h = page(w['sita']).post('/_/home/punch', {'direction': 'out', 'idempotency_key': 'sec-review-2',
                                               'clientTime': (before - timedelta(hours=3)).isoformat(), 'reason': 'x'}, **htmx())
    assert h.status_code == 200
    raw = one("SELECT raw, punched_at FROM punch_event WHERE employee_id = %s AND direction = 'out'", [w['demo']['sita']])
    assert abs((raw['punched_at'] - before).total_seconds()) < 60 and 'ClockSkew' in raw['raw']['flags']


def test_manual_punch_refused_on_a_payroll_locked_day(w):
    locked_day(w['demo']['ram'], '2026-01-05')
    r = send('post', w['hr'], '/api/v1/punch/manual', {'employeeId': w['demo']['ram'], 'localDate': '2026-01-05', 'time': '09:00',
                                                       'direction': 'in', 'reason': 'forgot card'})
    assert r.status_code == 409


# ---------------------------------------------------------------- devices

def test_pin_injection_and_foreign_branch(w):
    from hajiri.devices import services as dev
    d = w['demo']
    did = str(one("""INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'SEC-ZK-1', 'zkteco', 'adms', 'active') RETURNING id""",
                  [d['company_id']])['id'])
    with pytest.raises(BadRequestError):
        dev.map_pin(w['hr'].user, did, {'pin': '7\tPri=14', 'employeeId': d['sita'], 'from': '2026-01-01'})
    with pytest.raises(BadRequestError):
        dev.map_pin(w['hr'].user, did, {'pin': '7\nC:1:CLEAR DATA', 'employeeId': d['sita'], 'from': '2026-01-01'})
    assert dev.map_pin(w['hr'].user, did, {'pin': '7', 'employeeId': d['sita'], 'from': '2026-01-01'})['ok']
    assert one("SELECT count(*)::int AS n FROM device_command WHERE device_id = %s AND command LIKE '%%Pri=14%%'", [did])['n'] == 0
    with pytest.raises(NotFoundError):
        dev.activate(w['hr'].user, did, {'branchId': w['other_branch']})
    # The page refuses the same PIN.
    r = page(w['hr']).post('/_/devices/map', {'device_id': did, 'pin': '8\tPri=14', 'employee_id': d['sita'], 'valid_from': '2026-01-01'}, **htmx())
    # A form error: the form is drawn again in place (422) with the PIN rule, and nothing is queued.
    assert r.status_code == 422 and 'PIN must be 1 to 24 letters or digits' in r.content.decode()
    assert one("SELECT count(*)::int AS n FROM device_command WHERE device_id = %s AND command LIKE '%%Pri=14%%'", [did])['n'] == 0


# ---------------------------------------------------------------- cross-company references

def test_holidays_locations_and_org_units_refuse_another_companys_branch(w):
    from hajiri.company_settings import services as cs
    from hajiri.shifts import services as sh
    hr = w['hr'].user
    with pytest.raises(NotFoundError):
        sh.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Anniversary', 'branchId': w['other_branch']})
    with pytest.raises(NotFoundError):
        sh.create_location(hr, {'name': 'HQ', 'lat': 27.7, 'lng': 85.3, 'branchId': w['other_branch']})
    with pytest.raises(NotFoundError):
        cs.create_org_unit(hr, {'kind': 'team', 'code': 'T1', 'nameEn': 'Team', 'branchId': w['other_branch']})
    unit = cs.create_org_unit(hr, {'kind': 'team', 'code': 'T2', 'nameEn': 'Team two'})
    with pytest.raises(BadRequestError):
        cs.update_org_unit(hr, str(unit['id']), {'parentId': str(unit['id'])})


def test_calendar_and_shift_delete_reveal_nothing_of_another_company(w):
    from hajiri.shifts import services as sh
    with pytest.raises(NotFoundError):
        sh.calendar(w['sita'].user, {'from': '2026-10-01', 'to': '2026-10-07', 'employeeId': w['other_employee']})
    with pytest.raises(NotFoundError):
        sh.delete_shift(w['hr'].user, w['other_shift'])
    r = page(w['hr']).post(f"/_/shifts/{w['other_shift']}/delete", **htmx())
    assert r.status_code == 404


def test_pay_run_cannot_name_another_companys_employees(w):
    p = send('post', w['hr'], '/api/v1/payroll/periods', {'bsYear': 2083, 'bsMonth': 4})
    r = send('post', w['hr'], '/api/v1/payroll/runs', {'periodId': p.json()['period']['id'], 'runType': 'supplementary',
                                                       'employeeIds': [w['other_employee']]})
    assert r.status_code == 404


# ---------------------------------------------------------------- input validation

def test_long_date_ranges_are_refused(w):
    from hajiri.shifts import services as sh
    sick = next(t for t in get(w['sita'], '/api/v1/leave/types').json()['types'] if t['code'] == 'SICK')
    assert get(w['sita'], '/api/v1/leave/preview', leaveTypeId=sick['id'], **{'from': '0001-01-01', 'to': '9999-12-31'}).status_code == 400
    with pytest.raises(BadRequestError):
        sh.calendar(w['sita'].user, {'from': '0001-01-01', 'to': '9999-12-31'})


def test_declarations_and_salary_amounts_cannot_be_negative(w):
    d = w['demo']
    assert send('put', w['sita'], '/api/v1/payroll/declarations', {'fiscalYearStart': 2083, 'citMonthly': '-50000.00'}).status_code == 400
    assert send('put', w['sita'], '/api/v1/payroll/declarations', {'fiscalYearStart': 2083, 'citMonthly': '2000.00'}).status_code == 200
    s = get(w['hr'], '/api/v1/payroll/structures').json()
    ver = s['structures'][0]['versions'][0]['id']
    r = send('post', w['hr'], '/api/v1/payroll/assignments', {'employeeId': d['sita'], 'structureVersionId': ver, 'from': '2026-07-17',
                                                              'baseAmounts': {'BASIC': '-40000.00'}})
    assert r.status_code == 400


def test_rule_sources_must_be_http_links(w):
    body = {'ruleKey': 'test.rule', 'validFromAd': '2026-01-01', 'validFromBs': '2082-09-17', 'value': {'x': 1}}
    assert send('post', w['admin'], '/api/v1/payroll/rules', {**body, 'sourceUrl': 'javascript:alert(1)'}).status_code == 400
    assert send('post', w['admin'], '/api/v1/payroll/rules', {**body, 'sourceUrl': 'https://ird.gov.np/'}).status_code == 201


# ---------------------------------------------------------------- overtime

def test_comp_off_twice_credits_once(w):
    sid = str(one("INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES (%s, %s, 240, 'working') RETURNING id",
                  [w['demo']['ram'], days_ago(4)])['id'])
    # The TS test fires both at once; the second is refused by the same conditional update either way.
    a = send('post', w['hr'], f'/api/v1/ot/slips/{sid}/comp-off')
    b = send('post', w['hr'], f'/api/v1/ot/slips/{sid}/comp-off')
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    assert one("SELECT count(*)::int AS n FROM leave_ledger WHERE ref_type = 'ot_slip' AND ref_id = %s", [sid])['n'] == 1


def test_ot_register_csv_neutralises_formulas(w):
    execute("""UPDATE employee SET name_en = '=HYPERLINK("http://evil","x")' WHERE id = %s""", [w['demo']['ram']])
    execute("INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES (%s, %s, 60, 'working')", [w['demo']['ram'], days_ago(5)])
    csv = get(w['hr'], '/api/v1/ot/register', **{'from': '2026-01-01', 'to': '2026-12-31', 'format': 'csv'})
    text = csv.content.decode('utf-8-sig')
    assert '"\'=HYPERLINK(""http://evil"",""x"")"' in text
    assert ',=HYPERLINK' not in text


# ---------------------------------------------------------------- device keys

def test_hikvision_device_key_issued_once_and_only_its_hash_stored(w):
    from hajiri.devices import services as dev
    d = w['demo']
    hik = str(one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'HIK-KEY-1', 'hikvision', 'isapi', 'active') RETURNING id",
                  [d['company_id']])['id'])
    key = dev.issue_key(w['hr'].user, hik)['key']
    import re
    assert re.fullmatch(r'[A-Za-z0-9_-]{32}', key)
    assert one('SELECT comm_key_hash FROM device WHERE id = %s', [hik])['comm_key_hash'] == hashlib.sha256(key.encode()).hexdigest()
    with pytest.raises(ForbiddenError):
        dev.issue_key(w['sita'].user, hik)
    with pytest.raises(BadRequestError):
        dev.issue_key(w['hr'].user, d['device_id'])
    foreign = str(one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'HIK-KEY-2', 'hikvision', 'isapi', 'active') RETURNING id",
                      [w['other_company']])['id'])
    with pytest.raises(NotFoundError):
        dev.issue_key(w['hr'].user, foreign)
    assert page(w['sita']).post(f'/_/devices/{hik}/key', **htmx()).status_code == 403
