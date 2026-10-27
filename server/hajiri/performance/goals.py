"""
Goals and key results with check-ins, and the KPI library (port of apps/api/src/modules/performance/goals.ts).
Bodies use the TS camelCase shape and are validated with the same rules as the zod schemas.
"""
from __future__ import annotations

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, user_id_for_employee

from .common import (SUBORDINATES_CTE, can_see_employee, direct_manager, employee_in_company, is_above_in_chain, js_round,
                     num, recompute_goal_progress)

MEASURES = ('number', 'percent', 'currency', 'boolean')
GOAL_STATUSES = ('draft', 'active', 'completed', 'cancelled')


def _num(**kw):
    return Num(min=-1e13, max=1e13, coerce=True, **kw)


def key_result_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'title': Str(trim=True, min=1, max=200, **o),
        'measure': Enum(MEASURES, **(o or {'default': 'number'})),
        'startValue': _num(**(o or {'default': 0})),
        'targetValue': _num(**(o or {'default': 100})),
        'currentValue': _num(optional=True),
        'unit': Str(trim=True, max=20, nullish=True),
        'weight': Num(min=0, max=1000, coerce=True, refine=(lambda v: v > 0, 'Too small: expected number to be >0'),
                      **(o or {'default': 1})),
    }


KEY_RESULT_BODY = Obj(key_result_fields())


def _goal_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'parentId': Uuid(nullish=True),
        'title': Str(trim=True, min=1, max=200, **o),
        'description': Str(trim=True, max=4000, nullish=True),
        'weight': Num(min=0, max=100, coerce=True, **(o or {'default': 0})),
        'startDate': IsoDate(nullish=True),
        'dueDate': IsoDate(nullish=True),
    }


GOAL_BODY = Obj({
    'scope': Enum(('company', 'department', 'individual'), default='individual'),
    'employeeId': Uuid(optional=True),
    'orgUnitId': Uuid(optional=True),
    **_goal_fields(),
    'status': Enum(('draft', 'active'), default='active'),
    'keyResults': Arr(KEY_RESULT_BODY, max=20, default=list),
})
# PATCH: only the fields sent change (the zod partial() also filled in defaults, which reset the weight to 0 on every
# status change; that was never the intent).
GOAL_PATCH = Obj({
    **_goal_fields(partial=True),
    'status': Enum(GOAL_STATUSES, optional=True),
    'progress': Num(min=0, max=100, coerce=True, optional=True),
})

CHECKIN_BODY = Obj({
    'keyResultId': Uuid(optional=True),
    'value': _num(optional=True),
    'progress': Num(min=0, max=100, coerce=True, optional=True),
    'confidence': Int(min=1, max=3, coerce=True, optional=True),
    'comment': Str(trim=True, max=2000, optional=True),
}, refine=(lambda x: x.get('value') is not None or x.get('progress') is not None or bool(x.get('comment')),
           'add a value, progress or a comment'))


def _kpi_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'designation': Str(trim=True, max=120, nullish=True),
        'title': Str(trim=True, min=1, max=200, **o),
        'description': Str(trim=True, max=2000, nullish=True),
        'measure': Enum(MEASURES, **(o or {'default': 'number'})),
        'defaultTarget': _num(**(o or {'default': 100})),
        'unit': Str(trim=True, max=20, nullish=True),
        'active': Bool(**(o or {'default': True})),
    }


KPI_BODY = Obj(_kpi_fields())
KPI_PATCH = Obj(_kpi_fields(partial=True))
APPLY_KPI_BODY = Obj({'employeeId': Uuid(optional=True), 'dueDate': IsoDate(nullish=True),
                      'weight': Num(min=0, max=100, coerce=True, default=0), 'target': _num(optional=True)})


def load_goal(company_id: str, goal_id: str) -> dict:
    r = one('SELECT id, company_id, scope, org_unit_id, employee_id, status, title FROM perf_goal WHERE id = %s AND company_id = %s',
            [goal_id, company_id])
    if not r:
        raise NotFoundError('goal not found')
    return {**r, 'id': str(r['id']), 'employee_id': r['employee_id'] and str(r['employee_id']),
            'org_unit_id': r['org_unit_id'] and str(r['org_unit_id'])}


def _current_org_unit(employee_id: str | None) -> str | None:
    if not employee_id:
        return None
    r = one('SELECT org_unit_id FROM employee_assignment WHERE employee_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date', [employee_id])
    return str(r['org_unit_id']) if r and r['org_unit_id'] else None


def can_view_goal(user: AuthUser, g: dict) -> bool:
    """Company and department goals are visible company-wide; individual goals to the owner, their manager chain and HR."""
    if g['scope'] != 'individual':
        return True
    return can_see_employee(user, g['employee_id'])


def can_edit_goal(user: AuthUser, g: dict) -> bool:
    """Company goals: HR. Department goals: HR or a manager in that department. Individual: owner, manager chain, HR."""
    if has_role(user, 'hr'):
        return True
    if g['scope'] == 'company':
        return False
    if g['scope'] == 'department':
        return has_role(user, 'manager') and _current_org_unit(user.employee_id) == (g['org_unit_id'] and str(g['org_unit_id']))
    return user.employee_id == (g['employee_id'] and str(g['employee_id'])) or is_above_in_chain(user, g['employee_id'])


def _assert_can_edit(user: AuthUser, g: dict, what: str = 'change this goal') -> None:
    """Not visible: 404 (no probing); visible but read-only: 403."""
    if not can_view_goal(user, g):
        raise NotFoundError('goal not found')
    if not can_edit_goal(user, g):
        raise ForbiddenError(f'not allowed to {what}')


GOAL_SELECT = """
  SELECT g.id, g.scope, g.org_unit_id, ou.name_en AS org_unit_name, g.employee_id, e.name_en AS employee_name, e.emp_code,
         g.parent_id, p.title AS parent_title, g.title, g.description, g.weight, g.start_date, g.due_date, g.status, g.progress,
         g.created_at, g.updated_at,
         coalesce((SELECT json_agg(json_build_object('id', k.id, 'title', k.title, 'measure', k.measure, 'start_value', k.start_value,
                   'target_value', k.target_value, 'current_value', k.current_value, 'unit', k.unit, 'weight', k.weight) ORDER BY k.sort, k.created_at)
                   FROM perf_key_result k WHERE k.goal_id = g.id), '[]') AS key_results,
         (SELECT json_build_object('created_at', c.created_at, 'confidence', c.confidence, 'comment', c.comment)
            FROM perf_checkin c WHERE c.goal_id = g.id ORDER BY c.created_at DESC LIMIT 1) AS last_checkin
    FROM perf_goal g
    LEFT JOIN org_unit ou ON ou.id = g.org_unit_id
    LEFT JOIN employee e ON e.id = g.employee_id
    LEFT JOIN perf_goal p ON p.id = g.parent_id"""


def _goal_rows(sql: str, params: list) -> list[dict]:
    out = rows(sql, params)
    for g in out:
        g['id'] = str(g['id'])
        g['employee_id'] = g['employee_id'] and str(g['employee_id'])
        g['parent_id'] = g['parent_id'] and str(g['parent_id'])
        g['org_unit_id'] = g['org_unit_id'] and str(g['org_unit_id'])
        if isinstance(g['key_results'], str):
            import json
            g['key_results'] = json.loads(g['key_results'])
    return out


def departments(user: AuthUser) -> list[dict]:
    return rows("SELECT id, code, name_en FROM org_unit WHERE company_id = %s AND kind = 'department' ORDER BY name_en",
                [company_id_of(user)])


def colleagues(user: AuthUser, q: str | None = None) -> list[dict]:
    """Colleagues for pickers (feedback, peer reviewers): name, designation and department only."""
    q = parse(Str(trim=True, max=60, optional=True), q if q is not None else None) if q else None
    out = rows("""
        SELECT e.id, e.emp_code, e.name_en, e.designation, ou.name_en AS department, a.manager_id
          FROM employee e
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
          LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
         WHERE e.company_id = %s AND e.status = 'active' AND (%s::text IS NULL OR e.name_en ILIKE '%%' || %s || '%%' OR e.emp_code ILIKE %s || '%%')
         ORDER BY e.name_en LIMIT 500""", [company_id_of(user), q or None, q or None, q or None])
    for c in out:
        c['id'] = str(c['id'])
        c['manager_id'] = c['manager_id'] and str(c['manager_id'])
    return out


def list_goals(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'scope': Enum(('mine', 'team', 'company', 'employee', 'all'), default='mine'), 'employeeId': Uuid(optional=True),
                   'status': Enum(GOAL_STATUSES, optional=True)}), query or {})
    company_id = company_id_of(user)
    status = q.get('status')
    if q['scope'] == 'company':
        return _goal_rows(f"""{GOAL_SELECT} WHERE g.company_id = %s AND g.scope <> 'individual' AND (%s::text IS NULL OR g.status = %s)
                              AND g.status <> 'cancelled' ORDER BY g.scope, g.due_date NULLS LAST, g.title""", [company_id, status, status])
    if q['scope'] in ('mine', 'employee'):
        employee_id = user.employee_id if q['scope'] == 'mine' else q.get('employeeId')
        if not employee_id:
            return []
        employee_in_company(company_id, employee_id)
        if not can_see_employee(user, employee_id):
            raise ForbiddenError('not allowed to see these goals')
        return _goal_rows(f"""{GOAL_SELECT} WHERE g.company_id = %s AND g.scope = 'individual' AND g.employee_id = %s
                              AND (%s::text IS NULL OR g.status = %s) ORDER BY g.status = 'cancelled', g.due_date NULLS LAST, g.created_at""",
                          [company_id, employee_id, status, status])
    if q['scope'] == 'all':
        if not has_role(user, 'hr'):
            raise ForbiddenError('requires HR')
        return _goal_rows(f"""{GOAL_SELECT} WHERE g.company_id = %s AND g.scope = 'individual' AND (%s::text IS NULL OR g.status = %s)
                              ORDER BY e.name_en, g.due_date NULLS LAST""", [company_id, status, status])
    if not user.employee_id:
        if not has_role(user, 'hr'):
            return []
        return _goal_rows(f"""{GOAL_SELECT} WHERE g.company_id = %s AND g.scope = 'individual' AND (%s::text IS NULL OR g.status = %s)
                              ORDER BY e.name_en""", [company_id, status, status])
    return _goal_rows(f"""{SUBORDINATES_CTE} {GOAL_SELECT}
        WHERE g.company_id = %s AND g.scope = 'individual' AND g.employee_id IN (SELECT emp FROM subs) AND (%s::text IS NULL OR g.status = %s)
        ORDER BY e.name_en, g.due_date NULLS LAST""", [user.employee_id, company_id, status, status])


def goal_detail(user: AuthUser, goal_id: str) -> dict:
    g = load_goal(company_id_of(user), parse_uuid(str(goal_id)))
    if not can_view_goal(user, g):
        raise NotFoundError('goal not found')
    goal = _goal_rows(f'{GOAL_SELECT} WHERE g.id = %s', [g['id']])[0]
    checkins = rows("""
        SELECT c.id, c.key_result_id, k.title AS key_result_title, c.value, c.progress, c.confidence, c.comment, c.created_at, u.name AS author_name
          FROM perf_checkin c LEFT JOIN perf_key_result k ON k.id = c.key_result_id LEFT JOIN app_user u ON u.id = c.author_id
         WHERE c.goal_id = %s ORDER BY c.created_at DESC LIMIT 100""", [g['id']])
    children = rows("SELECT id, title, scope, progress, status FROM perf_goal WHERE parent_id = %s AND status <> 'cancelled' ORDER BY title",
                    [g['id']])
    for c in children:
        c['id'] = str(c['id'])
    return {'goal': goal, 'checkins': checkins, 'children': children, 'canEdit': can_edit_goal(user, g)}


def _insert_key_results(goal_id: str, krs: list[dict]) -> None:
    for sort, k in enumerate(krs):
        boolean = k['measure'] == 'boolean'
        cur = k.get('currentValue')
        execute("""INSERT INTO perf_key_result (goal_id, title, measure, start_value, target_value, current_value, unit, weight, sort)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [goal_id, k['title'], k['measure'], 0 if boolean else k['startValue'], 1 if boolean else k['targetValue'],
                 cur if cur is not None else (0 if boolean else k['startValue']), k.get('unit'),
                 k['weight'], sort])


def create_goal(user: AuthUser, body: dict) -> str:
    b = parse(GOAL_BODY, body)
    company_id = company_id_of(user)
    employee_id = org_unit_id = None
    if b['scope'] == 'individual':
        employee_id = b.get('employeeId') or user.employee_id
        if not employee_id:
            raise BadRequestError('choose whose goal this is')
        employee_in_company(company_id, employee_id)
        if employee_id != user.employee_id and not is_above_in_chain(user, employee_id):
            raise ForbiddenError('you can set goals only for yourself and your team')
    elif b['scope'] == 'department':
        if not b.get('orgUnitId'):
            raise BadRequestError('choose the department')
        if not one('SELECT 1 AS x FROM org_unit WHERE id = %s AND company_id = %s', [b['orgUnitId'], company_id]):
            raise NotFoundError('department not found')
        org_unit_id = b['orgUnitId']
    if not can_edit_goal(user, {'scope': b['scope'], 'org_unit_id': org_unit_id, 'employee_id': employee_id}):
        raise ForbiddenError('only HR sets company goals' if b['scope'] == 'company' else 'not allowed to set this goal')
    if b.get('parentId'):
        p = load_goal(company_id, b['parentId'])
        if not can_view_goal(user, p):
            raise NotFoundError('goal not found')
    if b.get('startDate') and b.get('dueDate') and b['dueDate'] < b['startDate']:
        raise BadRequestError('the due date is before the start date')
    with transaction.atomic():
        gid = str(one("""INSERT INTO perf_goal (company_id, scope, org_unit_id, employee_id, parent_id, title, description, weight, start_date,
                                                due_date, status, created_by)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                      [company_id, b['scope'], org_unit_id, employee_id, b.get('parentId'), b['title'], b.get('description'), b['weight'],
                       b.get('startDate'), b.get('dueDate'), b['status'], user.id])['id'])
        _insert_key_results(gid, b['keyResults'])
        recompute_goal_progress(gid)
        if employee_id and employee_id != user.employee_id:
            notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id, template='goal.assigned',
                   title='New goal', body=f"{user.name} set a goal for you: {b['title']}", payload={'goalId': gid})
    return gid


def _sets(b: dict, mapping: dict[str, str], vals: list, blank_to_null: bool = False) -> list[str]:
    sets = []
    for k, col in mapping.items():
        if k in b:
            v = b[k]
            vals.append(None if blank_to_null and v == '' else v)
            sets.append(f'{col} = %s')
    return sets


def update_goal(user: AuthUser, goal_id: str, body: dict) -> None:
    b = parse(GOAL_PATCH, body)
    company_id = company_id_of(user)
    g = load_goal(company_id, parse_uuid(str(goal_id)))
    _assert_can_edit(user, g)
    if b.get('parentId'):
        if b['parentId'] == g['id']:
            raise BadRequestError('a goal cannot be aligned to itself')
        load_goal(company_id, b['parentId'])
    if 'progress' in b:
        if one('SELECT 1 AS x FROM perf_key_result WHERE goal_id = %s LIMIT 1', [g['id']]):
            raise BadRequestError('progress comes from the key results; update those instead')
    vals: list = []
    sets = _sets(b, {'title': 'title', 'description': 'description', 'weight': 'weight', 'startDate': 'start_date', 'dueDate': 'due_date',
                     'status': 'status', 'progress': 'progress', 'parentId': 'parent_id'}, vals)
    if sets:
        execute(f"UPDATE perf_goal SET {', '.join(sets)}, updated_at = now() WHERE id = %s", [*vals, g['id']])


def cancel_goal(user: AuthUser, goal_id: str) -> None:
    g = load_goal(company_id_of(user), parse_uuid(str(goal_id)))
    _assert_can_edit(user, g)
    with transaction.atomic():
        execute("UPDATE perf_goal SET status = 'cancelled', updated_at = now() WHERE id = %s", [g['id']])
        audit('cancel', 'perf_goal', actor_id=user.id, row_id=g['id'], before={'title': g['title'], 'status': g['status']})


def add_key_result(user: AuthUser, goal_id: str, body: dict) -> None:
    b = parse(KEY_RESULT_BODY, body)
    g = load_goal(company_id_of(user), parse_uuid(str(goal_id)))
    _assert_can_edit(user, g)
    with transaction.atomic():
        _insert_key_results(g['id'], [b])
        recompute_goal_progress(g['id'])


def _kr_goal(company_id: str, kr_id: str) -> dict:
    r = one('SELECT k.goal_id, k.measure FROM perf_key_result k JOIN perf_goal g ON g.id = k.goal_id WHERE k.id = %s AND g.company_id = %s',
            [kr_id, company_id])
    if not r:
        raise NotFoundError('key result not found')
    return load_goal(company_id, str(r['goal_id']))


def update_key_result(user: AuthUser, kr_id: str, body: dict) -> None:
    b = parse(Obj(key_result_fields(partial=True)), body)
    kr_id = parse_uuid(str(kr_id))
    goal = _kr_goal(company_id_of(user), kr_id)
    _assert_can_edit(user, goal)
    vals: list = []
    sets = _sets(b, {'title': 'title', 'measure': 'measure', 'startValue': 'start_value', 'targetValue': 'target_value',
                     'currentValue': 'current_value', 'unit': 'unit', 'weight': 'weight'}, vals)
    with transaction.atomic():
        if sets:
            execute(f"UPDATE perf_key_result SET {', '.join(sets)} WHERE id = %s", [*vals, kr_id])
        recompute_goal_progress(goal['id'])


def delete_key_result(user: AuthUser, kr_id: str) -> None:
    kr_id = parse_uuid(str(kr_id))
    goal = _kr_goal(company_id_of(user), kr_id)
    _assert_can_edit(user, goal)
    with transaction.atomic():
        execute('DELETE FROM perf_key_result WHERE id = %s', [kr_id])
        recompute_goal_progress(goal['id'])


def checkin(user: AuthUser, goal_id: str, body: dict) -> None:
    b = parse(CHECKIN_BODY, body)
    company_id = company_id_of(user)
    g = load_goal(company_id, parse_uuid(str(goal_id)))
    _assert_can_edit(user, g, 'check in on this goal')
    if g['status'] == 'cancelled':
        raise BadRequestError('this goal was cancelled')
    if 'value' in b and not b.get('keyResultId'):
        raise BadRequestError('choose the key result the value belongs to')
    with transaction.atomic():
        if b.get('keyResultId'):
            if not one('SELECT 1 AS x FROM perf_key_result WHERE id = %s AND goal_id = %s', [b['keyResultId'], g['id']]):
                raise NotFoundError('key result not found')
            if 'value' in b:
                execute('UPDATE perf_key_result SET current_value = %s WHERE id = %s', [b['value'], b['keyResultId']])
            recompute_goal_progress(g['id'])
        elif 'progress' in b:
            if one('SELECT 1 AS x FROM perf_key_result WHERE goal_id = %s LIMIT 1', [g['id']]):
                raise BadRequestError('progress comes from the key results; update those instead')
            execute('UPDATE perf_goal SET progress = %s, updated_at = now() WHERE id = %s', [b['progress'], g['id']])
        after = one('SELECT progress FROM perf_goal WHERE id = %s', [g['id']])
        execute("""INSERT INTO perf_checkin (goal_id, key_result_id, author_id, value, progress, confidence, comment)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                [g['id'], b.get('keyResultId'), user.id, b.get('value'), after['progress'], b.get('confidence'), b.get('comment')])
        # The other side of the conversation hears about it: the owner's manager, or the owner when someone else checked in.
        if g['scope'] == 'individual' and g['employee_id']:
            target = direct_manager(g['employee_id']) if g['employee_id'] == user.employee_id else g['employee_id']
            if target:
                comment = b.get('comment')
                notify(company_id=company_id, user_id=user_id_for_employee(target), employee_id=target, template='goal.checkin',
                       title='Goal update',
                       body=f'{user.name} updated "{g["title"]}" ({js_round(num(after["progress"]))}%)' + (f': {comment[:140]}' if comment else ''),
                       payload={'goalId': g['id']})


# ---------------------------------------------------------------- KPI library

def list_kpis(user: AuthUser, designation: str | None = None) -> list[dict]:
    d = parse(Str(trim=True, max=120, optional=True), designation) if designation else None
    return rows("""SELECT id, designation, title, description, measure, default_target, unit, active FROM perf_kpi
                    WHERE company_id = %s AND (active OR %s) AND (%s::text IS NULL OR designation IS NULL OR lower(designation) = lower(%s))
                    ORDER BY designation NULLS FIRST, title""", [company_id_of(user), has_role(user, 'hr'), d or None, d or None])


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def create_kpi(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = parse(KPI_BODY, body)
    return str(one("""INSERT INTO perf_kpi (company_id, designation, title, description, measure, default_target, unit, active)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                   [company_id_of(user), b.get('designation') or None, b['title'], b.get('description'), b['measure'], b['defaultTarget'],
                    b.get('unit'), b['active']])['id'])


def update_kpi(user: AuthUser, kpi_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(KPI_PATCH, body)
    kpi_id = parse_uuid(str(kpi_id))
    vals: list = []
    sets = _sets(b, {'designation': 'designation', 'title': 'title', 'description': 'description', 'measure': 'measure',
                     'defaultTarget': 'default_target', 'unit': 'unit', 'active': 'active'}, vals, blank_to_null=True)
    if not sets:
        return
    if not execute(f"UPDATE perf_kpi SET {', '.join(sets)} WHERE id = %s AND company_id = %s", [*vals, kpi_id, company_id_of(user)]):
        raise NotFoundError('KPI not found')


def delete_kpi(user: AuthUser, kpi_id: str) -> None:
    _require_hr(user)
    if not execute('UPDATE perf_kpi SET active = false WHERE id = %s AND company_id = %s', [parse_uuid(str(kpi_id)), company_id_of(user)]):
        raise NotFoundError('KPI not found')


def apply_kpi(user: AuthUser, kpi_id: str, body: dict) -> str:
    """Turn a library KPI into an individual goal with one key result."""
    b = parse(APPLY_KPI_BODY, body)
    company_id = company_id_of(user)
    kpi = one('SELECT title, description, measure, default_target, unit FROM perf_kpi WHERE id = %s AND company_id = %s AND active',
              [parse_uuid(str(kpi_id)), company_id])
    if not kpi:
        raise NotFoundError('KPI not found')
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('choose whose goal this is')
    employee_in_company(company_id, employee_id)
    if employee_id != user.employee_id and not is_above_in_chain(user, employee_id):
        raise ForbiddenError('you can set goals only for yourself and your team')
    with transaction.atomic():
        gid = str(one("""INSERT INTO perf_goal (company_id, scope, employee_id, title, description, weight, due_date, created_by)
                         VALUES (%s, 'individual', %s, %s, %s, %s, %s, %s) RETURNING id""",
                      [company_id, employee_id, kpi['title'], kpi['description'], b['weight'], b.get('dueDate'), user.id])['id'])
        _insert_key_results(gid, [{'title': kpi['title'], 'measure': kpi['measure'], 'startValue': 0,
                                   'targetValue': b['target'] if 'target' in b else num(kpi['default_target']), 'unit': kpi['unit'],
                                   'weight': 1}])
    return gid
