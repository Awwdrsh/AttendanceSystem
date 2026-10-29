"""/api/v1/settings/features and /api/v1/notifications over the cookie session the React app uses (no CSRF token, Origin
checked as in the TS sameOriginGuard), with the full response shape and company isolation."""
import json

from django.test import Client

from hajiri.db.models import Company
from hajiri.notifications.service import notify
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import bearer, make_user, sign_in

SELF = {'HTTP_ORIGIN': 'http://testserver'}


def _client(demo, roles):
    c = Client(enforce_csrf_checks=True)          # like a browser fetch: no CSRF token is ever sent
    user = make_user(Company.objects.get(pk=demo['company_id']), roles=roles)
    sign_in(c, user)
    return c, user


def test_features_read_and_change_with_a_cookie_session(demo):
    c, _ = _client(demo, ['hr'])
    r = c.get('/api/v1/settings/features')
    assert r.status_code == 200
    assert {'features', 'enabled'} == set(r.json())
    assert {'key', 'enabled'} <= set(r.json()['features'][0])
    body = json.dumps({'enabled': {'tasks': False}})
    ok = c.put('/api/v1/settings/features', body, content_type='application/json', **SELF)
    assert ok.status_code == 200 and ok.json()['enabled']['tasks'] is False
    evil = c.put('/api/v1/settings/features', body, content_type='application/json', HTTP_ORIGIN='http://evil.example')
    assert evil.status_code == 403 and evil.json() == {'error': 'cross-site request refused'}
    bad = c.put('/api/v1/settings/features', json.dumps({'enabled': {'time': 'no'}}), content_type='application/json', **SELF)
    assert bad.status_code == 400
    emp, _ = _client(demo, ['employee'])
    assert emp.put('/api/v1/settings/features', body, content_type='application/json', **SELF).json() == {
        'error': 'requires role hr or admin'}


def test_notifications_with_a_cookie_session(demo):
    c, user = _client(demo, ['employee'])
    notify(company_id=demo['company_id'], user_id=user.id, template='leave.decided', title='Leave approved', body='ok')
    r = c.get('/api/v1/notifications')
    assert r.status_code == 200 and r.json()['unread'] == 1
    n = r.json()['notifications'][0]
    assert set(n) == {'id', 'template', 'title', 'body', 'payload', 'created_at', 'read_at'}
    read = c.post(f"/api/v1/notifications/{n['id']}/read", '{}', content_type='application/json', **SELF)
    assert read.status_code == 200 and read.json()['read_at'].endswith('Z')
    assert c.post('/api/v1/notifications/read-all', '{}', content_type='application/json',
                  HTTP_ORIGIN='http://evil.example').status_code == 403
    assert c.post('/api/v1/notifications/read-all', '{}', content_type='application/json', **SELF).json() == {'marked': 0}


def test_feature_switches_are_per_company(world):
    beta, _ = world.session(world.admin_b)
    alpha, _ = world.session(world.admin_a)
    r = Client().put('/api/v1/settings/features', json.dumps({'enabled': {'tasks': False}}), content_type='application/json',
                     **bearer(beta))
    assert r.status_code == 200
    assert Client().get('/api/v1/settings/features', **bearer(beta)).json()['enabled']['tasks'] is False
    assert Client().get('/api/v1/settings/features', **bearer(alpha)).json()['enabled']['tasks'] is True
