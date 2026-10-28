"""The help desk JSON API (DRF) for Bearer-token clients: same rules and messages as the pages."""
import json

import pytest
from django.test import Client

from hajiri.core import auth
from tests.web.factories import PW, bearer, set_features_off

from .world import build, raise_tickets


@pytest.fixture
def w(db):
    return build()


def _c(user):
    return Client(), bearer(auth.login(user.email, PW).token)


def _post(c, h, url, body=None):
    return c.post(url, json.dumps(body or {}), content_type='application/json', **h)


def test_raise_list_and_read_a_ticket(w):
    c, h = _c(w.sita)
    cats = c.get('/api/v1/helpdesk/categories', **h).json()
    assert cats and cats[0]['owner_user_id'] is None
    r = _post(c, h, '/api/v1/helpdesk/tickets', {'categoryId': cats[0]['id'], 'subject': 'Where is my slip', 'body': 'Hi'})
    assert r.status_code == 201 and r.json()['number'] == 1 and r.json()['slaDueAt'].endswith('Z')
    tid = r.json()['id']
    assert [t['id'] for t in c.get('/api/v1/helpdesk/tickets/mine', **h).json()] == [tid]
    d = c.get(f'/api/v1/helpdesk/tickets/{tid}', **h).json()
    assert d['role'] == 'requester' and d['messages'][0]['body'] == 'Hi'
    r = _post(c, h, '/api/v1/helpdesk/tickets', {'categoryId': cats[0]['id'], 'subject': 'x', 'body': ''})
    assert r.status_code == 400 and {i['path'] for i in r.json()['issues']} == {'subject', 'body'}


def test_queue_is_paginated_and_hr_only_actions(w):
    raise_tickets(w)
    hr, hh = _c(w.hr)
    page = hr.get('/api/v1/helpdesk/tickets?pageSize=1', **hh).json()
    assert page['total'] == 2 and page['pageSize'] == 1 and len(page['items']) == 1
    assert page['items'][0]['priority'] == 'high'
    assert hr.get('/api/v1/helpdesk/tickets?status=nope', **hh).status_code == 400
    assert hr.get('/api/v1/helpdesk/stats', **hh).json()['stats']['open'] == 2
    sita, sh = _c(w.sita)
    assert sita.get('/api/v1/helpdesk/stats', **sh).json() == {'error': 'requires role hr'}
    tid = w.ticket['id']
    r = sita.patch(f'/api/v1/helpdesk/tickets/{tid}', json.dumps({'status': 'resolved'}), content_type='application/json', **sh)
    assert r.status_code == 403 and r.json() == {'error': 'only the help desk team can change a ticket'}
    r = hr.patch(f'/api/v1/helpdesk/tickets/{tid}', json.dumps({'status': 'resolved'}), content_type='application/json', **hh)
    assert r.json() == {'ok': True}
    assert _post(sita, sh, f'/api/v1/helpdesk/tickets/{tid}/rate', {'rating': 5}).json() == {'ok': True}
    # Another company's HR does not find it.
    other, oh = _c(w.other_hr)
    assert other.get(f'/api/v1/helpdesk/tickets/{tid}', **oh).json() == {'error': 'ticket not found'}


def test_feature_off(w):
    set_features_off(w.co, 'helpdesk')
    c, h = _c(w.sita)
    assert c.get('/api/v1/helpdesk/tickets/mine', **h).json() == {'error': 'feature off', 'feature': 'helpdesk'}
