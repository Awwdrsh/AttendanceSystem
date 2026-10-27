"""Port of apps/api/test/dashboard.test.ts, the dashboard cases of security-authz.test.ts and roles-flows.test.ts,
and the Overview page (DashboardPage.tsx)."""
import re
from datetime import datetime, timezone

import pytest
from django.test import Client

from hajiri.core.db import execute
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.dashboard import services as dash
from tests.core.punches import insert_punch
from tests.web.factories import htmx, sign_in


@pytest.fixture
def hr(login_as):
    return login_as(['hr'])


@pytest.fixture
def emp(login_as, demo):
    return login_as(['employee'], demo['sita'])


def client_for(login):
    c = Client()
    sign_in(c, login.email)
    return c


def test_executive_dashboard_has_at_most_6_tiles_a_trend_and_a_needs_attention_list(demo, hr, emp):
    execute("UPDATE device SET last_seen_at = now() - interval '10 minutes' WHERE id = %s", [demo['device_id']])
    r = dash.executive(hr.user)
    assert len(r['tiles']) <= 6
    assert next(t for t in r['tiles'] if t['key'] == 'headcount')['value'] == 2
    assert isinstance(r['trend'], list)
    assert any(n['kind'] == 'device_offline' for n in r['needsAttention'])
    assert r['lastPayrollRun'] is None
    with pytest.raises(ForbiddenError):
        dash.executive(emp.user)


def test_employee_home_shows_today_month_summary_balances_and_pending_requests(demo, hr, emp):
    r = dash.me(emp.user)
    assert r['today'] is None and r['unreadNotifications'] == 0 and r['pendingRequests'] == []
    assert 'HOME' in [b['code'] for b in r['leaveBalances']]
    assert re.fullmatch(r'\d{4}-\d{2}', r['month']['bs'])
    with pytest.raises(BadRequestError, match='not linked to an employee'):
        dash.me(hr.user)


def test_the_manager_dashboard_does_not_show_payroll_totals(demo, hr, login_as):
    ram_mgr = login_as(['manager'], demo['ram'])
    execute("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days)
               VALUES (%s, 2083, 1, '2026-04-14', '2026-05-14', 31)""", [demo['company_id']])
    execute("""INSERT INTO payroll_run (company_id, period_id, run_type) SELECT %s, id, 'regular' FROM payroll_period
                WHERE company_id = %s AND bs_year = 2083 AND bs_month = 1""", [demo['company_id'], demo['company_id']])
    assert dash.executive(ram_mgr.user)['lastPayrollRun'] is None
    run = dash.executive(hr.user)['lastPayrollRun']
    assert run is not None and run['bs_month'] == 1 and run['headcount'] == 0 and run['net'] == '0'


def test_executive_reads_the_dashboard(demo, login_as):
    ex = login_as(['executive'])
    assert dash.executive(ex.user)['date']
    assert client_for(ex).get('/overview').status_code == 200


def test_tiles_count_todays_attendance_and_attention_lists_unmapped_punches(demo, hr):
    today = dash.npt_today().isoformat()
    from hajiri.attendance.service import recompute_day
    insert_punch(demo['sita'], today, '10:30')
    recompute_day(demo['sita'], today)
    insert_punch(None, today, '09:00', '42')
    r = dash.executive(hr.user, today)
    tiles = {t['key']: t['value'] for t in r['tiles']}
    assert tiles['present'] + tiles['absent'] >= 1
    assert tiles['late'] == 1
    assert {'kind': 'unmapped_punches', 'label': '1 punches from unknown PINs', 'count': 1} in r['needsAttention']


def test_arrivals_ribbon_geometry_follows_arrivals_ribbon_tsx():
    at = lambda hhmm: datetime.fromisoformat(f'2026-10-26T{hhmm}:00+05:45').astimezone(timezone.utc)  # noqa: E731
    team = [{'name_en': 'Sita', 'first_in': at('09:58'), 'late_min': 0}, {'name_en': 'Ram', 'first_in': at('10:20'), 'late_min': 5},
            {'name_en': 'Hari', 'first_in': None, 'late_min': None}]
    r = dash.arrivals_ribbon(team, '10:00', 15)
    # from = floor((min(540, 598, 620) - 10) / 30) * 30 = 510; to = ceil((max(660, ...) + 10) / 30) * 30 = 690
    assert r['hours'][0] == {'left': 0.0, 'label': '08:30'} and r['hours'][-1]['label'] == '11:30'
    assert r['shift_left'] == round(90 / 180 * 100, 3) and r['grace_width'] == round(15 / 180 * 100, 3)
    assert [m['late'] for m in r['marks']] == [False, True]
    assert r['marks'][1]['title'] == 'Ram, 10:20 (late)'
    assert r['height'] == 3 * 11 + 8


def test_overview_page_for_hr_and_access(demo, hr, emp, login_as):
    execute("UPDATE device SET last_seen_at = now() - interval '10 minutes' WHERE id = %s", [demo['device_id']])
    r = client_for(hr).get('/overview')
    assert r.status_code == 200
    body = r.content.decode()
    for text in ('In today', 'of 2 active employees', 'Arrivals today', 'Attendance rate', 'Needs attention',
                 'Front door (simulator) is offline', 'Check device', 'Latest clock-ins', 'No pay run yet', 'Overtime detected this month'):
        assert text in body, text
    assert client_for(emp).get('/overview', **htmx()).status_code == 403
    # The page follows the App.tsx guard (insights: HR and executives); managers use the API data on other pages.
    assert client_for(login_as(['manager'])).get('/overview', **htmx()).status_code == 403
