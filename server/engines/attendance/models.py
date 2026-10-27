"""Input and output types of the attendance engine (port of packages/attendance-engine/src/types.ts).

Fields are snake_case; to_dict() gives the camelCase JSON shape of the TS objects, which is what inputs_hash is computed
over and what the API stores, and from_dict() reads it back. A field marked OMIT_NONE is left out of the JSON when None,
like an absent optional property in TS; every other None is written as null.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, Mapping, Optional, Union

PairingMode = Literal['alternating', 'strict_log_type', 'first_last']
RoundingDirection = Literal['nearest', 'down', 'up']
PunchDirection = Literal['in', 'out', 'unknown']
LeaveCover = Literal['none', 'full', 'first_half', 'second_half']
HolidayWorkPolicy = Literal['substitute_leave', 'pay', 'employee_choice']
AttendanceStatus = Literal[
    'OffShift', 'Holiday', 'WeeklyOff', 'OnLeave', 'Present', 'HalfDay', 'HalfDayLeave', 'Absent', 'Incomplete', 'WFH',
]
DayType = Literal['working', 'weekly_off', 'holiday']
Number = Union[int, float]

_OMIT = {'omit_none': True}


def _optional() -> Any:
    return field(default=None, metadata=_OMIT)


def _camel(name: str) -> str:
    # 'grace_in_min' -> 'graceInMin'; a trailing underscore escapes a Python keyword ('from_' -> 'from').
    head, *rest = name.rstrip('_').split('_')
    return head + ''.join(w[:1].upper() + w[1:] for w in rest)


def to_json_value(v: Any) -> Any:
    """Dataclasses -> camelCase dicts, tuples -> lists, recursively."""
    if is_dataclass(v) and not isinstance(v, type):
        out: dict[str, Any] = {}
        for f in fields(v):
            x = getattr(v, f.name)
            if x is None and f.metadata.get('omit_none'):
                continue
            out[_camel(f.name)] = to_json_value(x)
        return out
    if isinstance(v, (list, tuple)):
        return [to_json_value(x) for x in v]
    if isinstance(v, dict):
        return {k: to_json_value(x) for k, x in v.items()}
    return v


def _kwargs(cls: type, d: Mapping[str, Any]) -> dict[str, Any]:
    """camelCase JSON keys -> constructor kwargs; unknown keys raise so nothing silently drops out of the hash."""
    names = {_camel(f.name): f.name for f in fields(cls)}
    unknown = set(d) - set(names)
    if unknown:
        raise TypeError(f'{cls.__name__}: unknown keys {sorted(unknown)}')
    return {names[k]: v for k, v in d.items()}


class _Json:
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return to_json_value(self)


@dataclass(frozen=True, slots=True)
class Shift(_Json):
    """Mirrors Frappe Shift Type semantics (plan §5.3, §10.1). Times are local 'HH:MM'; end <= start means the shift
    crosses midnight."""
    code: str
    start: str
    end: str
    grace_in_min: int
    grace_out_min: int
    early_in_window_min: int
    late_out_window_min: int
    # Fixed break deduction in minutes; 0 = none. With more than one pair, gaps between pairs are the break instead.
    break_minutes: int
    # Worked minutes below this -> HalfDay. 0 disables. Applies only when at least one pair exists.
    half_day_threshold_min: int
    # Worked minutes below this -> Absent. 0 disables.
    absent_threshold_min: int
    pairing_mode: PairingMode
    # Rounding step for worked minutes; 0 = none.
    rounding_min: int
    rounding_direction: RoundingDirection
    # Not part of the TS Shift type, but the API passes the shift row's id along with it (repo.toShift), so it is in
    # every stored inputs_hash. Set it to reproduce those hashes; leave it None for a shift without one.
    id: Optional[str] = _optional()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'Shift':
        return cls(**_kwargs(cls, d))


@dataclass(frozen=True, slots=True)
class Punch(_Json):
    id: str
    # Epoch ms (UTC instant).
    at: Number
    direction: PunchDirection
    verify_mode: str
    source: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'Punch':
        return cls(**_kwargs(cls, d))


@dataclass(frozen=True, slots=True)
class ShortLeaveWindow(_Json):
    from_: str
    to: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'ShortLeaveWindow':
        return cls(**_kwargs(cls, d))


@dataclass(frozen=True, slots=True)
class DayContext(_Json):
    holiday: bool
    weekly_off: bool
    leave: LeaveCover
    # Approved WFH or on-duty for the day.
    wfh: bool
    ot_eligible: bool
    # Approved pre-approval minutes for the day; None = none filed (written as null).
    ot_preapproved_min: Optional[Number]
    # OT minutes already counted this week on other days (for the 24 h/week cap, Sec 30(1)).
    ot_minutes_this_week: Number
    # Approved hourly (short) leave, local 'HH:MM' windows. None = none, and the key is then left out of the hash input
    # so existing inputs hashes do not change. An empty tuple is NOT the same: it is hashed as "shortLeave":[].
    short_leave: Optional[tuple[ShortLeaveWindow, ...]] = _optional()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'DayContext':
        kw = _kwargs(cls, d)
        if kw.get('short_leave') is not None:
            kw['short_leave'] = tuple(ShortLeaveWindow.from_dict(w) for w in kw['short_leave'])
        return cls(**kw)


@dataclass(frozen=True, slots=True)
class OtPolicy(_Json):
    min_threshold_min: Number
    rounding_min: Number
    require_preapproval: bool
    # Labour Act 2074 Sec 30(1): 4 h/day.
    cap_day_min: Number
    # Sec 30(1): 24 h/week.
    cap_week_min: Number
    # Sec 31(1): 1.5x basic on working days.
    multiplier_working: Number
    multiplier_weekly_off: Number
    multiplier_holiday: Number
    # Non-statutory 22:00-04:00 premium; default off (plan §6.1).
    night_window_enabled: bool
    night_window_start: str
    night_window_end: str
    night_premium_rate: Number

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'OtPolicy':
        return cls(**_kwargs(cls, d))


@dataclass(frozen=True, slots=True)
class AttendancePolicy(_Json):
    # Included in inputs_hash so policy edits trigger recompute. Must change whenever a value changes.
    version: str
    ot: OtPolicy
    holiday_work: HolidayWorkPolicy

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'AttendancePolicy':
        kw = _kwargs(cls, d)
        kw['ot'] = OtPolicy.from_dict(kw['ot'])
        return cls(**kw)


DEFAULT_POLICY = AttendancePolicy(
    version='default-v1',
    ot=OtPolicy(
        min_threshold_min=30, rounding_min=15, require_preapproval=True, cap_day_min=240, cap_week_min=1440,
        multiplier_working=1.5, multiplier_weekly_off=1.5, multiplier_holiday=1.5, night_window_enabled=False,
        night_window_start='22:00', night_window_end='04:00', night_premium_rate=0,
    ),
    holiday_work='substitute_leave',
)


@dataclass(frozen=True, slots=True)
class Flag(_Json):
    """One of the TS Flag union members; only the fields of its kind are set:
    Late/EarlyOut/OTCapExceeded/ShortLeave: minutes. HolidayWork: worked_min. OTPreapprovalMissing: raw_minutes.
    HalfDayLeave: half. UnpairedPunch: punch_id. MissingOut/UnpaidLeave/AutoClockOut: nothing.
    ShortLeave is hourly leave inside the shift: its minutes count toward the thresholds and excuse late/early."""
    kind: str
    minutes: Optional[Number] = _optional()
    worked_min: Optional[Number] = _optional()
    raw_minutes: Optional[Number] = _optional()
    half: Optional[Literal['first_half', 'second_half']] = _optional()
    punch_id: Optional[str] = _optional()


@dataclass(frozen=True, slots=True)
class OtResult(_Json):
    status: Literal['None', 'Detected']
    minutes: Number
    raw_minutes: Number
    multiplier: Number
    day_type: DayType
    night_minutes: Number
    night_premium_rate: Number


@dataclass(frozen=True, slots=True)
class Pair(_Json):
    in_ms: Number
    out_ms: Number
    in_id: str
    out_id: str


@dataclass(frozen=True, slots=True)
class Window(_Json):
    start_ms: Number
    end_ms: Number


@dataclass(frozen=True, slots=True)
class ComputeDayInput(_Json):
    employee_id: str
    local_date: str
    shift: Optional[Shift]
    punches: tuple[Punch, ...]
    ctx: DayContext
    policy: AttendancePolicy

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'ComputeDayInput':
        kw = _kwargs(cls, d)
        kw['shift'] = None if kw['shift'] is None else Shift.from_dict(kw['shift'])
        kw['punches'] = tuple(Punch.from_dict(p) for p in kw['punches'])
        kw['ctx'] = DayContext.from_dict(kw['ctx'])
        kw['policy'] = AttendancePolicy.from_dict(kw['policy'])
        return cls(**kw)


@dataclass(frozen=True, slots=True)
class AttendanceDay(_Json):
    employee_id: str
    local_date: str
    shift_code: Optional[str]
    status: AttendanceStatus
    window: Optional[Window]
    scheduled_min: Number
    first_in_ms: Optional[Number]
    last_out_ms: Optional[Number]
    worked_min: Number
    break_min: Number
    late_min: Number
    early_min: Number
    ot: OtResult
    flags: tuple[Flag, ...]
    pairs: tuple[Pair, ...]
    # Ids of punches inside the shift window, in order. Off-window punches never affect the day.
    punch_ids: tuple[str, ...]
    inputs_hash: str
