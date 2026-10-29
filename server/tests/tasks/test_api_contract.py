"""
Field-by-field response contract of /api/v1/tasks against the TS routes (65a9079 apps/api/src/modules/tasks/routes.ts):
the exact key sets each route returned (TASK_COLS, `task_board.*`, the report and to-do extras) and the JSON types the
React pages read (frontend/src/pages/tasks/*.jsx): ids as strings, due_date 'YYYY-MM-DD', timestamps as ISO strings
with milliseconds and Z, counts as numbers, assignees as [{userId, name}].
"""
import re

import pytest

from .test_api_tasks import Browser
from .world import build, plus, today, upload

TASK_KEYS = {'id', 'board_id', 'column_id', 'title', 'description', 'priority', 'due_date', 'labels', 'position',
             'created_by', 'completed_at', 'created_at', 'updated_at', 'assignees', 'checklist_total',
             'checklist_done', 'comments', 'attachments'}
BOARD_KEYS = {'id', 'company_id', 'kind', 'name', 'owner_user_id', 'columns', 'archived_at', 'created_at'}
TS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def is_ts(v):
    return isinstance(v, str) and TS.match(v)


def check_task(t, extra=()):
    assert set(t) == TASK_KEYS | set(extra), set(t) ^ (TASK_KEYS | set(extra))
    assert all(isinstance(t[k], str) for k in ('id', 'board_id', 'column_id', 'title', 'priority'))
    assert t['due_date'] is None or DAY.match(t['due_date'])
    assert isinstance(t['labels'], list) and isinstance(t['position'], (int, float))
    assert is_ts(t['created_at']) and is_ts(t['updated_at'])
    assert t['completed_at'] is None or is_ts(t['completed_at'])
    assert all(isinstance(t[k], int) for k in ('checklist_total', 'checklist_done', 'comments', 'attachments'))
    for a in t['assignees']:
        assert set(a) == {'userId', 'name'} and isinstance(a['userId'], str)


def check_board(bd, extra=()):
    assert set(bd) == BOARD_KEYS | set(extra), set(bd) ^ (BOARD_KEYS | set(extra))
    assert is_ts(bd['created_at']) and bd['archived_at'] is None
    for c in bd['columns']:
        assert set(c) == {'id', 'name', 'done'}


@pytest.fixture
def w(db):
    return build()


def test_every_response_shape(w):
    hr, sita, ram = Browser(w.hr), Browser(w.sita), Browser(w.ram)

    # GET /boards
    r = ram.get('/api/v1/tasks/boards').json()
    assert set(r) == {'boards', 'canCreateTeamBoard'} and r['canCreateTeamBoard'] is True
    for bd in r['boards']:
        check_board(bd, ('owner_name', 'open', 'overdue'))
        assert isinstance(bd['open'], int) and isinstance(bd['overdue'], int)

    # GET /people
    p = ram.get('/api/v1/tasks/people').json()
    assert set(p) == {'people'} and p['people']
    for x in p['people']:
        assert set(x) == {'user_id', 'name', 'designation'}
    assert str(w.sita.id) in [x['user_id'] for x in p['people']]
    assert sita.get('/api/v1/tasks/people').json() == {'people': []}
    assert len(hr.get('/api/v1/tasks/people').json()['people']) >= 4

    # POST /boards
    c = ram.send('post', '/api/v1/tasks/boards', {'name': 'Ops', 'memberUserIds': [str(w.sita.id)]})
    assert c.status_code == 201 and set(c.json()) == {'board'}
    check_board(c.json()['board'])
    board = c.json()['board']['id']

    # POST /boards/:id/tasks
    t = ram.send('post', f'/api/v1/tasks/boards/{board}/tasks',
                 {'title': '  Count stock ', 'assigneeIds': [str(w.sita.id)], 'dueDate': plus(-1),
                  'labels': [' store '], 'description': None})
    assert t.status_code == 201 and t.json() == {'task': {'id': t.json()['task']['id'], 'title': 'Count stock',
                                                          'due_date': plus(-1)}}
    task = t.json()['task']['id']

    # GET /boards/:id
    g = sita.get(f'/api/v1/tasks/boards/{board}').json()
    assert set(g) == {'board', 'access', 'ownerName', 'members', 'tasks'}
    check_board(g['board'])
    assert g['access'] == 'member' and g['ownerName'] == w.ram.name
    assert [set(m) for m in g['members']] == [{'user_id', 'name', 'employee_id'}]
    assert g['members'][0] == {'user_id': str(w.sita.id), 'name': w.sita.name, 'employee_id': str(w.sita_e.id)}
    [row] = g['tasks']
    check_task(row)
    assert row['labels'] == ['store'] and row['description'] is None and row['created_by'] == str(w.ram.id)
    assert row['assignees'] == [{'userId': str(w.sita.id), 'name': w.sita.name}] and row['column_id'] == 'todo'

    # Checklist, comment, attachment
    it = sita.send('post', f'/api/v1/tasks/{task}/checklist', {'text': 'Shelf A'})
    assert it.status_code == 201 and set(it.json()) == {'item'}
    assert it.json()['item'] == {'id': it.json()['item']['id'], 'text': 'Shelf A', 'done': False, 'position': 0}
    item = it.json()['item']['id']
    assert sita.send('patch', f'/api/v1/tasks/checklist/{item}', {'done': True}).json() == {'ok': True}
    cm = sita.send('post', f'/api/v1/tasks/{task}/comments', {'body': 'On it'})
    assert cm.status_code == 201 and set(cm.json()['comment']) == {'id', 'body', 'created_at', 'user_id', 'name'}
    assert is_ts(cm.json()['comment']['created_at']) and cm.json()['comment']['user_id'] == str(w.sita.id)
    at = sita.send('post', f'/api/v1/tasks/{task}/attachments', {'fileId': upload(w.sita)})
    assert at.status_code == 201 and set(at.json()) == {'id'}

    # GET /:id
    d = sita.get(f'/api/v1/tasks/{task}').json()
    assert set(d) == {'task', 'board', 'access', 'members', 'checklist', 'comments', 'attachments'}
    check_task(d['task'])
    assert set(d['board']) == {'id', 'name', 'kind', 'columns'}
    assert d['checklist'] == [{'id': item, 'text': 'Shelf A', 'done': True, 'position': 0}]
    assert set(d['comments'][0]) == {'id', 'body', 'created_at', 'user_id', 'name'}
    [a] = d['attachments']
    assert set(a) == {'id', 'filename', 'mime', 'bytes', 'added_at', 'added_by_name'}
    assert a['mime'] == 'application/pdf' and isinstance(a['bytes'], int) and is_ts(a['added_at'])

    # PATCH /:id
    u = sita.send('patch', f'/api/v1/tasks/{task}', {'columnId': 'done', 'position': 2})
    assert set(u.json()) == {'task'}
    check_task(u.json()['task'])
    assert is_ts(u.json()['task']['completed_at']) and u.json()['task']['position'] == 2
    sita.send('patch', f'/api/v1/tasks/{task}', {'columnId': 'todo'})

    # GET /my/todo
    todo = sita.get('/api/v1/tasks/my/todo').json()
    assert set(todo) == {'today', 'weekEnd', 'tasks'} and todo['today'] == today() and DAY.match(todo['weekEnd'])
    [mine] = [x for x in todo['tasks'] if x['id'] == task]
    check_task(mine, ('board_name', 'board_kind', 'column_name'))
    assert (mine['board_name'], mine['board_kind'], mine['column_name']) == ('Ops', 'team', 'To do')

    # GET /reports/boards/:id
    rep = ram.get(f'/api/v1/tasks/reports/boards/{board}').json()
    assert set(rep) == {'board', 'columns', 'totals', 'assignees', 'overdueTasks'}
    assert rep['board'] == {'id': board, 'name': 'Ops', 'kind': 'team'}
    assert [(c['id'], c['tasks']) for c in rep['columns']] == [('todo', 1), ('doing', 0), ('done', 0)]
    assert set(rep['columns'][0]) == {'id', 'name', 'done', 'tasks'}
    assert rep['totals'] == {'open': 1, 'overdue': 1, 'done': 0, 'done_this_week': 0, 'unassigned': 0}
    assert rep['assignees'] == [{'user_id': str(w.sita.id), 'name': w.sita.name, 'open': 1, 'overdue': 1, 'done': 0}]
    [od] = rep['overdueTasks']
    check_task(od)

    # PUT /boards/:id/members, PATCH /boards/:id
    m = ram.send('put', f'/api/v1/tasks/boards/{board}/members', {'userIds': []})
    assert m.json() == {'members': []}
    assert ram.get(f'/api/v1/tasks/{task}').json()['task']['assignees'] == []
    assert ram.send('patch', f'/api/v1/tasks/boards/{board}', {'name': 'Ops 2'}).json() == {'ok': True}
    assert ram.send('delete', f'/api/v1/tasks/{task}').json() == {'ok': True}


def test_validation_error_shape(w):
    ram = Browser(w.ram)
    r = ram.send('post', '/api/v1/tasks/boards', {'name': ''})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert r.json()['issues'][0]['path'] == 'name'
    board = ram.send('post', '/api/v1/tasks/boards', {'name': 'X'}).json()['board']['id']
    bad = ram.send('patch', f'/api/v1/tasks/boards/{board}', {'columns': [{'id': 'a', 'name': 'A'}]})
    assert bad.status_code == 400 and bad.json()['issues'] == [{'path': 'columns',
                                                                 'message': 'mark at least one column as done'}]
