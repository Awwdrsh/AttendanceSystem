"""
Attendance recompute (port of apps/api/src/modules/attendance/service.ts). Days are recomputed from immutable punches
by the pure engine; a new version is written only when the inputs hash changed, and locked days are never rewritten.
Other modules call enqueue_recompute when something that affects a day changes (an approval, a roster edit, a punch)
and recompute_queued_for after their transaction commits so the corrected day shows at once.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta

from django.db import transaction

from engines.attendance import (
    DEFAULT_POLICY, NPT_OFFSET_MIN, MIN_MS, AttendanceDay, AttendancePolicy, ComputeDayInput, DayContext, Flag,
    compute_day, local_date_of,
)
from engines.bs_calendar import weekday_of

from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, NotFoundError

from . import repo

log = logging.getLogger('hajiri.attendance')

MAX_RANGE_DAYS = 3660
INLINE_RECOMPUTE_MAX = 62


@dataclass(frozen=True)
class RecomputeResult:
    day: AttendanceDay
    version: int
    changed: bool
    locked: bool

    def to_dict(self) -> dict:
        return {'day': self.day.to_dict(), 'version': self.version, 'changed': self.changed, 'locked': self.locked}


def policy_from_settings(v: dict) -> AttendancePolicy:
    def n(k, d):
        x = v.get(k)
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else d

    def b(k, d):
        x = v.get(k)
        return x if isinstance(x, bool) else d

    ot = DEFAULT_POLICY.ot
    return AttendancePolicy(
        version=v['version'] if isinstance(v.get('version'), str) else DEFAULT_POLICY.version,
        holiday_work=v.get('holidayWork') or DEFAULT_POLICY.holiday_work,
        ot=replace(ot, min_threshold_min=n('otMinThresholdMin', ot.min_threshold_min),
                   rounding_min=n('otRoundingMin', ot.rounding_min),
                   require_preapproval=b('requirePreapproval', ot.require_preapproval),
                   night_window_enabled=b('nightWindowEnabled', False), night_premium_rate=n('nightPremiumRate', 0),
                   # Statutory caps (Labour Act Sec 30(1)) are never raised from settings.
                   cap_day_min=min(240, n('otCapDayMin', 240)), cap_week_min=min(1440, n('otCapWeekMin', 1440))))


def recompute_day(employee_id: str, local_date: str) -> RecomputeResult:
    """Recompute one (employee, date) under an advisory lock, in its own transaction (a savepoint when nested)."""
    with transaction.atomic():
        execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f'recompute:{employee_id}:{local_date}'])
        return _recompute_in_tx(str(employee_id), str(local_date))


def _recompute_in_tx(employee_id: str, local_date: str) -> RecomputeResult:
    employee = repo.get_employee(employee_id)
    if not employee:
        raise NotFoundError(f'employee {employee_id} not found')
    branch_id = repo.get_branch_id(employee_id, local_date)
    shift_resolved = repo.resolve_shift(employee_id, local_date)
    punches = repo.get_punches_around(employee_id, local_date)
    leave = repo.approved_leave(employee_id, local_date)
    wfh = repo.approved_wfh(employee_id, local_date)
    ot_pre = repo.approved_ot_minutes(employee_id, local_date)
    ot_week = repo.ot_minutes_this_week(employee_id, local_date)
    policy_raw = repo.attendance_policy(employee['company_id'])
    short_leave = repo.approved_short_leave(employee_id, local_date)
    holiday = repo.is_holiday(employee['company_id'], branch_id, local_date, employee['gender'])

    # Before joining or after exit there is no shift to work.
    join, exit_ = employee['join_date'], employee['exit_date']
    employed = (not join or local_date >= repo.iso(join)) and (not exit_ or local_date <= repo.iso(exit_))
    shift = shift_resolved if employed else None

    ctx = DayContext(
        holiday=holiday, weekly_off=weekday_of(local_date) in (employee['weekly_off_days'] or []), leave=leave.cover,
        wfh=wfh, ot_eligible=employee['ot_eligible'], ot_preapproved_min=ot_pre, ot_minutes_this_week=ot_week,
        # Only present when there is hourly leave, so days without it keep their existing inputs hash.
        short_leave=tuple(short_leave) if short_leave else None)
    policy = policy_from_settings(policy_raw)
    day = compute_day(ComputeDayInput(employee_id=employee_id, local_date=local_date, shift=shift,
                                      punches=tuple(punches), ctx=ctx, policy=policy))
    flags = day.flags + (Flag(kind='UnpaidLeave'),) if leave.cover != 'none' and not leave.paid else day.flags

    latest = repo.latest_version(employee_id, local_date)
    if latest and latest['locked_at']:
        return RecomputeResult(day, latest['version'], False, True)
    # A payroll period whose attendance is locked is frozen even for days that had no row yet.
    if not latest and _period_locked(employee['company_id'], local_date):
        return RecomputeResult(day, 0, False, True)
    if latest and latest['inputs_hash'] == day.inputs_hash:
        return RecomputeResult(day, latest['version'], False, False)

    version = (latest['version'] if latest else 0) + 1
    ins = one("""
        INSERT INTO attendance_day (employee_id, local_date, bs_date, version, shift_id, status, first_in, last_out,
          worked_min, break_min, late_min, early_min, ot_min, ot, flags, punch_ids, inputs_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s) RETURNING id""",
        [employee_id, local_date, repo.bs_date_for(local_date), version, shift.id if shift else None, day.status,
         None if day.first_in_ms is None else repo.from_ms(day.first_in_ms),
         None if day.last_out_ms is None else repo.from_ms(day.last_out_ms),
         day.worked_min, day.break_min, day.late_min, day.early_min, day.ot.minutes, json.dumps(day.ot.to_dict()),
         json.dumps([f.to_dict() for f in flags]), list(day.punch_ids), day.inputs_hash])

    # OT detected -> slip. An already approved or posted slip is never overwritten by a recompute.
    if day.ot.minutes > 0:
        execute("""
            INSERT INTO ot_slip (employee_id, local_date, attendance_day_id, detected_min, multiplier, day_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (employee_id, local_date) DO UPDATE SET attendance_day_id = EXCLUDED.attendance_day_id,
              detected_min = EXCLUDED.detected_min, multiplier = EXCLUDED.multiplier, day_type = EXCLUDED.day_type
            WHERE ot_slip.status = 'detected'""",
            [employee_id, local_date, ins['id'], day.ot.minutes, day.ot.multiplier, day.ot.day_type])
    else:
        execute("DELETE FROM ot_slip WHERE employee_id = %s AND local_date = %s AND status = 'detected'", [employee_id, local_date])
    return RecomputeResult(replace(day, flags=flags), version, True, False)


def enqueue_recompute(items, reason: str) -> None:
    """Queue (employee, date) pairs for the worker. Idempotent; one statement per 5,000 pairs.
    `items` holds (employee_id, local_date) tuples or {'employeeId', 'localDate'} dicts."""
    pairs = [(str(x['employeeId']), str(x['localDate'])) if isinstance(x, dict) else (str(x[0]), str(x[1])) for x in items]
    for i in range(0, len(pairs), 5000):
        chunk = pairs[i:i + 5000]
        execute("""INSERT INTO recompute_queue (employee_id, local_date, reason)
                   SELECT e, d, %s FROM unnest(%s::uuid[], %s::date[]) AS t(e, d)
                   ON CONFLICT (employee_id, local_date) DO UPDATE SET enqueued_at = now(), reason = EXCLUDED.reason""",
                [reason, [p[0] for p in chunk], [p[1] for p in chunk]])


def date_range(from_: str | date, to: str | date) -> list[str]:
    """Every date in [from, to] inclusive, as 'YYYY-MM-DD'; at most about ten years."""
    start, end = date.fromisoformat(str(from_)), date.fromisoformat(str(to))
    if (end - start).days >= MAX_RANGE_DAYS:
        raise BadRequestError(f'date range is too long (max {MAX_RANGE_DAYS} days)')
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def list_days(employee_id: str, from_: str, to: str) -> list[dict]:
    """Latest version of each day in [from, to]."""
    return rows("""SELECT id, employee_id, local_date, bs_date, version, status, shift_id, first_in, last_out,
                          worked_min, break_min, late_min, early_min, ot_min, ot, flags, computed_at, locked_at
                     FROM v_attendance_latest WHERE employee_id = %s AND local_date BETWEEN %s AND %s ORDER BY local_date""",
                [employee_id, from_, to])


def _period_locked(company_id: str, local_date: str) -> bool:
    return one("""SELECT 1 AS x FROM payroll_period WHERE company_id = %s AND attendance_locked_at IS NOT NULL
                   AND %s::date BETWEEN start_ad AND end_ad LIMIT 1""", [company_id, local_date]) is not None


def is_attendance_locked(employee_id: str, local_date: str) -> bool:
    """Frozen by payroll: the day's latest version is locked, or its payroll period's attendance is."""
    return one("""SELECT 1 AS x FROM v_attendance_latest WHERE employee_id = %(e)s AND local_date = %(d)s AND locked_at IS NOT NULL
                  UNION ALL
                  SELECT 1 FROM payroll_period p JOIN employee e ON e.company_id = p.company_id
                   WHERE e.id = %(e)s AND p.attendance_locked_at IS NOT NULL AND %(d)s::date BETWEEN p.start_ad AND p.end_ad
                  LIMIT 1""", {'e': employee_id, 'd': local_date}) is not None


def dates_for_punch(at_ms: int) -> list[str]:
    """Its own date and, for a morning punch, the previous date (a night shift or a late punch-out belongs to the day
    the shift started)."""
    d = local_date_of(at_ms)
    hour = ((at_ms + NPT_OFFSET_MIN * MIN_MS) // 3_600_000) % 24
    if hour >= 12:
        return [d]
    return [repo.add_days(d, -1), d]


def recompute_for_punch(employee_id: str, at_ms: int, punch_id: str) -> RecomputeResult:
    """Recompute every day a punch can affect; returns the day that now contains it (or the punch's own date)."""
    results = [recompute_day(employee_id, d) for d in dates_for_punch(at_ms)]
    return next((r for r in results if str(punch_id) in r.day.punch_ids), results[-1])


def recompute_queued_for(employee_id: str) -> int:
    """Process this employee's queued recomputes now (after an approval commits). Skipped when more than
    INLINE_RECOMPUTE_MAX days are queued; a failure stays queued for the worker."""
    q = rows("""SELECT local_date::text AS d FROM recompute_queue WHERE employee_id = %s AND enqueued_at <= now()
                 ORDER BY local_date LIMIT %s""", [employee_id, INLINE_RECOMPUTE_MAX + 1])
    if not q or len(q) > INLINE_RECOMPUTE_MAX:
        return 0
    n = 0
    for r in q:
        try:
            with transaction.atomic():
                recompute_day(employee_id, r['d'])
                execute('DELETE FROM recompute_queue WHERE employee_id = %s AND local_date = %s', [employee_id, r['d']])
            n += 1
        except Exception:  # noqa: BLE001 - left queued; the worker retries with backoff
            log.warning('inline recompute failed for %s %s; left queued', employee_id, r['d'], exc_info=True)
    return n


def lock_attendance(company_id: str, from_: str, to: str) -> int:
    """Lock every latest attendance_day in a period for a company (payroll pre-flight). Returns rows locked."""
    return execute("""UPDATE attendance_day a SET locked_at = now()
                        FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
                       WHERE a.id = v.id AND e.company_id = %s AND v.local_date BETWEEN %s AND %s AND a.locked_at IS NULL""",
                   [company_id, from_, to])
