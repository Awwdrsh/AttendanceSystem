"""No N+1 queries: the main lists of the slice run the same number of queries for 2 people as for 12."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.attendance import api as attendance_api
from hajiri.attendance.service import recompute_day
from hajiri.notifications.service import inbox, notify
from hajiri.reports import service
from hajiri.reports.people import PEOPLE_REPORTS, people_report
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import make_employee

pytestmark = pytest.mark.django_db

DAY = '2026-09-21'


def _count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


def _grow(world, n):
    for _ in range(n):
        e = make_employee(world.alpha, manager=world.admin_a.employee_id)
        recompute_day(str(e.id), DAY)
        notify(company_id=str(world.alpha.id), user_id=world.admin_a.id, template='x', title='t', body='b')


def _lists(user):
    return {
        'exceptions': lambda: attendance_api.exceptions(user, {'from': DAY, 'to': DAY}),
        'today': lambda: attendance_api.today(user, {'date': DAY}),
        'inbox': lambda: inbox(user.id, company_id=user.company_id),
        'builder': lambda: service.run_adhoc(user, {'dataset': 'employees',
                                                    'columns': ['emp_code', 'department', 'branch', 'manager', 'age']}),
        **{f'attendance:{name}': (lambda name=name: service.attendance_report(user, name, {'from': DAY, 'to': DAY, 'date': DAY}))
           for name in ('daily', 'ot', 'late-early', 'headcount', 'exceptions-summary', 'leave-balances', 'devices')},
        **{f'people:{name}': (lambda name=name: people_report(user, name, {'from': '2026-01-01', 'to': '2026-12-31'}))
           for name in PEOPLE_REPORTS},
    }


def test_list_queries_do_not_grow_with_the_rows(world):
    _, user = world.session(world.admin_a)
    _grow(world, 2)
    before = {k: _count(fn) for k, fn in _lists(user).items()}
    _grow(world, 10)
    after = {k: _count(fn) for k, fn in _lists(user).items()}
    assert after == before
