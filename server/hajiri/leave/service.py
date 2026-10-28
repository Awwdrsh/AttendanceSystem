"""
Leave rules (port of apps/api/src/modules/leave/service.ts): day counting with the sandwich rule, hourly leave,
event-based statutory leave, the ledger and balances, substitute (comp-off) credits and encashment at separation.

Numbers follow the TS code: ledger sums are turned into floats (JavaScript Number), and results are rounded with
Math.round semantics, so explanations and JSON match the TS API digit for digit. The number helpers below exist only
for that parity (Python has no standard way to print a number the way a JavaScript template literal does).

Dates come in as 'YYYY-MM-DD' strings (the TS shape) and are turned into datetime.date before they reach a query.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import (
    BooleanField, Case, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Q, Sum, Value, When,
)

from engines.bs_calendar import ad_to_bs, fiscal_year_of, weekday_of

from hajiri.attendance.repo import resolve_shift
from hajiri.attendance.service import date_range
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.db.models import AttendanceDay, Employee, EmployeeAssignment, Holiday, LeaveLedger, LeaveRequest, LeaveType
from hajiri.web.format import to_fixed

Status = LeaveRequest.Status
Reason = LeaveLedger.Reason
HELD = (Status.PENDING, Status.APPROVED)          # requests that hold days
ZERO = Value(Decimal(0), output_field=DecimalField())


# ---------------------------------------------------------------- number helpers (JavaScript semantics)

def num(v) -> float:
    """Number(v) of a numeric column or text; None is 0."""
    return 0.0 if v is None else float(v)


def js_round(x: float) -> float:
    """Math.round: halves go up (towards +infinity)."""
    return float(math.floor(x + 0.5))


def round2(x: float) -> float:
    """Math.round(x * 100) / 100."""
    return js_round(x * 100) / 100


def js(x) -> str:
    """How a JavaScript template literal prints a number: 2 not 2.0, 0.5, 1.1."""
    if isinstance(x, bool):
        return 'true' if x else 'false'
    if isinstance(x, (int, float)):
        f = float(x)
        if f.is_integer():
            return str(int(f))
        return repr(f)
    return str(x)


def out(x):
    """A number for JSON: integral values as int (JSON.stringify prints 10, not 10.0)."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def fixed(x: float, digits: int) -> str:
    return to_fixed(x, digits)


def dec(x) -> Decimal:
    """A number for a numeric(…, 2) column, rounded like PostgreSQL rounds a float it stores (half away from zero)."""
    return Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def day(v) -> date:
    """A date from 'YYYY-MM-DD' (or a date)."""
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def add_iso(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def hhmm(t) -> str | None:
    """A time column as 'HH:MM' (to_char(t, 'HH24:MI'))."""
    if t is None or isinstance(t, str):
        return t[:5] if t else None
    return t.strftime('%H:%M')


# ---------------------------------------------------------------- rows

def _type(r: dict | None) -> dict | None:
    """A leave_type row (`.values()`) with its ids as strings."""
    if r is None:
        return None
    t = dict(r)
    for k in ('id', 'company_id', 'club_with_id'):
        if t.get(k) is not None:
            t[k] = str(t[k])
    return t


def type_rows(qs) -> list[dict]:
    return [_type(r) for r in qs.values()]


def get_leave_type(type_id: str, company_id: str | None = None) -> dict | None:
    """One leave type; with `company_id`, only if it belongs to that company."""
    qs = LeaveType.objects.for_company(company_id) if company_id is not None else LeaveType.objects.all()
    return _type(qs.filter(pk=type_id).values().first())


def leave_type_by_code(company_id: str, code: str) -> dict | None:
    return _type(LeaveType.objects.for_company(company_id).filter(code=code).values().first())


def get_employee_lite(employee_id: str, company_id: str | None = None) -> dict | None:
    """The employee fields the leave rules need; with `company_id`, only an employee of that company."""
    qs = Employee.objects.for_company(company_id) if company_id is not None else Employee.objects.all()
    r = qs.filter(pk=employee_id).values('id', 'company_id', 'gender', 'weekly_off_days', 'join_date', 'probation_end',
                                         'exit_date', 'status').first()
    if not r:
        return None
    return {'id': str(r['id']), 'company_id': str(r['company_id']), 'gender': r['gender'],
            'weekly_off_days': list(r['weekly_off_days'] or []), 'join_date': iso(r['join_date']),
            'probation_end': iso(r['probation_end']), 'exit_date': iso(r['exit_date']), 'status': r['status']}


def latest_days():
    """attendance_day rows that are the newest computed version of their day (what v_attendance_latest shows)."""
    newer = AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                         version__gt=OuterRef('version'))
    return AttendanceDay.objects.exclude(Exists(newer))


def _not(qs, exclude_id):
    return qs.exclude(pk=exclude_id) if exclude_id else qs


# ---------------------------------------------------------------- day counting

def working_days_between(emp: dict, from_: str, to: str) -> tuple[list[str], list[str]]:
    """Dates in [from, to] that are neither weekly offs nor (gender-aware, branch-aware) holidays: (working, non_working)."""
    branch = next(iter(EmployeeAssignment.objects.filter(employee_id=emp['id'], valid__contains=day(from_))
                       .values_list('branch_id', flat=True)[:1]), None)
    holidays = {iso(d) for d in Holiday.objects.for_company(emp['company_id'])
                .filter(Q(branch__isnull=True) | Q(branch_id=branch), local_date__range=(day(from_), day(to)), optional=False)
                .filter(Q(gender_scope=Holiday.GenderScope.ALL) | Q(gender_scope=emp['gender']))
                .values_list('local_date', flat=True)}
    working: list[str] = []
    non_working: list[str] = []
    offs = emp['weekly_off_days']
    for d in date_range(from_, to):
        (non_working if d in holidays or weekday_of(d) in offs else working).append(d)
    return working, non_working


def group_head(t: dict) -> dict:
    """The type whose order and sandwich rule a type follows: the type it is clubbed with, or itself."""
    if not t.get('club_with_id'):
        return t
    return get_leave_type(t['club_with_id']) or t


def compute_leave_days(emp: dict, t: dict, from_: str, to: str, half_day: str | None, sandwich_policy: bool) -> dict:
    """
    Days deducted for a request (plan §10.6): working days only, unless the sandwich rule bridges the range with leave on
    both ends and only holidays/weekly-offs between. Half-day requests are a single date worth 0.5.
    """
    if half_day:
        if from_ != to:
            raise BadRequestError('half-day leave must be a single date')
        if not t['allow_half_day']:
            raise BadRequestError(f"{t['name_en']} cannot be taken as a half day")
        working, _ = working_days_between(emp, from_, to)
        if not working:
            raise BadRequestError(f'{from_} is not a working day')
        return {'days': 0.5, 'workingDays': working, 'excludedDays': [], 'sandwichApplied': False,
                'explanation': 'Half day will be deducted'}
    if t['event_days'] is not None:
        # Event-based statutory leave (maternity, maternity-care, mourning) runs in calendar days.
        all_ = date_range(from_, to)
        return {'days': len(all_), 'workingDays': all_, 'excludedDays': [], 'sandwichApplied': False,
                'explanation': f"{len(all_)} calendar day(s) of {t['name_en']}"}
    working, non_working = working_days_between(emp, from_, to)
    sandwich = ((group_head(t)['sandwich_rule'] or sandwich_policy) and len(working) >= 2 and len(non_working) > 0
                and working[0] == from_ and working[-1] == to)
    if sandwich:
        all_ = date_range(from_, to)
        return {'days': len(all_), 'workingDays': all_, 'excludedDays': [], 'sandwichApplied': True,
                'explanation': f'{len(all_)} calendar days will be deducted (sandwich rule: {len(non_working)} non-working '
                               'day(s) between leave days count)'}
    if not working:
        raise BadRequestError('the selected range contains no working days')
    explanation = (f'{len(working)} working day(s) will be deducted; {len(non_working)} weekly-off/holiday day(s) excluded'
                   if non_working else f'{len(working)} working day(s) will be deducted')
    return {'days': len(working), 'workingDays': working, 'excludedDays': non_working, 'sandwichApplied': False,
            'explanation': explanation}


HOURLY_MIN_MINUTES = 30


def _to_min(s: str) -> int:
    return int(s[0:2]) * 60 + int(s[3:5])


def compute_hourly_leave(emp: dict, t: dict, day_: str, from_time: str, to_time: str) -> dict:
    """
    Hourly (short) leave on one working day: the window must sit inside the day's (daytime) shift, and is deducted as
    minutes / scheduled working minutes of that shift (span minus the fixed break), to 2 decimals.
    """
    if not t['allow_hourly']:
        raise BadRequestError(f"{t['name_en']} cannot be taken by the hour")
    if not t['paid'] or t['event_days'] is not None:
        raise BadRequestError('only paid leave with a balance can be taken by the hour')
    working, _ = working_days_between(emp, day_, day_)
    if not working:
        raise BadRequestError('hourly leave must be on a working day')
    shift = resolve_shift(emp['id'], day_)
    if not shift:
        raise BadRequestError('there is no shift on this day to take hourly leave from')
    start, end = _to_min(shift.start), _to_min(shift.end)
    if end <= start:
        raise BadRequestError('hourly leave is not available on shifts that cross midnight; apply for a half or full day')
    f, tt = _to_min(from_time), _to_min(to_time)
    if tt <= f:
        raise BadRequestError('the end time must be after the start time')
    if f < start or tt > end:
        raise BadRequestError(f'hourly leave must be within your shift ({shift.start}–{shift.end})')
    minutes = tt - f
    if minutes < HOURLY_MIN_MINUTES:
        raise BadRequestError(f'hourly leave must be at least {HOURLY_MIN_MINUTES} minutes')
    scheduled = end - start - shift.break_minutes
    if scheduled <= 0 or minutes >= scheduled:
        raise BadRequestError('this covers the whole working day; apply for a full day instead')
    days = max(0.01, round2(minutes / scheduled))
    hours = round2(minutes / 60)
    return {'days': out(days), 'hours': out(hours), 'minutes': minutes, 'workingDays': [day_], 'excludedDays': [],
            'sandwichApplied': False,
            'explanation': f'{js(hours)} hour(s) of a {js(round2(scheduled / 60))}-hour working day = {js(days)} day(s) will be deducted'}


EVENT_GAP_DAYS = 30


def _sum(qs, expr='delta_days') -> float:
    return num(qs.aggregate(s=Sum(expr))['s'])


def event_days_used(employee_id: str, leave_type_id: str, from_: str, to: str, exclude_id: str | None = None) -> float:
    """Days already requested (pending or approved) for the same event: requests of the type within EVENT_GAP_DAYS."""
    gap = timedelta(days=EVENT_GAP_DAYS)
    qs = LeaveRequest.objects.filter(employee_id=employee_id, leave_type_id=leave_type_id, status__in=HELD,
                                     to_date__gte=day(from_) - gap, from_date__lte=day(to) + gap)
    return _sum(_not(qs, exclude_id), 'days_requested')


def ledger_balance(employee_id: str, leave_type_id: str, as_of: str | None = None) -> float:
    qs = LeaveLedger.objects.filter(employee_id=employee_id, leave_type_id=leave_type_id)
    if as_of is not None:
        qs = qs.filter(effective_date__lte=day(as_of))
    return _sum(qs)


def pending_days(employee_id: str, leave_type_id: str) -> float:
    """Days held by pending requests, including the non-working days a request bridges on the type that pays for them."""
    lt = str(leave_type_id)
    held = (Case(When(leave_type_id=lt, then=F('days_requested')), default=ZERO, output_field=DecimalField())
            + Case(When(bridge_type_id=lt, then=F('bridge_days')), default=ZERO, output_field=DecimalField()))
    qs = LeaveRequest.objects.filter(Q(leave_type_id=lt) | Q(bridge_type_id=lt), employee_id=employee_id,
                                     status=Status.PENDING)
    return _sum(qs, held)


def pending_by_type(employee_id: str) -> dict[str, float]:
    """pending_days for every type at once (one query): {leave_type_id: days held by pending requests}."""
    held: dict[str, Decimal] = {}
    for lt, days, bt, bridge in (LeaveRequest.objects.filter(employee_id=employee_id, status=Status.PENDING)
                                 .values_list('leave_type_id', 'days_requested', 'bridge_type_id', 'bridge_days')):
        held[str(lt)] = held.get(str(lt), Decimal(0)) + days
        if bt is not None:
            held[str(bt)] = held.get(str(bt), Decimal(0)) + bridge
    return {k: num(v) for k, v in held.items()}


def bridge_across(emp: dict, t: dict, from_: str, to: str, sandwich_policy: bool, exclude_id: str | None = None) -> dict | None:
    """
    Sandwich across requests: when this request and another pending or approved full-day leave are separated only by
    weekly offs or holidays, those days are charged to whichever of the two types comes first in the leave order (a
    clubbed type takes the order of the type it is clubbed with; on a tie, this request's type), if that type or the
    company applies the sandwich rule. Unpaid and event-based types never absorb bridge days, and a gap is charged once.
    """
    if t['event_days'] is not None:
        return None
    f, tt = day(from_), day(to)
    held = _not(LeaveRequest.objects.filter(employee_id=emp['id'], status__in=HELD), exclude_id)
    adjacent = (held.filter(half_day__isnull=True, from_time__isnull=True)
                .filter(Q(to_date__range=(f - timedelta(days=15), f - timedelta(days=2)))
                        | Q(from_date__range=(tt + timedelta(days=2), tt + timedelta(days=15))))
                .values('id', 'leave_type_id', 'from_date', 'to_date'))
    # Leave before this request first, then the nearest.
    adj = sorted(adjacent, key=lambda a: (not a['to_date'] < f, abs((a['from_date'] - f).days)))
    my_head = group_head(t)
    for a in adj:
        a_from, a_to = iso(a['from_date']), iso(a['to_date'])
        before = a_to < from_
        gap_from = add_iso(a_to, 1) if before else add_iso(to, 1)
        gap_to = add_iso(from_, -1) if before else add_iso(a_from, -1)
        if gap_from > gap_to:
            continue
        working, non_working = working_days_between(emp, gap_from, gap_to)
        if working or not non_working:
            continue
        gf, gt = day(gap_from), day(gap_to)
        taken = held.filter(Q(bridge_days__gt=0, bridge_from__lte=gt, bridge_to__gte=gf)
                            | Q(from_date__lte=gt, to_date__gte=gf)).exists()
        if taken:
            continue
        other = get_leave_type(str(a['leave_type_id']))
        if not other:
            continue
        other_head = group_head(other)
        mine_first = my_head['id'] == other_head['id'] or my_head['sort_order'] <= other_head['sort_order']
        owner = t if mine_first else other
        owner_head = my_head if mine_first else other_head
        if not (sandwich_policy or owner_head['sandwich_rule']):
            continue
        if not owner['paid'] or owner['event_days'] is not None:
            continue
        days = len(non_working)
        return {
            'days': days, 'typeId': owner['id'], 'typeName': owner['name_en'], 'from': gap_from, 'to': gap_to,
            'adjacentRequestId': str(a['id']),
            'explanation': f"{days} weekly-off/holiday day(s) between this and your "
                           f"{'other' if other['id'] == t['id'] else other['name_en']} leave will be charged to "
                           f"{owner['name_en']} (sandwich rule)",
        }
    return None


# ---------------------------------------------------------------- balances

def balances(emp: dict, as_of_ad: str) -> list[dict]:
    """Balance explanation per plan §13.5: Opening (1 Shrawan) + accruals - taken - lapsed + carried forward."""
    fy = fiscal_year_of(ad_to_bs(as_of_ad))
    fy_start = day(fy.start_ad)
    types = type_rows(LeaveType.objects.for_company(emp['company_id'])
                      .filter(Q(gender_scope=LeaveType.GenderScope.ALL) | Q(gender_scope=emp['gender']))
                      .order_by('sort_order', 'code'))
    # Ledger totals per type, reason and side of the fiscal-year start (summed as numbers like the TS code).
    groups: dict[str, list[dict]] = {}
    for g in (LeaveLedger.objects.filter(employee_id=emp['id'])
              .values('leave_type_id', 'reason',
                      in_fy=ExpressionWrapper(Q(effective_date__gte=fy_start), output_field=BooleanField()))
              .annotate(total=Sum('delta_days')).order_by()):
        groups.setdefault(str(g['leave_type_id']), []).append(g)
    pending_all = pending_by_type(emp['id'])
    result = []
    for t in types:
        r = groups.get(t['id'], [])

        def s(reason: str, in_fy: bool) -> float:
            return sum((num(x['total']) for x in r if x['reason'] == reason and x['in_fy'] == in_fy), 0.0)

        opening = sum((num(x['total']) for x in r if not x['in_fy']), 0.0)
        accrued = s(Reason.ACCRUAL, True) + s(Reason.ALLOCATION, True)
        taken = -s(Reason.USAGE, True)
        cancelled = s(Reason.CANCELLATION, True)
        lapsed = -s(Reason.EXPIRY, True)
        carried = s(Reason.CARRY_FORWARD, True)
        adjustments = s(Reason.ADJUSTMENT, True)
        comp_off = s(Reason.COMP_OFF, True)
        encashed = -s(Reason.ENCASHMENT, True)
        balance = sum((num(x['total']) for x in r), 0.0)
        pending = pending_all.get(t['id'], 0.0)
        base = {'leaveTypeId': t['id'], 'code': t['code'], 'nameEn': t['name_en'], 'nameNe': t['name_ne'], 'paid': t['paid'],
                'encashable': t['encashable']}
        if t['event_days'] is not None:
            per_event = num(t['event_days'])
            ev = (LeaveRequest.objects.filter(employee_id=emp['id'], leave_type_id=t['id'], status__in=HELD,
                                              to_date__gte=day(as_of_ad) - timedelta(days=EVENT_GAP_DAYS))
                  .order_by('from_date').values('from_date', 'to_date').first())
            used = event_days_used(emp['id'], t['id'], iso(ev['from_date']), iso(ev['to_date'])) if ev else 0.0
            left = max(0.0, per_event - used)
            extra = f'; {js(used)} day(s) already requested for the current event, {js(left)} left' if used else ''
            result.append({**base, 'opening': 0, 'accrued': 0, 'taken': out(taken), 'cancelled': out(cancelled), 'lapsed': 0,
                           'carried': 0, 'adjustments': 0, 'compOff': 0, 'encashed': 0, 'pending': out(pending),
                           'balance': out(left), 'available': out(left), 'fiscalYear': fy.label,
                           'explanation': f'Up to {js(per_event)} paid day(s) per event (statutory){extra}; '
                                          f'{fixed(taken, 2)} day(s) taken this fiscal year'})
            continue
        result.append({**base, 'opening': out(opening), 'accrued': out(accrued), 'taken': out(taken), 'cancelled': out(cancelled),
                       'lapsed': out(lapsed), 'carried': out(carried), 'adjustments': out(adjustments), 'compOff': out(comp_off),
                       'encashed': out(encashed), 'pending': out(pending), 'balance': out(balance),
                       'available': out(balance - pending), 'fiscalYear': fy.label,
                       'explanation': f'Opening {fixed(opening, 2)} on 1 Shrawan {fy.start_year} + accrued {fixed(accrued, 2)} '
                                      f'+ comp-off {fixed(comp_off, 2)} + adjustments {fixed(adjustments, 2)} − taken {fixed(taken, 2)} '
                                      f'+ cancelled {fixed(cancelled, 2)} − lapsed {fixed(lapsed, 2)} − encashed {fixed(encashed, 2)} '
                                      f'= {fixed(balance, 2)} days' + (f' ({js(pending)} pending)' if pending else '')})
    return result


# ---------------------------------------------------------------- used by other modules

def credit_comp_off(employee_id: str, days: float, expires_on: str, *, ref_type: str, ref_id: str, note: str | None = None,
                    posted_by: str | None = None) -> str:
    """Credit substitute leave for holiday/weekly-off work or comp-off (Sec 42: expires 21 days after). Returns the ledger id."""
    from hajiri.core.clock import npt_today
    emp = get_employee_lite(employee_id)
    if not emp:
        raise NotFoundError('employee not found')
    t = leave_type_by_code(emp['company_id'], 'SUBSTITUTE')
    if not t:
        raise NotFoundError('SUBSTITUTE leave type is not configured')
    row = LeaveLedger.objects.create(employee_id=employee_id, leave_type_id=t['id'], effective_date=npt_today(),
                                     delta_days=dec(days), reason=Reason.COMP_OFF, ref_type=ref_type, ref_id=str(ref_id),
                                     expires_on=day(expires_on) if expires_on else None, note=note, posted_by_id=posted_by)
    return str(row.id)


def encashable_balances(employee_id: str) -> list[dict]:
    """Encashable balances at separation (Sec 49(2)): [{'leaveTypeId', 'code', 'days'}] with days > 0."""
    emp = get_employee_lite(employee_id)
    if not emp:
        raise NotFoundError('employee not found')
    result = []
    for t in (LeaveType.objects.for_company(emp['company_id']).filter(encashable=True).order_by('sort_order')
              .values('id', 'code')):
        b = ledger_balance(employee_id, str(t['id']))
        if b > 0:
            result.append({'leaveTypeId': str(t['id']), 'code': t['code'], 'days': out(b)})
    return result


def post_encashment(employee_id: str, leave_type_id: str, days: float, *, ref_type: str, ref_id: str,
                    posted_by: str | None = None) -> None:
    """Post the encashment row (payroll final settlement calls this once the run is locked)."""
    from hajiri.core.clock import npt_today
    LeaveLedger.objects.create(employee_id=employee_id, leave_type_id=leave_type_id, effective_date=npt_today(),
                               delta_days=dec(-days), reason=Reason.ENCASHMENT, ref_type=ref_type, ref_id=str(ref_id),
                               posted_by_id=posted_by)


def assert_no_overlap(employee_id: str, from_: str, to: str, exclude_id: str | None = None) -> None:
    qs = LeaveRequest.objects.filter(employee_id=employee_id, status__in=HELD, from_date__lte=day(to), to_date__gte=day(from_))
    if _not(qs, exclude_id).exists():
        raise ConflictError('an existing leave request already covers part of this range')


def has_locked_day(employee_id: str, from_: str, to: str) -> bool:
    """Any latest attendance day in [from, to] already locked by payroll? Leave decisions never change such days."""
    return latest_days().filter(employee_id=employee_id, local_date__range=(day(from_), day(to)),
                                locked_at__isnull=False).exists()

