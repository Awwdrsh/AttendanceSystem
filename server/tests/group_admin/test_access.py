"""Access of people to the companies of the group: list, give/change, invite, remove; all audited."""
import pytest

from hajiri.core import auth
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.db.models import AppUser, AuditLog, CompanyMembership
from hajiri.group_admin import services
from tests.web.factories import PW, htmx, make_employee, toasts

from .conftest import actor


def _rows(admin, **kw):
    return {(r['email'], r['companyName']): r for r in services.list_access(admin, **kw)}


def test_lists_home_accounts_and_members_of_every_group_company(world, admin):
    rows = _rows(admin)
    assert rows[('solo@alpha.test', 'Alpha Traders')]['source'] == 'home'
    assert rows[('dual@alpha.test', 'Beta Foods')]['source'] == 'membership'
    assert rows[('dual@alpha.test', 'Beta Foods')]['roles'] == ['manager', 'employee']
    assert rows[('dual@alpha.test', 'Beta Foods')]['employeeName'] == 'Dipak at Beta'
    assert ('admin@gamma.test', 'Gamma Outside') not in rows
    beta = _rows(admin, company_id=str(world.beta.id))
    assert {k[1] for k in beta} == {'Beta Foods'}
    with pytest.raises(NotFoundError, match='company not found'):
        services.list_access(admin, str(world.gamma.id))


def test_give_access_with_roles_and_employee_record(world, admin):
    emp = make_employee(world.beta, name='Sita at Beta')
    r = services.grant_access(admin, {'email': 'SOLO@alpha.test', 'companyId': str(world.beta.id), 'roles': ['hr'],
                                      'employeeId': str(emp.id)})
    assert r['source'] == 'membership'
    ms = CompanyMembership.objects.get(user_id=world.solo.id, company=world.beta)
    assert ms.roles == ['hr'] and ms.employee_id == emp.id
    assert AuditLog.objects.filter(action='create', table_name='company_membership', row_id=f'{world.solo.id}:{world.beta.id}').exists()
    # The person can now work in Beta with those roles.
    token = auth.login(world.solo.email, PW).token
    user = auth.switch_company(auth.resolve_session(token), token, str(world.beta.id))
    assert user.roles == ('hr',) and user.employee_id == str(emp.id)
    # Changing it is an update, audited with what it was.
    services.grant_access(admin, {'email': 'solo@alpha.test', 'companyId': str(world.beta.id), 'roles': ['manager', 'employee']})
    ms.refresh_from_db()
    assert ms.roles == ['manager', 'employee'] and ms.employee_id is None
    entry = AuditLog.objects.filter(action='update', table_name='company_membership').latest('id')
    assert entry.before['roles'] == ['hr'] and entry.after['roles'] == ['manager', 'employee']


def test_change_roles_in_the_home_company(world, admin):
    services.grant_access(admin, {'email': 'solo@alpha.test', 'companyId': str(world.alpha.id), 'roles': ['hr', 'payroll_admin']})
    assert AppUser.objects.get(pk=world.solo.id).roles == ['hr', 'payroll_admin']
    assert AuditLog.objects.filter(action='update', table_name='app_user', row_id=world.solo.id).exists()
    with pytest.raises(BadRequestError, match='your own admin role'):
        services.grant_access(admin, {'email': 'admin@alpha.test', 'companyId': str(world.alpha.id), 'roles': ['hr']})


def test_nothing_reaches_outside_the_group(world, admin):
    with pytest.raises(NotFoundError, match='company not found'):
        services.grant_access(admin, {'email': 'solo@alpha.test', 'companyId': str(world.gamma.id), 'roles': ['admin']})
    with pytest.raises(NotFoundError, match='no account with this email in your group'):
        services.grant_access(admin, {'email': 'admin@gamma.test', 'companyId': str(world.beta.id), 'roles': ['employee']})
    gamma_emp = make_employee(world.gamma)
    with pytest.raises(NotFoundError, match='employee not found'):
        services.grant_access(admin, {'email': 'solo@alpha.test', 'companyId': str(world.beta.id), 'roles': ['employee'],
                                      'employeeId': str(gamma_emp.id)})
    with pytest.raises(NotFoundError):
        services.invite_user(admin, {'email': 'new@x.test', 'name': 'New', 'companyId': str(world.gamma.id), 'roles': ['admin'],
                                     'password': 'a-long-enough-password-1'})
    with pytest.raises(NotFoundError):
        services.revoke_access(admin, world.admin_c.id, world.gamma.id)
    assert not CompanyMembership.objects.filter(company=world.gamma).exists()


def test_an_employee_linked_to_someone_else_is_refused(world, admin):
    with pytest.raises(ConflictError, match='already linked'):
        services.grant_access(admin, {'email': 'solo@alpha.test', 'companyId': str(world.beta.id), 'roles': ['employee'],
                                      'employeeId': str(world.dual_beta_employee.id)})


def test_invite_a_new_person(world, admin):
    emp = make_employee(world.beta, name='Naya Manche')
    r = services.invite_user(admin, {'email': 'naya@beta.test', 'name': 'Naya Manche', 'companyId': str(world.beta.id),
                                     'roles': ['employee'], 'employeeId': str(emp.id), 'password': 'temporary-password-123'})
    u = AppUser.objects.get(pk=r['userId'])
    assert str(u.company_id) == str(world.beta.id) and u.employee_id == emp.id and u.must_change_password
    assert AuditLog.objects.filter(action='create', table_name='app_user', row_id=str(u.pk), actor_id=world.admin_a.id).exists()
    with pytest.raises(ConflictError, match='already exists'):
        services.invite_user(admin, {'email': 'Naya@beta.test', 'name': 'Again', 'companyId': str(world.beta.id),
                                     'roles': ['employee'], 'password': 'temporary-password-123'})
    with pytest.raises(BadRequestError):
        services.invite_user(admin, {'email': 'weak@beta.test', 'name': 'Weak', 'companyId': str(world.beta.id),
                                     'roles': ['employee'], 'password': 'short'})


def test_remove_access(world, admin):
    services.revoke_access(admin, world.dual.id, world.beta.id)
    assert not CompanyMembership.objects.filter(user_id=world.dual.id, company=world.beta).exists()
    entry = AuditLog.objects.filter(action='delete', table_name='company_membership').latest('id')
    assert entry.before['roles'] == ['manager', 'employee']
    with pytest.raises(NotFoundError, match='access not found'):
        services.revoke_access(admin, world.dual.id, world.beta.id)
    with pytest.raises(BadRequestError, match='home company'):
        services.revoke_access(admin, world.solo.id, world.alpha.id)
    with pytest.raises(BadRequestError, match='your own access'):
        services.revoke_access(admin, world.admin_a.id, world.beta.id)


def test_only_group_admins(world):
    for person in (world.admin_b, world.ghr, world.solo):
        with pytest.raises(ForbiddenError):
            services.list_access(actor(world, person))
        with pytest.raises(ForbiddenError):
            services.grant_access(actor(world, person), {'email': 'solo@alpha.test', 'companyId': str(world.beta.id),
                                                         'roles': ['admin']})


# ---------------------------------------------------------------- pages

def test_access_pages(world):
    c = world.signed_in(world.admin_a)
    html = c.get('/group/access').content.decode()
    assert 'dual@alpha.test' in html and 'admin@gamma.test' not in html
    assert c.get(f'/group/access?company={world.beta.id}').status_code == 200
    assert c.get(f'/_/group-admin/access?company={world.gamma.id}', **htmx()).status_code == 404
    form = c.get(f'/_/group-admin/access/new?company_id={world.beta.id}', **htmx()).content.decode()
    assert 'Dipak at Beta' not in form            # linked records are not offered
    emp = make_employee(world.beta, name='Free Record')
    options = c.get(f'/_/group-admin/access/employees?company_id={world.beta.id}', **htmx()).content.decode()
    assert 'Free Record' in options
    assert c.get(f'/_/group-admin/access/employees?company_id={world.gamma.id}', **htmx()).status_code == 404
    r = c.post('/_/group-admin/access/new', {'company_id': str(world.beta.id), 'email': 'solo@alpha.test',
                                             'roles': ['hr'], 'employee_id': str(emp.id)}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Access saved.'
    assert CompanyMembership.objects.get(user_id=world.solo.id, company=world.beta).employee_id == emp.id
    # A company outside the group is not found (and not a choice of the form).
    r = c.post('/_/group-admin/access/new', {'company_id': str(world.gamma.id), 'email': 'solo@alpha.test', 'roles': ['hr']}, **htmx())
    assert r.status_code == 404
    assert c.get(f'/_/group-admin/access/new?company_id={world.gamma.id}', **htmx()).status_code == 404
    r = c.post(f'/_/group-admin/access/{world.solo.id}/{world.beta.id}/revoke', **htmx())
    assert r.status_code == 204 and not CompanyMembership.objects.filter(user_id=world.solo.id, company=world.beta).exists()
    r = c.post('/_/group-admin/access/invite', {'company_id': str(world.beta.id), 'name': 'Page Person', 'email': 'page@beta.test',
                                                'roles': ['employee'], 'password': 'temporary-password-123'}, **htmx())
    assert r.status_code == 204 and AppUser.objects.filter(email='page@beta.test', company=world.beta).exists()


def test_access_api(world):
    c = world.signed_in(world.admin_a)
    items = c.get('/api/v1/group/access', {'companyId': str(world.beta.id)}).json()['items']
    assert {i['companyName'] for i in items} == {'Beta Foods'}
    r = c.post('/api/v1/group/access', {'email': 'solo@alpha.test', 'companyId': str(world.beta.id), 'roles': ['executive']},
               content_type='application/json')
    assert r.status_code == 200 and r.json()['roles'] == ['executive']
    assert c.delete(f'/api/v1/group/access/{world.solo.id}/{world.beta.id}').status_code == 204
    assert c.delete(f'/api/v1/group/access/{world.admin_c.id}/{world.gamma.id}').status_code == 404
