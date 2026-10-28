"""
Generic multi-level approval engine (port of apps/api/src/modules/workflow/service.ts, plan §5.8): one
workflow_instance per request object, steps copied from the company's flow when the request is made, signed
approval_action rows, and per-object-type decision handlers that apply the business effect (ledger posting, punch
creation, ...) in the same transaction as the decision.

How a request module uses it (see the package docstring in hajiri/workflow/__init__.py for the full contract):

    from hajiri.workflow.services import register_handler, start_workflow, cancel_workflow, DecisionContext

    def _decided(object_id: str, state: str, ctx: DecisionContext) -> None:   # 'approved' | 'rejected'
        LeaveRequest.objects.filter(pk=object_id).update(status=state, decided_at=Now())

    register_handler('leave_request', _decided)        # at import time (e.g. in your services.py or apps.ready)

    with transaction.atomic():
        lr = LeaveRequest.objects.create(...)
        inst = start_workflow(company_id=..., object_type='leave_request', object_id=lr.id, employee_id=emp,
                              title='Leave request', body='Sita Sharma asked for 2 days of home leave')
        LeaveRequest.objects.filter(pk=lr.id).update(workflow_instance_id=inst['id'])
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from django.db import transaction
from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import (Count, DateTimeField, DecimalField, ExpressionWrapper, F, Func, JSONField, OuterRef, Q, Subquery,
                              Sum, Value)
from django.db.models.functions import Coalesce, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_module, has_role, is_manager_of
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid, is_uuid
from hajiri.db.models import (ApprovalAction, Employee, LeaveLedger, LeaveRequest, LetterRequest, LrnEnrollment, OtRequest, PayrollRun,
                              RegularisationRequest, WorkflowInstance)
from hajiri.notifications.service import notify

from .flows import choose_flow, fell_back_to_hr, kind_of, resolve_step, steps_for_amount, visible_notes

log = logging.getLogger('hajiri.workflow')

State = WorkflowInstance.State
STATES = tuple(State.values)


@dataclass(frozen=True)
class DecisionContext:
    actor_id: str
    reason: str | None
    payload: dict = field(default_factory=dict)


# fn(object_id, state, ctx) -> None, run inside the decision's transaction. state: 'approved' | 'rejected'.
DecisionHandler = Callable[[str, str, DecisionContext], None]
# fn(instance) -> dict: inbox card context for object types owned by other modules.
Summariser = Callable[[dict], dict]

_handlers: dict[str, DecisionHandler] = {}
_summarisers: dict[str, Summariser] = {}


def register_handler(object_type: str, fn: DecisionHandler) -> None:
    """Each request module registers how an approved/rejected decision is applied to its object."""
    _handlers[object_type] = fn


def unregister_handler(object_type: str) -> None:
    _handlers.pop(object_type, None)


def handler_for(object_type: str) -> DecisionHandler | None:
    return _handlers.get(object_type)


def register_summariser(object_type: str, fn: Summariser) -> None:
    """Inbox card context for object types owned by other modules (expense claims, travel, ...)."""
    _summarisers[object_type] = fn


register_summary = register_summariser


def npt_today() -> str:
    """Today's calendar date in Nepal as 'YYYY-MM-DD'."""
    return _npt_today().isoformat()


def assert_manages(user: AuthUser, employee_id: str, area: str | None = None) -> None:
    """
    Team-scoped access as the TS workflow helper: HR and admin for anyone; `area`'s specialist role; everyone for
    themselves; a manager for the people they manage. ForbiddenError('not allowed to act for this employee')
    otherwise. An employee outside the active company is NotFoundError('employee not found') first, so ids from
    another company of a group behave as not found (hajiri.core.auth.assert_manages does the same).
    """
    if user.company_id and is_uuid(employee_id):
        if not Employee.objects.for_company(user.company_id).filter(pk=employee_id).exists():
            raise NotFoundError('employee not found')
    if has_role(user, 'hr'):
        return
    if area and has_module(user, area):
        return
    if user.employee_id == str(employee_id):
        return
    if has_role(user, 'manager') and user.employee_id and is_manager_of(user.employee_id, str(employee_id)):
        return
    raise ForbiddenError('not allowed to act for this employee')


# ---------------------------------------------------------------- instances

INSTANCE_FIELDS = ('id', 'company_id', 'object_type', 'object_id', 'employee_id', 'steps', 'current_step', 'state',
                   'created_at', 'completed_at', 'routing', 'amount', 'flow_id')


def _inst(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'company_id', 'object_id', 'employee_id', 'flow_id'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['steps'] = out.get('steps') or []
    out['routing'] = out.get('routing') or []
    return out


def instances():
    """Every workflow instance as the dicts this app hands out (scope with .for_company before reading)."""
    return WorkflowInstance.objects.all()


def get_instance(instance_id: str) -> dict | None:
    return _inst(instances().filter(pk=as_uuid(instance_id)).values(*INSTANCE_FIELDS).first())


def instance_in(company_id: str | None, instance_id) -> dict | None:
    """The instance if it belongs to this company; another company's approval does not exist for the caller."""
    return _inst(instances().for_company(company_id).filter(pk=as_uuid(instance_id)).values(*INSTANCE_FIELDS).first())


def instance_for_object(object_type: str, object_id: str) -> dict | None:
    return _inst(instances().filter(object_type=object_type, object_id=as_uuid(object_id)).values(*INSTANCE_FIELDS).first())


def approvers_for_step(company_id: str, employee_id: str | None, step: dict | str | None,
                       cache: dict | None = None) -> list[str]:
    """
    Users who may act on a step: line manager, manager's manager, department or branch head, a role, a named user or a
    group, with away approvers handed to their delegate and HR as the last resort. A plain role string is accepted for
    older callers ('manager' or a role name).
    """
    if step is None:
        return []
    st = {'role': step} if isinstance(step, str) else step
    key = f"{company_id}|{employee_id or ''}|{json.dumps(st, sort_keys=False)}"
    if cache is not None and key in cache:
        return cache[key]
    ids = resolve_step(str(company_id), employee_id and str(employee_id), st, npt_today()).users
    if cache is not None:
        cache[key] = ids
    return ids


approvers_for = approvers_for_step


def _current_step(inst: dict) -> dict | None:
    steps = inst['steps']
    i = inst['current_step']
    return steps[i] if 0 <= i < len(steps) else None


def can_act(user: AuthUser, inst: dict, cache: dict | None = None) -> bool:
    """Admins may act anywhere, nobody may decide their own request."""
    if inst['state'] != State.PENDING:
        return False
    if inst.get('employee_id') and user.employee_id == str(inst['employee_id']):
        return False
    if user.company_id != str(inst['company_id']):
        return False
    if has_role(user, 'admin'):
        return True
    step = _current_step(inst)
    if not step:
        return False
    # HR supervises line managers and heads: it may decide any reporting-line step.
    if kind_of(step) in ('manager', 'manager2', 'department_head', 'branch_head') and has_role(user, 'hr'):
        return True
    return user.id in approvers_for_step(inst['company_id'], inst.get('employee_id'), step, cache)


def can_view(user: AuthUser, inst: dict) -> bool:
    """Who may read an approval: HR, the subject, the subject's line manager, a current approver, or anyone who already acted."""
    if user.company_id != str(inst['company_id']):
        return False
    if has_role(user, 'hr'):
        return True
    emp = inst.get('employee_id')
    if emp and user.employee_id == str(emp):
        return True
    if emp and user.employee_id and has_role(user, 'manager') and is_manager_of(user.employee_id, str(emp)):
        return True
    if can_act(user, inst):
        return True
    return ApprovalAction.objects.filter(Q(actor_id=user.id) | Q(on_behalf_of=user.id), instance_id=inst['id']).exists()


def _iso_ms(d: datetime) -> str:
    """The instant as UTC with milliseconds ('2026-10-27T04:19:00.123Z'), the TS engine's signature input."""
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def _now_ms() -> datetime:
    d = datetime.now(timezone.utc)
    return d.replace(microsecond=(d.microsecond // 1000) * 1000)


def start_workflow(*, company_id: str, object_type: str, object_id: str, employee_id: str | None, title: str, body: str,
                   amount=None) -> dict:
    """
    Open the approval for a request (call inside the request's transaction). The chain is fixed now (the most
    specific flow, then amount thresholds); who fills each step is worked out when someone looks, so manager changes
    and delegations apply to requests already waiting. The first approvers get `title`/`body` as a push notification
    (template '<object_type>.pending'). `amount` (NPR) is for money requests.
    """
    flow = choose_flow(str(company_id), object_type, employee_id and str(employee_id))
    steps = steps_for_amount(flow.steps, amount)
    created = WorkflowInstance.objects.create(
        company_id=company_id, object_type=object_type, object_id=object_id, employee_id=employee_id, steps=steps,
        amount=None if amount is None else Decimal(str(amount)), flow_id=flow.flow_id)
    inst = get_instance(created.pk)
    _notify_step_approvers(inst, title, body)
    return get_instance(inst['id'])


def _append_routing(instance_id: str, entries: list[dict]) -> None:
    """routing || entries in one UPDATE, so concurrent notes are never lost."""
    WorkflowInstance.objects.filter(pk=instance_id).update(routing=Func(
        F('routing'), Value(entries, output_field=JSONField()), arg_joiner=' || ', template='(%(expressions)s)',
        output_field=JSONField()))


def _notify_step_approvers(inst: dict, title: str, body: str) -> None:
    step = _current_step(inst)
    if not step:
        return
    res = resolve_step(inst['company_id'], inst.get('employee_id'), step, npt_today())
    notes = visible_notes(res)
    # Approver away, delegated or nobody set: say so on the request so the requester and HR can see why it went where it did.
    if notes or fell_back_to_hr(res):
        _append_routing(inst['id'], [{'step': inst['current_step'], 'at': _iso_ms(datetime.now(timezone.utc)), 'notes': notes}])
    for user_id in res.users:
        notify(company_id=inst['company_id'], user_id=user_id, template=f"{inst['object_type']}.pending", title=title, body=body,
               payload={'instanceId': inst['id'], 'objectType': inst['object_type'], 'objectId': inst['object_id'],
                        'step': inst['current_step']},
               channels=('push',))


def signature_of(instance_id: str, step: int, actor_id: str, decision: str, acted_at: datetime) -> str:
    """sha256 hex of 'instance|step|actor|decision|actedAt(ISO, ms)', the same input as the TS engine."""
    return hashlib.sha256(f'{instance_id}|{step}|{actor_id}|{decision}|{_iso_ms(acted_at)}'.encode()).hexdigest()


def act(*, instance_id: str, user: AuthUser, decision: str, reason: str | None = None, on_behalf_of: str | None = None,
        payload: dict | None = None) -> str:
    """
    Record a signed decision ('approve' | 'reject'). Reject ends the workflow; approve advances a step and the last
    step applies the object's handler. Must be called inside a transaction; the row is locked FOR UPDATE.
    Returns the new state.
    """
    decision = str(decision)
    inst = _inst(instances().select_for_update().filter(pk=as_uuid(instance_id)).values(*INSTANCE_FIELDS).first())
    if not inst:
        raise NotFoundError('approval not found')
    if inst['state'] != State.PENDING:
        raise ConflictError(f"request already {inst['state']}")
    if not can_act(user, inst):
        raise ForbiddenError('you are not an approver for this step')
    reason = (reason or '').strip() or None
    if decision == ApprovalAction.Decision.REJECT and not reason:
        raise BadRequestError('a reason is required to reject')
    if on_behalf_of:
        # Delegation is recorded, never assumed: only HR/admin may act on behalf of someone, and only for a real approver of this step.
        if not has_role(user, 'hr'):
            raise ForbiddenError('only HR may act on behalf of another approver')
        step = _current_step(inst)
        ids = approvers_for_step(inst['company_id'], inst.get('employee_id'), step) if step else []
        if str(on_behalf_of) not in ids or str(on_behalf_of) == user.id:
            raise BadRequestError('onBehalfOf must be an approver for this step')

    acted_at = _now_ms()
    signature = signature_of(inst['id'], inst['current_step'], user.id, decision, acted_at)
    ApprovalAction.objects.create(instance_id=inst['id'], step=inst['current_step'], actor_id=user.id, decision=decision,
                                  reason=reason, on_behalf_of_id=on_behalf_of and str(on_behalf_of), signature=signature,
                                  acted_at=acted_at)

    row = WorkflowInstance.objects.filter(pk=inst['id'])
    if decision == ApprovalAction.Decision.REJECT:
        state = State.REJECTED
        row.update(state=state, completed_at=Now())
    elif inst['current_step'] + 1 >= len(inst['steps']):
        state = State.APPROVED
        row.update(state=state, current_step=inst['current_step'] + 1, completed_at=Now())
    else:
        state = State.PENDING
        row.update(current_step=inst['current_step'] + 1)
        # Only the first '_' becomes a space (the TS String.replace with a string pattern).
        _notify_step_approvers({**inst, 'current_step': inst['current_step'] + 1}, 'Approval needed',
                               f"{inst['object_type'].replace('_', ' ', 1)} awaiting your decision (step {inst['current_step'] + 2})")
    audit(decision, inst['object_type'], actor_id=user.id, row_id=inst['object_id'],
          after={'instanceId': inst['id'], 'step': inst['current_step'], 'reason': reason, 'signature': signature})

    if state != State.PENDING:
        handler = _handlers.get(inst['object_type'])
        if handler:
            handler(inst['object_id'], str(state), DecisionContext(user.id, reason, payload or {}))
    return str(state)


def cancel_workflow(object_type: str, object_id: str) -> None:
    """The requester withdrew (or HR cancelled) the request: a pending approval ends as 'cancelled'."""
    (instances().filter(object_type=object_type, object_id=as_uuid(object_id), state=State.PENDING)
     .update(state=State.CANCELLED, completed_at=Now()))


def pending_older_than(hours: float) -> list[dict]:
    """Pending instances older than N hours with their current approvers, for SLA reminders and escalation."""
    cutoff = ExpressionWrapper(Now() - timedelta(hours=hours), output_field=DateTimeField())
    out = []
    for r in (instances().filter(state=State.PENDING, created_at__lt=cutoff).order_by('created_at', 'id')
              .values(*INSTANCE_FIELDS)):
        inst = _inst(r)
        step = _current_step(inst)
        out.append({'instance': inst, 'approvers': approvers_for_step(inst['company_id'], inst.get('employee_id'), step) if step else []})
    return out


# ---------------------------------------------------------------- decisions (the routes' logic)

def _recompute_after(employee_id: str | None) -> None:
    # An approval that changes attendance (leave, correction, work from home, overtime) shows up immediately.
    if not employee_id:
        return
    try:
        from hajiri.attendance.service import recompute_queued_for
    except ImportError:  # pragma: no cover - attendance is always installed
        return
    recompute_queued_for(employee_id)


def decide(user: AuthUser, instance_id: str, decision: str, *, reason: str | None = None, on_behalf_of: str | None = None,
           approved_min: int | None = None) -> str:
    """POST /workflow/:id/approve|reject. Another company's approval does not exist for this user (404, not 403)."""
    existing = instance_in(user.company_id, instance_id)
    if not existing:
        raise NotFoundError('approval not found')
    with transaction.atomic():
        state = act(instance_id=str(instance_id), user=user, decision=decision, reason=reason, on_behalf_of=on_behalf_of,
                    payload={'approvedMin': approved_min} if approved_min is not None else {})
    _recompute_after(existing.get('employee_id'))
    return state


def bulk_approve(user: AuthUser, ids: list[str]) -> list[dict]:
    """Approve several at once; each one commits or fails on its own: [{'id', 'state'} | {'id', 'error'}]."""
    results = []
    for i in ids:
        existing = instance_in(user.company_id, i)
        if not existing:
            results.append({'id': i, 'error': 'approval not found'})
            continue
        try:
            with transaction.atomic():
                state = act(instance_id=i, user=user, decision=ApprovalAction.Decision.APPROVE)
        except Exception as e:  # noqa: BLE001 - as the TS route: any failure is reported for that id, the rest go on
            results.append({'id': i, 'error': getattr(e, 'message', None) or str(e)})
            continue
        _recompute_after(existing.get('employee_id'))
        results.append({'id': i, 'state': state})
    return results


def detail(user: AuthUser, instance_id: str) -> dict:
    """GET /workflow/:id. Only the subject, their line manager, HR, current approvers and people who already acted may
    read it (leave reasons can be medical)."""
    inst = instance_in(user.company_id, instance_id)
    if not inst or not can_view(user, inst):
        raise NotFoundError('approval not found')
    return {'instance': inst, 'summary': summarise(inst), 'actions': timeline(inst)}


# ---------------------------------------------------------------- inbox

def _employee_of(row: dict) -> dict | None:
    if not row.get('employee_id'):
        return None
    return {'id': str(row['employee_id']), 'empCode': row.get('emp_code') or '', 'nameEn': row.get('name_en') or '',
            'nameNe': row.get('name_ne')}


def _pending_for(user: AuthUser) -> list[dict]:
    return [_inst(r) for r in instances().for_company(user.company_id).filter(state=State.PENDING)
            .order_by('created_at', 'id')
            .values(*INSTANCE_FIELDS, emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                    name_ne=F('employee__name_ne'))]


def inbox_for(user: AuthUser) -> list[dict]:
    """
    Everything the user can decide right now, with enough context for a one-tap decision: [{instanceId, objectType,
    objectId, step, totalSteps, createdAt, employee: {id, empCode, nameEn, nameNe} | None, summary, routing}].
    """
    if not user.company_id:
        return []
    cache: dict = {}
    rows = [row for row in _pending_for(user) if can_act(user, row, cache)]
    cards = summaries(rows)
    items = []
    for row in rows:
        items.append({
            'instanceId': row['id'], 'objectType': row['object_type'], 'objectId': row['object_id'],
            'step': row['current_step'], 'totalSteps': len(row['steps']), 'createdAt': row['created_at'],
            'employee': _employee_of(row), 'summary': cards[row['id']],
            'routing': [n for x in row['routing'] if x.get('step') == row['current_step'] for n in x.get('notes', [])],
        })
    return items


def inbox_count(user: AuthUser) -> int:
    """How many approvals the user can decide now (the Approvals badge); no summaries."""
    if not user.company_id:
        return 0
    cache: dict = {}
    return sum(1 for row in _pending_for(user) if can_act(user, row, cache))


def as_member_of(user: AuthUser, company_id: str) -> AuthUser | None:
    """The same person as they are in another of their companies (that company's roles and employee record), or None
    when they have no access there. Only for reading what they could see after switching company."""
    ref = user.company(company_id)
    if ref is None:
        return None
    return replace(user, company_id=ref.id, employee_id=ref.employee_id, roles=tuple(ref.roles), group_id=ref.group_id)


def inbox_elsewhere(user: AuthUser) -> list[dict]:
    """Group-wide: how many approvals wait for this person in each of their OTHER companies (only companies they can
    switch to, counted with their roles there) -> [{'companyId', 'name', 'count'}] for the ones with any, in the order of
    the company switcher."""
    out = []
    for ref in user.companies:
        if ref.id == user.company_id:
            continue
        there = as_member_of(user, ref.id)
        n = inbox_count(there) if there else 0
        if n:
            out.append({'companyId': ref.id, 'name': ref.name, 'count': n})
    return out


def _num(v) -> int | float:
    """A numeric column as a JSON number: integral values as int."""
    if v is None:
        return 0
    f = float(v)
    return int(f) if f.is_integer() else f


def _hhmm(t) -> str | None:
    return t.strftime('%H:%M') if t is not None else None


def _leave_summaries(company_id: str, ids) -> dict[str, dict]:
    """Inbox card context of several leave requests in one query: the request, the balance of its leave type and up to
    five colleagues off (pending or approved) on overlapping days."""
    if not ids:
        return {}
    balance = (LeaveLedger.objects.filter(employee_id=OuterRef('employee_id'), leave_type_id=OuterRef('leave_type_id'))
               .values('employee_id').annotate(b=Sum('delta_days')).values('b'))
    overlap = (LeaveRequest.objects.for_company(company_id).exclude(pk=OuterRef('pk')).exclude(employee_id=OuterRef('employee_id'))
               .filter(status__in=(LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED),
                       from_date__lte=OuterRef('to_date'), to_date__gte=OuterRef('from_date'))
               .values('employee__name_en')[:5])
    found = (LeaveRequest.objects.for_company(company_id).filter(pk__in=[as_uuid(i) for i in ids])
             .annotate(balance=Coalesce(Subquery(balance), Value(Decimal(0)), output_field=DecimalField()),
                       overlap=ArraySubquery(overlap))
             .values('id', 'from_date', 'to_date', 'half_day', 'days_requested', 'reason', 'from_time', 'to_time', 'hours',
                     'balance', 'overlap', type_code=F('leave_type__code'), type_name=F('leave_type__name_en')))
    out = {}
    for lr in found:
        balance_now = _num(lr['balance'])
        days = _num(lr['days_requested'])
        s = {
            'leaveType': lr['type_code'], 'leaveTypeName': lr['type_name'], 'from': lr['from_date'].isoformat(),
            'to': lr['to_date'].isoformat(), 'halfDay': lr['half_day'], 'days': days, 'reason': lr['reason'],
            'balanceNow': balance_now, 'balanceAfter': _num(Decimal(str(balance_now)) - Decimal(str(days))),
            'teamOverlap': list(lr['overlap'] or []),
        }
        if lr['from_time']:
            s.update({'fromTime': _hhmm(lr['from_time']), 'toTime': _hhmm(lr['to_time']), 'hours': _num(lr['hours'])})
        out[str(lr['id'])] = s
    return out


def _leave_summary(company_id: str, oid: str) -> dict:
    return _leave_summaries(company_id, [oid]).get(str(oid), {})


_REG_FIELDS = ('local_date', 'kind', 'punch_time', 'punch_direction', 'shift_id', 'reason')
_OT_FIELDS = ('local_date', 'requested_min', 'reason')


def _rows_by_id(model, company_id: str, ids, fields) -> dict[str, dict]:
    if not ids:
        return {}
    return {str(r.pop('pk')): r for r in model.objects.for_company(company_id).filter(pk__in=[as_uuid(i) for i in ids])
            .values('pk', *fields)}


def summaries(insts: list[dict]) -> dict[str, dict]:
    """summarise() for many instances of one company ({instance id: summary}), with the built-in leave, correction and
    overtime cards read in one query per type instead of one per card."""
    if not insts:
        return {}
    cid = insts[0]['company_id']
    of = lambda t: [i['object_id'] for i in insts if i['object_type'] == t]  # noqa: E731
    batched = {'leave_request': _leave_summaries(cid, of('leave_request')),
               'regularisation_request': _rows_by_id(RegularisationRequest, cid, of('regularisation_request'), _REG_FIELDS),
               'ot_request': _rows_by_id(OtRequest, cid, of('ot_request'), _OT_FIELDS)}
    out = {}
    for i in insts:
        found = batched.get(i['object_type'])
        out[i['id']] = found.get(str(i['object_id']), {}) if found is not None else summarise(i)
    return out


def _payroll_summary(company_id: str, oid: str) -> dict:
    r = (PayrollRun.objects.for_company(company_id).filter(pk=oid)
         .annotate(n_payslips=Count('payslips'), net_total=Coalesce(Sum('payslips__net'), Value(Decimal(0)),
                                                                     output_field=DecimalField()))
         .values('run_type', 'status', 'n_payslips', 'net_total', bs_year=F('period__bs_year'),
                 bs_month=F('period__bs_month')).first())
    if not r:
        return {}
    return {'run_type': r['run_type'], 'status': r['status'], 'bs_year': r['bs_year'], 'bs_month': r['bs_month'],
            'payslips': r['n_payslips'], 'total_net': str(r['net_total'])}


def _training_summary(company_id: str, oid: str) -> dict:
    r = (LrnEnrollment.objects.for_company(company_id).filter(pk=oid)
         .values('reason', course_title=F('course__title'), delivery=F('course__delivery'), course_cost=F('course__cost'),
                 starts_at=F('session__starts_at')).first())
    if not r:
        return {}
    return {'course_title': r['course_title'], 'delivery': r['delivery'], 'cost': r['course_cost'], 'reason': r['reason'],
            'starts_at': r['starts_at']}


def summarise(inst: dict) -> dict:
    """Object-specific context for the inbox card (plan §13.5 manager inbox card)."""
    t, oid, cid = inst['object_type'], inst['object_id'], inst['company_id']
    if t == 'leave_request':
        return _leave_summary(cid, oid)
    if t == 'regularisation_request':
        return RegularisationRequest.objects.for_company(cid).filter(pk=oid).values(*_REG_FIELDS).first() or {}
    if t == 'ot_request':
        return OtRequest.objects.for_company(cid).filter(pk=oid).values(*_OT_FIELDS).first() or {}
    if t == 'payroll_run':
        return _payroll_summary(cid, oid)
    if t == 'timesheet' and t not in _summarisers:
        try:
            from hajiri.timesheets.services import summarise_timesheet  # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            return {}
        return summarise_timesheet(oid)
    if t == 'letter_request':
        return (LetterRequest.objects.for_company(cid).filter(pk=oid)
                .values('purpose', 'addressed_to', letter=F('template__name')).first()) or {}
    if t == 'training_nomination':
        return _training_summary(cid, oid)
    fn = _summarisers.get(t)
    return fn(inst) if fn else {}


def timeline(inst: dict) -> list[dict]:
    return list(ApprovalAction.objects.filter(instance_id=inst['id']).order_by('acted_at', 'id')
                .values('step', 'decision', 'reason', 'acted_at', 'signature', 'on_behalf_of', 'actor_id',
                        actor_name=F('actor__name')))
