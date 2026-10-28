"""The board pages, the drawer, the report and the to-do card run a fixed number of queries however many tasks,
assignees, checklist items, comments and boards there are (no N+1)."""
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.tasks import services as tk
from tests.web.factories import htmx, sign_in

from .world import build, plus, team_board, today


@pytest.fixture
def t(db):
    return team_board(build())


def _grow(w, n: int) -> str:
    """n more tasks on the team board, each with an assignee, a checklist item and a comment; one more board."""
    last = None
    for i in range(n):
        last = tk.create_task(w.ram, w.board_id, {'title': f'Job {i}', 'assigneeIds': [str(w.sita.id)],
                                                  'dueDate': plus(-2) if i % 2 else today()})['id']
        tk.add_checklist(w.ram, last, {'text': 'step'})
        tk.add_comment(w.sita, last, {'body': 'on it'})
    tk.create_board(w.ram, {'name': f'Extra {n}', 'addMyReports': True})
    return last


def _count(fn) -> int:
    with CaptureQueriesContext(connection) as q:
        r = fn()
    assert r.status_code == 200, r
    return len(q.captured_queries)


def _requests(w, task_id):
    c = Client()
    sign_in(c, w.ram)
    s = Client()
    sign_in(s, w.sita)
    return {
        'page': lambda: c.get(f'/tasks/{w.board_id}'),
        'columns': lambda: c.get(f'/_/tasks/boards/{w.board_id}/columns', **htmx()),
        'report': lambda: c.get(f'/_/tasks/boards/{w.board_id}/report', **htmx()),
        'drawer': lambda: c.get(f'/_/tasks/{task_id}', **htmx()),
        'todo': lambda: s.get('/_/tasks/todo', **htmx()),
        'api_boards': lambda: c.get('/api/v1/tasks/boards'),
        'api_people': lambda: c.get('/api/v1/tasks/people'),
    }


def test_list_views_do_not_grow_with_the_data(t):
    first = _grow(t, 1)
    reqs = _requests(t, first)
    for r in reqs.values():
        r()   # warm up: personal boards, sessions
    small = {k: _count(r) for k, r in reqs.items()}
    _grow(t, 6)
    reqs = _requests(t, first)
    for r in reqs.values():
        r()
    big = {k: _count(r) for k, r in reqs.items()}
    assert big == small
