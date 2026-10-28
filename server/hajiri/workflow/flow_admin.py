"""
Approval flow set-up (port of apps/api/src/modules/workflow/flow-routes.ts): default chain per request type,
conditional flows, user groups, approver delegation, a preview of who approves a request for someone, and a check
that every employee's chains reach a real person. Bodies use the TS JSON shape (camelCase keys); every query is scoped
to the user's active company, so ids of another company are not found.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from django.db import models, transaction
from django.db.models import F, Prefetch, Q
from django.db.models.functions import Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.scoping import get_or_404
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (AppUser, ApprovalFlow, ApproverDelegate, Branch, Designation, Employee, OrgUnit, UserGroup,
                              UserGroupMember, WorkflowDefinition)

from .flows import (CONDITION_KEYS, REQUEST_TYPE_SET, REQUEST_TYPES, FlowChooser, choose_flow, fell_back_to_hr, kind_of,
                    normalise_step, parse_steps, resolve_step, step_label, steps_for_amount, visible_notes)
from .serializers import DelegateIn, FlowIn, FlowPatchIn, GroupIn, GroupPatchIn, PreviewQueryIn
from .service import assert_manages, npt_today


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def parse_type(t) -> str:
    if not isinstance(t, str) or t not in REQUEST_TYPE_SET:
        raise ValidationError([{'path': '', 'message': 'unknown request type'}])
    return t


def _people(company_id: str):
    """Accounts that can act in the company: home accounts, members and group-role holders."""
    return AppUser.objects.for_company(company_id)


def check_step_refs(company_id: str, steps: list[dict]) -> None:
    for s in steps:
        if s.get('userId') and not _people(company_id).filter(pk=s['userId']).exists():
            raise BadRequestError('the approver chosen is not a user of your company')
        if s.get('groupId') and not UserGroup.objects.for_company(company_id).filter(pk=s['groupId']).exists():
            raise BadRequestError('the approver group does not exist')


_CHECKS = ((Employee, 'employeeIds', 'employees'), (UserGroup, 'groupIds', 'groups'),
           (Designation, 'designationIds', 'designations'), (OrgUnit, 'orgUnitIds', 'departments'),
           (Branch, 'branchIds', 'branches'))


def check_condition_refs(company_id: str, c: dict) -> None:
    for model, key, label in _CHECKS:
        ids = c.get(key) or []
        if not ids:
            continue
        if model.objects.for_company(company_id).filter(pk__in=list(ids)).count() != len(set(ids)):
            raise BadRequestError(f'one of the {label} is not in your company')


def names(ids: list[str]) -> list[dict]:
    """Names of the people a resolution points to, for previews and the verify report."""
    if not ids:
        return []
    return [{'id': str(r['id']), 'name': r['name']} for r in
            AppUser.objects.filter(pk__in=list(ids)).order_by('name').values('id', 'name')]


def _columns(obj: models.Model) -> dict:
    """A row as the TS `SELECT *` gave it: column names, foreign keys as their ids."""
    return {f.column: f.value_from_object(obj) for f in obj._meta.concrete_fields}


def _flow_row(r: dict) -> dict:
    return {**r, 'id': str(r['id'])}


# ---------------------------------------------------------------- configuration

def flows_config(user: AuthUser) -> dict:
    """GET /workflow/flows/config: every request type with its default chain and flows, and the user groups."""
    _require_hr(user)
    company_id = company_id_of(user)
    defs = {d['object_type']: d for d in WorkflowDefinition.objects.for_company(company_id)
            .values('object_type', 'steps', 'sla_hours', 'escalation_hours')}
    flows = [_flow_row(f) for f in ApprovalFlow.objects.for_company(company_id).order_by('object_type', '-priority', 'created_at')
             .values('id', 'object_type', 'name', 'conditions', 'steps', 'priority', 'active', 'updated_at')]
    return {
        'types': [{
            'objectType': t.type, 'label': t.label, 'money': t.money,
            'defaultSteps': (defs.get(t.type) or {}).get('steps') or [{'kind': 'manager', 'role': 'manager'}],
            'flows': [f for f in flows if f['object_type'] == t.type],
        } for t in REQUEST_TYPES],
        'groups': [_group_dict(g) for g in _groups(company_id).order_by('name')],
    }


def _groups(company_id: str):
    members = UserGroupMember.objects.select_related('user').order_by('user__name')
    return UserGroup.objects.for_company(company_id).prefetch_related(Prefetch('user_group_members', queryset=members))


def _group_dict(g: UserGroup) -> dict:
    return {'id': str(g.id), 'name': g.name, 'description': g.description,
            'members': [{'id': str(m.user_id), 'name': m.user.name} for m in g.user_group_members.all()]}


def get_group(user: AuthUser, group_id: str) -> dict:
    """One user group of the active company with its members (another company's group is not found)."""
    _require_hr(user)
    g = _groups(company_id_of(user)).filter(pk=parse_uuid(str(group_id))).first()
    if g is None:
        raise NotFoundError('group not found')
    return _group_dict(g)


@transaction.atomic
def set_default(user: AuthUser, object_type: str, body: dict) -> dict:
    """PUT /workflow/flows/defaults/:type {steps}."""
    _require_hr(user)
    company_id = company_id_of(user)
    t = parse_type(object_type)
    steps = [normalise_step(s) for s in parse_steps((body or {}).get('steps'), '')]
    check_step_refs(company_id, steps)
    before = WorkflowDefinition.objects.for_company(company_id).filter(object_type=t).values('steps').first()
    WorkflowDefinition.objects.update_or_create(company_id=company_id, object_type=t, defaults={'steps': steps})
    audit('update', 'workflow_definition', actor_id=user.id, row_id=None, before=before,
          after={'objectType': t, 'steps': steps})
    return {'objectType': t, 'steps': steps}


def _parse_flow(body: dict, partial: bool) -> dict:
    """flowBody (without objectType); `partial` for PATCH, where nothing has a default."""
    body = body if isinstance(body, dict) else {}
    parsed = plain(validate(FlowPatchIn if partial else FlowIn, {k: v for k, v in body.items() if k != 'steps'}))
    if not partial and 'conditions' not in parsed:
        parsed['conditions'] = {k: [] for k in CONDITION_KEYS}
    out = {k: parsed[k] for k in ('name', 'conditions', 'priority', 'active') if k in parsed}
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
    flow = ApprovalFlow.objects.create(company_id=company_id, object_type=t, name=b['name'], conditions=b['conditions'],
                                       steps=steps, priority=b['priority'], active=b['active'], created_by_id=user.id)
    fid = str(flow.id)
    audit('create', 'approval_flow', actor_id=user.id, row_id=fid, after={'objectType': t, **b, 'steps': steps})
    return {'id': fid}


@transaction.atomic
def update_flow(user: AuthUser, flow_id: str, body: dict) -> dict:
    """PATCH /workflow/flows/:id (any of name, conditions, steps, priority, active)."""
    _require_hr(user)
    company_id = company_id_of(user)
    fid = parse_uuid(str(flow_id))
    flow = ApprovalFlow.objects.for_company(company_id).select_for_update().filter(pk=fid).first()
    if flow is None:
        raise NotFoundError('flow not found')
    cur = _columns(flow)
    b = _parse_flow(body, partial=True)
    steps = [normalise_step(s) for s in b['steps']] if 'steps' in b else None
    if steps:
        check_step_refs(company_id, steps)
    if 'conditions' in b:
        check_condition_refs(company_id, b['conditions'])
    changes = {k: b[k] for k in ('name', 'conditions', 'priority', 'active') if b.get(k) is not None}
    if steps:
        changes['steps'] = steps
    ApprovalFlow.objects.filter(pk=fid).update(**changes, updated_at=Now())
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
    flow = ApprovalFlow.objects.for_company(company_id).select_for_update().filter(pk=fid).first()
    if flow is None:
        raise NotFoundError('flow not found')
    cur = _columns(flow)
    ApprovalFlow.objects.filter(pk=fid).delete()
    audit('delete', 'approval_flow', actor_id=user.id, row_id=fid, before=cur)
    return {'ok': True}


def get_flow(user: AuthUser, flow_id: str) -> dict:
    _require_hr(user)
    flow = get_or_404(ApprovalFlow, company_id_of(user), 'flow not found', pk=parse_uuid(str(flow_id)))
    return _flow_row(_columns(flow))


# ---------------------------------------------------------------- user groups

def _set_members(company_id: str, group_id: str, user_ids: list[str]) -> None:
    ids = list(dict.fromkeys(str(u) for u in user_ids))
    if ids and _people(company_id).filter(pk__in=ids).count() != len(ids):
        raise BadRequestError('one of the members is not a user of your company')
    UserGroupMember.objects.filter(group_id=group_id).delete()
    UserGroupMember.objects.bulk_create([UserGroupMember(group_id=group_id, user_id=u) for u in ids])


@transaction.atomic
def create_group(user: AuthUser, body: dict) -> dict:
    """POST /workflow/groups {name, description?, userIds?} -> {'id'}."""
    _require_hr(user)
    company_id = company_id_of(user)
    b = plain(validate(GroupIn, body if body is not None else {}))
    g = UserGroup.objects.create(company_id=company_id, name=b['name'], description=b.get('description'))
    gid = str(g.id)
    _set_members(company_id, gid, b['userIds'])
    audit('create', 'user_group', actor_id=user.id, row_id=gid, after=b)
    return {'id': gid}


@transaction.atomic
def update_group(user: AuthUser, group_id: str, body: dict) -> dict:
    """PATCH /workflow/groups/:id {name?, description?, userIds?}."""
    _require_hr(user)
    company_id = company_id_of(user)
    gid = parse_uuid(str(group_id))
    b = plain(validate(GroupPatchIn, body if body is not None else {}))
    if not UserGroup.objects.for_company(company_id).select_for_update().filter(pk=gid).exists():
        raise NotFoundError('group not found')
    changes = {k: b[k] for k in ('name', 'description') if k in b and (k == 'description' or b[k] is not None)}
    if changes:
        UserGroup.objects.filter(pk=gid).update(**changes)
    if 'userIds' in b:
        _set_members(company_id, gid, b['userIds'])
    audit('update', 'user_group', actor_id=user.id, row_id=gid, after=b)
    return {'ok': True}


@transaction.atomic
def delete_group(user: AuthUser, group_id: str) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    gid = parse_uuid(str(group_id))
    uses = [{'groupId': gid}]
    used = (ApprovalFlow.objects.for_company(company_id)
            .filter(Q(steps__contains=uses) | Q(conditions__groupIds__contains=[gid]))
            .values_list('name', flat=True).first()
            or WorkflowDefinition.objects.for_company(company_id).filter(steps__contains=uses)
            .values_list('object_type', flat=True).first())
    if used:
        raise BadRequestError(f"this group is used in an approval flow ({str(used).replace('_', ' ')}); change the flow first")
    group = UserGroup.objects.for_company(company_id).select_for_update().filter(pk=gid).first()
    if group is None:
        raise NotFoundError('group not found')
    cur = _columns(group)
    UserGroup.objects.filter(pk=gid).delete()      # members go with it (ON DELETE CASCADE in the schema)
    audit('delete', 'user_group', actor_id=user.id, row_id=gid, before=cur)
    return {'ok': True}


# ---------------------------------------------------------------- delegation

def list_delegates(user: AuthUser, all_: bool = False) -> list[dict]:
    """My delegations (and, for HR with all_, everyone's): while active, the delegate also receives and decides my approvals."""
    company_id = company_id_of(user)
    who = Q() if has_role(user, 'hr') and all_ else Q(user_id=user.id) | Q(delegate_id=user.id)
    found = (ApproverDelegate.objects.for_company(company_id)
             .filter(who, cancelled_at__isnull=True, to_date__gte=date.fromisoformat(npt_today()) - timedelta(days=30))
             .order_by('-from_date')
             .values('id', 'user_id', 'delegate_id', 'from_date', 'to_date', 'note', 'cancelled_at', 'created_at',
                     user_name=F('user__name'), delegate_name=F('delegate__name'))[:200])
    return [{**r, 'id': str(r['id']), 'user_id': str(r['user_id']), 'delegate_id': str(r['delegate_id'])} for r in found]


def delegate_candidates(user: AuthUser) -> list[dict]:
    """Colleagues who can be chosen as a delegate (active users of the company; names only)."""
    return [{'id': str(r['id']), 'name': r['name']} for r in
            _people(company_id_of(user)).filter(status=AppUser.Status.ACTIVE).exclude(pk=user.id)
            .order_by('name').values('id', 'name')[:1000]]


@transaction.atomic
def add_delegate(user: AuthUser, body: dict) -> dict:
    """POST /workflow/delegates {userId?, delegateId, from, to, note?} -> {'id'}."""
    company_id = company_id_of(user)
    b = plain(validate(DelegateIn, body if body is not None else {}))
    user_id = b.get('userId') or user.id
    if user_id != user.id and not has_role(user, 'hr'):
        raise ForbiddenError('only HR can set a delegate for someone else')
    if b['to'] < b['from']:
        raise BadRequestError('the end date is before the start date')
    if b['to'] < npt_today():
        raise BadRequestError('the delegation has already ended')
    if user_id == b['delegateId']:
        raise BadRequestError('choose someone else as the delegate')
    if _people(company_id).filter(pk__in=[user_id, b['delegateId']], status=AppUser.Status.ACTIVE).count() != 2:
        raise BadRequestError('the delegate must be an active user of your company')
    d = ApproverDelegate.objects.create(company_id=company_id, user_id=user_id, delegate_id=b['delegateId'],
                                        from_date=b['from'], to_date=b['to'], note=b.get('note'), created_by_id=user.id)
    did = str(d.id)
    audit('create', 'approver_delegate', actor_id=user.id, row_id=did, after={**b, 'userId': user_id})
    return {'id': did}


@transaction.atomic
def cancel_delegate(user: AuthUser, delegate_id: str) -> dict:
    company_id = company_id_of(user)
    did = parse_uuid(str(delegate_id))
    owner = (ApproverDelegate.objects.for_company(company_id).filter(pk=did, cancelled_at__isnull=True)
             .values_list('user_id', flat=True).first())
    if owner is None or (str(owner) != user.id and not has_role(user, 'hr')):
        raise NotFoundError('delegation not found')
    ApproverDelegate.objects.filter(pk=did).update(cancelled_at=Now())
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
    q = plain(validate(PreviewQueryIn, query or {}))
    object_type = parse_type(q['objectType'])
    employee_id = q.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    if not Employee.objects.for_company(company_id).filter(pk=employee_id).exists():
        raise NotFoundError('employee not found')
    if employee_id != user.employee_id:
        assert_manages(user, employee_id)
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
    emps = list(Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE).order_by('emp_code')
                .values('id', 'emp_code', 'name_en'))
    types = [t for t in REQUEST_TYPES if t.subject == 'employee' and (not ot or t.type == ot)]
    today = npt_today()
    cache: dict = {}
    chooser = FlowChooser(company_id)
    problems = []
    for e in emps:
        eid = str(e['id'])
        for t in types:
            flow = chooser.choose(t.type, eid)
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
