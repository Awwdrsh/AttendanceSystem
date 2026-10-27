"""
Generic multi-level approval engine (port of apps/api/src/modules/workflow/service.ts, plan §5.8): one
workflow_instance per request object, steps copied from the company's flow when the request is made, signed
approval_action rows, and per-object-type decision handlers that apply the business effect (ledger posting, punch
creation, ...) in the same transaction as the decision.

How a request module uses it (see the package docstring in hajiri/workflow/__init__.py for the full contract):

    from hajiri.workflow.services import register_handler, start_workflow, cancel_workflow, DecisionContext

    def _decided(object_id: str, state: str, ctx: DecisionContext) -> None:   # 'approved' | 'rejected'
        execute("UPDATE leave_request SET status = %s, decided_at = now() WHERE id = %s", [state, object_id])

    register_handler('leave_request', _decided)        # at import time (e.g. in your services.py or apps.ready)

    with transaction.atomic():
        lr = one("INSERT INTO leave_request (...) VALUES (...) RETURNING id")
        inst = start_workflow(company_id=..., object_type='leave_request', object_id=lr['id'], employee_id=emp,
                              title='Leave request', body='Sita Sharma asked for 2 days of home leave')
        execute('UPDATE leave_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], lr['id']])
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from django.db import DatabaseError, transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_module, has_role, is_manager_of
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.notifications.service import notify

from .flows import choose_flow, fell_back_to_hr, kind_of, resolve_step, steps_for_amount, visible_notes

log = logging.getLogger('hajiri.workflow')

STATES = ('pending', 'approved', 'rejected', 'cancelled')


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
    otherwise. Callers check the employee's company separately (hajiri.core.auth.assert_manages does both).
    """
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


def get_instance(instance_id: str) -> dict | None:
    return _inst(one('SELECT * FROM workflow_instance WHERE id = %s', [str(instance_id)]))


def instance_for_object(object_type: str, object_id: str) -> dict | None:
    return _inst(one('SELECT * FROM workflow_instance WHERE object_type = %s AND object_id = %s', [object_type, str(object_id)]))


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
    if inst['state'] != 'pending':
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
    return bool(rows('SELECT 1 FROM approval_action WHERE instance_id = %s AND (actor_id = %s OR on_behalf_of = %s) LIMIT 1',
                     [inst['id'], user.id, user.id]))


def _iso_ms(d: datetime) -> str:
    """JS Date.toISOString(): UTC with milliseconds."""
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
    r = one("""INSERT INTO workflow_instance (company_id, object_type, object_id, employee_id, steps, amount, flow_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            [str(company_id), object_type, str(object_id), employee_id and str(employee_id), json.dumps(steps),
             None if amount is None else Decimal(str(amount)), flow.flow_id])
    inst = _inst(r)
    _notify_step_approvers(inst, title, body)
    return get_instance(inst['id'])


def _notify_step_approvers(inst: dict, title: str, body: str) -> None:
    step = _current_step(inst)
    if not step:
        return
    res = resolve_step(inst['company_id'], inst.get('employee_id'), step, npt_today())
    notes = visible_notes(res)
    # Approver away, delegated or nobody set: say so on the request so the requester and HR can see why it went where it did.
    if notes or fell_back_to_hr(res):
        execute('UPDATE workflow_instance SET routing = routing || %s::jsonb WHERE id = %s',
                [json.dumps([{'step': inst['current_step'], 'at': _iso_ms(datetime.now(timezone.utc)), 'notes': notes}]), inst['id']])
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
    inst = _inst(one('SELECT * FROM workflow_instance WHERE id = %s FOR UPDATE', [str(instance_id)]))
    if not inst:
        raise NotFoundError('approval not found')
    if inst['state'] != 'pending':
        raise ConflictError(f"request already {inst['state']}")
    if not can_act(user, inst):
        raise ForbiddenError('you are not an approver for this step')
    reason = (reason or '').strip() or None
    if decision == 'reject' and not reason:
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
    execute("""INSERT INTO approval_action (instance_id, step, actor_id, decision, reason, on_behalf_of, signature, acted_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [inst['id'], inst['current_step'], user.id, decision, reason, on_behalf_of and str(on_behalf_of), signature, acted_at])

    if decision == 'reject':
        state = 'rejected'
        execute("UPDATE workflow_instance SET state = 'rejected', completed_at = now() WHERE id = %s", [inst['id']])
    elif inst['current_step'] + 1 >= len(inst['steps']):
        state = 'approved'
        execute("UPDATE workflow_instance SET state = 'approved', current_step = %s, completed_at = now() WHERE id = %s",
                [inst['current_step'] + 1, inst['id']])
    else:
        state = 'pending'
        execute('UPDATE workflow_instance SET current_step = %s WHERE id = %s', [inst['current_step'] + 1, inst['id']])
        # JS String.replace with a string pattern replaces only the first '_'.
        _notify_step_approvers({**inst, 'current_step': inst['current_step'] + 1}, 'Approval needed',
                               f"{inst['object_type'].replace('_', ' ', 1)} awaiting your decision (step {inst['current_step'] + 2})")
    audit(decision, inst['object_type'], actor_id=user.id, row_id=inst['object_id'],
          after={'instanceId': inst['id'], 'step': inst['current_step'], 'reason': reason, 'signature': signature})

    if state != 'pending':
        handler = _handlers.get(inst['object_type'])
        if handler:
            handler(inst['object_id'], state, DecisionContext(user.id, reason, payload or {}))
    return state


def cancel_workflow(object_type: str, object_id: str) -> None:
    """The requester withdrew (or HR cancelled) the request: a pending approval ends as 'cancelled'."""
    execute("""UPDATE workflow_instance SET state = 'cancelled', completed_at = now()
                WHERE object_type = %s AND object_id = %s AND state = 'pending'""", [object_type, str(object_id)])


def pending_older_than(hours: float) -> list[dict]:
    """Pending instances older than N hours with their current approvers, for SLA reminders and escalation."""
    out = []
    for r in rows("""SELECT * FROM workflow_instance WHERE state = 'pending' AND created_at < now() - make_interval(hours => %s)
                      ORDER BY created_at""", [hours]):
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
    existing = get_instance(instance_id)
    if not existing or existing['company_id'] != user.company_id:
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
        existing = get_instance(i)
        if not existing or existing['company_id'] != user.company_id:
            results.append({'id': i, 'error': 'approval not found'})
            continue
        try:
            with transaction.atomic():
                state = act(instance_id=i, user=user, decision='approve')
        except Exception as e:  # noqa: BLE001 - as the TS route: any failure is reported for that id, the rest go on
            results.append({'id': i, 'error': getattr(e, 'message', None) or str(e)})
            continue
        _recompute_after(existing.get('employee_id'))
        results.append({'id': i, 'state': state})
    return results


def detail(user: AuthUser, instance_id: str) -> dict:
    """GET /workflow/:id. Only the subject, their line manager, HR, current approvers and people who already acted may
    read it (leave reasons can be medical)."""
    inst = get_instance(instance_id)
    if not inst or inst['company_id'] != user.company_id or not can_view(user, inst):
        raise NotFoundError('approval not found')
    return {'instance': inst, 'summary': summarise(inst), 'actions': timeline(inst)}


# ---------------------------------------------------------------- inbox

def _employee_of(row: dict) -> dict | None:
    if not row.get('employee_id'):
        return None
    return {'id': str(row['employee_id']), 'empCode': row.get('emp_code') or '', 'nameEn': row.get('name_en') or '',
            'nameNe': row.get('name_ne')}


def _pending_for(user: AuthUser) -> list[dict]:
    return [_inst(r) | {'emp_code': r['emp_code'], 'name_en': r['name_en'], 'name_ne': r['name_ne']} for r in rows(
        """SELECT w.*, e.emp_code, e.name_en, e.name_ne FROM workflow_instance w LEFT JOIN employee e ON e.id = w.employee_id
            WHERE w.company_id = %s AND w.state = 'pending' ORDER BY w.created_at""", [user.company_id])]


def inbox_for(user: AuthUser) -> list[dict]:
    """
    Everything the user can decide right now, with enough context for a one-tap decision: [{instanceId, objectType,
    objectId, step, totalSteps, createdAt, employee: {id, empCode, nameEn, nameNe} | None, summary, routing}].
    """
    if not user.company_id:
        return []
    items = []
    cache: dict = {}
    for row in _pending_for(user):
        if not can_act(user, row, cache):
            continue
        items.append({
            'instanceId': row['id'], 'objectType': row['object_type'], 'objectId': row['object_id'],
            'step': row['current_step'], 'totalSteps': len(row['steps']), 'createdAt': row['created_at'],
            'employee': _employee_of(row), 'summary': summarise(row),
            'routing': [n for x in row['routing'] if x.get('step') == row['current_step'] for n in x.get('notes', [])],
        })
    return items


def inbox_count(user: AuthUser) -> int:
    """How many approvals the user can decide now (the Approvals badge); no summaries."""
    if not user.company_id:
        return 0
    cache: dict = {}
    return sum(1 for row in _pending_for(user) if can_act(user, row, cache))


def _num(v) -> int | float:
    """JS Number() of a numeric column: integral values as int."""
    if v is None:
        return 0
    f = float(v)
    return int(f) if f.is_integer() else f


def _safe_rows(sql: str, params) -> list[dict]:
    """A query against a table that may not exist yet in older databases (the TS .catch(() => ({rows: []})))."""
    try:
        with transaction.atomic():
            return rows(sql, params)
    except DatabaseError:
        return []


def summarise(inst: dict) -> dict:
    """Object-specific context for the inbox card (plan §13.5 manager inbox card)."""
    t, oid = inst['object_type'], inst['object_id']
    if t == 'leave_request':
        lr = one("""SELECT lr.from_date, lr.to_date, lr.half_day, lr.days_requested, lr.reason, lt.code AS type_code,
                           lt.name_en AS type_name, lr.leave_type_id, lr.employee_id,
                           to_char(lr.from_time, 'HH24:MI') AS from_time, to_char(lr.to_time, 'HH24:MI') AS to_time, lr.hours
                      FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id WHERE lr.id = %s""", [oid])
        if not lr:
            return {}
        bal = one('SELECT coalesce(sum(delta_days), 0)::text AS balance FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s',
                  [lr['employee_id'], lr['leave_type_id']])
        overlap = rows("""SELECT e.name_en FROM leave_request o JOIN employee e ON e.id = o.employee_id
                           WHERE o.id <> %s AND o.status IN ('pending', 'approved') AND o.employee_id <> %s
                             AND o.from_date <= %s::date AND o.to_date >= %s::date AND e.company_id = %s LIMIT 5""",
                       [oid, lr['employee_id'], lr['to_date'], lr['from_date'], inst['company_id']])
        balance = _num(Decimal(bal['balance']) if bal else 0)
        days = _num(lr['days_requested'])
        out = {
            'leaveType': lr['type_code'], 'leaveTypeName': lr['type_name'], 'from': lr['from_date'].isoformat(),
            'to': lr['to_date'].isoformat(), 'halfDay': lr['half_day'], 'days': days, 'reason': lr['reason'],
            'balanceNow': balance, 'balanceAfter': _num(Decimal(str(balance)) - Decimal(str(days))),
            'teamOverlap': [x['name_en'] for x in overlap],
        }
        if lr['from_time']:
            out.update({'fromTime': lr['from_time'], 'toTime': lr['to_time'], 'hours': _num(lr['hours'])})
        return out
    if t == 'regularisation_request':
        return one('SELECT local_date, kind, punch_time, punch_direction, shift_id, reason FROM regularisation_request WHERE id = %s',
                   [oid]) or {}
    if t == 'ot_request':
        return one('SELECT local_date, requested_min, reason FROM ot_request WHERE id = %s', [oid]) or {}
    if t == 'payroll_run':
        r = _safe_rows("""SELECT r.run_type, r.status, p.bs_year, p.bs_month, (SELECT count(*) FROM payslip s WHERE s.run_id = r.id) AS payslips,
                                 (SELECT coalesce(sum(net), 0)::text FROM payslip s WHERE s.run_id = r.id) AS total_net
                            FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.id = %s""", [oid])
        return r[0] if r else {}
    if t == 'timesheet' and t not in _summarisers:
        try:
            from hajiri.timesheets.services import summarise_timesheet  # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            return {}
        return summarise_timesheet(oid)
    if t == 'letter_request':
        return one("""SELECT t.name AS letter, lr.purpose, lr.addressed_to FROM letter_request lr
                        JOIN letter_template t ON t.id = lr.template_id WHERE lr.id = %s""", [oid]) or {}
    if t == 'training_nomination':
        return one("""SELECT c.title AS course_title, c.delivery, c.cost, e.reason, s.starts_at FROM lrn_enrollment e
                        JOIN lrn_course c ON c.id = e.course_id LEFT JOIN lrn_session s ON s.id = e.session_id
                       WHERE e.id = %s""", [oid]) or {}
    fn = _summarisers.get(t)
    return fn(inst) if fn else {}


def timeline(inst: dict) -> list[dict]:
    return rows("""SELECT a.step, a.decision, a.reason, a.acted_at, a.signature, a.on_behalf_of, u.name AS actor_name, u.id AS actor_id
                     FROM approval_action a JOIN app_user u ON u.id = a.actor_id WHERE a.instance_id = %s ORDER BY a.acted_at""",
                [inst['id']])
