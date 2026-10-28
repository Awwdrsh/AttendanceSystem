"""The JSON inbox (TS /api/v1/notifications) for Bearer clients."""
import json

from django.test import Client

from hajiri.notifications.service import notify
from tests.core.fixtures import bearer


def test_inbox_read_and_read_all(demo, login_as):
    me = login_as(['employee'], demo['sita'])
    other = login_as(['employee'])
    for t in ('A', 'B', 'C'):
        notify(company_id=demo['company_id'], user_id=me.user_id, template='x', title=t, body=t)
    c = Client()
    r = c.get('/api/v1/notifications?limit=2', **bearer(me.token))
    assert r.status_code == 200
    body = r.json()
    assert [n['title'] for n in body['notifications']] == ['C', 'B'] and body['unread'] == 3
    first = body['notifications'][0]['id']
    assert c.get('/api/v1/notifications?unreadOnly=true&limit=500', **bearer(me.token)).json()['error'] == 'validation'
    read = c.post(f'/api/v1/notifications/{first}/read', **bearer(me.token))
    assert read.status_code == 200 and read.json()['id'] == first and read.json()['read_at'].endswith('Z')
    assert c.post(f'/api/v1/notifications/{first}/read', **bearer(other.token)).json() == {'error': 'notification not found'}
    assert c.post('/api/v1/notifications/not-a-uuid/read', **bearer(me.token)).status_code == 400
    assert c.post('/api/v1/notifications/read-all', **bearer(me.token)).json() == {'marked': 2}
    assert c.get('/api/v1/notifications', **bearer(me.token)).json()['unread'] == 0
    assert c.get('/api/v1/notifications').status_code == 401
