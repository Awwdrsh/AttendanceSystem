"""The /api/v1/tasks JSON API (DRF) for Bearer-token clients: the TS routes' paths, statuses and shapes."""
import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import NotificationOutbox, Task, TaskAssignee
from tests.web.factories import PW, bearer, set_features_off

from .world import build, plus, today, upload


@pytest.fixture
def w(db):
    return build()


def api(user):
    token = auth.login(user.email, PW).token
    c = Client()
    h = bearer(token)

    class Api:
        def get(self, url):
            return c.get(url, **h)

        def send(self, method, url, body=None):
            return getattr(c, method)(url, body or {}, content_type='application/json', **h)
    return Api()


def test_boards_tasks_and_drawer_flow(w):
    ram, sita = api(w.ram), api(w.sita)
    r = ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'addMyReports': True})
    assert r.status_code == 201
    board_id = r.json()['board']['id']
    assert r.json()['board']['kind'] == 'team'
    r = ram.send('post', f'/api/v1/tasks/boards/{board_id}/tasks',
                 {'title': 'Count stock', 'assigneeIds': [w.sita.id], 'dueDate': today()})
    assert r.status_code == 201 and r.json()['task']['due_date'] == today()
    task_id = r.json()['task']['id']
    assert NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.assigned').count() == 1
    b = sita.get(f'/api/v1/tasks/boards/{board_id}').json()
    assert b['access'] == 'member' and b['ownerName'] == 'Ram Bahadur'
    assert [(t['id'], t['assignees']) for t in b['tasks']] == [(task_id, [{'userId': w.sita.id, 'name': w.sita.name}])]
    boards = sita.get('/api/v1/tasks/boards').json()
    assert boards['canCreateTeamBoard'] is False
    assert [x['kind'] for x in boards['boards']] == ['personal', 'team']
    assert next(x for x in boards['boards'] if x['id'] == board_id)['open'] == 1

    r = sita.send('patch', f'/api/v1/tasks/{task_id}', {'columnId': 'done', 'priority': 'urgent'})
    assert r.status_code == 200 and r.json()['task']['completed_at'] and r.json()['task']['priority'] == 'urgent'
    r = sita.send('post', f'/api/v1/tasks/{task_id}/checklist', {'text': 'Shelf A'})
    assert r.status_code == 201 and r.json()['item']['text'] == 'Shelf A'
    item = r.json()['item']['id']
    assert sita.send('patch', f'/api/v1/tasks/checklist/{item}', {'done': True}).json() == {'ok': True}
    r = sita.send('post', f'/api/v1/tasks/{task_id}/comments', {'body': 'Half done'})
    assert r.status_code == 201 and r.json()['comment']['name'] == w.sita.name
    r = sita.send('post', f'/api/v1/tasks/{task_id}/attachments', {'fileId': upload(w.sita)})
    assert r.status_code == 201
    att = r.json()['id']
    f = ram.get(f'/api/v1/tasks/attachments/{att}/file')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    full = sita.get(f'/api/v1/tasks/{task_id}').json()
    assert (full['task']['checklist_done'], full['task']['comments'], full['task']['attachments']) == (1, 1, 1)
    assert [c['done'] for c in full['checklist']] == [True]
    assert sita.send('delete', f'/api/v1/tasks/attachments/{att}').json() == {'ok': True}
    assert sita.send('delete', f'/api/v1/tasks/checklist/{item}').json() == {'ok': True}
    r = sita.send('delete', f'/api/v1/tasks/{task_id}')
    assert r.status_code == 403
    assert r.json() == {'error': 'only the person who created the task or the board owner can remove it'}
    assert ram.send('delete', f'/api/v1/tasks/{task_id}').json() == {'ok': True}
    assert Task.objects.get(pk=task_id).archived_at is not None


def test_board_settings_members_people_and_report(w):
    ram = api(w.ram)
    board_id = ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'addMyReports': True}).json()['board']['id']
    task_id = ram.send('post', f'/api/v1/tasks/boards/{board_id}/tasks',
                       {'title': 'Count stock', 'assigneeIds': [w.sita.id], 'dueDate': plus(-1)}).json()['task']['id']
    r = ram.send('patch', f'/api/v1/tasks/boards/{board_id}', {'columns': [{'id': 'todo', 'name': 'A'}]})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert r.json()['issues'][0]['message'] == 'mark at least one column as done'
    assert ram.send('patch', f'/api/v1/tasks/boards/{board_id}', {'name': 'Ops team'}).json() == {'ok': True}
    assert [p['user_id'] for p in ram.get('/api/v1/tasks/people').json()['people']] == [w.sita.id]
    rep = ram.get(f'/api/v1/tasks/reports/boards/{board_id}').json()
    assert rep['board']['name'] == 'Ops team'
    assert {k: rep['totals'][k] for k in ('open', 'overdue', 'unassigned')} == {'open': 1, 'overdue': 1, 'unassigned': 0}
    assert [t['id'] for t in rep['overdueTasks']] == [task_id]
    assert rep['assignees'][0]['user_id'] == w.sita.id
    r = ram.send('put', f'/api/v1/tasks/boards/{board_id}/members', {'userIds': []})
    assert r.status_code == 200 and r.json() == {'members': []}
    assert not TaskAssignee.objects.filter(task_id=task_id).exists()


def test_my_todo_and_errors(w):
    sita = api(w.sita)
    board_id = next(b['id'] for b in sita.get('/api/v1/tasks/boards').json()['boards'] if b['kind'] == 'personal')
    task_id = sita.send('post', f'/api/v1/tasks/boards/{board_id}/tasks',
                        {'title': 'Pay rent', 'dueDate': today()}).json()['task']['id']
    todo = sita.get('/api/v1/tasks/my/todo').json()
    assert todo['today'] == today() and [t['id'] for t in todo['tasks']] == [task_id]
    assert todo['tasks'][0]['column_name'] == 'To do' and todo['tasks'][0]['board_kind'] == 'personal'
    r = sita.send('post', '/api/v1/tasks/boards', {'name': 'Mine'})
    assert r.status_code == 403 and r.json() == {'error': 'only managers and HR can set up team boards'}
    r = sita.send('post', f'/api/v1/tasks/boards/{board_id}/tasks', {'title': ''})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'title'
    r = sita.send('patch', f'/api/v1/tasks/{task_id}', {'priority': 'asap'})
    assert r.status_code == 400
    assert r.json()['issues'][0]['message'] == 'Invalid option: expected one of "low"|"normal"|"high"|"urgent"'
    assert api(w.hr).get(f'/api/v1/tasks/boards/{board_id}').status_code == 404
    assert Client().get('/api/v1/tasks/boards').status_code == 401
    set_features_off(w.co, 'tasks')
    assert sita.get('/api/v1/tasks/boards').status_code == 404
