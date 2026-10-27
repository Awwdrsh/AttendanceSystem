"""Port of packages/attendance-engine/src/edge.test.ts."""
import itertools
from dataclasses import replace
from datetime import datetime, timezone

from engines.attendance import (
    DEFAULT_POLICY, ComputeDayInput, DayContext, Flag, Punch, Shift, compute_day, local_time_ms, npt_today,
)

D = '2026-09-21'
GENERAL = Shift(
    code='GEN', start='10:00', end='18:00', grace_in_min=15, grace_out_min=0, early_in_window_min=60,
    late_out_window_min=120, break_minutes=0, half_day_threshold_min=240, absent_threshold_min=120,
    pairing_mode='alternating', rounding_min=0, rounding_direction='nearest',
)
NIGHT = replace(GENERAL, code='NIGHT', start='22:00', end='06:00', grace_in_min=0, break_minutes=30)


def ctx(**o) -> DayContext:
    base = dict(holiday=False, weekly_off=False, leave='none', wfh=False, ot_eligible=True, ot_preapproved_min=None,
                ot_minutes_this_week=0)
    return DayContext(**{**base, **o})


_n = itertools.count(1)


def p(date, hhmm, off=0, direction='unknown', source='zkteco', id=None) -> Punch:
    return Punch(id or f'p{next(_n)}', local_time_ms(date, hhmm, off), direction, 'fingerprint', source)


def run(shift, punches, c=None, local_date=D):
    return compute_day(ComputeDayInput('E', local_date, shift, tuple(punches), c or ctx(), DEFAULT_POLICY))


def utc_ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


class TestWindowBoundaries:
    def test_window_edges_inclusive_one_ms_outside_excluded(self):
        # The late-out edge is max(configured 120 min, OT cap 240 + 120) = 6 h after the 18:00 end, i.e. midnight.
        start = p(D, '09:00')
        end = p(D, '00:00', 1)
        assert run(GENERAL, [start, end]).punch_ids == (start.id, end.id)
        before = replace(p(D, '09:00'), at=local_time_ms(D, '09:00') - 1)
        after = replace(p(D, '00:00', 1), at=local_time_ms(D, '00:00', 1) + 1)
        assert run(GENERAL, [before, after]).punch_ids == ()

    def test_night_out_never_lands_in_next_day(self):
        punches = [p('2026-09-20', '21:55'), p('2026-09-21', '06:10')]
        assert run(NIGHT, punches, ctx(), '2026-09-20').status == 'Present'
        nxt = run(NIGHT, punches, ctx(), '2026-09-21')
        assert nxt.status == 'Absent'
        assert nxt.punch_ids == ()

    def test_start_equals_end_is_24h_shift(self):
        day = run(replace(GENERAL, start='08:00', end='08:00'), [p(D, '08:00'), p(D, '08:00', 1)])
        assert day.scheduled_min == 1440
        assert day.worked_min == 1440
        assert day.status == 'Present'

    def test_identical_timestamps_ordered_by_id(self):
        a = p(D, '10:00', 0, 'unknown', 'zkteco', 'b')
        b = p(D, '10:00', 0, 'unknown', 'zkteco', 'a')
        out = p(D, '18:00')
        d1 = run(GENERAL, [a, b, out])
        d2 = run(GENERAL, [b, out, a])
        assert d1 == d2
        assert d1.punch_ids == ('a', 'b', out.id)


class TestThresholdsAndModes:
    def test_zero_thresholds_disable_classification(self):
        d = run(replace(GENERAL, half_day_threshold_min=0, absent_threshold_min=0), [p(D, '10:00'), p(D, '10:30')])
        assert d.status == 'Present'
        assert d.worked_min == 30

    def test_strict_with_unknown_directions_alternates(self):
        d = run(replace(GENERAL, pairing_mode='strict_log_type'), [p(D, '10:00'), p(D, '13:00'), p(D, '14:00'), p(D, '18:00')])
        assert d.worked_min == 420
        assert len(d.pairs) == 2

    def test_first_last_three_punches(self):
        d = run(replace(GENERAL, pairing_mode='first_last'), [p(D, '10:00'), p(D, '13:00'), p(D, '18:00')])
        assert d.worked_min == 480
        assert not any(f.kind in ('MissingOut', 'UnpairedPunch') for f in d.flags)


class TestLeaveHolidayOffDay:
    def test_half_day_leave_no_punches_absent_with_flag(self):
        d = run(GENERAL, [], ctx(leave='first_half'))
        assert d.status == 'Absent'
        assert Flag('HalfDayLeave', half='first_half') in d.flags

    def test_half_day_leave_present_other_half(self):
        d = run(GENERAL, [p(D, '14:00'), p(D, '18:00')], ctx(leave='second_half'))
        assert d.status == 'HalfDayLeave'
        assert Flag('HalfDayLeave', half='second_half') in d.flags

    def test_holiday_wins_over_leave(self):
        assert run(GENERAL, [], ctx(holiday=True, leave='full')).status == 'Holiday'

    def test_weekly_off_ot_without_preapproval(self):
        d = run(GENERAL, [p(D, '10:00'), p(D, '14:00')], ctx(weekly_off=True))
        assert d.status == 'WeeklyOff'
        assert d.ot.minutes == 0
        assert Flag('HolidayWork', worked_min=240) in d.flags
        assert Flag('OTPreapprovalMissing', raw_minutes=240) in d.flags

    def test_auto_clock_out_flag(self):
        d = run(GENERAL, [p(D, '10:00'), p(D, '18:00', 0, 'out', 'auto_clock_out')])
        assert d.status == 'Present'
        assert Flag('AutoClockOut') in d.flags


class TestNptToday:
    def test_nepal_date_not_utc_date(self):
        assert npt_today(utc_ms(2026, 9, 21, 18, 30)) == '2026-09-22'  # 00:15 NPT on the 22nd
        assert npt_today(utc_ms(2026, 9, 21, 18, 14)) == '2026-09-21'  # 23:59 NPT on the 21st
