"""Port of packages/attendance-engine/src/compute-day.test.ts."""
import itertools
from dataclasses import replace

from engines.attendance import DEFAULT_POLICY, ComputeDayInput, DayContext, Flag, Punch, Shift, compute_day, local_time_ms

D = '2026-09-21'  # Monday (BS 2083-06-05)

GENERAL = Shift(
    code='GEN', start='10:00', end='18:00', grace_in_min=15, grace_out_min=0, early_in_window_min=60,
    late_out_window_min=120, break_minutes=0, half_day_threshold_min=240, absent_threshold_min=120,
    pairing_mode='alternating', rounding_min=0, rounding_direction='nearest',
)
NIGHT = replace(GENERAL, code='NIGHT', start='22:00', end='06:00', grace_in_min=0, break_minutes=30)


def ctx(**over) -> DayContext:
    base = dict(holiday=False, weekly_off=False, leave='none', wfh=False, ot_eligible=True, ot_preapproved_min=None,
                ot_minutes_this_week=0)
    return DayContext(**{**base, **over})


_seq = itertools.count(1)


def punch(date: str, hhmm: str, day_offset: int = 0, direction: str = 'unknown') -> Punch:
    return Punch(f'p{next(_seq)}', local_time_ms(date, hhmm, day_offset), direction, 'fingerprint', 'zkteco')


def run(shift, punches, c=None, policy=DEFAULT_POLICY):
    return compute_day(ComputeDayInput('E1', D, shift, tuple(punches), c or ctx(), policy))


class TestWorkedExamples:
    """plan §10.5."""

    def test_a_late_2_min_sub_threshold_ot_present(self):
        day = run(GENERAL, [punch(D, '10:17'), punch(D, '18:40')])
        assert day.status == 'Present'
        assert day.worked_min == 503
        assert day.late_min == 2
        assert day.early_min == 0
        assert day.ot.minutes == 0
        assert day.ot.raw_minutes == 23
        assert day.flags == (Flag('Late', minutes=2),)

    def test_b_night_shift_across_midnight_attributed_to_start_date(self):
        punches = [punch(D, '21:50'), punch(D, '06:20', 1)]
        day = run(NIGHT, punches)
        assert day.status == 'Present'
        assert day.worked_min == 480
        assert day.break_min == 30
        assert day.late_min == 0
        assert day.early_min == 0
        assert day.ot.minutes == 0
        assert day.punch_ids == tuple(p.id for p in punches)

        # The same punches do not belong to the next day's night shift.
        nxt = compute_day(ComputeDayInput('E1', '2026-09-22', NIGHT, tuple(punches), ctx(), DEFAULT_POLICY))
        assert nxt.status == 'Absent'
        assert nxt.punch_ids == ()

    def test_c_day_cap_truncates_5h_to_4h(self):
        shift = replace(GENERAL, late_out_window_min=360)
        day = run(shift, [punch(D, '10:00'), punch(D, '23:00')], ctx(ot_preapproved_min=300))
        assert day.worked_min == 780
        assert day.ot.raw_minutes == 300
        assert day.ot.minutes == 240
        assert day.ot.multiplier == 1.5
        assert Flag('OTCapExceeded', minutes=60) in day.flags

    def test_d_weekly_cap_after_24h(self):
        shift = replace(GENERAL, late_out_window_min=360)
        day = run(shift, [punch(D, '10:00'), punch(D, '22:00')], ctx(ot_preapproved_min=240, ot_minutes_this_week=1440))
        assert day.ot.minutes == 0
        assert Flag('OTCapExceeded', minutes=240) in day.flags


class TestStatusPrecedenceAndFlags:
    def test_off_shift_when_no_shift(self):
        assert run(None, [punch(D, '10:00')]).status == 'OffShift'

    def test_single_punch_is_incomplete(self):
        day = run(GENERAL, [punch(D, '10:00')])
        assert day.status == 'Incomplete'
        assert Flag('MissingOut') in day.flags
        assert day.last_out_ms is None

    def test_no_punches_absent_wfh_overrides(self):
        assert run(GENERAL, []).status == 'Absent'
        assert run(GENERAL, [], ctx(wfh=True)).status == 'WFH'

    def test_thresholds(self):
        assert run(GENERAL, [punch(D, '10:00'), punch(D, '11:30')]).status == 'Absent'
        assert run(GENERAL, [punch(D, '10:00'), punch(D, '13:00')]).status == 'HalfDay'

    def test_half_day_leave_on_present_day(self):
        assert run(GENERAL, [punch(D, '10:00'), punch(D, '18:00')], ctx(leave='first_half')).status == 'HalfDayLeave'

    def test_full_leave_wins_over_punches(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '18:00')], ctx(leave='full'))
        assert day.status == 'OnLeave'
        assert day.late_min == 0

    def test_holiday_with_punches(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '14:00')], ctx(holiday=True, ot_preapproved_min=240))
        assert day.status == 'Holiday'
        assert Flag('HolidayWork', worked_min=240) in day.flags
        assert (day.ot.minutes, day.ot.day_type) == (240, 'holiday')

    def test_weekly_off_without_punches(self):
        assert run(GENERAL, [], ctx(weekly_off=True)).status == 'WeeklyOff'

    def test_ot_without_preapproval_flagged_not_paid(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '19:00')])
        assert day.ot.minutes == 0
        assert Flag('OTPreapprovalMissing', raw_minutes=60) in day.flags

    def test_ot_ineligible_never_gets_ot(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '19:00')], ctx(ot_eligible=False, ot_preapproved_min=60))
        assert day.ot.minutes == 0
        assert not any(f.kind.startswith('OT') for f in day.flags)

    def test_early_out(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '17:30')])
        assert day.early_min == 30
        assert Flag('EarlyOut', minutes=30) in day.flags


class TestPairingModes:
    def test_alternating_four_punches_excludes_break(self):
        day = run(GENERAL, [punch(D, '10:00'), punch(D, '13:00'), punch(D, '14:00'), punch(D, '18:00')])
        assert day.worked_min == 420
        assert day.break_min == 60

    def test_strict_ignores_duplicate_in(self):
        shift = replace(GENERAL, pairing_mode='strict_log_type')
        day = run(shift, [punch(D, '10:00', 0, 'in'), punch(D, '10:01', 0, 'in'), punch(D, '18:00', 0, 'out')])
        assert day.worked_min == 480
        assert Flag('UnpairedPunch', punch_id=day.punch_ids[1]) in day.flags
        assert Flag('MissingOut') not in day.flags

    def test_first_last_spans_breaks(self):
        shift = replace(GENERAL, pairing_mode='first_last', break_minutes=30)
        day = run(shift, [punch(D, '10:00'), punch(D, '13:00'), punch(D, '14:00'), punch(D, '18:00')])
        assert day.worked_min == 450

    def test_rounding_applies_to_worked(self):
        shift = replace(GENERAL, rounding_min=15, rounding_direction='down')
        assert run(shift, [punch(D, '10:00'), punch(D, '18:14')]).worked_min == 480


class TestInputsHash:
    def test_stable_and_ignores_off_window_punches(self):
        in_window = [punch(D, '10:00'), punch(D, '18:00')]
        a = run(GENERAL, in_window)
        b = run(GENERAL, [*in_window, punch(D, '03:00'), punch(D, '00:30', 1)])
        assert a.inputs_hash == b.inputs_hash
        assert a == b

    def test_changes_with_punch_shift_or_policy_version(self):
        base = run(GENERAL, [punch(D, '10:00'), punch(D, '18:00')])
        assert run(GENERAL, [punch(D, '10:00'), punch(D, '18:01')]).inputs_hash != base.inputs_hash
        assert run(replace(GENERAL, grace_in_min=10), [punch(D, '10:00'), punch(D, '18:00')]).inputs_hash != base.inputs_hash
        v2 = replace(DEFAULT_POLICY, version='v2')
        assert run(GENERAL, [punch(D, '10:00'), punch(D, '18:00')], ctx(), v2).inputs_hash != base.inputs_hash
