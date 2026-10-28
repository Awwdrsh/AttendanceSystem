"""
Sign-in pages and routes through django.test.Client: ports of apps/api/test/auth.test.ts and security-auth.test.ts
(the page and flow parts; account administration lives with the users page).
"""
import json
import time

import pytest
from django.test import Client

from hajiri.core import auth
from tests.support.sql import execute, one
from hajiri.core.totp import TOTP_STEP_SECONDS, totp_at
from hajiri.web import views

from .factories import (PW, audit_actions, bearer, htmx, make_company, make_employee, make_user, require_two_factor_for,
                        sign_in, signed_in_client, toasts, user_row)


@pytest.fixture(autouse=True)
def _reset_throttle():
    views.reset_throttle()
    yield
    views.reset_throttle()


@pytest.fixture
def co(db):
    return make_company('DEMO', 'CoreValley Pvt. Ltd.')


def post_login(client, email, password=PW, **extra):
    return client.post('/login', {'email': email, 'password': password}, **extra)


def json_login(client, email, password=PW):
    return client.post('/login', json.dumps({'email': email, 'password': password}), content_type='application/json')


# ---------------------------------------------------------------- auth.test.ts

def test_rejects_anonymous_calls_and_accepts_a_session(co):
    c = Client()
    assert c.get('/_/web/me', HTTP_ACCEPT='application/json').status_code == 401
    r = c.get('/me/profile/security')
    assert r.status_code == 302 and r['Location'] == '/login?next=/me/profile/security'
    user = make_user(co, roles=['hr'])
    token = sign_in(c, user)
    me = c.get('/_/web/me')
    assert me.status_code == 200 and me.json()['user']['roles'] == ['hr']
    assert Client().get('/_/web/me', **bearer(token)).json()['user']['id'] == user.id


def test_sets_an_httponly_cookie_on_login_and_clears_it_on_logout(co):
    make_user(co, email='cookie@test.local')
    c = Client()
    r = post_login(c, 'cookie@test.local')
    assert r.status_code == 302 and r['Location'] == '/'
    cookie = r.cookies[auth.SESSION_COOKIE]
    assert cookie['httponly'] and cookie['samesite'] == 'Lax' and cookie['path'] == '/'
    assert int(cookie['max-age']) == 12 * 3600
    assert not cookie['secure']
    assert c.get('/_/web/me').status_code == 200
    out = c.post('/logout')
    assert out.status_code == 302 and out['Location'] == '/login'
    assert out.cookies[auth.SESSION_COOKIE].value == ''
    c.cookies[auth.SESSION_COOKIE] = cookie.value  # the old token no longer works
    assert c.get('/_/web/me', HTTP_ACCEPT='application/json').status_code == 401


def test_cookie_is_secure_when_configured(co, settings):
    settings.SESSION_COOKIE_SECURE = True
    make_user(co, email='secure@test.local')
    r = post_login(Client(), 'secure@test.local')
    assert r.cookies[auth.SESSION_COOKIE]['secure']


def test_json_login_answers_like_the_api(co):
    make_user(co, email='json@test.local', roles=['employee'])
    r = json_login(Client(), 'json@test.local')
    assert r.status_code == 200
    body = r.json()
    assert body['user']['email'] == 'json@test.local' and body['token']
    assert auth.resolve_session(body['token']).email == 'json@test.local'


def test_wrong_password_unknown_user_and_disabled_user_fail_the_same_way_lockout_after_10(co):
    make_user(co, email='lock@test.local')
    c = Client()
    r = post_login(c, 'nobody@test.local', 'x' * 12)
    assert r.status_code == 401 and b'invalid email or password' in r.content
    for _ in range(10):
        post_login(c, 'lock@test.local', 'wrong-password-123')
    locked = post_login(c, 'lock@test.local')
    assert locked.status_code == 401
    assert b'locked' in locked.content
    disabled = make_user(co, email='off@test.local')
    execute("UPDATE app_user SET status = 'disabled' WHERE id = %s", [disabled.id])
    assert b'invalid email or password' in post_login(c, 'off@test.local').content


def test_password_change_revokes_sessions_and_enforces_the_policy(co):
    user = make_user(co, email='pw@test.local')
    c = Client()
    token = sign_in(c, user)
    weak = c.post('/change-password', {'current_password': PW, 'new_password': 'short', 'new_password_again': 'short'})
    assert weak.status_code == 400 and b'at least 12 characters' in weak.content
    ok = c.post('/change-password', {'current_password': PW, 'new_password': 'another-long-passphrase-42',
                                     'new_password_again': 'another-long-passphrase-42'})
    assert ok.status_code == 302 and ok['Location'] == '/login'
    assert toasts(ok)[0]['message'] == 'Password changed. Please sign in again.'
    assert auth.resolve_session(token) is None
    assert post_login(Client(), 'pw@test.local', 'another-long-passphrase-42').status_code == 302


def test_json_password_change(co):
    user = make_user(co)
    c = Client()
    token = sign_in(c, user)
    r = c.post('/change-password', json.dumps({'currentPassword': PW, 'newPassword': 'short'}), content_type='application/json')
    assert r.status_code == 400 and r.json()['error'] == 'Password must be at least 12 characters'
    r = c.post('/change-password', json.dumps({'currentPassword': PW, 'newPassword': 'a-new-long-passphrase'}),
               content_type='application/json', **bearer(token))
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_enforces_roles_on_the_employee_search(co):
    c, _ = signed_in_client(co, roles=['employee'])
    assert c.get('/_/web/employees/search', HTTP_ACCEPT='application/json').status_code == 403
    assert c.get('/_/web/employees/search', **htmx()).status_code == 403


# ---------------------------------------------------------------- security-auth.test.ts

def test_refuses_a_cross_site_cookie_post_same_origin_and_bearer_pass(co):
    user = make_user(co, email='csrf@test.local')
    c = Client(enforce_csrf_checks=True)
    sign_in(c, user)
    evil = c.post('/logout', HTTP_ORIGIN='http://localhost:5173', HTTP_ACCEPT='application/json')
    assert evil.status_code == 403 and 'cross-site' in evil.json()['error']
    evil_ref = c.post('/logout', HTTP_REFERER='http://evil.example/page', secure=True, HTTP_ACCEPT='application/json')
    assert evil_ref.status_code == 403
    assert c.post('/logout', HTTP_ORIGIN='null', HTTP_ACCEPT='application/json').status_code == 403
    # Reads are unaffected, and the session is still alive after the refused attempts.
    assert c.get('/_/web/me', HTTP_ORIGIN='http://evil.example').status_code == 200
    # Same origin with the token passes.
    c.get('/login')  # sets the CSRF cookie
    page = c.get('/me/profile/security')
    token = page.context['csrf_token']
    ok = c.post('/logout', {'csrfmiddlewaretoken': str(token)}, HTTP_ORIGIN='http://testserver')
    assert ok.status_code == 302
    # Bearer tokens are not sent automatically by browsers, so they are not subject to the check.
    other = make_user(co)
    t = auth.login(other.email, PW).token
    r = Client(enforce_csrf_checks=True).post('/logout', HTTP_ORIGIN='http://evil.example', **bearer(t))
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_cross_site_refusal_on_an_htmx_request_is_a_toast(co):
    user = make_user(co)
    c = Client(enforce_csrf_checks=True)
    sign_in(c, user)
    r = c.post('/_/web/sessions/revoke-others', HTTP_ORIGIN='http://evil.example', **htmx())
    assert r.status_code == 403
    assert toasts(r)[0]['message'] == 'cross-site request refused'


def test_sends_a_strict_csp_and_nosniff(db):
    r = Client().get('/login')
    csp = r.headers['Content-Security-Policy']
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'X-Powered-By' not in r.headers
    assert r.headers['X-Frame-Options'] == 'DENY'


@pytest.mark.django_db(transaction=True)
def test_counts_parallel_wrong_guesses_and_locks_the_account():
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection
    co = make_company('RACE')
    make_user(co, email='race@test.local')

    def guess(_):
        try:
            return post_login(Client(), 'race@test.local', 'wrong-password-123', HTTP_X_FORWARDED_FOR='10.9.9.9').status_code
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(guess, range(12)))
    assert codes == [401] * 12
    row = one("SELECT failed_logins, locked_until FROM app_user WHERE email = 'race@test.local'")
    assert row['failed_logins'] == 12
    assert row['locked_until'] is not None
    assert post_login(Client(), 'race@test.local').status_code == 401


def test_per_ip_throttle_stops_guessing_from_one_address(co):
    make_user(co, email='ip@test.local')
    c = Client()
    for i in range(views.MAX_FAILURES_PER_IP):
        post_login(c, f'nobody{i}@test.local', 'wrong-password-123')
    r = post_login(c, 'ip@test.local')
    assert r.status_code == 429 and views.THROTTLED.encode() in r.content
    assert json_login(Client(), 'ip@test.local').status_code == 429
    # Another address is not affected.
    assert post_login(Client(REMOTE_ADDR='10.1.1.1'), 'ip@test.local').status_code == 302


def test_rejects_reusing_the_current_password_and_oversized_inputs(co):
    c, _ = signed_in_client(co)
    same = c.post('/change-password', {'current_password': PW, 'new_password': PW, 'new_password_again': PW})
    assert same.status_code == 400 and b'must be different' in same.content
    big = c.post('/change-password', {'current_password': 'x' * 10_000, 'new_password': 'a-fresh-long-password',
                                      'new_password_again': 'a-fresh-long-password'})
    assert big.status_code == 400
    mismatch = c.post('/change-password', {'current_password': PW, 'new_password': 'a-fresh-long-password',
                                           'new_password_again': 'another-fresh-password'})
    assert mismatch.status_code == 400 and b'do not match' in mismatch.content
    wrong = c.post('/change-password', {'current_password': 'not-my-password', 'new_password': 'a-fresh-long-password',
                                        'new_password_again': 'a-fresh-long-password'})
    assert wrong.status_code == 401 and b'current password is incorrect' in wrong.content


def test_a_temporary_password_sends_you_to_change_it_first(co):
    make_user(co, email='temp-admin@test.local', roles=['admin'], must_change_password=True)
    c = Client()
    r = post_login(c, 'temp-admin@test.local')
    assert r['Location'] == '/change-password'
    assert c.get('/me/profile/security').status_code == 200          # sign-in security works before the change
    assert c.get('/_/web/components')['Location'] == '/change-password'
    assert c.get('/_/web/employees/search', HTTP_ACCEPT='application/json').status_code == 403
    page = c.get('/change-password')
    assert b'Choose a new password' in page.content
    assert c.get('/_/web/me').status_code == 200


# ---------------------------------------------------------------- login page and next

def test_login_page_and_next_redirect(co):
    make_user(co, email='next@test.local')
    c = Client()
    page = c.get('/login?next=/me/profile/security')
    assert page.status_code == 200 and b'Sign in' in page.content and b'value="/me/profile/security"' in page.content
    r = c.post('/login', {'email': 'next@test.local', 'password': PW, 'next': '/me/profile/security'})
    assert r['Location'] == '/me/profile/security'
    assert c.get('/login')['Location'] == '/'                       # already signed in
    # An outside address is never followed.
    c2 = Client()
    r = c2.post('/login', {'email': 'next@test.local', 'password': PW, 'next': 'https://evil.example/x'})
    assert r['Location'] == '/'
    r = Client().post('/login', {'email': 'next@test.local', 'password': PW, 'next': '//evil.example'})
    assert r['Location'] == '/'


def test_login_form_validation(db):
    r = Client().post('/login', {'email': 'not-an-email', 'password': ''})
    assert r.status_code == 400 and b'Enter a valid email address' in r.content


def test_leaver_sees_the_closed_account_message(co):
    emp = make_employee(co, exit_date='2020-01-01')
    make_user(co, employee=emp, email='left@test.local')
    r = post_login(Client(), 'left@test.local')
    assert r.status_code == 401 and b'closed when you left' in r.content


# ---------------------------------------------------------------- two-step sign-in

def _enable_two_factor(user) -> str:
    s = auth.begin_setup(user)
    auth.confirm_setup(user, totp_at(s['secret'], time.time()))
    return s['secret']


def _fresh_code(secret: str, steps: int = 1) -> str:
    return totp_at(secret, time.time() + steps * TOTP_STEP_SECONDS)


def test_two_step_login_page_flow(co):
    user = make_user(co, email='two@test.local')
    secret = _enable_two_factor(user)
    c = Client()
    r = post_login(c, 'two@test.local')
    assert r.status_code == 200 and b'Two-step sign-in' in r.content
    challenge = r.context['challenge']
    assert auth.SESSION_COOKIE not in r.cookies
    bad = c.post('/login/2fa', {'challenge': challenge, 'code': '000000'})
    assert bad.status_code == 401 and b'That code is not correct' in bad.content
    assert bad.context['challenge'] == challenge                      # still on the code step
    ok = c.post('/login/2fa', {'challenge': challenge, 'code': _fresh_code(secret)})
    assert ok.status_code == 302 and ok['Location'] == '/'
    assert auth.resolve_session(ok.cookies[auth.SESSION_COOKIE].value).id == user.id
    assert 'login_2fa_failed' in audit_actions(user.id)
    # The challenge is used up: going back to the password step.
    again = c.post('/login/2fa', {'challenge': challenge, 'code': _fresh_code(secret, 2)})
    assert again.status_code == 401 and b'expired' in again.content and again.context['challenge'] is None


def test_two_step_login_with_a_recovery_code_shows_a_toast(co):
    user = make_user(co)
    s = auth.begin_setup(user)
    codes = auth.confirm_setup(user, totp_at(s['secret'], time.time()))
    c = Client()
    challenge = post_login(c, user.email).context['challenge']
    r = c.post('/login/2fa', {'challenge': challenge, 'code': codes[0], 'recovery': '1'})
    assert r.status_code == 302
    t = toasts(r)
    assert t[0]['title'] == 'Signed in with a recovery code' and t[0]['tone'] == 'info'
    # The next full page shows it once and clears the cookie.
    c.cookies[auth.SESSION_COOKIE] = r.cookies[auth.SESSION_COOKIE].value
    page = c.get('/me/profile/security')
    assert b'Signed in with a recovery code' in page.content
    assert page.cookies['hj_flash'].value == ''


def test_two_step_json_login(co):
    user = make_user(co)
    secret = _enable_two_factor(user)
    r = json_login(Client(), user.email)
    assert r.json() == {'twoFactorRequired': True, 'challenge': r.json()['challenge']}
    ok = Client().post('/login/2fa', json.dumps({'challenge': r.json()['challenge'], 'code': _fresh_code(secret)}),
                       content_type='application/json')
    assert ok.status_code == 200 and ok.json()['token'] and 'usedRecoveryCode' not in ok.json()


def test_setup_from_the_security_page(co):
    c, user = signed_in_client(co)
    other = Client()
    other_token = sign_in(other, user)                                # a second session
    page = c.get('/me/profile/security')
    assert page.status_code == 200 and b'Turn on two-step sign-in' in page.content
    start = c.post('/_/web/2fa/setup', **htmx('two-factor-setup'))
    assert start.status_code == 200 and b'Scan this code' in start.content and b'<svg class="hj-qr"' in start.content
    secret = start.context['secret']
    wrong = c.post('/_/web/2fa/enable', {'code': '000000'}, **htmx('two-factor-setup'))
    assert wrong.status_code == 422 and b'That code did not match' in wrong.content
    ok = c.post('/_/web/2fa/enable', {'code': totp_at(secret, time.time())}, **htmx('two-factor-setup'))
    assert ok.status_code == 200 and b'Save your recovery codes now' in ok.content
    assert len(ok.context['codes']) == 10
    assert toasts(ok)[0]['message'] == 'Two-step sign-in is on.'
    assert one('SELECT two_factor FROM session WHERE token_hash = %s', [auth.sha256_hex(c.cookies[auth.SESSION_COOKIE].value)])['two_factor']
    assert auth.resolve_session(other_token) is None                  # other sessions ended
    page = c.get('/me/profile/security')
    assert b'unused recovery' in page.content and b'New recovery codes' in page.content


def test_new_recovery_codes_and_turning_off(co):
    c, user = signed_in_client(co)
    secret = _enable_two_factor(user)
    bad = c.post('/_/web/2fa/recovery-codes', {'code': '000000'}, **htmx())
    assert bad.status_code == 400 and toasts(bad)[0] == {'message': 'That code did not match.', 'tone': 'err', 'title': 'Not changed'}
    ok = c.post('/_/web/2fa/recovery-codes', {'code': _fresh_code(secret)}, **htmx())
    assert ok.status_code == 200 and len(ok.context['codes']) == 10 and b'hajiri-recovery-codes.txt' in ok.content
    codes = ok.context['codes']
    wrong_pw = c.post('/_/web/2fa/disable', {'password': 'nope-nope-nope', 'code': codes[0]}, **htmx())
    assert wrong_pw.status_code == 401 and toasts(wrong_pw)[0]['title'] == 'Still on'
    off = c.post('/_/web/2fa/disable', {'password': PW, 'code': codes[1]}, **htmx())
    assert off.status_code == 204 and off['HX-Redirect'] == '/me/profile/security'
    assert toasts(off)[0]['message'] == 'Two-step sign-in is off.'
    assert user_row(user)['totp_enabled_at'] is None
    assert 'two_factor_disable' in audit_actions(user.id)


def test_company_requirement_sends_you_to_setup_and_blocks_turning_off(co):
    require_two_factor_for(co, 'hr')
    user = make_user(co, roles=['hr'], email='hr2fa@test.local')
    c = Client()
    r = post_login(c, 'hr2fa@test.local')
    assert r['Location'] == '/setup-two-step'
    page = c.get('/setup-two-step')
    assert page.status_code == 200 and b'CoreValley requires a second step for your role' in page.content
    assert c.get('/_/web/components')['Location'] == '/setup-two-step'
    blocked = c.get('/_/web/employees/search', **htmx())
    assert blocked.status_code == 403 and blocked['HX-Redirect'] == '/setup-two-step'
    start = c.post('/_/web/2fa/setup', {'context': 'required'}, **htmx('two-factor-setup'))
    ok = c.post('/_/web/2fa/enable', {'code': totp_at(start.context['secret'], time.time()), 'context': 'required'}, **htmx())
    assert ok.context['done_url'] == '/'
    assert c.get('/setup-two-step')['Location'] == '/'
    off = c.post('/_/web/2fa/disable', {'password': PW, 'code': '123456'}, **htmx())
    assert off.status_code == 403 and 'cannot be turned off' in toasts(off)[0]['message']
    assert user


def test_setup_needs_a_new_password_first(co):
    user = make_user(co, must_change_password=True)
    c = Client()
    sign_in(c, user)
    r = c.post('/_/web/2fa/setup', **htmx('two-factor-setup'))
    assert r.status_code == 422 and b'choose a new password first' in r.content


# ---------------------------------------------------------------- sessions

def test_sessions_list_and_sign_out_elsewhere(co):
    c, user = signed_in_client(co)
    phone = Client()
    phone_token = sign_in(phone, user, user_agent='Mozilla/5.0 (iPhone) Safari/604', ip='10.0.0.7')
    laptop = Client()
    sign_in(laptop, user)
    page = c.get('/me/profile/security')
    assert b'This device' in page.content and b'Safari on iOS' in page.content and b'Sign out everywhere else' in page.content
    phone_session = next(s for s in page.context['sessions'] if s.device == 'Safari on iOS')
    r = c.post(f'/_/web/sessions/{phone_session.id}/revoke', **htmx('sessions'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Signed out Safari on iOS.'
    assert auth.resolve_session(phone_token) is None
    mine = next(s for s in page.context['sessions'] if s.current)
    own = c.post(f'/_/web/sessions/{mine.id}/revoke', **htmx('sessions'))
    assert own.status_code == 400 and 'sign out instead' in toasts(own)[0]['message']
    missing = c.post('/_/web/sessions/00000000-0000-0000-0000-000000000000/revoke', **htmx('sessions'))
    assert missing.status_code == 404
    others = c.post('/_/web/sessions/revoke-others', **htmx('sessions'))
    assert toasts(others)[0]['message'] == 'Signed out 1 other session.'
    none_left = c.post('/_/web/sessions/revoke-others', **htmx('sessions'))
    assert toasts(none_left)[0]['message'] == 'No other sessions.'
    assert 'session_revoke' in audit_actions()


def test_htmx_request_without_a_session_redirects_to_login(db):
    r = Client().post('/_/web/sessions/revoke-others', **htmx(current_url='http://testserver/me/profile/security'))
    assert r.status_code == 401 and r['HX-Redirect'] == '/login?next=/me/profile/security'


def test_json_login_validation_answers_with_zod_style_issues(co):
    r = Client().post('/login', json.dumps({'email': 'not-an-email', 'password': ''}), content_type='application/json')
    body = r.json()
    assert r.status_code == 400 and body['error'] == 'validation'
    assert {i['path'] for i in body['issues']} == {'email', 'password'}
    r2 = Client().post('/login/2fa', json.dumps({'challenge': 'short', 'code': '1'}), content_type='application/json')
    assert r2.status_code == 400 and r2.json()['error'] == 'validation'


def test_the_page_needs_the_new_password_twice(co):
    # ChangePasswordPage.tsx compares the two fields before sending; an empty repeat does not match.
    user = make_user(co)
    c = Client()
    sign_in(c, user)
    r = c.post('/change-password', {'current_password': PW, 'new_password': 'a-fresh-long-password'})
    assert r.status_code == 400 and b'The two new passwords do not match' in r.content
    assert c.get('/_/web/me').status_code == 200          # nothing changed, still signed in


def test_signing_out_after_the_session_ended_goes_to_the_sign_in_page(db):
    r = Client().post('/logout')
    assert r.status_code == 302 and r['Location'] == '/login'
    assert Client().post('/logout', HTTP_ACCEPT='application/json').status_code == 401


def test_signing_out_a_session_that_is_gone_says_not_signed_out(co):
    user = make_user(co)
    c = Client()
    sign_in(c, user)
    import uuid
    r = c.post(f'/_/web/sessions/{uuid.uuid4()}/revoke', **htmx())
    assert r.status_code == 404 and toasts(r) == [{'message': 'session not found', 'tone': 'err', 'title': 'Not signed out'}]
