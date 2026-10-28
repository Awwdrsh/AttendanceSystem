"""
Leave service layer (port of apps/api/src/modules/leave/routes.ts): leave types and the leave order, balances, the
deduction preview, requests (with the approval workflow), cancellation, HR adjustments, the ledger, the accrual
catch-up and the team calendar. Bodies and results use the TS API's camelCase shapes, so the JSON endpoints, the pages
and other modules share one implementation. The rules live in service.py and accrual.py; the request contracts in
serializers.py. Every query is scoped to the user's active company.

Other modules use:
    credit_comp_off(employee_id, days, expires_on, ref_type=, ref_id=, note=, posted_by=) -> ledger id   (OT comp-off)
    encashable_balances(employee_id) -> [{'leaveTypeId', 'code', 'days'}]                              (final settlement)
    post_encashment(employee_id, leave_type_id, days, ref_type=, ref_id=, posted_by=)
    ledger_balance(employee_id, leave_type_id, as_of=None), pending_days(employee_id, leave_type_id)   (late penalties)
    run_monthly_accrual(company_id, bs_year, bs_month), run_leave_catch_up(company_id, as_of)          (worker hooks)
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Now

from engines.bs_calendar import ad_to_bs, fiscal_year, fiscal_year_of

from hajiri.attendance.service import date_range, enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, has_module, has_role, manages
from hajiri.core.bsdate import bs_range
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import plain, validate
from hajiri.db.models import (
    AttendanceDay, Employee, EmployeeAssignment, LeaveLedger, LeaveRequest, LeaveType,
)
from hajiri.notifications.service import notify, user_id_for_employee  # noqa: F401 (user_id_for_employee: callers)
from hajiri.workflow.service import DecisionContext, assert_manages, cancel_workflow, register_handler, start_workflow

from .accrual import allocate_annual_leave, expire_substitute_leave, run_leave_catch_up, run_monthly_accrual  # noqa: F401
from .filters import LeaveRequestFilter
from .serializers import (
    AdjustIn, CancelIn, ListQueryIn, OrderIn, PreviewQueryIn, RequestIn, TeamCalendarQueryIn, TypeIn,
)
from .service import (  # noqa: F401
    EVENT_GAP_DAYS, HELD, HOURLY_MIN_MINUTES, Reason, Status, assert_no_overlap, balances, bridge_across,
    compute_hourly_leave, compute_leave_days, credit_comp_off, day, dec, encashable_balances, event_days_used, fixed,
    get_employee_lite, get_leave_type, group_head, has_locked_day, hhmm, iso, js, latest_days, leave_type_by_code,
    ledger_balance, num, out, pending_days, post_encashment, type_rows, working_days_between,
)


def npt_today() -> str:
    """Today's calendar date in Nepal as 'YYYY-MM-DD' (the shape the TS services passed around)."""
    return _npt_today().isoformat()


class LeaveSuggestionError(BadRequestError):
    """A 400 that also carries a suggestion (the TS route answered {error, suggestion})."""

    def __init__(self, message: str, suggestion: dict):
        super().__init__(message)
        self.suggestion = suggestion


def _require_module(user: AuthUser, area: str) -> None:
    if not has_module(user, area):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES[area])}")


def _company_of(user: AuthUser) -> str:
    if not user.company_id:
        raise ForbiddenError('user is not attached to a company')
    return user.company_id


def leave_policy(company_id: str) -> dict:
    v = get_setting(company_id, 'leave.policy') or {}
    mcd = v.get('maxContinuousDays')
    return {'sandwichRule': v.get('sandwichRule') is True, 'probationRestriction': v.get('probationRestriction') is not False,
            'maxContinuousDays': mcd if isinstance(mcd, (int, float)) and not isinstance(mcd, bool) else 30}


def _ledger_row(**kw) -> LeaveLedger:
    if 'delta_days' in kw:
        kw['delta_days'] = dec(kw['delta_days'])
    return LeaveLedger(**kw)


# ================================================================ workflow decision

def apply_leave_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """Approval/rejection effect for leave requests (registered with the workflow engine, which has already scoped
    the instance to its company)."""
    lr = (LeaveRequest.objects.select_for_update(of=('self',)).select_related('leave_type')
          .filter(pk=request_id).first())
    if not lr:
        raise NotFoundError('leave request not found')
    if lr.status != Status.PENDING:
        raise ConflictError(f'leave request already {lr.status}')
    emp_id, frm, to = str(lr.employee_id), iso(lr.from_date), iso(lr.to_date)
    if state == Status.APPROVED and has_locked_day(emp_id, frm, to):
        raise ConflictError('the leave falls in a payroll-locked period; reject it and let HR raise an adjustment')
    LeaveRequest.objects.filter(pk=lr.pk).update(status=state, decided_at=Now())
    if state == Status.APPROVED:
        _ledger_row(employee_id=emp_id, leave_type_id=lr.leave_type_id, effective_date=lr.from_date,
                    delta_days=-num(lr.days_requested), reason=Reason.USAGE, ref_type='leave_request', ref_id=lr.pk,
                    posted_by_id=ctx.actor_id).save()
        _post_bridge(lr, ctx.actor_id)
        enqueue_recompute([(emp_id, d) for d in date_range(frm, to)], 'leave_approved')
    notify(company_id=str(lr.leave_type.company_id), user_id=user_id_for_employee(emp_id), employee_id=emp_id,
           template=f'leave.{state}', title='Leave approved' if state == Status.APPROVED else 'Leave rejected',
           body=f"{lr.leave_type.name_en} {bs_range(frm, to)} was {state}{f': {ctx.reason}' if ctx.reason else ''}.",
           payload={'requestId': str(request_id)}, channels=('push', 'sms'))


def _post_bridge(lr: LeaveRequest, actor_id: str) -> None:
    """On approval, charge the bridged non-working days to the type that pays for them, if the leave on the other side is
    still pending or approved; otherwise the bridge is dropped."""
    if not lr.bridge_type_id or num(lr.bridge_days) <= 0 or not lr.bridge_from or not lr.bridge_to:
        return
    other = (LeaveRequest.objects.filter(employee_id=lr.employee_id, status__in=HELD).exclude(pk=lr.pk)
             .filter(Q(to_date=lr.bridge_from - timedelta(days=1)) | Q(from_date=lr.bridge_to + timedelta(days=1))).exists())
    if not other:
        LeaveRequest.objects.filter(pk=lr.pk).update(bridge_days=0, bridge_type=None, bridge_from=None, bridge_to=None)
        return
    _ledger_row(employee_id=lr.employee_id, leave_type_id=lr.bridge_type_id, effective_date=lr.bridge_from,
                delta_days=-num(lr.bridge_days), reason=Reason.USAGE, ref_type='leave_bridge', ref_id=lr.pk,
                posted_by_id=actor_id, note='Weekly-off/holiday days between two leaves (sandwich rule)').save()


register_handler('leave_request', apply_leave_decision)


# ================================================================ leave types

# Columns that define a statutory entitlement (Labour Act Ch. 9): not editable once statutory.
STATUTORY_LOCKED = ('accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays', 'eventDays')

_TYPE_COLUMNS = (
    ('code', 'code'), ('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('paid', 'paid'), ('accrualBasis', 'accrual_basis'),
    ('accrualDays', 'accrual_days'), ('accrualPerWorkedDays', 'accrual_per_worked_days'), ('maxBalance', 'max_balance'),
    ('carryForward', 'carry_forward'), ('encashable', 'encashable'), ('certificateAfterDays', 'certificate_after_days'),
    ('allowHalfDay', 'allow_half_day'), ('maxConsecutiveDays', 'max_consecutive_days'), ('minServiceDays', 'min_service_days'),
    ('negativeBalanceLimit', 'negative_balance_limit'), ('genderScope', 'gender_scope'), ('sandwichRule', 'sandwich_rule'),
    ('expiryDays', 'expiry_days'), ('eventDays', 'event_days'), ('allowHourly', 'allow_hourly'), ('clubWithId', 'club_with_id'),
    ('sortOrder', 'sort_order'),
)
_DECIMAL_COLUMNS = ('accrual_days', 'max_balance', 'negative_balance_limit', 'event_days')


def _column(col: str, v):
    return dec(v) if col in _DECIMAL_COLUMNS and v is not None else v


def _types(company_id):
    return LeaveType.objects.for_company(company_id)


def list_types(user: AuthUser) -> list[dict]:
    """GET /leave/types: the company's leave types in leave order."""
    return type_rows(_types(user.company_id).order_by('sort_order', 'code'))


def own_type(user: AuthUser, type_id) -> dict:
    """One leave type of the user's company, or NotFoundError('leave type not found')."""
    t = get_leave_type(type_id, user.company_id)
    if not t:
        raise NotFoundError('leave type not found')
    return t


def _assert_club_target(company_id: str, type_id: str, target_id: str) -> None:
    """A type can be clubbed with another type of the same company that is not itself clubbed (no chains or loops)."""
    if str(type_id) == str(target_id):
        raise BadRequestError('a leave type cannot be clubbed with itself')
    t = _types(company_id).filter(pk=target_id).values('club_with_id').first()
    if not t:
        raise BadRequestError('the leave type to club with does not exist')
    if t['club_with_id']:
        raise BadRequestError('that leave type is itself clubbed with another; club with the main type instead')
    if LeaveType.objects.filter(club_with_id=type_id).exists():
        raise BadRequestError('other leave types are clubbed with this one, so it cannot follow another type')


def create_type(user: AuthUser, body: dict) -> str:
    """POST /leave/types (attendance area). Returns the new id."""
    _require_module(user, 'attendance')
    b = plain(validate(TypeIn, body))
    company = _company_of(user)
    with transaction.atomic():
        t = LeaveType.objects.create(
            company_id=company, code=b['code'], name_en=b['nameEn'], name_ne=b.get('nameNe'), paid=b['paid'], statutory=False,
            accrual_basis=b['accrualBasis'], accrual_days=dec(b['accrualDays']),
            accrual_per_worked_days=b.get('accrualPerWorkedDays'), max_balance=_column('max_balance', b.get('maxBalance')),
            carry_forward=b['carryForward'], encashable=b['encashable'], certificate_after_days=b.get('certificateAfterDays'),
            allow_half_day=b['allowHalfDay'], max_consecutive_days=b.get('maxConsecutiveDays'),
            min_service_days=b['minServiceDays'], negative_balance_limit=dec(b['negativeBalanceLimit']),
            gender_scope=b['genderScope'], sandwich_rule=b['sandwichRule'], expiry_days=b.get('expiryDays'),
            event_days=_column('event_days', b.get('eventDays')), sort_order=100,
            allow_hourly=bool(b['allowHourly'] and b['paid'] and b.get('eventDays') is None))
        new_id = str(t.pk)
        later = {}
        if b.get('clubWithId'):
            _assert_club_target(company, new_id, b['clubWithId'])
            later['club_with_id'] = b['clubWithId']
        if 'sortOrder' in b:
            later['sort_order'] = b['sortOrder']
        if later:
            LeaveType.objects.filter(pk=new_id).update(**later)
        audit('create', 'leave_type', actor_id=user.id, row_id=new_id, after=b)
    return new_id


def update_type(user: AuthUser, type_id: str, body: dict) -> None:
    """PATCH /leave/types/:id (attendance area). Only the keys sent are changed."""
    _require_module(user, 'attendance')
    b = plain(validate(TypeIn, body if isinstance(body, dict) else {}, partial=True))
    cur = _types(user.company_id).filter(pk=as_uuid(type_id)).values().first()
    if not cur:
        raise NotFoundError('leave type not found')
    if cur['statutory']:
        locked = [k for k in b if k in STATUTORY_LOCKED]
        if locked:
            raise BadRequestError(f"statutory leave entitlement fields are locked: {', '.join(locked)} (Labour Act 2074 Ch. 9)")
    if b.get('clubWithId'):
        _assert_club_target(str(cur['company_id']), str(type_id), b['clubWithId'])
    if b.get('allowHourly'):
        paid = b['paid'] if 'paid' in b else cur['paid']
        event = b['eventDays'] if 'eventDays' in b else cur['event_days']
        if not paid or event is not None:
            raise BadRequestError('only paid leave with a balance can be taken by the hour')
    changes = {col: _column(col, b[k]) for k, col in _TYPE_COLUMNS if k in b}
    with transaction.atomic():
        if changes:
            LeaveType.objects.filter(pk=cur['id']).update(**changes)
        audit('update', 'leave_type', actor_id=user.id, row_id=str(type_id), before=type_row(cur), after=b)


def type_row(r: dict) -> dict:
    """A leave_type row from `.values()` with its ids as strings."""
    t = dict(r)
    for k in ('id', 'company_id', 'club_with_id'):
        if t.get(k) is not None:
            t[k] = str(t[k])
    return t


_type_row = type_row


def reorder_types(user: AuthUser, ids: list[str]) -> None:
    """POST /leave/types/order (HR): the listed types get sort orders 10, 20, 30..."""
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    b = plain(validate(OrderIn, {'ids': ids}))
    company = _company_of(user)
    if _types(company).filter(pk__in=b['ids']).count() != len(set(b['ids'])):
        raise BadRequestError('a leave type in the list does not exist')
    order = {tid: (i + 1) * 10 for i, tid in enumerate(b['ids'])}       # a repeated id keeps its last place
    with transaction.atomic():
        LeaveType.objects.bulk_update([LeaveType(pk=tid, sort_order=n) for tid, n in order.items()], ['sort_order'])
        audit('reorder', 'leave_type', actor_id=user.id, after={'ids': b['ids']})


# ================================================================ balances and preview

def _employee_in_company(user: AuthUser, employee_id: str | None) -> dict:
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    emp = get_employee_lite(as_uuid(eid), user.company_id)
    if not emp:
        raise NotFoundError('employee not found')
    return emp


def get_balances(user: AuthUser, employee_id: str | None = None, as_of: str | None = None) -> dict:
    """GET /leave/balances: {'employeeId', 'balances'}; the employee, their managers and the attendance area."""
    emp = _employee_in_company(user, employee_id)
    assert_manages(user, emp['id'], 'attendance')
    allocate_annual_leave(emp['company_id'], npt_today(), emp['id'])
    return {'employeeId': emp['id'], 'balances': balances(emp, as_of or npt_today())}


def _hourly_window(b: dict) -> dict | None:
    """An hourly request names both times, a single date and no half day; None for day-based requests."""
    if 'fromTime' not in b and 'toTime' not in b:
        return None
    if not b.get('fromTime') or not b.get('toTime'):
        raise BadRequestError('give both a start and an end time for hourly leave')
    if b['from'] != b['to']:
        raise BadRequestError('hourly leave must be on a single date')
    if b.get('halfDay'):
        raise BadRequestError('choose either a half day or hours, not both')
    return {'fromTime': b['fromTime'], 'toTime': b['toTime']}


def _emp_and_type(user: AuthUser, employee_id: str | None, leave_type_id: str) -> tuple[dict, dict]:
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    emp = get_employee_lite(as_uuid(eid), user.company_id)
    t = get_leave_type(as_uuid(leave_type_id), user.company_id)
    if not emp or not t:
        raise NotFoundError('employee or leave type not found')
    return emp, t


def preview(user: AuthUser, query: dict) -> dict:
    """GET /leave/preview: the deduction and the balance after it, before submitting (plan §13.5 computed line)."""
    q = plain(validate(PreviewQueryIn, query))
    emp, t = _emp_and_type(user, q.get('employeeId'), q['leaveTypeId'])
    assert_manages(user, emp['id'], 'attendance')
    policy = leave_policy(emp['company_id'])
    hourly = _hourly_window(q)
    calc = (compute_hourly_leave(emp, t, q['from'], hourly['fromTime'], hourly['toTime']) if hourly
            else compute_leave_days(emp, t, q['from'], q['to'], q.get('halfDay'), policy['sandwichRule']))
    bridge = (bridge_across(emp, t, q['from'], q['to'], policy['sandwichRule'])
              if not hourly and not q.get('halfDay') else None)
    if t['event_days'] is not None:
        left = num(t['event_days']) - event_days_used(emp['id'], t['id'], q['from'], q['to'])
        return {**calc, 'balance': out(num(t['event_days'])), 'available': out(left), 'balanceAfter': out(left - calc['days'])}
    allocate_annual_leave(emp['company_id'], npt_today(), emp['id'])
    bal = ledger_balance(emp['id'], t['id'])
    pend = pending_days(emp['id'], t['id'])
    own_bridge = bridge['days'] if bridge and bridge['typeId'] == t['id'] else 0
    return {**calc, 'bridge': bridge, 'balance': out(bal), 'available': out(bal - pend),
            'balanceAfter': out(bal - pend - calc['days'] - own_bridge)}


# ================================================================ requests

def create_request(user: AuthUser, body: dict) -> dict:
    """
    POST /leave/requests: validate against the policy and the balance, file the request and start its approval.
    {'id', 'days', 'explanation', 'sandwichApplied', 'bridge', 'teamOverlap'}. A shortfall raises LeaveSuggestionError.
    """
    b = plain(validate(RequestIn, body))
    if b.get('override') and not has_module(user, 'attendance'):
        raise ForbiddenError('only HR can override the worked-day check')
    if b['to'] < b['from']:
        raise BadRequestError('to must be on or after from')
    emp, t = _emp_and_type(user, b.get('employeeId'), b['leaveTypeId'])
    employee_id = emp['id']
    assert_manages(user, employee_id, 'attendance')
    if emp['status'] != Employee.Status.ACTIVE:
        raise BadRequestError('employee is not active')
    if emp['join_date'] and b['from'] < emp['join_date']:
        raise BadRequestError(f"leave cannot start before the joining date {emp['join_date']}")
    if emp['exit_date'] and b['to'] > emp['exit_date']:
        raise BadRequestError(f"leave cannot end after the exit date {emp['exit_date']}")
    if has_locked_day(employee_id, b['from'], b['to']):
        raise ConflictError('part of this range is locked by payroll; ask HR for an adjustment')
    if t['gender_scope'] != LeaveType.GenderScope.ALL and t['gender_scope'] != emp['gender']:
        raise BadRequestError(f"{t['name_en']} is not available for this employee")
    policy = leave_policy(emp['company_id'])
    if emp['join_date']:
        service_days = (date.fromisoformat(b['from']) - date.fromisoformat(emp['join_date'])).days
        if service_days < t['min_service_days']:
            raise BadRequestError(f"{t['name_en']} needs {t['min_service_days']} days of service")
    if (policy['probationRestriction'] and emp['probation_end'] and b['from'] <= emp['probation_end']
            and not t['statutory'] and t['paid']):
        raise BadRequestError(f"{t['name_en']} cannot be taken during probation (ends {emp['probation_end']})")
    hourly = _hourly_window(b)
    calc = (compute_hourly_leave(emp, t, b['from'], hourly['fromTime'], hourly['toTime']) if hourly
            else compute_leave_days(emp, t, b['from'], b['to'], b.get('halfDay'), policy['sandwichRule']))
    days = calc['days']
    bridge = bridge_across(emp, t, b['from'], b['to'], policy['sandwichRule']) if not hourly and not b.get('halfDay') else None
    own_bridge = bridge['days'] if bridge and bridge['typeId'] == t['id'] else 0
    if bridge and bridge['typeId'] != t['id']:
        avail = ledger_balance(employee_id, bridge['typeId']) - pending_days(employee_id, bridge['typeId'])
        if bridge['days'] > avail:
            raise BadRequestError(f"{bridge['explanation']}, but only {fixed(max(0.0, avail), 1)} day(s) of {bridge['typeName']} "
                                  'are left. Change the dates or ask HR.')
    # The company's maxContinuousDays limits discretionary leave only; a statutory entitlement cannot be capped by policy.
    max_run = t['max_consecutive_days'] if t['max_consecutive_days'] is not None else (None if t['statutory'] else policy['maxContinuousDays'])
    if max_run is not None and days > max_run:
        raise BadRequestError(f"{t['name_en']} cannot exceed {js(max_run)} consecutive days")
    if t['event_days'] is not None:
        per_event = num(t['event_days'])
        used = event_days_used(employee_id, t['id'], b['from'], b['to'])
        if used + days > per_event:
            left = max(0.0, per_event - used)
            unpaid = leave_type_by_code(emp['company_id'], 'SPECIAL_UNPAID')
            rest = f" Take the remaining days (up to 98 in total) as {unpaid['name_en']}." if t['code'] == 'MATERNITY' and unpaid else ''
            already = f' and {js(used)} are already requested for this event' if used else ''
            suggestion = {'paidDays': out(left), 'remainingDays': out(days - left)}
            if rest:
                suggestion['remainderLeaveTypeId'] = unpaid['id']
            raise LeaveSuggestionError(
                f"{t['name_en']} allows {js(per_event)} day(s) per event{already}; this request needs {js(days)}.{rest}", suggestion)
    # Full-day leave cannot replace a past day already recorded as worked; HR can override for a mis-attributed punch.
    if not b.get('halfDay') and not hourly and not b.get('override'):
        worked = list(latest_days().filter(employee_id=employee_id, local_date__in=[day(d) for d in calc['workingDays']],
                                           local_date__lt=_npt_today(),
                                           status__in=(AttendanceDay.Status.PRESENT, AttendanceDay.Status.WFH))
                      .order_by('local_date').values_list('local_date', flat=True))
        if worked:
            who = 'You were' if employee_id == user.employee_id else 'The employee was'
            raise ConflictError(f'{who} recorded as working on {bs_range(iso(worked[0]), iso(worked[-1]))} '
                                f'({len(worked)} day(s)). Apply for a half day, or ask HR to correct the attendance first.')
    if t['certificate_after_days'] is not None and days > t['certificate_after_days'] and not b.get('attachmentKey'):
        raise BadRequestError(f"a medical certificate is required for more than {t['certificate_after_days']} consecutive days "
                              f"of {t['name_en']} (Sec 44(2))")
    if t['paid'] and t['event_days'] is None:
        allocate_annual_leave(emp['company_id'], npt_today(), employee_id)
        available = ledger_balance(employee_id, t['id']) - pending_days(employee_id, t['id'])
        if days + own_bridge > available + num(t['negative_balance_limit']):
            paid_days = max(0.0, math.floor(available * 2) / 2)
            raise LeaveSuggestionError(
                f"You have {fixed(available, 1)} days of {t['name_en']} left but this request needs {js(days)}. Change the dates, "
                f'or apply {fixed(days - paid_days, 1)} day(s) as unpaid leave.',
                {'paidDays': out(paid_days), 'lwpDays': out(days - paid_days)})
    overlap = list(LeaveRequest.objects.for_company(emp['company_id'])
                   .filter(status__in=HELD, from_date__lte=day(b['to']), to_date__gte=day(b['from']))
                   .exclude(employee_id=employee_id).values_list('employee__name_en', flat=True)[:10])
    with transaction.atomic():
        # Serialise per employee (a lock on the employee row) so two concurrent requests for the same dates cannot
        # both pass the overlap check.
        list(Employee.objects.select_for_update(no_key=True).filter(pk=employee_id).values_list('pk', flat=True))
        assert_no_overlap(employee_id, b['from'], b['to'])
        hours = calc.get('hours', 0) if hourly else None
        lr = LeaveRequest.objects.create(
            employee_id=employee_id, leave_type_id=t['id'], from_date=day(b['from']), to_date=day(b['to']),
            half_day=b.get('halfDay'), days_requested=dec(days), reason=b.get('reason'), attachment_key=b.get('attachmentKey'),
            created_by_id=user.id, from_time=hourly['fromTime'] if hourly else None, to_time=hourly['toTime'] if hourly else None,
            hours=dec(hours) if hours is not None else None, bridge_days=dec(bridge['days'] if bridge else 0),
            bridge_type_id=bridge['typeId'] if bridge else None, bridge_from=day(bridge['from']) if bridge else None,
            bridge_to=day(bridge['to']) if bridge else None)
        request_id = str(lr.pk)
        when = f"{bs_range(b['from'], b['to'])} {hourly['fromTime']}–{hourly['toTime']}" if hourly else bs_range(b['from'], b['to'])
        hours_part = f'{js(hours)} hour(s), ' if hourly else ''
        reason_part = f": {b['reason']}" if b.get('reason') else ''
        inst = start_workflow(company_id=emp['company_id'], object_type='leave_request', object_id=request_id, employee_id=employee_id,
                              title='Leave request to approve',
                              body=f"{t['name_en']} {when} ({hours_part}{js(days)} day(s)){reason_part}")
        LeaveRequest.objects.filter(pk=lr.pk).update(workflow_instance_id=inst['id'])
        audit('create', 'leave_request', actor_id=user.id, row_id=request_id, after={**b, 'days': days})
    return {'id': request_id, 'days': days, 'explanation': calc['explanation'], 'sandwichApplied': calc['sandwichApplied'],
            'bridge': bridge, 'teamOverlap': overlap}


_LIST_FIELDS = ('id', 'employee_id', 'leave_type_id', 'from_date', 'to_date', 'half_day', 'days_requested', 'reason',
                'attachment_key', 'status', 'workflow_instance_id', 'created_at', 'decided_at', 'from_time', 'to_time', 'hours',
                'bridge_days', 'bridge_type_id')
_LIST_JOINED = {'emp_code': F('employee__emp_code'), 'name_en': F('employee__name_en'), 'name_ne': F('employee__name_ne'),
                'type_code': F('leave_type__code'), 'type_name': F('leave_type__name_en'), 'paid': F('leave_type__paid')}
_LIST_ORDER = ('id', 'employee_id', 'emp_code', 'name_en', 'name_ne', 'leave_type_id', 'type_code', 'type_name', 'paid',
               'from_date', 'to_date', 'half_day', 'days_requested', 'reason', 'attachment_key', 'status',
               'workflow_instance_id', 'created_at', 'decided_at', 'from_time', 'to_time', 'hours', 'bridge_days',
               'bridge_type_id')


def _request_row(r: dict) -> dict:
    r['from_time'], r['to_time'] = hhmm(r['from_time']), hhmm(r['to_time'])
    return {k: r[k] for k in _LIST_ORDER}


def requests_queryset(user: AuthUser, query: dict | None = None):
    """The leave requests the user may list (own, team or all), filtered by the query, newest first."""
    q = plain(validate(ListQueryIn, query or {}))
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    if q.get('employeeId'):
        if not Employee.objects.for_company(user.company_id).filter(pk=q['employeeId']).exists():
            raise NotFoundError('employee not found')
        assert_manages(user, q['employeeId'], 'attendance')
    qs = LeaveRequest.objects.for_company(user.company_id)
    if scope == 'own':
        qs = qs.filter(employee_id=user.employee_id)
    elif scope == 'team':
        qs = qs.filter(Q(manages(user.employee_id, F('employee_id'))) | Q(employee_id=user.employee_id))
    filters = {'employee': q.get('employeeId'), 'status': q.get('status'), 'ends_from': q.get('from'), 'starts_to': q.get('to')}
    return LeaveRequestFilter({k: v for k, v in filters.items() if v is not None}, queryset=qs).qs.order_by('-created_at', '-id')


def request_values(qs):
    """The list columns of leave requests (with employee and type names) as `.values()` rows."""
    return qs.values(*_LIST_FIELDS, **_LIST_JOINED)


def list_requests(user: AuthUser, query: dict | None = None) -> list[dict]:
    """GET /leave/requests: own, team (the people the user manages, and themselves) or all (attendance area)."""
    return [_request_row(r) for r in request_values(requests_queryset(user, query))[:500]]


def get_request(user: AuthUser, request_id: str) -> dict:
    """One request the user may see (same scoping as the list): the detail modal."""
    r = (LeaveRequest.objects.for_company(user.company_id).filter(pk=as_uuid(request_id))
         .values(*(f.attname for f in LeaveRequest._meta.concrete_fields), emp_code=F('employee__emp_code'),
                 name_en=F('employee__name_en'), company_id=F('employee__company_id'), type_code=F('leave_type__code'),
                 type_name=F('leave_type__name_en'))
         .first())
    if not r:
        raise NotFoundError('leave request not found')
    assert_manages(user, str(r['employee_id']), 'attendance')
    r['from_time_hm'], r['to_time_hm'] = hhmm(r['from_time']), hhmm(r['to_time'])
    return r


def cancel_request(user: AuthUser, request_id: str, body: dict | None = None) -> None:
    """POST /leave/requests/:id/cancel: an approved request gives its days (and any bridged days) back."""
    b = plain(validate(CancelIn, body or {}))
    reason = b.get('reason')
    with transaction.atomic():
        lr = (LeaveRequest.objects.for_company(user.company_id).select_for_update(of=('self',))
              .filter(pk=as_uuid(request_id)).select_related('employee').first())
        if not lr:
            raise NotFoundError('leave request not found')
        emp_id = str(lr.employee_id)
        assert_manages(user, emp_id)
        if lr.status in (Status.CANCELLED, Status.REJECTED):
            raise ConflictError(f'request is already {lr.status}')
        frm, to = iso(lr.from_date), iso(lr.to_date)
        if lr.status == Status.APPROVED:
            if has_locked_day(emp_id, frm, to):
                raise ConflictError('leave falls in a payroll-locked period; ask HR for an adjustment')
            give_back = [_ledger_row(employee_id=emp_id, leave_type_id=lr.leave_type_id, effective_date=lr.from_date,
                                     delta_days=num(lr.days_requested), reason=Reason.CANCELLATION, ref_type='leave_request',
                                     ref_id=lr.pk, posted_by_id=user.id, note=reason)]
            # Give back the bridged weekly-off/holiday days, if any were charged.
            give_back += [LeaveLedger(employee_id=x.employee_id, leave_type_id=x.leave_type_id, effective_date=x.effective_date,
                                      delta_days=-x.delta_days, reason=Reason.CANCELLATION, ref_type='leave_bridge',
                                      ref_id=x.ref_id, posted_by_id=user.id, note=reason)
                          for x in LeaveLedger.objects.filter(ref_type='leave_bridge', ref_id=lr.pk, reason=Reason.USAGE)]
            LeaveLedger.objects.bulk_create(give_back)
            enqueue_recompute([(emp_id, d) for d in date_range(frm, to)], 'leave_cancelled')
        LeaveRequest.objects.filter(pk=lr.pk).update(status=Status.CANCELLED, decided_at=Now())
        cancel_workflow('leave_request', str(lr.pk))
        before = {'employee_id': emp_id, 'leave_type_id': str(lr.leave_type_id), 'from_date': frm, 'to_date': to,
                  'days_requested': str(lr.days_requested), 'status': lr.status, 'company_id': str(lr.employee.company_id)}
        audit('cancel', 'leave_request', actor_id=user.id, row_id=str(lr.pk), before=before, after=b)


# ================================================================ accrual, adjustments, ledger

def run_accrual(user: AuthUser) -> dict:
    """POST /leave/accrual/run (attendance area): missed months and missing annual allocations, idempotently."""
    _require_module(user, 'attendance')
    company = _company_of(user)
    with transaction.atomic():
        summary = run_leave_catch_up(company, npt_today())
        postings = sum(m['postings'] for m in summary['months']) + summary['annualPostings']
        audit('accrual_catch_up', 'leave_ledger', actor_id=user.id, row_id=company,
              after={'postings': postings, 'months': len(summary['months'])})
    return {'postings': postings, 'months': [{'bsYear': m['bsYear'], 'bsMonth': m['bsMonth'], 'postings': m['postings']}
                                             for m in summary['months']],
            'annualPostings': summary['annualPostings']}


def adjust(user: AuthUser, body: dict) -> dict:
    """POST /leave/adjust (attendance area): an audited ledger adjustment or allocation. {'id', 'balance', 'replacedAutomatic'}."""
    _require_module(user, 'attendance')
    b = plain(validate(AdjustIn, body))
    emp = get_employee_lite(b['employeeId'], user.company_id)
    t = get_leave_type(b['leaveTypeId'], user.company_id)
    if not emp or not t:
        raise NotFoundError('employee or leave type not found')
    effective = b.get('effectiveDate') or npt_today()
    replaced = None
    with transaction.atomic():
        # An HR allocation for a fiscal year replaces the automatic annual grant for that year instead of stacking on it.
        if b['kind'] == Reason.ALLOCATION and t['accrual_basis'] == LeaveType.AccrualBasis.ANNUAL:
            fy = fiscal_year_of(ad_to_bs(effective))
            net = num(LeaveLedger.objects
                      .filter(employee_id=b['employeeId'], leave_type_id=b['leaveTypeId'], effective_date__gte=day(fy.start_ad),
                              effective_date__lt=day(fiscal_year(fy.start_year + 1).start_ad))
                      .filter(Q(reason=Reason.ALLOCATION, ref_type='accrual') | Q(ref_type='accrual_replaced'))
                      .aggregate(total=Sum('delta_days'))['total'])
            if net > 0:
                _ledger_row(employee_id=b['employeeId'], leave_type_id=b['leaveTypeId'], effective_date=day(effective),
                            delta_days=-net, reason=Reason.ADJUSTMENT, ref_type='accrual_replaced',
                            note=f'Automatic FY{fy.start_year} grant replaced by HR allocation', posted_by_id=user.id).save()
                replaced = fixed(net, 2)
        row = _ledger_row(employee_id=b['employeeId'], leave_type_id=b['leaveTypeId'], effective_date=day(effective),
                          delta_days=b['deltaDays'], reason=b['kind'], ref_type='hr_adjustment', note=b['reason'],
                          posted_by_id=user.id)
        row.save()
        new_id = str(row.pk)
        audit('adjust', 'leave_ledger', actor_id=user.id, row_id=new_id, after={**b, 'replacedAutomatic': replaced})
    return {'id': new_id, 'balance': out(ledger_balance(b['employeeId'], b['leaveTypeId'])), 'replacedAutomatic': replaced}


def ledger_queryset(user: AuthUser, employee_id: str | None = None, leave_type_id: str | None = None):
    """Every credit and debit for an employee of the active company (default: the user), newest first, as rows."""
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    if not Employee.objects.for_company(user.company_id).filter(pk=as_uuid(eid)).exists():
        raise NotFoundError('employee not found')
    assert_manages(user, str(eid), 'attendance')
    qs = LeaveLedger.objects.for_company(user.company_id).filter(employee_id=eid)
    if leave_type_id:
        qs = qs.filter(leave_type_id=leave_type_id)
    return (qs.order_by('-effective_date', '-posted_at')
            .values('id', 'leave_type_id', 'posted_at', 'effective_date', 'delta_days', 'reason', 'ref_type', 'ref_id',
                    'expires_on', 'note', code=F('leave_type__code')))


def ledger(user: AuthUser, employee_id: str | None = None, leave_type_id: str | None = None) -> list[dict]:
    """GET /leave/ledger: every credit and debit for an employee, newest first (at most 500)."""
    return list(ledger_queryset(user, employee_id, leave_type_id)[:500])


# ================================================================ team calendar

def team_calendar(user: AuthUser, from_: str, to: str) -> list[dict]:
    """
    GET /leave/team-calendar: who is off when (plan §5.6). Employees see their own team (people they manage and people
    with the same line manager); the attendance area sees everyone. [{'date', 'off': [...]}] sorted by date.
    """
    q = plain(validate(TeamCalendarQueryIn, {k: v for k, v in (('from', from_), ('to', to)) if v is not None}))
    today = _npt_today()
    qs = LeaveRequest.objects.for_company(user.company_id).filter(status__in=HELD, from_date__lte=day(q['to']),
                                                                  to_date__gte=day(q['from']))
    if not has_module(user, 'attendance'):
        my_manager = EmployeeAssignment.objects.filter(employee_id=user.employee_id, valid__contains=today).values('manager_id')[:1]
        same_manager = EmployeeAssignment.objects.filter(employee_id=OuterRef('employee_id'), valid__contains=today,
                                                         manager_id=Subquery(my_manager))
        qs = qs.filter(Q(manages(user.employee_id, F('employee_id'), today)) | Q(employee_id=user.employee_id)
                       | Exists(same_manager))
    found = qs.order_by('from_date').values('employee_id', 'from_date', 'to_date', 'half_day', 'status', 'from_time', 'to_time',
                                            emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                                            type_code=F('leave_type__code'))
    days: dict[str, list[dict]] = {}
    for lr in found:
        lr_from, lr_to = iso(lr['from_date']), iso(lr['to_date'])
        start = q['from'] if lr_from < q['from'] else lr_from
        end = q['to'] if lr_to > q['to'] else lr_to
        for d in date_range(start, end):
            item = {'employeeId': str(lr['employee_id']), 'empCode': lr['emp_code'], 'nameEn': lr['name_en'], 'type': lr['type_code'],
                    'status': lr['status'], 'halfDay': lr['half_day']}
            if lr['from_time']:
                item.update({'fromTime': hhmm(lr['from_time']), 'toTime': hhmm(lr['to_time'])})
            days.setdefault(d, []).append(item)
    return [{'date': d, 'off': days[d]} for d in sorted(days)]
