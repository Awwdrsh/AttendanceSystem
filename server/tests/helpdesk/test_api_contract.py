"""
The help desk JSON API against what the React pages (frontend/src/pages/helpdesk/*.jsx, api/helpdesk.js) send and
read, field by field, compared with the TS routes at commit 65a9079 (TICKET_SELECT = t.* plus the joined names).
Every request goes the way the browser sends it: the session cookie, Django's CSRF checks on, a same-site Origin
and no CSRF token.
"""
from __future__ import annotations

import json
import re

import pytest
from django.test import Client

from tests.support.sql import execute
from tests.web.factories import sign_in

from .world import build, upload

API = '/api/v1/helpdesk'
ISO = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')      # JSON.stringify(new Date())
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

# helpdesk_ticket's columns (t.*) plus the joins of TICKET_SELECT.
TICKET_KEYS = {
    'id', 'company_id', 'number', 'requester_user_id', 'employee_id', 'category_id', 'subject', 'priority', 'status',
    'assignee_user_id', 'sla_due_at', 'first_response_at', 'resolved_at', 'closed_at', 'breached_at', 'rating',
    'rating_comment', 'created_at', 'updated_at',
    'confidential', 'owner_user_id', 'category_name', 'requester_name', 'assignee_name', 'emp_code', 'employee_name',
}
DATES = ('sla_due_at', 'first_response_at', 'resolved_at', 'closed_at', 'breached_at', 'created_at', 'updated_at')


class Browser:
    """fetch() from the React app: cookie session, Origin of the page, JSON bodies, no CSRF token."""

    def __init__(self, user, origin='http://testserver'):
        self.c = Client(enforce_csrf_checks=True)
        sign_in(self.c, user)
        self.origin = origin

    def get(self, path, query=None):
        return self.c.get(f'{API}{path}', query or {})

    def send(self, method, path, body=None):
        kw = {'HTTP_ORIGIN': self.origin}
        if body is None:                       # api.post(path) with no body: no Content-Type, empty body
            return getattr(self.c, method)(f'{API}{path}', **kw)
        return getattr(self.c, method)(f'{API}{path}', json.dumps(body), content_type='application/json', **kw)


@pytest.fixture
def w(db):
    w = build()
    w.b = {k: Browser(getattr(w, k)) for k in ('hr', 'sita', 'ram', 'it_', 'other_hr')}
    return w


def _ticket_types(t):
    assert UUID.match(t['id']) and UUID.match(t['company_id']) and UUID.match(t['category_id'])
    assert isinstance(t['number'], int) and isinstance(t['confidential'], bool)
    for k in DATES:
        assert t[k] is None or ISO.match(t[k]), (k, t[k])
    assert t['rating'] is None or isinstance(t['rating'], int)


def test_cookie_session_writes_need_no_csrf_token_but_a_same_site_origin(w):
    sita, hr = w.b['sita'], w.b['hr']
    r = sita.send('post', '/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'Hello there', 'body': 'x'})
    assert r.status_code == 201, r.content
    tid = r.json()['id']
    assert hr.send('patch', f'/tickets/{tid}', {'priority': 'high'}).status_code == 200
    assert hr.send('put', '/settings', {'slaHours': {'low': 72, 'normal': 48, 'high': 24, 'urgent': 8},
                                        'autoCloseDays': 7, 'reopenDays': 7}).status_code == 200
    assert sita.send('post', f'/tickets/{tid}/close').status_code == 200
    # Another site's page cannot use the cookie.
    evil = Browser(w.sita, origin='https://evil.example')
    r = evil.send('post', '/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'Forged one', 'body': 'x'})
    assert r.status_code == 403 and 'error' in r.json()


def test_every_page_call_field_by_field(w):
    hr, sita, it_ = w.b['hr'], w.b['sita'], w.b['it_']

    # ---- Setup tab: policy round trip (savePolicy(p) sends back exactly what GET returned).
    pol = hr.get('/settings').json()
    assert pol == {'policy': {'slaHours': {'low': 72, 'normal': 48, 'high': 24, 'urgent': 8}, 'autoCloseDays': 7, 'reopenDays': 7}}
    assert hr.send('put', '/settings', pol['policy']).json() == pol

    # ---- Topics: a new topic as the modal sends it (description may be '' or null, owner null, sort 0).
    body = {'name': 'Travel', 'description': '', 'confidential': False, 'ownerUserId': None, 'sort': 0, 'active': True}
    r = hr.send('post', '/categories', body)
    assert r.status_code == 201 and set(r.json()) == {'id'} and UUID.match(r.json()['id'])
    cats = hr.get('/categories').json()
    assert set(cats) == {'categories'}
    c = next(x for x in cats['categories'] if x['name'] == 'Travel')
    assert c == {'id': r.json()['id'], 'name': 'Travel', 'description': '', 'confidential': False, 'sort': 0,
                 'active': True, 'owner_user_id': None, 'owner_name': None}
    # Editing a row: the page builds the body from the row's own fields.
    edit = {**c, 'owner_user_id': w.it_.id, 'description': None}
    body = {'name': edit['name'].strip(), 'description': edit['description'], 'confidential': bool(edit['confidential']),
            'ownerUserId': edit['owner_user_id'], 'sort': edit['sort'], 'active': edit['active'] is not False}
    assert hr.send('put', f"/categories/{c['id']}", body).json() == {'ok': True}
    c = next(x for x in hr.get('/categories').json()['categories'] if x['id'] == c['id'])
    assert c['owner_user_id'] == w.it_.id and c['owner_name'] == w.it_.name and c['description'] is None
    # The employee's topic picker: active topics, owners hidden.
    mine_cats = sita.get('/categories').json()['categories']
    assert all(x['active'] is True and x['owner_user_id'] is None and x['owner_name'] is None for x in mine_cats)
    assert [x['sort'] for x in mine_cats] == sorted(x['sort'] for x in mine_cats)

    # ---- Agents (assignee pickers): {id, name, email, roles, is_hr}.
    agents = hr.get('/agents').json()['agents']
    a = next(x for x in agents if x['id'] == w.hr.id)
    assert a == {'id': w.hr.id, 'name': w.hr.name, 'email': w.hr.email, 'roles': ['hr'], 'is_hr': True}
    assert [x['name'] for x in agents] == sorted(x['name'] for x in agents)

    # ---- NewTicket: create as the modal sends it; r.number and r.id are read.
    f1 = upload(w.sita)
    r = sita.send('post', '/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'SSF missing', 'body': 'No SSF line.',
                                       'priority': 'normal', 'fileIds': [f1]})
    created = r.json()
    assert r.status_code == 201 and set(created) == {'id', 'number', 'slaDueAt'}
    assert isinstance(created['number'], int) and ISO.match(created['slaDueAt'])
    tid = created['id']

    # ---- MyTicketsPage: {tickets}, t.* without owner_user_id.
    mine = sita.get('/tickets/mine').json()
    assert set(mine) == {'tickets'}
    t = mine['tickets'][0]
    assert set(t) == TICKET_KEYS - {'owner_user_id'}
    _ticket_types(t)
    assert t['id'] == tid and t['category_name'] == 'Payroll & tax' and t['requester_name'] == w.sita.name
    assert t['status'] == 'open' and t['priority'] == 'normal' and t['assignee_user_id'] is None and t['assignee_name'] is None
    assert t['employee_name'] == 'Sita' and t['emp_code'] and t['employee_id']
    assert t['sla_due_at'] == created['slaDueAt']

    # ---- HR queue: every key, as HelpdeskPage reads it, with the exact query the page sends.
    q = hr.get('/tickets', {'status': 'active'}).json()
    assert set(q) == {'tickets'}
    t = q['tickets'][0]
    assert set(t) == TICKET_KEYS
    _ticket_types(t)
    assert t['owner_user_id'] is None and t['confidential'] is False

    # ---- TicketThread as HR: reply (internal false, fileIds []), internal note with a file, assign, unassign.
    r = hr.send('post', f'/tickets/{tid}/messages', {'body': 'Looking into it', 'internal': False, 'fileIds': []})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    f2 = upload(w.hr)
    assert hr.send('post', f'/tickets/{tid}/messages', {'body': 'note', 'internal': True, 'fileIds': [f2]}).status_code == 201
    assert hr.send('patch', f'/tickets/{tid}', {'assigneeUserId': w.it_.id}).json() == {'ok': True}
    assert hr.send('patch', f'/tickets/{tid}', {'assigneeUserId': None}).json() == {'ok': True}
    assert hr.send('patch', f'/tickets/{tid}', {'assigneeUserId': w.it_.id}).json() == {'ok': True}
    d = hr.get(f'/tickets/{tid}').json()
    assert set(d) == {'ticket', 'role', 'messages', 'files', 'canReopen'}
    assert d['role'] == 'agent' and d['canReopen'] is False
    assert set(d['ticket']) == TICKET_KEYS | {'overdue'}
    _ticket_types(d['ticket'])
    assert d['ticket']['overdue'] is False and d['ticket']['assignee_user_id'] == w.it_.id
    assert d['ticket']['assignee_name'] == w.it_.name and d['ticket']['status'] == 'in_progress'
    assert len(d['messages']) == 3
    for m in d['messages']:
        assert set(m) == {'id', 'body', 'internal', 'file_ids', 'created_at', 'author_user_id', 'author_name', 'mine'}
        assert ISO.match(m['created_at']) and isinstance(m['internal'], bool) and isinstance(m['mine'], bool)
        assert all(isinstance(x, str) and UUID.match(x) for x in m['file_ids'])
    assert [m['body'] for m in d['messages']] == ['No SSF line.', 'Looking into it', 'note']
    assert [m['mine'] for m in d['messages']] == [False, True, True]
    assert d['messages'][0]['file_ids'] == [f1] and d['messages'][0]['author_name'] == w.sita.name
    assert sorted(f['id'] for f in d['files']) == sorted([f1, f2])
    for f in d['files']:
        assert set(f) == {'id', 'filename', 'mime', 'bytes'} and f['mime'] == 'application/pdf' and isinstance(f['bytes'], int)
    # The attachment link (an <a href> the browser opens with Accept: text/html).
    dl = hr.c.get(f'{API}/tickets/{tid}/files/{f1}', HTTP_ACCEPT='text/html,application/xhtml+xml,*/*;q=0.8')
    assert dl.status_code == 200 and dl['Content-Type'].startswith('application/pdf')

    # ---- The assignee (not HR) works the ticket and resolves it.
    assert it_.send('patch', f'/tickets/{tid}', {'status': 'resolved'}).json() == {'ok': True}

    # ---- TicketThread as the requester: no internal note, canReopen, rate without a comment, then reopen.
    d = sita.get(f'/tickets/{tid}').json()
    assert d['role'] == 'requester' and d['canReopen'] is True
    assert set(d['ticket']) == (TICKET_KEYS - {'owner_user_id'}) | {'overdue'}
    assert d['ticket']['status'] == 'resolved' and ISO.match(d['ticket']['resolved_at']) and d['ticket']['rating'] is None
    assert [m['body'] for m in d['messages']] == ['No SSF line.', 'Looking into it']
    assert [f['id'] for f in d['files']] == [f1]
    # helpdesk.rate(id, rating, undefined): JSON.stringify drops the comment.
    assert sita.send('post', f'/tickets/{tid}/rate', {'rating': 2}).json() == {'ok': True}
    t = sita.get(f'/tickets/{tid}').json()['ticket']
    assert t['rating'] == 2 and t['rating_comment'] is None
    # helpdesk.reopen(id) / close(id) post with no body at all.
    r = sita.send('post', f'/tickets/{tid}/reopen')
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert sita.get(f'/tickets/{tid}').json()['ticket']['status'] == 'in_progress'

    # ---- Stats strip: exact keys and types (counts as numbers, averages as numeric text or null).
    hr.send('patch', f'/tickets/{tid}', {'status': 'resolved'})
    execute("UPDATE helpdesk_ticket SET created_at = resolved_at - interval '90 minutes', "
            "first_response_at = resolved_at - interval '30 minutes', rating = 4 WHERE id = %s", [tid])
    s = hr.get('/stats').json()
    assert set(s) == {'stats', 'byCategory'}
    assert set(s['stats']) == {'open', 'unassigned', 'overdue', 'breached_90d', 'resolved_30d', 'avg_resolution_hours',
                               'avg_first_response_hours', 'avg_rating', 'ratings'}
    for k in ('open', 'unassigned', 'overdue', 'breached_90d', 'resolved_30d', 'ratings'):
        assert isinstance(s['stats'][k], int)
    assert s['stats']['avg_resolution_hours'] == '1.5' and s['stats']['avg_first_response_hours'] == '1.0'
    assert s['stats']['avg_rating'] == '4.00' and s['stats']['ratings'] == 1 and s['stats']['resolved_30d'] == 1
    pay = next(x for x in s['byCategory'] if x['name'] == 'Payroll & tax')
    assert pay == {'name': 'Payroll & tax', 'open': 0, 'total': 1}


def test_stats_with_no_tickets_are_null_averages(w):
    s = w.b['hr'].get('/stats').json()['stats']
    assert s == {'open': 0, 'unassigned': 0, 'overdue': 0, 'breached_90d': 0, 'resolved_30d': 0,
                 'avg_resolution_hours': None, 'avg_first_response_hours': None, 'avg_rating': None, 'ratings': 0}


def test_queue_query_exactly_as_the_page_builds_it(w):
    hr, sita = w.b['hr'], w.b['sita']
    a = sita.send('post', '/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'Late one', 'body': 'x', 'priority': 'urgent'}).json()
    sita.send('post', '/tickets', {'categoryId': w.cat('Leave'), 'subject': 'On time', 'body': 'x'})
    execute("UPDATE helpdesk_ticket SET sla_due_at = now() - interval '1 hour' WHERE id = %s", [a['id']])
    # {status, breached: '1'} with the switch on; agents' ids in the assignee picker.
    q = hr.get('/tickets', {'status': 'active', 'breached': '1'}).json()['tickets']
    assert [t['id'] for t in q] == [a['id']]
    hr.send('patch', f"/tickets/{a['id']}", {'assigneeUserId': w.it_.id})
    q = hr.get('/tickets', {'status': 'active', 'assignee': w.it_.id}).json()['tickets']
    assert [t['id'] for t in q] == [a['id']] and q[0]['assignee_name'] == w.it_.name
    # Overdue first in the default order.
    assert hr.get('/tickets', {'status': 'active'}).json()['tickets'][0]['id'] == a['id']
    assert hr.get(f"/tickets/{a['id']}").json()['ticket']['overdue'] is True


def test_validation_errors_have_the_ts_issue_shape(w):
    r = w.b['sita'].send('post', '/tickets', {'categoryId': 'nope', 'subject': 'ab', 'body': ' '})
    assert r.status_code == 400
    j = r.json()
    assert j['error'] and {i['path'] for i in j['issues']} == {'categoryId', 'subject', 'body'}
    assert all(set(i) >= {'path', 'message'} for i in j['issues'])
    r = w.b['sita'].send('post', '/tickets/00000000-0000-0000-0000-000000000000/rate', {'rating': 9})
    assert r.status_code == 404 and r.json() == {'error': 'ticket not found'}
