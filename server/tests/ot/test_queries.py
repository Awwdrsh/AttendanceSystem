"""The overtime lists cost a fixed number of queries however many rows they show (no query per row)."""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import Employee, OtRequest

from .helpers import browser, days_ago, slip


def _count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        r = fn()
    assert r.status_code == 200, r.content[:300]
    return len(ctx.captured_queries)


def _more_people(world, n: int) -> list[str]:
    return [str(Employee.objects.create(company_id=world.company, emp_code=f'Q{i:03}', name_en=f'Query Person {i}').id)
            for i in range(n)]


def test_overtime_page_and_slip_api_do_not_query_per_row(world):
    c = browser(world.hr.token)
    slip(world.demo['ram'], days_ago(2), 60)
    page_one = _count(lambda: c.get('/attendance/overtime'))
    api_one = _count(lambda: world.hr.get('/api/v1/ot/slips'))
    for i, e in enumerate(_more_people(world, 12)):
        slip(e, days_ago(2 + i % 5), 30 + i)
    assert _count(lambda: c.get('/attendance/overtime')) <= page_one
    assert _count(lambda: world.hr.get('/api/v1/ot/slips')) <= api_one
    assert len(world.hr.get('/api/v1/ot/slips').json()['slips']) == 13


def test_request_lists_and_register_do_not_query_per_row(world):
    c = browser(world.hr.token)
    OtRequest.objects.create(employee_id=world.demo['ram'], local_date=days_ago(1), requested_min=60)
    slip(world.demo['ram'], days_ago(1), 60)
    rng = {'from': days_ago(30), 'to': days_ago(0)}
    frag_one = _count(lambda: c.get('/_/ot/requests'))
    api_one = _count(lambda: world.hr.get('/api/v1/ot/requests'))
    reg_one = _count(lambda: world.hr.get('/api/v1/ot/register', rng))
    for i, e in enumerate(_more_people(world, 12)):
        OtRequest.objects.create(employee_id=e, local_date=days_ago(1 + i % 5), requested_min=30 + i)
        slip(e, days_ago(1 + i % 5), 30 + i)
    assert _count(lambda: c.get('/_/ot/requests')) <= frag_one
    assert _count(lambda: world.hr.get('/api/v1/ot/requests')) <= api_one
    assert _count(lambda: world.hr.get('/api/v1/ot/register', rng)) <= reg_one
    assert len(world.hr.get('/api/v1/ot/requests').json()['requests']) == 13
