"""Pure, recomputable attendance-day computation over immutable punches (plan.md §10).

Port of packages/attendance-engine. No Django, no I/O, no clock: compute_day(input) depends only on its input, and
inputs_hash is byte-identical to the TS engine's so stored hashes stay valid across the migration.
"""
from .compute_day import compute_day
from .hash import canonical_json, sha256_hex
from .models import (
    DEFAULT_POLICY, AttendanceDay, AttendancePolicy, AttendanceStatus, ComputeDayInput, DayContext, DayType, Flag,
    HolidayWorkPolicy, LeaveCover, OtPolicy, OtResult, Pair, PairingMode, Punch, PunchDirection, RoundingDirection,
    Shift, ShortLeaveWindow, Window,
)
from .ot import OtInput, compute_ot, no_ot
from .pairing import PairingResult, locale_compare, pair_punches, sort_punches
from .timeutil import (
    DAY_MIN, MIN_MS, NPT_OFFSET_MIN, AttendanceRangeError, local_date_of, local_midnight_ms, local_time_ms,
    minutes_between, npt_today, parse_hhmm, round_minutes,
)

__all__ = [
    'compute_day', 'compute_ot', 'no_ot', 'OtInput', 'pair_punches', 'sort_punches', 'locale_compare', 'PairingResult',
    'canonical_json', 'sha256_hex', 'DEFAULT_POLICY', 'AttendanceDay', 'AttendancePolicy', 'AttendanceStatus',
    'ComputeDayInput', 'DayContext', 'DayType', 'Flag', 'HolidayWorkPolicy', 'LeaveCover', 'OtPolicy', 'OtResult',
    'Pair', 'PairingMode', 'Punch', 'PunchDirection', 'RoundingDirection', 'Shift', 'ShortLeaveWindow', 'Window',
    'NPT_OFFSET_MIN', 'MIN_MS', 'DAY_MIN', 'AttendanceRangeError', 'parse_hhmm', 'local_midnight_ms', 'local_time_ms',
    'local_date_of', 'npt_today', 'minutes_between', 'round_minutes',
]
