"""The platform endpoints the way the React app (frontend/src/api/*.js) calls them: a cookie session from POST
/auth/login, writes with the browser's Origin header and no CSRF token, and the Settings page's habit of saving back
the value it loaded. Checked against the TS routes at 65a9079."""
import json

import pytest
from django.test import Client

from hajiri.core import auth
from tests.core.fixtures import PW, bearer

SELF = 'http://testserver'


@pytest.fixture
def browser(demo):
    """A signed-in HR admin's browser: the cookie set by POST /api/v1/auth/login, nothing else."""
    auth.create_user(company_id=demo['company_id'], email='hr.react@test.local', name='HR React', password=PW,
                     roles=['admin', 'hr'])
    c = Client()
    r = c.post('/api/v1/auth/login', json.dumps({'email': 'hr.react@test.local', 'password': PW}),
               content_type='application/json', HTTP_ORIGIN=SELF)
    assert r.status_code == 200 and set(r.json()) == {'user', 'token'}
    assert r.cookies[auth.SESSION_COOKIE]['httponly'] and r.cookies[auth.SESSION_COOKIE]['samesite'] == 'Lax'
    return c


def send(c, method, path, body=None, origin=SELF, **extra):
    return getattr(c, method)(f'/api/v1{path}', json.dumps(body if body is not None else {}),
                              content_type='application/json', HTTP_ORIGIN=origin, **extra)


def test_cookie_writes_from_the_app_origin_work_without_a_csrf_token(browser, demo):
    assert browser.get('/api/v1/auth/me').json()['user']['email'] == 'hr.react@test.local'
    assert send(browser, 'put', '/settings/leave.policy', {'sandwichRule': True}).status_code == 200
    assert send(browser, 'put', '/settings/features', {'enabled': {'tasks': False}}).json()['enabled']['tasks'] is False
    assert send(browser, 'post', '/notifications/read-all').json() == {'marked': 0}
    assert send(browser, 'post', '/settings/branches', {'code': 'B9', 'nameEn': 'Nine'}).status_code == 201
    assert send(browser, 'post', '/reports/adhoc/saved',
                {'name': 'Mine', 'definition': {'dataset': 'employees', 'columns': ['emp_code']}}).status_code == 201
    r = send(browser, 'post', '/attendance/recompute', {'employeeId': demo['sita'], 'localDate': '2026-08-17'})
    assert r.status_code == 200 and set(r.json()) == {'day', 'version', 'changed', 'locked'}
    up = send(browser, 'post', '/files', {'purpose': 'employee_document', 'filename': 'a.pdf',
                                          'dataUrl': 'data:application/pdf;base64,JVBERi0xLjQKJcfsj6IKMSAwIG9iago8PD4+CmVuZG9iagp0cmFpbGVyCjw8Pj4KJSVFT0YK'})
    assert up.status_code == 201 and set(up.json()['file']) == {'id', 'filename', 'mime', 'bytes', 'purpose'}
    assert send(browser, 'post', '/auth/logout').json() == {'ok': True}
    assert browser.get('/api/v1/auth/me').status_code == 401


def test_cookie_writes_from_another_origin_are_refused_but_bearer_clients_are_not(browser, demo):
    for method, path, body in (('put', '/settings/leave.policy', {}), ('post', '/notifications/read-all', None),
                               ('put', '/settings/features', {'enabled': {}}), ('post', '/auth/logout', None),
                               ('post', '/attendance/recompute', {'employeeId': demo['sita'], 'localDate': '2026-08-17'})):
        r = send(browser, method, path, body, origin='http://localhost:5174')
        assert (r.status_code, r.json()) == (403, {'error': 'cross-site request refused'}), path
    # Reads pass whatever the Origin; a Bearer token is never refused for its Origin.
    assert browser.get('/api/v1/settings', HTTP_ORIGIN='http://evil.example').status_code == 200
    token = auth.login('hr.react@test.local', PW).token
    assert Client().put('/api/v1/settings/leave.policy', '{}', content_type='application/json',
                        HTTP_ORIGIN='http://evil.example', **bearer(token)).status_code == 200


def test_every_setting_saves_back_the_value_the_settings_page_loaded(browser):
    """SettingsPage loads GET /settings and PUTs a value back (empty strings dropped, nulls kept): every key's
    default and stored value must be accepted unchanged, as the zod schemas accepted their own output."""
    send(browser, 'put', '/settings/company.profile', {'nameEn': 'Acme', 'nameNe': None, 'address': None, 'pan': None,
                                                        'ssfEmployerId': None, 'logoDataUrl': None})
    for s in browser.get('/api/v1/settings').json()['settings']:
        body = {k: v for k, v in (s['value'] or {}).items() if v != ''}
        r = send(browser, 'put', f"/settings/{s['key']}", body)
        assert r.status_code == 200, (s['key'], r.json())
        assert r.json()['key'] == s['key']


def test_a_regex_mismatch_is_reported_as_an_invalid_string_not_a_type_error(browser):
    r = send(browser, 'put', '/settings/company.profile', {'nameEn': 'Acme', 'pan': '12'})
    assert r.status_code == 400
    assert r.json() == {'error': 'validation', 'issues': [{'path': 'pan', 'message': 'Invalid string'}]}
    r = send(browser, 'put', '/settings/company.profile', {'nameEn': 'Acme', 'logoDataUrl': 'data:text/html;base64,AA=='})
    assert r.json()['issues'] == [{'path': 'logoDataUrl', 'message': 'Invalid string'}]
    assert send(browser, 'put', '/settings/company.profile', {'nameEn': 'Acme', 'pan': '123456789'}).status_code == 200


def test_another_companys_branch_and_org_unit_are_not_found(browser):
    """Ids of another company behave as not found (the TS company_id filter), for writes and as references."""
    from hajiri.db.models import Branch, Company, OrgUnit
    other = Company.objects.create(code='OTHERX', name_en='Other X')
    br = Branch.objects.create(company=other, code='OB', name_en='Other branch')
    unit = OrgUnit.objects.create(company=other, kind='department', code='OD', name_en='Other dept')
    r = send(browser, 'patch', f'/settings/branches/{br.pk}', {'nameEn': 'Hijack'})
    assert (r.status_code, r.json()) == (404, {'error': 'branch not found'})
    r = send(browser, 'patch', f'/settings/org-units/{unit.pk}', {'nameEn': 'Hijack'})
    assert (r.status_code, r.json()) == (404, {'error': 'org unit not found'})
    r = send(browser, 'post', '/settings/org-units', {'kind': 'team', 'code': 'T1', 'nameEn': 'T', 'parentId': str(unit.pk)})
    assert (r.status_code, r.json()) == (404, {'error': 'parent unit not found'})
    assert all(b['code'] != 'OB' for b in browser.get('/api/v1/settings/branches').json()['branches'])
    Branch.objects.filter(pk=br.pk).update(name_en='Other branch')
    assert Branch.objects.get(pk=br.pk).name_en == 'Other branch'
