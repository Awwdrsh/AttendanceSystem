"""
Leave service layer (port of apps/api/src/modules/leave/routes.ts): leave types and the leave order, balances, the
deduction preview, requests (with the approval workflow), cancellation, HR adjustments, the ledger, the accrual
catch-up and the team calendar. Bodies and results use the TS API's camelCase shapes, so the JSON endpoints, the pages
and other modules share one implementation. The rules live in service.py and accrual.py.

Other modules use:
    credit_comp_off(employee_id, days, expires_on, ref_type=, ref_id=, note=, posted_by=) -> ledger id   (OT comp-off)
    encashable_balances(employee_id) -> [{'leaveTypeId', 'code', 'days'}]                              (final settlement)
    post_encashment(employee_id, leave_type_id, days, ref_type=, ref_id=, posted_by=)
    ledger_balance(employee_id, leave_type_id, as_of=None), pending_days(employee_id, leave_type_id)   (late penalties)
    run_monthly_accrual(company_id, bs_year, bs_month), run_leave_catch_up(company_id, as_of)          (worker hooks)
"""
from __future__ import annotations

import math
from datetime import date

from django.db import transaction

from engines.bs_calendar import ad_to_bs, fiscal_year, fiscal_year_of

from hajiri.attendance.service import date_range, enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_module, has_role
from hajiri.core.bsdate import bs_range
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import MISSING, Arr, Bool, Enum, Hhmm, Int, IsoDate, Num, Obj, Str, Uuid, parse
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import DecisionContext, assert_manages, cancel_workflow, npt_today, register_handler, start_workflow

from .accrual import allocate_annual_leave, expire_substitute_leave, run_leave_catch_up, run_monthly_accrual  # noqa: F401
from .service import (  # noqa: F401
    EVENT_GAP_DAYS, HOURLY_MIN_MINUTES, assert_no_overlap, balances, bridge_across, compute_hourly_leave,
    compute_leave_days, credit_comp_off, encashable_balances, event_days_used, fixed, get_employee_lite, get_leave_type,
    group_head, has_locked_day, iso, js, leave_type_by_code, ledger_balance, num, out, pending_days, post_encashment,
    working_days_between,
)


class LeaveSuggestionError(BadRequestError):
    """A 400 that also carries a suggestion (the TS route answered {error, suggestion})."""

    def __init__(self, message: str, suggestion: dict):
        super().__init__(message)
        self.suggestion = suggestion


def _require_module(user: AuthUser, area: str) -> None:
    from hajiri.core.auth import MODULE_ROLES
    if not has_module(user, area):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES[area])}")


def _company_of(user: AuthUser) -> str:
    if not user.company_id:
        raise ForbiddenError('user is not attached to a company')
    return user.company_id


def leave_policy(company_id: str) -> dict:
    s = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'leave.policy'", [str(company_id)])
    v = (s or {}).get('value') or {}
    mcd = v.get('maxContinuousDays')
    return {'sandwichRule': v.get('sandwichRule') is True, 'probationRestriction': v.get('probationRestriction') is not False,
            'maxContinuousDays': mcd if isinstance(mcd, (int, float)) and not isinstance(mcd, bool) else 30}


# ================================================================ workflow decision

def apply_leave_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """Approval/rejection effect for leave requests (registered with the workflow engine)."""
    lr = one("""SELECT lr.*, lt.company_id, lt.name_en AS type_name FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id
                 WHERE lr.id = %s FOR UPDATE""", [str(request_id)])
    if not lr:
        raise NotFoundError('leave request not found')
    if lr['status'] != 'pending':
        raise ConflictError(f"leave request already {lr['status']}")
    emp_id, frm, to = str(lr['employee_id']), iso(lr['from_date']), iso(lr['to_date'])
    if state == 'approved' and has_locked_day(emp_id, frm, to):
        raise ConflictError('the leave falls in a payroll-locked period; reject it and let HR raise an adjustment')
    execute('UPDATE leave_request SET status = %s, decided_at = now() WHERE id = %s', [state, str(request_id)])
    if state == 'approved':
        execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by)
                   VALUES (%s, %s, %s, %s, 'usage', 'leave_request', %s, %s)""",
                [emp_id, lr['leave_type_id'], frm, -num(lr['days_requested']), str(request_id), ctx.actor_id])
        _post_bridge(str(request_id), emp_id, ctx.actor_id)
        enqueue_recompute([(emp_id, d) for d in date_range(frm, to)], 'leave_approved')
    user_id = user_id_for_employee(emp_id)
    notify(company_id=str(lr['company_id']), user_id=user_id, employee_id=emp_id, template=f'leave.{state}',
           title='Leave approved' if state == 'approved' else 'Leave rejected',
           body=f"{lr['type_name']} {bs_range(frm, to)} was {state}{f': {ctx.reason}' if ctx.reason else ''}.",
           payload={'requestId': str(request_id)}, channels=('push', 'sms'))


def _post_bridge(request_id: str, employee_id: str, actor_id: str) -> None:
    """On approval, charge the bridged non-working days to the type that pays for them, if the leave on the other side is
    still pending or approved; otherwise the bridge is dropped."""
    x = one('SELECT bridge_days, bridge_type_id, bridge_from::text, bridge_to::text FROM leave_request WHERE id = %s', [request_id])
    if not x or not x['bridge_type_id'] or num(x['bridge_days']) <= 0 or not x['bridge_from'] or not x['bridge_to']:
        return
    other = one("""SELECT 1 AS x FROM leave_request WHERE employee_id = %s AND id <> %s AND status IN ('pending', 'approved')
                     AND (to_date = %s::date - 1 OR from_date = %s::date + 1) LIMIT 1""",
                [employee_id, request_id, x['bridge_from'], x['bridge_to']])
    if not other:
        execute('UPDATE leave_request SET bridge_days = 0, bridge_type_id = NULL, bridge_from = NULL, bridge_to = NULL WHERE id = %s',
                [request_id])
        return
    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note)
               VALUES (%s, %s, %s, %s, 'usage', 'leave_bridge', %s, %s, %s)""",
            [employee_id, x['bridge_type_id'], x['bridge_from'], -num(x['bridge_days']), request_id, actor_id,
             'Weekly-off/holiday days between two leaves (sandwich rule)'])


register_handler('leave_request', apply_leave_decision)


# ================================================================ leave types

def _type_fields(partial: bool) -> dict:
    """typeBody; partial: every field optional and no defaults (PATCH keeps only the keys that were sent)."""
    def f(schema_cls, default=MISSING, **kw):
        if partial:
            return schema_cls(optional=True, **kw)
        return schema_cls(default=default, **kw) if default is not MISSING else schema_cls(**kw)
    return {
        'code': f(Str, min=1, max=20), 'nameEn': f(Str, min=1, max=80), 'nameNe': Str(max=80, optional=True),
        'paid': f(Bool, True),
        'accrualBasis': (Enum(('none', 'worked_days', 'monthly', 'annual'), optional=True) if partial
                         else Enum(('none', 'worked_days', 'monthly', 'annual'), default='none')),
        'accrualDays': f(Num, 0, min=0),
        'accrualPerWorkedDays': Int(min=1, nullish=True), 'maxBalance': Num(min=0, nullish=True),
        'carryForward': f(Bool, True), 'encashable': f(Bool, False),
        'certificateAfterDays': Int(min=0, nullish=True), 'allowHalfDay': f(Bool, True),
        'maxConsecutiveDays': Int(min=1, nullish=True), 'minServiceDays': (Int(min=0, optional=True) if partial else Int(min=0, default=0)),
        'negativeBalanceLimit': f(Num, 0, min=0),
        'genderScope': (Enum(('all', 'female', 'male'), optional=True) if partial else Enum(('all', 'female', 'male'), default='all')),
        'sandwichRule': f(Bool, False), 'expiryDays': Int(min=1, nullish=True),
        'eventDays': Num(min=0.5, max=365, nullish=True), 'allowHourly': f(Bool, False),
        'clubWithId': Uuid(nullish=True), 'sortOrder': Int(min=0, max=9999, optional=True),
    }


TYPE_BODY = Obj(_type_fields(False))
TYPE_PATCH = Obj(_type_fields(True))

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


def list_types(user: AuthUser) -> list[dict]:
    """GET /leave/types: the company's leave types in leave order."""
    return [_type_row(r) for r in rows('SELECT * FROM leave_type WHERE company_id = %s ORDER BY sort_order, code', [user.company_id])]


def _type_row(r: dict) -> dict:
    t = dict(r)
    for k in ('id', 'company_id', 'club_with_id'):
        if t.get(k) is not None:
            t[k] = str(t[k])
    return t


def _assert_club_target(company_id: str, type_id: str, target_id: str) -> None:
    """A type can be clubbed with another type of the same company that is not itself clubbed (no chains or loops)."""
    if str(type_id) == str(target_id):
        raise BadRequestError('a leave type cannot be clubbed with itself')
    t = one('SELECT club_with_id FROM leave_type WHERE id = %s AND company_id = %s', [str(target_id), str(company_id)])
    if not t:
        raise BadRequestError('the leave type to club with does not exist')
    if t['club_with_id']:
        raise BadRequestError('that leave type is itself clubbed with another; club with the main type instead')
    if one('SELECT 1 AS x FROM leave_type WHERE club_with_id = %s LIMIT 1', [str(type_id)]):
        raise BadRequestError('other leave types are clubbed with this one, so it cannot follow another type')


def create_type(user: AuthUser, body: dict) -> str:
    """POST /leave/types (attendance area). Returns the new id."""
    _require_module(user, 'attendance')
    b = parse(TYPE_BODY, body)
    company = _company_of(user)
    with transaction.atomic():
        ins = one("""INSERT INTO leave_type (company_id, code, name_en, name_ne, paid, statutory, accrual_basis, accrual_days,
                       accrual_per_worked_days, max_balance, carry_forward, encashable, certificate_after_days, allow_half_day,
                       max_consecutive_days, min_service_days, negative_balance_limit, gender_scope, sandwich_rule, expiry_days,
                       event_days, sort_order, allow_hourly)
                     VALUES (%s, %s, %s, %s, %s, false, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 100, %s) RETURNING id""",
                  [company, b['code'], b['nameEn'], b.get('nameNe'), b['paid'], b['accrualBasis'], b['accrualDays'],
                   b.get('accrualPerWorkedDays'), b.get('maxBalance'), b['carryForward'], b['encashable'],
                   b.get('certificateAfterDays'), b['allowHalfDay'], b.get('maxConsecutiveDays'), b['minServiceDays'],
                   b['negativeBalanceLimit'], b['genderScope'], b['sandwichRule'], b.get('expiryDays'), b.get('eventDays'),
                   bool(b['allowHourly'] and b['paid'] and b.get('eventDays') is None)])
        new_id = str(ins['id'])
        if b.get('clubWithId'):
            _assert_club_target(company, new_id, b['clubWithId'])
            execute('UPDATE leave_type SET club_with_id = %s WHERE id = %s', [b['clubWithId'], new_id])
        if 'sortOrder' in b:
            execute('UPDATE leave_type SET sort_order = %s WHERE id = %s', [b['sortOrder'], new_id])
        audit('create', 'leave_type', actor_id=user.id, row_id=new_id, after=b)
    return new_id


def update_type(user: AuthUser, type_id: str, body: dict) -> None:
    """PATCH /leave/types/:id (attendance area). Only the keys sent are changed."""
    _require_module(user, 'attendance')
    b = parse(TYPE_PATCH, body if isinstance(body, dict) else {})
    cur = one('SELECT * FROM leave_type WHERE id = %s AND company_id = %s', [str(type_id), user.company_id])
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
    sets, vals = [], []
    for k, col in _TYPE_COLUMNS:
        if k in b:
            sets.append(f'{col} = %s')
            vals.append(b[k])
    with transaction.atomic():
        if sets:
            execute(f"UPDATE leave_type SET {', '.join(sets)} WHERE id = %s", [*vals, str(type_id)])
        audit('update', 'leave_type', actor_id=user.id, row_id=str(type_id), before=_type_row(cur), after=b)


def reorder_types(user: AuthUser, ids: list[str]) -> None:
    """POST /leave/types/order (HR): the listed types get sort orders 10, 20, 30..."""
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    b = parse(Obj({'ids': Arr(Uuid(), min=1, max=100)}), {'ids': ids})
    company = _company_of(user)
    own = rows('SELECT id FROM leave_type WHERE company_id = %s AND id = ANY(%s::uuid[])', [company, b['ids']])
    if len(own) != len(set(b['ids'])):
        raise BadRequestError('a leave type in the list does not exist')
    with transaction.atomic():
        for i, tid in enumerate(b['ids']):
            execute('UPDATE leave_type SET sort_order = %s WHERE id = %s', [(i + 1) * 10, tid])
        audit('reorder', 'leave_type', actor_id=user.id, after={'ids': b['ids']})


# ================================================================ balances and preview

def _employee_in_company(user: AuthUser, employee_id: str | None) -> dict:
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    emp = get_employee_lite(eid)
    if not emp or emp['company_id'] != user.company_id:
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


PREVIEW_QUERY = Obj({'employeeId': Uuid(optional=True), 'leaveTypeId': Uuid(), 'from': IsoDate(), 'to': IsoDate(),
                     'halfDay': Enum(('first_half', 'second_half'), optional=True), 'fromTime': Hhmm(optional=True),
                     'toTime': Hhmm(optional=True)})


def _emp_and_type(user: AuthUser, employee_id: str | None, leave_type_id: str) -> tuple[dict, dict]:
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    emp = get_employee_lite(eid)
    t = get_leave_type(leave_type_id)
    if not emp or not t or emp['company_id'] != t['company_id'] or emp['company_id'] != user.company_id:
        raise NotFoundError('employee or leave type not found')
    return emp, t


def preview(user: AuthUser, query: dict) -> dict:
    """GET /leave/preview: the deduction and the balance after it, before submitting (plan §13.5 computed line)."""
    q = parse(PREVIEW_QUERY, query)
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

REQUEST_BODY = Obj({
    'employeeId': Uuid(optional=True), 'leaveTypeId': Uuid(), 'from': IsoDate(), 'to': IsoDate(),
    'halfDay': Enum(('first_half', 'second_half'), optional=True),
    'fromTime': Hhmm(optional=True), 'toTime': Hhmm(optional=True),
    'reason': Str(max=1000, optional=True), 'attachmentKey': Str(max=200, optional=True),
    'override': Bool(optional=True),
})


def create_request(user: AuthUser, body: dict) -> dict:
    """
    POST /leave/requests: validate against the policy and the balance, file the request and start its approval.
    {'id', 'days', 'explanation', 'sandwichApplied', 'bridge', 'teamOverlap'}. A shortfall raises LeaveSuggestionError.
    """
    b = parse(REQUEST_BODY, body)
    if b.get('override') and not has_module(user, 'attendance'):
        raise ForbiddenError('only HR can override the worked-day check')
    if b['to'] < b['from']:
        raise BadRequestError('to must be on or after from')
    emp, t = _emp_and_type(user, b.get('employeeId'), b['leaveTypeId'])
    employee_id = emp['id']
    assert_manages(user, employee_id, 'attendance')
    if emp['status'] != 'active':
        raise BadRequestError('employee is not active')
    if emp['join_date'] and b['from'] < emp['join_date']:
        raise BadRequestError(f"leave cannot start before the joining date {emp['join_date']}")
    if emp['exit_date'] and b['to'] > emp['exit_date']:
        raise BadRequestError(f"leave cannot end after the exit date {emp['exit_date']}")
    if has_locked_day(employee_id, b['from'], b['to']):
        raise ConflictError('part of this range is locked by payroll; ask HR for an adjustment')
    if t['gender_scope'] != 'all' and t['gender_scope'] != emp['gender']:
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
        worked = rows("""SELECT local_date::text FROM v_attendance_latest WHERE employee_id = %s AND local_date = ANY(%s::date[])
                           AND local_date < %s::date AND status IN ('Present', 'WFH') ORDER BY local_date""",
                      [employee_id, calc['workingDays'], npt_today()])
        if worked:
            who = 'You were' if employee_id == user.employee_id else 'The employee was'
            raise ConflictError(f"{who} recorded as working on {bs_range(worked[0]['local_date'], worked[-1]['local_date'])} "
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
    overlap = rows("""SELECT e.name_en FROM leave_request o JOIN employee e ON e.id = o.employee_id
                       WHERE o.status IN ('pending', 'approved') AND o.employee_id <> %s AND o.from_date <= %s::date
                         AND o.to_date >= %s::date AND e.company_id = %s LIMIT 10""",
                   [employee_id, b['to'], b['from'], emp['company_id']])
    with transaction.atomic():
        # Serialise per employee so two concurrent requests for the same dates cannot both pass the overlap check.
        one('SELECT pg_advisory_xact_lock(hashtext(%s)) AS x', [f'leave:{employee_id}'])
        assert_no_overlap(employee_id, b['from'], b['to'])
        hours = calc.get('hours', 0) if hourly else None
        ins = one("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, half_day, days_requested, reason,
                       attachment_key, created_by, from_time, to_time, hours, bridge_days, bridge_type_id, bridge_from, bridge_to)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [employee_id, t['id'], b['from'], b['to'], b.get('halfDay'), days, b.get('reason'), b.get('attachmentKey'),
                   user.id, hourly['fromTime'] if hourly else None, hourly['toTime'] if hourly else None, hours,
                   bridge['days'] if bridge else 0, bridge['typeId'] if bridge else None,
                   bridge['from'] if bridge else None, bridge['to'] if bridge else None])
        request_id = str(ins['id'])
        when = f"{bs_range(b['from'], b['to'])} {hourly['fromTime']}–{hourly['toTime']}" if hourly else bs_range(b['from'], b['to'])
        hours_part = f'{js(hours)} hour(s), ' if hourly else ''
        reason_part = f": {b['reason']}" if b.get('reason') else ''
        inst = start_workflow(company_id=emp['company_id'], object_type='leave_request', object_id=request_id, employee_id=employee_id,
                              title='Leave request to approve',
                              body=f"{t['name_en']} {when} ({hours_part}{js(days)} day(s)){reason_part}")
        execute('UPDATE leave_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], request_id])
        audit('create', 'leave_request', actor_id=user.id, row_id=request_id, after={**b, 'days': days})
    return {'id': request_id, 'days': days, 'explanation': calc['explanation'], 'sandwichApplied': calc['sandwichApplied'],
            'bridge': bridge, 'teamOverlap': [x['name_en'] for x in overlap]}


LIST_QUERY = Obj({'employeeId': Uuid(optional=True), 'status': Enum(('pending', 'approved', 'rejected', 'cancelled'), optional=True),
                  'from': IsoDate(optional=True), 'to': IsoDate(optional=True), 'scope': Enum(('own', 'team', 'all'), optional=True)})


def list_requests(user: AuthUser, query: dict | None = None) -> list[dict]:
    """GET /leave/requests: own, team (the people the user manages, and themselves) or all (attendance area)."""
    q = parse(LIST_QUERY, query or {})
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    return rows(
        """SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, e.name_ne, lr.leave_type_id, lt.code AS type_code, lt.name_en AS type_name, lt.paid,
                  lr.from_date, lr.to_date, lr.half_day, lr.days_requested, lr.reason, lr.attachment_key, lr.status, lr.workflow_instance_id,
                  lr.created_at, lr.decided_at, to_char(lr.from_time, 'HH24:MI') AS from_time, to_char(lr.to_time, 'HH24:MI') AS to_time,
                  lr.hours, lr.bridge_days, lr.bridge_type_id
             FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
            WHERE e.company_id = %s
              AND (%s::uuid IS NULL OR lr.employee_id = %s::uuid)
              AND (%s::text IS NULL OR lr.status = %s::text)
              AND (%s::date IS NULL OR lr.to_date >= %s::date) AND (%s::date IS NULL OR lr.from_date <= %s::date)
              AND CASE %s::text WHEN 'own' THEN lr.employee_id = %s::uuid
                                WHEN 'team' THEN (hj_manages(%s::uuid, lr.employee_id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR lr.employee_id = %s::uuid)
                                ELSE true END
            ORDER BY lr.created_at DESC LIMIT 500""",
        [user.company_id, q.get('employeeId'), q.get('employeeId'), q.get('status'), q.get('status'), q.get('from'), q.get('from'),
         q.get('to'), q.get('to'), scope, user.employee_id, user.employee_id, user.employee_id])


def get_request(user: AuthUser, request_id: str) -> dict:
    """One request the user may see (same scoping as the list): the detail modal."""
    r = one("""SELECT lr.*, e.emp_code, e.name_en, e.company_id, lt.code AS type_code, lt.name_en AS type_name,
                      to_char(lr.from_time, 'HH24:MI') AS from_time_hm, to_char(lr.to_time, 'HH24:MI') AS to_time_hm
                 FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
                WHERE lr.id = %s""", [str(request_id)])
    if not r or str(r['company_id']) != user.company_id:
        raise NotFoundError('leave request not found')
    assert_manages(user, str(r['employee_id']), 'attendance')
    return r


CANCEL_BODY = Obj({'reason': Str(max=500, optional=True)})


def cancel_request(user: AuthUser, request_id: str, body: dict | None = None) -> None:
    """POST /leave/requests/:id/cancel: an approved request gives its days (and any bridged days) back."""
    b = parse(CANCEL_BODY, body or {})
    reason = b.get('reason')
    with transaction.atomic():
        lr = one("""SELECT lr.employee_id, lr.leave_type_id, lr.from_date, lr.to_date, lr.days_requested, lr.status, e.company_id
                      FROM leave_request lr JOIN employee e ON e.id = lr.employee_id WHERE lr.id = %s FOR UPDATE OF lr""",
                 [str(request_id)])
        if not lr or str(lr['company_id']) != user.company_id:
            raise NotFoundError('leave request not found')
        emp_id = str(lr['employee_id'])
        assert_manages(user, emp_id)
        if lr['status'] in ('cancelled', 'rejected'):
            raise ConflictError(f"request is already {lr['status']}")
        frm, to = iso(lr['from_date']), iso(lr['to_date'])
        if lr['status'] == 'approved':
            if has_locked_day(emp_id, frm, to):
                raise ConflictError('leave falls in a payroll-locked period; ask HR for an adjustment')
            execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note)
                       VALUES (%s, %s, %s, %s, 'cancellation', 'leave_request', %s, %s, %s)""",
                    [emp_id, lr['leave_type_id'], frm, num(lr['days_requested']), str(request_id), user.id, reason])
            # Give back the bridged weekly-off/holiday days, if any were charged.
            execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note)
                       SELECT employee_id, leave_type_id, effective_date, -delta_days, 'cancellation', 'leave_bridge', ref_id, %s, %s
                         FROM leave_ledger WHERE ref_type = 'leave_bridge' AND ref_id = %s AND reason = 'usage'""",
                    [user.id, reason, str(request_id)])
            enqueue_recompute([(emp_id, d) for d in date_range(frm, to)], 'leave_cancelled')
        execute("UPDATE leave_request SET status = 'cancelled', decided_at = now() WHERE id = %s", [str(request_id)])
        cancel_workflow('leave_request', str(request_id))
        before = {'employee_id': emp_id, 'leave_type_id': str(lr['leave_type_id']), 'from_date': frm, 'to_date': to,
                  'days_requested': str(lr['days_requested']), 'status': lr['status'], 'company_id': str(lr['company_id'])}
        audit('cancel', 'leave_request', actor_id=user.id, row_id=str(request_id), before=before, after=b)


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


ADJUST_BODY = Obj({
    'employeeId': Uuid(), 'leaveTypeId': Uuid(),
    'deltaDays': Num(min=-365, max=365, refine=(lambda n: n != 0, 'deltaDays cannot be 0')),
    'reason': Str(min=3, max=500), 'effectiveDate': IsoDate(optional=True),
    'kind': Enum(('adjustment', 'allocation', 'carry_forward'), default='adjustment'),
})


def adjust(user: AuthUser, body: dict) -> dict:
    """POST /leave/adjust (attendance area): an audited ledger adjustment or allocation. {'id', 'balance', 'replacedAutomatic'}."""
    _require_module(user, 'attendance')
    b = parse(ADJUST_BODY, body)
    emp = get_employee_lite(b['employeeId'])
    t = get_leave_type(b['leaveTypeId'])
    if not emp or not t or emp['company_id'] != user.company_id or t['company_id'] != emp['company_id']:
        raise NotFoundError('employee or leave type not found')
    effective = b.get('effectiveDate') or npt_today()
    replaced = None
    with transaction.atomic():
        # An HR allocation for a fiscal year replaces the automatic annual grant for that year instead of stacking on it.
        if b['kind'] == 'allocation' and t['accrual_basis'] == 'annual':
            fy = fiscal_year_of(ad_to_bs(effective))
            auto = one("""SELECT coalesce(sum(delta_days), 0)::text AS total FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s
                            AND effective_date >= %s::date AND effective_date < %s::date
                            AND ((reason = 'allocation' AND ref_type = 'accrual') OR ref_type = 'accrual_replaced')""",
                       [b['employeeId'], b['leaveTypeId'], fy.start_ad, fiscal_year(fy.start_year + 1).start_ad])
            net = num(auto['total'])
            if net > 0:
                execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note, posted_by)
                           VALUES (%s, %s, %s, %s, 'adjustment', 'accrual_replaced', %s, %s)""",
                        [b['employeeId'], b['leaveTypeId'], effective, -net, f'Automatic FY{fy.start_year} grant replaced by HR allocation',
                         user.id])
                replaced = fixed(net, 2)
        ins = one("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note, posted_by)
                     VALUES (%s, %s, %s, %s, %s, 'hr_adjustment', %s, %s) RETURNING id""",
                  [b['employeeId'], b['leaveTypeId'], effective, b['deltaDays'], b['kind'], b['reason'], user.id])
        new_id = str(ins['id'])
        audit('adjust', 'leave_ledger', actor_id=user.id, row_id=new_id, after={**b, 'replacedAutomatic': replaced})
    return {'id': new_id, 'balance': out(ledger_balance(b['employeeId'], b['leaveTypeId'])), 'replacedAutomatic': replaced}


def ledger(user: AuthUser, employee_id: str | None = None, leave_type_id: str | None = None) -> list[dict]:
    """GET /leave/ledger: every credit and debit for an employee, newest first (at most 500)."""
    eid = employee_id or user.employee_id
    if not eid:
        raise BadRequestError('employeeId required')
    if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [str(eid), user.company_id]):
        raise NotFoundError('employee not found')
    assert_manages(user, str(eid), 'attendance')
    return rows("""SELECT l.id, l.leave_type_id, t.code, l.posted_at, l.effective_date, l.delta_days, l.reason, l.ref_type, l.ref_id,
                          l.expires_on, l.note
                     FROM leave_ledger l JOIN leave_type t ON t.id = l.leave_type_id
                    WHERE l.employee_id = %s AND (%s::uuid IS NULL OR l.leave_type_id = %s::uuid)
                    ORDER BY l.effective_date DESC, l.posted_at DESC LIMIT 500""", [str(eid), leave_type_id, leave_type_id])


# ================================================================ team calendar

def team_calendar(user: AuthUser, from_: str, to: str) -> list[dict]:
    """
    GET /leave/team-calendar: who is off when (plan §5.6). Employees see their own team (people they manage and people
    with the same line manager); the attendance area sees everyone. [{'date', 'off': [...]}] sorted by date.
    """
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate()}), {k: v for k, v in (('from', from_), ('to', to)) if v is not None})
    found = rows(
        """SELECT lr.employee_id, e.emp_code, e.name_en, lr.from_date::text, lr.to_date::text, lr.half_day, lt.code AS type_code, lr.status,
                  to_char(lr.from_time, 'HH24:MI') AS from_time, to_char(lr.to_time, 'HH24:MI') AS to_time
             FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
             LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
            WHERE e.company_id = %s AND lr.status IN ('pending', 'approved') AND lr.from_date <= %s::date AND lr.to_date >= %s::date
              AND (%s OR hj_manages(%s::uuid, lr.employee_id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR lr.employee_id = %s::uuid
                   OR a.manager_id = (SELECT manager_id FROM employee_assignment m WHERE m.employee_id = %s::uuid AND m.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date))
            ORDER BY lr.from_date""",
        [user.company_id, q['to'], q['from'], has_module(user, 'attendance'), user.employee_id, user.employee_id, user.employee_id])
    days: dict[str, list[dict]] = {}
    for lr in found:
        start = q['from'] if lr['from_date'] < q['from'] else lr['from_date']
        end = q['to'] if lr['to_date'] > q['to'] else lr['to_date']
        for d in date_range(start, end):
            item = {'employeeId': str(lr['employee_id']), 'empCode': lr['emp_code'], 'nameEn': lr['name_en'], 'type': lr['type_code'],
                    'status': lr['status'], 'halfDay': lr['half_day']}
            if lr['from_time']:
                item.update({'fromTime': lr['from_time'], 'toTime': lr['to_time']})
            days.setdefault(d, []).append(item)
    return [{'date': d, 'off': days[d]} for d in sorted(days)]
