"""
Tasks (study: "Tasks"): a personal board for every user and team boards that managers and HR set up for their people.
A personal board is private to its owner. A team board is shared with its members; its owner manages the name, the
columns and who is on it. HR can open any team board (never a personal one).

Port of apps/api/src/modules/tasks/service.ts and routes.ts. Every public function takes the signed-in AuthUser and
a body in the TS API's shape (camelCase keys), validated by the serializers in serializers.py with the same rules and
messages as the zod schemas, and returns rows shaped like the TS JSON (ids as str, dates as 'YYYY-MM-DD').

Groups of companies: everything is scoped to the ACTIVE company. A board, task, checklist item or attachment of
another company is not found, and only people who can act in the company (home accounts, members with access and
group-role holders: hajiri.core.users.company_users) can be put on its boards. A person has one personal board in
each company they work in (task_board_one_personal is per owner and company), so private to-dos never cross companies.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.contrib.postgres.expressions import ArraySubquery
from django.db import models, transaction
from django.db.models import (
    Case, Count, Exists, ExpressionWrapper, F, IntegerField, Max, OuterRef, Q, Subquery, TextField, When,
)
from django.db.models.functions import Cast, Coalesce, JSONObject, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.users import company_users
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    AppUser, CompanyMembership, Employee, EmployeeAssignment, Task, TaskAssignee, TaskAttachment, TaskBoard,
    TaskBoardMember, TaskChecklistItem, TaskComment, TaskReminderSent,
)
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.notifications.service import notify

from .serializers import (
    AttachmentIn, BoardIn, BoardPatchIn, ChecklistIn, ChecklistPatchIn, ColumnsIn, CommentIn, MembersIn, TaskIn,
    TaskPatchIn,
)

Kind = TaskBoard.Kind
Priority = Task.Priority

PRIORITIES = tuple(Priority.values)
PRIORITY_LABEL = dict(Priority.choices)
# api/tasks.ts PRIORITY_COLOR (gray, blue, orange, red) as hj tones.
PRIORITY_TONE = {Priority.LOW: 'idle', Priority.NORMAL: 'info', Priority.HIGH: 'warn', Priority.URGENT: 'bad'}

DEFAULT_COLUMNS: list[dict] = [
    {'id': 'todo', 'name': 'To do', 'done': False},
    {'id': 'doing', 'name': 'In progress', 'done': False},
    {'id': 'done', 'name': 'Done', 'done': True},
]

MAX_CHECKLIST = 50
MAX_ATTACHMENTS = 10

_BOARD_FIELDS = ('id', 'company_id', 'kind', 'name', 'owner_user_id', 'columns', 'archived_at', 'created_at')
_TASK_FIELDS = ('id', 'board_id', 'column_id', 'title', 'description', 'priority', 'due_date', 'labels', 'position',
                'created_by', 'completed_at', 'created_at', 'updated_at')
_PRIORITY_RANK = Case(When(priority=Priority.URGENT, then=0), When(priority=Priority.HIGH, then=1),
                      When(priority=Priority.NORMAL, then=2), default=3, output_field=IntegerField())


# ---------------------------------------------------------------- row shapes

def _row(r: dict) -> dict:
    """A values() row as the TS JSON had it: UUIDs as strings, dates as 'YYYY-MM-DD'."""
    out = plain(r)
    if out.get('due_date') is not None and not isinstance(out['due_date'], str):
        out['due_date'] = out['due_date'].isoformat()
    return out


def _count(model: type[models.Model], link: str = 'task', **filters) -> Coalesce:
    """`(SELECT count(*) FROM <model> WHERE <link> = outer.id AND ...)` as an expression."""
    qs = (model.objects.filter(**{link: OuterRef('pk')}, **filters).order_by().values(link)
          .annotate(n=Count('*')).values('n'))
    return Coalesce(Subquery(qs, output_field=IntegerField()), 0)


def tasks() -> models.QuerySet:
    """Tasks with what a card needs (TASK_COLS in the TS code): assignees by name and the checklist, comment and
    attachment counts."""
    assignees = (TaskAssignee.objects.filter(task=OuterRef('pk')).order_by('user__name')
                 .values(j=JSONObject(userId=Cast('user_id', TextField()), name=F('user__name'))))
    return Task.objects.annotate(
        assignees=ArraySubquery(assignees),
        checklist_total=_count(TaskChecklistItem), checklist_done=_count(TaskChecklistItem, done=True),
        comments=_count(TaskComment), attachments=_count(TaskAttachment))


def _task_rows(qs: models.QuerySet, *extra: str) -> list[dict]:
    return [_row(r) for r in qs.values(*_TASK_FIELDS, 'assignees', 'checklist_total', 'checklist_done', 'comments',
                                       'attachments', *extra)]


def _board_row(b: TaskBoard) -> dict:
    return {'id': str(b.id), 'company_id': str(b.company_id), 'kind': b.kind, 'name': b.name,
            'owner_user_id': str(b.owner_user_id), 'columns': b.columns, 'archived_at': b.archived_at,
            'created_at': b.created_at}


def _id(v: Any) -> str:
    """A path id (Django's uuid converter hands over a UUID; anything else must parse as one)."""
    return parse_uuid(v)


def today_iso() -> str:
    return npt_today().isoformat()


def parse_columns(value: Any) -> list[dict]:
    """columnsSchema: 1 to 8 columns with unique short ids, at least one marked done."""
    cols = plain(validate(ColumnsIn, {'columns': value})['columns'])
    check_columns(cols, ['columns'])
    return cols


def check_columns(cols: list[dict], path: list) -> None:
    """The zod .refine()s on the column list: unique ids and at least one done column."""
    issues = []
    if len({c['id'] for c in cols}) != len(cols):
        issues.append({'path': path, 'message': 'column ids must be unique'})
    if not any(c['done'] for c in cols):
        issues.append({'path': path, 'message': 'mark at least one column as done'})
    if issues:
        raise ValidationError(issues)


# ---------------------------------------------------------------- people of the company

def _report_employee_ids(user: AuthUser) -> set[str]:
    """The user's direct reports in the active company today."""
    if not user.employee_id:
        return set()
    return {str(e) for e in EmployeeAssignment.objects.for_company(company_id_of(user))
            .filter(manager_id=user.employee_id, valid__contains=npt_today()).values_list('employee_id', flat=True)}


def _report_users(user: AuthUser) -> list[dict]:
    """Active people of the company whose employee record here reports to the user (name order)."""
    reports = _report_employee_ids(user)
    if not reports:
        return []
    return [u for u in company_users(company_id_of(user)) if u['employee_id'] and str(u['employee_id']) in reports]


def _employee_here(company_id: str, user_ref: str = 'user'):
    """The employee record a person has in a company: the account's own for its home company, the membership's
    elsewhere (app_user.employee_id is only the home company's)."""
    membership = CompanyMembership.objects.filter(user_id=OuterRef(f'{user_ref}_id'), company_id=company_id)
    return Case(When(**{f'{user_ref}__company_id': company_id}, then=F(f'{user_ref}__employee_id')),
                default=Subquery(membership.values('employee_id')[:1]), output_field=models.UUIDField())


# ---------------------------------------------------------------- access

def personal_board(user: AuthUser) -> dict:
    """The user's personal board in the active company, made on first use (one per person and company:
    task_board_one_personal)."""
    cid = company_id_of(user)
    board, _ = TaskBoard.objects.for_company(cid).get_or_create(
        owner_user_id=user.id, kind=Kind.PERSONAL, company_id=cid,
        defaults={'name': 'My tasks', 'columns': [dict(c) for c in DEFAULT_COLUMNS]})
    return _board_row(board)


def board_for(user: AuthUser, board_id: str) -> tuple[dict, str]:
    """The board and how the user may use it ('owner' | 'member' | 'hr'); 404 for boards they cannot see."""
    board_id = _id(board_id)
    found = TaskBoard.objects.for_company(company_id_of(user)).filter(pk=board_id).first()
    if found is None:
        raise NotFoundError('board not found')
    board = _board_row(found)
    if board['owner_user_id'] == str(user.id):
        return board, 'owner'
    if board['kind'] == Kind.PERSONAL:
        raise NotFoundError('board not found')
    if TaskBoardMember.objects.filter(board_id=board_id, user_id=user.id).exists():
        return board, 'member'
    if has_role(user, 'hr'):
        return board, 'hr'
    raise NotFoundError('board not found')


def assert_owner(access: str) -> None:
    if access != 'owner':
        raise ForbiddenError('only the board owner can change the board')


def assert_members_allowed(user: AuthUser, user_ids: list[str]) -> None:
    """Users who may be put on a team board: HR anyone who can act in the company; a manager their direct reports."""
    if not user_ids:
        return
    people = {str(u['id']): u for u in company_users(company_id_of(user))}
    found = [people[i] for i in set(user_ids) if i in people]
    if len(found) != len(set(user_ids)):
        raise BadRequestError('a chosen person does not exist or cannot sign in')
    if has_role(user, 'hr'):
        return
    if not user.employee_id:
        raise ForbiddenError('only managers and HR can set up team boards')
    reports = _report_employee_ids(user)
    bad = [u for u in found if str(u['id']) != str(user.id)
           and (not u['employee_id'] or str(u['employee_id']) not in reports)]
    if bad:
        raise ForbiddenError('managers can add only their direct reports to a team board')


def _member_ids(board_id: str) -> list[str]:
    return [str(u) for u in TaskBoardMember.objects.filter(board_id=board_id).values_list('user_id', flat=True)]


def assert_assignable(board: dict, user_ids: list[str]) -> None:
    """Assignees must be able to see the board: the owner and members of a team board, only the owner on a
    personal one."""
    if not user_ids:
        return
    if board['kind'] == Kind.PERSONAL:
        if any(i != board['owner_user_id'] for i in user_ids):
            raise BadRequestError('tasks on your personal board can only be assigned to you')
        return
    allowed = {board['owner_user_id'], *_member_ids(board['id'])}
    if any(i not in allowed for i in user_ids):
        raise BadRequestError('assign tasks only to people on this board')


def task_for(user: AuthUser, task_id: str) -> tuple[dict, dict, str]:
    task_id = _id(task_id)
    found = (Task.objects.for_company(company_id_of(user)).filter(pk=task_id)
             .values('company_id', 'archived_at', *_TASK_FIELDS).first())
    if not found:
        raise NotFoundError('task not found')
    task = _row(found)
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


def members(board: dict) -> list[dict]:
    """People on a team board (name order), with their employee record in the board's company."""
    return [plain(r) for r in TaskBoardMember.objects.filter(board_id=board['id']).order_by('user__name')
            .values('user_id', name=F('user__name'), employee_id=_employee_here(board['company_id']))]


def can_remove_task(user: AuthUser, task: dict, access: str) -> bool:
    return access == 'owner' or task.get('created_by') == str(user.id) or access == 'hr'


# ---------------------------------------------------------------- boards

def _open(**more) -> Coalesce:
    return _count(Task, 'board', archived_at__isnull=True, completed_at__isnull=True, **more)


def list_boards(user: AuthUser, all_: bool = False) -> dict:
    cid = company_id_of(user)
    mine = personal_board(user)
    team = Q() if (has_role(user, 'hr') and all_) else (
        Q(owner_user_id=user.id) | Exists(TaskBoardMember.objects.filter(board=OuterRef('pk'), user_id=user.id)))
    visible = Q(kind=Kind.TEAM) & team
    if mine:
        visible |= Q(pk=mine['id'])
    personal_first = ExpressionWrapper(Q(kind=Kind.PERSONAL), output_field=models.BooleanField())
    out = (TaskBoard.objects.for_company(cid).filter(visible, archived_at__isnull=True)
           .annotate(owner_name=F('owner_user__name'), open=_open(), overdue=_open(due_date__lt=npt_today()))
           .order_by(personal_first.desc(), 'name')
           .values(*_BOARD_FIELDS, 'owner_name', 'open', 'overdue'))
    return {'boards': [plain(r) for r in out], 'canCreateTeamBoard': has_role(user, 'hr', 'manager')}


def create_board(user: AuthUser, body: dict) -> dict:
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only managers and HR can set up team boards')
    b = plain(validate(BoardIn, body))
    ids = list(b['memberUserIds'])
    if b['addMyReports'] and user.employee_id:
        ids = list(dict.fromkeys([*ids, *(str(u['id']) for u in _report_users(user))]))
    ids = [i for i in ids if i != str(user.id)]
    assert_members_allowed(user, ids)
    company = company_id_of(user)
    with transaction.atomic():
        board = TaskBoard.objects.create(company_id=company, kind=Kind.TEAM, name=b['name'], owner_user_id=user.id,
                                         columns=[dict(c) for c in DEFAULT_COLUMNS])
        TaskBoardMember.objects.bulk_create([TaskBoardMember(board=board, user_id=i) for i in ids],
                                            ignore_conflicts=True)
        audit('create', 'task_board', actor_id=user.id, row_id=str(board.id),
              after={'name': b['name'], 'members': len(ids)})
    return _board_row(board)


def get_board(user: AuthUser, board_id: str) -> dict:
    board, access = board_for(user, board_id)
    rows = _task_rows(tasks().for_company(board['company_id']).filter(board_id=board['id'], archived_at__isnull=True)
                      .order_by('column_id', 'position', 'created_at'))
    owner = AppUser.objects.filter(pk=board['owner_user_id']).values_list('name', flat=True).first()
    return {'board': board, 'access': access, 'ownerName': owner,
            'members': members(board) if board['kind'] == Kind.TEAM else [], 'tasks': rows}


def update_board(user: AuthUser, board_id: str, body: dict) -> dict:
    board, access = board_for(user, board_id)
    assert_owner(access)
    b = plain(validate(BoardPatchIn, body))
    if 'columns' in b:
        check_columns(b['columns'], ['columns'])
    if b.get('archived') and board['kind'] == Kind.PERSONAL:
        raise BadRequestError('your personal board cannot be archived')
    board_qs = TaskBoard.objects.filter(pk=board['id'])
    board_tasks = Task.objects.filter(board_id=board['id'])
    with transaction.atomic():
        if 'columns' in b:
            cols = b['columns']
            keep = {c['id'] for c in cols}
            removed = [c['id'] for c in board['columns'] if c['id'] not in keep]
            if removed:
                n = board_tasks.filter(column_id__in=removed, archived_at__isnull=True).count()
                if n > 0:
                    if not b.get('moveTo') or b['moveTo'] not in keep:
                        raise ConflictError(
                            f'{n} task(s) are in the columns you removed; choose a column to move them to')
                    board_tasks.filter(column_id__in=removed).update(column_id=b['moveTo'], updated_at=Now())
            board_qs.update(columns=cols)
            # Done-ness follows the column a task sits in.
            done = [c['id'] for c in cols if c['done']]
            board_tasks.update(completed_at=Case(When(column_id__in=done, then=Coalesce('completed_at', Now())),
                                                 default=None, output_field=models.DateTimeField()))
        if b.get('name'):
            board_qs.update(name=b['name'])
        if 'archived' in b:
            board_qs.update(archived_at=Now() if b['archived'] else None)
        audit('update', 'task_board', actor_id=user.id, row_id=board['id'], after=b)
    return {'ok': True}


def set_members(user: AuthUser, board_id: str, body: dict) -> dict:
    board, access = board_for(user, board_id)
    assert_owner(access)
    if board['kind'] != Kind.TEAM:
        raise BadRequestError('a personal board has no members')
    b = plain(validate(MembersIn, body))
    ids = [i for i in dict.fromkeys(b['userIds']) if i != board['owner_user_id']]
    current = [m['user_id'] for m in members(board)]
    added = [i for i in ids if i not in current]
    assert_members_allowed(user, added)
    with transaction.atomic():
        removed = [i for i in current if i not in ids]
        if removed:
            TaskBoardMember.objects.filter(board_id=board['id'], user_id__in=removed).delete()
            TaskAssignee.objects.filter(user_id__in=removed, task__board_id=board['id']).delete()
        TaskBoardMember.objects.bulk_create([TaskBoardMember(board_id=board['id'], user_id=i) for i in added],
                                            ignore_conflicts=True)
        audit('members', 'task_board', actor_id=user.id, row_id=board['id'], after={'added': added, 'removed': removed})
    return {'members': members(board)}


def people(user: AuthUser) -> dict:
    """People the current user may add to a team board: HR everyone who can act in the company, a manager their
    direct reports."""
    cid = company_id_of(user)
    if not has_role(user, 'hr', 'manager'):
        return {'people': []}
    found = company_users(cid)[:1000] if has_role(user, 'hr') else _report_users(user)
    emp_ids = [u['employee_id'] for u in found if u['employee_id']]
    designation = {str(k): v for k, v in Employee.objects.for_company(cid).filter(pk__in=emp_ids)
                   .values_list('id', 'designation')}
    return {'people': [{'user_id': str(u['id']), 'name': u['name'],
                        'designation': designation.get(str(u['employee_id'])) if u['employee_id'] else None}
                       for u in found]}


# ---------------------------------------------------------------- tasks

def create_task(user: AuthUser, board_id: str, body: dict) -> dict:
    board, _ = board_for(user, board_id)
    b = plain(validate(TaskIn, body))
    column = b['columnId'] if 'columnId' in b else board['columns'][0]['id']
    col = next((c for c in board['columns'] if c['id'] == column), None)
    if not col:
        raise BadRequestError('that column does not exist on this board')
    assignees = ([board['owner_user_id']] if board['kind'] == Kind.PERSONAL
                 else list(dict.fromkeys(b['assigneeIds'])))
    assert_assignable(board, assignees)
    with transaction.atomic():
        pos = (Task.objects.filter(board_id=board['id'], column_id=column)
               .aggregate(p=Coalesce(Max('position'), 0.0))['p'] + 1)
        t = Task.objects.create(
            company_id=board['company_id'], board_id=board['id'], column_id=column, title=b['title'],
            description=b.get('description'), priority=b['priority'], due_date=b.get('dueDate'), labels=b['labels'],
            position=pos, created_by_id=user.id, completed_at=Now() if col.get('done') else None)
        task = {'id': str(t.id), 'title': t.title, 'due_date': b.get('dueDate')}
        TaskAssignee.objects.bulk_create([TaskAssignee(task=t, user_id=i) for i in assignees], ignore_conflicts=True)
        notify_assigned(user, task, board, assignees)
    return task


def my_todo(user: AuthUser) -> dict:
    cid = company_id_of(user)
    today = npt_today()
    # The week runs Sunday to Saturday (isoweekday() % 7: Sunday = 0); overdue tasks always show.
    week_end = today + timedelta(days=6 - today.isoweekday() % 7)
    mine = (Exists(TaskAssignee.objects.filter(task=OuterRef('pk'), user_id=user.id))
            | Q(board__kind=Kind.PERSONAL, board__owner_user_id=user.id))
    qs = (tasks().for_company(cid)
          .filter(mine, archived_at__isnull=True, completed_at__isnull=True, board__archived_at__isnull=True)
          .filter(Q(due_date__isnull=True) | Q(due_date__lte=week_end))
          .annotate(board_name=F('board__name'), board_kind=F('board__kind'), board_columns=F('board__columns'))
          .order_by(F('due_date').asc(nulls_last=True), _PRIORITY_RANK, 'created_at')[:100])
    out = _task_rows(qs, 'board_name', 'board_kind', 'board_columns')
    for t in out:
        cols = t.pop('board_columns') or []
        t['column_name'] = next((c.get('name') for c in cols if c.get('id') == t['column_id']), None)
    return {'today': today.isoformat(), 'weekEnd': week_end.isoformat(), 'tasks': out}


def full_task(task_id: str) -> dict | None:
    found = _task_rows(tasks().filter(pk=task_id))
    return found[0] if found else None


def get_task(user: AuthUser, task_id: str) -> dict:
    task, board, access = task_for(user, task_id)
    tid = task['id']
    checklist = [plain(r) for r in TaskChecklistItem.objects.filter(task_id=tid).order_by('position', 'id')
                 .values('id', 'text', 'done', 'position')]
    comments = [plain(r) for r in TaskComment.objects.filter(task_id=tid).order_by('created_at', 'id')
                .values('id', 'body', 'created_at', 'user_id', name=F('user__name'))]
    attachments = [plain(r) for r in TaskAttachment.objects.filter(task_id=tid, file__deleted_at__isnull=True)
                   .order_by('added_at')
                   .values('id', 'added_at', 'added_by', filename=F('file__filename'), mime=F('file__mime'),
                           bytes=F('file__bytes'), added_by_name=F('added_by__name'))]
    return {'task': full_task(tid),
            'board': {'id': board['id'], 'name': board['name'], 'kind': board['kind'], 'columns': board['columns']},
            'access': access, 'members': members(board) if board['kind'] == Kind.TEAM else [],
            'checklist': checklist, 'comments': comments, 'attachments': attachments}


def update_task(user: AuthUser, task_id: str, body: dict) -> dict:
    task, board, _ = task_for(user, task_id)
    body = body or {}
    keys = set(body.keys())
    b = plain(validate(TaskPatchIn, body, partial=True))
    col = None
    if 'columnId' in b:
        col = next((c for c in board['columns'] if c['id'] == b['columnId']), None)
        if not col:
            raise BadRequestError('that column does not exist on this board')
    changes: dict[str, Any] = {}
    if 'title' in b:
        changes['title'] = b['title']
    if 'description' in keys:
        changes['description'] = b.get('description')
    if 'priority' in keys and b.get('priority'):
        changes['priority'] = b['priority']
    if 'dueDate' in keys:
        changes['due_date'] = b.get('dueDate')
    if 'labels' in keys and b.get('labels') is not None:
        changes['labels'] = b['labels']
    if 'position' in b:
        changes['position'] = b['position']
    if col:
        changes['column_id'] = col['id']
        changes['completed_at'] = Coalesce('completed_at', Now()) if col.get('done') else None
    with transaction.atomic():
        if changes:
            Task.objects.filter(pk=task['id']).update(**changes, updated_at=Now())
        if 'assigneeIds' in keys and b.get('assigneeIds') is not None and board['kind'] == Kind.TEAM:
            ids = list(dict.fromkeys(b['assigneeIds']))
            assert_assignable(board, ids)
            current = TaskAssignee.objects.filter(task_id=task['id'])
            cur = [str(u) for u in current.values_list('user_id', flat=True)]
            new = [i for i in ids if i not in cur]
            current.exclude(user_id__in=ids).delete()
            TaskAssignee.objects.bulk_create([TaskAssignee(task_id=task['id'], user_id=i) for i in new],
                                             ignore_conflicts=True)
            t = _row(Task.objects.filter(pk=task['id']).values('id', 'title', 'due_date').get())
            notify_assigned(user, t, board, new)
    return {'task': full_task(task['id'])}


def delete_task(user: AuthUser, task_id: str) -> dict:
    task, _, access = task_for(user, task_id)
    if not can_remove_task(user, task, access):
        raise ForbiddenError('only the person who created the task or the board owner can remove it')
    with transaction.atomic():
        Task.objects.filter(pk=task['id']).update(archived_at=Now(), updated_at=Now())
        audit('archive', 'task', actor_id=user.id, row_id=task['id'])
    return {'ok': True}


# ---------------------------------------------------------------- checklist, comments, attachments

def add_checklist(user: AuthUser, task_id: str, body: dict) -> dict:
    task, _, _ = task_for(user, task_id)
    b = plain(validate(ChecklistIn, body))
    n = TaskChecklistItem.objects.filter(task_id=task['id']).count()
    if n >= MAX_CHECKLIST:
        raise BadRequestError('a task can have at most 50 checklist items')
    item = TaskChecklistItem.objects.create(task_id=task['id'], text=b['text'], done=False, position=n)
    return {'id': str(item.id), 'text': item.text, 'done': item.done, 'position': item.position}


def _task_of(model: type[models.Model], user: AuthUser, row_id: str, message: str) -> tuple[str, dict]:
    """(row id, row) for a row of the active company hanging off a task; NotFoundError(message) otherwise."""
    row_id = _id(row_id)
    found = (model.objects.for_company(company_id_of(user)).filter(pk=row_id)
             .values('task_id', *(['file_id', 'added_by'] if model is TaskAttachment else [])).first())
    if not found:
        raise NotFoundError(message)
    return row_id, plain(found)


def checklist_task_id(user: AuthUser, item_id: str) -> str:
    """The task a checklist item of the active company belongs to (access is checked by the call that follows)."""
    return _task_of(TaskChecklistItem, user, item_id, 'checklist item not found')[1]['task_id']


def attachment_task_id(user: AuthUser, att_id: str) -> str:
    return _task_of(TaskAttachment, user, att_id, 'attachment not found')[1]['task_id']


def _checklist_item(user: AuthUser, item_id: str) -> str:
    item_id, it = _task_of(TaskChecklistItem, user, item_id, 'checklist item not found')
    task_for(user, it['task_id'])
    return item_id


def update_checklist(user: AuthUser, item_id: str, body: dict) -> dict:
    item_id = _checklist_item(user, item_id)
    b = plain(validate(ChecklistPatchIn, body))
    changes = {k: b[k] for k in ('text', 'done') if b.get(k) is not None}
    if changes:
        TaskChecklistItem.objects.filter(pk=item_id).update(**changes)
    return {'ok': True}


def delete_checklist(user: AuthUser, item_id: str) -> dict:
    item_id = _checklist_item(user, item_id)
    TaskChecklistItem.objects.filter(pk=item_id).delete()
    return {'ok': True}


def add_comment(user: AuthUser, task_id: str, body: dict) -> dict:
    task, board, _ = task_for(user, task_id)
    b = plain(validate(CommentIn, body))
    with transaction.atomic():
        c = TaskComment.objects.create(task_id=task['id'], user_id=user.id, body=b['body'])
        # Tell the assignees (and the creator) that someone commented.
        to = {str(u) for u in TaskAssignee.objects.filter(task_id=task['id']).values_list('user_id', flat=True)}
        if task.get('created_by'):
            to.add(task['created_by'])
        for uid in sorted(to - {str(user.id)}):
            notify(company_id=board['company_id'], user_id=uid, template='tasks.comment', title='New comment on a task',
                   body=f"{user.name} commented on “{task['title']}”.", payload={'taskId': task['id']})
    # id and created_at are database defaults, returned by the INSERT.
    return {'id': str(c.id), 'body': c.body, 'created_at': c.created_at, 'user_id': str(user.id), 'name': user.name}


def add_attachment(user: AuthUser, task_id: str, body: dict) -> dict:
    task, _, _ = task_for(user, task_id)
    b = plain(validate(AttachmentIn, body))
    n = TaskAttachment.objects.filter(task_id=task['id']).count()
    if n >= MAX_ATTACHMENTS:
        raise BadRequestError('a task can have at most 10 attachments')
    with transaction.atomic():
        att = TaskAttachment.objects.create(task_id=task['id'], file_id=b['fileId'], added_by_id=user.id)
        attach_file(company_id=company_id_of(user), file_id=b['fileId'], user_id=user.id, purpose='task_attachment',
                    attached_to=f'task_attachment:{att.id}')
    return {'id': str(att.id)}


def attachment_file(user: AuthUser, att_id: str) -> dict:
    _, a = _task_of(TaskAttachment, user, att_id, 'attachment not found')
    task_for(user, a['task_id'])
    return read_file(company_id_of(user), a['file_id'])


def delete_attachment(user: AuthUser, att_id: str) -> dict:
    att_id, a = _task_of(TaskAttachment, user, att_id, 'attachment not found')
    _, _, access = task_for(user, a['task_id'])
    if a['added_by'] != str(user.id) and access != 'owner':
        raise ForbiddenError('only the person who added it or the board owner can remove it')
    with transaction.atomic():
        TaskAttachment.objects.filter(pk=att_id).delete()
        delete_file(company_id_of(user), a['file_id'])
    return {'ok': True}


# ---------------------------------------------------------------- reports

def board_report(user: AuthUser, board_id: str) -> dict:
    board, _ = board_for(user, board_id)
    today = npt_today()
    live = Task.objects.for_company(board['company_id']).filter(board_id=board['id'], archived_at__isnull=True)
    open_ = Q(completed_at__isnull=True)
    by_column = dict(live.order_by().values('column_id').annotate(n=Count('id')).values_list('column_id', 'n'))
    t_open = Q(task__completed_at__isnull=True)
    by_assignee = [plain(r) for r in TaskAssignee.objects
                   .filter(task__board_id=board['id'], task__archived_at__isnull=True)
                   .values('user_id', name=F('user__name'))
                   .annotate(open=Count('task_id', filter=t_open),
                             overdue=Count('task_id', filter=t_open & Q(task__due_date__lt=today)),
                             done=Count('task_id', filter=Q(task__completed_at__isnull=False)))
                   .order_by('-open', 'name')]
    unassigned = ~Exists(TaskAssignee.objects.filter(task=OuterRef('pk')))
    totals = live.aggregate(
        open=Count('id', filter=open_),
        overdue=Count('id', filter=open_ & Q(due_date__lt=today)),
        done=Count('id', filter=Q(completed_at__isnull=False)),
        done_this_week=Count('id', filter=Q(completed_at__gte=Now() - timedelta(days=7))),
        unassigned=Count('id', filter=open_ & unassigned))
    overdue = _task_rows(tasks().for_company(board['company_id'])
                         .filter(open_, board_id=board['id'], archived_at__isnull=True, due_date__lt=today)
                         .order_by('due_date')[:50])
    return {'board': {'id': board['id'], 'name': board['name'], 'kind': board['kind']},
            'columns': [{**c, 'tasks': by_column.get(c['id'], 0)} for c in board['columns']],
            'totals': totals, 'assignees': by_assignee, 'overdueTasks': overdue}


# ---------------------------------------------------------------- daily job

def task_reminders(now: datetime | None = None) -> dict:
    """
    Daily: remind assignees (or the owner of an unassigned personal task) of open tasks due today, and once of tasks
    that just became overdue. Each reminder goes once per task, person and day. Runs over every company; each
    notice is filed under the task's own company.
    """
    today = npt_today()   # as the TS job: the Nepal date now, whatever `now` the worker passes
    found = (Task.objects.filter(completed_at__isnull=True, archived_at__isnull=True, board__archived_at__isnull=True,
                                 due_date__in=(today, today - timedelta(days=1)))
             .annotate(assignee_ids=ArraySubquery(TaskAssignee.objects.filter(task=OuterRef('pk')).values('user_id')))
             .values('id', 'company_id', 'title', 'due_date', 'assignee_ids', board_name=F('board__name'),
                     board_kind=F('board__kind'), owner_id=F('board__owner_user_id')))
    due_today = overdue = 0
    for r in found:
        recipients = list(r['assignee_ids']) or ([r['owner_id']] if r['board_kind'] == Kind.PERSONAL else [])
        kind = 'due_today' if r['due_date'] == today else 'overdue'
        for uid in recipients:
            with transaction.atomic():
                _, created = TaskReminderSent.objects.get_or_create(task_id=r['id'], user_id=uid, kind=kind,
                                                                    local_date=today)
                if not created:
                    continue
                notify(company_id=str(r['company_id']), user_id=str(uid), template=f'tasks.{kind}',
                       title='Task due today' if kind == 'due_today' else 'Task overdue',
                       body=f"“{r['title']}” on {r['board_name']} "
                            f"{'is due today' if kind == 'due_today' else 'was due yesterday and is still open'}.",
                       payload={'taskId': str(r['id'])})
            if kind == 'due_today':
                due_today += 1
            else:
                overdue += 1
    return {'dueToday': due_today, 'overdue': overdue}
