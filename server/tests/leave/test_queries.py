"""Query counts of the leave lists: the number of queries does not grow with the number of rows (no N+1)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import LeaveLedger, LeaveRequest
from tests.core.fixtures import PW
from tests.web.factories import sign_in

from .helpers import one


def _requests(w, n: int, start: date) -> None:
    """n pending one-day requests for Sita (and as many approved for Ram), straight into the table."""
    rows = []
    for i in range(n):
        d = start + timedelta(days=i * 3)
        for emp, status in ((w.demo['sita'], LeaveRequest.Status.PENDING), (w.demo['ram'], LeaveRequest.Status.APPROVED)):
            rows.append(LeaveRequest(employee_id=emp, leave_type_id=w.t('HOME'), from_date=d, to_date=d,
                                     days_requested=Decimal(1), status=status, bridge_days=Decimal(0)))
    LeaveRequest.objects.bulk_create(rows)
    LeaveLedger.objects.bulk_create([LeaveLedger(employee_id=w.demo['sita'], leave_type_id=w.t('HOME'),
                                                 effective_date=start + timedelta(days=i), delta_days=Decimal(1),
                                                 reason=LeaveLedger.Reason.ADJUSTMENT, ref_type='hr_adjustment')
                                     for i in range(n)])


def _count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        r = fn()
    assert r.status_code == 200, r.content[:300]
    return len(ctx.captured_queries)


@pytest.fixture
def sita_page(world):
    c = Client()
    sign_in(c, one('SELECT email FROM app_user WHERE employee_id = %s', [world.demo['sita']])['email'], PW)
    return c


def _flat(w, fn):
    """Queries with 1 row and with 8 rows are the same."""
    _requests(w, 1, date(2026, 11, 2))
    _count(fn)                        # first read: one-off work (the annual allocation, session refresh)
    few = _count(fn)
    _requests(w, 7, date(2026, 12, 1))
    many = _count(fn)
    assert many == few, f'{few} queries for 1 row, {many} for 8'
    return many


def test_time_off_page_has_no_n_plus_one(world, sita_page):
    n = _flat(world, lambda: sita_page.get('/me/leave'))
    assert n <= 40


def test_request_list_api_has_no_n_plus_one(world):
    n = _flat(world, lambda: world.hr.get('/api/v1/leave/requests'))
    assert n <= 15


def test_team_calendar_has_no_n_plus_one(world):
    n = _flat(world, lambda: world.hr.get('/api/v1/leave/team-calendar', {'from': '2026-11-01', 'to': '2027-01-31'}))
    assert n <= 15


def test_ledger_api_has_no_n_plus_one(world):
    n = _flat(world, lambda: world.hr.get('/api/v1/leave/ledger', {'employeeId': world.demo['sita']}))
    assert n <= 15


def test_balances_cost_one_query_for_all_pending_days(world):
    """The balances read the pending days of every type at once, not a query per type."""
    _requests(world, 3, date(2026, 11, 2))
    world.sita.get('/api/v1/leave/balances')        # the annual allocation is posted on the first read
    with CaptureQueriesContext(connection) as ctx:
        r = world.sita.get('/api/v1/leave/balances')
    assert r.status_code == 200
    pending_queries = [q for q in ctx.captured_queries if 'FROM "leave_request"' in q['sql'] and "'pending'" in q['sql']
                       and '"bridge_type_id"' in q['sql']]
    assert len(pending_queries) == 1, [q['sql'] for q in pending_queries]
    home = next(b for b in r.json()['balances'] if b['code'] == 'HOME')
    assert home['pending'] == 3
