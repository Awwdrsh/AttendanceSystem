"""Port of apps/api/test/auth.test.ts and security-auth.test.ts for the JSON sign-in endpoints that Bearer-token
clients use, and of the hire hooks in integrations.test.ts. Page-level sign-in belongs to the web app's tests."""
import json
import threading

import pytest
from django.db import connection
from django.test import Client

from hajiri.core import auth
from hajiri.core.db import one
from hajiri.integrations.auth_api import MAX_FAILURES_PER_IP
from hajiri.integrations.hooks import HireEvent, run_hire_hooks
from hajiri.worker import registry
from tests.core.fixtures import PW, bearer


def post(c, path, body=None, **extra):
    return c.post(path, json.dumps(body or {}), content_type='application/json', **extra)


def make_user(demo, email, roles=('employee',), **kw):
    return auth.create_user(company_id=demo['company_id'], email=email, name=email, password=PW, roles=list(roles), **kw)


def test_login_returns_a_token_that_works_as_bearer(demo):
    make_user(demo, 'hr@test.local', ('hr',))
    c = Client()
    r = post(c, '/api/v1/auth/login', {'email': 'hr@test.local', 'password': PW})
    assert r.status_code == 200
    body = r.json()
    assert body['user']['roles'] == ['hr'] and body['user']['mustChangePassword'] is False
    me = Client().get('/api/v1/auth/me', **bearer(body['token']))
    assert me.status_code == 200 and me.json()['user']['email'] == 'hr@test.local'
    assert Client().get('/api/v1/auth/me').status_code == 401
    assert Client().get('/api/v1/auth/me').json() == {'error': 'unauthorized'}


def test_sets_an_httponly_cookie_on_login_and_clears_it_on_logout(demo):
    make_user(demo, 'cookie@test.local')
    c = Client()
    r = post(c, '/api/v1/auth/login', {'email': 'cookie@test.local', 'password': PW})
    morsel = r.cookies[auth.SESSION_COOKIE]
    assert morsel['httponly'] and morsel['samesite'] == 'Lax'
    assert c.get('/api/v1/auth/me').status_code == 200
    host = {'HTTP_ORIGIN': 'http://testserver'}
    assert post(c, '/api/v1/auth/logout', **host).status_code == 200
    c.cookies[auth.SESSION_COOKIE] = morsel.value
    assert c.get('/api/v1/auth/me').status_code == 401


def test_failures_look_the_same_and_lock_after_ten(demo):
    make_user(demo, 'lock@test.local')
    c = Client()
    assert post(c, '/api/v1/auth/login', {'email': 'nobody@test.local', 'password': 'x' * 12}).status_code == 401
    for _ in range(10):
        post(c, '/api/v1/auth/login', {'email': 'lock@test.local', 'password': 'wrong-password-123'})
    locked = post(c, '/api/v1/auth/login', {'email': 'lock@test.local', 'password': PW})
    assert locked.status_code == 401 and 'locked' in locked.json()['error']


def test_validation_errors_are_400(demo):
    c = Client()
    assert post(c, '/api/v1/auth/login', {'email': 'not-an-email', 'password': 'x'}).status_code == 400
    bad = c.post('/api/v1/auth/login', '{"broken"', content_type='application/json')
    assert bad.status_code == 400 and bad.json() == {'error': 'invalid JSON body'}
    assert c.get('/api/v1/auth/login').status_code == 404


def test_per_address_throttle_on_failed_sign_ins(demo):
    c = Client()
    for _ in range(MAX_FAILURES_PER_IP):
        post(c, '/api/v1/auth/login', {'email': 'ghost@test.local', 'password': 'wrong-password-123'})
    r = post(c, '/api/v1/auth/login', {'email': 'ghost@test.local', 'password': 'wrong-password-123'})
    assert r.status_code == 429 and 'too many failed sign-in attempts' in r.json()['error']


def test_password_change_revokes_sessions_and_enforces_the_policy(demo):
    make_user(demo, 'pw@test.local')
    token = auth.login('pw@test.local', PW).token
    c = Client()
    weak = post(c, '/api/v1/auth/change-password', {'currentPassword': PW, 'newPassword': 'short'}, **bearer(token))
    assert weak.status_code == 400
    same = post(c, '/api/v1/auth/change-password', {'currentPassword': PW, 'newPassword': PW}, **bearer(token))
    assert same.status_code == 400
    huge = post(c, '/api/v1/auth/change-password', {'currentPassword': 'x' * 10_000, 'newPassword': 'a-fresh-long-password'}, **bearer(token))
    assert huge.status_code == 400
    ok = post(c, '/api/v1/auth/change-password', {'currentPassword': PW, 'newPassword': 'another-long-passphrase-42'}, **bearer(token))
    assert ok.status_code == 200
    assert Client().get('/api/v1/auth/me', **bearer(token)).status_code == 401
    assert post(Client(), '/api/v1/auth/login', {'email': 'pw@test.local', 'password': 'another-long-passphrase-42'}).status_code == 200


def test_a_temporary_password_still_reaches_me_and_change_password(demo):
    make_user(demo, 'temp@test.local', ('admin',), must_change_password=True)
    r = post(Client(), '/api/v1/auth/login', {'email': 'temp@test.local', 'password': PW})
    assert r.json()['user']['mustChangePassword'] is True
    assert Client().get('/api/v1/auth/me', **bearer(r.json()['token'])).status_code == 200


def test_refuses_a_cross_site_cookie_request_but_not_same_origin_or_bearer(demo, settings):
    settings.ALLOWED_HOSTS = ['testserver', 'hajiri.local']
    make_user(demo, 'csrf@test.local')
    c = Client()
    post(c, '/api/v1/auth/login', {'email': 'csrf@test.local', 'password': PW})
    evil = post(c, '/api/v1/auth/logout', HTTP_ORIGIN='http://localhost:5173')
    assert evil.status_code == 403 and 'cross-site' in evil.json()['error']
    assert post(c, '/api/v1/auth/logout', HTTP_REFERER='http://evil.example/page').status_code == 403
    assert post(c, '/api/v1/auth/logout', HTTP_ORIGIN='null').status_code == 403
    # Reads are unaffected, and the session survived the refused attempts.
    assert c.get('/api/v1/auth/me', HTTP_ORIGIN='http://evil.example').status_code == 200
    assert post(c, '/api/v1/auth/logout', HTTP_HOST='hajiri.local', HTTP_ORIGIN='http://hajiri.local').status_code == 200
    token = auth.login('csrf@test.local', PW).token
    assert post(Client(), '/api/v1/auth/logout', HTTP_ORIGIN='http://evil.example', **bearer(token)).status_code == 200


def test_two_step_sign_in(demo):
    import time
    from hajiri.core.totp import totp_at
    u = make_user(demo, '2fa@test.local')
    secret = auth.begin_setup(u)['secret']
    auth.confirm_setup(u, totp_at(secret, time.time() - 30))
    r = post(Client(), '/api/v1/auth/login', {'email': '2fa@test.local', 'password': PW}).json()
    assert r['twoFactorRequired'] is True and 'token' not in r
    wrong = post(Client(), '/api/v1/auth/login/2fa', {'challenge': r['challenge'], 'code': '000000'})
    assert wrong.status_code == 401
    ok = post(Client(), '/api/v1/auth/login/2fa', {'challenge': r['challenge'], 'code': totp_at(secret, time.time())})
    assert ok.status_code == 200 and ok.json()['user']['twoFactorEnabled'] is True


@pytest.mark.django_db(transaction=True)
def test_counts_parallel_wrong_guesses_exactly_and_locks_the_account():
    from hajiri.company_settings.demo import DEMO, seed_demo
    seed_demo(calendar=False, defaults=False)
    auth.create_user(company_id=DEMO['company_id'], email='race@test.local', name='r', password=PW, roles=['employee'])

    def guess():
        try:
            post(Client(), '/api/v1/auth/login', {'email': 'race@test.local', 'password': 'wrong-password-123'}, REMOTE_ADDR='10.9.9.9')
        finally:
            connection.close()
    threads = [threading.Thread(target=guess) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    row = one("SELECT failed_logins, locked_until > now() AS locked FROM app_user WHERE email = 'race@test.local'")
    assert row['failed_logins'] == 12 and row['locked'] is True
    assert post(Client(), '/api/v1/auth/login', {'email': 'race@test.local', 'password': PW}).status_code == 401


# ---------------------------------------------------------------- hire hooks (integrations.test.ts)

def test_a_hired_candidate_gets_a_joined_event_and_onboarding(demo, monkeypatch):
    events, onboarded = [], []
    monkeypatch.setitem(registry._HOOKS, 'people.record_career_event', lambda **kw: events.append(kw))
    monkeypatch.setitem(registry._HOOKS, 'lifecycle.auto_start_onboarding', lambda *a: onboarded.append(a))
    from hajiri.core.db import execute
    execute("UPDATE employee SET designation = 'Engineer' WHERE id = %s", [demo['sita']])
    run_hire_hooks(HireEvent(company_id=demo['company_id'], employee_id=demo['sita'], application_id='a', job_id='j',
                             join_date='2026-11-01', actor_id=None))
    assert events == [{'company_id': demo['company_id'], 'employee_id': demo['sita'], 'kind': 'hire', 'effective_date': '2026-11-01',
                       'details': {'designation': 'Engineer'}, 'note': 'Hired through the careers pipeline', 'actor_id': None}]
    assert onboarded == [(demo['company_id'], demo['sita'], '2026-11-01', None)]


def test_health_and_security_headers(db, settings):
    settings.MIDDLEWARE = [*settings.MIDDLEWARE, 'hajiri.core.security.SecurityHeadersMiddleware']
    r = Client().get('/health')
    assert r.status_code == 200 and r.json() == {'ok': True, 'db': 'up'}
    assert "default-src 'self'" in r['Content-Security-Policy'] and "frame-ancestors 'none'" in r['Content-Security-Policy']
    assert r['X-Content-Type-Options'] == 'nosniff'
    assert 'X-Powered-By' not in r
