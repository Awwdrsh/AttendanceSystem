"""The rest of apps/api/test/security-auth.test.ts and the API-level cases of qa-foundation.test.ts that apply to the
JSON endpoints: request logs never hold tokens or passwords, configured CORS origins pass the cross-site check, password
changes refuse reuse and oversized input, account administration refuses oversized names, and oversized bodies are 413."""
import json
import logging

from django.test import Client

from hajiri.core import auth
from hajiri.core.errors import BadRequestError
from hajiri.core.users import admin_create_user
from tests.core.fixtures import PW, bearer

import pytest


def post(c, path, body=None, **extra):
    return c.post(path, json.dumps(body or {}), content_type='application/json', **extra)


class _Sink(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        extra = ' '.join(str(v) for k, v in record.__dict__.items() if k in ('request', 'args'))
        self.lines.append(f'{msg} {extra}')


def test_request_logging_never_writes_session_tokens_or_passwords(demo):
    auth.create_user(company_id=demo['company_id'], email='log@test.local', name='l', password=PW, roles=['employee'])
    sink = _Sink()
    root = logging.getLogger()
    old = root.level
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        login = post(Client(), '/api/v1/auth/login', {'email': 'log@test.local', 'password': PW})
        token = login.json()['token']
        Client().get('/api/v1/auth/me', HTTP_COOKIE=f'{auth.SESSION_COOKIE}={token}', **bearer(token))
        # A refused request is logged too; it must not echo the credentials either.
        post(Client(), '/api/v1/auth/login', {'email': 'log@test.local', 'password': 'wrong-password-123'})
    finally:
        root.removeHandler(sink)
        root.setLevel(old)
    out = '\n'.join(sink.lines)
    assert token not in out
    assert PW not in out and 'wrong-password-123' not in out


def test_allows_configured_cors_origins(demo, settings):
    settings.CORS_ORIGINS = 'http://localhost:5173'
    auth.create_user(company_id=demo['company_id'], email='cors@test.local', name='c', password=PW, roles=['employee'])
    c = Client()
    login = post(c, '/api/v1/auth/login', {'email': 'cors@test.local', 'password': PW}, HTTP_ORIGIN='http://localhost:5173')
    assert login['Access-Control-Allow-Origin'] == 'http://localhost:5173'
    assert login['Access-Control-Allow-Credentials'] == 'true'
    pre = c.options('/api/v1/auth/logout', HTTP_ORIGIN='http://localhost:5173')
    assert pre.status_code == 204 and 'POST' in pre['Access-Control-Allow-Methods']
    # Another origin still gets no CORS headers and is still refused for cookie requests.
    other = post(c, '/api/v1/auth/logout', HTTP_ORIGIN='http://evil.example')
    assert other.status_code == 403 and not other.has_header('Access-Control-Allow-Origin')
    assert post(c, '/api/v1/auth/logout', HTTP_ORIGIN='http://localhost:5173').status_code == 200


def test_no_cors_headers_without_configuration(demo, settings, monkeypatch):
    settings.CORS_ORIGINS = ''
    r = Client().get('/api/v1/auth/me', HTTP_ORIGIN='http://localhost:5173')
    assert r.status_code == 401 and not r.has_header('Access-Control-Allow-Origin')


def test_rejects_reusing_the_current_password_and_oversized_inputs(login_as):
    me = login_as(['employee'])
    c = Client()
    same = post(c, '/api/v1/auth/change-password', {'currentPassword': PW, 'newPassword': PW}, **bearer(me.token))
    assert same.status_code == 400
    huge = post(c, '/api/v1/auth/change-password', {'currentPassword': 'x' * 10_000, 'newPassword': 'a-fresh-long-password'},
                **bearer(me.token))
    assert huge.status_code == 400
    # The session survived both refusals.
    assert c.get('/api/v1/auth/me', **bearer(me.token)).status_code == 200


def test_rejects_oversized_names_and_emails(login_as):
    admin = login_as(['admin'])
    with pytest.raises(BadRequestError):
        admin_create_user(admin.user, {'email': 'n@test.local', 'name': 'x' * 500, 'password': 'a-long-enough-password',
                                       'roles': ['employee']})
    with pytest.raises(BadRequestError):
        admin_create_user(admin.user, {'email': ('x' * 300) + '@test.local', 'name': 'n', 'password': 'a-long-enough-password',
                                       'roles': ['employee']})


def test_bodies_over_the_limit_answer_413(demo):
    big = post(Client(), '/api/v1/auth/login', {'email': 'a@test.local', 'password': 'x' * (3 * 1024 * 1024)})
    assert big.status_code == 413 and big.json() == {'error': 'request body too large'}


def test_unknown_api_routes_answer_json_404(db, settings):
    # app.ts: app.use((_req, res) => res.status(404).json({ error: 'not found' })) (qa-foundation 'unknown API routes answer JSON 404')
    from django.test import Client
    settings.DEBUG = False
    r = Client().get('/api/v1/no/such/route')
    assert r.status_code == 404 and r.json() == {'error': 'not found'}
    r = Client().post('/api/v1/no/such/route', data='{}', content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'not found'}
