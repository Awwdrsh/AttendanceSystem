"""The /api/v1/dashboard JSON endpoints (dashboard/routes.ts) as the React Home and Overview pages call them: the
HTTP-level assertions of dashboard.test.ts, roles-flows.test.ts and security-authz.test.ts, plus response shapes and
company isolation."""
import re

import pytest

from tests.devices.jsonapi import Api
from tests.support.sql import execute

OTHER = '00000000-0000-7000-8000-00000000cccc'


@pytest.fixture
def hr(login_as):
    return Api(login_as(['hr']).token)


@pytest.fixture
def emp(login_as, demo):
    return Api(login_as(['employee'], demo['sita']).token)


def test_executive_dashboard_has_at_most_6_tiles_a_trend_and_a_needs_attention_list(demo, hr, emp):
    execute("UPDATE device SET last_seen_at = now() - interval '10 minutes' WHERE id = %s", [demo['device_id']])
    r = hr.get('/api/v1/dashboard/executive')
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {'date', 'tiles', 'trend', 'needsAttention', 'otMinutesThisMonth', 'lastPayrollRun'}
    assert len(body['tiles']) <= 6
    assert next(t for t in body['tiles'] if t['key'] == 'headcount')['value'] == 2
    assert isinstance(body['trend'], list)
    offline = [n for n in body['needsAttention'] if n['kind'] == 'device_offline']
    assert offline and offline[0]['label'].endswith(' is offline') and offline[0]['since'].endswith('Z')
    assert body['lastPayrollRun'] is None
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', body['date'])
    r = emp.get('/api/v1/dashboard/executive')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr or manager or executive'}


def test_employee_home_shows_today_month_summary_balances_and_pending_requests(demo, hr, emp):
    r = emp.get('/api/v1/dashboard/me')
    assert r.status_code == 200
    body = r.json()
    assert body['today'] is None and body['unreadNotifications'] == 0 and body['pendingRequests'] == []
    assert 'HOME' in [b['code'] for b in body['leaveBalances']]
    assert set(body['leaveBalances'][0]) == {'code', 'name_en', 'name_ne', 'balance'}
    assert isinstance(body['leaveBalances'][0]['balance'], str)
    assert re.fullmatch(r'\d{4}-\d{2}', body['month']['bs'])
    assert set(body['month']) == {'bs', 'from', 'to', 'byStatus', 'lateMin', 'otMin'}
    assert body['workFromHome'] == {'allowed': False, 'approvedToday': False}
    r = hr.get('/api/v1/dashboard/me')
    assert r.status_code == 400 and r.json() == {'error': 'Your account is not linked to an employee record.'}


def test_an_executive_reads_the_executive_dashboard(login_as):
    assert Api(login_as(['executive']).token).get('/api/v1/dashboard/executive').status_code == 200


def test_the_manager_dashboard_does_not_show_payroll_totals(demo, hr, login_as):
    execute("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days)
               VALUES (%s, 2083, 1, '2026-04-14', '2026-05-14', 31)""", [demo['company_id']])
    execute("""INSERT INTO payroll_run (company_id, period_id, run_type)
               SELECT %s, id, 'regular' FROM payroll_period WHERE company_id = %s AND bs_year = 2083 AND bs_month = 1""",
            [demo['company_id'], demo['company_id']])
    ram_mgr = Api(login_as(['manager'], demo['ram']).token)
    assert ram_mgr.get('/api/v1/dashboard/executive').json()['lastPayrollRun'] is None
    run = hr.get('/api/v1/dashboard/executive').json()['lastPayrollRun']
    assert run is not None
    assert set(run) == {'id', 'status', 'run_type', 'bs_year', 'bs_month', 'headcount', 'net', 'gross'}
    assert (run['bs_year'], run['bs_month'], run['headcount'], run['net']) == (2083, 1, 0, '0')


def test_another_companys_hr_sees_only_their_own_company(demo, login_as):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHERD', 'Other') ON CONFLICT DO NOTHING", [OTHER])
    execute("UPDATE device SET last_seen_at = now() - interval '10 minutes' WHERE id = %s", [demo['device_id']])
    other = Api(login_as(['hr'], company_id=OTHER).token)
    body = other.get('/api/v1/dashboard/executive').json()
    assert next(t for t in body['tiles'] if t['key'] == 'headcount')['value'] == 0
    assert body['needsAttention'] == [] and body['trend'] == []


def test_sign_in_and_methods(demo, emp):
    assert Api(None).get('/api/v1/dashboard/me').status_code == 401
    assert Api(None).get('/api/v1/dashboard/executive').status_code == 401
    assert emp.post('/api/v1/dashboard/me', {}).status_code == 404
