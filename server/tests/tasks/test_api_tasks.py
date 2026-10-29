"""
The /api/v1/tasks endpoints as the React app calls them (frontend/src/api/tasks.js): the session cookie, no CSRF token.
A port of the HTTP-level assertions of apps/api/test/tasks.test.ts (65a9079), plus the path-id, Origin and company
isolation rules.
"""
import json

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import NotificationOutbox, Task, TaskAssignee
from hajiri.tasks import services as tk
from tests.web.factories import PW, make_company, make_employee, make_user, set_features_off

from .world import build, plus, today, upload


class Browser:
    """The React client: the hajiri_session cookie and JSON bodies (client.js), never a CSRF token."""

    def __init__(self, user, origin: str | None = None):
        self.c = Client()
        self.c.cookies[auth.SESSION_COOKIE] = auth.login(user.email, PW).token
        self.h = {'HTTP_ORIGIN': origin} if origin else {}

    def get(self, url, query=None):
        return self.c.get(url, query or {}, **self.h)

    def send(self, method, url, body=None):
        return getattr(self.c, method)(url, json.dumps(body if body is not None else {}),
                                       content_type='application/json', **self.h)


@pytest.fixture
def w(db):
    return build()


@pytest.fixture
def b(w):
    """Browsers for the TS test's people: hr, sita (employee), ram (sita's manager) and other (not a report)."""
    return type('B', (), {k: Browser(getattr(w, k)) for k in ('hr', 'sita', 'ram', 'other')})


def ids(rows):
    return [r['id'] for r in rows]


# ---------------------------------------------------------------- personal boards

def test_personal_board_is_private_and_follows_columns(w, b):
    r = b.sita.get('/api/v1/tasks/boards')
    assert r.status_code == 200 and r.json()['canCreateTeamBoard'] is False
    mine = next(x for x in r.json()['boards'] if x['kind'] == 'personal')
    assert [c['name'] for c in mine['columns']] == ['To do', 'In progress', 'Done']
    assert {'id', 'name', 'kind', 'columns', 'owner_user_id', 'owner_name', 'open', 'overdue'} <= set(mine)
    again = b.sita.get('/api/v1/tasks/boards').json()['boards']
    assert len([x for x in again if x['kind'] == 'personal']) == 1
    board = mine['id']

    # Nobody else can open it, not even HR.
    assert b.hr.get(f'/api/v1/tasks/boards/{board}').status_code == 404
    assert b.ram.get(f'/api/v1/tasks/boards/{board}').json() == {'error': 'board not found'}

    t = b.sita.send('post', f'/api/v1/tasks/boards/{board}/tasks',
                    {'title': 'Renew passport', 'dueDate': plus(1), 'priority': 'high', 'labels': ['personal']})
    assert t.status_code == 201 and set(t.json()['task']) == {'id', 'title', 'due_date'}
    task = t.json()['task']['id']
    bad = b.sita.send('post', f'/api/v1/tasks/boards/{board}/tasks', {'title': 'x', 'columnId': 'nope'})
    assert bad.status_code == 400 and bad.json() == {'error': 'that column does not exist on this board'}
    # Tasks on a personal board go to its owner.
    assert [a['userId'] for a in b.sita.get(f'/api/v1/tasks/{task}').json()['task']['assignees']] == [str(w.sita.id)]

    todo = b.sita.get('/api/v1/tasks/my/todo').json()
    assert task in ids(todo['tasks']) and todo['today'] == today() and todo['weekEnd'] >= today()
    done = b.sita.send('patch', f'/api/v1/tasks/{task}', {'columnId': 'done'})
    assert done.status_code == 200 and done.json()['task']['completed_at'] is not None
    assert task not in ids(b.sita.get('/api/v1/tasks/my/todo').json()['tasks'])
    back = b.sita.send('patch', f'/api/v1/tasks/{task}', {'columnId': 'todo'})
    assert back.json()['task']['completed_at'] is None

    r = b.sita.send('patch', f'/api/v1/tasks/boards/{board}', {'archived': True})
    assert r.status_code == 400 and r.json() == {'error': 'your personal board cannot be archived'}
    r = b.sita.send('put', f'/api/v1/tasks/boards/{board}/members', {'userIds': []})
    assert r.status_code == 400 and r.json() == {'error': 'a personal board has no members'}


def test_employees_cannot_set_up_team_boards(b):
    r = b.sita.send('post', '/api/v1/tasks/boards', {'name': 'Mine'})
    assert r.status_code == 403 and r.json() == {'error': 'only managers and HR can set up team boards'}
    assert b.sita.get('/api/v1/tasks/people').json() == {'people': []}


# ---------------------------------------------------------------- team boards

def test_team_board_flow(w, b):
    sita, ram, other = str(w.sita.id), str(w.ram.id), str(w.other.id)

    # A manager sets up a board with their reports; adding a non-report is refused.
    r = b.ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'memberUserIds': [other]})
    assert r.status_code == 403
    assert r.json() == {'error': 'managers can add only their direct reports to a team board'}
    assert [p['user_id'] for p in b.ram.get('/api/v1/tasks/people').json()['people']] == [sita]
    r = b.ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'addMyReports': True})
    assert r.status_code == 201 and r.json()['board']['kind'] == 'team'
    board = r.json()['board']['id']
    got = b.ram.get(f'/api/v1/tasks/boards/{board}').json()
    assert got['access'] == 'owner' and got['ownerName'] == 'Ram Bahadur'
    assert [m['user_id'] for m in got['members']] == [sita]

    # Tasks can be assigned to members only and the assignee is notified.
    r = b.ram.send('post', f'/api/v1/tasks/boards/{board}/tasks', {'title': 'Count stock', 'assigneeIds': [other]})
    assert r.status_code == 400 and r.json() == {'error': 'assign tasks only to people on this board'}
    r = b.ram.send('post', f'/api/v1/tasks/boards/{board}/tasks',
                   {'title': 'Count stock', 'assigneeIds': [sita], 'dueDate': today()})
    assert r.status_code == 201 and r.json()['task']['due_date'] == today()
    task = r.json()['task']['id']
    assert NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.assigned').count() == 1

    # Members see the board and task; outsiders get 404; HR can open team boards.
    assert b.sita.get(f'/api/v1/tasks/boards/{board}').json()['access'] == 'member'
    assert b.other.get(f'/api/v1/tasks/{task}').status_code == 404
    assert b.other.get(f'/api/v1/tasks/boards/{board}').status_code == 404
    assert b.hr.get(f'/api/v1/tasks/boards/{board}').json()['access'] == 'hr'
    assert board in ids(b.sita.get('/api/v1/tasks/boards').json()['boards'])
    assert board not in ids(b.hr.get('/api/v1/tasks/boards').json()['boards'])
    assert board in ids(b.hr.get('/api/v1/tasks/boards', {'all': 1}).json()['boards'])

    # Checklist, comments and attachments.
    c = b.sita.send('post', f'/api/v1/tasks/{task}/checklist', {'text': 'Shelf A'})
    assert c.status_code == 201 and c.json()['item'] == {**c.json()['item'], 'text': 'Shelf A', 'done': False}
    item = c.json()['item']['id']
    assert b.sita.send('patch', f'/api/v1/tasks/checklist/{item}', {'done': True}).json() == {'ok': True}
    assert b.other.send('patch', f'/api/v1/tasks/checklist/{item}', {'done': False}).status_code == 404
    cm = b.sita.send('post', f'/api/v1/tasks/{task}/comments', {'body': 'Half done'})
    assert cm.status_code == 201
    assert {k: cm.json()['comment'][k] for k in ('body', 'user_id', 'name')} == \
        {'body': 'Half done', 'user_id': sita, 'name': w.sita.name}
    assert NotificationOutbox.objects.filter(user_id=w.ram.id, template='tasks.comment').count() == 1
    at = b.sita.send('post', f'/api/v1/tasks/{task}/attachments', {'fileId': upload(w.sita)})
    assert at.status_code == 201 and set(at.json()) == {'id'}
    att = at.json()['id']
    f = b.ram.get(f'/api/v1/tasks/attachments/{att}/file')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    assert 'attachment' in b.ram.get(f'/api/v1/tasks/attachments/{att}/file', {'download': 1})['Content-Disposition']
    assert b.other.get(f'/api/v1/tasks/attachments/{att}/file').status_code == 404
    full = b.sita.get(f'/api/v1/tasks/{task}').json()
    assert {k: full['task'][k] for k in ('checklist_total', 'checklist_done', 'comments', 'attachments')} == \
        {'checklist_total': 1, 'checklist_done': 1, 'comments': 1, 'attachments': 1}
    assert set(full) == {'task', 'board', 'access', 'members', 'checklist', 'comments', 'attachments'}
    assert full['attachments'][0]['filename'] == 'count.pdf' and full['attachments'][0]['added_by_name'] == w.sita.name
    assert b.ram.send('delete', f'/api/v1/tasks/attachments/{att}').json() == {'ok': True}

    # Only the owner changes columns and members; removing a used column needs a target.
    cols = [{'id': 'todo', 'name': 'Backlog', 'done': False}, {'id': 'done', 'name': 'Finished', 'done': True}]
    r = b.sita.send('patch', f'/api/v1/tasks/boards/{board}', {'columns': cols})
    assert r.status_code == 403 and r.json() == {'error': 'only the board owner can change the board'}
    b.sita.send('patch', f'/api/v1/tasks/{task}', {'columnId': 'doing'})
    r = b.ram.send('patch', f'/api/v1/tasks/boards/{board}', {'columns': cols})
    assert r.status_code == 409
    assert r.json() == {'error': '1 task(s) are in the columns you removed; choose a column to move them to'}
    r = b.ram.send('patch', f'/api/v1/tasks/boards/{board}', {'columns': [{'id': 'todo', 'name': 'A', 'done': False}]})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    r = b.ram.send('patch', f'/api/v1/tasks/boards/{board}', {'columns': cols, 'moveTo': 'todo'})
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert b.sita.get(f'/api/v1/tasks/{task}').json()['task']['column_id'] == 'todo'
    assert b.sita.send('put', f'/api/v1/tasks/boards/{board}/members', {'userIds': []}).status_code == 403

    # Reports count open, overdue and per assignee; the daily job reminds once.
    Task.objects.filter(pk=task).update(due_date=plus(-1))
    r = b.ram.get(f'/api/v1/tasks/reports/boards/{board}')
    assert r.status_code == 200
    assert {k: r.json()['totals'][k] for k in ('open', 'overdue')} == {'open': 1, 'overdue': 1}
    assert {k: r.json()['assignees'][0][k] for k in ('user_id', 'open', 'overdue')} == \
        {'user_id': sita, 'open': 1, 'overdue': 1}
    assert [c['tasks'] for c in r.json()['columns']] == [1, 0] and ids(r.json()['overdueTasks']) == [task]
    assert tk.task_reminders()['overdue'] == 1
    assert tk.task_reminders()['overdue'] == 0

    # Removing a member unassigns them; only the creator or owner can remove a task.
    r = b.sita.send('delete', f'/api/v1/tasks/{task}')
    assert r.status_code == 403
    assert r.json() == {'error': 'only the person who created the task or the board owner can remove it'}
    m = b.ram.send('put', f'/api/v1/tasks/boards/{board}/members', {'userIds': []})
    assert m.status_code == 200 and m.json() == {'members': []}
    assert b.sita.get(f'/api/v1/tasks/{task}').status_code == 404
    assert not TaskAssignee.objects.filter(task_id=task).exists()
    assert b.ram.send('delete', f'/api/v1/tasks/{task}').json() == {'ok': True}
    assert b.ram.get(f'/api/v1/tasks/boards/{board}').json()['tasks'] == []

    # The owner archives the board; it leaves the list.
    assert b.ram.send('patch', f'/api/v1/tasks/boards/{board}', {'archived': True}).json() == {'ok': True}
    assert board not in ids(b.ram.get('/api/v1/tasks/boards').json()['boards'])


def test_task_patch_fields_and_assignees(w, b):
    board = b.ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'addMyReports': True}).json()['board']['id']
    task = b.ram.send('post', f'/api/v1/tasks/boards/{board}/tasks', {'title': 'Count'}).json()['task']['id']
    r = b.ram.send('patch', f'/api/v1/tasks/{task}', {
        'title': 'Count stock', 'description': 'Both shelves', 'priority': 'urgent', 'dueDate': plus(2),
        'labels': ['store'], 'assigneeIds': [str(w.sita.id)], 'position': 5})
    t = r.json()['task']
    assert r.status_code == 200
    assert (t['title'], t['description'], t['priority'], t['due_date'], t['labels'], t['position']) == \
        ('Count stock', 'Both shelves', 'urgent', plus(2), ['store'], 5)
    assert t['assignees'] == [{'userId': str(w.sita.id), 'name': w.sita.name}]
    assert NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.assigned').count() == 1
    t = b.ram.send('patch', f'/api/v1/tasks/{task}', {'dueDate': None, 'description': None}).json()['task']
    assert (t['due_date'], t['description'], t['priority']) == (None, None, 'urgent')
    r = b.ram.send('patch', f'/api/v1/tasks/{task}', {'priority': 'asap'})
    assert r.status_code == 400
    assert r.json()['issues'][0]['message'] == 'Invalid option: expected one of "low"|"normal"|"high"|"urgent"'
    r = b.ram.send('post', f'/api/v1/tasks/boards/{board}/tasks', {'title': ''})
    assert r.status_code == 400 and r.json()['error'] == 'validation'


# ---------------------------------------------------------------- transport rules

def test_auth_feature_ids_and_origin(w, b):
    assert Client().get('/api/v1/tasks/boards').status_code == 401
    assert 'error' in Client().get('/api/v1/tasks/my/todo').json()
    # A malformed id is a 400, as the zod z.uuid() answered.
    for url in ('/api/v1/tasks/nope', '/api/v1/tasks/boards/nope', '/api/v1/tasks/checklist/nope',
                '/api/v1/tasks/reports/boards/nope', '/api/v1/tasks/attachments/nope/file'):
        r = b.sita.get(url) if 'checklist' not in url else b.sita.send('patch', url, {'done': True})
        assert r.status_code == 400, url
    # The cookie is honoured only from this site's own origin for writes.
    evil = Browser(w.ram, origin='https://evil.example')
    r = evil.send('post', '/api/v1/tasks/boards', {'name': 'Ops'})
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert evil.get('/api/v1/tasks/boards').status_code == 200
    own = Browser(w.ram, origin='http://testserver')
    assert own.send('post', '/api/v1/tasks/boards', {'name': 'Ops'}).status_code == 201
    set_features_off(w.co, 'tasks')
    assert b.sita.get('/api/v1/tasks/boards').status_code == 404


def test_other_company_ids_are_not_found(w, b):
    beta = make_company('BETA', 'Beta Pvt. Ltd.')
    boss = make_user(beta, roles=['hr'], employee=make_employee(beta, name='Beta HR'))
    board = tk.create_board(boss, {'name': 'Beta launch'})['id']
    task = tk.create_task(boss, board, {'title': 'Print menus', 'assigneeIds': [str(boss.id)]})['id']
    item = tk.add_checklist(boss, task, {'text': 'Proof'})['id']
    att = tk.add_attachment(boss, task, {'fileId': upload(boss, 'menu.pdf')})['id']
    hr = b.hr     # HR of the first company: sees every team board of its own company, none of Beta's
    assert board not in ids(hr.get('/api/v1/tasks/boards', {'all': 1}).json()['boards'])
    for r in (hr.get(f'/api/v1/tasks/boards/{board}'), hr.get(f'/api/v1/tasks/reports/boards/{board}'),
              hr.send('patch', f'/api/v1/tasks/boards/{board}', {'name': 'Mine'}),
              hr.send('put', f'/api/v1/tasks/boards/{board}/members', {'userIds': []}),
              hr.send('post', f'/api/v1/tasks/boards/{board}/tasks', {'title': 'Sneaky'}),
              hr.get(f'/api/v1/tasks/{task}'), hr.send('patch', f'/api/v1/tasks/{task}', {'title': 'Sneaky'}),
              hr.send('delete', f'/api/v1/tasks/{task}'),
              hr.send('post', f'/api/v1/tasks/{task}/checklist', {'text': 'Sneaky'}),
              hr.send('post', f'/api/v1/tasks/{task}/comments', {'body': 'Sneaky'}),
              hr.send('patch', f'/api/v1/tasks/checklist/{item}', {'done': True}),
              hr.send('delete', f'/api/v1/tasks/checklist/{item}'),
              hr.get(f'/api/v1/tasks/attachments/{att}/file'), hr.send('delete', f'/api/v1/tasks/attachments/{att}')):
        assert r.status_code == 404, r.content
    # Beta's people cannot be put on a first-company board.
    own = hr.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'memberUserIds': [str(boss.id)]})
    assert own.status_code == 400 and own.json() == {'error': 'a chosen person does not exist or cannot sign in'}
    assert str(boss.id) not in [p['user_id'] for p in hr.get('/api/v1/tasks/people').json()['people']]
    assert Task.objects.get(pk=task).title == 'Print menus' and Task.objects.get(pk=task).archived_at is None
