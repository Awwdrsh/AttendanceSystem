"""Who can work in which company: own accounts, memberships, group roles, switching, leavers and sign-in."""
from datetime import timedelta

import pytest

from hajiri.core import auth, companies
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.users import company_roles, company_users, user_ids_with_role
from hajiri.db.models import AppUser, AuditLog, CompanyMembership, Employee, GroupMember, Session
from tests.web.factories import PW, htmx, make_employee, make_user


def ids(*cos):
    return {str(c.id) for c in cos}


def test_single_company_people_are_unchanged(world):
    _, solo = world.session(world.solo)
    assert solo.company_id == str(world.alpha.id) and solo.roles == ('hr',) and not solo.multi_company
    assert solo.group_id == str(world.alpha.group_id) and solo.group_roles == ()
    assert [c.code for c in solo.companies] == ['ALPHA'] and solo.active_company.home
    _, outsider = world.session(world.admin_c)
    assert outsider.company_ids == (str(world.gamma.id),) and outsider.group_id is None


def test_companies_of_each_kind_of_person(world):
    _, dual = world.session(world.dual)
    assert set(dual.company_ids) == ids(world.alpha, world.beta) and dual.company_ids[0] == str(world.alpha.id)
    assert dual.company(world.beta.id).roles == ('manager', 'employee') and not dual.company(world.beta.id).via_group
    _, ghr = world.session(world.ghr)
    assert ghr.company_id == str(world.beta.id)              # home first
    assert ghr.roles == ('employee', 'hr') and ghr.group_roles == ('group_hr',)
    alpha = ghr.company(world.alpha.id)
    assert alpha.via_group and alpha.roles == ('hr',) and alpha.employee_id is None
    _, gexec = world.session(world.gexec)
    assert gexec.company(world.beta.id).roles == ('executive',)
    assert world.gamma.id not in {c.id for c in gexec.companies}
    _, admin_a = world.session(world.admin_a)
    assert admin_a.group_roles == ('group_admin',) and admin_a.company(world.beta.id).roles == ('admin',)


def test_switching_changes_roles_and_employee_and_is_audited(world):
    token, dual = world.session(world.dual)
    assert dual.roles == ('hr',) and dual.employee_id == world.dual.employee_id
    there = auth.switch_company(dual, token, str(world.beta.id))
    assert there.company_id == str(world.beta.id) and there.roles == ('manager', 'employee')
    assert there.employee_id == str(world.dual_beta_employee.id)
    assert not auth.has_role(there, 'hr')
    assert auth.resolve_session(token).company_id == str(world.beta.id)
    entry = AuditLog.objects.filter(action='company_switch', actor_id=world.dual.id).latest('id')
    assert entry.before == {'companyId': str(world.alpha.id)} and entry.after == {'companyId': str(world.beta.id)}
    # Only this session moved; a new sign-in starts at home again.
    _, fresh = world.session(world.dual)
    assert fresh.company_id == str(world.alpha.id)


def test_group_role_switch_gives_the_group_role_there(world):
    token, ghr = world.session(world.ghr)
    there = auth.switch_company(ghr, token, str(world.alpha.id))
    assert there.company_id == str(world.alpha.id) and there.roles == ('hr',) and there.employee_id is None
    assert there.group_roles == ('group_hr',)
    assert auth.has_module(there, 'payroll') and not auth.has_role(there, 'admin')


@pytest.mark.parametrize('who', ['solo', 'admin_c', 'dual'])
def test_no_access_no_switch(world, who):
    person = getattr(world, who)
    token, user = world.session(person)
    target = world.gamma if who != 'admin_c' else world.alpha
    with pytest.raises(NotFoundError, match='company not found'):
        auth.switch_company(user, token, str(target.id))
    with pytest.raises(NotFoundError):
        auth.switch_company(user, token, '00000000-0000-0000-0000-000000000000')
    assert auth.resolve_session(token).company_id == user.company_id
    c = world.signed_in(person)
    assert c.post('/_/web/company/switch', {'company_id': str(target.id)}, **htmx()).status_code == 404
    api = c.post('/api/v1/me/companies/switch', {'companyId': str(target.id)}, content_type='application/json')
    assert api.status_code == 404 and api.json()['error'] == 'company not found'


def test_a_stale_active_company_falls_back_home(world):
    token, dual = world.session(world.dual)
    auth.switch_company(dual, token, str(world.beta.id))
    CompanyMembership.objects.filter(user_id=world.dual.id).update(status='disabled')
    back = auth.resolve_session(token)
    assert back.company_id == str(world.alpha.id) and back.roles == ('hr',) and not back.multi_company
    # A session pointed at a company the person never had (a forged row) also lands at home.
    Session.objects.filter(user_id=world.dual.id).update(active_company=world.gamma)
    assert auth.resolve_session(token).company_id == str(world.alpha.id)


def test_leavers_lose_access_per_company(world):
    yesterday = npt_today() - timedelta(days=1)
    # Left Beta: the membership no longer counts, Alpha keeps working.
    Employee.objects.filter(pk=world.dual_beta_employee.id).update(exit_date=yesterday)
    _, dual = world.session(world.dual)
    assert dual.company_ids == (str(world.alpha.id),)
    # Left the home company but still works in Beta: signs in straight into Beta.
    Employee.objects.filter(pk=world.dual_beta_employee.id).update(exit_date=None)
    Employee.objects.filter(pk=world.dual.employee_id).update(exit_date=yesterday)
    token, dual = world.session(world.dual)
    assert dual.company_id == str(world.beta.id) and dual.company_ids == (str(world.beta.id),)
    with pytest.raises(NotFoundError):
        auth.switch_company(dual, token, str(world.alpha.id))
    # Left both: no sign-in, and open sessions end.
    Employee.objects.filter(pk=world.dual_beta_employee.id).update(exit_date=yesterday)
    assert auth.resolve_session(token) is None
    with pytest.raises(auth.UnauthorizedError, match='when you left the company'):
        auth.login(world.dual.email, PW)


def test_a_leaver_loses_group_roles(world):
    Employee.objects.filter(pk=world.ghr.employee_id).update(exit_date=npt_today() - timedelta(days=1))
    with pytest.raises(auth.UnauthorizedError):
        auth.login(world.ghr.email, PW)
    assert world.ghr.id not in user_ids_with_role(str(world.alpha.id), 'hr')


def test_default_company_at_sign_in(world):
    token, dual = world.session(world.dual)
    auth.set_default_company(dual, str(world.beta.id))
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).is_default
    _, fresh = world.session(world.dual)
    assert fresh.company_id == str(world.beta.id) and fresh.company(world.beta.id).is_default
    auth.set_default_company(fresh, str(world.alpha.id))
    assert not CompanyMembership.objects.filter(user_id=world.dual.id, is_default=True).exists()
    assert world.session(world.dual)[1].company_id == str(world.alpha.id)
    with pytest.raises(NotFoundError):
        auth.set_default_company(fresh, str(world.gamma.id))
    _, ghr = world.session(world.ghr)
    with pytest.raises(BadRequestError):
        auth.set_default_company(ghr, str(world.alpha.id))      # only through the group role


def test_people_of_a_company_include_members_and_group_roles(world):
    alpha = str(world.alpha.id)
    assert company_roles(alpha, world.ghr.id) == ['hr']
    assert company_roles(alpha, world.gexec.id) == ['employee', 'executive']
    assert company_roles(alpha, world.admin_b.id) is None
    hr = set(user_ids_with_role(alpha, 'hr'))
    assert {world.ghr.id, world.solo.id, world.dual.id, world.admin_a.id} <= hr and world.admin_b.id not in hr
    listed = {str(u['id']): u for u in company_users(alpha)}
    assert listed[world.ghr.id].get('via_group') and listed[world.ghr.id]['roles'] == ['hr']
    assert {str(i) for i in AppUser.objects.for_company(alpha).values_list('id', flat=True)} >= {world.ghr.id, world.gexec.id}
    assert not AppUser.objects.for_company(alpha).filter(pk=world.admin_c.id).exists()


def test_group_roles_are_managed_by_group_admins(world):
    _, admin_a = world.session(world.admin_a)
    assert companies.is_group_admin(admin_a)
    companies.grant_group_role(admin_a, {'email': world.solo.email, 'roles': ['group_hr', 'group_executive']})
    _, solo = world.session(world.solo)
    assert solo.multi_company and solo.company(world.beta.id).roles == ('hr', 'executive')
    assert {r['email'] for r in companies.list_group_roles(admin_a)} >= {'solo@alpha.test', 'ghr@beta.test'}
    # Outsiders cannot be given group roles; nobody removes their own; others are not group admins.
    with pytest.raises(NotFoundError, match='in your group'):
        companies.grant_group_role(admin_a, {'email': world.admin_c.email, 'roles': ['group_hr']})
    with pytest.raises(BadRequestError):
        companies.grant_group_role(admin_a, {'email': world.solo.email, 'roles': ['admin']})
    with pytest.raises(BadRequestError):
        companies.revoke_group_role(admin_a, admin_a.id)
    with pytest.raises(ForbiddenError):
        companies.grant_group_role(solo, {'email': world.dual.email, 'roles': ['group_admin']})
    _, admin_b = world.session(world.admin_b)
    with pytest.raises(ForbiddenError):
        companies.list_group_roles(admin_b)          # admin of Beta, not of the group
    companies.revoke_group_role(admin_a, world.solo.id)
    assert not world.session(world.solo)[1].multi_company
    assert AuditLog.objects.filter(table_name='group_member', action='delete').exists()


def test_revoking_a_group_role_sends_sessions_home(world):
    token, ghr = world.session(world.ghr)
    auth.switch_company(ghr, token, str(world.alpha.id))
    _, admin_a = world.session(world.admin_a)
    companies.revoke_group_role(admin_a, world.ghr.id)
    back = auth.resolve_session(token)
    assert back.company_id == str(world.beta.id) and back.roles == ('employee',) and back.group_roles == ()


def test_only_group_admins_add_companies_to_an_existing_group(world):
    _, admin_b = world.session(world.admin_b)
    with pytest.raises(ForbiddenError, match='group admin'):
        companies.create_group_company(admin_b, {'code': 'DELTA', 'name_en': 'Delta'})
    _, admin_a = world.session(world.admin_a)
    delta = companies.create_group_company(admin_a, {'code': 'DELTA', 'name_en': 'Delta Dairy'})
    # Group roles reach the new company straight away.
    _, ghr = world.session(world.ghr)
    assert ghr.company(delta['id']).roles == ('hr',)
    # A standalone company's admin starts their own group and becomes its group admin.
    _, admin_c = world.session(world.admin_c)
    companies.create_group_company(admin_c, {'code': 'GAMMA2', 'name_en': 'Gamma Two'})
    assert GroupMember.objects.filter(user_id=world.admin_c.id, roles=['group_admin']).exists()


def test_last_admin_rule_counts_group_admins(world):
    from hajiri.core.users import admin_update_user
    _, admin_b = world.session(world.admin_b)
    # Beta has its home admin plus the group admin (admin_a, also a member admin): demoting admin_b is allowed.
    admin_update_user(admin_b, world.admin_a.id, {'roles': ['hr']})
    _, admin_a = world.session(world.admin_a, world.beta)
    assert 'admin' in admin_a.roles                     # still admin there through the group role
    admin_update_user(admin_a, world.admin_b.id, {'roles': ['employee']})


def test_group_overview_counts_only_visible_companies(world):
    _, gexec = world.session(world.gexec)
    o = companies.group_overview(gexec)
    assert {c['code'] for c in o['companies']} == {'ALPHA', 'BETA'}
    _, dual = world.session(world.dual)
    assert companies.group_company_ids(dual) == [str(world.alpha.id)]     # only an employee/manager in Beta
    assert not companies.can_view_group(dual)
    nobody = make_user(None, roles=['employee'])
    with pytest.raises(ForbiddenError):
        companies.group_overview(nobody)


def test_resolving_a_session_stays_cheap(world, django_assert_max_num_queries):
    token, _ = world.session(world.solo)
    with django_assert_max_num_queries(2):         # the session (with every hint) and last_seen_at
        assert auth.resolve_session(token).company_id == str(world.alpha.id)
    token, dual = world.session(world.dual, world.beta)
    with django_assert_max_num_queries(6):
        assert auth.resolve_session(token).company_id == str(world.beta.id)
