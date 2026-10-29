"""The request lists answer in a fixed number of queries however many requests there are (no query per row), and a
punch entered for a date before 1986 keeps the engine's fixed +05:45 Nepal offset."""
from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.core import auth
from hajiri.core.clock import npt_today
from hajiri.db.models import Company, PunchEvent
from hajiri.regularisation import services
from tests.core.fixtures import bearer
from tests.web.factories import make_employee


def _file(user, employee_id, n: int):
    for i in range(n):
        d = (npt_today() - timedelta(days=i + 1)).isoformat()
        services.create_request(user, {'employeeId': employee_id, 'localDate': d, 'kind': 'on_duty', 'reason': f'site {i}'})


def _count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        r = fn()
    assert r.status_code == 200
    return len(ctx.captured_queries)


def test_the_lists_do_not_query_per_row(demo, reg):
    hr = reg['hr']
    page = Client()
    page.cookies[auth.SESSION_COOKIE] = hr.token
    api = lambda: Client().get('/api/v1/regularisation', **bearer(hr.token))  # noqa: E731
    html = lambda: page.get('/api/v1/regularisation', {'scope': 'all'})  # noqa: E731  (the Requests page)
    _file(hr.user, demo['sita'], 2)
    few = (_count(api), _count(html))
    _file(hr.user, demo['ram'], 8)
    assert len(api().json()['requests']) == 10
    assert (_count(api), _count(html)) == few


def test_an_hr_punch_before_1986_uses_the_fixed_nepal_offset(demo, reg):
    worker = make_employee(Company.objects.get(pk=demo['company_id']), name='Old Record')
    out = services.manual_punch(reg['hr'].user, {'employeeId': str(worker.id), 'localDate': '1980-03-01', 'time': '00:10',
                                                 'direction': 'in', 'reason': 'archive'})
    p = PunchEvent.objects.get(id=out['punchId'])
    assert p.local_date.isoformat() == '1980-03-01'
    assert p.punched_at.isoformat().startswith('1980-02-29T18:25')
