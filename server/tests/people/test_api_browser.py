"""
The people, employees and lifecycle endpoints as the React app calls them from the browser: the session cookie,
fetch() with the page's own Origin and no CSRF token (frontend/src/api/client.js). A write from the site's own origin
works, one from another origin is refused, as the TS sameOriginGuard did. The lifecycle module answers 404
"feature off" when the company switched it off (TS featureGate), before any role check.
"""
import json

from django.test import Client

from tests.web.factories import set_features_off, sign_in

ORIGIN = 'http://testserver'


def _browser(user, origin: str = ORIGIN) -> tuple[Client, dict]:
    c = Client(enforce_csrf_checks=True)      # like a browser: Django's CSRF check is not switched off
    sign_in(c, user)
    return c, {'HTTP_ORIGIN': origin}


def _send(c, method, path, body, headers):
    return getattr(c, method)(f'/api/v1{path}', json.dumps(body), content_type='application/json', **headers)


def test_cookie_writes_from_the_app_origin_work(world):
    w = world
    c, h = _browser(w.hr)
    r = _send(c, 'post', '/people/structure/branches', {'code': 'BRT', 'nameEn': 'Biratnagar'}, h)
    assert r.status_code == 201, r.json()
    r = _send(c, 'put', '/people/policy', {'makerChecker': True}, h)
    assert r.status_code == 200 and r.json() == {'policy': {'makerChecker': True}}
    r = _send(c, 'patch', f'/employees/{w.sita_id}', {'phone': '9800000001'}, h)
    assert r.status_code == 200 and r.json()['employee']['phone'] == '9800000001'
    r = _send(c, 'post', '/employees', {'empCode': 'BR1', 'nameEn': 'Browser One'}, h)
    assert r.status_code == 201 and set(r.json()) == {'id'}
    r = _send(c, 'post', '/lifecycle/asset-types', {'name': 'Tablet'}, h)
    assert r.status_code == 201 and r.json()['type']['name'] == 'Tablet'
    r = _send(c, 'put', '/lifecycle/settings', {**c.get('/api/v1/lifecycle/settings').json()['policy'], 'noticeDays': 40}, h)
    assert r.status_code == 200 and r.json()['policy']['noticeDays'] == 40
    # The employee's own change request, from the browser.
    e, eh = _browser(w.sita)
    r = _send(e, 'post', '/people/me/change-requests', {'section': 'contact', 'value': {'personalEmail': 'sita@home.np'}}, eh)
    assert r.status_code == 201 and r.json()['request']['sectionLabel'] == 'Contact and address'
    # A DELETE with no body, as client.js del() sends it.
    br = c.get('/api/v1/people/structure').json()['branches']
    bid = next(b['id'] for b in br if b['code'] == 'BRT')
    r = c.delete(f'/api/v1/people/structure/branches/{bid}', **h)
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_cookie_writes_from_another_origin_are_refused(world):
    w = world
    c, h = _browser(w.hr, 'https://evil.example')
    for method, path, body in (('post', '/people/structure/branches', {'code': 'EV', 'nameEn': 'Evil'}),
                               ('patch', f'/employees/{w.sita_id}', {'phone': '1'}),
                               ('post', '/lifecycle/asset-types', {'name': 'Evil'})):
        r = _send(c, method, path, body, h)
        assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}, path
    # Reads always pass.
    assert c.get('/api/v1/people/structure', **h).status_code == 200


def test_lifecycle_feature_off(world):
    w = world
    set_features_off(w.company, 'lifecycle')
    c, h = _browser(w.hr)
    for path in ('/lifecycle/settings', '/lifecycle/cases', '/lifecycle/my-tasks', '/lifecycle/assets/mine'):
        r = c.get(f'/api/v1{path}')
        assert r.status_code == 404 and r.json() == {'error': 'feature off', 'feature': 'lifecycle'}, path
    e, _ = _browser(w.sita)
    assert e.get('/api/v1/lifecycle/templates').json() == {'error': 'feature off', 'feature': 'lifecycle'}   # before the role check
    r = _send(c, 'post', '/lifecycle/asset-types', {'name': 'Tablet'}, h)
    assert r.status_code == 404 and r.json()['error'] == 'feature off'
    # Signed out is still 401, and another company with the module on is unaffected.
    assert Client().get('/api/v1/lifecycle/settings').status_code == 401
    f, _ = _browser(w.foreign_hr)
    assert f.get('/api/v1/lifecycle/settings').status_code == 200
    # People and employees are not part of the switch.
    assert c.get('/api/v1/people/directory').status_code == 200 and c.get('/api/v1/employees').status_code == 200


def test_bigint_ids_are_strings_as_node_postgres_sent_them(world):
    w = world
    c, h = _browser(w.hr)
    assert _send(c, 'patch', f'/employees/{w.sita_id}', {'phone': '9800000002'}, h).status_code == 200
    hist = c.get(f'/api/v1/people/{w.sita_id}/history').json()['history']
    assert hist and all(isinstance(x['id'], str) and x['id'].isdigit() for x in hist)
    events = c.get(f'/api/v1/employees/{w.sita_id}/audit').json()['events']
    assert events and all(isinstance(x['id'], str) and x['id'].isdigit() for x in events)
