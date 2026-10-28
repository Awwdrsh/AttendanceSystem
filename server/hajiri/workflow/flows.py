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
from datetime import date
from typing import Any

from django.db import connection
from django.db.models import Case, Exists, F, OuterRef, Q, Subquery, UUIDField, When

from hajiri.core.clock import npt_today
from hajiri.core.errors import ValidationError
from hajiri.core.users import user_ids_with_role
from hajiri.core.validation import issues_from, plain
from hajiri.db.models import (AppUser, ApprovalFlow, ApproverDelegate, CompanyMembership, Employee, EmployeeAssignment,
                              LeaveRequest, UserGroupMember, WorkflowDefinition)

from .serializers import APPROVER_KINDS, CONDITION_KEYS, STEP_ROLES, ConditionsIn, StepIn  # noqa: F401  (re-exported)


def empty_conditions() -> dict:
    return {k: [] for k in CONDITION_KEYS}


def parse_steps(data: Any, path: str = 'steps') -> list[dict]:
    """stepsSchema: 1 to 6 steps, each with the field its kind needs. Raises ValidationError with the TS messages."""
    if isinstance(data, list):
        if len(data) < 1:
            raise ValidationError([{'path': path, 'message': 'a flow needs at least one step'}])
        if len(data) > 6:
            raise ValidationError([{'path': path, 'message': 'at most 6 steps'}])
    elif data is None:
        raise ValidationError([{'path': '', 'message': 'Required'}])
    else:
        raise ValidationError([{'path': '', 'message': 'Expected array'}])
    ser = StepIn(data=data, many=True)
    if not ser.is_valid():
        raise ValidationError(issues_from(ser.errors, ser))
    out = [plain(dict(s)) for s in ser.validated_data]
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


def _day(d) -> date:
    return date.fromisoformat(d) if isinstance(d, str) else d


def _assignment(employee_id, on_date):
    """The employee's assignment on that day (employee_assignment.valid is a daterange)."""
    return EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=_day(on_date))


def org_unit_chain(org_unit_id) -> list[tuple[str, str | None]]:
    """(id, head_employee_id) of a department and each parent above it, nearest first (at most 20 levels)."""
    if not org_unit_id:
        return []
    with connection.cursor() as cur:
        # Raw SQL: a recursive CTE walking org_unit.parent_id upwards; the ORM has no recursive queries.
        cur.execute("""WITH RECURSIVE up(id, parent_id, head, depth) AS (
                         SELECT id, parent_id, head_employee_id, 1 FROM org_unit WHERE id = %s
                         UNION ALL SELECT o.id, o.parent_id, o.head_employee_id, up.depth + 1
                           FROM org_unit o JOIN up ON o.id = up.parent_id WHERE up.depth < 20)
                       SELECT id, head FROM up ORDER BY depth""", [str(org_unit_id)])
        return [(str(i), _s(h)) for i, h in cur.fetchall()]


def employee_users(employee_ids, *, active_only: bool = True):
    """Accounts that act as these employee records: the home account (app_user.employee_id) or a member of the group
    whose company_membership points at the record (an employee record belongs to exactly one company)."""
    ids = [str(e) for e in employee_ids if e]
    if not ids:
        return AppUser.objects.none()
    member = CompanyMembership.objects.filter(user=OuterRef('pk'), employee_id__in=ids)
    if active_only:
        member = member.filter(status=CompanyMembership.Status.ACTIVE)
    qs = AppUser.objects.filter(Q(employee_id__in=ids) | Exists(member))
    return qs.filter(status=AppUser.Status.ACTIVE) if active_only else qs


def subject_of(employee_id: str, on_date=None) -> Subject:
    on_date = on_date or npt_today()
    here = _assignment(OuterRef('pk'), on_date)
    e = (Employee.objects.filter(pk=employee_id)
         .annotate(unit=Subquery(here.values('org_unit_id')[:1]), branch=Subquery(here.values('branch_id')[:1]))
         .values('company_id', 'designation_ref_id', 'unit', 'branch').first())
    if e is None:
        return Subject()
    user_id = _s(employee_users([employee_id]).values_list('id', flat=True).first())
    units = [u for u, _h in org_unit_chain(e['unit'])]
    groups = [str(g) for g in UserGroupMember.objects.for_company(e['company_id']).filter(user_id=user_id)
              .values_list('group_id', flat=True)] \
        if user_id else []
    return Subject(user_id, _s(e['designation_ref_id']), units, _s(e['branch']), groups)


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


_FLOW_FIELDS = ('id', 'name', 'conditions', 'steps', 'priority')


def _active_flows(company_id: str):
    return (ApprovalFlow.objects.for_company(company_id).filter(active=True).order_by('-priority', 'created_at'))


def _pick(flows: list[dict], employee_id: str | None, subject) -> dict | None:
    """The most specific matching flow (ties: higher priority, then the earlier one). `subject` is called only when
    there is a flow to match against."""
    if not flows:
        return None
    s = subject(employee_id) if employee_id else Subject()
    best = None
    for row in flows:
        score = match_score(row['conditions'] or {}, employee_id, s)
        if score < 0:
            continue
        if best is None or score > best[1] or (score == best[1] and row['priority'] > best[0]['priority']):
            best = (row, score)
    return best[0] if best and best[0]['steps'] else None


def choose_flow(company_id: str, object_type: str, employee_id: str | None) -> ChosenFlow:
    """The flow for this request: most specific matching conditional flow, else the company default, else the reporting line."""
    row = _pick(list(_active_flows(company_id).filter(object_type=object_type).values(*_FLOW_FIELDS)), employee_id, subject_of)
    if row:
        return ChosenFlow(row['steps'], str(row['id']), row['name'])
    steps = (WorkflowDefinition.objects.for_company(company_id).filter(object_type=object_type)
             .values_list('steps', flat=True).first())
    return ChosenFlow(steps or DEFAULT_STEPS, None, None)


class FlowChooser:
    """choose_flow for many requests of one company (the verify report): the company's flows and default chains are
    read once and each employee's subject once."""

    def __init__(self, company_id: str):
        self.flows: dict[str, list[dict]] = {}
        for row in _active_flows(company_id).values('object_type', *_FLOW_FIELDS):
            self.flows.setdefault(row['object_type'], []).append(row)
        self.defaults = dict(WorkflowDefinition.objects.for_company(company_id).values_list('object_type', 'steps'))
        self._subjects: dict[str, Subject] = {}

    def _subject(self, employee_id: str) -> Subject:
        if employee_id not in self._subjects:
            self._subjects[employee_id] = subject_of(employee_id)
        return self._subjects[employee_id]

    def choose(self, object_type: str, employee_id: str | None) -> ChosenFlow:
        row = _pick(self.flows.get(object_type, []), employee_id, self._subject)
        if row:
            return ChosenFlow(row['steps'], str(row['id']), row['name'])
        return ChosenFlow(self.defaults.get(object_type) or DEFAULT_STEPS, None, None)


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
    return [str(u) for u in employee_users(employee_ids).values_list('id', flat=True)]


def line_manager(employee_id: str, on_date) -> str | None:
    return _s(_assignment(employee_id, on_date).values_list('manager_id', flat=True).first())


def department_head(employee_id: str, on_date) -> str | None:
    """Nearest head above the employee who is not the employee: their department, then each parent department."""
    unit = _assignment(employee_id, on_date).values_list('org_unit_id', flat=True).first()
    return next((h for _u, h in org_unit_chain(unit) if h and h != str(employee_id)), None)


def branch_head(employee_id: str, on_date) -> str | None:
    return _s(_assignment(employee_id, on_date).filter(branch__head_employee__isnull=False)
              .exclude(branch__head_employee_id=employee_id).values_list('branch__head_employee_id', flat=True).first())


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
    if kind in ('manager', 'manager2', 'department_head', 'branch_head') and not employee_id:
        return []
    if kind == 'manager':
        m = line_manager(employee_id, on_date)
        return _users_of_employees([m]) if m else []
    if kind == 'manager2':
        m = line_manager(employee_id, on_date)
        m2 = line_manager(m, on_date) if m else None
        return _users_of_employees([m2]) if m2 and m2 != str(employee_id) else []
    if kind == 'department_head':
        h = department_head(employee_id, on_date)
        return _users_of_employees([h]) if h else []
    if kind == 'branch_head':
        h = branch_head(employee_id, on_date)
        return _users_of_employees([h]) if h else []
    if kind == 'role':
        return user_ids_with_role(company_id, step.get('role'))
    if kind == 'user':
        if not step.get('userId'):
            return []
        return [str(u) for u in AppUser.objects.for_company(company_id)
                .filter(pk=step['userId'], status=AppUser.Status.ACTIVE).values_list('id', flat=True)]
    if kind == 'group':
        if not step.get('groupId'):
            return []
        return [str(u) for u in UserGroupMember.objects.for_company(company_id)
                .filter(group_id=step['groupId'], user__status=AppUser.Status.ACTIVE)
                .order_by('user__name').values_list('user_id', flat=True)]
    return []


def _employee_here(company_id):
    """The employee record an account acts as in this company: the home record at home, the membership's elsewhere."""
    if company_id is None:
        return F('employee_id')
    return Case(When(company_id=company_id, then=F('employee_id')),
                default=Subquery(CompanyMembership.objects.filter(user=OuterRef('pk'), company_id=company_id)
                                 .values('employee_id')[:1]), output_field=UUIDField())


def presence_of(user_ids: list[str], on_date, company_id: str | None = None) -> dict[str, dict]:
    """Who of these users is away today (full-day leave or exited) and whom they delegated to (in this company)."""
    if not user_ids:
        return {}
    delegations = ApproverDelegate.objects.filter(user=OuterRef('pk'), cancelled_at__isnull=True, from_date__lte=on_date,
                                                  to_date__gte=on_date, delegate__status=AppUser.Status.ACTIVE)
    if company_id is not None:
        delegations = delegations.for_company(company_id)
    has_left = Employee.objects.filter(pk=OuterRef('emp')).filter(Q(status=Employee.Status.EXITED) | Q(exit_date__lt=on_date))
    on_leave = LeaveRequest.objects.filter(employee_id=OuterRef('emp'), status=LeaveRequest.Status.APPROVED,
                                           from_time__isnull=True, half_day__isnull=True, from_date__lte=on_date,
                                           to_date__gte=on_date)
    found = (AppUser.objects.filter(pk__in=list(user_ids)).annotate(emp=_employee_here(company_id))
             .annotate(left=Exists(has_left), leave=Exists(on_leave),
                       delegate=Subquery(delegations.order_by('-created_at').values('delegate_id')[:1]))
             .values('id', 'name', 'left', 'leave', 'delegate'))
    return {str(r['id']): {'id': str(r['id']), 'name': r['name'],
                           'away': 'has left' if r['left'] else 'is on leave' if r['leave'] else None,
                           'delegate': _s(r['delegate'])} for r in found}


def _dedupe(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def resolve_step(company_id: str, employee_id: str | None, step: dict, on_date=None) -> Resolution:
    """
    Everyone who may decide this step now: the named approvers, minus the requester, with away approvers replaced by
    their delegate (a delegate also covers while the approver is present). Nobody left -> HR (and admins).
    """
    on_date = on_date or npt_today()
    notes: list[str] = []
    subject_user = [str(u) for u in employee_users([employee_id], active_only=False).values_list('id', flat=True)] \
        if employee_id else []
    named = [u for u in named_approvers(company_id, employee_id, step, on_date) if u not in subject_user]
    label = step_label(step)
    if not named and kind_of(step) != 'role':
        notes.append(f'No {label.lower()} is set')
    presence = presence_of(named, on_date, company_id)
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
