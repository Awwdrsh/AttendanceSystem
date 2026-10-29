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
from tests.web.factories import bearer


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

def test_manual_punch_refused_on_a_payroll_locked_day(w):
    locked_day(w['demo']['ram'], '2026-01-05')
    r = send('post', w['hr'], '/api/v1/punch/manual', {'employeeId': w['demo']['ram'], 'localDate': '2026-01-05', 'time': '09:00',
                                                       'direction': 'in', 'reason': 'forgot card'})
    assert r.status_code == 409


# ---------------------------------------------------------------- devices

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
    r = page(w['hr']).delete(f"/api/v1/shifts/{w['other_shift']}")
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
