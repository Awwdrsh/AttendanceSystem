"""
Approval flow set-up (port of apps/api/src/modules/workflow/flow-routes.ts): default chain per request type,
conditional flows, user groups, approver delegation, a preview of who approves a request for someone, and a check
that every employee's chains reach a real person. Bodies use the TS JSON shape (camelCase keys).
"""
from __future__ import annotations

import json
from datetime import date

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid

from .flows import (CONDITION_KEYS, CONDITIONS_SCHEMA, REQUEST_TYPE_SET, REQUEST_TYPES, choose_flow, empty_conditions, fell_back_to_hr,
                    kind_of, normalise_step, parse_steps, resolve_step, step_label, steps_for_amount, visible_notes)
from .service import assert_manages, npt_today


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def parse_type(t) -> str:
    if not isinstance(t, str) or t not in REQUEST_TYPE_SET:
        from hajiri.core.errors import ValidationError
        raise ValidationError([{'path': '', 'message': 'unknown request type'}])
    return t


def check_step_refs(company_id: str, steps: list[dict]) -> None:
    for s in steps:
        if s.get('userId') and not one('SELECT 1 AS x FROM app_user WHERE id = %s AND company_id = %s', [s['userId'], company_id]):
            raise BadRequestError('the approver chosen is not a user of your company')
        if s.get('groupId') and not one('SELECT 1 AS x FROM user_group WHERE id = %s AND company_id = %s', [s['groupId'], company_id]):
            raise BadRequestError('the approver group does not exist')


_CHECKS = (('employee', 'employeeIds', 'employees'), ('user_group', 'groupIds', 'groups'),
           ('designation', 'designationIds', 'designations'), ('org_unit', 'orgUnitIds', 'departments'),
           ('branch', 'branchIds', 'branches'))


def check_condition_refs(company_id: str, c: dict) -> None:
    for table, key, label in _CHECKS:
        ids = c.get(key) or []
        if not ids:
            continue
        n = one(f'SELECT count(*)::int AS n FROM {table} WHERE id = ANY(%s::uuid[]) AND company_id = %s', [list(ids), company_id])['n']
        if n != len(set(ids)):
            raise BadRequestError(f'one of the {label} is not in your company')


def names(ids: list[str]) -> list[dict]:
    """Names of the people a resolution points to, for previews and the verify report."""
    if not ids:
        return []
    return [{'id': str(r['id']), 'name': r['name']} for r in
            rows('SELECT id, name FROM app_user WHERE id = ANY(%s::uuid[]) ORDER BY name', [list(ids)])]


def _flow_row(r: dict) -> dict:
    return {**r, 'id': str(r['id'])}


# ---------------------------------------------------------------- configuration

def flows_config(user: AuthUser) -> dict:
    """GET /workflow/flows/config: every request type with its default chain and flows, and the user groups."""
    _require_hr(user)
    company_id = company_id_of(user)
    defs = {d['object_type']: d for d in rows(
        'SELECT object_type, steps, sla_hours, escalation_hours FROM workflow_definition WHERE company_id = %s', [company_id])}
    flows = [_flow_row(f) for f in rows(
        """SELECT id, object_type, name, conditions, steps, priority, active, updated_at FROM approval_flow
            WHERE company_id = %s ORDER BY object_type, priority DESC, created_at""", [company_id])]
    groups = rows('SELECT id, name, description FROM user_group WHERE company_id = %s ORDER BY name', [company_id])
    members = rows("""SELECT m.group_id, m.user_id, u.name FROM user_group_member m JOIN user_group g ON g.id = m.group_id
                        JOIN app_user u ON u.id = m.user_id WHERE g.company_id = %s ORDER BY u.name""", [company_id])
    return {
        'types': [{
            'objectType': t.type, 'label': t.label, 'money': t.money,
            'defaultSteps': (defs.get(t.type) or {}).get('steps') or [{'kind': 'manager', 'role': 'manager'}],
            'flows': [f for f in flows if f['object_type'] == t.type],
        } for t in REQUEST_TYPES],
        'groups': [{'id': str(g['id']), 'name': g['name'], 'description': g['description'],
                    'members': [{'id': str(m['user_id']), 'name': m['name']} for m in members if m['group_id'] == g['id']]}
                   for g in groups],
    }


@transaction.atomic
def set_default(user: AuthUser, object_type: str, body: dict) -> dict:
    """PUT /workflow/flows/defaults/:type {steps}."""
    _require_hr(user)
    company_id = company_id_of(user)
    t = parse_type(object_type)
    steps = [normalise_step(s) for s in parse_steps((body or {}).get('steps'), '')]
    check_step_refs(company_id, steps)
    before = one('SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = %s', [company_id, t])
    execute("""INSERT INTO workflow_definition (company_id, object_type, steps) VALUES (%s, %s, %s)
               ON CONFLICT (company_id, object_type) DO UPDATE SET steps = EXCLUDED.steps""", [company_id, t, json.dumps(steps)])
    audit('update', 'workflow_definition', actor_id=user.id, row_id=None, before=before,
          after={'objectType': t, 'steps': steps})
    return {'objectType': t, 'steps': steps}


def _parse_flow(body: dict, partial: bool) -> dict:
    """flowBody (without objectType); `partial` for PATCH, where nothing has a default."""
    body = body if isinstance(body, dict) else {}
    if partial:
        schema = Obj({
            'name': Str(trim=True, min=1, max=100, optional=True),
            'conditions': Obj(CONDITIONS_SCHEMA.fields, optional=True),
            'priority': Int(min=-100, max=100, optional=True),
            'active': Bool(optional=True),
        })
    else:
        schema = Obj({
            'name': Str(trim=True, min=1, max=100),
            'conditions': Obj(CONDITIONS_SCHEMA.fields, default=empty_conditions),
            'priority': Int(min=-100, max=100, default=0),
            'active': Bool(default=True),
        })
    out = parse(schema, {k: v for k, v in body.items() if k != 'steps'})
    if 'steps' in body or not partial:
        out['steps'] = parse_steps(body.get('steps'))
    if 'conditions' in out:
        out['conditions'] = {k: out['conditions'].get(k, []) for k in CONDITION_KEYS}
    return out


@transaction.atomic
def create_flow(user: AuthUser, body: dict) -> dict:
    """POST /workflow/flows -> {'id'}."""
    _require_hr(user)
    company_id = company_id_of(user)
    body = body if isinstance(body, dict) else {}
    t = parse_type(body.get('objectType'))
    b = _parse_flow(body, partial=False)
    steps = [normalise_step(s) for s in b['steps']]
    check_step_refs(company_id, steps)
    check_condition_refs(company_id, b['conditions'])
    r = one("""INSERT INTO approval_flow (company_id, object_type, name, conditions, steps, priority, active, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            [company_id, t, b['name'], json.dumps(b['conditions']), json.dumps(steps), b['priority'], b['active'], user.id])
    fid = str(r['id'])
    audit('create', 'approval_flow', actor_id=user.id, row_id=fid, after={'objectType': t, **b, 'steps': steps})
    return {'id': fid}


@transaction.atomic
def update_flow(user: AuthUser, flow_id: str, body: dict) -> dict:
    """PATCH /workflow/flows/:id (any of name, conditions, steps, priority, active)."""
    _require_hr(user)
    company_id = company_id_of(user)
    fid = parse_uuid(str(flow_id))
    cur = one('SELECT * FROM approval_flow WHERE id = %s AND company_id = %s', [fid, company_id])
    if not cur:
        raise NotFoundError('flow not found')
    b = _parse_flow(body, partial=True)
    steps = [normalise_step(s) for s in b['steps']] if 'steps' in b else None
    if steps:
        check_step_refs(company_id, steps)
    if 'conditions' in b:
        check_condition_refs(company_id, b['conditions'])
    execute("""UPDATE approval_flow SET name = coalesce(%s, name), conditions = coalesce(%s::jsonb, conditions),
                      steps = coalesce(%s::jsonb, steps), priority = coalesce(%s, priority), active = coalesce(%s, active),
                      updated_at = now() WHERE id = %s AND company_id = %s""",
            [b.get('name'), json.dumps(b['conditions']) if 'conditions' in b else None, json.dumps(steps) if steps else None,
             b.get('priority'), b.get('active'), fid, company_id])
    after = {k: v for k, v in b.items() if k != 'steps'}
    if steps:
        after['steps'] = steps
    audit('update', 'approval_flow', actor_id=user.id, row_id=fid, before=cur, after=after)
    return {'ok': True}


@transaction.atomic
def delete_flow(user: AuthUser, flow_id: str) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    fid = parse_uuid(str(flow_id))
    cur = one('DELETE FROM approval_flow WHERE id = %s AND company_id = %s RETURNING *', [fid, company_id])
    if not cur:
        raise NotFoundError('flow not found')
    audit('delete', 'approval_flow', actor_id=user.id, row_id=fid, before=cur)
    return {'ok': True}


def get_flow(user: AuthUser, flow_id: str) -> dict:
    _require_hr(user)
    r = one('SELECT * FROM approval_flow WHERE id = %s AND company_id = %s', [parse_uuid(str(flow_id)), company_id_of(user)])
    if not r:
        raise NotFoundError('flow not found')
    return _flow_row(r)


# ---------------------------------------------------------------- user groups

def _set_members(company_id: str, group_id: str, user_ids: list[str]) -> None:
    ids = list(dict.fromkeys(user_ids))
    if ids:
        n = one('SELECT count(*)::int AS n FROM app_user WHERE id = ANY(%s::uuid[]) AND company_id = %s', [ids, company_id])['n']
        if n != len(ids):
            raise BadRequestError('one of the members is not a user of your company')
    execute('DELETE FROM user_group_member WHERE group_id = %s', [group_id])
    if ids:
        execute('INSERT INTO user_group_member (group_id, user_id) SELECT %s, unnest(%s::uuid[])', [group_id, ids])


@transaction.atomic
def create_group(user: AuthUser, body: dict) -> dict:
    """POST /workflow/groups {name, description?, userIds?} -> {'id'}."""
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(Obj({'name': Str(trim=True, min=1, max=80), 'description': Str(trim=True, max=300, nullish=True),
                   'userIds': Arr(Uuid(), max=500, default=list)}), body if body is not None else {})
    g = one('INSERT INTO user_group (company_id, name, description) VALUES (%s, %s, %s) RETURNING id',
            [company_id, b['name'], b.get('description')])
    gid = str(g['id'])
    _set_members(company_id, gid, b['userIds'])
    audit('create', 'user_group', actor_id=user.id, row_id=gid, after=b)
    return {'id': gid}


@transaction.atomic
def update_group(user: AuthUser, group_id: str, body: dict) -> dict:
    """PATCH /workflow/groups/:id {name?, description?, userIds?}."""
    _require_hr(user)
    company_id = company_id_of(user)
    gid = parse_uuid(str(group_id))
    b = parse(Obj({'name': Str(trim=True, min=1, max=80, optional=True), 'description': Str(trim=True, max=300, nullish=True),
                   'userIds': Arr(Uuid(), max=500, optional=True)}), body if body is not None else {})
    if not one('SELECT id FROM user_group WHERE id = %s AND company_id = %s FOR UPDATE', [gid, company_id]):
        raise NotFoundError('group not found')
    execute('UPDATE user_group SET name = coalesce(%s, name), description = CASE WHEN %s THEN %s ELSE description END WHERE id = %s',
            [b.get('name'), 'description' in b, b.get('description'), gid])
    if 'userIds' in b:
        _set_members(company_id, gid, b['userIds'])
    audit('update', 'user_group', actor_id=user.id, row_id=gid, after=b)
    return {'ok': True}


@transaction.atomic
def delete_group(user: AuthUser, group_id: str) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    gid = parse_uuid(str(group_id))
    used = one("""SELECT name FROM approval_flow WHERE company_id = %s AND (steps @> %s::jsonb OR conditions->'groupIds' ? %s)
                  UNION ALL SELECT object_type FROM workflow_definition WHERE company_id = %s AND steps @> %s::jsonb LIMIT 1""",
               [company_id, json.dumps([{'groupId': gid}]), gid, company_id, json.dumps([{'groupId': gid}])])
    if used:
        raise BadRequestError(f"this group is used in an approval flow ({str(used['name']).replace('_', ' ')}); change the flow first")
    cur = one('DELETE FROM user_group WHERE id = %s AND company_id = %s RETURNING *', [gid, company_id])
    if not cur:
        raise NotFoundError('group not found')
    audit('delete', 'user_group', actor_id=user.id, row_id=gid, before=cur)
    return {'ok': True}


# ---------------------------------------------------------------- delegation

def list_delegates(user: AuthUser, all_: bool = False) -> list[dict]:
    """My delegations (and, for HR with all_, everyone's): while active, the delegate also receives and decides my approvals."""
    company_id = company_id_of(user)
    every = has_role(user, 'hr') and all_
    found = rows("""SELECT d.id, d.user_id, u.name AS user_name, d.delegate_id, du.name AS delegate_name, d.from_date, d.to_date,
                           d.note, d.cancelled_at, d.created_at
                      FROM approver_delegate d JOIN app_user u ON u.id = d.user_id JOIN app_user du ON du.id = d.delegate_id
                     WHERE d.company_id = %s AND (%s OR d.user_id = %s OR d.delegate_id = %s) AND d.cancelled_at IS NULL
                       AND d.to_date >= %s::date - 30
                     ORDER BY d.from_date DESC LIMIT 200""", [company_id, every, user.id, user.id, npt_today()])
    return [{**r, 'id': str(r['id']), 'user_id': str(r['user_id']), 'delegate_id': str(r['delegate_id'])} for r in found]


def delegate_candidates(user: AuthUser) -> list[dict]:
    """Colleagues who can be chosen as a delegate (active users of the company; names only)."""
    return [{'id': str(r['id']), 'name': r['name']} for r in rows(
        "SELECT id, name FROM app_user WHERE company_id = %s AND status = 'active' AND id <> %s ORDER BY name LIMIT 1000",
        [company_id_of(user), user.id])]


@transaction.atomic
def add_delegate(user: AuthUser, body: dict) -> dict:
    """POST /workflow/delegates {userId?, delegateId, from, to, note?} -> {'id'}."""
    company_id = company_id_of(user)
    b = parse(Obj({'userId': Uuid(optional=True), 'delegateId': Uuid(), 'from': IsoDate(), 'to': IsoDate(),
                   'note': Str(trim=True, max=300, optional=True)}), body if body is not None else {})
    user_id = b.get('userId') or user.id
    if user_id != user.id and not has_role(user, 'hr'):
        raise ForbiddenError('only HR can set a delegate for someone else')
    if b['to'] < b['from']:
        raise BadRequestError('the end date is before the start date')
    if b['to'] < npt_today():
        raise BadRequestError('the delegation has already ended')
    if user_id == b['delegateId']:
        raise BadRequestError('choose someone else as the delegate')
    n = one("SELECT count(*)::int AS n FROM app_user WHERE id = ANY(%s::uuid[]) AND company_id = %s AND status = 'active'",
            [[user_id, b['delegateId']], company_id])['n']
    if n != 2:
        raise BadRequestError('the delegate must be an active user of your company')
    r = one("""INSERT INTO approver_delegate (company_id, user_id, delegate_id, from_date, to_date, note, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            [company_id, user_id, b['delegateId'], b['from'], b['to'], b.get('note'), user.id])
    did = str(r['id'])
    audit('create', 'approver_delegate', actor_id=user.id, row_id=did, after={**b, 'userId': user_id})
    return {'id': did}


@transaction.atomic
def cancel_delegate(user: AuthUser, delegate_id: str) -> dict:
    company_id = company_id_of(user)
    did = parse_uuid(str(delegate_id))
    cur = one('SELECT user_id FROM approver_delegate WHERE id = %s AND company_id = %s AND cancelled_at IS NULL', [did, company_id])
    if not cur:
        raise NotFoundError('delegation not found')
    if str(cur['user_id']) != user.id and not has_role(user, 'hr'):
        raise NotFoundError('delegation not found')
    execute('UPDATE approver_delegate SET cancelled_at = now() WHERE id = %s', [did])
    audit('cancel', 'approver_delegate', actor_id=user.id, row_id=did)
    return {'ok': True}


def active_delegation(user: AuthUser) -> dict | None:
    """My delegation covering today (DelegateApprovals.tsx button label)."""
    today = date.fromisoformat(npt_today())
    for d in list_delegates(user):
        if d['user_id'] == user.id and d['from_date'] <= today <= d['to_date']:
            return d
    return None


# ---------------------------------------------------------------- preview and verify

def preview(user: AuthUser, query: dict) -> dict:
    """
    GET /workflow/preview {objectType, employeeId?, amount?}: who will approve request type X for employee Y (default:
    me), step by step -> {'flow': {id, name} | None, 'steps': [{label, minAmount, approvers: [{id, name}], notes, fallback}]}.
    """
    company_id = company_id_of(user)
    q = parse(Obj({'objectType': Str(), 'employeeId': Uuid(optional=True), 'amount': Num(min=0, max=1e10, coerce=True, optional=True)}),
              query or {})
    object_type = parse_type(q['objectType'])
    employee_id = q.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    if employee_id != user.employee_id:
        if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id]):
            raise NotFoundError('employee not found')
        assert_manages(user, employee_id)
    elif not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id]):
        raise NotFoundError('employee not found')
    flow = choose_flow(company_id, object_type, employee_id)
    steps = steps_for_amount(flow.steps, q['amount']) if 'amount' in q else flow.steps
    today = npt_today()
    out = []
    for s in steps:
        res = resolve_step(company_id, employee_id, s, today)
        out.append({'label': step_label(s), 'minAmount': s.get('minAmount'), 'approvers': names(res.users),
                    'notes': visible_notes(res), 'fallback': fell_back_to_hr(res)})
    return {'flow': {'id': flow.flow_id, 'name': flow.flow_name} if flow.flow_id else None, 'steps': out}


_SUGGESTION = {
    'manager': 'Set a line manager on the employee record',
    'manager2': "Set a manager for the employee's manager",
    'department_head': 'Set a head for the department (Employees → Org structure)',
    'branch_head': 'Set a head for the branch (Employees → Org structure)',
    'group': 'Add active members to the group',
    'user': 'Choose an active user for this step',
}


def verify(user: AuthUser, query: dict | None = None) -> dict:
    """
    GET /workflow/verify {objectType?}: every active employee x request type whose chain does not reach a real
    approver (a step with nobody set, so HR gets it by default), with a fix per problem -> {'checked', 'problems'}.
    """
    _require_hr(user)
    company_id = company_id_of(user)
    q = query or {}
    ot = q.get('objectType') or None
    if ot is not None:
        parse_type(ot)
    emps = rows("SELECT id, emp_code, name_en FROM employee WHERE company_id = %s AND status = 'active' ORDER BY emp_code", [company_id])
    types = [t for t in REQUEST_TYPES if t.subject == 'employee' and (not ot or t.type == ot)]
    today = npt_today()
    cache: dict = {}
    problems = []
    for e in emps:
        eid = str(e['id'])
        for t in types:
            flow = choose_flow(company_id, t.type, eid)
            for i, s in enumerate(flow.steps):
                key = f'{eid}|{json.dumps(s)}'
                res = cache.get(key)
                if res is None:
                    res = resolve_step(company_id, eid, s, today)
                    cache[key] = res
                notes = visible_notes(res)
                if not fell_back_to_hr(res) and not any(n.startswith('No ') for n in notes):
                    continue
                problems.append({
                    'employeeId': eid, 'empCode': e['emp_code'], 'name': e['name_en'], 'objectType': t.type,
                    'typeLabel': t.label, 'flow': flow.flow_name, 'step': i + 1, 'stepLabel': step_label(s),
                    'problem': '; '.join(notes) or 'Nobody can approve',
                    'suggestion': _SUGGESTION.get(kind_of(s), 'Give the role to at least one active user'),
                })
    return {'checked': len(emps) * len(types), 'problems': problems}
