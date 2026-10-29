"""The JSON API at /api/v1/documents (the TS routes) for Bearer-token clients: shapes, status codes, errors."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core import auth
from tests.web.factories import PW, bearer, set_features_off

from .world import build, upload

pytestmark = pytest.mark.django_db


def token_of(user) -> str:
    return auth.login(user.email, PW).token


def call(user, method: str, url: str, data=None, client: Client | None = None):
    c = client or Client()
    if method == 'get':
        return c.get(url, data or {}, **bearer(token_of(user)))
    return getattr(c, method)(url, data or {}, content_type='application/json', **bearer(token_of(user)))


def test_employee_uploads_and_hr_verifies_through_the_api():
    w = build()
    cats = call(w.sita, 'get', '/api/v1/documents/categories').json()['categories']
    cat = next(c['id'] for c in cats if c['code'] == 'citizenship')
    r = call(w.sita, 'post', f'/api/v1/documents/employee/{w.sita_id}',
             {'categoryId': cat, 'title': 'Citizenship', 'fileId': upload(w.sita)})
    assert r.status_code == 201
    doc = r.json()['id']
    mine = call(w.sita, 'get', '/api/v1/documents/mine').json()['documents']
    assert [(d['id'], d['status'], d['version']) for d in mine] == [(doc, 'pending', 1)]
    q = call(w.hr, 'get', '/api/v1/documents/queue').json()
    assert doc in [d['id'] for d in q['pending']] and q['expiring'] == []
    assert call(w.sita, 'get', '/api/v1/documents/queue').json() == {'error': 'requires role hr'}
    assert call(w.hr, 'post', f'/api/v1/documents/{doc}/verify', {'decision': 'verify'}).json() == {'ok': True}
    r = call(w.hr, 'post', f'/api/v1/documents/{doc}/verify', {'decision': 'verify'})
    assert r.status_code == 409 and r.json() == {'error': 'this document is not waiting for verification'}
    f = call(w.sita, 'get', f'/api/v1/documents/{doc}/file')
    assert f.status_code == 200 and f['Content-Type'] == 'application/pdf'
    hist = call(w.hr, 'get', f'/api/v1/documents/employee/{w.sita_id}', {'history': '1'}).json()['documents']
    assert [d['id'] for d in hist] == [doc]
    assert call(w.sita, 'delete', f'/api/v1/documents/{doc}').json() == {'error': 'only HR can delete a verified document'}
    assert call(w.hr, 'patch', f'/api/v1/documents/{doc}', {'title': 'Citizenship (both sides)'}).json() == {'ok': True}
    assert call(w.hr, 'delete', f'/api/v1/documents/{doc}').json() == {'ok': True}


def test_validation_errors_have_the_ts_shape():
    w = build()
    r = call(w.hr, 'post', '/api/v1/documents/categories', {'code': 'Bad Code', 'name': ''})
    assert r.status_code == 400
    body = r.json()
    assert body['error'] == 'validation'
    issues = {i['path']: i['message'] for i in body['issues']}
    assert issues['code'] == 'lowercase letters, digits and _'
    assert issues['name'].startswith('Too small')
    r = call(w.hr, 'post', '/api/v1/documents/categories', {'code': 'work_permit', 'name': 'Work permit'})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    r = call(w.hr, 'put', '/api/v1/documents/letter-settings', {'signatoryName': None, 'signatoryTitle': None, 'refPrefix': 'a b'})
    assert r.status_code == 400 and r.json()['issues'][0] == {'path': 'refPrefix', 'message': 'letters, digits, / _ - only'}
    r = call(w.hr, 'post', '/api/v1/documents/letters', {'templateId': 'nope', 'employeeId': w.sita_id})
    assert r.status_code == 400 and {'path': 'templateId', 'message': 'Invalid UUID'} in r.json()['issues']


def test_letters_requests_and_settings_through_the_api():
    w = build()
    r = call(w.hr, 'put', '/api/v1/documents/letter-settings', {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager',
                                                                 'refPrefix': 'CV/HR'})
    assert r.json() == {'settings': {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager', 'refPrefix': 'CV/HR'}}
    assert call(w.hr, 'get', '/api/v1/documents/letter-settings').json()['settings']['refPrefix'] == 'CV/HR'
    assert {'key': 'employee.name', 'label': 'Employee full name'} in \
        call(w.hr, 'get', '/api/v1/documents/placeholders').json()['placeholders']
    tpls = call(w.sita, 'get', '/api/v1/documents/templates').json()['templates']
    salary = next(t['id'] for t in tpls if t['code'] == 'salary_certificate')
    assert 'body' not in tpls[0]
    r = call(w.sita, 'post', '/api/v1/documents/letter-requests', {'templateId': salary, 'purpose': 'bank loan'})
    assert r.status_code == 201
    req = r.json()['id']
    open_ = call(w.hr, 'get', '/api/v1/documents/letter-requests', {'status': 'open'}).json()['requests']
    assert [x['id'] for x in open_] == [req] and open_[0]['template_code'] == 'salary_certificate'
    p = call(w.hr, 'post', '/api/v1/documents/templates/preview', {'employeeId': w.sita_id, 'templateId': salary}).json()
    assert 'DRAFT' in p['html'] and 'salary.gross' in p['missing'] and p['unknown'] == []
    r = call(w.hr, 'post', '/api/v1/documents/letters', {'templateId': salary, 'employeeId': w.sita_id, 'requestId': req})
    assert r.status_code == 400 and r.json()['details']['code'] == 'missing_values'
    r = call(w.hr, 'post', '/api/v1/documents/letters', {'templateId': salary, 'employeeId': w.sita_id, 'requestId': req,
                                                         'allowMissing': True})
    assert r.status_code == 201 and r.json()['refNo'].startswith('CV/HR/')
    letter = r.json()['id']
    mine = call(w.sita, 'get', '/api/v1/documents/letters').json()['letters']
    assert [x['id'] for x in mine] == [letter]
    found = call(w.hr, 'get', '/api/v1/documents/letters', {'q': 'sita'}).json()['letters']
    assert [x['id'] for x in found] == [letter] and found[0]['name_en'] == 'Sita Sharma'
    assert call(w.hr, 'get', '/api/v1/documents/letters', {'q': 'nobody'}).json() == {'letters': []}
    html = call(w.sita, 'get', f'/api/v1/documents/letters/{letter}/html')
    assert html.status_code == 200 and html['Content-Type'].startswith('text/html') and 'bank loan' in html.content.decode()
    assert call(w.hr, 'post', f'/api/v1/documents/letters/{letter}/void', {'reason': 'wrong'}).json() == {'ok': True}
    assert call(w.sita, 'get', '/api/v1/documents/letters').json() == {'letters': []}
    assert call(w.sita, 'get', '/api/v1/documents/letter-requests').json()['requests'][0]['status'] == 'issued'


def test_policies_through_the_api():
    w = build()
    r = call(w.hr, 'post', '/api/v1/documents/policies', {'title': 'Employee handbook',
                                                          'fileId': upload(w.hr, 'company_document')})
    assert r.status_code == 201
    pid = r.json()['id']
    mine = call(w.sita, 'get', '/api/v1/documents/policies').json()['policies']
    assert [(p['id'], p['my_ack_at'], p['in_audience']) for p in mine] == [(pid, None, True)]
    assert call(w.sita, 'post', f'/api/v1/documents/policies/{pid}/acknowledge').json() == {'ok': True}
    assert call(w.sita, 'get', f'/api/v1/documents/policies/{pid}/file').status_code == 200
    acks = call(w.hr, 'get', f'/api/v1/documents/policies/{pid}/acks').json()['employees']
    assert next(e for e in acks if e['employee_id'] == w.sita_id)['acknowledged_at'] is not None
    hr_list = call(w.hr, 'get', '/api/v1/documents/policies').json()['policies']
    assert hr_list[0]['acks'] == 1 and hr_list[0]['audience_size'] == 4
    assert call(w.hr, 'post', f'/api/v1/documents/policies/{pid}/remind').json() == {'sent': 2}
    assert call(w.sita, 'post', f'/api/v1/documents/policies/{pid}/remind').json() == {'error': 'requires role hr'}
    assert call(w.hr, 'patch', f'/api/v1/documents/policies/{pid}', {'archived': True}).json() == {'ok': True}
    assert call(w.sita, 'get', '/api/v1/documents/policies').json() == {'policies': []}
    assert call(w.hr, 'get', '/api/v1/documents/policies', {'archived': '1'}).json()['policies'][0]['archived_at']


def test_feature_switch_signed_out_and_csrf():
    w = build()
    assert Client().get('/api/v1/documents/mine').status_code == 401
    # A cookie session (the React app) needs no CSRF token, but a cross-site Origin is refused (the TS
    # sameOriginGuard); a Bearer token needs neither.
    c = Client(enforce_csrf_checks=True)
    c.cookies[auth.SESSION_COOKIE] = token_of(w.hr)
    r = c.post('/api/v1/documents/categories', {'code': 'x_y', 'name': 'X'}, content_type='application/json',
               HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403
    r = c.post('/api/v1/documents/categories', {'code': 'x_v', 'name': 'X'}, content_type='application/json',
               HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201
    assert call(w.hr, 'post', '/api/v1/documents/categories', {'code': 'x_y', 'name': 'X'},
                client=Client(enforce_csrf_checks=True)).status_code == 201
    set_features_off(w.co, 'documents')
    r = call(w.sita, 'get', '/api/v1/documents/mine')
    assert r.status_code == 404 and r.json()['error'] == 'feature off'
