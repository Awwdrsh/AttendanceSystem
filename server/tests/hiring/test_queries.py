"""
The hiring hub's JSON lists and the public careers endpoints run a fixed number of queries however many rows they show: counts,
names and the next interview come from joins and subqueries, not one query per row.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.hiring import services as hiring

from .world import future

H = '/api/v1/hiring'
PAGES = [
    ('jobs', H + '/jobs', None),
    ('requests', H + '/requisitions', None),
    ('job', H + '/jobs/{job}', None),
    ('board', H + '/jobs/{job}/applications', None),
    ('interviews', H + '/interviews?scope=all', None),
    ('candidate drawer', H + '/applications/{app}', None),
]


def _grow(hr, n: int, panel: list[str]) -> tuple[str, str]:
    """n more jobs, each with a hiring request; the first job gets n more candidates with an interview each."""
    first = app = None
    for k in range(n):
        hiring.create_requisition(hr, {'title': f'Role {k}', 'justification': 'The team has grown this year.'})
        job = hiring.create_job(hr, {'title': f'Role {k}', 'description': 'A job description long enough to publish.',
                                     'category': f'Cat {k}'})
        hiring.publish_job(hr, job)
        first = first or job
    for k in range(n):
        app = hiring.add_candidate(hr, first, {'name': f'Candidate {k}', 'email': f'c{k}@x.test', 'source': 'referral'})
        hiring.schedule_interview(hr, app, {'date': future(2), 'time': f'1{k % 8}:00', 'mode': 'phone', 'panel': panel,
                                            'notifyCandidate': False})
        hiring.add_note(hr, app, {'body': f'Note {k}'})
    return first, app


def _count(client, url, hx) -> int:
    with CaptureQueriesContext(connection) as q:
        r = client.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return len(q)


@pytest.mark.parametrize('page', PAGES, ids=[p[0] for p in PAGES])
def test_hub_lists_do_not_query_per_row(world, page):
    _, url, hx = page
    _, hr = world.session(world.admin_b)
    panel = [hr.id, world.dual.id, world.ghr.id]
    job, app = _grow(hr, 2, panel)
    c = world.signed_in(world.admin_b)
    target = url.format(job=job, app=app)
    small = _count(c, target, hx)
    _, app_more = _grow(hr, 6, panel)
    # The candidate drawer of the first job's latest candidate, after more candidates and interviews were added.
    big = _count(c, target if '{app}' not in url else url.format(job=job, app=app_more), hx)
    assert big <= small, f'{target}: {small} queries with few rows, {big} with more'


def test_careers_page_does_not_query_per_job(world):
    from hajiri.careers import services as careers
    careers.apply_limiter.reset()
    _, hr = world.session(world.admin_b)
    _grow(hr, 2, [hr.id])
    small = _count(Client(), '/api/v1/public/careers/companies/BETA/jobs', None)
    _grow(hr, 6, [hr.id])
    big = _count(Client(), '/api/v1/public/careers/companies/BETA/jobs', None)
    assert big <= small
