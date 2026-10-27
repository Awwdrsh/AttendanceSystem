"""The attendance day (port of packages/attendance-engine/src/compute-day.ts)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .hash import canonical_json, sha256_hex
from .models import (
    AttendanceDay, AttendanceStatus, ComputeDayInput, DayType, Flag, Number, Pair, Punch, ShortLeaveWindow, Window,
)
from .ot import OtInput, compute_ot, no_ot
from .pairing import pair_punches, sort_punches
from .timeutil import DAY_MIN, MIN_MS, local_midnight_ms, minutes_between, parse_hhmm, round_minutes


def compute_day(inp: ComputeDayInput) -> AttendanceDay:
    """Pure function: (employee, date, shift, punches, context, policy) -> AttendanceDay.
    No I/O, no clock. Same inputs always give the same output and the same inputs_hash (plan §10.1-10.3)."""
    employee_id, local_date, shift, ctx, policy = inp.employee_id, inp.local_date, inp.shift, inp.ctx, inp.policy
    flags: list[Flag] = []

    if not shift:
        return _finalize(dict(
            employee_id=employee_id, local_date=local_date, shift_code=None, status='OffShift', window=None,
            scheduled_min=0, first_in_ms=None, last_out_ms=None, worked_min=0, break_min=0, late_min=0, early_min=0,
            ot=no_ot('working', policy.ot.multiplier_working), flags=(), pairs=(), punch_ids=(),
        ), [], inp)

    midnight = local_midnight_ms(local_date)
    start_min = parse_hhmm(shift.start)
    end_min_raw = parse_hhmm(shift.end)
    crosses_midnight = end_min_raw <= start_min
    start_ms = midnight + start_min * MIN_MS
    end_ms = midnight + (end_min_raw + (DAY_MIN if crosses_midnight else 0)) * MIN_MS
    scheduled_min = (end_ms - start_ms) // MIN_MS  # exact: both are whole minutes
    # An overtime punch-out must still belong to this day: the late-out window always reaches past the daily OT cap,
    # but never so far that it would overlap the next day's early-in window.
    ot_reach = min(policy.ot.cap_day_min + 120, DAY_MIN - scheduled_min - shift.early_in_window_min - 1)
    late_out_min = max(shift.late_out_window_min, ot_reach)
    window = Window(start_ms - shift.early_in_window_min * MIN_MS, end_ms + late_out_min * MIN_MS)

    # Only punches inside the window belong to this day; everything else is off-shift for this date.
    punches = sort_punches(p for p in inp.punches if window.start_ms <= p.at <= window.end_ms)

    result = pair_punches(punches, shift.pairing_mode)
    pairs, unpaired = result.pairs, result.unpaired

    gross: Number = 0
    break_min: Number = 0
    if pairs:
        for p in pairs:
            gross += minutes_between(p.in_ms, p.out_ms)
        if len(pairs) == 1:
            break_min = min(shift.break_minutes, gross)
        else:
            for i in range(1, len(pairs)):
                break_min += minutes_between(pairs[i - 1].out_ms, pairs[i].in_ms)
    # With one pair the fixed break is deducted; with several pairs the gaps are already excluded from gross.
    net = gross - break_min if len(pairs) == 1 else gross
    worked_min = round_minutes(net, shift.rounding_min, shift.rounding_direction)
    # Worked minutes exclude the break, so overtime compares them with the scheduled span minus the break.
    expected_work_min = max(0, scheduled_min - shift.break_minutes)

    first_in_ms = pairs[0].in_ms if pairs else punches[0].at if punches else None
    last_out_ms = pairs[-1].out_ms if pairs else None

    day_type: DayType = 'holiday' if ctx.holiday else 'weekly_off' if ctx.weekly_off else 'working'
    status: AttendanceStatus
    late_min: Number = 0
    early_min: Number = 0
    ot = no_ot(day_type, policy.ot.multiplier_working)

    def run_ot() -> None:
        nonlocal ot
        ot = compute_ot(OtInput(local_date, expected_work_min, worked_min, day_type, pairs, ctx, policy.ot), flags)

    if day_type != 'working':
        # Holiday/WeeklyOff > everything. Never Absent on an off day (plan §10.7).
        status = 'Holiday' if ctx.holiday else 'WeeklyOff'
        if punches:
            flags.append(Flag('HolidayWork', worked_min=worked_min))
            run_ot()
    elif ctx.leave == 'full':
        status = 'OnLeave'
    else:
        # Hourly leave moves the expected arrival past a window that starts the shift and the expected departure
        # before a window that ends it; leave minutes not worked count toward the half-day and absent thresholds.
        leave = _short_leave_windows(ctx.short_leave, midnight, start_min, crosses_midnight, start_ms, end_ms)
        expected_in = start_ms
        for w in leave:
            if w[0] <= expected_in + shift.grace_in_min * MIN_MS and w[1] > expected_in:
                expected_in = w[1]
        expected_out = end_ms
        for w in reversed(leave):
            if w[1] >= expected_out - shift.grace_out_min * MIN_MS and w[0] < expected_out:
                expected_out = w[0]
        # Summed in the TS order, (sum + window) - overlap: overlap minutes are fractional when punches carry seconds.
        leave_min: Number = 0
        for w in leave:
            overlap: Number = 0
            for p in pairs:
                overlap += _overlap_min(w[0], w[1], p.in_ms, p.out_ms)
            leave_min = leave_min + minutes_between(w[0], w[1]) - overlap
        credited_min = worked_min + max(0, leave_min)

        if first_in_ms is not None:
            late_min = max(0, minutes_between(expected_in + shift.grace_in_min * MIN_MS, first_in_ms))
        if last_out_ms is not None:
            early_min = max(0, minutes_between(last_out_ms, expected_out - shift.grace_out_min * MIN_MS))

        if not punches:
            status = 'Absent'
        elif not pairs:
            status = 'Incomplete'
        elif shift.absent_threshold_min > 0 and credited_min < shift.absent_threshold_min:
            status = 'Absent'
        elif shift.half_day_threshold_min > 0 and credited_min < shift.half_day_threshold_min:
            status = 'HalfDay'
        else:
            status = 'Present'
        if leave:
            flags.append(Flag('ShortLeave', minutes=sum((minutes_between(w[0], w[1]) for w in leave), 0)))

        if ctx.leave in ('first_half', 'second_half') and status == 'Present':
            status = 'HalfDayLeave'
        if ctx.wfh and status in ('Absent', 'Incomplete'):
            status = 'WFH'

        if late_min > 0:
            flags.append(Flag('Late', minutes=late_min))
        if early_min > 0:
            flags.append(Flag('EarlyOut', minutes=early_min))
        run_ot()

    if day_type == 'working' and ctx.leave in ('first_half', 'second_half'):
        flags.append(Flag('HalfDayLeave', half=ctx.leave))
    if any(p.source == 'auto_clock_out' for p in punches):
        flags.append(Flag('AutoClockOut'))
    for p in unpaired:
        flags.append(Flag('UnpairedPunch', punch_id=p.id))
    if unpaired and punches and unpaired[-1].id == punches[-1].id and unpaired[-1].direction != 'out':
        flags.append(Flag('MissingOut'))

    return _finalize(dict(
        employee_id=employee_id, local_date=local_date, shift_code=shift.code, status=status, window=window,
        scheduled_min=scheduled_min, first_in_ms=first_in_ms, last_out_ms=last_out_ms, worked_min=worked_min,
        break_min=break_min, late_min=late_min, early_min=early_min, ot=ot, flags=tuple(flags), pairs=pairs,
        punch_ids=tuple(p.id for p in punches),
    ), punches, inp)


def _overlap_min(a0: Number, a1: Number, b0: Number, b1: Number) -> float:
    return max(0, min(a1, b1) - max(a0, b0)) / MIN_MS


def _short_leave_windows(windows: Optional[Iterable[ShortLeaveWindow]], midnight: int, start_min: int,
                         crosses_midnight: bool, start_ms: int, end_ms: int) -> list[list[int]]:
    """Hourly-leave windows as [from_ms, to_ms] instants, clipped to the scheduled span, sorted and merged. On a shift
    that crosses midnight a time before the shift start belongs to the next calendar day."""
    windows = list(windows or ())
    if not windows:
        return []

    def at(hhmm: str) -> int:
        m = parse_hhmm(hhmm)
        return midnight + (m + (DAY_MIN if crosses_midnight and m < start_min else 0)) * MIN_MS

    out: list[list[int]] = []
    for w in windows:
        from_ms = max(start_ms, at(w.from_))
        to_ms = min(end_ms, at(w.to))
        if to_ms <= from_ms and crosses_midnight:
            to_ms = min(end_ms, at(w.to) + DAY_MIN * MIN_MS)
        if to_ms > from_ms:
            out.append([from_ms, to_ms])
    out.sort(key=lambda w: w[0])  # stable, like the TS comparator a.fromMs - b.fromMs
    merged: list[list[int]] = []
    for w in out:
        if merged and w[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], w[1])
        else:
            merged.append(list(w))
    return merged


def _finalize(day: dict, punches: list[Punch], inp: ComputeDayInput) -> AttendanceDay:
    inputs_hash = sha256_hex(canonical_json({
        'employeeId': inp.employee_id,
        'localDate': inp.local_date,
        'shift': None if inp.shift is None else inp.shift.to_dict(),
        'ctx': inp.ctx.to_dict(),
        'policy': inp.policy.to_dict(),
        'punches': [{'id': p.id, 'at': p.at, 'direction': p.direction, 'verifyMode': p.verify_mode, 'source': p.source}
                    for p in punches],
    }))
    return AttendanceDay(**day, inputs_hash=inputs_hash)
