"""HTTP port of the /api/v1/auth/users cases (Users page) in qa-foundation.test.ts (last admin, temporary password,
cross-company invisibility), roles-flows.test.ts (only an admin hands out roles) and auth.test.ts, plus the group
cases: people of the group with access to a company are listed and managed there by their membership, and users of
other companies behave as not found. Complements tests/integrations/test_api_security.py."""
import json

from django.test import Client

from hajiri.db.models import AppUser, CompanyMembership, Employee
from tests.core.fixtures import PW, bearer
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import one

API = '/api/v1/auth'


def post(path, token=None, body=None):
    return Client().post(path, json.dumps(body or {}), content_type='application/json', **(bearer(token) if token else {}))


def patch(path, token, body):
    return Client().patch(path, json.dumps(body), content_type='application/json', **bearer(token))


def get(path, token=None):
    return Client().get(path, **(bearer(token) if token else {}))


def test_the_last_active_admin_cannot_be_disabled_or_demoted_and_nobody_disables_themselves(demo, login_as):
    admin = login_as(['admin'])
    own = patch(f'{API}/users/{admin.user_id}', admin.token, {'status': 'disabled'})
    assert own.status_code == 400 and 'own' in own.json()['error']
    second = login_as(['admin'])
    ok = patch(f'{API}/users/{second.user_id}', admin.token, {'roles': ['hr']})
    assert ok.status_code == 200 and ok.json()['user']['roles'] == ['hr']
    again = patch(f'{API}/users/{admin.user_id}', admin.token, {'roles': ['hr']})
    assert again.status_code == 400 and 'own admin role' in again.json()['error']


def test_a_created_user_must_change_the_temporary_password_before_using_the_api(demo, login_as):
    admin = login_as(['admin'])
    created = post(f'{API}/users', admin.token, {'email': 'temp@test.local', 'name': 't',
                                                 'password': 'temporary-passphrase-1', 'roles': ['hr']})
    assert created.status_code == 201
    login = post(f'{API}/login', body={'email': 'temp@test.local', 'password': 'temporary-passphrase-1'})
    assert login.json()['user']['mustChangePassword'] is True
    tok = login.json()['token']
    blocked = get('/api/v1/employees', tok)
    assert blocked.status_code == 403 and 'password change required' in blocked.json()['error']
    changed = post(f'{API}/change-password', tok, {'currentPassword': 'temporary-passphrase-1',
                                                    'newPassword': 'my-own-long-passphrase'})
    assert changed.status_code == 200
    relogin = post(f'{API}/login', body={'email': 'temp@test.local', 'password': 'my-own-long-passphrase'})
    assert get('/api/v1/employees', relogin.json()['token']).status_code == 200


def test_only_an_admin_hands_out_roles(demo, login_as):
    hr = login_as(['hr'])
    admin = login_as(['admin'])
    u = login_as(['employee'])
    assert patch(f'{API}/users/{u.user_id}', hr.token, {'roles': ['payroll_admin']}).status_code == 403
    ok = patch(f'{API}/users/{u.user_id}', admin.token, {'roles': ['employee', 'payroll_admin']})
    assert ok.status_code == 200 and ok.json()['user']['roles'] == ['employee', 'payroll_admin']
    bad = patch(f'{API}/users/{u.user_id}', admin.token, {'roles': ['superuser']})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'roles': []}).status_code == 400
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'status': 'gone'}).status_code == 400
    unknown = patch(f'{API}/users/00000000-0000-4000-8000-000000000000', admin.token, {'status': 'active'})
    assert unknown.status_code == 404 and unknown.json() == {'error': 'user not found'}


def test_the_list_shows_status_and_a_disabled_account_cannot_sign_in(demo, login_as):
    admin = login_as(['admin'])
    u = login_as(['employee'])
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'status': 'disabled'}).status_code == 200
    row = next(r for r in get(f'{API}/users', admin.token).json()['users'] if r['id'] == u.user_id)
    assert row['status'] == 'disabled' and row['membership'] is False and row['two_factor'] is False
    assert post(f'{API}/login', body={'email': u.email, 'password': PW}).status_code == 401
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'status': 'active'}).json()['user']['status'] == 'active'
    assert post(f'{API}/login', body={'email': u.email, 'password': PW}).status_code == 200
    # Unlinking the employee: null clears it, leaving it out keeps it.
    patch(f'{API}/users/{u.user_id}', admin.token, {'employeeId': demo['sita']})
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'roles': ['employee']}).json()['user']['employee_id'] == demo['sita']
    assert patch(f'{API}/users/{u.user_id}', admin.token, {'employeeId': None}).json()['user']['employee_id'] is None


def test_an_employee_record_is_linked_to_one_sign_in_only(demo, login_as):
    admin = login_as(['admin'])
    first, second = login_as(['employee']), login_as(['employee'])
    assert patch(f'{API}/users/{first.user_id}', admin.token, {'employeeId': demo['sita']}).status_code == 200
    taken = patch(f'{API}/users/{second.user_id}', admin.token, {'employeeId': demo['sita']})
    assert taken.status_code == 409 and 'already linked' in taken.json()['error']
    created = post(f'{API}/users', admin.token, {'email': 'dup@test.local', 'name': 'd', 'password': 'temporary-passphrase-1',
                                                 'roles': ['employee'], 'employeeId': demo['sita']})
    assert created.status_code == 409
    # Saving the same link again is not a conflict with itself.
    assert patch(f'{API}/users/{first.user_id}', admin.token, {'employeeId': demo['sita']}).status_code == 200


def test_group_members_are_listed_and_managed_by_their_membership(world):
    beta_admin, _ = world.session(world.admin_b)
    listed = get(f'{API}/users', beta_admin)
    assert listed.status_code == 200
    by_email = {u['email']: u for u in listed.json()['users']}
    dual = by_email['dual@alpha.test']
    assert dual['membership'] is True and dual['roles'] == ['manager', 'employee']
    assert dual['home_company'] == 'Alpha Traders' and dual['employee_id'] == str(world.dual_beta_employee.id)
    assert by_email['admin@beta.test']['membership'] is False
    assert 'gexec@alpha.test' not in by_email and 'solo@alpha.test' not in by_email
    # Changing roles in Beta changes the membership only; the person stays HR in Alpha.
    r = patch(f'{API}/users/{world.dual.id}', beta_admin, {'roles': ['employee']})
    assert r.status_code == 200 and r.json()['user']['roles'] == ['employee']
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).roles == ['employee']
    assert AppUser.objects.get(pk=world.dual.id).roles == ['hr']
    # Linking to an employee of another company of the group is refused.
    alpha_emp = Employee.objects.filter(company=world.alpha).values_list('id', flat=True).first()
    assert patch(f'{API}/users/{world.dual.id}', beta_admin, {'employeeId': str(alpha_emp)}).json() == {
        'error': 'employee not found'}
    # Disabling access in Beta leaves the Alpha account active.
    assert patch(f'{API}/users/{world.dual.id}', beta_admin, {'status': 'disabled'}).json()['user']['status'] == 'disabled'
    assert one('SELECT status FROM app_user WHERE id = %s', [world.dual.id])['status'] == 'active'
    alpha_hr, _ = world.session(world.dual)
    assert get(f'{API}/users', alpha_hr).status_code == 200


def test_users_and_employees_of_other_companies_behave_as_not_found(world):
    gamma_admin, _ = world.session(world.admin_c)
    beta_admin, _ = world.session(world.admin_b)
    gamma_ids = {u['id'] for u in get(f'{API}/users', gamma_admin).json()['users']}
    assert str(world.admin_b.id) not in gamma_ids and str(world.dual.id) not in gamma_ids
    for uid in (world.admin_b.id, world.dual.id, world.solo.id):
        assert patch(f'{API}/users/{uid}', gamma_admin, {'roles': ['hr']}).json() == {'error': 'user not found'}
    # Solo is HR of Alpha only: not a user of Beta.
    assert patch(f'{API}/users/{world.solo.id}', beta_admin, {'roles': ['hr']}).status_code == 404
    beta_emp = str(world.dual_beta_employee.id)
    refused = post(f'{API}/users', gamma_admin, {'email': 'g2@gamma.test', 'name': 'G', 'password': 'a-long-enough-password',
                                                 'roles': ['employee'], 'employeeId': beta_emp})
    assert refused.status_code == 404 and refused.json() == {'error': 'employee not found'}
    assert AppUser.objects.filter(email='g2@gamma.test').count() == 0
