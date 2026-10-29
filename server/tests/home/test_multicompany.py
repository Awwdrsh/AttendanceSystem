"""
Home, notifications, Settings and Users & roles in a group of companies: every JSON endpoint works in the ACTIVE
company, ids of another company are not found, and the Users list manages the access people of the group have to
this company (company_membership), never their own account. Giving and removing that access is the group admin's
(Group hub, /api/v1/group/access).

Uses the `world` of tests/multicompany: Alpha and Beta in one group, Gamma outside it.
"""
import pytest

from hajiri.core.settings_store import get_setting
from hajiri.db.models import AppUser, Branch, CompanyMembership, NotificationOutbox
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def _json(c, method, url, body=None):
    if method == 'get':
        return c.get(url)
    return getattr(c, method)(url, body if body is not None else {}, content_type='application/json')


# ---------------------------------------------------------------- Users & roles

def test_standalone_company_lists_only_its_own_accounts(world):
    c = world.signed_in(world.admin_c)
    users = c.get('/api/v1/auth/users').json()['users']
    assert {u['email'] for u in users} == {'admin@gamma.test'} and not any(u['membership'] for u in users)
    # Outside a group there is no group access to manage.
    assert c.get('/api/v1/group/access').status_code in (403, 404)


def test_users_list_shows_members_with_their_home_company(world):
    users = {u['email']: u for u in world.signed_in(world.admin_b).get('/api/v1/auth/users').json()['users']}
    # Dipak's account belongs to Alpha; in Beta he is a member with Beta's roles.
    dual = users['dual@alpha.test']
    assert dual['membership'] is True and dual['home_company'] == 'Alpha Traders'
    assert sorted(dual['roles']) == ['employee', 'manager']
    assert users['admin@beta.test']['name'] == 'Bimal Admin' and users['admin@beta.test']['membership'] is False
    # Alpha's own accounts that have no access to Beta are not listed.
    assert 'solo@alpha.test' not in users


def test_admin_changes_a_members_roles_only_in_this_company(world):
    c = world.signed_in(world.admin_b)
    r = _json(c, 'patch', f'/api/v1/auth/users/{world.dual.id}', {'roles': ['employee']})
    assert r.status_code == 200, r.content
    assert list(CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).roles) == ['employee']
    # His own account (HR in Alpha) is untouched.
    assert list(AppUser.objects.get(pk=world.dual.id).roles) == ['hr']
    r = _json(c, 'patch', f'/api/v1/auth/users/{world.dual.id}', {'status': 'disabled'})
    assert r.status_code == 200, r.content
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).status == 'disabled'
    assert AppUser.objects.get(pk=world.dual.id).status == 'active'


def test_another_companys_user_is_not_found(world):
    # Gamma's admin and Alpha's admin working in Alpha cannot reach Beta's own admin.
    for person, company in ((world.admin_c, None), (world.admin_a, world.alpha)):
        c = world.signed_in(person, company)
        assert _json(c, 'patch', f'/api/v1/auth/users/{world.admin_b.id}', {'status': 'disabled'}).status_code == 404
        r = _json(c, 'post', f'/api/v1/auth/users/{world.admin_b.id}/2fa/reset', {'reason': 'Lost phone'})
        assert r.status_code == 404
    assert AppUser.objects.get(pk=world.admin_b.id).status == 'active'
    # Working in Beta, the group admin reaches Beta's people.
    c = world.signed_in(world.admin_a, world.beta)
    assert world.admin_b.id in {str(u['id']) for u in c.get('/api/v1/auth/users').json()['users']}
    r = _json(c, 'post', f'/api/v1/auth/users/{world.admin_b.id}/2fa/reset', {'reason': 'Lost phone'})
    assert r.status_code == 200, r.content


def test_admin_resets_two_step_sign_in_for_a_member(world):
    c = world.signed_in(world.admin_b)
    r = _json(c, 'post', f'/api/v1/auth/users/{world.dual.id}/2fa/reset', {'reason': ' x '})
    assert r.status_code == 400
    r = _json(c, 'post', f'/api/v1/auth/users/{world.dual.id}/2fa/reset', {'reason': 'Lost phone'})
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_group_admin_gives_and_removes_access_for_a_person_of_the_group(world):
    c = world.signed_in(world.admin_a)          # started the group: group admin
    beta = str(world.beta.id)
    r = _json(c, 'post', '/api/v1/group/access', {'email': 'not-an-email', 'companyId': beta, 'roles': ['employee']})
    assert r.status_code == 400
    r = _json(c, 'post', '/api/v1/group/access', {'email': 'solo@alpha.test', 'companyId': beta, 'roles': ['employee']})
    assert r.status_code == 200, r.content
    ms = CompanyMembership.objects.get(user_id=world.solo.id, company=world.beta)
    assert list(ms.roles) == ['employee'] and ms.status == 'active'
    users = {u['email']: u for u in world.signed_in(world.admin_b).get('/api/v1/auth/users').json()['users']}
    assert users['solo@alpha.test']['home_company'] == 'Alpha Traders'
    # Someone outside the group cannot be given access.
    r = _json(c, 'post', '/api/v1/group/access', {'email': 'admin@gamma.test', 'companyId': beta, 'roles': ['employee']})
    assert r.status_code == 404
    # A company admin or HR without a group role cannot give or remove access.
    for other in (world.signed_in(world.admin_b), world.signed_in(world.ghr)):
        r = _json(other, 'post', '/api/v1/group/access', {'email': 'solo@alpha.test', 'companyId': beta, 'roles': ['hr']})
        assert r.status_code == 403
        assert _json(other, 'delete', f'/api/v1/group/access/{world.solo.id}/{beta}').status_code == 403
    r = _json(c, 'delete', f'/api/v1/group/access/{world.solo.id}/{beta}')
    assert r.status_code == 204, r.content
    assert not CompanyMembership.objects.filter(user_id=world.solo.id, company=world.beta).exists()


# ---------------------------------------------------------------- notifications

def _notice(company, user, title):
    NotificationOutbox.objects.create(company=company, user_id=user.id, channel='in_app', template='t', title=title,
                                      body='body text')


def test_notifications_follow_the_active_company(world):
    _notice(world.alpha, world.dual, 'Alpha notice')
    _notice(world.beta, world.dual, 'Beta notice')
    _notice(world.beta, world.dual, 'Beta notice two')
    d = world.signed_in(world.dual).get('/api/v1/notifications').json()
    assert [n['title'] for n in d['notifications']] == ['Alpha notice'] and d['unread'] == 1
    d = world.signed_in(world.dual, world.beta).get('/api/v1/notifications').json()
    assert sorted(n['title'] for n in d['notifications']) == ['Beta notice', 'Beta notice two'] and d['unread'] == 2


# ---------------------------------------------------------------- settings

def test_settings_are_saved_for_the_active_company_only(world):
    a = world.signed_in(world.admin_a, world.alpha)
    b = world.signed_in(world.admin_a, world.beta)
    profile = {'nameEn': 'Beta Foods Pvt. Ltd.', 'address': 'Pokhara'}
    r = _json(b, 'put', '/api/v1/settings/company.profile', profile)
    assert r.status_code == 200, r.content
    assert get_setting(str(world.beta.id), 'company.profile')['address'] == 'Pokhara'
    assert (get_setting(str(world.alpha.id), 'company.profile') or {}).get('address') != 'Pokhara'
    r = _json(b, 'put', '/api/v1/settings/security.policy', {'require2faRoles': ['hr']})
    assert r.status_code == 200, r.content
    assert get_setting(str(world.beta.id), 'security.policy') == {'require2faRoles': ['hr']}
    assert (get_setting(str(world.alpha.id), 'security.policy') or {}).get('require2faRoles') != ['hr']
    r = _json(b, 'post', '/api/v1/settings/branches', {'code': 'PKR', 'nameEn': 'Pokhara'})
    assert r.status_code == 201, r.content
    assert Branch.objects.for_company(world.beta.id).filter(code='PKR').exists()
    assert not Branch.objects.for_company(world.alpha.id).filter(code='PKR').exists()
    assert 'Pokhara' not in a.get('/api/v1/settings/branches').content.decode()


def test_settings_are_refused_where_the_person_is_not_hr(world):
    # Dipak is HR at home (Alpha) but only a manager in Beta.
    assert world.signed_in(world.dual).get('/api/v1/auth/users').status_code == 200
    c = world.signed_in(world.dual, world.beta)
    assert c.get('/api/v1/auth/users').status_code == 403
    assert _json(c, 'put', '/api/v1/settings/security.policy', {'require2faRoles': ['hr']}).status_code == 403
    # Group HR is HR in every company of the group.
    assert world.signed_in(world.ghr, world.alpha).get('/api/v1/auth/users').status_code == 200


def test_home_page_uses_the_employee_record_of_the_active_company(world):
    c = world.signed_in(world.dual, world.beta)
    me = c.get('/api/v1/auth/me').json()
    user = me.get('user') or me
    assert user['employeeId'] == str(world.dual_beta_employee.id)
    r = c.get('/api/v1/dashboard/me')
    assert r.status_code == 200, r.content
    assert c.get('/api/v1/punch/today').status_code == 200
