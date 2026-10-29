"""
Leave and attendance extras (port of apps/api/src/modules/attendance-tools: policy.ts, block.ts, penalties.ts,
issues.ts, upload.ts, whosout.ts and routes.ts): blocking late clock-ins, the late-arrival penalty, attendance issues and
nudges, the manual CSV upload and who's out. Every query is scoped to the caller's ACTIVE company.

Public interface:
- extras_policy(company_id) -> dict (blockLateClockIn, blockAfterMin, whosOutScope, latePenalty{enabled, lateCount,
  minLateMinutes, penaltyDays, leaveTypeIds}); save_extras_policy(company_id, value, user_id); EXTRAS_KEY.
- assert_clock_in_allowed(*, company_id, employee_id, at_ms, direction) raises ConflictError 409 {'code': 'late_blocked'}.
- penalty_for(late_days, rule), allocate_penalty(days, available), month_locked(company_id, y, m),
  penalty_preview(company_id, y, m, rule, *, employee_id=None, manager_employee_id=None) -> [PenaltyLine dict],
  apply_penalties(...), reverse_penalty(...), penalty_unpaid_days(employee_id, y, m) -> float (payroll's loss of pay).
- attendance_issues(company_id, from, to, manager_employee_id) -> [IssueRow], summarise(rows).
- check_upload(company_id, csv, self_employee_id=None) -> {'rows', 'errors'}, import_upload(...), UPLOAD_TEMPLATE.
- scope_employee_ids(user, scope), whos_out(company_id, ids, from, to).
- paid_leave_types(company_id) -> [{id, code, name_en}] (the penalty rule's leave picker).
- One function per TS route taking (user, body_or_query) in the TS camelCase shape: get_policy, put_policy, penalties,
  my_penalties, apply, reverse, issues, issue_summary, nudge, upload_check, upload_import, uploads, whos_out_for.
"""
from __future__ import annotations

import csv as csv_module
import io
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.postgres.aggregates import ArrayAgg
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Now

from engines.attendance import MIN_MS, local_date_of, local_time_ms
from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds, bs_to_ad, parse_bs, weekday_of

from hajiri.attendance.repo import latest_days, resolve_shift
from hajiri.attendance.service import date_range, is_attendance_locked, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role, manages
from hajiri.core.bsdate import bs_month_name, bs_range
from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.collation import collate_key
from hajiri.core.crypto import sha256_hex
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.scoping import exists_in_company
from hajiri.core.settings_store import get_setting, put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    AppUser, AttendanceDay, AttendanceNudge, AttendanceUpload, Employee, EmployeeAssignment, FieldVisit, Holiday,
    LatePenalty, LeaveLedger, LeaveRequest, LeaveType, PayrollRun, PunchEvent, RegularisationRequest, TravelRequest,
    WorkflowInstance,
)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.punch.services import is_duplicate
from hajiri.workflow.service import approvers_for, assert_manages as wf_assert_manages, get_instance

from .serializers import (
    ApplyPenaltiesIn, AttendanceUploadIn, ExtrasPolicyIn, ImportIn, IssueRangeIn, MonthIn, MonthPartialIn, ReverseIn,
    WhosOutIn, nudge_body,
)

NIL_UUID = '00000000-0000-0000-0000-000000000000'
Day = AttendanceDay
Reg = RegularisationRequest


def npt_today() -> str:
    """Today's Nepal date as 'YYYY-MM-DD'."""
    return _npt_today().isoformat()


# ================================================================ policy.ts

EXTRAS_KEY = 'attendance.extras'


def default_extras() -> dict:
    return validate(ExtrasPolicyIn, {})


def extras_policy(company_id: str) -> dict:
    """The company's rules; a stored value that no longer validates falls back to the defaults."""
    try:
        return plain(validate(ExtrasPolicyIn, get_setting(company_id, EXTRAS_KEY) or {}))
    except ValidationError:
        return default_extras()


def save_extras_policy(company_id: str, value: dict, user_id: str) -> None:
    put_setting(company_id, EXTRAS_KEY, value, updated_by=user_id)


# ================================================================ block.ts

def _npt_local_time(ms: int) -> str:
    from hajiri.punch.services import npt_local_time
    return npt_local_time(ms)


def _plus_minutes(hhmm: str, minutes: int) -> time:
    """'HH:MM' + minutes as a time of day (wrapping at midnight, like time + interval in SQL)."""
    h, m = (int(x) for x in hhmm.split(':')[:2])
    return (datetime(2000, 1, 1, h, m) + timedelta(minutes=minutes)).time()


def assert_clock_in_allowed(*, company_id: str, employee_id: str, at_ms: int, direction: str) -> None:
    """
    Optional rule (off by default): the first clock-in of a working day later than shift start + grace (+ the extra
    minutes HR allows) is refused. Clock-outs, second clock-ins, holidays, weekly offs and days with approved first-half
    or hourly leave at the start are never blocked.
    """
    if direction != PunchEvent.Direction.IN:
        return
    policy = extras_policy(company_id)
    if not policy['blockLateClockIn']:
        return
    local_date = local_date_of(at_ms)
    day = date.fromisoformat(local_date)
    if PunchEvent.objects.filter(employee_id=employee_id, local_date=day, direction=PunchEvent.Direction.IN).exists():
        return
    e = (Employee.objects.filter(pk=employee_id)
         .annotate(branch_id=Subquery(EmployeeAssignment.objects.filter(employee_id=OuterRef('pk'), valid__contains=day)
                                      .values('branch_id')[:1]))
         .values('weekly_off_days', 'gender', 'branch_id').first())
    if not e:
        return
    if weekday_of(local_date) in (e['weekly_off_days'] or []):
        return
    for_everyone = Q(gender_scope=Holiday.GenderScope.ALL)
    if (Holiday.objects.for_company(company_id).filter(Q(branch__isnull=True) | Q(branch_id=e['branch_id']), local_date=day, optional=False)
            .filter(for_everyone | Q(gender_scope=e['gender']) if e['gender'] else for_everyone).exists()):
        return
    shift = resolve_shift(employee_id, local_date)
    if not shift:
        return
    leave_at_start = (Q(half_day__isnull=True, from_time__isnull=True) | Q(half_day=LeaveRequest.HalfDay.FIRST_HALF)
                      | Q(from_time__lte=_plus_minutes(shift.start, shift.grace_in_min)))
    if LeaveRequest.objects.filter(leave_at_start, employee_id=employee_id, status=LeaveRequest.Status.APPROVED,
                                   from_date__lte=day, to_date__gte=day).exists():
        return
    limit = local_time_ms(local_date, shift.start) + (shift.grace_in_min + policy['blockAfterMin']) * MIN_MS
    if at_ms <= limit:
        return
    closed_at = _npt_local_time(limit)[11:16]
    raise ConflictError(
        f'Clock-in closed at {closed_at} (your shift starts at {shift.start}). Raise a time request with the time you arrived '
        'and your manager will approve it.',
        {'code': 'late_blocked', 'shiftStart': shift.start, 'closedAt': closed_at, 'localDate': local_date})


# ================================================================ penalties.ts

def penalty_for(late_days: int, rule: dict) -> float:
    """Penalty for a number of late days under a rule."""
    if late_days < rule['lateCount']:
        return 0
    return (late_days // rule['lateCount']) * rule['penaltyDays']


def _round2(x: float) -> float:
    """Two decimals, halves up (the values are differences of quarter days, never negative here)."""
    return float(Decimal(repr(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def allocate_penalty(days: float, available: list[dict]) -> dict:
    """Split penalty days over the available balances of the leave types in order; the rest is unpaid."""
    left = days
    allocations = []
    for a in available:
        if left <= 0:
            break
        can = math.floor(max(0, a['available']) * 4) / 4   # quarter days, so balances never end up with odd fractions
        take = min(left, can)
        if take > 0:
            allocations.append({'leaveTypeId': a['leaveTypeId'], 'code': a['code'], 'days': take})
            left = _round2(left - take)
    return {'allocations': allocations, 'unpaid': max(0, _round2(left))}


def month_locked(company_id: str, bs_year: int, bs_month: int) -> bool:
    return PayrollRun.objects.filter(period__company_id=company_id, period__bs_year=bs_year, period__bs_month=bs_month,
                                     run_type=PayrollRun.RunType.REGULAR, locked_at__isnull=False).exists()


def _iso_ms(d: datetime) -> str:
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def _num(v) -> float:
    f = float(v)
    return int(f) if f.is_integer() else f


def _team(manager_employee_id: str | None, employee_field: str = 'employee_id') -> Q:
    """Everyone (None) or the people the manager manages today."""
    if manager_employee_id is None:
        return Q()
    return Q(manages(manager_employee_id, F(employee_field)))


def penalty_preview(company_id: str, bs_year: int, bs_month: int, rule: dict, *, employee_id: str | None = None,
                    manager_employee_id: str | None = None) -> list[dict]:
    """Late days per employee in the month (one employee, a manager's team or the whole company)."""
    b = bs_month_bounds(bs_year, bs_month)
    late = (latest_days().for_company(company_id)
            .filter(_team(manager_employee_id), local_date__range=(b['startAd'], b['endAd']), late_min__gte=rule['minLateMinutes']))
    if employee_id:
        late = late.filter(employee_id=employee_id)
    found = (late.values('employee_id', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))
             .annotate(late_days=Count('local_date'), late_minutes=Coalesce(Sum('late_min'), 0),
                       late_dates=ArrayAgg('local_date', order_by='local_date'))
             .order_by('-late_days', 'emp_code'))
    applied = {str(p['employee_id']): p for p in LatePenalty.objects.for_company(company_id)
               .filter(bs_year=bs_year, bs_month=bs_month, status=LatePenalty.Status.APPLIED)
               .values('id', 'employee_id', 'penalty_days', 'leave_days', 'unpaid_days', 'applied_at')}
    types = {str(t['id']): t for t in LeaveType.objects.for_company(company_id).filter(pk__in=rule['leaveTypeIds'])
             .values('id', 'code')} if rule['leaveTypeIds'] else {}
    ordered = [types[i] for i in rule['leaveTypeIds'] if i in types]
    found = list(found)
    available = _available_days([str(r['employee_id']) for r in found], [str(t['id']) for t in ordered])
    out = []
    for r in found:
        emp = str(r['employee_id'])
        penalty_days = penalty_for(r['late_days'], rule)
        avail = [{'leaveTypeId': str(t['id']), 'code': t['code'], 'available': available[(emp, str(t['id']))]} for t in ordered]
        a = allocate_penalty(penalty_days, avail)
        ap = applied.get(emp)
        out.append({
            'employeeId': emp, 'empCode': r['emp_code'], 'name': r['name_en'], 'lateDays': r['late_days'],
            'lateMinutes': r['late_minutes'], 'lateDates': [d.isoformat() for d in r['late_dates'] or []],
            'penaltyDays': _num(penalty_days), 'allocations': a['allocations'], 'unpaidDays': _num(a['unpaid']),
            'applied': {'id': str(ap['id']), 'penaltyDays': _num(ap['penalty_days']), 'leaveDays': _num(ap['leave_days']),
                        'unpaidDays': _num(ap['unpaid_days']), 'appliedAt': _iso_ms(ap['applied_at'])} if ap else None,
        })
    return out


def _available_days(employee_ids: list[str], type_ids: list[str]) -> dict[tuple[str, str], float]:
    """hajiri.leave's ledger_balance - pending_days for every (employee, leave type) pair, in two queries."""
    if not employee_ids or not type_ids:
        return {}
    ledger = {(str(r['employee_id']), str(r['leave_type_id'])): r['days'] for r in (
        LeaveLedger.objects.filter(employee_id__in=employee_ids, leave_type_id__in=type_ids)
        .values('employee_id', 'leave_type_id').annotate(days=Sum('delta_days')).order_by())}
    held: dict[tuple[str, str], Decimal] = {}
    for emp, lt, days, bridge_type, bridge_days in (
            LeaveRequest.objects.filter(Q(leave_type_id__in=type_ids) | Q(bridge_type_id__in=type_ids), employee_id__in=employee_ids,
                                        status=LeaveRequest.Status.PENDING)
            .values_list('employee_id', 'leave_type_id', 'days_requested', 'bridge_type_id', 'bridge_days')):
        # a request holds its own days on its type and the bridged non-working days on the type that pays for them
        for t, d in ((lt, days), (bridge_type, bridge_days)):
            if t is not None and str(t) in type_ids:
                k = (str(emp), str(t))
                held[k] = held.get(k, Decimal(0)) + (d or 0)
    return {(e, t): float(ledger.get((e, t)) or 0) - float(held.get((e, t)) or 0) for e in employee_ids for t in type_ids}


def _new_penalty(**fields) -> LatePenalty | None:
    """The month's penalty row, or None when one is already there (one per employee and month)."""
    try:
        with transaction.atomic():
            return LatePenalty.objects.create(**fields)
    except IntegrityError as e:
        if is_duplicate(e):
            return None
        raise


def apply_penalties(company_id: str, bs_year: int, bs_month: int, rule: dict, employee_ids: list[str] | None, actor_id: str) -> dict:
    """Apply the month's penalties for the listed employees (or everyone with one); already-applied ones are skipped."""
    if month_locked(company_id, bs_year, bs_month):
        raise ConflictError(f'{bs_month_name(bs_year, bs_month)} payroll is locked; penalties can no longer change')
    end_ad = bs_month_bounds(bs_year, bs_month)['endAd']
    label = bs_month_name(bs_year, bs_month)
    applied = skipped = 0
    for ln in penalty_preview(company_id, bs_year, bs_month, rule):
        if employee_ids is not None and ln['employeeId'] not in employee_ids:
            continue
        if ln['applied'] or ln['penaltyDays'] <= 0:
            skipped += 1
            continue
        leave_days = _num(sum(a['days'] for a in ln['allocations']))
        p = _new_penalty(company_id=company_id, employee_id=ln['employeeId'], bs_year=bs_year, bs_month=bs_month,
                         late_days=ln['lateDays'], late_minutes=ln['lateMinutes'], penalty_days=ln['penaltyDays'],
                         leave_days=leave_days, leave_type_id=ln['allocations'][0]['leaveTypeId'] if ln['allocations'] else None,
                         unpaid_days=ln['unpaidDays'], rule={**rule, 'allocations': ln['allocations'], 'lateDates': ln['lateDates']},
                         applied_by_id=actor_id)
        if p is None:
            skipped += 1
            continue
        pid = str(p.id)
        LeaveLedger.objects.bulk_create([
            LeaveLedger(employee_id=ln['employeeId'], leave_type_id=a['leaveTypeId'], effective_date=end_ad, delta_days=-a['days'],
                        reason=LeaveLedger.Reason.ADJUSTMENT, ref_type='late_penalty', ref_id=pid,
                        note=f"Late-arrival penalty, {label} ({ln['lateDays']} late days)", posted_by_id=actor_id)
            for a in ln['allocations']])
        audit('apply_late_penalty', 'late_penalty', actor_id=actor_id, row_id=pid,
              after={'employeeId': ln['employeeId'], 'bsYear': bs_year, 'bsMonth': bs_month, 'lateDays': ln['lateDays'],
                     'penaltyDays': ln['penaltyDays'], 'leaveDays': leave_days, 'unpaidDays': ln['unpaidDays']})
        parts = ' and '.join(x for x in [f'{leave_days} day(s) from leave' if leave_days else '',
                                         f"{ln['unpaidDays']} unpaid day(s)" if ln['unpaidDays'] else ''] if x)
        notify(company_id=company_id, user_id=user_id_for_employee(ln['employeeId']), employee_id=ln['employeeId'],
               template='attendance.late_penalty', title='Late-arrival penalty',
               body=f"You arrived late on {ln['lateDays']} day(s) in {label}. Under the company rule this counts as "
                    f"{ln['penaltyDays']} day(s): {parts}.", payload={'penaltyId': pid})
        applied += 1
    return {'applied': applied, 'skipped': skipped}


def reverse_penalty(company_id: str, penalty_id: str, reason: str, actor_id: str, actor_employee_id: str | None = None) -> None:
    p = LatePenalty.objects.for_company(company_id).select_for_update().filter(pk=penalty_id).first()
    if not p:
        raise NotFoundError('penalty not found')
    if actor_employee_id and str(p.employee_id) == str(actor_employee_id):
        raise ForbiddenError('someone else must reverse your own late penalty')
    if p.status != LatePenalty.Status.APPLIED:
        raise ConflictError('this penalty is already reversed')
    if month_locked(company_id, p.bs_year, p.bs_month):
        raise ConflictError('the payroll for this month is locked; the penalty can no longer be reversed')
    end_ad = bs_month_bounds(p.bs_year, p.bs_month)['endAd']
    LeaveLedger.objects.bulk_create([
        LeaveLedger(employee_id=p.employee_id, leave_type_id=a['leaveTypeId'], effective_date=end_ad, delta_days=a['days'],
                    reason=LeaveLedger.Reason.ADJUSTMENT, ref_type='late_penalty_reversed', ref_id=penalty_id,
                    note=f'Late-arrival penalty reversed: {reason}', posted_by_id=actor_id)
        for a in (p.rule or {}).get('allocations') or []])
    LatePenalty.objects.filter(pk=p.pk).update(status=LatePenalty.Status.REVERSED, reversed_by_id=actor_id, reversed_at=Now(),
                                               reverse_reason=reason)
    audit('reverse_late_penalty', 'late_penalty', actor_id=actor_id, row_id=penalty_id, after={'reason': reason})


def penalty(user: AuthUser, penalty_id: str) -> dict:
    """One applied or reversed penalty of the active company (HR)."""
    _require_module(user, 'attendance')
    p = (LatePenalty.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(penalty_id))
         .values('id', 'employee_id', 'bs_year', 'bs_month', 'status').first())
    if not p:
        raise NotFoundError('penalty not found')
    return plain(p)


def penalty_unpaid_days(employee_id: str, bs_year: int, bs_month: int) -> float:
    """Unpaid penalty days for payroll's loss of pay in a BS month (applied penalties only)."""
    total = (LatePenalty.objects.filter(employee_id=employee_id, bs_year=bs_year, bs_month=bs_month,
                                        status=LatePenalty.Status.APPLIED).aggregate(d=Sum('unpaid_days'))['d'])
    return _num(total or 0)


# ================================================================ issues.ts

def _flagged(kind: str) -> Q:
    return Q(flags__contains=[{'kind': kind}])


def attendance_issues(company_id: str, from_: str, to: str, manager_employee_id: str | None) -> list[dict]:
    """Missing punches, absences nobody asked to cover, and leave and time requests still waiting (with who they wait on).
    HR sees the company, a manager their team (manager_employee_id)."""
    missing = Q(status=Day.Status.INCOMPLETE) | _flagged('MissingOut') | _flagged('UnpairedPunch')
    pending_leave = LeaveRequest.objects.filter(employee_id=OuterRef('employee_id'), status=LeaveRequest.Status.PENDING,
                                                from_date__lte=OuterRef('local_date'), to_date__gte=OuterRef('local_date'))
    pending_time = Reg.objects.filter(employee_id=OuterRef('employee_id'), status=Reg.Status.PENDING,
                                      local_date=OuterRef('local_date'))
    days = (latest_days().for_company(company_id)
            .filter(_team(manager_employee_id), employee__status=Employee.Status.ACTIVE, local_date__range=(from_, to),
                    local_date__lt=_npt_today())
            .filter(missing | (Q(status=Day.Status.ABSENT) & ~_flagged('HalfDayLeave')))
            .exclude(Exists(pending_leave)).exclude(Exists(pending_time))
            .annotate(issue=Case(When(missing, then=Value('missing')), default=Value('absent')))
            .order_by('employee__emp_code', 'local_date')
            .values('employee_id', 'local_date', 'issue', emp_code=F('employee__emp_code'), name_en=F('employee__name_en')))

    def leave(field):
        return Subquery(LeaveRequest.objects.filter(pk=OuterRef('object_id')).values(field)[:1])

    def time_request(field):
        return Subquery(Reg.objects.filter(pk=OuterRef('object_id')).values(field)[:1])
    is_leave = Q(object_type='leave_request')
    pend = (WorkflowInstance.objects.for_company(company_id)
            .filter(_team(manager_employee_id), state=WorkflowInstance.State.PENDING,
                    object_type__in=('leave_request', 'regularisation_request'))
            .annotate(from_date=Case(When(is_leave, then=leave('from_date')), default=time_request('local_date')),
                      to_date=Case(When(is_leave, then=leave('to_date')), default=time_request('local_date')),
                      leave_name=Case(When(is_leave, then=leave('leave_type__name_en'))),
                      reg_kind=Case(When(~is_leave, then=time_request('kind'))))
            .filter(to_date__gte=from_, from_date__lte=to)
            .order_by('created_at')
            .values('id', 'object_type', 'object_id', 'employee_id', 'steps', 'current_step', 'created_at', 'from_date', 'to_date',
                    'leave_name', 'reg_kind', emp_code=F('employee__emp_code'), name_en=F('employee__name_en')))
    cache: dict = {}
    names: dict[str, str] = {}
    by_emp: dict[str, dict] = {}

    def row(i, code, name):
        return by_emp.setdefault(i, {'employeeId': i, 'empCode': code, 'name': name, 'missing': [], 'absent': [], 'pending': []})

    for d in days:
        row(str(d['employee_id']), d['emp_code'], d['name_en'])[d['issue']].append(d['local_date'].isoformat())
    now = _now()
    waiting = []
    for x in pend:
        steps = x['steps'] or []
        step = steps[x['current_step']] if 0 <= x['current_step'] < len(steps) else None
        waiting.append(approvers_for(company_id, str(x['employee_id']),
                                     step.get('role') if isinstance(step, dict) and step.get('role') else step, cache) if step else [])
    every = {i for ids in waiting for i in ids}
    if every:
        names.update({str(u['id']): u['name'] for u in AppUser.objects.filter(pk__in=every).values('id', 'name')})
    for x, ids in zip(pend, waiting):          # pend is evaluated once: the loop above filled its cache
        label = x['leave_name'] if x['object_type'] == 'leave_request' else (x['reg_kind'] or '').replace('_', ' ').title()
        row(str(x['employee_id']), x['emp_code'], x['name_en'])['pending'].append({
            'instanceId': str(x['id']), 'type': 'leave' if x['object_type'] == 'leave_request' else 'time',
            'requestId': str(x['object_id']), 'from': x['from_date'].isoformat(), 'to': x['to_date'].isoformat(), 'label': label,
            'createdAt': _iso_ms(x['created_at']), 'ageDays': math.floor((now - x['created_at']).total_seconds() / 86400),
            'waitingOn': [{'userId': i, 'name': names.get(i, 'Approver')} for i in ids],
        })
    return sorted(by_emp.values(), key=lambda r: collate_key(r['empCode']))


def summarise(issue_rows: list[dict]) -> dict:
    pending = [p for r in issue_rows for p in r['pending']]
    return {
        'employees': len(issue_rows),
        'missing': sum(len(r['missing']) for r in issue_rows),
        'absent': sum(len(r['absent']) for r in issue_rows),
        'pendingLeave': sum(1 for p in pending if p['type'] == 'leave'),
        'pendingTime': sum(1 for p in pending if p['type'] == 'time'),
        'oldestPendingDays': max([0] + [p['ageDays'] for p in pending]),
    }


# ================================================================ upload.ts

HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
UPLOAD_TEMPLATE = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-29,09:05,17:10,Device was offline\n'
MAX_UPLOAD_ROWS = 2000


def _norm(t: str) -> str | None:
    m = HHMM.match(t.strip())
    return f'{m.group(1).zfill(2)}:{m.group(2)}' if m else None


def _valid_ad(s: str) -> bool:
    """A real AD date written YYYY-MM-DD (2026-02-30 is refused: it cannot be stored or computed)."""
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def read_csv(text: str) -> list[list[str]]:
    """RFC 4180 CSV (quotes, escaped quotes, CRLF, a leading byte-order mark); rows of nothing but blanks are dropped."""
    return [r for r in csv_module.reader(io.StringIO(text.removeprefix('﻿'), newline='')) if any(f.strip() for f in r)]


def check_upload(company_id: str, csv: str, self_employee_id: str | None = None) -> dict:
    """Read and check every row: the file's columns first, then each row's employee, date, times and lock."""
    table = read_csv(csv)
    if not table:
        return {'rows': [], 'errors': ['The file is empty.']}
    header = [re.sub(r'\s+', '_', h.strip().lower()) for h in table[0]]

    def col(name):
        return header.index(name) if name in header else -1
    i_code = col('emp_code')
    i_bs = col('date_bs') if col('date_bs') >= 0 else col('date')
    i_ad = col('date_ad')
    i_in, i_out, i_note = col('in_time'), col('out_time'), col('note')
    errors = []
    if i_code < 0:
        errors.append('The first row must name the columns: emp_code, date_bs (or date_ad), in_time, out_time, note.')
    if i_bs < 0 and i_ad < 0:
        errors.append('Add a date_bs (BS, YYYY-MM-DD) or date_ad column.')
    if i_in < 0 and i_out < 0:
        errors.append('Add in_time and/or out_time columns (HH:MM, 24-hour).')
    if errors:
        return {'rows': [], 'errors': errors}
    body = table[1:]
    if len(body) > MAX_UPLOAD_ROWS:
        return {'rows': [], 'errors': [f'Upload at most {MAX_UPLOAD_ROWS} rows at a time.']}

    def cell_of(r, k):
        return (r[k] if k < len(r) and r[k] is not None else '').strip() if k >= 0 else ''
    codes = list(dict.fromkeys(c for c in (cell_of(r, i_code) for r in body) if c))
    by_code = {e['emp_code']: e for e in Employee.objects.for_company(company_id).filter(emp_code__in=codes)
               .values('id', 'emp_code', 'name_en', 'status', 'join_date', 'exit_date')}
    today = npt_today()
    seen: set[str] = set()
    out = []
    for i, r in enumerate(body):
        def cell(k):
            return cell_of(r, k)
        row = {'line': i + 2, 'empCode': cell(i_code), 'date': None, 'dateBs': None, 'inTime': None, 'outTime': None,
               'note': cell(i_note) or None, 'employeeId': None, 'name': None, 'overnight': False, 'errors': []}
        e = by_code.get(row['empCode'])
        if not row['empCode']:
            row['errors'].append('Employee code is missing.')
        elif not e:
            row['errors'].append(f"No employee with code {row['empCode']}.")
        elif e['status'] != Employee.Status.ACTIVE:
            row['errors'].append(f"{e['name_en']} is not active.")
        elif self_employee_id and str(e['id']) == str(self_employee_id):
            row['errors'].append('You cannot upload your own attendance; ask another HR person.')
        else:
            row['employeeId'] = str(e['id'])
            row['name'] = e['name_en']
        try:
            if i_bs >= 0 and cell(i_bs):
                row['dateBs'] = cell(i_bs)
                row['date'] = bs_to_ad(parse_bs(row['dateBs']))
            elif i_ad >= 0 and cell(i_ad) and _valid_ad(cell(i_ad)):
                row['date'] = cell(i_ad)
            else:
                row['errors'].append('The date is missing or not YYYY-MM-DD.')
        except (BsRangeError, ValueError, KeyError):
            row['errors'].append(f'{cell(i_bs)} is not a valid BS date.')
        joined = e['join_date'].isoformat() if e and e['join_date'] else None
        left = e['exit_date'].isoformat() if e and e['exit_date'] else None
        if row['date'] and row['date'] > today:
            row['errors'].append('The date is in the future.')
        if row['date'] and joined and row['date'] < joined:
            row['errors'].append('The date is before the employee joined.')
        if row['date'] and left and row['date'] > left:
            row['errors'].append('The date is after the employee left.')
        raw_in, raw_out = cell(i_in), cell(i_out)
        row['inTime'] = _norm(raw_in) if raw_in else None
        row['outTime'] = _norm(raw_out) if raw_out else None
        if raw_in and not row['inTime']:
            row['errors'].append(f'In time {raw_in} is not HH:MM.')
        if raw_out and not row['outTime']:
            row['errors'].append(f'Out time {raw_out} is not HH:MM.')
        if not raw_in and not raw_out:
            row['errors'].append('Give an in time, an out time or both.')
        if row['inTime'] and row['outTime'] and row['outTime'] <= row['inTime']:
            row['overnight'] = True
        if row['note'] and len(row['note']) > 200:
            row['errors'].append('The note is longer than 200 characters.')
        if row['employeeId'] and row['date']:
            k = f"{row['employeeId']}|{row['date']}"
            if k in seen:
                row['errors'].append('This employee and date appear more than once in the file.')
            seen.add(k)
            if not row['errors'] and is_attendance_locked(row['employeeId'], row['date']):
                row['errors'].append('This day is locked by payroll.')
        out.append(row)
    return {'rows': out, 'errors': []}


def _upload_punch(*, company_id, employee_id, emp_code, local_date, time, day_offset, direction, reason, actor_id) -> dict | None:
    """One manual punch with the same idempotency key as POST /punch/manual; None for a duplicate."""
    from hajiri.attendance.repo import from_ms
    from hajiri.punch.services import ensure_pseudo_device, insert_punch, npt_local_time
    device = ensure_pseudo_device(company_id, 'MANUAL')
    at = local_time_ms(local_date, time, day_offset)
    punch_date = local_date_of(at)
    p = insert_punch(device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'], device_user_pin=emp_code,
                     employee_id=employee_id, punched_at=from_ms(at), device_local_time=npt_local_time(at), local_date=punch_date,
                     direction=direction, verify_mode='manual', source='manual',
                     raw={'reason': reason, 'enteredBy': actor_id, 'upload': True},
                     idempotency_key=sha256_hex(f'manual|{employee_id}|{punch_date}|{time}|{direction}'))
    return {'id': str(p.id), 'at': at} if p else None


def import_upload(company_id: str, upload_rows: list[dict], reason: str, actor_id: str) -> dict:
    punches = duplicates = days = 0
    for r in upload_rows:
        if r['errors'] or not r['employeeId'] or not r['date']:
            continue
        note = f" ({r['note']})" if r['note'] else ''
        why = f'CSV upload: {reason}{note}'
        last = None
        for direction, time_, offset in (('in', r['inTime'], 0), ('out', r['outTime'], 1 if r['overnight'] else 0)):
            if not time_:
                continue
            p = _upload_punch(company_id=str(company_id), employee_id=r['employeeId'], emp_code=r['empCode'], local_date=r['date'],
                              time=time_, day_offset=offset, direction=direction, reason=why, actor_id=actor_id)
            if p:
                punches += 1
                last = p
            else:
                duplicates += 1
        if last:
            recompute_for_punch(r['employeeId'], last['at'], last['id'])
            days += 1
    return {'punches': punches, 'duplicates': duplicates, 'days': days}


# ================================================================ whosout.ts

OUT_LABEL = {'leave': 'On leave', 'travel': 'Travelling', 'wfh': 'From home', 'field': 'In the field'}
OUT_TONE = {'leave': 'info', 'travel': 'accent', 'wfh': 'ok', 'field': 'warn'}


def scope_employee_ids(user: AuthUser, scope: str) -> list[str] | None:
    """Whose absences the person sees: None for everyone, else the ids of their department or team (and themselves)."""
    if scope == 'company':
        return None
    me = user.employee_id
    if not me:
        return None if has_role(user, 'hr') else []
    current = EmployeeAssignment.objects.for_company(user.company_id).filter(valid__contains=_npt_today())
    mine = current.filter(employee_id=me).values('manager_id', 'org_unit_id').first()
    if scope == 'department':
        unit = mine and mine['org_unit_id']
        ids = [str(i) for i in current.filter(org_unit_id=unit).values_list('employee_id', flat=True)] if unit else []
        return ids or [me]
    team = Q(manager_id=me)
    if mine and mine['manager_id']:
        team |= Q(manager_id=mine['manager_id']) | Q(employee_id=mine['manager_id'])
    return list(dict.fromkeys([*(str(i) for i in current.filter(team).values_list('employee_id', flat=True)), me]))


PERSON = {'emp': F('employee_id'), 'name_en': F('employee__name_en'), 'designation': F('employee__designation')}


def whos_out(company_id: str, ids: list[str] | None, from_: str, to: str) -> list[dict]:
    def in_scope(model):
        qs = model.objects.for_company(company_id).filter(employee__status=Employee.Status.ACTIVE)
        return qs.filter(employee_id__in=ids) if ids is not None else qs
    leave = (in_scope(LeaveRequest).filter(status=LeaveRequest.Status.APPROVED, to_date__gte=from_, from_date__lte=to)
             .values('from_date', 'to_date', 'half_day', 'hours', **PERSON))
    travel = (in_scope(TravelRequest).filter(status__in=(TravelRequest.Status.APPROVED, TravelRequest.Status.SETTLING,
                                                         TravelRequest.Status.SETTLED), to_date__gte=from_, from_date__lte=to)
              .values('from_date', 'to_date', 'destination', **PERSON))
    wfh_days = (in_scope(Reg).filter(kind=Reg.Kind.WFH, status=Reg.Status.APPROVED, local_date__range=(from_, to))
                .values_list('local_date', 'employee_id', 'employee__name_en', 'employee__designation')
                .union(in_scope(PunchEvent).filter(local_date__range=(from_, to), raw__flags__has_key='Remote')
                       .values_list('local_date', 'employee_id', 'employee__name_en', 'employee__designation')))
    field = (in_scope(FieldVisit).filter(local_date__range=(from_, to)).order_by('employee_id', 'local_date', '-started_at')
             .distinct('employee_id', 'local_date').values('local_date', 'place', **PERSON))
    days = [{'date': d, 'people': []} for d in date_range(from_, to)]
    by_date = {d['date']: d for d in days}

    def add(d, x):
        day = by_date.get(d)
        if day and not any(q['employeeId'] == x['employeeId'] for q in day['people']):
            day['people'].append(x)

    def person(r, kind, detail):
        return {'employeeId': str(r['emp']), 'name': r['name_en'], 'designation': r['designation'], 'kind': kind, 'detail': detail}

    def span(r):
        return date_range(max(r['from_date'].isoformat(), from_), min(r['to_date'].isoformat(), to))
    for lv in leave:
        detail = (f"{_num(lv['hours'])} h" if lv['hours'] else
                  ('First half' if lv['half_day'] == LeaveRequest.HalfDay.FIRST_HALF else 'Second half') if lv['half_day'] else None)
        for d in span(lv):
            add(d, person(lv, 'leave', detail))
    for t in travel:
        for d in span(t):
            add(d, person(t, 'travel', t['destination']))
    for local_date, emp, name, designation in wfh_days:
        add(local_date.isoformat(), person({'emp': emp, 'name_en': name, 'designation': designation}, 'wfh', None))
    for f in field:
        add(f['local_date'].isoformat(), person(f, 'field', f['place']))
    for d in days:
        d['people'].sort(key=lambda x: collate_key(x['name']))
    return days


# ================================================================ routes.ts

def self_of(user: AuthUser) -> str | None:
    """The acting person's own employee id, so nobody but an administrator edits their own attendance or penalties."""
    return user.employee_id if not has_role(user, 'admin') else None


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _require_module(user: AuthUser, module: str) -> None:
    _require(user, *MODULE_ROLES[module])


def _staff(user: AuthUser) -> None:
    _require(user, *MODULE_ROLES['attendance'], 'manager')


def _manager_scope(user: AuthUser) -> str | None:
    """None for HR (everyone), else the manager's own employee id (NIL_UUID: a manager without one sees nobody)."""
    return None if has_module(user, 'attendance') else (user.employee_id or NIL_UUID)


def paid_leave_types(company_id: str) -> list[dict]:
    """Paid leave types with a balance a penalty may be taken from (not the event leaves)."""
    return plain(list(LeaveType.objects.for_company(company_id).filter(paid=True)
                      .exclude(code__in=('MATERNITY', 'PATERNITY', 'MOURNING')).order_by('sort_order', 'code')
                      .values('id', 'code', 'name_en')))


def get_policy(user: AuthUser) -> dict:
    return {'policy': extras_policy(company_id_of(user))}


def put_policy(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = plain(validate(ExtrasPolicyIn, body))
    ids = b['latePenalty']['leaveTypeIds']
    if ids:
        t = list(LeaveType.objects.for_company(company_id).filter(pk__in=ids).values('paid', 'event_days'))
        if len(t) != len(set(ids)):
            raise BadRequestError('a chosen leave type does not exist')
        if any(not x['paid'] or x['event_days'] is not None for x in t):
            raise BadRequestError('penalties can only be taken from paid leave with a balance')
    with transaction.atomic():
        before = extras_policy(company_id)
        save_extras_policy(company_id, b, user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=company_id, before={EXTRAS_KEY: before}, after={EXTRAS_KEY: b})
    return {'policy': b}


def penalties(user: AuthUser, query: dict) -> dict:
    _staff(user)
    company_id = company_id_of(user)
    q = validate(MonthIn, query)
    policy = extras_policy(company_id)
    lines = penalty_preview(company_id, q['bsYear'], q['bsMonth'], policy['latePenalty'], manager_employee_id=_manager_scope(user))
    return {'rule': policy['latePenalty'], 'locked': month_locked(company_id, q['bsYear'], q['bsMonth']), 'lines': lines}


def my_penalties(user: AuthUser, query: dict) -> dict:
    company_id = company_id_of(user)
    if not user.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    t = ad_to_bs(npt_today())
    q = validate(MonthPartialIn, query)
    y, m = q.get('bsYear') or t.year, q.get('bsMonth') or t.month
    lp = extras_policy(company_id)['latePenalty']
    lines = penalty_preview(company_id, y, m, lp, employee_id=user.employee_id)
    line = lines[0] if lines else None
    return {'bsYear': y, 'bsMonth': m, 'enabled': lp['enabled'],
            'rule': {'lateCount': lp['lateCount'], 'penaltyDays': lp['penaltyDays'], 'minLateMinutes': lp['minLateMinutes']},
            'lateDays': line['lateDays'] if line else 0, 'lateDates': line['lateDates'] if line else [],
            'projectedDays': (line['penaltyDays'] if line else 0) if lp['enabled'] else 0, 'applied': line['applied'] if line else None}


def apply(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = plain(validate(ApplyPenaltiesIn, body))
    policy = extras_policy(company_id)
    if not policy['latePenalty']['enabled']:
        raise ConflictError('turn the late-arrival penalty on first')
    me = self_of(user)
    if b.get('employeeIds') is not None and me and me in b['employeeIds']:
        raise ForbiddenError('someone else must apply your own late penalty')
    with transaction.atomic():
        return apply_penalties(company_id, b['bsYear'], b['bsMonth'], policy['latePenalty'], b.get('employeeIds'), user.id)


def reverse(user: AuthUser, penalty_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    pid = parse_uuid(penalty_id)
    b = validate(ReverseIn, body)
    with transaction.atomic():
        reverse_penalty(company_id_of(user), pid, b['reason'], user.id, self_of(user))
    return {'ok': True}


def issues(user: AuthUser, query: dict) -> dict:
    _staff(user)
    q = validate(IssueRangeIn, query)
    found = attendance_issues(company_id_of(user), q['from'], q['to'], _manager_scope(user))
    return {'summary': summarise(found), 'rows': found}


def issue_summary(user: AuthUser, query: dict) -> dict:
    """Pre-payroll check: the open issues of a BS month in numbers (the payroll pre-flight links here). Payroll admins
    read it too: it is on the first step of the pay run wizard. Only counts; the issue list stays attendance's."""
    _require(user, *MODULE_ROLES['attendance'], *MODULE_ROLES['payroll'])
    q = validate(MonthIn, query)
    b = bs_month_bounds(q['bsYear'], q['bsMonth'])
    found = attendance_issues(company_id_of(user), b['startAd'], b['endAd'], None)
    return {'bsYear': q['bsYear'], 'bsMonth': q['bsMonth'], 'from': b['startAd'], 'to': b['endAd'], **summarise(found)}


def _nudge_once(company_id: str, user_id: str, kind: str, ref: str, today: str, actor_id: str) -> bool:
    """Record today's reminder; False when this one was already sent today."""
    _, created = AttendanceNudge.objects.get_or_create(target_user_id=user_id, kind=kind, ref=ref, local_date=today,
                                                       defaults={'company_id': company_id, 'nudged_by_id': actor_id})
    return created


def nudge(user: AuthUser, body: dict) -> dict:
    _staff(user)
    company_id = company_id_of(user)
    b = plain(nudge_body(body))
    today = npt_today()
    if b['target'] == 'employee':
        if not exists_in_company(Employee, company_id, pk=b['employeeId']):
            raise NotFoundError('employee not found')
        wf_assert_manages(user, b['employeeId'])
        if b['employeeId'] == user.employee_id:
            raise BadRequestError('you cannot nudge yourself')
        r = next((x for x in attendance_issues(company_id, b['from'], b['to'], None) if x['employeeId'] == b['employeeId']), None)
        if not r or (not r['missing'] and not r['absent']):
            raise ConflictError('there is nothing for this employee to fix in this period')
        user_id = user_id_for_employee(b['employeeId'])
        if not user_id:
            raise ConflictError('this employee has no sign-in to notify')
        with transaction.atomic():
            if not _nudge_once(company_id, user_id, AttendanceNudge.Kind.EMPLOYEE, f"{b['from']}:{b['to']}", today, user.id):
                return {'sent': 0, 'already': True}
            parts = ' and '.join(x for x in [f"{len(r['missing'])} day(s) with a missing punch" if r['missing'] else '',
                                             f"{len(r['absent'])} absent day(s) with no leave or time request" if r['absent'] else ''] if x)
            notify(company_id=company_id, user_id=user_id, employee_id=b['employeeId'], template='attendance.nudge',
                   title='Please fix your attendance',
                   body=f"You have {parts} between {bs_range(b['from'], b['to'])}. Apply for leave or raise a time request before payroll.",
                   payload={'from': b['from'], 'to': b['to']}, channels=['push'])
        return {'sent': 1, 'already': False}
    inst = get_instance(b['instanceId'])
    if not inst or inst['company_id'] != company_id or inst['object_type'] not in ('leave_request', 'regularisation_request'):
        raise NotFoundError('request not found')
    if inst['state'] != WorkflowInstance.State.PENDING:
        raise ConflictError('this request is no longer waiting')
    if inst['employee_id']:
        wf_assert_manages(user, inst['employee_id'])
    steps = inst['steps']
    step = steps[inst['current_step']] if 0 <= inst['current_step'] < len(steps) else None
    ids = []
    if step:
        role = step.get('role') if isinstance(step, dict) and step.get('role') else step
        ids = [i for i in dict.fromkeys(approvers_for(company_id, inst['employee_id'], role)) if i != user.id]
    if not ids:
        raise ConflictError('there is nobody else to remind about this request')
    emp_name = (Employee.objects.for_company(company_id).filter(pk=inst['employee_id']).values_list('name_en', flat=True).first()
                if inst['employee_id'] else None)
    created = inst['created_at'].astimezone(timezone.utc).date().isoformat()
    kind = 'leave' if inst['object_type'] == 'leave_request' else 'time'
    sent = 0
    with transaction.atomic():
        for i in ids:
            if not _nudge_once(company_id, i, AttendanceNudge.Kind.APPROVER, inst['id'], today, user.id):
                continue
            notify(company_id=company_id, user_id=i, template='attendance.nudge_approver', title='A request is waiting for you',
                   body=f"{emp_name or 'An employee'}'s {kind} request has been waiting since {bs_range(created, created)}. "
                        'Please decide it before payroll.',
                   payload={'instanceId': inst['id']}, channels=['push'])
            sent += 1
    return {'sent': sent, 'already': sent == 0}


def upload_check(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = validate(AttendanceUploadIn, body)
    out = check_upload(company_id_of(user), b['csv'], self_of(user))
    return {'errors': out['errors'], 'rows': out['rows'], 'valid': sum(1 for x in out['rows'] if not x['errors']),
            'invalid': sum(1 for x in out['rows'] if x['errors'])}


def upload_import(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = validate(ImportIn, body)
    checked = check_upload(company_id, b['csv'], self_of(user))
    if checked['errors']:
        raise BadRequestError(' '.join(checked['errors']))
    bad = [x for x in checked['rows'] if x['errors']]
    if bad:
        raise BadRequestError(f'{len(bad)} row(s) have errors; fix them or remove them and upload again',
                              {'rows': [{'line': x['line'], 'errors': x['errors']} for x in bad]})
    if not checked['rows']:
        raise BadRequestError('there are no rows to import')
    # All or nothing: a failure part-way must not leave punches behind with no upload on record (a retry would then
    # count them as duplicates and skip recomputing their days).
    with transaction.atomic():
        out = import_upload(company_id, checked['rows'], b['reason'], user.id)
        up = AttendanceUpload.objects.create(company_id=company_id, filename=b.get('filename') or None, reason=b['reason'],
                                             rows_total=len(checked['rows']), punches=out['punches'],
                                             duplicates=out['duplicates'], uploaded_by_id=user.id)
        audit('attendance_upload', 'attendance_upload', actor_id=user.id, row_id=str(up.id),
              after={'rows': len(checked['rows']), **out, 'reason': b['reason']})
    return {'id': str(up.id), 'rows': len(checked['rows']), **out}


def uploads(user: AuthUser) -> dict:
    _require_module(user, 'attendance')
    found = (AttendanceUpload.objects.for_company(company_id_of(user)).order_by('-created_at')
             .values('id', 'filename', 'reason', 'rows_total', 'punches', 'duplicates', 'created_at',
                     uploaded_by_name=F('uploaded_by__name'))[:50])
    return {'uploads': plain(list(found))}


def whos_out_for(user: AuthUser, query: dict) -> dict:
    company_id = company_id_of(user)
    q = validate(WhosOutIn, query)
    policy = extras_policy(company_id)
    ids = scope_employee_ids(user, policy['whosOutScope'])
    if ids is not None and not ids:
        return {'scope': policy['whosOutScope'], 'days': []}
    from_ = npt_today()
    to = (date.fromisoformat(from_) + timedelta(days=q['days'] - 1)).isoformat()
    return {'scope': policy['whosOutScope'], 'days': whos_out(company_id, ids, from_, to)}
