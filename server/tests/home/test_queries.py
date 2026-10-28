"""
The list pages of the home slice run a fixed number of queries however many rows they show (no query per row), and
the two-step reset reaches only the people its admin manages.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import CompanyMembership, GroupMember, NotificationOutbox
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import htmx, make_employee, make_user

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


def test_users_page_queries_do_not_grow_with_the_people_listed(world):
    c = world.signed_in(world.admin_b)
    _add_people(world, 2, 0)
    few = _count(c, '/settings/users')
    _add_people(world, 6, 10)
    many = _count(c, '/settings/users')
    html = c.get('/settings/users').content.decode()
    assert 'Beta Person 15' in html and 'Alpha Person 15' in html and 'g15@alpha.test' in html
    assert many == few
    # The table fragment too.
    assert _count(c, '/settings/users', **htmx('users')) <= few


def test_notifications_page_queries_do_not_grow_with_the_notices(world):
    c = world.signed_in(world.admin_b)

    def add(n):
        for i in range(n):
            NotificationOutbox.objects.create(company=world.beta, user_id=world.admin_b.id, channel='in_app',
                                              template='t', title=f'Notice {i}', body='body')
    add(2)
    few = _count(c, '/me/notifications')
    add(10)
    assert _count(c, '/me/notifications') == few


def test_org_tab_queries_do_not_grow_with_branches(world):
    from hajiri.db.models import Branch
    c = world.signed_in(world.admin_b)

    def add(n, start):
        for i in range(start, start + n):
            Branch.objects.create(company=world.beta, code=f'B{i}', name_en=f'Branch {i}')
    add(2, 0)
    few = _count(c, '/settings?tab=org', **htmx('settings-panel'))
    add(8, 10)
    assert _count(c, '/settings?tab=org', **htmx('settings-panel')) == few


def test_a_company_admin_does_not_reset_a_group_role_holder(world):
    # Beta's admin manages Beta's accounts and the members given access to Beta, not the people who work in every
    # company of the group through a group role (the group admin resets those).
    c = world.signed_in(world.admin_b)
    assert c.get(f'/_/home/users/{world.gexec.id}/reset', **htmx('hj-modal-body')).status_code == 404
    assert c.post(f'/_/home/users/{world.admin_a.id}/reset', {'reason': 'Lost phone'}, **htmx()).status_code == 404
    # Beta's own account with a group role is still Beta's to reset, and a plain member too.
    assert c.get(f'/_/home/users/{world.ghr.id}/reset', **htmx('hj-modal-body')).status_code == 200
    assert c.get(f'/_/home/users/{world.dual.id}/reset', **htmx('hj-modal-body')).status_code == 200
    # The group admin, working in Beta, reaches group-role holders.
    g = world.signed_in(world.admin_a, world.beta)
    assert g.get(f'/_/home/users/{world.gexec.id}/reset', **htmx('hj-modal-body')).status_code == 200


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
    few = _count(c, '/settings/setup')
    add(8, 10)
    assert _count(c, '/settings/setup') == few


def test_reset_button_is_hidden_for_a_member_holding_a_group_role(world):
    from django.utils import timezone

    from hajiri.db.models import AppUser
    CompanyMembership.objects.create(user_id=world.gexec.id, company=world.beta, roles=['employee'])
    AppUser.objects.filter(pk__in=[world.gexec.id, world.dual.id]).update(totp_enabled_at=timezone.now())
    c = world.signed_in(world.admin_b)
    html = c.get('/settings/users').content.decode()
    assert f'/_/home/users/{world.dual.id}/reset' in html
    assert f'/_/home/users/{world.gexec.id}/reset' not in html
    assert c.post(f'/_/home/users/{world.gexec.id}/reset', {'reason': 'Lost phone'}, **htmx()).status_code == 404
    assert AppUser.objects.get(pk=world.gexec.id).totp_enabled_at is not None
    # The group admin working in Beta sees and uses it.
    g = world.signed_in(world.admin_a, world.beta)
    assert f'/_/home/users/{world.gexec.id}/reset' in g.get('/settings/users').content.decode()
