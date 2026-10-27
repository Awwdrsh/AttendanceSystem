"""
Approval flows (port of apps/api/src/modules/workflow/flows.ts). A step names who approves: the line manager, the
manager's manager, the department or branch head, everyone holding a role, one named user, or any member of a user
group. A company has a default chain per request type (workflow_definition) and may add conditional flows
(approval_flow) for initiators in a department, branch, designation or user group; the most specific match wins, and
with no default the reporting line approves.

Resolution happens when someone looks (inbox, notification), so a manager change or a head appointment is picked up
without editing any flow. Approvers who are away (on full-day leave today, exited, disabled) hand over to an active
delegate; when nobody is left the step falls back to HR, and the instance records why.

Steps are plain dicts in the stored (camelCase) shape: {'kind', 'role', 'userId'?, 'groupId'?, 'minAmount'?, 'label'?}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hajiri.core.clock import npt_today
from hajiri.core.db import rows
from hajiri.core.errors import ValidationError
from hajiri.core.schema import Arr, Enum, Num, Obj, Str, Uuid, parse
from hajiri.notifications.service import user_ids_with_role

APPROVER_KINDS = ('manager', 'manager2', 'department_head', 'branch_head', 'role', 'user', 'group')
STEP_ROLES = ('hr', 'admin', 'payroll_admin', 'attendance_admin', 'recruiter')

_STEP = Obj({
    'kind': Enum(APPROVER_KINDS),
    'role': Enum(STEP_ROLES, optional=True),
    'userId': Uuid(optional=True),
    'groupId': Uuid(optional=True),
    'minAmount': Num(min=0, max=1e10, optional=True),
    'label': Str(trim=True, max=60, optional=True),
})
CONDITION_KEYS = ('employeeIds', 'groupIds', 'designationIds', 'orgUnitIds', 'branchIds')
CONDITIONS_SCHEMA = Obj({
    'employeeIds': Arr(Uuid(), max=500, default=list),
    'groupIds': Arr(Uuid(), max=50, default=list),
    'designationIds': Arr(Uuid(), max=100, default=list),
    'orgUnitIds': Arr(Uuid(), max=100, default=list),
    'branchIds': Arr(Uuid(), max=100, default=list),
})


def empty_conditions() -> dict:
    return {k: [] for k in CONDITION_KEYS}


def parse_steps(data: Any, path: str = 'steps') -> list[dict]:
    """stepsSchema: 1 to 6 steps, each with the field its kind needs. Raises ValidationError with the TS messages."""
    if isinstance(data, list):
        if len(data) < 1:
            raise ValidationError([{'path': path, 'message': 'a flow needs at least one step'}])
        if len(data) > 6:
            raise ValidationError([{'path': path, 'message': 'at most 6 steps'}])
    out = parse(Arr(_STEP), data)
    issues = []
    for i, s in enumerate(out):
        if s['kind'] == 'role' and not s.get('role'):
            issues.append({'path': f'{path}.{i}.role' if path else f'{i}.role', 'message': 'choose the role that approves'})
        if s['kind'] == 'user' and not s.get('userId'):
            issues.append({'path': f'{path}.{i}.userId' if path else f'{i}.userId', 'message': 'choose the person who approves'})
        if s['kind'] == 'group' and not s.get('groupId'):
            issues.append({'path': f'{path}.{i}.groupId' if path else f'{i}.groupId', 'message': 'choose the group that approves'})
    if issues:
        raise ValidationError(issues)
    return out


def normalise_step(s: dict) -> dict:
    """Stored shape: `role` mirrors the kind (or the role for role steps) so older code that reads step['role'] works."""
    out: dict = {'kind': s['kind'], 'role': s['role'] if s['kind'] == 'role' else s['kind']}
    if s['kind'] == 'user':
        out['userId'] = s['userId']
    if s['kind'] == 'group':
        out['groupId'] = s['groupId']
    if s.get('minAmount') is not None:
        out['minAmount'] = s['minAmount']
    if s.get('label'):
        out['label'] = s['label']
    return out


def kind_of(s: dict) -> str:
    return s.get('kind') or ('manager' if s.get('role') == 'manager' else 'role')


def to_step_input(s: dict) -> dict:
    """A stored step back in the editor's form (flows.ts toStepInput)."""
    kind = kind_of(s)
    out: dict = {'kind': kind}
    if kind == 'role':
        out['role'] = s.get('role')
    if s.get('userId'):
        out['userId'] = s['userId']
    if s.get('groupId'):
        out['groupId'] = s['groupId']
    if s.get('minAmount') is not None:
        out['minAmount'] = s['minAmount']
    if s.get('label'):
        out['label'] = s['label']
    return out


@dataclass(frozen=True)
class RequestType:
    type: str
    label: str
    subject: str = 'employee'        # 'employee' | 'requester'
    money: bool = False


# Request types that go through approvals, with the label HR sees. Money types can have amount thresholds.
REQUEST_TYPES: tuple[RequestType, ...] = (
    RequestType('leave_request', 'Leave'),
    RequestType('regularisation_request', 'Attendance correction'),
    RequestType('ot_request', 'Overtime'),
    RequestType('timesheet', 'Timesheet'),
    RequestType('shift_swap', 'Shift swap'),
    RequestType('expense_claim', 'Expense claim', money=True),
    RequestType('travel_request', 'Travel request', money=True),
    RequestType('travel_settlement', 'Travel settlement', money=True),
    RequestType('letter_request', 'Letter request'),
    RequestType('training_nomination', 'Training request'),
    RequestType('tax_declaration', 'Tax savings declaration'),
    RequestType('profile_change', 'Profile change'),
    RequestType('resignation', 'Resignation'),
    RequestType('probation_review', 'Probation review'),
    RequestType('job_requisition', 'Hiring request', subject='requester'),
    RequestType('job_offer', 'Job offer', subject='requester'),
)
REQUEST_TYPE_SET = frozenset(t.type for t in REQUEST_TYPES)
REQUEST_TYPE_BY_KEY = {t.type: t for t in REQUEST_TYPES}

DEFAULT_STEPS: list[dict] = [{'kind': 'manager', 'role': 'manager'}]


# ---------------------------------------------------------------- choosing the flow

@dataclass
class Subject:
    user_id: str | None = None
    designation_id: str | None = None
    org_unit_ids: list[str] = field(default_factory=list)
    branch_id: str | None = None
    group_ids: list[str] = field(default_factory=list)


def _s(v) -> str | None:
    return None if v is None else str(v)


def subject_of(employee_id: str, on_date=None) -> Subject:
    r = rows("""SELECT (SELECT u.id FROM app_user u WHERE u.employee_id = e.id AND u.status = 'active' LIMIT 1) AS user_id,
                       e.designation_id, a.org_unit_id, a.branch_id
                  FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %s::date
                 WHERE e.id = %s""", [on_date or npt_today(), employee_id])
    if not r:
        return Subject()
    row = r[0]
    units = [str(x['id']) for x in rows(
        """WITH RECURSIVE up(id, parent_id, depth) AS (SELECT id, parent_id, 1 FROM org_unit WHERE id = %s
             UNION ALL SELECT o.id, o.parent_id, up.depth + 1 FROM org_unit o JOIN up ON o.id = up.parent_id WHERE up.depth < 20)
           SELECT id FROM up""", [row['org_unit_id']])] if row['org_unit_id'] else []
    groups = [str(x['group_id']) for x in rows('SELECT group_id FROM user_group_member WHERE user_id = %s', [row['user_id']])] \
        if row['user_id'] else []
    return Subject(_s(row['user_id']), _s(row['designation_id']), units, _s(row['branch_id']), groups)


def match_score(c: dict, subject_employee_id: str | None, s: Subject) -> int:
    """How specifically a flow's conditions match the subject: -1 = no match, 0 = matches everyone, higher = narrower."""
    c = c or {}
    lists = [
        (c.get('employeeIds'), 16, lambda ids: bool(subject_employee_id) and str(subject_employee_id) in ids),
        (c.get('groupIds'), 8, lambda ids: any(g in ids for g in s.group_ids)),
        (c.get('designationIds'), 4, lambda ids: bool(s.designation_id) and s.designation_id in ids),
        (c.get('orgUnitIds'), 2, lambda ids: any(u in ids for u in s.org_unit_ids)),
        (c.get('branchIds'), 1, lambda ids: bool(s.branch_id) and s.branch_id in ids),
    ]
    score = 0
    for ids, weight, test in lists:
        if not ids:
            continue
        if not test([str(x) for x in ids]):
            return -1
        score += weight
    return score


@dataclass(frozen=True)
class ChosenFlow:
    steps: list[dict]
    flow_id: str | None
    flow_name: str | None


def choose_flow(company_id: str, object_type: str, employee_id: str | None) -> ChosenFlow:
    """The flow for this request: most specific matching conditional flow, else the company default, else the reporting line."""
    flows = rows("""SELECT id, name, conditions, steps, priority FROM approval_flow
                     WHERE company_id = %s AND object_type = %s AND active ORDER BY priority DESC, created_at""",
                 [company_id, object_type])
    if flows:
        subject = subject_of(employee_id) if employee_id else Subject()
        best = None
        for row in flows:
            score = match_score(row['conditions'] or {}, employee_id, subject)
            if score < 0:
                continue
            if best is None or score > best[1] or (score == best[1] and row['priority'] > best[0]['priority']):
                best = (row, score)
        if best and best[0]['steps']:
            return ChosenFlow(best[0]['steps'], str(best[0]['id']), best[0]['name'])
    d = rows('SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = %s', [company_id, object_type])
    steps = d[0]['steps'] if d and d[0]['steps'] else DEFAULT_STEPS
    return ChosenFlow(steps, None, None)


def steps_for_amount(steps: list[dict], amount) -> list[dict]:
    """Steps with an amount threshold apply only when the request reaches it; a request without an amount skips them."""
    kept = [s for s in steps if s.get('minAmount') is None or (amount is not None and float(amount) >= float(s['minAmount']))]
    return kept if kept else steps[:1]


# ---------------------------------------------------------------- resolving a step to people

@dataclass
class Resolution:
    users: list[str]
    notes: list[str]


def _users_of_employees(employee_ids: list[str]) -> list[str]:
    if not employee_ids:
        return []
    return [str(r['id']) for r in rows("SELECT id FROM app_user WHERE employee_id = ANY(%s::uuid[]) AND status = 'active'",
                                       [list(employee_ids)])]


def line_manager(employee_id: str, on_date) -> str | None:
    r = rows('SELECT manager_id FROM employee_assignment WHERE employee_id = %s AND valid @> %s::date', [employee_id, on_date])
    return _s(r[0]['manager_id']) if r else None


def department_head(employee_id: str, on_date) -> str | None:
    """Nearest head above the employee who is not the employee: their department, then each parent department."""
    r = rows("""WITH RECURSIVE up(id, parent_id, head, depth) AS (
                  SELECT o.id, o.parent_id, o.head_employee_id, 1 FROM employee_assignment a JOIN org_unit o ON o.id = a.org_unit_id
                   WHERE a.employee_id = %s AND a.valid @> %s::date
                  UNION ALL SELECT p.id, p.parent_id, p.head_employee_id, up.depth + 1 FROM org_unit p JOIN up ON p.id = up.parent_id WHERE up.depth < 20)
                SELECT head FROM up WHERE head IS NOT NULL AND head <> %s ORDER BY depth LIMIT 1""",
             [employee_id, on_date, employee_id])
    return _s(r[0]['head']) if r else None


def branch_head(employee_id: str, on_date) -> str | None:
    r = rows("""SELECT b.head_employee_id AS head FROM employee_assignment a JOIN branch b ON b.id = a.branch_id
                 WHERE a.employee_id = %s AND a.valid @> %s::date AND b.head_employee_id IS NOT NULL AND b.head_employee_id <> %s""",
             [employee_id, on_date, employee_id])
    return _s(r[0]['head']) if r else None


KIND_LABEL = {
    'manager': 'Line manager', 'manager2': "Manager's manager", 'department_head': 'Department head',
    'branch_head': 'Branch head', 'role': 'Role', 'user': 'Named person', 'group': 'Group',
}
ROLE_LABEL = {'hr': 'HR', 'admin': 'Admin', 'payroll_admin': 'Payroll admin', 'attendance_admin': 'Attendance admin',
              'recruiter': 'Recruiter'}


def step_label(s: dict) -> str:
    if s.get('label'):
        return s['label']
    k = kind_of(s)
    return ROLE_LABEL.get(s.get('role'), s.get('role')) if k == 'role' else KIND_LABEL[k]


def named_approvers(company_id: str, employee_id: str | None, step: dict, on_date) -> list[str]:
    """The people named by the step before anyone is checked for absence."""
    kind = kind_of(step)
    if kind == 'manager':
        if not employee_id:
            return []
        m = line_manager(employee_id, on_date)
        return _users_of_employees([m]) if m else []
    if kind == 'manager2':
        if not employee_id:
            return []
        m = line_manager(employee_id, on_date)
        m2 = line_manager(m, on_date) if m else None
        return _users_of_employees([m2]) if m2 and m2 != str(employee_id) else []
    if kind == 'department_head':
        if not employee_id:
            return []
        h = department_head(employee_id, on_date)
        return _users_of_employees([h]) if h else []
    if kind == 'branch_head':
        if not employee_id:
            return []
        h = branch_head(employee_id, on_date)
        return _users_of_employees([h]) if h else []
    if kind == 'role':
        return user_ids_with_role(company_id, step.get('role'))
    if kind == 'user':
        if not step.get('userId'):
            return []
        return [str(r['id']) for r in rows("SELECT id FROM app_user WHERE id = %s AND company_id = %s AND status = 'active'",
                                           [step['userId'], company_id])]
    if kind == 'group':
        if not step.get('groupId'):
            return []
        return [str(r['id']) for r in rows(
            """SELECT u.id FROM user_group_member m JOIN user_group g ON g.id = m.group_id JOIN app_user u ON u.id = m.user_id
                WHERE m.group_id = %s AND g.company_id = %s AND u.status = 'active' ORDER BY u.name""",
            [step['groupId'], company_id])]
    return []


def presence_of(user_ids: list[str], on_date) -> dict[str, dict]:
    """Who of these users is away today (full-day leave or exited) and whom they delegated to."""
    if not user_ids:
        return {}
    found = rows("""
        SELECT u.id, u.name,
               CASE WHEN e.id IS NOT NULL AND (e.status = 'exited' OR (e.exit_date IS NOT NULL AND e.exit_date < %(d)s::date)) THEN 'has left'
                    WHEN EXISTS (SELECT 1 FROM leave_request l WHERE l.employee_id = e.id AND l.status = 'approved' AND l.from_time IS NULL
                                   AND %(d)s::date BETWEEN l.from_date AND l.to_date AND l.half_day IS NULL) THEN 'is on leave' END AS away,
               (SELECT d.delegate_id FROM approver_delegate d JOIN app_user du ON du.id = d.delegate_id AND du.status = 'active'
                 WHERE d.user_id = u.id AND d.cancelled_at IS NULL AND %(d)s::date BETWEEN d.from_date AND d.to_date
                 ORDER BY d.created_at DESC LIMIT 1) AS delegate
          FROM app_user u LEFT JOIN employee e ON e.id = u.employee_id WHERE u.id = ANY(%(ids)s::uuid[])""",
                 {'d': on_date, 'ids': list(user_ids)})
    return {str(r['id']): {'id': str(r['id']), 'name': r['name'], 'away': r['away'], 'delegate': _s(r['delegate'])} for r in found}


def _dedupe(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def resolve_step(company_id: str, employee_id: str | None, step: dict, on_date=None) -> Resolution:
    """
    Everyone who may decide this step now: the named approvers, minus the requester, with away approvers replaced by
    their delegate (a delegate also covers while the approver is present). Nobody left -> HR (and admins).
    """
    on_date = on_date or npt_today()
    notes: list[str] = []
    subject_user = [str(r['id']) for r in rows('SELECT id FROM app_user WHERE employee_id = %s', [employee_id])] \
        if employee_id else []
    named = [u for u in named_approvers(company_id, employee_id, step, on_date) if u not in subject_user]
    label = step_label(step)
    if not named and kind_of(step) != 'role':
        notes.append(f'No {label.lower()} is set')
    presence = presence_of(named, on_date)
    users: list[str] = []

    def add(u):
        if u not in users:
            users.append(u)
    for uid in named:
        p = presence.get(uid)
        if p and p['delegate'] and p['delegate'] not in subject_user:
            add(p['delegate'])
            if p['away']:
                notes.append(f"{p['name']} {p['away']}; delegated")
        if p and p['away']:
            if not p['delegate']:
                notes.append(f"{p['name']} {p['away']}")
            continue
        add(uid)
    if not users:
        hr = [u for u in user_ids_with_role(company_id, 'hr') if u not in subject_user]
        if named or kind_of(step) == 'role':
            notes.append('Nobody available; HR decides')
        else:
            notes.append('HR decides instead')
        for u in hr:
            add(u)
        return Resolution(users, _dedupe(notes) + ['fallback:hr'])
    return Resolution(users, _dedupe(notes))


def fell_back_to_hr(r: Resolution) -> bool:
    return 'fallback:hr' in r.notes


def visible_notes(r: Resolution) -> list[str]:
    return [n for n in r.notes if n != 'fallback:hr']
