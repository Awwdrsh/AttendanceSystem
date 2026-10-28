"""
Regressions found in review of the ORM port: re-enrolment after a closed enrolment, non-HR accounts without an
employee, and list pages whose query count must not grow with the number of rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.core.errors import NotFoundError
from hajiri.db.models import LrnEnrollment, PerfOneOnOne, PerfReview
from hajiri.learning import services as lrn
from hajiri.performance import services as perf
from tests.web.factories import make_user, sign_in

from .world import plus_days

pytestmark = pytest.mark.django_db


def _course(w, code='EXCEL', **kw):
    return lrn.create_course(w.hr, {'code': code, 'title': f'Course {code}', 'delivery': 'classroom', 'cost': '100', **kw})


# ---------------------------------------------------------------- enrolments: only open ones are unique

def test_assign_again_after_completion_creates_a_new_enrolment(w):
    cid = _course(w)
    assert lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]}) == {'added': 1, 'skipped': 0}
    first = LrnEnrollment.objects.get(course_id=cid, employee_id=w.HARI)
    lrn.record_attendance(w.ram, str(first.id), {'attended': True})
    # A refresher: the completed enrolment does not block a new one (the TS insert only conflicted with open ones).
    assert lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]}) == {'added': 1, 'skipped': 0}
    assert lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]}) == {'added': 0, 'skipped': 1}
    assert LrnEnrollment.objects.filter(course_id=cid, employee_id=w.HARI).count() == 2


def test_assign_after_two_closed_enrolments_does_not_crash(w):
    cid = _course(w)
    for _ in range(2):
        lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]})
        e = LrnEnrollment.objects.get(course_id=cid, employee_id=w.HARI, status=LrnEnrollment.Status.ENROLLED)
        lrn.cancel(w.ram, str(e.id))
    assert lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]}) == {'added': 1, 'skipped': 0}


def test_review_nominations_skip_only_open_enrolments(w):
    cid = _course(w)
    lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.HARI]})
    assert lrn.create_review_nominations(str(w.co.id), w.HARI, None, [cid], w.hr.id) == 0
    e = LrnEnrollment.objects.get(course_id=cid, employee_id=w.HARI)
    lrn.cancel(w.ram, str(e.id))
    assert lrn.create_review_nominations(str(w.co.id), w.HARI, None, [cid], w.hr.id) == 1
    assert LrnEnrollment.objects.get(course_id=cid, employee_id=w.HARI, status=LrnEnrollment.Status.ENROLLED).source == 'review'


# ---------------------------------------------------------------- accounts without an employee

def test_non_hr_account_without_employee_sees_no_cycles(w):
    cycle = perf.create_cycle(w.hr, {'name': 'FY 82/83', 'periodStart': plus_days(-30), 'periodEnd': plus_days(30)})
    perf.launch_cycle(w.hr, cycle)
    # Ram heads the company, so his own review has no manager: filtering on a missing employee must not match it.
    assert PerfReview.objects.filter(cycle_id=cycle, manager_employee_id__isnull=True).exists()
    nobody = make_user(w.co, roles=['manager'])
    assert perf.list_cycles(nobody) == []
    with pytest.raises(NotFoundError):
        perf.cycle_detail(nobody, cycle)


# ---------------------------------------------------------------- the launch reads every participant's goals at once

def test_launch_picks_up_each_participants_goals(w):
    g1 = perf.create_goal(w.sita, {'title': 'Sita goal', 'weight': 40})
    g2 = perf.create_goal(w.hari, {'title': 'Hari goal', 'weight': 0})
    cycle = perf.create_cycle(w.hr, {'name': 'FY', 'periodStart': plus_days(-30), 'periodEnd': plus_days(30)})
    perf.launch_cycle(w.hr, cycle)
    items = {str(r.employee_id): list(r.perf_review_items.filter(item_type='goal').values_list('ref_id', 'weight'))
             for r in PerfReview.objects.filter(cycle_id=cycle)}
    assert [(str(a), float(b)) for a, b in items[w.SITA]] == [(g1, 40.0)]
    assert [(str(a), float(b)) for a, b in items[w.HARI]] == [(g2, 1.0)]
    assert items[w.GITA] == []


# ---------------------------------------------------------------- 1-on-1 marked done twice

def test_marking_a_recurring_meeting_done_twice_books_one_next_meeting(w):
    at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    mid = perf.create_meeting(w.ram, {'withEmployeeId': w.SITA, 'scheduledAt': at, 'cadence': 'weekly'})
    assert perf.update_meeting(w.ram, mid, {'status': 'done'})
    assert perf.update_meeting(w.sita, mid, {'status': 'done'}) is None
    assert PerfOneOnOne.objects.filter(manager_employee_id=w.RAM, employee_id=w.SITA).count() == 2


# ---------------------------------------------------------------- query counts of the list pages

def _client(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def _queries(c: Client, url: str) -> int:
    with CaptureQueriesContext(connection) as ctx:
        r = c.get(url)
    assert r.status_code == 200, url
    return len(ctx.captured_queries)


def _populate(w, n: int, tag: str) -> None:
    """n more goals (with key results and check-ins), feedback, 1-on-1s, courses and enrolments."""
    for i in range(n):
        gid = perf.create_goal(w.sita, {'title': f'{tag} goal {i}', 'weight': 1,
                                        'keyResults': [{'title': 'a', 'targetValue': 10}, {'title': 'b', 'targetValue': 5}]})
        perf.checkin(w.sita, gid, {'comment': 'on track'})
        perf.create_goal(w.hari, {'title': f'{tag} hari {i}', 'weight': 1})
        perf.give_feedback(w.hari, {'toEmployeeId': w.SITA, 'body': f'{tag} thanks {i}'})
        at = (datetime.now(timezone.utc) + timedelta(days=i + 1)).isoformat().replace('+00:00', 'Z')
        perf.create_meeting(w.ram, {'withEmployeeId': w.SITA, 'scheduledAt': at, 'agenda': ['x', 'y']})
        cid = _course(w, f'{tag}{i}')
        lrn.assign(w.ram, {'courseId': cid, 'employeeIds': [w.SITA, w.HARI]})
        perf.create_cycle(w.hr, {'name': f'{tag} cycle {i}', 'periodStart': plus_days(-30), 'periodEnd': plus_days(30)})


PAGES = [
    ('sita', '/me/growth'),
    ('sita', '/me/growth?view=company'),
    ('sita', '/me/growth/feedback'),
    ('sita', '/me/growth/one-on-ones'),
    ('sita', '/me/growth/reviews'),
    ('sita', '/me/growth/learning'),
    ('sita', '/me/growth/learning?view=catalogue'),
    ('ram', '/performance'),
    ('hr', '/performance'),
    ('hr', '/performance/reviews'),
    ('hr', '/performance/learning'),
    ('hr', '/performance/learning?view=enrollments'),
    ('ram', '/performance/learning?view=enrollments'),
]


def test_list_pages_do_not_query_per_row(w):
    _populate(w, 2, 'A')
    before = {(who, url): _queries(_client(getattr(w, who)), url) for who, url in PAGES}
    _populate(w, 5, 'B')
    after = {(who, url): _queries(_client(getattr(w, who)), url) for who, url in PAGES}
    grew = {k: (before[k], after[k]) for k in before if after[k] > before[k]}
    assert not grew, f'query count grows with the rows: {grew}'
    assert max(after.values()) <= 40, after


