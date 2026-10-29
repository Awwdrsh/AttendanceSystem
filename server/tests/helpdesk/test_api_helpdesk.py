"""
The help desk JSON API (/api/v1/helpdesk/...) the React app calls: a port of the HTTP assertions of
apps/api/test/helpdesk.test.ts (commit 65a9079), plus the response shapes the pages read, the error bodies,
the feature switch, the cookie session and company isolation.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.clock import now
from hajiri.helpdesk import services as hd
from tests.support.sql import execute, one, rows
from tests.web.factories import PW, set_features_off, sign_in

from .world import build, upload

API = '/api/v1/helpdesk'


class Api:
    """Bearer-token requests as one user."""

    def __init__(self, user):
        self.c = Client()
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {auth.login(user.email, PW).token}'}

    def get(self, path, query=None):
        return self.c.get(f'{API}{path}', query or {}, **self.h)

    def _send(self, method, path, body):
        return getattr(self.c, method)(f'{API}{path}', json.dumps(body if body is not None else {}),
                                       content_type='application/json', **self.h)

    def post(self, path, body=None):
        return self._send('post', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)


@pytest.fixture
def w(db):
    w = build()
    w.api = {k: Api(getattr(w, k)) for k in ('hr', 'sita', 'ram', 'it_', 'other_hr')}
    return w


def ids(r) -> list[str]:
    return [t['id'] for t in r.json()['tickets']]


def test_the_ts_suite_end_to_end(w):
    hr, sita, ram, it_, other = (w.api[k] for k in ('hr', 'sita', 'ram', 'it_', 'other_hr'))

    # beforeAll: HR adds a confidential topic over HTTP; Sita lists the topics.
    r = hr.post('/categories', {'name': 'Secret HR matter', 'confidential': True, 'sort': 10})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    cats = sita.get('/categories').json()['categories']

    # seeds categories without a grievance topic, owners hidden from employees
    assert len(cats) == 7      # five defaults, the world's confidential topic and the one above
    assert not any(c['name'] == 'Grievance' for c in cats)
    assert next(c for c in cats if c['name'].startswith('Confidential'))['confidential'] is True
    assert cats[0]['owner_user_id'] is None and cats[0]['owner_name'] is None
    assert set(cats[0]) == {'id', 'name', 'description', 'confidential', 'sort', 'active', 'owner_user_id', 'owner_name'}

    def cat(prefix):
        return next(c['id'] for c in cats if c['name'].startswith(prefix))

    # an employee raises a ticket with an attachment; numbers are sequential with an SLA due time
    file_id = upload(w.sita)
    r = sita.post('/tickets', {'categoryId': cat('Payroll'), 'subject': 'SSF missing on payslip',
                               'body': 'My Bhadra payslip has no SSF line.', 'priority': 'high', 'fileIds': [file_id]})
    assert r.status_code == 201 and set(r.json()) == {'id', 'number', 'slaDueAt'}
    assert r.json()['number'] == 1 and r.json()['slaDueAt'].endswith('Z')
    ticket = r.json()
    r2 = ram.post('/tickets', {'categoryId': cat('Leave'), 'subject': 'Balance looks wrong', 'body': 'Please check'})
    assert r2.status_code == 201 and r2.json()['number'] == 2
    # Someone else's upload cannot be attached.
    other_file = upload(w.ram)
    assert sita.post('/tickets', {'categoryId': cat('Other'), 'subject': 'Steal', 'body': 'x', 'fileIds': [other_file]}).status_code == 403
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'helpdesk.ticket.new' "
               "AND channel = 'in_app'", [w.hr.id])['n'] >= 2

    # only the requester and the help desk team can see a ticket
    tid = ticket['id']
    assert sita.get(f'/tickets/{tid}').status_code == 200
    assert ram.get(f'/tickets/{tid}').status_code == 404
    r = other.get(f'/tickets/{tid}')
    assert r.status_code == 404 and r.json() == {'error': 'ticket not found'}
    assert ram.get('/tickets').json() == {'tickets': []}
    q = hr.get('/tickets').json()['tickets']
    assert len(q) == 2 and q[0]['priority'] == 'high'           # high before normal
    assert {'owner_user_id', 'category_name', 'requester_name', 'assignee_name', 'emp_code', 'employee_name',
            'sla_due_at', 'number', 'subject', 'status', 'confidential'} <= set(q[0])
    mine = sita.get('/tickets/mine').json()['tickets']
    assert [t['id'] for t in mine] == [tid] and 'owner_user_id' not in mine[0]

    # internal notes and their files stay inside the team; a public reply counts as first response
    note_file = upload(w.hr)
    r = hr.post(f'/tickets/{tid}/messages', {'body': 'Check the SSF registration date.', 'internal': True, 'fileIds': [note_file]})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    r = sita.post(f'/tickets/{tid}/messages', {'body': 'sneaky', 'internal': True})
    assert r.status_code == 403 and r.json() == {'error': 'only the help desk team can add internal notes'}
    as_sita = sita.get(f'/tickets/{tid}').json()
    assert set(as_sita) == {'ticket', 'role', 'messages', 'files', 'canReopen'}
    assert as_sita['role'] == 'requester' and len(as_sita['messages']) == 1
    assert 'owner_user_id' not in as_sita['ticket'] and as_sita['ticket']['overdue'] is False
    assert as_sita['messages'][0]['mine'] is True and as_sita['messages'][0]['author_name']
    assert sita.get(f'/tickets/{tid}/files/{note_file}').status_code == 404
    own = as_sita['messages'][0]['file_ids'][0]
    assert [f['id'] for f in as_sita['files']] == [own] and as_sita['files'][0]['filename'] == 'slip.pdf'
    dl = sita.c.get(f'{API}/tickets/{tid}/files/{own}', HTTP_ACCEPT='application/pdf', **sita.h)
    assert dl.status_code == 200 and 'application/pdf' in dl['Content-Type'] and dl['Content-Disposition'].startswith('inline')
    assert sita.get(f'/tickets/{tid}/files/{own}', {'download': '1'})['Content-Disposition'].startswith('attachment')
    assert ram.get(f'/tickets/{tid}/files/{own}').status_code == 404
    assert other.get(f'/tickets/{tid}/files/{own}').status_code == 404
    hr.post(f'/tickets/{tid}/messages', {'body': 'Fixed in the supplementary run.'})
    as_hr = hr.get(f'/tickets/{tid}').json()
    assert as_hr['role'] == 'agent' and len(as_hr['messages']) == 3 and 'owner_user_id' in as_hr['ticket']
    assert as_hr['ticket']['status'] == 'in_progress' and as_hr['ticket']['first_response_at']

    # only HR reassigns; the assignee then works the ticket
    r = sita.patch(f'/tickets/{tid}', {'status': 'resolved'})
    assert r.status_code == 403 and r.json() == {'error': 'only the help desk team can change a ticket'}
    r = hr.patch(f'/tickets/{tid}', {'assigneeUserId': w.sita.id})
    assert r.status_code == 400 and r.json()['error'] == 'a ticket cannot be assigned to the person who raised it'
    r = hr.patch(f'/tickets/{tid}', {'assigneeUserId': w.it_.id})
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert ids(it_.get('/tickets', {'assignee': 'me'})) == [tid]
    r = it_.patch(f'/tickets/{tid}', {'assigneeUserId': w.ram.id})
    assert r.status_code == 403 and r.json() == {'error': 'only HR can reassign or move a ticket'}
    assert it_.patch(f'/tickets/{tid}', {'status': 'resolved'}).status_code == 200

    # the requester rates once, can reopen within the window, and close
    r = hr.post(f'/tickets/{tid}/rate', {'rating': 5})
    assert r.status_code == 403 and r.json() == {'error': 'only the person who raised the ticket can rate it'}
    assert sita.post(f'/tickets/{tid}/rate', {'rating': 2, 'comment': 'Slow'}).json() == {'ok': True}
    r = sita.post(f'/tickets/{tid}/rate', {'rating': 5})
    assert r.status_code == 409 and r.json() == {'error': 'you have already rated this ticket'}
    assert sita.get(f'/tickets/{tid}').json()['canReopen'] is True
    r = sita.post(f'/tickets/{tid}/reopen')
    assert r.status_code == 200 and r.json() == {'ok': True}
    t = sita.get(f'/tickets/{tid}').json()['ticket']
    assert t['status'] == 'in_progress' and t['rating'] is None
    r = sita.post(f'/tickets/{tid}/close')
    assert r.status_code == 200 and r.json() == {'ok': True}
    r = sita.post(f'/tickets/{tid}/messages', {'body': 'one more thing'})
    assert r.status_code == 409 and r.json() == {'error': 'this ticket is closed; raise a new ticket instead'}
    assert sita.post(f'/tickets/{tid}/close').status_code == 409
    assert sita.post(f'/tickets/{tid}/reopen').status_code == 409

    # confidential tickets are never visible to managers or non-HR owners, and only HR can be assigned
    r = sita.post('/tickets', {'categoryId': cat('Confidential'), 'subject': 'Concern about my manager', 'body': 'Confidential matter'})
    assert r.status_code == 201
    grievance = r.json()['id']
    r = hr.patch(f'/tickets/{grievance}', {'assigneeUserId': w.ram.id})
    assert r.status_code == 400 and r.json() == {'error': 'confidential tickets can only be assigned to HR'}
    r = hr.put(f"/categories/{cat('Confidential')}", {'name': 'Confidential HR matter', 'confidential': True, 'ownerUserId': w.ram.id})
    assert r.status_code == 400 and r.json() == {'error': 'a confidential category can only be owned by HR'}
    assert ram.get(f'/tickets/{grievance}').status_code == 404
    assert all('manager' not in x['body'] for x in rows(
        "SELECT body FROM notification_outbox WHERE template = 'helpdesk.ticket.new' AND payload->>'ticketId' = %s", [grievance]))

    # a non-HR category owner sees tickets in their category only
    r = hr.put(f"/categories/{cat('IT')}", {'name': 'IT & equipment', 'ownerUserId': w.it_.id})
    assert r.status_code == 200 and r.json() == {'ok': True}
    r = sita.post('/tickets', {'categoryId': cat('IT'), 'subject': 'Laptop fan noisy', 'body': 'Very loud'})
    active = ids(it_.get('/tickets', {'status': 'active'}))
    assert r.json()['id'] in active and grievance not in active
    assert it_.get(f"/tickets/{r.json()['id']}").json()['role'] == 'agent'
    # HR sees the owner on the topic list; employees do not.
    it_cat = next(c for c in hr.get('/categories').json()['categories'] if c['name'] == 'IT & equipment')
    assert it_cat['owner_user_id'] == w.it_.id and it_cat['owner_name'] == w.it_.name

    # the daily job escalates overdue tickets once and closes old resolved ones
    execute("UPDATE helpdesk_ticket SET sla_due_at = now() - interval '1 hour' WHERE id = %s", [grievance])
    assert hd.helpdesk_daily()['ticketsBreached'] >= 1
    assert hd.helpdesk_daily()['ticketsBreached'] == 0
    assert grievance in ids(hr.get('/tickets', {'breached': '1'}))
    assert hr.get(f'/tickets/{grievance}').json()['ticket']['overdue'] is True
    hr.patch(f'/tickets/{grievance}', {'status': 'resolved'})
    assert hd.helpdesk_daily(now() + timedelta(days=8))['ticketsClosed'] >= 1
    s = hr.get('/stats')
    assert s.status_code == 200 and set(s.json()) == {'stats', 'byCategory'}
    assert s.json()['stats']['breached_90d'] >= 1
    assert {'name', 'open', 'total'} == set(s.json()['byCategory'][0])
    r = sita.get('/stats')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}

    # validates input and settings
    r = sita.post('/tickets', {'categoryId': cat('Other'), 'subject': 'x', 'body': ''})
    assert r.status_code == 400 and {i['path'] for i in r.json()['issues']} == {'subject', 'body'}
    r = sita.put('/settings', {})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    policy = {'slaHours': {'low': 96, 'normal': 48, 'high': 12, 'urgent': 4}, 'autoCloseDays': 5, 'reopenDays': 3}
    r = hr.put('/settings', policy)
    assert r.status_code == 200 and r.json() == {'policy': policy}
    assert sita.get('/settings').json()['policy']['slaHours']['urgent'] == 4


def test_categories_and_agents(w):
    hr, sita = w.api['hr'], w.api['sita']
    r = sita.post('/categories', {'name': 'Mine'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    r = hr.post('/categories', {'name': 'Travel', 'description': 'Trips', 'sort': 3})
    cid = r.json()['id']
    r = hr.post('/categories', {'name': 'Travel'})
    assert r.status_code == 409 and r.json() == {'error': 'a category with this name already exists'}
    r = hr.post('/categories', {'name': ''})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'name'
    r = hr.post('/categories', {'name': 'Owned', 'ownerUserId': w.other_hr.id})
    assert r.status_code == 400 and r.json() == {'error': 'the owner must be an active user in your company'}
    # Inactive topics: HR still lists them, employees do not.
    assert hr.put(f'/categories/{cid}', {'name': 'Travel', 'active': False}).json() == {'ok': True}
    assert any(c['id'] == cid for c in hr.get('/categories').json()['categories'])
    assert not any(c['id'] == cid for c in sita.get('/categories').json()['categories'])
    assert sita.put(f'/categories/{cid}', {'name': 'Travel'}).status_code == 403
    assert hr.put('/categories/00000000-0000-0000-0000-000000000000', {'name': 'Nope'}).json() == {'error': 'category not found'}
    r = hr.put('/categories/not-a-uuid', {'name': 'Nope'})
    assert r.status_code == 400

    a = hr.get('/agents')
    assert a.status_code == 200
    agents = a.json()['agents']
    assert set(agents[0]) == {'id', 'name', 'email', 'roles', 'is_hr'}
    by_id = {x['id']: x for x in agents}
    assert by_id[w.hr.id]['is_hr'] is True and by_id[w.sita.id]['is_hr'] is False
    assert w.other_hr.id not in by_id
    r = sita.get('/agents')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}


def test_queue_filters_and_search(w):
    hr, sita = w.api['hr'], w.api['sita']
    payroll = w.cat('Payroll')
    a = sita.post('/tickets', {'categoryId': payroll, 'subject': 'Missing allowance', 'body': 'x', 'priority': 'urgent'}).json()
    b = sita.post('/tickets', {'categoryId': w.cat('Leave'), 'subject': 'Holiday list', 'body': 'x', 'priority': 'low'}).json()
    assert ids(hr.get('/tickets')) == [a['id'], b['id']]
    assert ids(hr.get('/tickets', {'categoryId': payroll})) == [a['id']]
    assert ids(hr.get('/tickets', {'priority': 'low'})) == [b['id']]
    assert ids(hr.get('/tickets', {'assignee': 'none'})) == [a['id'], b['id']]
    assert ids(hr.get('/tickets', {'search': 'holiday'})) == [b['id']]
    assert ids(hr.get('/tickets', {'search': f"#{a['number']}"})) == [a['id']]
    assert ids(hr.get('/tickets', {'status': 'closed'})) == []
    r = hr.get('/tickets', {'status': 'nope'})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'status'
    assert hr.get('/tickets', {'assignee': 'someone'}).status_code == 400
    # Priority change moves the SLA.
    before = hr.get(f"/tickets/{b['id']}").json()['ticket']['sla_due_at']
    assert hr.patch(f"/tickets/{b['id']}", {'priority': 'urgent'}).json() == {'ok': True}
    assert hr.get(f"/tickets/{b['id']}").json()['ticket']['sla_due_at'] < before
    assert hr.patch(f"/tickets/{b['id']}", {'status': 'bogus'}).status_code == 400
    s = hr.get('/stats').json()['stats']
    assert s['open'] == 2 and s['unassigned'] == 2


def test_errors_routing_and_feature_switch(w):
    sita = w.api['sita']
    assert sita.get('/tickets/not-a-uuid').status_code == 400
    r = sita.get('/tickets/00000000-0000-0000-0000-000000000000')
    assert r.status_code == 404 and r.json() == {'error': 'ticket not found'}
    assert sita.post('/tickets', {'categoryId': '00000000-0000-0000-0000-000000000000', 'subject': 'Hello',
                                  'body': 'x'}).json() == {'error': 'choose a category'}
    assert Client().get(f'{API}/tickets/mine').status_code == 401
    set_features_off(w.co, 'helpdesk')
    for path in ('/tickets/mine', '/tickets', '/categories', '/settings'):
        r = sita.get(path)
        assert r.status_code == 404 and r.json() == {'error': 'feature off', 'feature': 'helpdesk'}
    # The feature gate comes before the HR check.
    assert w.api['hr'].get('/stats').json() == {'error': 'feature off', 'feature': 'helpdesk'}
    assert sita.get('/stats').json() == {'error': 'feature off', 'feature': 'helpdesk'}


def test_cookie_session_like_the_react_app(w):
    """The React app sends the session cookie (same origin) instead of a Bearer token."""
    c = Client()
    sign_in(c, w.sita)
    cats = c.get(f'{API}/categories').json()['categories']
    r = c.post(f'{API}/tickets', json.dumps({'categoryId': cats[0]['id'], 'subject': 'From the web app', 'body': 'Hi'}),
               content_type='application/json')
    assert r.status_code == 201
    assert [t['id'] for t in c.get(f'{API}/tickets/mine').json()['tickets']] == [r.json()['id']]


def test_company_isolation(w):
    """Another company's ids behave as not found everywhere, and each company numbers and lists its own tickets."""
    hr, other = w.api['hr'], w.api['other_hr']
    t = w.api['sita'].post('/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'Ours', 'body': 'x'}).json()
    tid = t['id']
    assert other.get('/tickets').json() == {'tickets': []}
    assert other.get('/stats').json()['stats']['open'] == 0
    assert other.get(f'/tickets/{tid}').status_code == 404
    for method, path, body in (('patch', f'/tickets/{tid}', {'status': 'closed'}),
                               ('post', f'/tickets/{tid}/messages', {'body': 'hi'}),
                               ('post', f'/tickets/{tid}/close', None), ('post', f'/tickets/{tid}/reopen', None)):
        r = getattr(other, method)(path, body)
        assert r.status_code == 404 and r.json() == {'error': 'ticket not found'}
    # Their topics are their own: our category id is unknown to them.
    other_cats = other.get('/categories').json()['categories']
    assert w.cat('Payroll') not in {c['id'] for c in other_cats}
    assert other.post('/tickets', {'categoryId': w.cat('Payroll'), 'subject': 'Theirs', 'body': 'x'}).json() == {'error': 'choose a category'}
    assert other.put(f"/categories/{w.cat('Payroll')}", {'name': 'Hijack'}).json() == {'error': 'category not found'}
    # Our HR cannot assign a ticket to their user, and their settings do not change ours.
    assert hr.patch(f'/tickets/{tid}', {'assigneeUserId': w.other_hr.id}).status_code == 400
    other.put('/settings', {'slaHours': {'low': 1, 'normal': 1, 'high': 1, 'urgent': 1}})
    assert hr.get('/settings').json()['policy']['slaHours']['urgent'] == 8
    # Ticket numbers are per company.
    their = other.post('/tickets', {'categoryId': other_cats[0]['id'], 'subject': 'Theirs', 'body': 'x'}).json()
    assert their['number'] == 1 and t['number'] == 1
    assert [x['id'] for x in hr.get('/tickets').json()['tickets']] == [tid]

