"""The list pages and JSON lists of time tracking run a fixed number of queries, however many rows they show."""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import Timesheet
from hajiri.timesheets import services as ts
from tests.web.factories import htmx, make_employee, sign_in

from .helpers import add_days, make_world

PAGES = [
    ('hr', '/attendance/timesheets?view=approve', 'ts-review'),
    ('hr', '/attendance/timesheets?view=history', 'ts-review'),
    ('hr', '/attendance/timesheets?view=week&week={WEEK}', 'ts-review'),
    ('hr', '/attendance/timesheets?view=report&group_by=project', 'ts-review'),
    ('hr', '/attendance/timesheets?view=report&group_by=employee', 'ts-review'),
    ('hr', '/attendance/projects?view=projects', 'ts-catalogue'),
    ('hr', '/attendance/projects?view=clients', 'ts-catalogue'),
    ('hr', '/attendance/projects?view=activities', 'ts-catalogue'),
    ('sita', '/me/timesheet?week={WEEK}', None),
    ('sita', '/_/timesheets/week?week={WEEK}', None),
    ('hr', '/api/v1/time/timesheets', None),
    ('hr', '/api/v1/time/projects?all=1', None),
    ('sita', '/api/v1/time/projects', None),
    ('hr', '/api/v1/time/activities?all=1', None),
    ('sita', '/api/v1/time/entries?from={WEEK}&to={WEEK}', None),
    ('hr', '/api/v1/time/team-week?weekStart={WEEK}', None),
]


@pytest.fixture
def w(db):
    return make_world()


def _seed(w, n: int, tag: str) -> None:
    """n more clients, projects with members, activities, people with entries and submitted weeks."""
    for i in range(n):
        c = ts.create_client(w.hr, {'name': f'Client {tag}{i}'})['id']
        emp = make_employee(w.company, code=f'Q{tag}{i}', name=f'Person {tag}{i}')
        p = ts.create_project(w.hr, {'code': f'P{tag}{i}', 'name': f'Project {tag}{i}', 'clientId': c, 'billRate': 1000,
                                     'budgetHours': 50, 'allMembers': False,
                                     'memberIds': [str(w.sita_emp.id), str(emp.id)]})
        ts.create_activity(w.hr, {'name': f'Activity {tag}{i}', 'projectId': p})
        ts.add_entry(w.sita, {'date': add_days(w.WEEK, i % 7), 'durationMin': 30, 'projectId': p})
        Timesheet.objects.create(company=w.company, employee=emp, week_start=add_days(w.WEEK, -7 * len(tag)),
                                 status=Timesheet.Status.SUBMITTED, total_min=60)


def _count(client: Client, url: str, target: str | None) -> int:
    kw = htmx(target=target) if target else {}
    with CaptureQueriesContext(connection) as q:
        r = client.get(url, **kw)
    assert r.status_code == 200, (url, r.status_code, r.content[:300])
    return len(q.captured_queries)


def test_list_pages_do_not_query_per_row(w):
    clients = {'hr': Client(), 'sita': Client()}
    sign_in(clients['hr'], w.hr)
    sign_in(clients['sita'], w.sita)
    pages = [(who, url.replace('{WEEK}', w.WEEK), t) for who, url, t in PAGES]
    _seed(w, 2, 'a')
    for who, url, t in pages:                    # warm the per-process caches (features, policy, nav)
        _count(clients[who], url, t)
    before = {url: _count(clients[who], url, t) for who, url, t in pages}
    _seed(w, 6, 'bb')
    for who, url, t in pages:
        after = _count(clients[who], url, t)
        assert after == before[url], f'{url}: {before[url]} queries with few rows, {after} with more'
