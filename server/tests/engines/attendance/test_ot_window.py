"""Port of packages/attendance-engine/src/ot-window.test.ts."""
import itertools
from dataclasses import replace

from engines.attendance import DEFAULT_POLICY, ComputeDayInput, DayContext, Punch, Shift, compute_day, local_time_ms

D = '2026-09-21'
GENERAL = Shift(
    code='GEN', start='09:00', end='18:00', grace_in_min=15, grace_out_min=0, early_in_window_min=60,
    late_out_window_min=120, break_minutes=60, half_day_threshold_min=240, absent_threshold_min=120,
    pairing_mode='alternating', rounding_min=0, rounding_direction='nearest',
)
NIGHT = replace(GENERAL, code='NIGHT', start='22:00', end='06:00', grace_in_min=0, break_minutes=30)
CTX = DayContext(holiday=False, weekly_off=False, leave='none', wfh=False, ot_eligible=True, ot_preapproved_min=600,
                 ot_minutes_this_week=0)
_seq = itertools.count(1)


def punch(date, hhmm, day_offset=0) -> Punch:
    return Punch(f'w{next(_seq)}', local_time_ms(date, hhmm, day_offset), 'unknown', 'fingerprint', 'zkteco')


def run(shift, punches, date=D):
    return compute_day(ComputeDayInput('E1', date, shift, tuple(punches), CTX, DEFAULT_POLICY))


class TestOvertimePunchOutsBeyondLateOutWindow:
    def test_out_2_5h_after_end_still_pairs(self):
        day = run(GENERAL, [punch(D, '09:00'), punch(D, '20:30')])
        assert day.status == 'Present'
        assert 'MissingOut' not in [f.kind for f in day.flags]
        assert day.worked_min == 630  # 11.5 h less the 1 h break
        assert day.ot.raw_minutes == 150  # like with like: 630 worked vs 480 expected

    def test_night_out_3_5h_late_pairs_across_midnight(self):
        day = run(NIGHT, [punch(D, '21:58'), punch(D, '09:30', 1)])
        assert day.status == 'Present'
        # 11 h 32 m - 30 m break = 662 worked vs 450 expected -> 212, rounded down to 15 -> 210
        assert day.ot.raw_minutes == 210

    def test_widened_window_never_swallows_next_day(self):
        nxt = '2026-09-22'
        punches = [punch(D, '09:00'), punch(D, '18:00'), punch(nxt, '08:30'), punch(nxt, '18:00', 0)]
        assert len(run(GENERAL, punches).punch_ids) == 2
        assert len(run(GENERAL, punches, nxt).punch_ids) == 2


class TestOvertimeComparesWithScheduledLessBreak:
    def test_30_min_break_out_90_late(self):
        assert run(replace(GENERAL, break_minutes=30), [punch(D, '09:00'), punch(D, '19:30')]).ot.raw_minutes == 90

    def test_no_ot_when_exactly_shift_less_break(self):
        assert run(GENERAL, [punch(D, '09:00'), punch(D, '18:00')]).ot.raw_minutes == 0
