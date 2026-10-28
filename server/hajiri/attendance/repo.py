"""
Inputs of the attendance engine, read from the database (port of apps/api/src/modules/attendance/repo.ts). Local dates
are 'YYYY-MM-DD' strings; instants are epoch milliseconds, as the engine expects. Every function takes an employee id
that the caller has already scoped to the active company (attendance.api, the punch and approval modules).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Coalesce

from engines.attendance import DAY_MIN, MIN_MS, Punch, Shift, ShortLeaveWindow, local_midnight_ms

from hajiri.core.settings_store import get_setting
from hajiri.db.models import (
    AttendanceDay, BsCalendar, Employee, EmployeeAssignment, Holiday, LeaveRequest, OtRequest, PunchEvent,
    RegularisationRequest, Roster, Shift as ShiftRow, ShiftAssignment,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
Reg = RegularisationRequest


def to_ms(dt: datetime) -> int:
    """Epoch ms of an instant, truncated like JS Date.getTime()."""
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int | float) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def iso(v) -> str:
    """'YYYY-MM-DD' for a date, a datetime or a string."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def _hm(t: time | None) -> str | None:
    return t.strftime('%H:%M') if t is not None else None


def latest_days():
    """attendance_day rows that are the newest computed version of their day (what the v_attendance_latest view
    shows); add .for_company(cid) and filters."""
    newer = AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                         version__gt=OuterRef('version'))
    return AttendanceDay.objects.exclude(Exists(newer))


def live_punches():
    """Punches no correction has replaced (no punch_event supersedes them)."""
    return PunchEvent.objects.exclude(Exists(PunchEvent.objects.filter(supersedes_id=OuterRef('id'))))


def get_employee(employee_id: str) -> dict | None:
    r = (Employee.objects.filter(pk=employee_id)
         .values('id', 'company_id', 'gender', 'ot_eligible', 'weekly_off_days', 'join_date', 'exit_date').first())
    if r:
        r['id'], r['company_id'] = str(r['id']), str(r['company_id'])
    return r


def get_branch_id(employee_id: str, local_date: str) -> str | None:
    b = (EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=date.fromisoformat(local_date))
         .values_list('branch_id', flat=True).first())
    return str(b) if b else None


SHIFT_FIELDS = ('id', 'code', 'start_time', 'end_time', 'grace_in_min', 'grace_out_min', 'early_in_window_min',
                'late_out_window_min', 'break_minutes', 'half_day_threshold_min', 'absent_threshold_min', 'pairing_mode',
                'rounding_min', 'rounding_direction')


def to_shift(r: dict) -> Shift:
    """The engine's Shift, carrying the row id (it is part of every stored inputs_hash)."""
    start, end = r['start_time'], r['end_time']
    return Shift(
        id=str(r['id']), code=r['code'], start=_hm(start) if isinstance(start, time) else str(start)[:5],
        end=_hm(end) if isinstance(end, time) else str(end)[:5], grace_in_min=r['grace_in_min'],
        grace_out_min=r['grace_out_min'], early_in_window_min=r['early_in_window_min'],
        late_out_window_min=r['late_out_window_min'], break_minutes=r['break_minutes'],
        half_day_threshold_min=r['half_day_threshold_min'], absent_threshold_min=r['absent_threshold_min'],
        pairing_mode=r['pairing_mode'], rounding_min=r['rounding_min'], rounding_direction=r['rounding_direction'])


def get_shift(shift_id: str) -> Shift | None:
    s = ShiftRow.objects.filter(pk=shift_id).values(*SHIFT_FIELDS).first()
    return to_shift(s) if s else None


def resolve_shift(employee_id: str, local_date: str) -> Shift | None:
    """approved shift_change regularisation > roster row (even if its shift is NULL) > shift_assignment > none."""
    d = date.fromisoformat(local_date)
    reg = (Reg.objects.filter(employee_id=employee_id, local_date=d, kind=Reg.Kind.SHIFT_CHANGE, status=Reg.Status.APPROVED)
           .order_by('-decided_at').values('shift_id')[:1])
    if reg:
        shift_id = reg[0]['shift_id']
    else:
        roster = Roster.objects.filter(employee_id=employee_id, local_date=d).values('shift_id')[:1]
        if roster:
            shift_id = roster[0]['shift_id']
        else:
            shift_id = (ShiftAssignment.objects.filter(employee_id=employee_id, valid__contains=d)
                        .values_list('shift_id', flat=True).first())
    return get_shift(str(shift_id)) if shift_id else None


def _punch(p: dict) -> Punch:
    return Punch(id=str(p['id']), at=to_ms(p['punched_at']), direction=p['direction'], verify_mode=p['verify_mode'],
                 source=p['source'])


PUNCH_FIELDS = ('id', 'punched_at', 'direction', 'verify_mode', 'source')


def get_punches_around(employee_id: str, local_date: str) -> list[Punch]:
    """Punches that could fall in any window of this date's shift: from the previous local day to two days after."""
    midnight = local_midnight_ms(local_date)
    found = (live_punches().filter(employee_id=employee_id, punched_at__gte=from_ms(midnight - DAY_MIN * MIN_MS),
                                   punched_at__lt=from_ms(midnight + 3 * DAY_MIN * MIN_MS))
             .order_by('punched_at', 'id').values(*PUNCH_FIELDS))
    return [_punch(p) for p in found]


def is_holiday(company_id: str, branch_id: str | None, local_date: str, gender: str | None) -> bool:
    return (Holiday.objects.for_company(company_id)
            .filter(Q(branch__isnull=True) | Q(branch_id=branch_id), local_date=local_date, optional=False)
            .filter(Q(gender_scope=Holiday.GenderScope.ALL) | Q(gender_scope=gender)).exists())


@dataclass(frozen=True)
class LeaveOnDay:
    cover: str
    paid: bool
    leave_type_id: str | None


def approved_leave(employee_id: str, local_date: str) -> LeaveOnDay:
    """Approved leave covering the date: full day, or a half."""
    d = date.fromisoformat(local_date)
    r = (LeaveRequest.objects.filter(employee_id=employee_id, status=LeaveRequest.Status.APPROVED, from_date__lte=d,
                                     to_date__gte=d, from_time__isnull=True)
         .order_by('-decided_at').values('half_day', 'leave_type__paid', 'leave_type_id').first())
    if r:
        return LeaveOnDay(r['half_day'] or 'full', r['leave_type__paid'], str(r['leave_type_id']))
    # An approved half-day regularisation is a paid half day; the request does not say which half, so the afternoon
    # is assumed (the employee worked the morning).
    if Reg.objects.filter(employee_id=employee_id, local_date=d, kind=Reg.Kind.HALF_DAY, status=Reg.Status.APPROVED).exists():
        return LeaveOnDay('second_half', True, None)
    return LeaveOnDay('none', True, None)


def approved_short_leave(employee_id: str, local_date: str) -> list[ShortLeaveWindow]:
    """Approved hourly leave windows on the date ('HH:MM'); empty when there is none."""
    found = (LeaveRequest.objects.filter(employee_id=employee_id, status=LeaveRequest.Status.APPROVED, from_date=local_date,
                                         from_time__isnull=False)
             .order_by('from_time').values('from_time', 'to_time'))
    return [ShortLeaveWindow(from_=_hm(r['from_time']), to=_hm(r['to_time'])) for r in found]


def approved_wfh(employee_id: str, local_date: str) -> bool:
    return Reg.objects.filter(employee_id=employee_id, local_date=local_date, kind__in=(Reg.Kind.WFH, Reg.Kind.ON_DUTY),
                              status=Reg.Status.APPROVED).exists()


def approved_ot_minutes(employee_id: str, local_date: str) -> int | None:
    r = (OtRequest.objects.filter(employee_id=employee_id, local_date=local_date, status=OtRequest.Status.APPROVED)
         .order_by('-decided_at').values('approved_min').first())
    return r['approved_min'] if r else None


def week_start(d: str) -> str:
    """The Sunday that starts d's week (Sunday..Saturday)."""
    return add_days(d, -((date.fromisoformat(d).weekday() + 1) % 7))


def ot_minutes_this_week(employee_id: str, local_date: str) -> int:
    """OT minutes already recorded this week (Sunday..Saturday) on other days, from the latest version of each day."""
    start = week_start(local_date)
    total = (latest_days().filter(employee_id=employee_id, local_date__gte=start, local_date__lt=add_days(start, 7))
             .exclude(local_date=local_date).aggregate(total=Coalesce(Sum('ot_min'), 0))['total'])
    return int(total)


def latest_version(employee_id: str, local_date: str) -> dict | None:
    return (AttendanceDay.objects.filter(employee_id=employee_id, local_date=local_date).order_by('-version')
            .values('id', 'version', 'inputs_hash', 'locked_at').first())


def bs_date_for(local_date: str) -> str | None:
    return BsCalendar.objects.filter(ad_date=local_date).values_list('bs_date', flat=True).first()


def attendance_policy(company_id: str) -> dict:
    return get_setting(company_id, 'attendance.policy') or {}


# ---------------------------------------------------------------- bulk inputs for previews (same rules as above)

@dataclass
class DayInputs:
    shift: Shift | None
    punches: list[Punch]
    holiday: bool
    leave: str
    wfh: bool
    ot_preapproved_min: int | None
    ot_minutes_this_week: int
    stored: dict | None
    short_leave: list[ShortLeaveWindow]


def _in_range(rng, d: str) -> bool:
    """A daterange value (psycopg Range or Postgres text '[2026-01-01,2026-02-01)') contains d."""
    if not isinstance(rng, str):
        if rng.isempty:
            return False
        lo = iso(rng.lower) if rng.lower is not None else None
        hi = iso(rng.upper) if rng.upper is not None else None
        lo_ok = lo is None or (d >= lo if rng.lower_inc else d > lo)
        hi_ok = hi is None or (d <= hi if rng.upper_inc else d < hi)
        return lo_ok and hi_ok
    m = re.fullmatch(r'[\[(]([^,]*),([^)\]]*)[)\]]', str(rng))
    if not m:
        return False
    return (not m[1] or d >= m[1]) and (not m[2] or d < m[2])


def load_range_inputs(company_id: str, employees: list[dict], dates: list[str]) -> dict[str, DayInputs]:
    """Everything compute_day needs for every (employee, date) in a range, in a fixed number of queries."""
    ids = [str(e['id']) for e in employees]
    lo, hi = dates[0], dates[-1]
    span = DateRange(date.fromisoformat(lo), date.fromisoformat(hi), '[]')
    mine = {'employee_id__in': ids}
    assign = list(EmployeeAssignment.objects.filter(**mine, valid__overlap=span).values('employee_id', 'branch_id', 'valid'))
    regs = list(Reg.objects.filter(**mine, status=Reg.Status.APPROVED, local_date__range=(lo, hi),
                                   kind__in=(Reg.Kind.SHIFT_CHANGE, Reg.Kind.HALF_DAY, Reg.Kind.WFH, Reg.Kind.ON_DUTY))
                .order_by(F('decided_at').desc(nulls_last=True)).values('employee_id', 'local_date', 'kind', 'shift_id'))
    roster = list(Roster.objects.filter(**mine, local_date__range=(lo, hi)).values('employee_id', 'local_date', 'shift_id'))
    sa = list(ShiftAssignment.objects.filter(**mine, valid__overlap=span).values('employee_id', 'shift_id', 'valid'))
    punches = (live_punches().filter(**mine, punched_at__gte=from_ms(local_midnight_ms(lo) - DAY_MIN * MIN_MS),
                                     punched_at__lt=from_ms(local_midnight_ms(hi) + 3 * DAY_MIN * MIN_MS))
               .order_by('punched_at', 'id').values('employee_id', *PUNCH_FIELDS))
    leave = list(LeaveRequest.objects.filter(**mine, status=LeaveRequest.Status.APPROVED, to_date__gte=lo, from_date__lte=hi)
                 .order_by(F('decided_at').desc(nulls_last=True))
                 .values('employee_id', 'from_date', 'to_date', 'half_day', 'from_time', 'to_time'))
    ot = list(OtRequest.objects.filter(**mine, status=OtRequest.Status.APPROVED, local_date__range=(lo, hi))
              .order_by(F('decided_at').desc(nulls_last=True)).values('employee_id', 'local_date', 'approved_min'))
    stored = list(latest_days().filter(**mine, local_date__range=(week_start(lo), add_days(hi, 6)))
                  .values('employee_id', 'local_date', 'status', 'late_min', 'ot_min'))
    holidays = list(Holiday.objects.for_company(company_id).filter(optional=False, local_date__range=(lo, hi))
                    .values('local_date', 'branch_id', 'gender_scope'))
    shift_ids = {str(x['shift_id']) for x in regs + roster + sa if x.get('shift_id')}
    shifts = {str(r['id']): to_shift(r) for r in ShiftRow.objects.filter(pk__in=shift_ids).values(*SHIFT_FIELDS)}

    def key(e, d):
        return f'{e}|{d}'

    def first(items, k):
        out = {}
        for r in items:
            out.setdefault(k(r), r)
        return out

    shift_reg = first([r for r in regs if r['kind'] == Reg.Kind.SHIFT_CHANGE], lambda r: key(r['employee_id'], iso(r['local_date'])))
    half_reg = {key(r['employee_id'], iso(r['local_date'])) for r in regs if r['kind'] == Reg.Kind.HALF_DAY}
    wfh_reg = {key(r['employee_id'], iso(r['local_date'])) for r in regs if r['kind'] in (Reg.Kind.WFH, Reg.Kind.ON_DUTY)}
    roster_by = first(roster, lambda r: key(r['employee_id'], iso(r['local_date'])))
    ot_by = first(ot, lambda r: key(r['employee_id'], iso(r['local_date'])))
    stored_by = {key(r['employee_id'], iso(r['local_date'])): r for r in stored}
    punches_by: dict[str, list[Punch]] = {}
    for p in punches:
        punches_by.setdefault(str(p['employee_id']), []).append(_punch(p))

    out: dict[str, DayInputs] = {}
    for e in employees:
        eid = str(e['id'])
        allp = punches_by.get(eid, [])
        for d in dates:
            k = key(eid, d)
            if k in shift_reg:
                shift_id = shift_reg[k]['shift_id']
            elif k in roster_by:
                shift_id = roster_by[k]['shift_id']
            else:
                shift_id = next((r['shift_id'] for r in sa if str(r['employee_id']) == eid and _in_range(r['valid'], d)), None)
            branch_id = next((r['branch_id'] for r in assign if str(r['employee_id']) == eid and _in_range(r['valid'], d)), None)
            lv = next((r for r in leave if str(r['employee_id']) == eid and r['from_time'] is None
                       and iso(r['from_date']) <= d <= iso(r['to_date'])), None)
            short = sorted((ShortLeaveWindow(from_=_hm(r['from_time']), to=_hm(r['to_time'])) for r in leave
                            if str(r['employee_id']) == eid and r['from_time'] is not None and r['to_time'] is not None
                            and iso(r['from_date']) == d), key=lambda w: w.from_)
            cover = (lv['half_day'] or 'full') if lv else ('second_half' if k in half_reg else 'none')
            ws = week_start(d)
            week = 0
            for i in range(7):
                wd = add_days(ws, i)
                if wd != d:
                    week += (stored_by.get(key(eid, wd)) or {}).get('ot_min') or 0
            lo_ms = local_midnight_ms(d) - DAY_MIN * MIN_MS
            hi_ms = local_midnight_ms(d) + 3 * DAY_MIN * MIN_MS
            out[k] = DayInputs(
                shift=shifts.get(str(shift_id)) if shift_id else None,
                punches=[p for p in allp if lo_ms <= p.at < hi_ms],
                holiday=any(iso(h['local_date']) == d and (h['branch_id'] is None or h['branch_id'] == branch_id)
                            and (h['gender_scope'] == Holiday.GenderScope.ALL or h['gender_scope'] == e.get('gender'))
                            for h in holidays),
                leave=cover, wfh=k in wfh_reg, ot_preapproved_min=(ot_by.get(k) or {}).get('approved_min'),
                ot_minutes_this_week=week, stored=stored_by.get(k), short_leave=short)
    return out
