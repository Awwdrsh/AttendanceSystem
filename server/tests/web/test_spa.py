"""The React app served by Django (frontend/ built into server/frontend_build/): index.html for every page address,
the build's files at the site root with the right caching, the TS API's Content-Security-Policy, and JSON for every
/api/ address. Uses a small stand-in build in a temporary folder, so it runs without Node and without a real build;
test_real_build_is_served checks the committed build when it is there."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

from tests.web.factories import make_company, signed_in_client

INDEX = ('<!doctype html><html><head><script type="module" crossorigin src="/assets/index-AbCd1234.js"></script>'
         '</head><body><div id="root"></div></body></html>')
TS_CSP = ("default-src 'self'; base-uri 'self'; connect-src 'self'; font-src 'self' data:; form-action 'self'; "
          "frame-ancestors 'none'; img-src 'self' data: blob:; object-src 'none'; script-src 'self'; "
          "script-src-attr 'none'; style-src 'self' 'unsafe-inline'")


@pytest.fixture
def build(tmp_path):
    """A stand-in frontend build, served by a fresh WhiteNoise (it scans its folders when the middleware starts)."""
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'index.html').write_text(INDEX)
    (tmp_path / 'assets' / 'index-AbCd1234.js').write_text('console.log("hajiri")')
    (tmp_path / 'assets' / 'index-Xy_-9876.css').write_text('body{}')
    (tmp_path / 'sw.js').write_text('self.addEventListener("fetch", () => {})')
    (tmp_path / 'manifest.webmanifest').write_text('{"name": "Hajiri"}')
    (tmp_path / 'favicon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    with override_settings(FRONTEND_BUILD_DIR=tmp_path, WHITENOISE_ROOT=tmp_path):
        yield tmp_path


def body(r) -> bytes:
    return b''.join(r.streaming_content) if r.streaming else r.content


PAGES = ['/', '/login', '/change-password', '/me/leave', '/employees', f'/employees/{uuid.uuid4()}',
         f'/payroll/runs/{uuid.uuid4()}', '/settings/devices', '/careers', '/careers/ACME', f'/careers/ACME/{uuid.uuid4()}',
         '/kiosk', '/hr/employees/123', '/some/address/react/redirects', '/group/companies']


@pytest.mark.parametrize('path', PAGES)
def test_page_addresses_answer_the_react_index(db, build, path):
    r = Client().get(path)
    assert r.status_code == 200, path
    assert r['Content-Type'] == 'text/html; charset=utf-8'
    assert body(r).decode() == INDEX
    assert r['Cache-Control'] == 'no-cache'
    assert r['Content-Security-Policy'] == TS_CSP
    assert r['X-Content-Type-Options'] == 'nosniff' and r['X-Frame-Options'] == 'DENY'


def test_signed_in_users_get_the_same_index(db, build):
    acme = make_company('ACME', 'Acme')
    c, _ = signed_in_client(acme, roles=['hr'])
    r = c.get('/payroll')
    assert r.status_code == 200 and body(r).decode() == INDEX


def test_index_follows_a_rebuild(db, build):
    assert Client().get('/').content.decode() == INDEX
    (build / 'index.html').write_text('<!doctype html><p>new release</p>')
    import os
    os.utime(build / 'index.html', (1, 1))     # a different mtime, as a rebuild gives
    assert Client().get('/leave').content.decode() == '<!doctype html><p>new release</p>'


def test_hashed_assets_are_cached_for_a_year(db, build):
    r = Client().get('/assets/index-AbCd1234.js')
    assert r.status_code == 200
    assert body(r) == b'console.log("hajiri")'
    assert 'immutable' in r['Cache-Control'] and 'max-age=315360000' in r['Cache-Control']
    css = Client().get('/assets/index-Xy_-9876.css')
    assert css.status_code == 200 and 'immutable' in css['Cache-Control']


def test_app_shell_files_are_revalidated(db, build):
    sw = Client().get('/sw.js')
    assert sw.status_code == 200 and sw['Cache-Control'] == 'no-cache' and sw['Service-Worker-Allowed'] == '/'
    assert 'javascript' in sw['Content-Type']
    m = Client().get('/manifest.webmanifest')
    assert m.status_code == 200 and m['Cache-Control'] == 'no-cache'
    icon = Client().get('/favicon.svg')
    assert icon.status_code == 200 and 'immutable' not in icon['Cache-Control']
    assert icon['Content-Type'].startswith('image/svg+xml')


def test_a_missing_asset_is_not_the_index(db, build):
    # Not a page: an HTML answer to a module script leaves the app blank ("Expected a JavaScript module script").
    r = Client().get('/assets/gone-12345678.js')
    assert r.status_code == 404 and not r['Content-Type'].startswith('text/html')


def test_assets_of_a_rebuild_on_a_running_server_load(db, build):
    """WhiteNoise lists the build when the server starts; index.html is read again after a rebuild, so the new hashed
    files it names are served from the folder too."""
    (build / 'assets' / 'LeavePage-Nw7r4Abc.js').write_text('export default 1')
    r = Client().get('/assets/LeavePage-Nw7r4Abc.js')
    assert r.status_code == 200 and body(r) == b'export default 1'
    assert 'javascript' in r['Content-Type'] and 'immutable' in r['Cache-Control']


def test_an_asset_deleted_by_a_rebuild_is_a_404(db, build):
    """A file WhiteNoise listed at startup that a rebuild then removed: a 404 (the tab reloads on vite:preloadError),
    not a 500 from the missing file, and never index.html."""
    c = Client()
    assert c.get('/assets/index-AbCd1234.js').status_code == 200
    (build / 'assets' / 'index-AbCd1234.js').unlink()
    for _ in range(2):
        r = c.get('/assets/index-AbCd1234.js')
        assert r.status_code == 404 and not r['Content-Type'].startswith('text/html')


def test_api_addresses_stay_json(db, build):
    r = Client().get('/api/v1/auth/me')
    assert r.status_code == 401 and r['Content-Type'].startswith('application/json')
    assert r.json() == {'error': 'unauthorized'} or 'error' in r.json()
    r = Client().get('/api/v1/no-such-endpoint')
    assert r.status_code == 404 and r.json() == {'error': 'not found'}
    r = Client().post('/api/v1/no/such', data='{}', content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'not found'}


def test_api_json_for_a_signed_in_user(db, build):
    acme = make_company('ACME', 'Acme')
    c, user = signed_in_client(acme, roles=['hr'])
    r = c.get('/api/v1/auth/me')
    assert r.status_code == 200 and r['Content-Type'].startswith('application/json')
    assert r.json()['user']['email'] == user.email


def test_health_is_json(db, build):
    r = Client().get('/health')
    assert r.status_code == 200 and r.json() == {'ok': True, 'db': 'up'}


def test_other_methods_on_a_page_address_are_the_ts_404(db, build):
    r = Client().post('/employees', data='{}', content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'not found'}


def test_without_a_build_pages_say_how_to_build(db, tmp_path):
    with override_settings(FRONTEND_BUILD_DIR=tmp_path / 'missing', WHITENOISE_ROOT=tmp_path / 'missing'):
        r = Client().get('/employees')
    assert r.status_code == 503 and b'scripts/build-frontend.sh' in r.content
    assert Client().get('/api/v1/no-such').json() == {'error': 'not found'}


def test_server_rendered_pages_are_gone(db, build):
    """The HTMX partial routes and the old page assets are not served any more."""
    assert Client().get('/_/web/me').content.decode() == INDEX        # just another address for React
    assert Client().get('/static/css/hajiri.css').status_code == 404
    assert Client().get('/static/vendor/htmx/htmx.min.js').status_code == 404
    assert 'django_htmx' not in settings.INSTALLED_APPS


def test_https_installs_add_upgrade_insecure_requests(db, build):
    with override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=True):
        r = Client().get('/', secure=True)
    assert r['Content-Security-Policy'] == TS_CSP + '; upgrade-insecure-requests'


# ---------------------------------------------------------------- the TS sameOriginGuard for cookie requests

def test_cookie_writes_from_another_site_are_refused(db, build):
    acme = make_company('ACME', 'Acme')
    c, _ = signed_in_client(acme, roles=['hr'])
    r = c.post('/api/v1/auth/logout', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    r = c.post('/api/v1/auth/logout', HTTP_ORIGIN='null')
    assert r.status_code == 403
    r = c.post('/api/v1/auth/logout', HTTP_REFERER='https://evil.example/page')
    assert r.status_code == 403


def test_cookie_writes_from_this_site_need_no_csrf_token(db, build):
    acme = make_company('ACME', 'Acme')
    c, _ = signed_in_client(acme, roles=['hr'])
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = c.cookies
    r = strict.post('/api/v1/auth/logout', HTTP_ORIGIN='http://testserver')
    assert r.status_code in (200, 204), r.content
    assert strict.get('/api/v1/auth/me').status_code == 401


def test_sign_in_needs_no_csrf_token(db, build):
    from tests.web.factories import PW, make_user
    acme = make_company('ACME', 'Acme')
    user = make_user(acme, roles=['hr'])
    strict = Client(enforce_csrf_checks=True)
    r = strict.post('/api/v1/auth/login', data=json.dumps({'email': user.email, 'password': PW}),
                    content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert r.status_code == 200, r.content
    assert strict.get('/api/v1/auth/me').status_code == 200


# ---------------------------------------------------------------- the committed build

REAL = Path(settings.BASE_DIR) / 'frontend_build'


@pytest.mark.skipif(not (REAL / 'index.html').exists(), reason='no frontend build (scripts/build-frontend.sh)')
def test_real_build_is_served(db):
    import re
    with override_settings(FRONTEND_BUILD_DIR=REAL, WHITENOISE_ROOT=REAL):
        c = Client()
        index = c.get(f'/employees/{uuid.uuid4()}')
        assert index.status_code == 200 and b'<div id="root"></div>' in index.content
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.content.decode())
        assert assets, 'index.html loads no hashed assets'
        for a in assets:
            r = c.get(a)
            assert r.status_code == 200, a
            assert 'immutable' in r['Cache-Control'], a
        assert c.get('/sw.js').status_code == 200
        assert c.get('/manifest.webmanifest').status_code == 200
        assert c.get('/icon-192.png').status_code == 200
