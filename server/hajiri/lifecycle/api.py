"""
Employee lifecycle routes (port of apps/api/src/modules/lifecycle/routes.ts): onboarding and exit checklists,
probation, resignations and exits, the exit interview and the asset register. Each function is one TS route: it takes
the signed-in AuthUser and a body in the TS camelCase shape and raises hajiri.core.errors with the TS messages.
"""
from __future__ import annotations

import json
from datetime import date

from django.db import IntegrityError, transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import MISSING, Arr, Bool, Enum, Int, IsoDate, Money, Obj, Str, Union, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, user_id_for_employee

from . import service as svc
from .service import (EXIT_KINDS, POLICY_SCHEMA, apply_confirmation, can_complete_task, can_view_case, clearance_blockers,
                      complete_case_if_done, complete_exit, diff_days, lifecycle_policy, record_exit, responsible_users,
                      settlement_status, start_case, task_row)

ASSIGNEES = ('hr', 'manager', 'employee', 'user')
EXIT_KIND_LABEL = {'resignation': 'Resignation', 'termination': 'Termination', 'retirement': 'Retirement', 'contract_end': 'Contract ended',
                   'death': 'Death', 'other': 'Other'}
ASSIGNEE_LABEL = {'hr': 'HR', 'manager': 'Manager', 'employee': 'Employee', 'user': 'Named person'}
RESIGN_REASONS = ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation', 'Further studies', 'Health or family', 'Other']
CONDITIONS = ('new', 'good', 'fair', 'poor', 'damaged', 'lost')


def _page_link(**kw) -> Str:
    return Str(max=200, regex=r'^/[A-Za-z0-9/_?=&-]*$', regex_message='a page inside Hajiri, starting with /', **kw)


TEMPLATE_TASK = Obj({
    'title': Str(trim=True, min=1, max=160), 'description': Str(max=1000, nullish=True), 'department': Str(max=60, nullish=True),
    'assigneeType': Enum(ASSIGNEES), 'assigneeUserId': Uuid(nullish=True), 'dueOffsetDays': Int(min=-90, max=365, default=0),
    'link': _page_link(nullish=True),
}, refine=(lambda t: t['assigneeType'] != 'user' or bool(t.get('assigneeUserId')), 'choose the person for this task'))
TEMPLATE_BODY = Obj({
    'kind': Enum(('onboarding', 'offboarding')), 'name': Str(trim=True, min=1, max=120), 'description': Str(max=1000, nullish=True),
    'branchId': Uuid(nullish=True), 'orgUnitId': Uuid(nullish=True), 'isDefault': Bool(default=False), 'active': Bool(default=True),
    'tasks': Arr(TEMPLATE_TASK, max=60),
})


def _s(r: dict | None, *keys: str) -> dict | None:
    if r is None:
        return None
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def _is_hr(user: AuthUser) -> bool:
    return has_role(user, 'hr')


def assert_manages(user: AuthUser, employee_id: str) -> None:
    """The TS lifecycle routes use the workflow helper ('not allowed to act for this employee')."""
    from hajiri.workflow.services import assert_manages as wf_assert
    wf_assert(user, employee_id)


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def employee_in(company_id: str, id_: str) -> dict:
    r = one('SELECT id, name_en, join_date, probation_end, confirmed_on, status, exit_date FROM employee WHERE id = %s AND company_id = %s',
            [id_, company_id])
    if not r:
        raise NotFoundError('employee not found')
    return _s(r, 'id')


def users_in_company(company_id: str, ids) -> None:
    lst = list(dict.fromkeys(str(x) for x in ids if x))
    if not lst:
        return
    r = one("SELECT count(*)::int AS n FROM app_user WHERE company_id = %s AND id = ANY(%s::uuid[]) AND status = 'active'", [company_id, lst])
    if r['n'] != len(lst):
        raise BadRequestError('the chosen person is not an active user in your company')


def _unique(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505'


def ensure_defaults(user: AuthUser) -> None:
    """Defaults are seeded on first use, so companies that existed before this module get them too."""
    if user and user.company_id:
        svc.ensure_lifecycle_defaults(user.company_id)


# ------------------------------------------------------------ settings

def get_settings(user: AuthUser) -> dict:
    return lifecycle_policy(company_id_of(user))


def save_settings(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    company_id = company_id_of(user)
    policy = parse(POLICY_SCHEMA, body)
    before = lifecycle_policy(company_id)
    with transaction.atomic():
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, 'lifecycle.policy', %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, json.dumps(policy), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id='lifecycle.policy', before=before, after=policy)
    return policy


# ------------------------------------------------------------ templates

def templates(user: AuthUser, kind: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    kind = parse(Enum(('onboarding', 'offboarding'), optional=True), kind) if kind else None
    out = rows("""SELECT t.*, b.name_en AS branch_name, o.name_en AS org_unit_name,
                         coalesce((SELECT json_agg(x ORDER BY x.sort) FROM lifecycle_template_task x WHERE x.template_id = t.id), '[]') AS tasks
                    FROM lifecycle_template t LEFT JOIN branch b ON b.id = t.branch_id LEFT JOIN org_unit o ON o.id = t.org_unit_id
                   WHERE t.company_id = %s AND (%s::text IS NULL OR t.kind = %s) ORDER BY t.kind, t.is_default DESC, t.name""",
               [company_id_of(user), kind, kind])
    return [_s(r, 'id', 'company_id', 'branch_id', 'org_unit_id') for r in out]


def get_template(user: AuthUser, id_: str) -> dict:
    t = next((x for x in templates(user) if x['id'] == str(id_)), None)
    if not t:
        raise NotFoundError('template not found')
    return t


def save_template(user: AuthUser, id_: str | None, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    if id_ is not None:
        id_ = parse_uuid(str(id_))
    b = parse(TEMPLATE_BODY, body)
    users_in_company(company_id, [t.get('assigneeUserId') for t in b['tasks']])
    for table, ref in (('branch', b.get('branchId')), ('org_unit', b.get('orgUnitId'))):
        if ref and not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [ref, company_id]):
            raise BadRequestError(f"{table.replace('_', ' ')} not found")
    with transaction.atomic():
        tid = id_
        if b['isDefault']:
            execute('UPDATE lifecycle_template SET is_default = false WHERE company_id = %s AND kind = %s', [company_id, b['kind']])
        if tid:
            u = one("""UPDATE lifecycle_template SET name = %s, description = %s, branch_id = %s, org_unit_id = %s, is_default = %s, active = %s,
                              updated_at = now()
                        WHERE id = %s AND company_id = %s AND kind = %s RETURNING id""",
                    [b['name'], b.get('description'), b.get('branchId'), b.get('orgUnitId'), b['isDefault'], b['active'], tid, company_id, b['kind']])
            if not u:
                raise NotFoundError('template not found')
            execute('DELETE FROM lifecycle_template_task WHERE template_id = %s', [tid])
        else:
            try:
                with transaction.atomic():
                    ins = one("""INSERT INTO lifecycle_template (company_id, kind, name, description, branch_id, org_unit_id, is_default, active)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                              [company_id, b['kind'], b['name'], b.get('description'), b.get('branchId'), b.get('orgUnitId'), b['isDefault'], b['active']])
            except IntegrityError as e:
                if _unique(e):
                    raise ConflictError('a checklist with this name already exists') from e
                raise
            tid = str(ins['id'])
        for i, t in enumerate(b['tasks']):
            execute("""INSERT INTO lifecycle_template_task (template_id, sort, title, description, department, assignee_type, assignee_user_id,
                                                           due_offset_days, link)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [tid, i, t['title'], t.get('description'), t.get('department'), t['assigneeType'],
                     t.get('assigneeUserId') if t['assigneeType'] == 'user' else None, t['dueOffsetDays'], t.get('link')])
        audit('update' if id_ else 'create', 'lifecycle_template', actor_id=user.id, row_id=tid,
              after={'name': b['name'], 'kind': b['kind'], 'tasks': len(b['tasks'])})
    return str(tid)


# ------------------------------------------------------------ cases and tasks

CASE_KINDS = ('onboarding', 'offboarding')


def cases(user: AuthUser, kind: str | None = None, status: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    kind = parse(Enum(CASE_KINDS), kind) if kind else None
    status = parse(Enum(('active', 'completed', 'cancelled')), status) if status else None
    return [_s(r, 'id', 'exit_id', 'employee_id') for r in rows(
        """SELECT c.id, c.kind, c.status, c.anchor_date, c.created_at, c.completed_at, c.exit_id, c.employee_id, e.emp_code, e.name_en,
                  e.designation, e.join_date, t.name AS template_name,
                  (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id) AS total,
                  (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status <> 'open') AS done,
                  (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status = 'open' AND x.due_date < (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS overdue,
                  (SELECT min(x.due_date) FROM lifecycle_task x WHERE x.case_id = c.id AND x.status = 'open') AS next_due
             FROM lifecycle_case c JOIN employee e ON e.id = c.employee_id LEFT JOIN lifecycle_template t ON t.id = c.template_id
             LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> greatest((now() AT TIME ZONE 'Asia/Kathmandu')::date, c.anchor_date)
            WHERE c.company_id = %s AND (%s::text IS NULL OR c.kind = %s) AND (%s::text IS NULL OR c.status = %s)
              AND (%s OR hj_manages(%s::uuid, e.id, greatest((now() AT TIME ZONE 'Asia/Kathmandu')::date, c.anchor_date)))
            ORDER BY c.status = 'active' DESC, c.anchor_date DESC LIMIT 500""",
        [company_id_of(user), kind, kind, status, status, _is_hr(user), user.employee_id])]


START_CASE = Obj({'employeeId': Uuid(), 'templateId': Uuid(nullish=True), 'anchorDate': IsoDate(optional=True)})


def start_onboarding(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = parse(START_CASE, body)
    e = employee_in(company_id, b['employeeId'])
    if e['status'] == 'exited':
        raise BadRequestError('this employee has left')
    with transaction.atomic():
        return start_case(company_id=company_id, employee_id=b['employeeId'], kind='onboarding', template_id=b.get('templateId'),
                          anchor_date=b.get('anchorDate') or e['join_date'] or npt_today(), started_by=user.id)


def load_case(user: AuthUser, id_: str) -> dict:
    c = _s(one('SELECT * FROM lifecycle_case WHERE id = %s', [parse_uuid(str(id_))]), 'id', 'company_id', 'employee_id', 'exit_id',
           'template_id', 'started_by')
    if not c or not can_view_case(user, c):
        raise NotFoundError('checklist not found')
    return c


def case_detail(user: AuthUser, id_: str) -> dict:
    c = load_case(user, id_)
    emp = _s(one('SELECT id, emp_code, name_en, designation, join_date, exit_date FROM employee WHERE id = %s', [c['employee_id']]), 'id')
    tasks = rows("""SELECT t.*, u.name AS assignee_name, d.name AS done_by_name,
                           (SELECT count(*)::int FROM lifecycle_task_comment m WHERE m.task_id = t.id) AS comments
                      FROM lifecycle_task t LEFT JOIN app_user u ON u.id = t.assignee_user_id LEFT JOIN app_user d ON d.id = t.done_by
                     WHERE t.case_id = %s ORDER BY t.sort, t.due_date""", [c['id']])
    out = []
    for t in tasks:
        t = task_row(t)
        t['can_complete'] = can_complete_task(user, t)
        out.append(t)
    return {'case': c, 'employee': emp, 'tasks': out}


def cancel_case(user: AuthUser, id_: str) -> None:
    _require(user, 'hr')
    c = load_case(user, id_)
    if c['status'] != 'active':
        raise ConflictError(f"checklist is {c['status']}")
    if c['exit_id']:
        raise BadRequestError('cancel the exit instead; its clearance checklist closes with it')
    with transaction.atomic():
        execute("UPDATE lifecycle_case SET status = 'cancelled', completed_at = now() WHERE id = %s", [c['id']])
        audit('cancel', 'lifecycle_case', actor_id=user.id, row_id=c['id'])


ADD_TASK = Obj({
    'title': Str(trim=True, min=1, max=160), 'description': Str(max=1000, nullish=True), 'department': Str(max=60, nullish=True),
    'assigneeType': Enum(ASSIGNEES), 'assigneeUserId': Uuid(nullish=True), 'dueDate': IsoDate(nullish=True), 'link': _page_link(nullish=True),
})


def add_task(user: AuthUser, case_id: str, body: dict) -> dict:
    _require(user, 'hr')
    c = load_case(user, case_id)
    if c['status'] != 'active':
        raise ConflictError(f"checklist is {c['status']}")
    b = parse(ADD_TASK, body)
    if b['assigneeType'] == 'user' and not b.get('assigneeUserId'):
        raise BadRequestError('choose the person for this task')
    users_in_company(c['company_id'], [b.get('assigneeUserId')])
    with transaction.atomic():
        t = task_row(one("""INSERT INTO lifecycle_task (case_id, company_id, employee_id, sort, title, description, department, assignee_type,
                                                       assignee_user_id, due_date, link)
                            VALUES (%s, %s, %s, (SELECT coalesce(max(sort), 0) + 1 FROM lifecycle_task WHERE case_id = %s), %s, %s, %s, %s, %s, %s, %s)
                            RETURNING *""",
                         [c['id'], c['company_id'], c['employee_id'], c['id'], b['title'], b.get('description'), b.get('department'),
                          b['assigneeType'], b.get('assigneeUserId') if b['assigneeType'] == 'user' else None, b.get('dueDate'), b.get('link')]))
        for uid in responsible_users(t):
            if uid != user.id:
                notify(company_id=c['company_id'], user_id=uid, template='lifecycle.task.assigned', title='New checklist task',
                       body=b['title'], payload={'caseId': c['id']})
    return t


def load_task(user: AuthUser, id_: str) -> dict:
    t = task_row(one('SELECT * FROM lifecycle_task WHERE id = %s', [parse_uuid(str(id_))]))
    if not t:
        raise NotFoundError('task not found')
    load_case(user, t['case_id'])
    return t


UPDATE_TASK = Obj({
    'status': Enum(('open', 'done', 'skipped'), optional=True), 'note': Str(max=1000, nullish=True), 'dueDate': IsoDate(nullish=True),
    'assigneeType': Enum(ASSIGNEES, optional=True), 'assigneeUserId': Uuid(nullish=True),
})


def update_task(user: AuthUser, id_: str, body: dict) -> dict:
    t = load_task(user, id_)
    b = parse(UPDATE_TASK, body)
    case_row = one('SELECT status FROM lifecycle_case WHERE id = %s', [t['case_id']])
    if case_row['status'] == 'cancelled':
        raise ConflictError('this checklist was cancelled')
    edits_plan = 'dueDate' in b or 'assigneeType' in b or 'assigneeUserId' in b
    if edits_plan and not _is_hr(user):
        raise ForbiddenError('only HR can change due dates and owners')
    if ('status' in b or 'note' in b) and not can_complete_task(user, t):
        raise ForbiddenError('this task is for someone else')
    if b.get('status') == 'skipped' and not _is_hr(user):
        raise ForbiddenError('only HR can skip a task')
    if b.get('assigneeType') == 'user' and not b.get('assigneeUserId'):
        raise BadRequestError('choose the person for this task')
    users_in_company(t['company_id'], [b.get('assigneeUserId')])
    status = b.get('status')
    with transaction.atomic():
        u = task_row(one("""UPDATE lifecycle_task SET
                              status = coalesce(%s, status), note = CASE WHEN %s::boolean THEN %s ELSE note END,
                              done_by = CASE WHEN %s IN ('done', 'skipped') THEN %s::uuid WHEN %s = 'open' THEN NULL ELSE done_by END,
                              done_at = CASE WHEN %s IN ('done', 'skipped') THEN now() WHEN %s = 'open' THEN NULL ELSE done_at END,
                              due_date = CASE WHEN %s::boolean THEN %s::date ELSE due_date END,
                              assignee_type = coalesce(%s, assignee_type),
                              assignee_user_id = CASE WHEN %s::text IS NULL THEN assignee_user_id WHEN %s = 'user' THEN %s::uuid ELSE NULL END
                            WHERE id = %s RETURNING *""",
                         [status, 'note' in b, b.get('note'), status, user.id, status, status, status, 'dueDate' in b, b.get('dueDate'),
                          b.get('assigneeType'), b.get('assigneeType'), b.get('assigneeType'), b.get('assigneeUserId'), t['id']]))
        # Clearance checklists stay open until the exit is completed; onboarding closes itself when everything is ticked.
        completed = False
        if status in ('done', 'skipped'):
            k = one('SELECT exit_id FROM lifecycle_case WHERE id = %s', [t['case_id']])
            if not (k and k['exit_id']):
                completed = complete_case_if_done(t['case_id'])
        if status == 'open':
            execute("UPDATE lifecycle_case SET status = 'active', completed_at = NULL WHERE id = %s AND status = 'completed' AND exit_id IS NULL",
                    [t['case_id']])
        audit('update', 'lifecycle_task', actor_id=user.id, row_id=t['id'], before={'status': t['status']}, after=b)
    return {'task': u, 'caseCompleted': completed}


def comments(user: AuthUser, task_id: str) -> list[dict]:
    t = load_task(user, task_id)
    return rows("""SELECT m.id, m.body, m.created_at, m.author_id, u.name AS author_name FROM lifecycle_task_comment m
                     JOIN app_user u ON u.id = m.author_id WHERE m.task_id = %s ORDER BY m.created_at""", [t['id']])


COMMENT = Obj({'body': Str(trim=True, min=1, max=2000)})


def add_comment(user: AuthUser, task_id: str, body: dict) -> dict:
    t = load_task(user, task_id)
    b = parse(COMMENT, body)
    with transaction.atomic():
        ins = one('INSERT INTO lifecycle_task_comment (task_id, author_id, body) VALUES (%s, %s, %s) RETURNING id, body, created_at',
                  [t['id'], user.id, b['body']])
        for uid in responsible_users(t):
            if uid != user.id:
                notify(company_id=t['company_id'], user_id=uid, template='lifecycle.task.comment', title=f'Comment on "{t["title"]}"',
                       body=b['body'][:200], payload={'caseId': t['case_id'], 'taskId': t['id']})
    return ins


def my_tasks(user: AuthUser) -> list[dict]:
    """Tasks the signed-in person should act on: their own checklist items, tasks for their team or assigned to them,
    and HR tasks for HR."""
    return [task_row(r) for r in rows(
        """SELECT t.*, c.kind, e.name_en AS employee_name, e.emp_code, (t.employee_id = %s::uuid) AS own
             FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id JOIN employee e ON e.id = t.employee_id
             LEFT JOIN employee_assignment a ON a.employee_id = t.employee_id AND a.valid @> greatest((now() AT TIME ZONE 'Asia/Kathmandu')::date, c.anchor_date)
            WHERE t.company_id = %s AND c.status = 'active' AND t.status = 'open'
              AND ((t.assignee_type = 'employee' AND t.employee_id = %s::uuid)
                OR (t.assignee_type = 'user' AND t.assignee_user_id = %s)
                OR (t.assignee_type = 'manager' AND a.manager_id = %s::uuid)
                OR (%s AND (t.assignee_type = 'hr' OR (t.assignee_type = 'manager' AND a.manager_id IS NULL))))
            ORDER BY t.due_date NULLS LAST, t.sort LIMIT 200""",
        [user.employee_id, company_id_of(user), user.employee_id, user.id, user.employee_id, _is_hr(user)])]


# ------------------------------------------------------------ one employee at a glance (employee page)

def employee_summary(user: AuthUser, id_: str) -> dict:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    e = employee_in(company_id, id_)
    assert_manages(user, id_)
    cs = [_s(r, 'id') for r in rows(
        """SELECT c.id, c.kind, c.status, c.anchor_date, c.created_at, (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id) AS total,
                  (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status <> 'open') AS done
             FROM lifecycle_case c WHERE c.employee_id = %s ORDER BY c.created_at DESC""", [id_])]
    reviews = [_s(r, 'id') for r in rows(
        """SELECT r.id, r.rating, r.recommendation, r.extend_to, r.last_working_day, r.status, r.created_at, r.probation_end,
                  u.name AS submitted_by_name
             FROM probation_review r JOIN app_user u ON u.id = r.submitted_by WHERE r.employee_id = %s ORDER BY r.created_at DESC""", [id_])]
    confirmations = [_s(r, 'id', 'review_id') for r in rows(
        'SELECT id, confirmed_on, review_id, created_at FROM employee_confirmation WHERE employee_id = %s ORDER BY created_at DESC', [id_])]
    exits = [_s(r, 'id') for r in rows(
        """SELECT id, kind, status, voluntary, reason_category, proposed_lwd, accepted_lwd, submitted_on FROM employee_exit
            WHERE employee_id = %s ORDER BY created_at DESC""", [id_])]
    assets = [_s(r, 'id', 'assignment_id') for r in rows(
        """SELECT g.id AS assignment_id, g.assigned_on, g.returned_on, g.condition_out, g.condition_in, a.id, a.tag, a.name, a.serial,
                  t.name AS type_name
             FROM asset_assignment g JOIN asset a ON a.id = g.asset_id LEFT JOIN asset_type t ON t.id = a.type_id
            WHERE g.employee_id = %s ORDER BY g.returned_on IS NULL DESC, g.assigned_on DESC""", [id_])]
    return {'employee': e, 'cases': cs,
            'probation': {'end': e['probation_end'], 'confirmedOn': e['confirmed_on'], 'reviews': reviews, 'confirmations': confirmations},
            'exits': exits, 'assets': assets}


# ------------------------------------------------------------ probation

def probation(user: AuthUser, within: int | str | None = 60) -> list[dict]:
    _require(user, 'hr', 'manager')
    within = parse(Int(min=1, max=365, coerce=True, default=60), MISSING if within in (None, '') else within)
    return [_s(r, 'id') for r in rows(
        """SELECT e.id, e.emp_code, e.name_en, e.designation, e.join_date, e.probation_end, (e.probation_end - (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS days_left,
                  m.name_en AS manager_name,
                  (SELECT row_to_json(x) FROM (SELECT r.id, r.status, r.recommendation, r.created_at FROM probation_review r
                                               WHERE r.employee_id = e.id ORDER BY r.created_at DESC LIMIT 1) x) AS last_review
             FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date LEFT JOIN employee m ON m.id = a.manager_id
            WHERE e.company_id = %s AND e.status = 'active' AND e.probation_end IS NOT NULL AND e.confirmed_on IS NULL
              AND (e.exit_date IS NULL OR e.exit_date >= (now() AT TIME ZONE 'Asia/Kathmandu')::date)
              AND e.probation_end <= (now() AT TIME ZONE 'Asia/Kathmandu')::date + %s::int AND (%s OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))
            ORDER BY e.probation_end""", [company_id_of(user), within, _is_hr(user), user.employee_id])]


REVIEW_BODY = Obj({
    'employeeId': Uuid(), 'rating': Int(min=1, max=5), 'recommendation': Enum(('confirm', 'extend', 'terminate')),
    'extendTo': IsoDate(nullish=True), 'lastWorkingDay': IsoDate(nullish=True), 'comments': Str(trim=True, min=3, max=4000),
})


def submit_review(user: AuthUser, body: dict) -> str:
    from hajiri.workflow.services import start_workflow
    _require(user, 'hr', 'manager')
    company_id = company_id_of(user)
    b = parse(REVIEW_BODY, body)
    e = employee_in(company_id, b['employeeId'])
    if b['employeeId'] == user.employee_id:
        raise ForbiddenError('you cannot review your own probation')
    assert_manages(user, b['employeeId'])
    if e['status'] != 'active':
        raise BadRequestError('this employee has left')
    if e['confirmed_on']:
        raise ConflictError(f"already confirmed on {bs_label(e['confirmed_on'])}")
    if b['recommendation'] == 'extend':
        if not b.get('extendTo'):
            raise BadRequestError('choose the new probation end date')
        if date.fromisoformat(b['extendTo']) <= (e['probation_end'] or npt_today()):
            raise BadRequestError('the new end date must be after the current probation end')
    if b['recommendation'] == 'terminate':
        if not b.get('lastWorkingDay'):
            raise BadRequestError('choose the last working day')
        if e['join_date'] and date.fromisoformat(b['lastWorkingDay']) < e['join_date']:
            raise BadRequestError('the last working day cannot be before the join date')
    try:
        with transaction.atomic():
            rid = str(one("""INSERT INTO probation_review (company_id, employee_id, probation_end, rating, recommendation, extend_to,
                                                          last_working_day, comments, submitted_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                          [company_id, b['employeeId'], e['probation_end'], b['rating'], b['recommendation'],
                           b.get('extendTo') if b['recommendation'] == 'extend' else None,
                           b.get('lastWorkingDay') if b['recommendation'] == 'terminate' else None, b['comments'], user.id])['id'])
            rec = {'confirm': 'confirm', 'extend': 'extend probation'}.get(b['recommendation'], 'end employment')
            start_workflow(company_id=company_id, object_type='probation_review', object_id=rid, employee_id=b['employeeId'],
                           title='Probation review to approve', body=f"{e['name_en']}: {rec}")
            audit('create', 'probation_review', actor_id=user.id, row_id=rid, after={'rating': b['rating'], 'recommendation': b['recommendation']})
    except IntegrityError as ex:
        if _unique(ex):
            raise ConflictError('a probation review is already waiting for approval') from ex
        raise
    return rid


CONFIRM_BODY = Obj({'employeeId': Uuid(), 'confirmedOn': IsoDate()})


def confirm(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = parse(CONFIRM_BODY, body)
    e = employee_in(company_id, b['employeeId'])
    if e['confirmed_on']:
        raise ConflictError(f"already confirmed on {bs_label(e['confirmed_on'])}")
    if e['join_date'] and date.fromisoformat(b['confirmedOn']) < e['join_date']:
        raise BadRequestError('confirmation cannot be before the join date')
    with transaction.atomic():
        return apply_confirmation(company_id=company_id, employee_id=b['employeeId'], confirmed_on=b['confirmedOn'], review_id=None,
                                  actor_id=user.id)


# ------------------------------------------------------------ exits

EXIT_COLS = """x.id, x.employee_id, x.kind, x.voluntary, x.reason_category, x.reason, x.submitted_on, x.notice_days, x.proposed_lwd,
    x.accepted_lwd, x.status, x.decided_at, x.completed_at, x.created_at, e.emp_code, e.name_en, e.designation, e.join_date,
    (SELECT c.id FROM lifecycle_case c WHERE c.exit_id = x.id ORDER BY c.created_at DESC LIMIT 1) AS case_id,
    (SELECT count(*)::int FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id WHERE c.exit_id = x.id AND c.status <> 'cancelled') AS tasks_total,
    (SELECT count(*)::int FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id WHERE c.exit_id = x.id AND c.status <> 'cancelled'
        AND t.status <> 'open') AS tasks_done,
    (SELECT count(*)::int FROM asset_assignment g WHERE g.employee_id = x.employee_id AND g.returned_on IS NULL) AS assets_held,
    EXISTS (SELECT 1 FROM exit_interview i WHERE i.exit_id = x.id) AS interview_done"""
EXIT_STATUSES = ('pending', 'accepted', 'rejected', 'withdrawn', 'cancelled', 'completed', 'open')


def _exit(r):
    r = _s(r, 'id', 'employee_id', 'case_id')
    if r is not None:
        r['lwd'] = r['accepted_lwd'] or r['proposed_lwd']
    return r


def exits(user: AuthUser, status: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    status = parse(Enum(EXIT_STATUSES), status) if status else None
    return [_exit(r) for r in rows(
        f"""SELECT {EXIT_COLS} FROM employee_exit x JOIN employee e ON e.id = x.employee_id
              LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> least((now() AT TIME ZONE 'Asia/Kathmandu')::date, coalesce(x.accepted_lwd, x.proposed_lwd))
             WHERE x.company_id = %s AND (%s::text IS NULL OR x.status = %s OR (%s = 'open' AND x.status IN ('pending', 'accepted')))
               AND (%s OR hj_manages(%s::uuid, e.id, least((now() AT TIME ZONE 'Asia/Kathmandu')::date, coalesce(x.accepted_lwd, x.proposed_lwd))))
             ORDER BY x.status IN ('pending', 'accepted') DESC, coalesce(x.accepted_lwd, x.proposed_lwd) DESC LIMIT 500""",
        [company_id_of(user), status, status, status, _is_hr(user), user.employee_id])]


def my_exits(user: AuthUser) -> dict:
    policy = lifecycle_policy(company_id_of(user))
    if not user.employee_id:
        return {'exits': [], 'policy': policy}
    return {'exits': [_exit(r) for r in rows(
        f'SELECT {EXIT_COLS} FROM employee_exit x JOIN employee e ON e.id = x.employee_id WHERE x.employee_id = %s ORDER BY x.created_at DESC',
        [user.employee_id])], 'policy': policy}


RESIGN_BODY = Obj({'proposedLastWorkingDay': IsoDate(), 'reasonCategory': Str(trim=True, max=60, nullish=True),
                   'reason': Str(trim=True, max=2000, nullish=True)})


def resign(user: AuthUser, body: dict) -> dict:
    from hajiri.workflow.services import start_workflow
    company_id = company_id_of(user)
    employee_id = user.employee_id
    if not employee_id:
        raise BadRequestError('your account is not linked to an employee record')
    b = parse(RESIGN_BODY, body)
    e = employee_in(company_id, employee_id)
    if e['status'] != 'active':
        raise BadRequestError('your employment has already ended')
    today = npt_today()
    lwd = date.fromisoformat(b['proposedLastWorkingDay'])
    if lwd < today:
        raise BadRequestError('the last working day cannot be in the past')
    if diff_days(lwd, today) > 365:
        raise BadRequestError('the last working day must be within a year')
    policy = lifecycle_policy(company_id)
    try:
        with transaction.atomic():
            xid = str(one("""INSERT INTO employee_exit (company_id, employee_id, kind, voluntary, reason_category, reason, submitted_on, notice_days,
                                                        proposed_lwd, initiated_by)
                             VALUES (%s, %s, 'resignation', true, %s, %s, %s, %s, %s, %s) RETURNING id""",
                          [company_id, employee_id, b.get('reasonCategory'), b.get('reason'), today, policy['noticeDays'], lwd, user.id])['id'])
            start_workflow(company_id=company_id, object_type='resignation', object_id=xid, employee_id=employee_id, title='Resignation submitted',
                           body=f"{e['name_en']} has resigned. Proposed last working day {bs_label(lwd)}.")
            audit('resign', 'employee_exit', actor_id=user.id, row_id=xid, after={'proposedLwd': b['proposedLastWorkingDay']})
    except IntegrityError as ex:
        if _unique(ex):
            raise ConflictError('you already have a resignation in progress') from ex
        raise
    shortfall = max(0, policy['noticeDays'] - diff_days(lwd, today))
    return {'id': xid, 'noticeDays': policy['noticeDays'], 'shortfallDays': shortfall}


RECORD_EXIT = Obj({
    'employeeId': Uuid(), 'kind': Enum(EXIT_KINDS), 'lastWorkingDay': IsoDate(), 'voluntary': Bool(optional=True),
    'reasonCategory': Str(trim=True, max=60, nullish=True), 'reason': Str(trim=True, max=2000, nullish=True), 'templateId': Uuid(nullish=True),
})


def create_exit(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = parse(RECORD_EXIT, body)
    e = employee_in(company_id, b['employeeId'])
    if e['status'] != 'active':
        raise BadRequestError('this employee has already left')
    lwd = date.fromisoformat(b['lastWorkingDay'])
    if e['join_date'] and lwd < e['join_date']:
        raise BadRequestError('the last working day cannot be before the join date')
    if b['employeeId'] == user.employee_id:
        raise ForbiddenError('another HR user must record your own exit')
    voluntary = b['voluntary'] if 'voluntary' in b else b['kind'] in ('resignation', 'retirement')
    with transaction.atomic():
        return record_exit(company_id=company_id, employee_id=b['employeeId'], kind=b['kind'], voluntary=voluntary,
                           reason_category=b.get('reasonCategory'), reason=b.get('reason'), last_working_day=lwd, actor_id=user.id,
                           template_id=b.get('templateId'))


def load_exit(user: AuthUser, id_: str) -> dict:
    x = one('SELECT id, company_id, employee_id, status, kind, proposed_lwd, accepted_lwd FROM employee_exit WHERE id = %s', [parse_uuid(str(id_))])
    if not x or str(x['company_id']) != user.company_id:
        raise NotFoundError('exit not found')
    x = _s(x, 'id', 'company_id', 'employee_id')
    lwd = x['accepted_lwd'] or x['proposed_lwd']
    today = npt_today()
    as_of = lwd if lwd < today else today
    allowed = (has_role(user, 'hr') or user.employee_id == x['employee_id']
               or (bool(user.employee_id) and has_role(user, 'manager') and is_manager_of(user.employee_id, x['employee_id'], as_of)))
    if not allowed:
        raise NotFoundError('exit not found')
    return x


def exit_detail(user: AuthUser, id_: str) -> dict:
    x = load_exit(user, id_)
    row = _exit(one(f'SELECT {EXIT_COLS} FROM employee_exit x JOIN employee e ON e.id = x.employee_id WHERE x.id = %s', [x['id']]))
    return {'exit': row, 'blockers': clearance_blockers(x['id'], x['employee_id']),
            'settlement': settlement_status(x['company_id'], x['employee_id'], x['accepted_lwd'] or x['proposed_lwd']) if _is_hr(user) else None}


UPDATE_EXIT = Obj({'lastWorkingDay': IsoDate(optional=True), 'voluntary': Bool(optional=True),
                   'reasonCategory': Str(trim=True, max=60, nullish=True), 'reason': Str(trim=True, max=2000, nullish=True)})


def update_exit(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = parse(UPDATE_EXIT, body)
    if x['status'] not in ('pending', 'accepted'):
        raise ConflictError(f"exit is {x['status']}")
    with transaction.atomic():
        execute("""UPDATE employee_exit SET voluntary = coalesce(%s, voluntary), reason_category = CASE WHEN %s::boolean THEN %s ELSE reason_category END,
                          reason = CASE WHEN %s::boolean THEN %s ELSE reason END, accepted_lwd = coalesce(%s::date, accepted_lwd) WHERE id = %s""",
                [b.get('voluntary'), 'reasonCategory' in b, b.get('reasonCategory'), 'reason' in b, b.get('reason'), b.get('lastWorkingDay'), x['id']])
        if b.get('lastWorkingDay') and x['status'] == 'accepted':
            lwd = b['lastWorkingDay']
            execute('UPDATE employee SET exit_date = %s, updated_at = now() WHERE id = %s', [lwd, x['employee_id']])
            # Clearance due dates follow the new last working day.
            execute("""UPDATE lifecycle_task t SET due_date = t.due_date + (%s::date - c.anchor_date) FROM lifecycle_case c
                        WHERE c.id = t.case_id AND c.exit_id = %s AND c.status = 'active' AND t.due_date IS NOT NULL""", [lwd, x['id']])
            execute("UPDATE lifecycle_case SET anchor_date = %s WHERE exit_id = %s AND status = 'active'", [lwd, x['id']])
            own = user_id_for_employee(x['employee_id'])
            if own:
                notify(company_id=x['company_id'], user_id=own, employee_id=x['employee_id'], template='lifecycle.exit.lwd_changed',
                       title='Last working day changed', body=f'Your last working day is now {bs_label(lwd)}.', payload={'exitId': x['id']})
        audit('update', 'employee_exit', actor_id=user.id, row_id=x['id'], after=b)


def withdraw_exit(user: AuthUser, id_: str) -> None:
    from hajiri.workflow.services import cancel_workflow
    x = load_exit(user, id_)
    if x['employee_id'] != user.employee_id:
        raise ForbiddenError('only the employee can withdraw their resignation')
    if x['status'] != 'pending':
        raise ConflictError('your resignation was already accepted; ask HR to cancel it' if x['status'] == 'accepted'
                            else f"resignation is {x['status']}")
    with transaction.atomic():
        execute("UPDATE employee_exit SET status = 'withdrawn', decided_at = now() WHERE id = %s", [x['id']])
        cancel_workflow('resignation', x['id'])
        audit('withdraw', 'employee_exit', actor_id=user.id, row_id=x['id'])


CANCEL_EXIT = Obj({'reason': Str(trim=True, min=3, max=500)})


def cancel_exit(user: AuthUser, id_: str, body: dict) -> None:
    from hajiri.workflow.services import cancel_workflow
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = parse(CANCEL_EXIT, body)
    if x['status'] not in ('pending', 'accepted'):
        raise ConflictError(f"exit is {x['status']}")
    with transaction.atomic():
        execute("UPDATE employee_exit SET status = 'cancelled', decided_at = now(), reason = concat_ws(E'\\n', reason, %s::text) WHERE id = %s",
                [f"Cancelled: {b['reason']}", x['id']])
        cancel_workflow('resignation', x['id'])
        if x['status'] == 'accepted':
            execute('UPDATE employee SET exit_date = NULL, updated_at = now() WHERE id = %s AND exit_date = %s', [x['employee_id'], x['accepted_lwd']])
            execute("UPDATE lifecycle_case SET status = 'cancelled', completed_at = now() WHERE exit_id = %s AND status = 'active'", [x['id']])
        audit('cancel', 'employee_exit', actor_id=user.id, row_id=x['id'], after=b)


COMPLETE_EXIT = Obj({'skipOpenTasks': Bool(default=False)})


def complete(user: AuthUser, id_: str, body: dict | None = None) -> None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = parse(COMPLETE_EXIT, body or {})
    with transaction.atomic():
        complete_exit(x['id'], user.id, skip_open_tasks=b['skipOpenTasks'])


# Exit interview: questions are company-wide; answers are encrypted and only HR can read them.

def questions(user: AuthUser) -> list[dict]:
    return [_s(r, 'id') for r in rows(
        'SELECT id, sort, question, kind, options, active FROM exit_interview_question WHERE company_id = %s AND (active OR %s) ORDER BY sort',
        [company_id_of(user), _is_hr(user)])]


QUESTIONS_BODY = Obj({'questions': Arr(Obj({
    'id': Uuid(optional=True), 'question': Str(trim=True, min=1, max=300), 'kind': Enum(('text', 'rating', 'choice')),
    'options': Arr(Str(trim=True, min=1, max=80), max=12, default=list), 'active': Bool(default=True),
}, refine=(lambda q: q['kind'] != 'choice' or len(q['options']) >= 2, 'a choice question needs at least two options')), max=30)})


def save_questions(user: AuthUser, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = parse(QUESTIONS_BODY, body)
    with transaction.atomic():
        keep: list[str] = []
        for i, q in enumerate(b['questions']):
            if q.get('id'):
                u = one("""UPDATE exit_interview_question SET sort = %s, question = %s, kind = %s, options = %s, active = %s
                            WHERE id = %s AND company_id = %s RETURNING id""",
                        [i, q['question'], q['kind'], json.dumps(q['options']), q['active'], q['id'], company_id])
                if not u:
                    raise BadRequestError('question not found')
                keep.append(q['id'])
            else:
                ins = one("""INSERT INTO exit_interview_question (company_id, sort, question, kind, options, active)
                             VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                          [company_id, i, q['question'], q['kind'], json.dumps(q['options']), q['active']])
                keep.append(str(ins['id']))
        # Questions are never deleted (old answers refer to them); dropped ones are switched off.
        execute('UPDATE exit_interview_question SET active = false WHERE company_id = %s AND NOT (id = ANY(%s::uuid[]))', [company_id, keep])
        audit('update', 'exit_interview_question', actor_id=user.id, after={'count': len(b['questions'])})


def _interview_aad(exit_id: str) -> str:
    return f'exit_interview:{exit_id}'


def interview(user: AuthUser, id_: str) -> dict | None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    i = one("""SELECT i.answers_enc, i.by_employee, i.created_at, u.name AS recorded_by_name FROM exit_interview i
                 JOIN app_user u ON u.id = i.recorded_by WHERE i.exit_id = %s""", [x['id']])
    if not i:
        return None
    audit('read_exit_interview', 'exit_interview', actor_id=user.id, row_id=x['id'])
    return {'answers': json.loads(field_cipher().decrypt(bytes(i['answers_enc']), _interview_aad(x['id']))), 'byEmployee': i['by_employee'],
            'createdAt': i['created_at'], 'recordedBy': i['recorded_by_name']}


INTERVIEW_BODY = Obj({'answers': Arr(Obj({'questionId': Uuid(), 'answer': Union(Str(max=3000), Int(min=1, max=5))}), min=1, max=30)})


def record_interview(user: AuthUser, id_: str, body: dict) -> None:
    x = load_exit(user, id_)
    own = user.employee_id == x['employee_id']
    if not own and not _is_hr(user):
        raise ForbiddenError('only the leaver or HR can record the exit interview')
    if own and not lifecycle_policy(x['company_id'])['selfExitInterview']:
        raise ForbiddenError('HR will hold your exit interview in person')
    if x['status'] not in ('pending', 'accepted', 'completed'):
        raise ConflictError(f"exit is {x['status']}")
    b = parse(INTERVIEW_BODY, body)
    by_id = {str(q['id']): q for q in rows('SELECT id, question, kind, options FROM exit_interview_question WHERE company_id = %s', [x['company_id']])}
    answers = []
    for a in b['answers']:
        q = by_id.get(a['questionId'])
        if not q:
            raise BadRequestError('unknown question')
        ans = a['answer']
        if q['kind'] == 'rating' and not isinstance(ans, int):
            raise BadRequestError(f'"{q["question"]}" needs a rating from 1 to 5')
        if q['kind'] == 'choice' and (not isinstance(ans, str) or ans not in (q['options'] or [])):
            raise BadRequestError(f'"{q["question"]}" needs one of the listed options')
        if q['kind'] == 'text' and not isinstance(ans, str):
            raise BadRequestError(f'"{q["question"]}" needs a written answer')
        answers.append({'questionId': str(q['id']), 'question': q['question'], 'kind': q['kind'], 'answer': ans})
    with transaction.atomic():
        ins = one("""INSERT INTO exit_interview (exit_id, company_id, answers_enc, by_employee, recorded_by) VALUES (%s, %s, %s, %s, %s)
                     ON CONFLICT (exit_id) DO NOTHING RETURNING exit_id""",
                  [x['id'], x['company_id'], field_cipher().encrypt(json.dumps(answers, ensure_ascii=False), _interview_aad(x['id'])), own, user.id])
        if not ins:
            raise ConflictError('the exit interview has already been recorded')
        execute("""UPDATE lifecycle_task t SET status = 'done', done_by = %s, done_at = now() FROM lifecycle_case c
                    WHERE c.id = t.case_id AND c.exit_id = %s AND t.status = 'open' AND t.title ILIKE 'exit interview%%'""", [user.id, x['id']])
        audit('record_exit_interview', 'exit_interview', actor_id=user.id, row_id=x['id'], after={'byEmployee': own})


# ------------------------------------------------------------ assets

def asset_types(user: AuthUser) -> list[dict]:
    return [_s(r, 'id') for r in rows('SELECT id, name FROM asset_type WHERE company_id = %s ORDER BY name', [company_id_of(user)])]


def add_asset_type(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    b = parse(Obj({'name': Str(trim=True, min=1, max=80)}), body)
    try:
        with transaction.atomic():
            return _s(one('INSERT INTO asset_type (company_id, name) VALUES (%s, %s) RETURNING id, name', [company_id_of(user), b['name']]), 'id')
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('this asset type already exists') from e
        raise


ASSET_COLS = """a.id, a.tag, a.name, a.serial, a.condition, a.status, a.purchase_date, a.cost, a.notes, a.type_id, t.name AS type_name, a.created_at,
    g.id AS assignment_id, g.employee_id, g.assigned_on, e.name_en AS employee_name, e.emp_code"""


def _asset(r):
    return _s(r, 'id', 'type_id', 'assignment_id', 'employee_id')


def assets(user: AuthUser, query: dict | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    q = parse(Obj({'status': Enum(('available', 'assigned', 'retired'), optional=True), 'employeeId': Uuid(optional=True),
                   'search': Str(max=80, optional=True)}), query or {})
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'])
    elif not _is_hr(user):
        raise ForbiddenError('choose one of your team members')
    search = q.get('search')
    esc = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') if search else None
    return [_asset(r) for r in rows(
        f"""SELECT {ASSET_COLS} FROM asset a LEFT JOIN asset_type t ON t.id = a.type_id
              LEFT JOIN asset_assignment g ON g.asset_id = a.id AND g.returned_on IS NULL LEFT JOIN employee e ON e.id = g.employee_id
             WHERE a.company_id = %s AND (%s::text IS NULL OR a.status = %s) AND (%s::uuid IS NULL OR g.employee_id = %s)
               AND (%s::text IS NULL OR a.tag ILIKE '%%' || %s || '%%' OR a.name ILIKE '%%' || %s || '%%' OR a.serial ILIKE '%%' || %s || '%%')
             ORDER BY a.tag LIMIT 1000""",
        [company_id_of(user), q.get('status'), q.get('status'), q.get('employeeId'), q.get('employeeId'), esc, esc, esc, esc])]


def my_assets(user: AuthUser) -> list[dict]:
    if not user.employee_id:
        return []
    return [_asset(r) for r in rows(
        f"""SELECT {ASSET_COLS} FROM asset_assignment g JOIN asset a ON a.id = g.asset_id LEFT JOIN asset_type t ON t.id = a.type_id
              JOIN employee e ON e.id = g.employee_id WHERE g.employee_id = %s AND g.returned_on IS NULL ORDER BY g.assigned_on""",
        [user.employee_id])]


def asset_history(user: AuthUser, id_: str) -> list[dict]:
    _require(user, 'hr')
    return [_s(r, 'id', 'employee_id') for r in rows(
        """SELECT g.id, g.employee_id, e.name_en, e.emp_code, g.assigned_on, g.returned_on, g.condition_out, g.condition_in, g.notes
             FROM asset_assignment g JOIN employee e ON e.id = g.employee_id WHERE g.asset_id = %s AND g.company_id = %s ORDER BY g.assigned_on DESC""",
        [parse_uuid(str(id_)), company_id_of(user)])]


ASSET_BODY = Obj({
    'tag': Str(trim=True, min=1, max=40), 'name': Str(trim=True, min=1, max=120), 'typeId': Uuid(nullish=True),
    'serial': Str(trim=True, max=80, nullish=True), 'condition': Enum(CONDITIONS, default='good'), 'purchaseDate': IsoDate(nullish=True),
    'cost': Money(nullish=True), 'notes': Str(max=1000, nullish=True),
})


def _check_type(company_id: str, type_id) -> None:
    if type_id and not one('SELECT 1 AS x FROM asset_type WHERE id = %s AND company_id = %s', [type_id, company_id]):
        raise BadRequestError('asset type not found')


def create_asset(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = parse(ASSET_BODY, body)
    _check_type(company_id, b.get('typeId'))
    try:
        with transaction.atomic():
            aid = str(one("""INSERT INTO asset (company_id, type_id, tag, name, serial, condition, purchase_date, cost, notes)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                          [company_id, b.get('typeId'), b['tag'], b['name'], b.get('serial'), b['condition'], b.get('purchaseDate'),
                           b.get('cost'), b.get('notes')])['id'])
            audit('create', 'asset', actor_id=user.id, row_id=aid, after=b)
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('an asset with this tag already exists') from e
        raise
    return aid


def update_asset(user: AuthUser, id_: str, body: dict) -> None:
    from copy import copy
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    fields = {}
    for k, f in ASSET_BODY.fields.items():
        f2 = copy(f)
        f2.optional, f2.default = True, MISSING
        fields[k] = f2
    fields['status'] = Enum(('available', 'retired'), optional=True)
    b = parse(Obj(fields), body)
    _check_type(company_id, b.get('typeId'))
    cur = one('SELECT status FROM asset WHERE id = %s AND company_id = %s', [id_, company_id])
    if not cur:
        raise NotFoundError('asset not found')
    if b.get('status') and cur['status'] == 'assigned':
        raise ConflictError('record the return before changing the status of this asset')
    cols = [('tag', 'tag'), ('name', 'name'), ('typeId', 'type_id'), ('serial', 'serial'), ('condition', 'condition'),
            ('purchaseDate', 'purchase_date'), ('cost', 'cost'), ('notes', 'notes'), ('status', 'status')]
    sets = [(col, b[k]) for k, col in cols if k in b]
    try:
        with transaction.atomic():
            if sets:
                execute(f"UPDATE asset SET {', '.join(f'{c} = %s' for c, _ in sets)} WHERE id = %s", [v for _, v in sets] + [id_])
            audit('update', 'asset', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('an asset with this tag already exists') from e
        raise


ASSIGN_BODY = Obj({'employeeId': Uuid(), 'assignedOn': IsoDate(optional=True), 'notes': Str(max=500, nullish=True)})


def assign_asset(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(ASSIGN_BODY, body)
    e = employee_in(company_id, b['employeeId'])
    if e['status'] != 'active':
        raise BadRequestError('this employee has left')
    with transaction.atomic():
        asset = one('SELECT status, condition, name, tag FROM asset WHERE id = %s AND company_id = %s FOR UPDATE', [id_, company_id])
        if not asset:
            raise NotFoundError('asset not found')
        if asset['status'] != 'available':
            raise ConflictError('this asset is already with someone; record the return first' if asset['status'] == 'assigned'
                                else 'this asset is retired')
        execute("""INSERT INTO asset_assignment (asset_id, company_id, employee_id, assigned_on, condition_out, notes, assigned_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                [id_, company_id, b['employeeId'], b.get('assignedOn') or npt_today(), asset['condition'], b.get('notes'), user.id])
        execute("UPDATE asset SET status = 'assigned' WHERE id = %s", [id_])
        audit('assign', 'asset', actor_id=user.id, row_id=id_, after={'employeeId': b['employeeId']})
        own = user_id_for_employee(b['employeeId'])
        if own:
            notify(company_id=company_id, user_id=own, employee_id=b['employeeId'], template='lifecycle.asset.assigned',
                   title='Company asset handed to you', body=f"{asset['name']} ({asset['tag']}). Please return it when you leave.",
                   payload={'assetId': id_})


RETURN_BODY = Obj({'returnedOn': IsoDate(optional=True), 'condition': Enum(CONDITIONS), 'notes': Str(max=500, nullish=True)})


def return_asset(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(RETURN_BODY, body)
    with transaction.atomic():
        g = one("""SELECT g.id, g.assigned_on, g.employee_id FROM asset_assignment g JOIN asset a ON a.id = g.asset_id
                    WHERE g.asset_id = %s AND a.company_id = %s AND g.returned_on IS NULL FOR UPDATE OF g""", [id_, company_id])
        if not g:
            raise ConflictError('this asset is not with anyone')
        on = date.fromisoformat(b['returnedOn']) if b.get('returnedOn') else npt_today()
        if on < g['assigned_on']:
            raise BadRequestError('the return date cannot be before it was handed over')
        execute('UPDATE asset_assignment SET returned_on = %s, condition_in = %s, notes = coalesce(%s, notes), returned_by = %s WHERE id = %s',
                [on, b['condition'], b.get('notes'), user.id, g['id']])
        # A lost asset leaves service; anything else is available again.
        execute("UPDATE asset SET status = CASE WHEN %s = 'lost' THEN 'retired' ELSE 'available' END, condition = %s WHERE id = %s",
                [b['condition'], b['condition'], id_])
        audit('return', 'asset', actor_id=user.id, row_id=id_,
              after={'employeeId': str(g['employee_id']), 'condition': b['condition'], 'returnedOn': on.isoformat()})


# ------------------------------------------------------------ approval inbox summaries

def probation_summary(inst: dict) -> dict:
    x = one('SELECT rating, recommendation, extend_to, last_working_day, probation_end, comments FROM probation_review WHERE id = %s', [inst['object_id']])
    if not x:
        return {}
    if x['recommendation'] == 'confirm':
        rec = 'confirm'
    elif x['recommendation'] == 'extend':
        rec = f"extend probation to {bs_label(x['extend_to'])}"
    else:
        rec = f"end employment on {bs_label(x['last_working_day'])}"
    detail = f"Rating {x['rating']} of 5" + (f", probation ends {bs_label(x['probation_end'])}" if x['probation_end'] else '')
    return {'headline': f'Probation review: {rec}', 'detail': detail, 'note': x['comments'], **x}


def resignation_summary(inst: dict) -> dict:
    x = one('SELECT proposed_lwd, accepted_lwd, reason, reason_category, notice_days, submitted_on FROM employee_exit WHERE id = %s', [inst['object_id']])
    if not x:
        return {}
    lwd = x['accepted_lwd'] or x['proposed_lwd']
    shortfall = max(0, x['notice_days'] - diff_days(lwd, x['submitted_on']))
    return {
        'headline': f'Resignation, last working day {bs_label(lwd)}',
        'detail': f"Notice short by {shortfall} {'day' if shortfall == 1 else 'days'}" if shortfall else 'Full notice given',
        'note': ': '.join(p for p in (x['reason_category'], x['reason']) if p) or None, **x,
    }


def _card(item: dict) -> dict:
    s = item.get('summary') or {}
    extra = [s['detail']] if s.get('detail') else []
    if s.get('note'):
        extra.append(f"“{s['note']}”")
    return {'main': s.get('headline') or '', 'extra': extra}


def register() -> None:
    """Decision handlers, inbox summaries and cards for probation reviews and resignations (LifecycleConfig.ready)."""
    from hajiri.workflow.services import register_card, register_handler, register_summary
    register_handler('probation_review', svc.apply_probation_decision)
    register_handler('resignation', svc.apply_resignation_decision)
    register_summary('probation_review', probation_summary)
    register_summary('resignation', resignation_summary)
    register_card('probation_review', _card)
    register_card('resignation', _card)
