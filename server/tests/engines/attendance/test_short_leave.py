"""Port of packages/attendance-engine/src/short-leave.test.ts."""
import itertools
from dataclasses import replace

from engines.attendance import (
    DEFAULT_POLICY, ComputeDayInput, DayContext, Flag, Punch, Shift, ShortLeaveWindow, compute_day, local_time_ms,
)

D = '2026-09-21'
GENERAL = Shift(
    code='GEN', start='10:00', end='18:00', grace_in_min=15, grace_out_min=0, early_in_window_min=60,
    late_out_window_min=120, break_minutes=0, half_day_threshold_min=240, absent_threshold_min=120,
    pairing_mode='alternating', rounding_min=0, rounding_direction='nearest',
)
NIGHT = replace(GENERAL, code='NIGHT', start='22:00', end='06:00', grace_in_min=0)


def ctx(**over) -> DayContext:
    base = dict(holiday=False, weekly_off=False, leave='none', wfh=False, ot_eligible=True, ot_preapproved_min=None,
                ot_minutes_this_week=0)
    return DayContext(**{**base, **over})


def leave(*windows) -> tuple[ShortLeaveWindow, ...]:
    return tuple(ShortLeaveWindow(f, t) for f, t in windows)


_seq = itertools.count(1)


def punch(hhmm, day_offset=0) -> Punch:
    return Punch(f's{next(_seq)}', local_time_ms(D, hhmm, day_offset), 'unknown', 'fp', 'zkteco')


def run(shift, punches, c):
    return compute_day(ComputeDayInput('E1', D, shift, tuple(punches), c, DEFAULT_POLICY))


class TestHourlyLeave:
    def test_morning_window_excuses_late(self):
        without = run(GENERAL, [punch('12:05'), punch('18:00')], ctx())
        assert without.late_min == 110
        day = run(GENERAL, [punch('12:05'), punch('18:00')], ctx(short_leave=leave(('10:00', '12:00'))))
        assert day.late_min == 0
        assert day.status == 'Present'
        assert Flag('ShortLeave', minutes=120) in day.flags

    def test_late_counted_from_end_of_window(self):
        day = run(GENERAL, [punch('12:30'), punch('18:00')], ctx(short_leave=leave(('10:00', '12:00'))))
        assert day.late_min == 15

    def test_afternoon_window_excuses_early(self):
        day = run(GENERAL, [punch('10:00'), punch('16:00')], ctx(short_leave=leave(('16:00', '18:00'))))
        assert day.early_min == 0
        partly = run(GENERAL, [punch('10:00'), punch('15:30')], ctx(short_leave=leave(('16:00', '18:00'))))
        assert partly.early_min == 30

    def test_leave_minutes_count_toward_half_day(self):
        # 3 h worked + 2 h leave = 5 h credited, above the 4 h half-day threshold.
        without = run(GENERAL, [punch('13:00'), punch('16:00')], ctx())
        assert without.status == 'HalfDay'
        day = run(GENERAL, [punch('13:00'), punch('16:00')], ctx(short_leave=leave(('16:00', '18:00'))))
        assert day.status == 'Present'
        assert day.worked_min == 180

    def test_mid_day_window_no_double_count(self):
        day = run(GENERAL, [punch('10:00'), punch('14:00'), punch('15:00'), punch('15:30')],
                  ctx(short_leave=leave(('13:00', '15:00'))))
        assert day.early_min == 150
        # worked 270 min; leave 13:00-15:00 overlaps 60 min of work, so 60 leave minutes are credited.
        assert day.status == 'Present'

    def test_leave_but_no_punches_absent(self):
        assert run(GENERAL, [], ctx(short_leave=leave(('10:00', '12:00')))).status == 'Absent'

    def test_across_midnight_on_night_shift(self):
        day = run(NIGHT, [punch('22:00'), punch('04:00', 1)], ctx(short_leave=leave(('04:00', '06:00'))))
        assert day.early_min == 0
        assert Flag('ShortLeave', minutes=120) in day.flags

    def test_hash_unchanged_without_short_leave(self):
        ps = [punch('10:00'), punch('18:00')]
        plain = run(GENERAL, ps, ctx())
        assert run(GENERAL, ps, replace(ctx(), short_leave=None)).inputs_hash == plain.inputs_hash
        assert run(GENERAL, ps, ctx(short_leave=leave(('10:00', '11:00')))).inputs_hash != plain.inputs_hash
