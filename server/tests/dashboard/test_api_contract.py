"""/api/v1/dashboard/executive and /me as the React Overview (DashboardPage.jsx) and Home (HomePage.jsx) pages call
them through client.js (the session cookie, no CSRF token), compared key by key with dashboard/routes.ts at 65a9079."""
import re
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.attendance.service import recompute_day
from hajiri.core.clock import npt_today
from hajiri.db.models import Company, Employee
from tests.core.punches import insert_punch
from tests.support.sql import execute
from tests.web.factories import make_user, sign_in

ISO_Z = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')


def browser(user):
    c = Client(enforce_csrf_checks=True)
    sign_in(c, user)
    return c


@pytest.fixture
def company(demo):
    return Company.objects.get(pk=demo['company_id'])


def test_overview_page_reads_every_field_of_the_executive_dashboard(demo, company):
    yesterday = (npt_today() - timedelta(days=1)).isoformat()
    insert_punch(demo['sita'], yesterday, '10:00', '1')
    insert_punch(demo['sita'], yesterday, '18:00', '1')
    recompute_day(demo['sita'], yesterday)
    execute("UPDATE device SET last_seen_at = NULL WHERE id = %s", [demo['device_id']])
    c = browser(make_user(company, roles=['hr']))
    r = c.get('/api/v1/dashboard/executive')
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {'date', 'tiles', 'trend', 'needsAttention', 'otMinutesThisMonth', 'lastPayrollRun'}
    assert b['date'] == npt_today().isoformat()
    assert [t['key'] for t in b['tiles']] == ['headcount', 'present', 'late', 'absent', 'pendingApprovals', 'exceptions']
    assert all(set(t) == {'key', 'label', 'value'} and isinstance(t['value'], int) for t in b['tiles'])
    point = next(p for p in b['trend'] if p['date'] == yesterday)
    assert set(point) == {'date', 'presentPct'} and isinstance(point['presentPct'], float)
    off = next(n for n in b['needsAttention'] if n['kind'] == 'device_offline')
    assert set(off) == {'kind', 'label', 'since'} and off['since'] is None   # never seen: null, as the TS row
    assert isinstance(b['otMinutesThisMonth'], int)


def test_home_page_reads_every_field_of_the_employee_home(demo, company):
    today = npt_today().isoformat()
    user = make_user(company, roles=['employee'], employee=Employee.objects.get(pk=demo['sita']))
    execute("""INSERT INTO regularisation_request (employee_id, local_date, kind, reason, status, created_by)
               VALUES (%s, %s, 'wfh', 'delivery', 'pending', %s)""", [demo['sita'], today, user.id])
    c = browser(user)
    r = c.get('/api/v1/dashboard/me')
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {'date', 'today', 'month', 'leaveBalances', 'unreadNotifications', 'pendingRequests', 'workFromHome'}
    assert b['date'] == today
    m = b['month']
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', m['from']) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', m['to'])
    assert isinstance(m['byStatus'], dict) and isinstance(m['lateMin'], int) and isinstance(m['otMin'], int)
    assert re.fullmatch(r'-?\d+\.\d{2}', b['leaveBalances'][0]['balance'])           # numeric(8,2)::text
    req = b['pendingRequests'][0]
    assert set(req) == {'kind', 'id', 'created_at', 'detail'}
    assert req['kind'] == 'regularisation' and req['detail'] == f'{today} wfh' and ISO_Z.match(req['created_at'])
    assert isinstance(b['unreadNotifications'], int)
    assert set(b['workFromHome']) == {'allowed', 'approvedToday'}
