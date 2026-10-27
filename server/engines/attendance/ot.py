"""Overtime (port of packages/attendance-engine/src/ot.ts), plan §10.4."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import DayContext, DayType, Flag, Number, OtPolicy, OtResult, Pair
from .timeutil import MIN_MS, local_time_ms, parse_hhmm, round_minutes


@dataclass(frozen=True, slots=True)
class OtInput:
    local_date: str
    # Minutes the employee is expected to work (shift span minus break).
    scheduled_min: Number
    worked_min: Number
    day_type: DayType
    pairs: tuple[Pair, ...]
    ctx: DayContext
    policy: OtPolicy


def no_ot(day_type: DayType, multiplier: Number, raw_minutes: Number = 0) -> OtResult:
    return OtResult('None', 0, raw_minutes, multiplier, day_type, 0, 0)


def compute_ot(inp: OtInput, flags: list[Flag]) -> OtResult:
    """Appends any OT flags to `flags`."""
    policy, ctx, day_type = inp.policy, inp.ctx, inp.day_type
    multiplier = (policy.multiplier_holiday if day_type == 'holiday'
                  else policy.multiplier_weekly_off if day_type == 'weekly_off' else policy.multiplier_working)

    if not ctx.ot_eligible:
        return no_ot(day_type, multiplier)

    # On an off day every worked minute is beyond schedule.
    raw = max(0, inp.worked_min - inp.scheduled_min) if day_type == 'working' else inp.worked_min
    if raw < policy.min_threshold_min:
        return no_ot(day_type, multiplier, raw)
    raw = round_minutes(raw, policy.rounding_min, 'down')

    if policy.require_preapproval:
        approved = 0 if ctx.ot_preapproved_min is None else ctx.ot_preapproved_min
        if approved <= 0:
            flags.append(Flag('OTPreapprovalMissing', raw_minutes=raw))
            return no_ot(day_type, multiplier, raw)
        raw = min(raw, approved)

    cap_week = max(0, policy.cap_week_min - ctx.ot_minutes_this_week)
    minutes = min(raw, policy.cap_day_min, cap_week)
    if raw > minutes:
        flags.append(Flag('OTCapExceeded', minutes=raw - minutes))

    night_minutes = 0
    if policy.night_window_enabled:
        n_start = local_time_ms(inp.local_date, policy.night_window_start)
        n_end = (local_time_ms(inp.local_date, policy.night_window_end, 1)
                 if parse_hhmm(policy.night_window_end) <= parse_hhmm(policy.night_window_start)
                 else local_time_ms(inp.local_date, policy.night_window_end))
        for p in inp.pairs:
            overlap = min(p.out_ms, n_end) - max(p.in_ms, n_start)
            if overlap > 0:
                night_minutes += math.floor(overlap / MIN_MS)

    return OtResult(
        'Detected' if minutes > 0 else 'None', minutes, raw, multiplier, day_type, night_minutes,
        policy.night_premium_rate if policy.night_window_enabled else 0,
    )
