"""
No N+1 on the slice's list pages: the number of queries a page makes must not grow with the number of rows on it.
Each page is fetched once with a few rows and again after adding more; the query count must stay the same.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.core.clock import npt_today
from hajiri.lifecycle import api as lc
from tests.web.factories import make_employee, make_user, sign_in

from .conftest import build_world

PAGES = [
    '/people', '/people/org-chart', '/employees', '/employees/structure', '/employees/requests',
    '/employees/onboarding?tab=joiners', '/employees/onboarding?tab=probation', '/employees/onboarding?tab=assets',
    '/employees/exits?tab=exits',
]


def _plus(n: int) -> str:
    return (npt_today() + timedelta(days=n)).isoformat()


def _add_rows(w, n: int, start: int) -> None:
    """n more people under Ram, each with an onboarding case, an exit, an asset held and a pending change request."""
    from hajiri.people import service as ppl
    for i in range(start, start + n):
        e = make_employee(w.company, code=f'Q{i:03d}', name=f'Query Person {i}', branch=w.branch_id, manager=w.ram_id)
        u = make_user(w.company, roles=['employee'], employee=e, email=f'q{i}@people.test')
        lc.start_onboarding(w.hr, {'employeeId': str(e.id), 'anchorDate': _plus(3)})
        lc.create_exit(w.hr, {'employeeId': str(e.id), 'kind': 'resignation', 'lastWorkingDay': _plus(20)})
        a = lc.create_asset(w.hr, {'tag': f'QA-{i}', 'name': f'Laptop {i}'})
        lc.assign_asset(w.hr, a, {'employeeId': str(e.id)})
        ppl.request_change(u, {'section': 'contact', 'value': {'personalPhone': f'+977 98000{i:05d}'}, 'reason': 'moved'})


def _counts(c: Client) -> dict[str, int]:
    out = {}
    for page in PAGES:
        with CaptureQueriesContext(connection) as q:
            r = c.get(page)
        assert r.status_code == 200, (page, r.status_code)
        out[page] = len(q)
    return out


@pytest.mark.django_db
def test_list_pages_do_not_query_per_row(monkeypatch):
    # The Approvals badge in the nav (hajiri.workflow, another slice) resolves approvers once per pending instance;
    # stub it so this measures the people, employees and lifecycle pages themselves.
    monkeypatch.setattr('hajiri.workflow.service.inbox_count', lambda user: 0)
    w = build_world(join_days_ago=200)
    c = Client()
    sign_in(c, w.hr)
    _add_rows(w, 2, 0)
    before = _counts(c)
    _add_rows(w, 6, 2)
    after = _counts(c)
    grew = {p: (before[p], after[p]) for p in PAGES if after[p] > before[p]}
    assert not grew, f'queries grow with rows (before, after): {grew}'
