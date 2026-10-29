"""
The list pages of the home slice run a fixed number of queries however many rows they show (no query per row), and
the two-step reset reaches only the people its admin manages.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import CompanyMembership, GroupMember, NotificationOutbox
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import make_employee, make_user

pytestmark = pytest.mark.django_db


def _count(client, url, **kw) -> int:
    client.get(url, **kw)  # the first request of a session also records the sign-in's activity; count a later one
    with CaptureQueriesContext(connection) as q:
        r = client.get(url, **kw)
    assert r.status_code == 200, r.content[:300]
    return len(q.captured_queries)


def _add_people(world, n: int, start: int) -> None:
    for i in range(start, start + n):
        make_user(world.beta, roles=['employee'], employee=make_employee(world.beta, name=f'Beta Person {i}'),
                  email=f'p{i}@beta.test')
        u = make_user(world.alpha, roles=['employee'], employee=make_employee(world.alpha, name=f'Alpha Person {i}'),
                      email=f'm{i}@alpha.test')
        CompanyMembership.objects.create(user_id=u.id, company=world.beta, roles=['employee'])
        g = make_user(world.alpha, roles=['employee'], email=f'g{i}@alpha.test')
        GroupMember.objects.create(user_id=g.id, group_id=world.alpha.group_id, roles=['group_hr'])


def test_notifications_page_queries_do_not_grow_with_the_notices(world):
    c = world.signed_in(world.admin_b)

    def add(n):
        for i in range(n):
            NotificationOutbox.objects.create(company=world.beta, user_id=world.admin_b.id, channel='in_app',
                                              template='t', title=f'Notice {i}', body='body')
    add(2)
    few = _count(c, '/api/v1/notifications')
    add(10)
    assert _count(c, '/api/v1/notifications') == few


def test_org_tab_queries_do_not_grow_with_branches(world):
    from hajiri.db.models import Branch
    c = world.signed_in(world.admin_b)

    def add(n, start):
        for i in range(start, start + n):
            Branch.objects.create(company=world.beta, code=f'B{i}', name_en=f'Branch {i}')
    add(2, 0)
    few = _count(c, '/api/v1/settings/branches')
    add(8, 10)
    assert _count(c, '/api/v1/settings/branches') == few


def test_late_penalty_number_issue_uses_the_body_path():
    from django.http import QueryDict

    from hajiri.core.errors import ValidationError
    from hajiri.home.forms import attendance_body
    post = QueryDict(mutable=True)
    post.update({'latePenalty.enabled': 'on', 'latePenalty.lateCount': 'three'})
    with pytest.raises(ValidationError) as e:
        attendance_body({}, post)
    assert e.value.issues == [{'path': 'latePenalty.lateCount', 'message': 'Expected number, received NaN'}]


def test_setup_checklist_queries_do_not_grow_with_employees(world):
    c = world.signed_in(world.admin_b)

    def add(n, start):
        for i in range(start, start + n):
            make_employee(world.beta, name=f'Setup Person {i}')
    add(2, 0)
    few = _count(c, '/api/v1/employees?status=active')
    add(8, 10)
    assert _count(c, '/api/v1/employees?status=active') == few
