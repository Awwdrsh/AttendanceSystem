"""HTTP port of apps/api/test/security-selfservice.test.ts (two-step sign-in, company policy and admin reset, active
sessions), of the /auth/users cases in auth.test.ts, security-auth.test.ts and endpoints.test.ts, with company
isolation checks for the account-administration endpoints."""
import json
import time

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.totp import totp_at
from tests.core.fixtures import PW, bearer
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import execute, one, rows

API = '/api/v1/auth'


def post(path, token=None, body=None, client=None, **extra):
    c = client or Client()
    return c.post(path, json.dumps(body or {}), content_type='application/json', **(bearer(token) if token else {}), **extra)


def patch(path, token, body):
    return Client().patch(path, json.dumps(body), content_type='application/json', **bearer(token))


def get(path, token=None, **extra):
    return Client().get(path, **(bearer(token) if token else {}), **extra)


def make_user(demo, email, roles=('employee',), company_id=None, **kw):
    return auth.create_user(company_id=company_id or demo['company_id'], email=email, name=email, password=PW,
                            roles=list(roles), **kw)


def sign_in(email, password=PW, **extra):
    return post(f'{API}/login', body={'email': email, 'password': password}, **extra)


# ---------------------------------------------------------------- two-step sign-in

def test_two_step_sign_in_setup_status_recovery_codes_and_turning_it_off(demo):
    u = make_user(demo, 'twofa@test.local')
    token = sign_in('twofa@test.local').json()['token']
    st = get(f'{API}/2fa', token)
    assert st.status_code == 200
    assert st.json() == {'enabled': False, 'enabledAt': None, 'recoveryCodesLeft': 0, 'required': False, 'setupPending': False}
    setup = post(f'{API}/2fa/setup', token)
    assert setup.status_code == 200
    secret = setup.json()['secret']
    assert f'secret={secret}' in setup.json()['otpauthUri']
    raw = one('SELECT totp_pending_enc, totp_secret_enc FROM app_user WHERE id = %s', [u.id])
    assert secret.encode() not in bytes(raw['totp_pending_enc']) and raw['totp_secret_enc'] is None
    assert get(f'{API}/2fa', token).json()['setupPending'] is True
    assert post(f'{API}/2fa/enable', token, {'code': '000000'}).status_code == 400
    assert post(f'{API}/2fa/enable', token, {'code': '12'}).json()['error'] == 'validation'
    en = post(f'{API}/2fa/enable', token, {'code': totp_at(secret, time.time())})
    assert en.status_code == 200
    recovery = en.json()['recoveryCodes']
    assert len(recovery) == 10 and len(set(recovery)) == 10
    me = get(f'{API}/me', token).json()['user']
    assert me['twoFactorEnabled'] is True and me['mustSetUpTwoFactor'] is False
    assert one('SELECT two_factor FROM session WHERE user_id = %s AND revoked_at IS NULL', [u.id])['two_factor'] is True
    assert post(f'{API}/2fa/setup', token).status_code == 409
    st = get(f'{API}/2fa', token).json()
    assert st['enabled'] is True and st['recoveryCodesLeft'] == 10 and st['enabledAt'].endswith('Z')

    # A password alone no longer signs in.
    r = sign_in('twofa@test.local')
    assert r.status_code == 200 and set(r.json()) == {'twoFactorRequired', 'challenge'}
    assert auth.SESSION_COOKIE not in r.cookies
    ok = post(f'{API}/login/2fa', body={'challenge': r.json()['challenge'], 'code': recovery[0].lower()})
    assert ok.status_code == 200 and ok.json()['usedRecoveryCode'] is True
    assert get(f'{API}/2fa', token).json()['recoveryCodesLeft'] == 9

    # New recovery codes need a current code; turning it off needs the password and a code.
    assert post(f'{API}/2fa/recovery-codes', token, {'code': '123456'}).status_code == 400
    rc = post(f'{API}/2fa/recovery-codes', token, {'code': recovery[3]})
    assert rc.status_code == 200
    recovery = rc.json()['recoveryCodes']
    assert post(f'{API}/2fa/disable', token, {'password': 'wrong-password-123', 'code': recovery[0]}).status_code == 401
    assert post(f'{API}/2fa/disable', token, {'password': PW, 'code': '000000'}).status_code == 400
    assert post(f'{API}/2fa/disable', token, {'password': '', 'code': recovery[1]}).status_code == 400
    off = post(f'{API}/2fa/disable', token, {'password': PW, 'code': recovery[1]})
    assert off.status_code == 200 and off.json() == {'ok': True}
    assert sign_in('twofa@test.local').json()['token']
    assert one('SELECT count(*)::int AS n FROM user_recovery_code WHERE user_id = %s', [u.id])['n'] == 0
    assert post(f'{API}/2fa/disable', token, {'password': PW, 'code': '123456'}).json() == {'error': 'two-step sign-in is already off'}


def test_enabling_signs_out_the_other_sessions(demo):
    make_user(demo, 'others@test.local')
    a = sign_in('others@test.local').json()['token']
    b = sign_in('others@test.local').json()['token']
    secret = post(f'{API}/2fa/setup', a).json()['secret']
    assert post(f'{API}/2fa/enable', a, {'code': totp_at(secret, time.time())}).status_code == 200
    assert get(f'{API}/me', a).status_code == 200
    assert get(f'{API}/me', b).status_code == 401


def test_two_step_endpoints_need_a_session_and_setup_needs_a_real_password(demo):
    assert get(f'{API}/2fa').status_code == 401
    assert post(f'{API}/2fa/setup').json() == {'error': 'unauthorized'}
    make_user(demo, 'temp2fa@test.local', must_change_password=True)
    token = sign_in('temp2fa@test.local').json()['token']
    assert get(f'{API}/2fa', token).status_code == 200                  # works before the password-change gate
    r = post(f'{API}/2fa/setup', token)
    assert r.status_code == 400 and r.json() == {'error': 'choose a new password first'}


def test_a_company_can_require_it_and_an_admin_resets_it(demo, login_as):
    admin = login_as(['admin'])
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    target = make_user(demo, 'needs2fa@test.local', ('hr',))
    hr_token = sign_in('needs2fa@test.local').json()['token']
    put = Client().put('/api/v1/settings/security.policy', json.dumps({'require2faRoles': ['hr', 'hr']}),
                       content_type='application/json', **bearer(admin.token))
    assert put.status_code == 200 and put.json()['value']['require2faRoles'] == ['hr']
    assert get(f'{API}/me', hr_token).json()['user']['mustSetUpTwoFactor'] is True
    blocked = get(f'{API}/users', hr_token)
    assert blocked.status_code == 403 and blocked.json() == {'error': 'two-step sign-in setup required'}
    assert get('/api/v1/notifications', sita.token).status_code == 200    # employees are not affected
    setup = post(f'{API}/2fa/setup', hr_token)
    assert setup.status_code == 200
    en = post(f'{API}/2fa/enable', hr_token, {'code': totp_at(setup.json()['secret'], time.time())})
    assert en.status_code == 200
    assert get(f'{API}/users', hr_token).status_code == 200
    # It cannot be turned off while required.
    off = post(f'{API}/2fa/disable', hr_token, {'password': PW, 'code': en.json()['recoveryCodes'][0]})
    assert off.status_code == 403

    # Only an admin of the same company resets it; the person is signed out everywhere.
    other = auth.create_user(company_id=None, email='x@x.local', name='x', password=PW, roles=['admin'])
    execute("INSERT INTO company (code, name_en) VALUES ('OTHERJ', 'Other Co')")
    other_cid = one("SELECT id FROM company WHERE code = 'OTHERJ'")['id']
    execute('UPDATE app_user SET company_id = %s WHERE id = %s', [other_cid, other.id])
    other_admin = sign_in('x@x.local').json()['token']
    url = f'{API}/users/{target.id}/2fa/reset'
    assert post(url, hr.token, {'reason': 'lost phone'}).status_code == 403
    assert post(url, other_admin, {'reason': 'lost phone'}).status_code == 404
    assert post(url, admin.token, {'reason': ''}).status_code == 400
    assert post(f'{API}/users/not-a-uuid/2fa/reset', admin.token, {'reason': 'lost phone'}).json() == {
        'error': 'validation', 'issues': [{'path': '', 'message': 'Invalid UUID'}]}
    assert post(f'{API}/users/{admin.user_id}/2fa/reset', admin.token, {'reason': 'lost phone'}).status_code == 400
    r = post(url, admin.token, {'reason': 'lost phone'})
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert get(f'{API}/me', hr_token).status_code == 401
    assert one("SELECT after FROM audit_log WHERE action = 'two_factor_reset' AND row_id = %s", [target.id])['after'] == {
        'reason': 'lost phone'}
    listed = get(f'{API}/users', admin.token).json()['users']
    assert next(u for u in listed if u['id'] == target.id)['two_factor'] is False


# ---------------------------------------------------------------- active sessions

def test_lists_my_sessions_signs_out_one_or_all_others_never_someone_elses(demo, login_as):
    sita = login_as(['employee'], demo['sita'])
    make_user(demo, 'sessions@test.local')
    android = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36'
    a = sign_in('sessions@test.local', HTTP_USER_AGENT=android).json()['token']
    b = sign_in('sessions@test.local').json()['token']
    c = sign_in('sessions@test.local').json()['token']
    listed = get(f'{API}/sessions', a)
    assert listed.status_code == 200
    sessions = listed.json()['sessions']
    assert len(sessions) == 3
    assert sessions[0]['current'] is True and sessions[0]['device'] == 'Chrome on Android'
    assert set(sessions[0]) == {'id', 'createdAt', 'lastSeenAt', 'expiresAt', 'ip', 'device', 'twoFactor', 'current'}
    assert 'token_hash' not in listed.content.decode()
    other = next(s['id'] for s in sessions if not s['current'])
    assert Client().delete(f"{API}/sessions/{sessions[0]['id']}", **bearer(a)).status_code == 400
    assert Client().delete(f'{API}/sessions/{other}', **bearer(sita.token)).json() == {'error': 'session not found'}
    assert Client().delete(f'{API}/sessions/not-a-uuid', **bearer(a)).status_code == 400
    r = Client().delete(f'{API}/sessions/{other}', **bearer(a))
    assert r.status_code == 200 and r.json() == {'ok': True}
    left = post(f'{API}/sessions/revoke-others', a)
    assert left.status_code == 200 and left.json() == {'revoked': 1}
    assert get(f'{API}/me', b).status_code == 401
    assert get(f'{API}/me', c).status_code == 401
    assert get(f'{API}/me', a).status_code == 200
    assert get(f'{API}/sessions').status_code == 401


# ---------------------------------------------------------------- account administration

def test_lists_company_users_for_hr_only(demo, login_as):
    hr = login_as(['hr'])
    login_as(['admin'])
    sita = login_as(['employee'], demo['sita'])
    login_as(['manager', 'employee'], demo['ram'])
    r = get(f'{API}/users', hr.token)
    assert r.status_code == 200
    users = r.json()['users']
    assert len(users) >= 4
    assert {'id', 'email', 'roles', 'status', 'employee_id', 'last_login_at', 'created_at', 'two_factor'} <= set(users[0])
    assert 'password_hash' not in users[0]
    assert get(f'{API}/users', sita.token).json() == {'error': 'requires role hr'}
    assert get(f'{API}/users').status_code == 401


def test_admin_creates_users_and_others_cannot(demo, login_as):
    admin = login_as(['admin'])
    hr = login_as(['hr'])
    body = {'email': 'new@test.local', 'name': 'New', 'password': 'a-strong-passphrase-99', 'roles': ['manager']}
    refused = post(f'{API}/users', hr.token, body)
    assert refused.status_code == 403 and refused.json() == {'error': 'requires role admin'}
    created = post(f'{API}/users', admin.token, body)
    assert created.status_code == 201
    user = created.json()['user']
    assert user['email'] == 'new@test.local' and user['roles'] == ['manager'] and user['mustChangePassword'] is True
    assert set(user) >= {'id', 'companyId', 'employeeId', 'name', 'twoFactorEnabled', 'mustSetUpTwoFactor'}
    assert post(f'{API}/users', admin.token, body).status_code == 409
    weak = post(f'{API}/users', admin.token, {**body, 'email': 'weak@test.local', 'password': 'short'})
    assert weak.status_code == 400
    assert post(f'{API}/users', admin.token, {**body, 'email': 'n@test.local', 'name': 'x' * 500}).status_code == 400
    bad_role = post(f'{API}/users', admin.token, {**body, 'email': 'r@test.local', 'roles': ['boss']})
    assert bad_role.status_code == 400 and bad_role.json()['error'] == 'validation'


def test_an_admin_with_a_temporary_password_cannot_manage_accounts(demo):
    make_user(demo, 'temp-admin@test.local', ('admin',), must_change_password=True)
    token = sign_in('temp-admin@test.local').json()['token']
    r = get(f'{API}/users', token)
    assert r.status_code == 403 and r.json() == {'error': 'password change required'}
    assert post(f'{API}/users', token, {'email': 'x@test.local', 'name': 'x', 'password': 'another-long-password',
                                        'roles': ['admin']}).status_code == 403
    assert get(f'{API}/me', token).status_code == 200


def test_updates_roles_status_and_employee_link_within_the_company(demo, login_as):
    admin = login_as(['admin'])
    other = one("INSERT INTO company (code, name_en) VALUES ('SEC1', 'Other') RETURNING id")['id']
    foreign = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'F1', 'Foreign') RETURNING id", [other])['id']
    base = {'email': 'link@test.local', 'name': 'Link', 'password': 'a-long-enough-password', 'roles': ['employee']}
    assert post(f'{API}/users', admin.token, {**base, 'employeeId': str(foreign)}).json() == {'error': 'employee not found'}
    ok = post(f'{API}/users', admin.token, base)
    assert ok.status_code == 201
    uid = ok.json()['user']['id']
    assert patch(f'{API}/users/{uid}', admin.token, {'employeeId': str(foreign)}).status_code == 404
    assert one('SELECT employee_id FROM app_user WHERE id = %s', [uid])['employee_id'] is None
    r = patch(f'{API}/users/{uid}', admin.token, {'employeeId': demo['sita'], 'roles': ['employee', 'manager']})
    assert r.status_code == 200
    assert r.json()['user'] == {'id': uid, 'email': 'link@test.local', 'name': 'Link', 'roles': ['employee', 'manager'],
                                'status': 'active', 'employee_id': demo['sita']}
    token = sign_in('link@test.local', 'a-long-enough-password').json()['token']
    assert patch(f'{API}/users/{uid}', admin.token, {'status': 'disabled'}).json()['user']['status'] == 'disabled'
    assert get(f'{API}/me', token).status_code == 401
    assert patch(f'{API}/users/{admin.user_id}', admin.token, {'status': 'disabled'}).json() == {
        'error': 'you cannot disable your own account or remove your own admin role'}
    assert patch(f'{API}/users/not-a-uuid', admin.token, {'status': 'active'}).status_code == 400
    assert patch(f'{API}/users/{uid}', login_as(['hr']).token, {'status': 'active'}).status_code == 403


def test_users_of_another_company_are_not_found(world):
    beta_admin, _ = world.session(world.admin_b)
    alpha_admin, _ = world.session(world.admin_a)          # group admin, working in Alpha
    outsider, _ = world.session(world.admin_c)
    created = post(f'{API}/users', beta_admin, {'email': 'beta-new@test.local', 'name': 'Beta New',
                                                'password': 'a-long-enough-password', 'roles': ['employee']})
    assert created.status_code == 201 and created.json()['user']['companyId'] == str(world.beta.id)
    uid = created.json()['user']['id']
    for token in (alpha_admin, outsider):
        assert patch(f'{API}/users/{uid}', token, {'roles': ['hr']}).json() == {'error': 'user not found'}
        assert post(f'{API}/users/{uid}/2fa/reset', token, {'reason': 'lost phone'}).status_code == 404
        assert uid not in [u['id'] for u in get(f'{API}/users', token).json()['users']]
    assert uid in [u['id'] for u in get(f'{API}/users', beta_admin).json()['users']]
    switched, _ = world.session(world.admin_a, world.beta)
    assert patch(f'{API}/users/{uid}', switched, {'roles': ['hr']}).status_code == 200
    assert rows('SELECT roles FROM app_user WHERE id = %s', [uid])[0]['roles'] == ['hr']


@pytest.mark.parametrize('path', ['/2fa', '/sessions', '/users'])
def test_cookie_posts_from_another_site_are_refused(demo, path):
    make_user(demo, 'site@test.local', ('admin',))
    c = Client()
    sign_in('site@test.local', client=c)
    url = f'{API}{path}' if path != '/2fa' else f'{API}/2fa/setup'
    if path == '/sessions':
        url = f'{API}/sessions/revoke-others'
    r = post(url, client=c, body={}, HTTP_ORIGIN='http://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert post(url, client=c, body={}, HTTP_ORIGIN='http://testserver').status_code != 403
