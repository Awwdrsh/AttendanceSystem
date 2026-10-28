"""The company switcher, the Group hub entry and the group pages and JSON endpoints."""
import pytest
from django.urls import reverse

from hajiri.db.models import CompanyMembership, GroupMember
from tests.web.factories import htmx, toasts


def switch(client, company, next_url=None):
    data = {'company_id': str(company.id)}
    if next_url is not None:
        data['next'] = next_url
    return client.post('/_/web/company/switch', data, **htmx())


def test_active_company_is_always_shown(world):
    page = world.signed_in(world.solo).get('/me/profile/security')
    html = page.content.decode()
    assert 'Alpha Traders' in html and '>ALPHA<' in html
    assert 'hj-companymenu' not in html                 # one company: no switcher
    page = world.signed_in(world.dual).get('/me/profile/security').content.decode()
    assert 'hj-companymenu' in page and 'hj-companymenu-top' in page and 'Beta Foods' in page and '>BETA<' in page


def test_switching_reloads_the_same_page(world):
    c = world.signed_in(world.ghr)
    r = switch(c, world.alpha, '/employees?q=sita')
    assert r.status_code == 204 and r['HX-Redirect'] == '/employees?q=sita'
    assert toasts(r)[0]['message'] == 'Now working in Alpha Traders.'
    # The page now works in Alpha.
    assert c.get('/api/v1/me/companies').json()['companies'][0]['active'] is False
    assert any(x['active'] and x['code'] == 'ALPHA' for x in c.get('/api/v1/me/companies').json()['companies'])


def test_a_record_page_goes_to_its_hub(world):
    c = world.signed_in(world.ghr)
    emp = world.ghr.employee_id
    r = switch(c, world.alpha, f'/employees/{emp}')
    assert r['HX-Redirect'] == reverse('employees:list')
    r = switch(c, world.beta, f'/employees?employee={emp}')
    assert r['HX-Redirect'] == reverse('employees:list')


def test_a_hub_hidden_in_the_new_company_goes_home(world):
    c = world.signed_in(world.dual)                       # HR at Alpha, manager + employee at Beta
    assert switch(c, world.beta, reverse('payroll:runs'))['HX-Redirect'] == '/'
    assert switch(c, world.alpha, '/me/profile/security')['HX-Redirect'] == '/me/profile/security'


@pytest.mark.parametrize('next_url', ['https://evil.test/x', '//evil.test/x', '/_/web/group/access', '/api/v1/me/companies',
                                      '/no/such/page', ''])
def test_unsafe_or_odd_next_goes_home(world, next_url):
    c = world.signed_in(world.dual)
    assert switch(c, world.beta, next_url)['HX-Redirect'] == '/'


def test_plain_form_switch_redirects(world):
    c = world.signed_in(world.dual)
    r = c.post('/_/web/company/switch', {'company_id': str(world.beta.id), 'next': '/me/profile/security'})
    assert r.status_code == 302 and r['Location'] == '/me/profile/security'


def _nav_urls(client):
    html = client.get('/me/profile/security').content.decode()
    return html


def test_group_hub_in_the_sidebar(world):
    for who in ('ghr', 'gexec', 'admin_a'):
        assert 'href="/group"' in _nav_urls(world.signed_in(getattr(world, who))), who
    assert 'href="/group"' not in _nav_urls(world.signed_in(world.solo))
    # A standalone company's admin sees it too: that is where a sister company is added.
    assert 'href="/group"' in _nav_urls(world.signed_in(world.admin_c))


def test_group_page_for_each_role(world):
    page = world.signed_in(world.gexec).get('/group')
    assert page.status_code == 200 and b'Beta Foods' in page.content and b'Alpha Traders group' in page.content
    assert b'Give group roles' not in page.content and b'Add a company' not in page.content
    page = world.signed_in(world.admin_a).get('/group')
    assert b'Give group roles' in page.content and b'Add a company' in page.content and b'ghr@beta.test' in page.content
    page = world.signed_in(world.admin_b).get('/group')         # Beta's own admin: no group admin powers
    assert page.status_code == 200 and b'Give group roles' not in page.content and b'Add a company' not in page.content
    assert world.signed_in(world.solo).get('/group', **htmx()).status_code == 403


def test_group_roles_pages(world):
    c = world.signed_in(world.admin_a)
    assert c.get('/_/web/group/roles/new', **htmx()).status_code == 200
    bad = c.post('/_/web/group/roles/new', {'email': world.solo.email}, **htmx())
    assert bad.status_code == 422
    ok = c.post('/_/web/group/roles/new', {'email': world.solo.email, 'roles': ['group_hr']}, **htmx())
    assert ok.status_code == 204 and toasts(ok)[0]['message'] == 'Group roles saved.'
    assert GroupMember.objects.get(user_id=world.solo.id).roles == ['group_hr']
    assert b'solo@alpha.test' in c.get('/_/web/group/roles', **htmx()).content
    gone = c.post(f'/_/web/group/roles/{world.solo.id}/revoke', **htmx())
    assert gone.status_code == 204 and not GroupMember.objects.filter(user_id=world.solo.id).exists()
    other = world.signed_in(world.admin_b)
    assert other.get('/_/web/group/roles/new', **htmx()).status_code == 403
    assert other.post(f'/_/web/group/roles/{world.ghr.id}/revoke', **htmx()).status_code == 403


def test_group_roles_api(world):
    c = world.signed_in(world.admin_a)
    me = c.get('/api/v1/me/companies').json()
    assert me['groupRoles'] == ['group_admin'] and me['groupId'] == str(world.alpha.group_id)
    listed = c.get('/api/v1/group/roles').json()['items']
    assert {r['email'] for r in listed} == {'admin@alpha.test', 'ghr@beta.test', 'gexec@alpha.test'}
    r = c.post('/api/v1/group/roles', {'email': world.dual.email, 'roles': ['group_executive']}, content_type='application/json')
    assert r.status_code == 201 and r.json() == {'userId': world.dual.id, 'roles': ['group_executive']}
    bad = c.post('/api/v1/group/roles', {'email': world.dual.email, 'roles': ['admin']}, content_type='application/json')
    assert bad.status_code == 400
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 204
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 404
    outsider = world.signed_in(world.admin_c)
    assert outsider.get('/api/v1/group/roles').status_code == 403
    assert outsider.delete(f'/api/v1/group/roles/{world.ghr.id}').status_code == 403


def test_group_overview_api(world):
    o = world.signed_in(world.ghr).get('/api/v1/group/overview').json()
    assert [c['code'] for c in o['companies']] == ['ALPHA', 'BETA'] and o['totals']['headcount'] >= 4
    alone = world.signed_in(world.admin_c).get('/api/v1/group/overview').json()   # a standalone company: itself only
    assert [c['code'] for c in alone['companies']] == ['GAMMA']
    assert world.signed_in(world.dual, world.beta).get('/api/v1/group/overview').json()['companies'][0]['code'] == 'ALPHA'


def test_start_in_this_company(world):
    c = world.signed_in(world.dual, world.beta)
    r = c.post('/_/web/company/default', {'company_id': str(world.beta.id)}, **htmx())
    assert r.status_code == 204
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).is_default
    assert c.post('/_/web/company/default', {'company_id': str(world.gamma.id)}, **htmx()).status_code == 404


@pytest.mark.parametrize('path', ['/', '/employees', '/payroll', '/leave', '/overview', '/insights', '/settings/users', '/group'])
def test_group_role_holder_without_an_employee_record_can_work(world, path):
    """Group HR in Alpha has no Alpha employee record: the company pages work, self-service pages step aside."""
    c = world.signed_in(world.ghr, world.alpha)
    r = c.get(path)
    assert r.status_code in (200, 302), (path, r.status_code)
    if r.status_code == 302:
        assert c.get(r['Location']).status_code == 200


@pytest.mark.parametrize('path', ['/overview', '/insights'])
def test_group_executive_reads_every_company(world, path):
    c = world.signed_in(world.gexec, world.beta)
    assert c.get(path).status_code == 200
    assert c.get('/payroll').status_code in (302, 403)
