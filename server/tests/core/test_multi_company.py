"""Groups of companies: one sign-in, several companies, an active company per session, isolation between them."""
import json

import pytest
from django.test import Client

from hajiri.core import auth, companies
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.db.models import Company, CompanyMembership, Employee, HelpdeskTicket
from hajiri.helpdesk import services as hd
from tests.web.factories import PW, bearer, htmx, make_company, make_employee, make_user, sign_in, toasts


@pytest.fixture
def group(db):
    """Admin of Alpha adds Beta to the group; Hari (HR at Alpha) gets HR access to Beta."""
    alpha = make_company('ALPHA', 'Alpha Traders')
    admin = make_user(alpha, roles=['admin'], email='admin@alpha.test')
    hari = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Hari'), email='hari@alpha.test')
    beta = companies.create_group_company(admin, {'code': 'BETA', 'name_en': 'Beta Foods'})
    beta_co = Company.objects.get(pk=beta['id'])
    token = auth.login(admin.email, PW).token
    admin = auth.resolve_session(token)
    admin_beta = auth.switch_company(admin, token, beta['id'])
    beta_emp = make_employee(beta_co, name='Hari at Beta')
    companies.grant_access(admin_beta, {'email': 'hari@alpha.test', 'roles': ['hr'], 'employee_id': str(beta_emp.id)})
    return {'alpha': alpha, 'beta': beta_co, 'admin_token': token, 'admin_beta': admin_beta, 'hari': hari,
            'beta_emp': beta_emp}


def test_new_company_joins_the_group_seeded_and_creator_is_its_admin(group):
    alpha, beta = group['alpha'], group['beta']
    alpha.refresh_from_db()
    assert alpha.group_id and beta.group_id == alpha.group_id
    assert alpha.group.name_en == 'Alpha Traders group'
    assert group['admin_beta'].company_id == str(beta.id) and group['admin_beta'].roles == ('admin',)
    assert group['admin_beta'].home_company_id == str(alpha.id)
    # Seeded like any new company (leave types and settings).
    assert beta.leave_types.exists() and beta.company_settings.exists()
    with pytest.raises(ConflictError):
        companies.create_group_company(group['admin_beta'], {'code': 'beta', 'name_en': 'Again'})


def test_switching_changes_company_roles_and_employee_and_back(group):
    token = auth.login('hari@alpha.test', PW).token
    hari = auth.resolve_session(token)
    assert hari.company_id == str(group['alpha'].id) and hari.multi_company
    assert set(hari.company_ids) == {str(group['alpha'].id), str(group['beta'].id)}
    there = auth.switch_company(hari, token, str(group['beta'].id))
    assert there.company_id == str(group['beta'].id) and there.employee_id == str(group['beta_emp'].id)
    assert auth.resolve_session(token).company_id == str(group['beta'].id)        # it sticks to the session
    back = auth.switch_company(there, token, str(group['alpha'].id))
    assert back.company_id == str(group['alpha'].id) and back.employee_id == group['hari'].employee_id
    # A company without access is not found; a fresh sign-in starts at home.
    with pytest.raises(NotFoundError):
        auth.switch_company(hari, token, str(make_company().id))
    assert auth.resolve_session(auth.login('hari@alpha.test', PW).token).company_id == str(group['alpha'].id)


def test_data_stays_in_its_company(group):
    token = auth.login('hari@alpha.test', PW).token
    hari = auth.resolve_session(token)
    t = hd.create_ticket(hari, {'categoryId': hd.list_categories(hari)[0]['id'], 'subject': 'Alpha question', 'body': 'x'})
    there = auth.switch_company(hari, token, str(group['beta'].id))
    assert hd.queue(there, {}) == []
    with pytest.raises(NotFoundError):
        hd.ticket_detail(there, t['id'])
    assert HelpdeskTicket.objects.for_company(group['beta'].id).count() == 0
    assert Employee.objects.for_company(group['beta'].id).count() == 1
    with pytest.raises(NotFoundError):
        auth.assert_manages(there, group['hari'].employee_id)


def test_members_count_as_users_of_the_company(group):
    from hajiri.core.users import company_roles, list_users, user_ids_with_role
    beta = str(group['beta'].id)
    hari = group['hari']
    assert company_roles(beta, hari.id) == ['hr']
    assert hari.id in user_ids_with_role(beta, 'hr')
    listed = {u['email']: u for u in list_users(group['admin_beta'])}
    assert listed['hari@alpha.test']['membership'] is True and listed['hari@alpha.test']['roles'] == ['hr']
    # HR at Beta can assign Beta tickets to Hari (a member, not a Beta account).
    assert hari.id in [a['id'] for a in hd.list_agents(group['admin_beta'])]


def test_access_rules(group):
    admin_beta = group['admin_beta']
    with pytest.raises(NotFoundError):
        companies.grant_access(admin_beta, {'email': 'nobody@else.test', 'roles': ['hr']})
    outsider = make_user(make_company(), roles=['hr'], email='out@other.test')
    with pytest.raises(NotFoundError, match='in your group'):
        companies.grant_access(admin_beta, {'email': outsider.email, 'roles': ['hr']})
    with pytest.raises(BadRequestError):
        companies.grant_access(admin_beta, {'email': 'hari@alpha.test', 'roles': []})
    with pytest.raises(BadRequestError, match='own access'):
        companies.revoke_access(admin_beta, admin_beta.id)
    # The last admin of Beta (a member) stays, even against a session that still claims admin after a demotion.
    stale = make_admin_member(group)
    CompanyMembership.objects.filter(user_id=stale.id).update(roles=['hr'])
    with pytest.raises(BadRequestError, match='last active admin'):
        companies.revoke_access(stale, admin_beta.id)
    hr_only = auth.switch_company(auth.resolve_session(tok := auth.login('hari@alpha.test', PW).token), tok,
                                  str(group['beta'].id))
    with pytest.raises(ForbiddenError):
        companies.grant_access(hr_only, {'email': 'admin@alpha.test', 'roles': ['hr']})


def make_admin_member(group):
    """A second Beta admin to act with (only to reach the last-admin rule)."""
    u = make_user(group['alpha'], roles=['employee'], email='second@alpha.test')
    companies.grant_access(group['admin_beta'], {'email': u.email, 'roles': ['admin']})
    token = auth.login(u.email, PW).token
    return auth.switch_company(auth.resolve_session(token), token, str(group['beta'].id))


def test_revoking_sends_open_sessions_home(group):
    token = auth.login('hari@alpha.test', PW).token
    auth.switch_company(auth.resolve_session(token), token, str(group['beta'].id))
    companies.revoke_access(group['admin_beta'], group['hari'].id)
    u = auth.resolve_session(token)
    assert u.company_id == str(group['alpha'].id) and not u.multi_company
    assert not CompanyMembership.objects.filter(user_id=group['hari'].id).exists()


def test_group_overview_counts_only_companies_with_a_viewing_role(group):
    token = auth.login('hari@alpha.test', PW).token
    hari = auth.resolve_session(token)
    o = companies.group_overview(hari)
    assert {c['code'] for c in o['companies']} == {'ALPHA', 'BETA'}
    assert o['totals']['headcount'] == 2
    # As an employee at Beta, Beta is not counted.
    CompanyMembership.objects.filter(user_id=hari.id).update(roles=['employee'])
    hari = auth.resolve_session(token)
    assert companies.group_company_ids(hari) == [str(group['alpha'].id)]
    assert not companies.can_view_group(hari)


def test_pages_switcher_and_group_page(group):
    c = Client()
    sign_in(c, 'hari@alpha.test')
    page = c.get('/group')
    assert page.status_code == 200 and b'Beta Foods' in page.content and b'Alpha Traders group' in page.content
    home = c.get('/me/profile/security')
    assert b'hj-companymenu' in home.content
    r = c.post('/_/web/company/switch', {'company_id': str(group['beta'].id)}, **htmx())
    assert r.status_code == 204 and r['HX-Redirect'] == '/' and toasts(r)[0]['message'] == 'Now working in Beta Foods.'
    assert c.post('/_/web/company/switch', {'company_id': str(make_company().id)}, **htmx()).status_code == 404
    single = Client()
    sign_in(single, make_user(make_company(), roles=['hr']))
    assert single.get('/group', **htmx()).status_code == 403


def test_admin_adds_company_and_gives_access_from_the_page(db):
    alpha = make_company('GAMMA', 'Gamma Ltd')
    make_user(alpha, roles=['admin'], email='boss@gamma.test')
    c = Client()
    sign_in(c, 'boss@gamma.test')
    assert b'not part of a group yet' in c.get('/group').content
    assert c.post('/_/web/group/companies/new', {'code': '!', 'name_en': ''}, **htmx()).status_code == 422
    r = c.post('/_/web/group/companies/new', {'code': 'DELTA', 'name_en': 'Delta Ltd'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'].startswith('Delta Ltd added to the group')
    staff = make_user(alpha, roles=['employee'], email='staff@gamma.test')
    r = c.post('/_/web/group/access/new', {'email': staff.email, 'roles': ['manager']}, **htmx())
    assert r.status_code == 400 and 'already belongs' in toasts(r)[0]['message']     # Gamma is staff's home
    delta = Company.objects.get(code='DELTA')
    c.post('/_/web/company/switch', {'company_id': str(delta.id)}, **htmx())
    r = c.post('/_/web/group/access/new', {'email': staff.email, 'roles': ['manager']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Access given.'
    assert list(CompanyMembership.objects.get(user_id=staff.id, company=delta).roles) == ['manager']
    assert b'staff@gamma.test' in c.get('/_/web/group/access', **htmx('group-members')).content
    r = c.post(f'/_/web/group/access/{staff.id}/revoke', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Access removed.'


def test_json_api_lists_and_switches(group):
    token = auth.login('hari@alpha.test', PW).token
    c = Client()
    r = c.get('/api/v1/me/companies', **bearer(token))
    assert r.status_code == 200
    rows = r.json()['companies']
    assert [x['home'] for x in rows] == [True, False] and rows[0]['active']
    r = c.post('/api/v1/me/companies/switch', json.dumps({'companyId': str(group['beta'].id)}),
               content_type='application/json', **bearer(token))
    assert r.status_code == 200 and r.json()['companyId'] == str(group['beta'].id)
    r = c.post('/api/v1/me/companies/switch', json.dumps({'companyId': 'nope'}), content_type='application/json',
               **bearer(token))
    assert r.status_code == 400 and r.json() == {'error': 'validation', 'issues': [{'path': 'companyId', 'message': 'Invalid UUID'}]}
    assert c.get('/api/v1/group/overview', **bearer(token)).json()['totals']['headcount'] == 2
