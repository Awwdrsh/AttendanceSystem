"""Port of packages/attendance-engine/src/properties.test.ts (hypothesis in place of fast-check).
Invariants from plan §10.3 / §15.2."""
import math
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from engines.attendance import (
    DAY_MIN, DEFAULT_POLICY, MIN_MS, ComputeDayInput, DayContext, Punch, Shift, compute_day, local_midnight_ms,
)

D = '2026-09-21'
MIDNIGHT = local_midnight_ms(D)


def hhmm(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def js_mod(a: int, b: int) -> int:
    """JS %: the remainder takes the sign of the dividend."""
    return int(math.fmod(a, b))


shifts = st.builds(
    Shift,
    code=st.just('S'),
    start=st.integers(0, DAY_MIN - 1).map(hhmm),
    end=st.integers(0, DAY_MIN - 1).map(hhmm),
    grace_in_min=st.integers(0, 60),
    grace_out_min=st.integers(0, 60),
    early_in_window_min=st.integers(0, 180),
    late_out_window_min=st.integers(0, 360),
    break_minutes=st.integers(0, 90),
    half_day_threshold_min=st.integers(0, 300),
    absent_threshold_min=st.integers(0, 180),
    pairing_mode=st.sampled_from(['alternating', 'strict_log_type', 'first_last']),
    rounding_min=st.sampled_from([0, 5, 15]),
    rounding_direction=st.sampled_from(['nearest', 'down', 'up']),
)


def punches_with(prefix: str):
    return st.builds(
        Punch,
        id=st.uuids().map(lambda u: f'{prefix}{u}'),
        # Anywhere from one day before to two days after local midnight, with second-level jitter.
        at=st.integers(-DAY_MIN * 60, 2 * DAY_MIN * 60).map(lambda s: MIDNIGHT + s * 1000),
        direction=st.sampled_from(['in', 'out', 'unknown']),
        verify_mode=st.sampled_from(['fingerprint', 'face', 'card']),
        source=st.just('zkteco'),
    )


contexts = st.builds(
    DayContext,
    holiday=st.booleans(),
    weekly_off=st.booleans(),
    leave=st.sampled_from(['none', 'full', 'first_half', 'second_half']),
    wfh=st.booleans(),
    ot_eligible=st.booleans(),
    ot_preapproved_min=st.none() | st.integers(0, 600),
    ot_minutes_this_week=st.integers(0, 1440),
)

punch_lists = st.lists(punches_with('p'), max_size=12)

CTX_WORKING = DayContext(holiday=False, weekly_off=False, leave='none', wfh=False, ot_eligible=True,
                         ot_preapproved_min=None, ot_minutes_this_week=0)


def compute(shift, punches, ctx):
    return compute_day(ComputeDayInput('E', D, shift, tuple(punches), ctx, DEFAULT_POLICY))


@settings(max_examples=300, deadline=None)
@given(shifts, punch_lists, contexts)
def test_no_negative_durations_and_worked_within_window(shift, punches, ctx):
    day = compute(shift, punches, ctx)
    window_min = (day.window.end_ms - day.window.start_ms) / MIN_MS
    assert day.worked_min >= 0
    assert day.break_min >= 0
    assert day.late_min >= 0
    assert day.early_min >= 0
    assert day.ot.minutes >= 0
    assert day.ot.minutes <= DEFAULT_POLICY.ot.cap_day_min
    # Rounding up by at most one step may push worked slightly past the window.
    assert day.worked_min <= window_min + shift.rounding_min


@settings(max_examples=200, deadline=None)
@given(shifts, punch_lists, contexts)
def test_recompute_is_idempotent(shift, punches, ctx):
    assert compute(shift, punches, ctx) == compute(shift, list(reversed(punches)), ctx)


@settings(max_examples=200, deadline=None)
@given(shifts, punch_lists, contexts, punches_with('x'))
def test_punch_outside_window_never_changes_day(shift, punches, ctx, extra):
    base = compute(shift, punches, ctx)
    w = base.window
    at = w.end_ms + 60_000 + js_mod(extra.at, 7) * 1000 if extra.at <= w.end_ms else extra.at
    assert compute(shift, [*punches, replace(extra, at=at)], ctx) == base


@settings(max_examples=200, deadline=None)
@given(shifts, punches_with('p'))
def test_single_in_window_punch_never_present(shift, p):
    w = compute(shift, [], CTX_WORKING).window
    inside = replace(p, at=w.start_ms + abs(js_mod(p.at, w.end_ms - w.start_ms + 1)))
    day = compute(shift, [inside], CTX_WORKING)
    assert day.status != 'Present'
    assert day.status in ('Incomplete', 'WFH', 'Absent')
