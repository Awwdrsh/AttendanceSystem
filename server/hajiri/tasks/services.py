"""
Tasks (study: "Tasks"): a personal board for every user and team boards that managers and HR set up for their people.
A personal board is private to its owner. A team board is shared with its members; its owner manages the name, the
columns and who is on it. HR can open any team board (never a personal one).

Port of apps/api/src/modules/tasks/service.ts and routes.ts. Every public function takes the signed-in AuthUser and
a body in the TS API's shape (camelCase keys), validated with the same rules and messages as the zod schemas, and
returns rows shaped like the TS JSON (ids as str).
"""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.schema import Arr, Bool, Enum, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.notifications.service import notify

PRIORITIES = ('low', 'normal', 'high', 'urgent')
PRIORITY_LABEL = {'low': 'Low', 'normal': 'Normal', 'high': 'High', 'urgent': 'Urgent'}
# api/tasks.ts PRIORITY_COLOR (gray, blue, orange, red) as hj tones.
PRIORITY_TONE = {'low': 'idle', 'normal': 'info', 'high': 'warn', 'urgent': 'bad'}

DEFAULT_COLUMNS: list[dict] = [
    {'id': 'todo', 'name': 'To do', 'done': False},
    {'id': 'doing', 'name': 'In progress', 'done': False},
    {'id': 'done', 'name': 'Done', 'done': True},
]

MAX_CHECKLIST = 50
MAX_ATTACHMENTS = 10

_COLUMN = Obj({
    'id': Str(regex=r'^[a-z0-9-]{1,24}$', regex_message='column ids are short lowercase words'),
    'name': Str(trim=True, min=1, max=40),
    'done': Bool(default=False),
})
_LABEL = Str(trim=True, min=1, max=24)

TASK_FIELDS = {
    'title': Str(trim=True, min=1, max=200),
    'description': Str(max=5000, nullish=True),
    'columnId': Str(max=24, optional=True),
    'priority': Enum(PRIORITIES, default='normal'),
    'dueDate': IsoDate(nullish=True),
    'labels': Arr(_LABEL, max=8, default=list),
    'assigneeIds': Arr(Uuid(), max=10, default=list),
}
TASK_BODY = Obj(TASK_FIELDS)

BOARD_BODY = Obj({
    'name': Str(trim=True, min=1, max=80),
    'memberUserIds': Arr(Uuid(), max=200, default=list),
    'addMyReports': Bool(default=False),
})
BOARD_PATCH = Obj({
    'name': Str(trim=True, min=1, max=80, optional=True),
    'columns': Arr(_COLUMN, min=1, max=8, optional=True),
    'moveTo': Str(max=24, optional=True),
    'archived': Bool(optional=True),
})
MEMBERS_BODY = Obj({'userIds': Arr(Uuid(), max=200)})
CHECKLIST_BODY = Obj({'text': Str(trim=True, min=1, max=300)})
CHECKLIST_PATCH = Obj({'text': Str(trim=True, min=1, max=300, optional=True), 'done': Bool(optional=True)})
COMMENT_BODY = Obj({'body': Str(trim=True, min=1, max=2000)})
ATTACH_BODY = Obj({'fileId': Uuid()})


def _partial(fields: dict) -> dict:
    """zod .partial() in zod 4: every field optional; a field with a default still gets it when absent."""
    out = {}
    for k, s in fields.items():
        s2 = type(s).__new__(type(s))
        s2.__dict__.update(s.__dict__)
        s2.optional = True
        out[k] = s2
    return out


TASK_PATCH = Obj({**_partial(TASK_FIELDS), 'position': Num(min=0, max=1e9, optional=True)})

TASK_COLS = """t.id::text AS id, t.board_id::text AS board_id, t.column_id, t.title, t.description, t.priority,
  t.due_date::text AS due_date, t.labels, t.position, t.created_by::text AS created_by,
  t.completed_at, t.created_at, t.updated_at,
  coalesce((SELECT json_agg(json_build_object('userId', u.id, 'name', u.name) ORDER BY u.name) FROM task_assignee a JOIN app_user u ON u.id = a.user_id WHERE a.task_id = t.id), '[]') AS assignees,
  (SELECT count(*)::int FROM task_checklist_item c WHERE c.task_id = t.id) AS checklist_total,
  (SELECT count(*)::int FROM task_checklist_item c WHERE c.task_id = t.id AND c.done) AS checklist_done,
  (SELECT count(*)::int FROM task_comment c WHERE c.task_id = t.id) AS comments,
  (SELECT count(*)::int FROM task_attachment c WHERE c.task_id = t.id) AS attachments"""

_BOARD_COLS = """b.id::text AS id, b.company_id::text AS company_id, b.kind, b.name, b.owner_user_id::text AS owner_user_id,
  b.columns, b.archived_at, b.created_at"""


def _add_days(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


def _id(v: Any) -> str:
    """A path id (Django's uuid converter hands over a UUID; anything else must parse as one)."""
    return str(v) if isinstance(v, _uuid.UUID) else parse_uuid(v)


def today_iso() -> str:
    return npt_today().isoformat()


def parse_columns(value: Any) -> list[dict]:
    """columnsSchema: 1 to 8 columns with unique short ids, at least one marked done."""
    cols = parse(Obj({'columns': Arr(_COLUMN, min=1, max=8)}), {'columns': value})['columns']
    _refine_columns(cols, ['columns'])
    return cols


def _refine_columns(cols: list[dict], path: list) -> None:
    issues = []
    if len({c['id'] for c in cols}) != len(cols):
        issues.append({'path': path, 'message': 'column ids must be unique'})
    if not any(c['done'] for c in cols):
        issues.append({'path': path, 'message': 'mark at least one column as done'})
    if issues:
        raise ValidationError(issues)


# ---------------------------------------------------------------- access

def personal_board(user: AuthUser) -> dict:
    r = one(f"SELECT {_BOARD_COLS} FROM task_board b WHERE b.owner_user_id = %s AND b.kind = 'personal'", [user.id])
    if r:
        return r
    return one(f"""WITH ins AS (
        INSERT INTO task_board (company_id, kind, name, owner_user_id, columns) VALUES (%s, 'personal', 'My tasks', %s, %s)
        ON CONFLICT (owner_user_id) WHERE kind = 'personal' DO UPDATE SET name = task_board.name RETURNING *)
        SELECT {_BOARD_COLS} FROM ins b""", [user.company_id, user.id, json.dumps(DEFAULT_COLUMNS)])


def board_for(user: AuthUser, board_id: str) -> tuple[dict, str]:
    """The board and how the user may use it ('owner' | 'member' | 'hr'); 404 for boards they cannot see."""
    board_id = _id(board_id)
    board = one(f'SELECT {_BOARD_COLS} FROM task_board b WHERE b.id = %s AND b.company_id = %s', [board_id, user.company_id])
    if not board:
        raise NotFoundError('board not found')
    if board['owner_user_id'] == str(user.id):
        return board, 'owner'
    if board['kind'] == 'personal':
        raise NotFoundError('board not found')
    if one('SELECT 1 AS x FROM task_board_member WHERE board_id = %s AND user_id = %s', [board_id, user.id]):
        return board, 'member'
    if has_role(user, 'hr'):
        return board, 'hr'
    raise NotFoundError('board not found')


def assert_owner(access: str) -> None:
    if access != 'owner':
        raise ForbiddenError('only the board owner can change the board')


def assert_members_allowed(user: AuthUser, user_ids: list[str]) -> None:
    """Users who may be put on a team board: HR any active user of the company; a manager their direct reports."""
    if not user_ids:
        return
    found = rows("""SELECT id::text AS id, employee_id::text AS employee_id FROM app_user
                    WHERE company_id = %s AND status = 'active' AND id = ANY(%s::uuid[])""", [user.company_id, list(user_ids)])
    if len(found) != len(set(user_ids)):
        raise BadRequestError('a chosen person does not exist or cannot sign in')
    if has_role(user, 'hr'):
        return
    if not user.employee_id:
        raise ForbiddenError('only managers and HR can set up team boards')
    reports = {r['employee_id'] for r in rows(
        'SELECT employee_id::text AS employee_id FROM employee_assignment WHERE manager_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date',
        [user.employee_id])}
    bad = [u for u in found if u['id'] != str(user.id) and (not u['employee_id'] or u['employee_id'] not in reports)]
    if bad:
        raise ForbiddenError('managers can add only their direct reports to a team board')


def assert_assignable(board: dict, user_ids: list[str]) -> None:
    """Assignees must be able to see the board: the owner and members of a team board, only the owner on a personal one."""
    if not user_ids:
        return
    if board['kind'] == 'personal':
        if any(i != board['owner_user_id'] for i in user_ids):
            raise BadRequestError('tasks on your personal board can only be assigned to you')
        return
    allowed = {board['owner_user_id'], *(r['user_id'] for r in rows(
        'SELECT user_id::text AS user_id FROM task_board_member WHERE board_id = %s', [board['id']]))}
    if any(i not in allowed for i in user_ids):
        raise BadRequestError('assign tasks only to people on this board')


def task_for(user: AuthUser, task_id: str) -> tuple[dict, dict, str]:
    task_id = _id(task_id)
    task = one("""SELECT id::text AS id, company_id::text AS company_id, board_id::text AS board_id, column_id, title, description,
                         priority, due_date::text AS due_date, labels, position, created_by::text AS created_by, completed_at,
                         archived_at, created_at, updated_at
                    FROM task WHERE id = %s AND company_id = %s""", [task_id, user.company_id])
    if not task:
        raise NotFoundError('task not found')
    board, access = board_for(user, task['board_id'])
    return task, board, access


def notify_assigned(actor: AuthUser, task: dict, board: dict, user_ids: list[str]) -> None:
    for uid in user_ids:
        if uid == str(actor.id):
            continue
        due = f", due {bs_label(task['due_date'])}" if task.get('due_date') else ''
        notify(company_id=board['company_id'], user_id=uid, template='tasks.assigned', title='New task for you',
               body=f"{actor.name} assigned you “{task['title']}” on {board['name']}{due}.",
               payload={'taskId': task['id'], 'boardId': board['id']})


def members(board_id: str) -> list[dict]:
    return rows("""SELECT m.user_id::text AS user_id, u.name, u.employee_id::text AS employee_id
                     FROM task_board_member m JOIN app_user u ON u.id = m.user_id WHERE m.board_id = %s ORDER BY u.name""",
                [board_id])


def can_remove_task(user: AuthUser, task: dict, access: str) -> bool:
    return access == 'owner' or task.get('created_by') == str(user.id) or access == 'hr'


# ---------------------------------------------------------------- boards

def list_boards(user: AuthUser, all_: bool = False) -> dict:
    company_id_of(user)
    mine = personal_board(user)
    out = rows(f"""SELECT {_BOARD_COLS}, u.name AS owner_name,
              (SELECT count(*)::int FROM task t WHERE t.board_id = b.id AND t.archived_at IS NULL AND t.completed_at IS NULL) AS open,
              (SELECT count(*)::int FROM task t WHERE t.board_id = b.id AND t.archived_at IS NULL AND t.completed_at IS NULL AND t.due_date < %s::date) AS overdue
         FROM task_board b JOIN app_user u ON u.id = b.owner_user_id
        WHERE b.company_id = %s AND b.archived_at IS NULL
          AND (b.id = %s OR (b.kind = 'team' AND (b.owner_user_id = %s OR EXISTS (SELECT 1 FROM task_board_member m WHERE m.board_id = b.id AND m.user_id = %s) OR %s)))
        ORDER BY (b.kind = 'personal') DESC, b.name""",
               [today_iso(), user.company_id, mine['id'], user.id, user.id, bool(has_role(user, 'hr') and all_)])
    return {'boards': out, 'canCreateTeamBoard': has_role(user, 'hr', 'manager')}


def create_board(user: AuthUser, body: dict) -> dict:
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only managers and HR can set up team boards')
    b = parse(BOARD_BODY, body)
    ids = list(b['memberUserIds'])
    if b['addMyReports'] and user.employee_id:
        rep = [r['id'] for r in rows("""SELECT u.id::text AS id FROM employee_assignment a
                                          JOIN app_user u ON u.employee_id = a.employee_id AND u.status = 'active'
                                         WHERE a.manager_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date""", [user.employee_id])]
        ids = list(dict.fromkeys([*ids, *rep]))
    ids = [i for i in ids if i != str(user.id)]
    assert_members_allowed(user, ids)
    company = company_id_of(user)
    with transaction.atomic():
        board = one(f"""WITH ins AS (INSERT INTO task_board (company_id, kind, name, owner_user_id, columns)
                        VALUES (%s, 'team', %s, %s, %s) RETURNING *) SELECT {_BOARD_COLS} FROM ins b""",
                    [company, b['name'], user.id, json.dumps(DEFAULT_COLUMNS)])
        for i in ids:
            execute('INSERT INTO task_board_member (board_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [board['id'], i])
        audit('create', 'task_board', actor_id=user.id, row_id=board['id'], after={'name': b['name'], 'members': len(ids)})
    return board


def get_board(user: AuthUser, board_id: str) -> dict:
    board, access = board_for(user, board_id)
    tasks = rows(f"""SELECT {TASK_COLS} FROM task t WHERE t.board_id = %s AND t.archived_at IS NULL
                      ORDER BY t.column_id, t.position, t.created_at""", [board['id']])
    owner = one('SELECT name FROM app_user WHERE id = %s', [board['owner_user_id']])
    return {'board': board, 'access': access, 'ownerName': owner['name'] if owner else None,
            'members': members(board['id']) if board['kind'] == 'team' else [], 'tasks': tasks}


def update_board(user: AuthUser, board_id: str, body: dict) -> dict:
    board, access = board_for(user, board_id)
    assert_owner(access)
    b = parse(BOARD_PATCH, body)
    if 'columns' in b:
        _refine_columns(b['columns'], ['columns'])
    if b.get('archived') and board['kind'] == 'personal':
        raise BadRequestError('your personal board cannot be archived')
    with transaction.atomic():
        if 'columns' in b:
            cols = b['columns']
            keep = {c['id'] for c in cols}
            removed = [c['id'] for c in board['columns'] if c['id'] not in keep]
            if removed:
                n = one("""SELECT count(*)::int AS n FROM task WHERE board_id = %s AND column_id = ANY(%s::text[])
                           AND archived_at IS NULL""", [board['id'], removed])['n']
                if n > 0:
                    if not b.get('moveTo') or b['moveTo'] not in keep:
                        raise ConflictError(f'{n} task(s) are in the columns you removed; choose a column to move them to')
                    execute("""UPDATE task SET column_id = %s, updated_at = now()
                               WHERE board_id = %s AND column_id = ANY(%s::text[])""", [b['moveTo'], board['id'], removed])
            execute('UPDATE task_board SET columns = %s WHERE id = %s', [json.dumps(cols), board['id']])
            # Done-ness follows the column a task sits in.
            done = [c['id'] for c in cols if c['done']]
            execute("""UPDATE task SET completed_at = CASE WHEN column_id = ANY(%s::text[]) THEN coalesce(completed_at, now()) ELSE NULL END
                       WHERE board_id = %s""", [done, board['id']])
        if b.get('name'):
            execute('UPDATE task_board SET name = %s WHERE id = %s', [b['name'], board['id']])
        if 'archived' in b:
            execute('UPDATE task_board SET archived_at = CASE WHEN %s THEN now() ELSE NULL END WHERE id = %s',
                    [b['archived'], board['id']])
        audit('update', 'task_board', actor_id=user.id, row_id=board['id'], after=b)
    return {'ok': True}


def set_members(user: AuthUser, board_id: str, body: dict) -> dict:
    board, access = board_for(user, board_id)
    assert_owner(access)
    if board['kind'] != 'team':
        raise BadRequestError('a personal board has no members')
    b = parse(MEMBERS_BODY, body)
    ids = [i for i in dict.fromkeys(b['userIds']) if i != board['owner_user_id']]
    current = [m['user_id'] for m in members(board['id'])]
    added = [i for i in ids if i not in current]
    assert_members_allowed(user, added)
    with transaction.atomic():
        removed = [i for i in current if i not in ids]
        if removed:
            execute('DELETE FROM task_board_member WHERE board_id = %s AND user_id = ANY(%s::uuid[])', [board['id'], removed])
            execute("""DELETE FROM task_assignee WHERE user_id = ANY(%s::uuid[])
                       AND task_id IN (SELECT id FROM task WHERE board_id = %s)""", [removed, board['id']])
        for i in added:
            execute('INSERT INTO task_board_member (board_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [board['id'], i])
        audit('members', 'task_board', actor_id=user.id, row_id=board['id'], after={'added': added, 'removed': removed})
    return {'members': members(board['id'])}


def people(user: AuthUser) -> dict:
    """People the current user may add to a team board."""
    company = company_id_of(user)
    if not has_role(user, 'hr', 'manager'):
        return {'people': []}
    if has_role(user, 'hr'):
        out = rows("""SELECT u.id::text AS user_id, u.name, e.designation FROM app_user u LEFT JOIN employee e ON e.id = u.employee_id
                       WHERE u.company_id = %s AND u.status = 'active' ORDER BY u.name LIMIT 1000""", [company])
    else:
        out = rows("""SELECT u.id::text AS user_id, u.name, e.designation FROM employee_assignment a
                        JOIN app_user u ON u.employee_id = a.employee_id AND u.status = 'active'
                        JOIN employee e ON e.id = a.employee_id
                       WHERE a.manager_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date ORDER BY u.name""", [user.employee_id])
    return {'people': out}


# ---------------------------------------------------------------- tasks

def create_task(user: AuthUser, board_id: str, body: dict) -> dict:
    board, _ = board_for(user, board_id)
    b = parse(TASK_BODY, body)
    column = b.get('columnId') or board['columns'][0]['id']
    if 'columnId' in b:
        column = b['columnId']
    col = next((c for c in board['columns'] if c['id'] == column), None)
    if not col:
        raise BadRequestError('that column does not exist on this board')
    assignees = [board['owner_user_id']] if board['kind'] == 'personal' else list(dict.fromkeys(b['assigneeIds']))
    assert_assignable(board, assignees)
    with transaction.atomic():
        pos = one('SELECT coalesce(max(position), 0) + 1 AS p FROM task WHERE board_id = %s AND column_id = %s',
                  [board['id'], column])['p']
        task = one("""INSERT INTO task (company_id, board_id, column_id, title, description, priority, due_date, labels, position, created_by, completed_at)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
                      RETURNING id::text AS id, title, due_date::text AS due_date""",
                   [board['company_id'], board['id'], column, b['title'], b.get('description'), b['priority'],
                    b.get('dueDate'), b['labels'], pos, user.id, bool(col.get('done'))])
        for i in assignees:
            execute('INSERT INTO task_assignee (task_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [task['id'], i])
        notify_assigned(user, task, board, assignees)
    return task


def my_todo(user: AuthUser) -> dict:
    today = today_iso()
    # The week runs Sunday to Saturday; overdue tasks always show.
    dow = (date.fromisoformat(today).weekday() + 1) % 7   # JS getUTCDay: Sunday = 0
    week_end = _add_days(today, 6 - dow)
    out = rows(f"""SELECT {TASK_COLS}, b.name AS board_name, b.kind AS board_kind,
                          (SELECT x->>'name' FROM jsonb_array_elements(b.columns) x WHERE x->>'id' = t.column_id) AS column_name
         FROM task t JOIN task_board b ON b.id = t.board_id
        WHERE t.company_id = %s AND t.archived_at IS NULL AND t.completed_at IS NULL AND b.archived_at IS NULL
          AND (EXISTS (SELECT 1 FROM task_assignee a WHERE a.task_id = t.id AND a.user_id = %s) OR (b.kind = 'personal' AND b.owner_user_id = %s))
          AND (t.due_date IS NULL OR t.due_date <= %s::date)
        ORDER BY t.due_date NULLS LAST, CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.created_at
        LIMIT 100""", [company_id_of(user), user.id, user.id, week_end])
    return {'today': today, 'weekEnd': week_end, 'tasks': out}


def full_task(task_id: str) -> dict | None:
    return one(f'SELECT {TASK_COLS} FROM task t WHERE t.id = %s', [task_id])


def get_task(user: AuthUser, task_id: str) -> dict:
    task, board, access = task_for(user, task_id)
    checklist = rows("""SELECT id::text AS id, text, done, position FROM task_checklist_item WHERE task_id = %s
                        ORDER BY position, id""", [task['id']])
    comments = rows("""SELECT c.id::text AS id, c.body, c.created_at, c.user_id::text AS user_id, u.name
                         FROM task_comment c JOIN app_user u ON u.id = c.user_id WHERE c.task_id = %s ORDER BY c.created_at""",
                    [task['id']])
    attachments = rows("""SELECT a.id::text AS id, f.filename, f.mime, f.bytes, a.added_at, u.name AS added_by_name,
                                 a.added_by::text AS added_by
                            FROM task_attachment a JOIN stored_file f ON f.id = a.file_id
                            LEFT JOIN app_user u ON u.id = a.added_by WHERE a.task_id = %s AND f.deleted_at IS NULL
                           ORDER BY a.added_at""", [task['id']])
    return {'task': full_task(task['id']),
            'board': {'id': board['id'], 'name': board['name'], 'kind': board['kind'], 'columns': board['columns']},
            'access': access, 'members': members(board['id']) if board['kind'] == 'team' else [],
            'checklist': checklist, 'comments': comments, 'attachments': attachments}


def update_task(user: AuthUser, task_id: str, body: dict) -> dict:
    task, board, _ = task_for(user, task_id)
    body = body or {}
    keys = set(body.keys())
    b = parse(TASK_PATCH, body)
    col = None
    if 'columnId' in b:
        col = next((c for c in board['columns'] if c['id'] == b['columnId']), None)
        if not col:
            raise BadRequestError('that column does not exist on this board')
    with transaction.atomic():
        sets: list[str] = []
        vals: list[Any] = []

        def put(column: str, v: Any) -> None:
            sets.append(f'{column} = %s')
            vals.append(v)

        if 'title' in b:
            put('title', b['title'])
        if 'description' in keys:
            put('description', b.get('description'))
        if 'priority' in keys and b.get('priority'):
            put('priority', b['priority'])
        if 'dueDate' in keys:
            put('due_date', b.get('dueDate'))
        if 'labels' in keys and b.get('labels') is not None:
            put('labels', b['labels'])
        if 'position' in b:
            put('position', b['position'])
        if col:
            put('column_id', col['id'])
            sets.append('completed_at = coalesce(completed_at, now())' if col.get('done') else 'completed_at = NULL')
        if sets:
            execute(f"UPDATE task SET {', '.join(sets)}, updated_at = now() WHERE id = %s", [*vals, task['id']])
        if 'assigneeIds' in keys and b.get('assigneeIds') is not None and board['kind'] == 'team':
            ids = list(dict.fromkeys(b['assigneeIds']))
            assert_assignable(board, ids)
            cur = [r['user_id'] for r in rows('SELECT user_id::text AS user_id FROM task_assignee WHERE task_id = %s', [task['id']])]
            new = [i for i in ids if i not in cur]
            execute('DELETE FROM task_assignee WHERE task_id = %s AND NOT (user_id = ANY(%s::uuid[]))', [task['id'], ids])
            for i in new:
                execute('INSERT INTO task_assignee (task_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [task['id'], i])
            t = one('SELECT id::text AS id, title, due_date::text AS due_date FROM task WHERE id = %s', [task['id']])
            notify_assigned(user, t, board, new)
    return {'task': full_task(task['id'])}


def delete_task(user: AuthUser, task_id: str) -> dict:
    task, _, access = task_for(user, task_id)
    if not can_remove_task(user, task, access):
        raise ForbiddenError('only the person who created the task or the board owner can remove it')
    with transaction.atomic():
        execute('UPDATE task SET archived_at = now(), updated_at = now() WHERE id = %s', [task['id']])
        audit('archive', 'task', actor_id=user.id, row_id=task['id'])
    return {'ok': True}


# ---------------------------------------------------------------- checklist, comments, attachments

def add_checklist(user: AuthUser, task_id: str, body: dict) -> dict:
    task, _, _ = task_for(user, task_id)
    b = parse(CHECKLIST_BODY, body)
    n = one('SELECT count(*)::int AS n FROM task_checklist_item WHERE task_id = %s', [task['id']])['n']
    if n >= MAX_CHECKLIST:
        raise BadRequestError('a task can have at most 50 checklist items')
    return one("""INSERT INTO task_checklist_item (task_id, text, position) VALUES (%s, %s, %s)
                  RETURNING id::text AS id, text, done, position""", [task['id'], b['text'], n])


def checklist_task_id(item_id: str) -> str:
    """The task a checklist item belongs to (access is checked by the call that follows)."""
    it = one('SELECT task_id::text AS task_id FROM task_checklist_item WHERE id = %s', [_id(item_id)])
    if not it:
        raise NotFoundError('checklist item not found')
    return it['task_id']


def attachment_task_id(att_id: str) -> str:
    a = one('SELECT task_id::text AS task_id FROM task_attachment WHERE id = %s', [_id(att_id)])
    if not a:
        raise NotFoundError('attachment not found')
    return a['task_id']


def _checklist_item(user: AuthUser, item_id: str) -> str:
    item_id = _id(item_id)
    it = one('SELECT task_id::text AS task_id FROM task_checklist_item WHERE id = %s', [item_id])
    if not it:
        raise NotFoundError('checklist item not found')
    task_for(user, it['task_id'])
    return item_id


def update_checklist(user: AuthUser, item_id: str, body: dict) -> dict:
    item_id = _checklist_item(user, item_id)
    b = parse(CHECKLIST_PATCH, body)
    execute('UPDATE task_checklist_item SET text = coalesce(%s, text), done = coalesce(%s, done) WHERE id = %s',
            [b.get('text'), b.get('done'), item_id])
    return {'ok': True}


def delete_checklist(user: AuthUser, item_id: str) -> dict:
    item_id = _checklist_item(user, item_id)
    execute('DELETE FROM task_checklist_item WHERE id = %s', [item_id])
    return {'ok': True}


def add_comment(user: AuthUser, task_id: str, body: dict) -> dict:
    task, board, _ = task_for(user, task_id)
    b = parse(COMMENT_BODY, body)
    with transaction.atomic():
        c = one("""INSERT INTO task_comment (task_id, user_id, body) VALUES (%s, %s, %s)
                   RETURNING id::text AS id, body, created_at""", [task['id'], user.id, b['body']])
        # Tell the assignees (and the creator) that someone commented.
        to = rows("""SELECT user_id::text AS user_id FROM task_assignee WHERE task_id = %s
                     UNION SELECT created_by::text FROM task WHERE id = %s AND created_by IS NOT NULL""", [task['id'], task['id']])
        for u in to:
            if u['user_id'] == str(user.id):
                continue
            notify(company_id=board['company_id'], user_id=u['user_id'], template='tasks.comment', title='New comment on a task',
                   body=f"{user.name} commented on “{task['title']}”.", payload={'taskId': task['id']})
    return {**c, 'user_id': str(user.id), 'name': user.name}


def add_attachment(user: AuthUser, task_id: str, body: dict) -> dict:
    task, _, _ = task_for(user, task_id)
    b = parse(ATTACH_BODY, body)
    n = one('SELECT count(*)::int AS n FROM task_attachment WHERE task_id = %s', [task['id']])['n']
    if n >= MAX_ATTACHMENTS:
        raise BadRequestError('a task can have at most 10 attachments')
    with transaction.atomic():
        att = one("""INSERT INTO task_attachment (task_id, file_id, added_by) VALUES (%s, %s, %s) RETURNING id::text AS id""",
                  [task['id'], b['fileId'], user.id])
        attach_file(company_id=company_id_of(user), file_id=b['fileId'], user_id=user.id, purpose='task_attachment',
                    attached_to=f"task_attachment:{att['id']}")
    return {'id': att['id']}


def attachment_file(user: AuthUser, att_id: str) -> dict:
    att_id = _id(att_id)
    a = one('SELECT task_id::text AS task_id, file_id::text AS file_id FROM task_attachment WHERE id = %s', [att_id])
    if not a:
        raise NotFoundError('attachment not found')
    task_for(user, a['task_id'])
    return read_file(company_id_of(user), a['file_id'])


def delete_attachment(user: AuthUser, att_id: str) -> dict:
    att_id = _id(att_id)
    a = one("""SELECT task_id::text AS task_id, file_id::text AS file_id, added_by::text AS added_by
                 FROM task_attachment WHERE id = %s""", [att_id])
    if not a:
        raise NotFoundError('attachment not found')
    _, _, access = task_for(user, a['task_id'])
    if a['added_by'] != str(user.id) and access != 'owner':
        raise ForbiddenError('only the person who added it or the board owner can remove it')
    with transaction.atomic():
        execute('DELETE FROM task_attachment WHERE id = %s', [att_id])
        delete_file(company_id_of(user), a['file_id'])
    return {'ok': True}


# ---------------------------------------------------------------- reports

def board_report(user: AuthUser, board_id: str) -> dict:
    board, _ = board_for(user, board_id)
    today = today_iso()
    by_column = {r['column_id']: r['n'] for r in rows(
        'SELECT column_id, count(*)::int AS n FROM task WHERE board_id = %s AND archived_at IS NULL GROUP BY column_id', [board['id']])}
    by_assignee = rows("""SELECT u.id::text AS user_id, u.name, count(*) FILTER (WHERE t.completed_at IS NULL)::int AS open,
                                 count(*) FILTER (WHERE t.completed_at IS NULL AND t.due_date < %s::date)::int AS overdue,
                                 count(*) FILTER (WHERE t.completed_at IS NOT NULL)::int AS done
                            FROM task t JOIN task_assignee a ON a.task_id = t.id JOIN app_user u ON u.id = a.user_id
                           WHERE t.board_id = %s AND t.archived_at IS NULL GROUP BY u.id, u.name ORDER BY open DESC, u.name""",
                       [today, board['id']])
    totals = one("""SELECT count(*) FILTER (WHERE completed_at IS NULL)::int AS open,
                           count(*) FILTER (WHERE completed_at IS NULL AND due_date < %s::date)::int AS overdue,
                           count(*) FILTER (WHERE completed_at IS NOT NULL)::int AS done,
                           count(*) FILTER (WHERE completed_at >= now() - interval '7 days')::int AS done_this_week,
                           count(*) FILTER (WHERE completed_at IS NULL AND NOT EXISTS (SELECT 1 FROM task_assignee a WHERE a.task_id = task.id))::int AS unassigned
                      FROM task WHERE board_id = %s AND archived_at IS NULL""", [today, board['id']])
    overdue = rows(f"""SELECT {TASK_COLS} FROM task t WHERE t.board_id = %s AND t.archived_at IS NULL AND t.completed_at IS NULL
                        AND t.due_date < %s::date ORDER BY t.due_date LIMIT 50""", [board['id'], today])
    return {'board': {'id': board['id'], 'name': board['name'], 'kind': board['kind']},
            'columns': [{**c, 'tasks': by_column.get(c['id'], 0)} for c in board['columns']],
            'totals': totals, 'assignees': by_assignee, 'overdueTasks': overdue}


# ---------------------------------------------------------------- daily job

def task_reminders(now: datetime | None = None) -> dict:
    """
    Daily: remind assignees (or the owner of an unassigned personal task) of open tasks due today, and once of tasks
    that just became overdue. Each reminder goes once per task, person and day.
    """
    today = today_iso()   # as the TS job: the Nepal date now, whatever `now` the worker passes
    found = rows("""SELECT t.id::text AS id, t.company_id::text AS company_id, t.title, t.due_date::text AS due_date,
                           b.name AS board_name, coalesce(a.user_id, b.owner_user_id)::text AS user_id
                      FROM task t JOIN task_board b ON b.id = t.board_id LEFT JOIN task_assignee a ON a.task_id = t.id
                     WHERE t.completed_at IS NULL AND t.archived_at IS NULL AND b.archived_at IS NULL
                       AND (t.due_date = %s::date OR t.due_date = %s::date - 1)
                       AND (a.user_id IS NOT NULL OR b.kind = 'personal')""", [today, today])
    due_today = overdue = 0
    for r in found:
        kind = 'due_today' if r['due_date'] == today else 'overdue'
        with transaction.atomic():
            ins = one("""INSERT INTO task_reminder_sent (task_id, user_id, kind, local_date) VALUES (%s, %s, %s, %s)
                         ON CONFLICT DO NOTHING RETURNING task_id""", [r['id'], r['user_id'], kind, today])
            if not ins:
                continue
            notify(company_id=r['company_id'], user_id=r['user_id'], template=f'tasks.{kind}',
                   title='Task due today' if kind == 'due_today' else 'Task overdue',
                   body=f"“{r['title']}” on {r['board_name']} "
                        f"{'is due today' if kind == 'due_today' else 'was due yesterday and is still open'}.",
                   payload={'taskId': r['id']})
        if kind == 'due_today':
            due_today += 1
        else:
            overdue += 1
    return {'dueToday': due_today, 'overdue': overdue}
