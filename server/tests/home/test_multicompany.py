"""
Home, notifications, Settings and Users & roles in a group of companies: every page works in the ACTIVE company,
ids of another company are not found, and the Users page manages the access people of the group have to this
company (company_membership), never their own account.

Uses the `world` of tests/multicompany: Alpha and Beta in one group, Gamma outside it.
"""
import pytest

from hajiri.core.settings_store import get_setting
from hajiri.db.models import AppUser, Branch, CompanyMembership, NotificationOutbox
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import htmx, hx_triggers, toasts

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- Users & roles

def test_users_page_lists_members_with_their_home_company_and_group_role_holders(world):
    c = world.signed_in(world.admin_b)
    html = c.get('/settings/users').content.decode()
    # Dipak's account belongs to Alpha; in Beta he is a member with Beta's roles.
    assert 'Dipak Dual' in html and 'From Alpha Traders' in html and 'Remove access' in html
    assert 'data-confirm-label="Remove access"' in html and "Remove Dipak Dual&#x27;s access" in html
    assert 'Bimal Admin' in html
    # Alpha's own accounts that have no access to Beta are not listed.
    assert 'solo@alpha.test' not in html
    # Group-role holders work here through the group, listed read-only.
    assert 'Through group roles' in html and 'ghr@beta.test' in html and 'gexec@alpha.test' in html
    # Beta is in a group: its admin can give people of the group access.
    assert 'Give access from the group' in html


def test_standalone_company_has_no_group_access_controls(world):
    html = world.signed_in(world.admin_c).get('/settings/users').content.decode()
    assert 'Give access from the group' not in html and 'Through group roles' not in html
    assert world.signed_in(world.admin_c).get('/_/home/users/access/new', **htmx('hj-modal-body')).status_code == 200


def test_admin_changes_a_members_roles_only_in_this_company(world):
    c = world.signed_in(world.admin_b)
    r = c.post(f'/_/home/users/{world.dual.id}', {'roles_sent': '1', 'roles': ['employee']}, **htmx())
    assert r.status_code == 204 and 'hj:refresh-users' in hx_triggers(r)
    assert list(CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).roles) == ['employee']
    # His own account (HR in Alpha) is untouched.
    assert list(AppUser.objects.get(pk=world.dual.id).roles) == ['hr']
    r = c.post(f'/_/home/users/{world.dual.id}', {'status': 'disabled'}, **htmx())
    assert r.status_code == 204
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).status == 'disabled'
    assert AppUser.objects.get(pk=world.dual.id).status == 'active'


def test_another_companys_user_is_not_found(world):
    # Gamma's admin and Alpha's admin working in Alpha cannot reach Beta's own admin.
    for person, company in ((world.admin_c, None), (world.admin_a, world.alpha)):
        c = world.signed_in(person, company)
        assert c.post(f'/_/home/users/{world.admin_b.id}', {'status': 'disabled'}, **htmx()).status_code == 404
        assert c.get(f'/_/home/users/{world.admin_b.id}/reset', **htmx('hj-modal-body')).status_code == 404
        assert c.post(f'/_/home/users/{world.admin_b.id}/reset', {'reason': 'Lost phone'}, **htmx()).status_code == 404
        assert c.post(f'/_/home/users/{world.dual.id}/access/revoke', **htmx()).status_code == 404
    assert AppUser.objects.get(pk=world.admin_b.id).status == 'active'
    # Working in Beta, the group admin reaches Beta's people.
    c = world.signed_in(world.admin_a, world.beta)
    assert c.get(f'/_/home/users/{world.admin_b.id}/reset', **htmx('hj-modal-body')).status_code == 200


def test_admin_resets_two_step_sign_in_for_a_member(world):
    c = world.signed_in(world.admin_b)
    form = c.get(f'/_/home/users/{world.dual.id}/reset', **htmx('hj-modal-body')).content.decode()
    assert 'Dipak Dual is signed out everywhere' in form
    r = c.post(f'/_/home/users/{world.dual.id}/reset', {'reason': ' x '}, **htmx())
    assert r.status_code == 400
    r = c.post(f'/_/home/users/{world.dual.id}/reset', {'reason': 'Lost phone'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Two-step sign-in reset for Dipak Dual.'


def test_admin_gives_and_removes_access_for_a_person_of_the_group(world):
    c = world.signed_in(world.admin_b)
    form = c.get('/_/home/users/access/new', **htmx('hj-modal-body')).content.decode()
    assert 'Give access to this company' in form and 'Roles in this company' in form
    # The form is checked first (422, shown in the modal).
    r = c.post('/_/home/users/access/new', {'email': 'not-an-email', 'roles': ['employee']}, **htmx())
    assert r.status_code == 422
    r = c.post('/_/home/users/access/new', {'email': 'solo@alpha.test', 'roles': ['employee']}, **htmx())
    assert r.status_code == 204, r.content
    assert toasts(r)[0]['message'] == 'Access given.' and 'hj:refresh-users' in hx_triggers(r)
    ms = CompanyMembership.objects.get(user_id=world.solo.id, company=world.beta)
    assert list(ms.roles) == ['employee'] and ms.status == 'active'
    html = c.get('/settings/users').content.decode()
    assert 'solo@alpha.test' in html and 'From Alpha Traders' in html
    # Someone outside the group cannot be given access.
    r = c.post('/_/home/users/access/new', {'email': 'admin@gamma.test', 'roles': ['employee']}, **htmx())
    assert r.status_code == 404 and 'no account with this email in your group' in toasts(r)[0]['message']
    # HR (not admin) cannot give or remove access.
    hr = world.signed_in(world.ghr)
    assert hr.post('/_/home/users/access/new', {'email': 'solo@alpha.test', 'roles': ['hr']}, **htmx()).status_code == 403
    assert hr.post(f'/_/home/users/{world.solo.id}/access/revoke', **htmx()).status_code == 403
    r = c.post(f'/_/home/users/{world.solo.id}/access/revoke', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Access removed.'
    assert not CompanyMembership.objects.filter(user_id=world.solo.id, company=world.beta).exists()
    # A home account of this company has no access row to remove.
    assert c.post(f'/_/home/users/{world.ghr.id}/access/revoke', **htmx()).status_code == 404


# ---------------------------------------------------------------- notifications

def _notice(company, user, title):
    NotificationOutbox.objects.create(company=company, user_id=user.id, channel='in_app', template='t', title=title,
                                      body='body text')


def test_notifications_follow_the_active_company(world):
    from hajiri.home.nav import BADGES
    _notice(world.alpha, world.dual, 'Alpha notice')
    _notice(world.beta, world.dual, 'Beta notice')
    _notice(world.beta, world.dual, 'Beta notice two')
    c = world.signed_in(world.dual)
    html = c.get('/me/notifications').content.decode()
    assert 'Alpha notice' in html and 'Beta notice' not in html and '1 unread' in html
    _token, in_beta = world.session(world.dual, world.beta)
    assert BADGES['notifications'](in_beta) == 2
    c = world.signed_in(world.dual, world.beta)
    html = c.get('/me/notifications').content.decode()
    assert 'Beta notice' in html and 'Alpha notice' not in html and '2 unread' in html


# ---------------------------------------------------------------- settings

def test_settings_are_saved_for_the_active_company_only(world):
    a = world.signed_in(world.admin_a, world.alpha)
    b = world.signed_in(world.admin_a, world.beta)
    assert b.post('/_/home/settings/company', {'nameEn': 'Beta Foods Pvt. Ltd.', 'address': 'Pokhara', 'pan': '',
                                                'ssfEmployerId': ''}, **htmx()).status_code == 204
    assert get_setting(str(world.beta.id), 'company.profile')['address'] == 'Pokhara'
    assert (get_setting(str(world.alpha.id), 'company.profile') or {}).get('address') != 'Pokhara'
    assert b.post('/_/home/settings/security', {'require2faRoles': ['hr']}, **htmx()).status_code == 204
    assert get_setting(str(world.beta.id), 'security.policy') == {'require2faRoles': ['hr']}
    assert (get_setting(str(world.alpha.id), 'security.policy') or {}).get('require2faRoles') != ['hr']
    r = b.post('/_/home/settings/branches', {'code': 'pkr', 'name': 'Pokhara'}, **htmx())
    assert r.status_code == 200 and 'Pokhara' in r.content.decode()
    assert Branch.objects.for_company(world.beta.id).filter(code='PKR').exists()
    assert not Branch.objects.for_company(world.alpha.id).filter(code='PKR').exists()
    assert 'Pokhara' not in a.get('/settings?tab=org', **htmx('settings-panel')).content.decode()


def test_settings_pages_are_refused_where_the_person_is_not_hr(world):
    # Dipak is HR at home (Alpha) but only a manager in Beta.
    assert world.signed_in(world.dual).get('/settings').status_code == 200
    c = world.signed_in(world.dual, world.beta)
    assert c.get('/settings/users').status_code == 403
    assert c.post('/_/home/settings/security', {'require2faRoles': ['hr']}, **htmx()).status_code == 403
    # Group HR is HR in every company of the group.
    assert world.signed_in(world.ghr, world.alpha).get('/settings/setup').status_code == 200


def test_home_page_uses_the_employee_record_of_the_active_company(world):
    c = world.signed_in(world.dual, world.beta)
    r = c.get('/')
    assert r.status_code == 200 and 'Dipak' in r.content.decode()
    assert c.get('/_/home/today', **htmx()).status_code == 200
