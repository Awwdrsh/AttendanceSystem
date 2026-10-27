"""Local-time helpers (port of packages/attendance-engine/src/time.ts).

Nepal Standard Time is UTC+05:45 with no DST (plan §6.10), so a fixed offset is used exactly as the TS does, not
zoneinfo. Instants are epoch milliseconds. Arithmetic mirrors the JS Number operations step for step (true division
then floor, Math.round half-up) so results match bit for bit.
"""
from __future__ import annotations

import math
import re
import time as _time
from datetime import date

NPT_OFFSET_MIN = 345
MIN_MS = 60_000
DAY_MIN = 1440

_DAY_MS = 86_400_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
# [0-9] rather than \d: Python's \d also matches non-ASCII digits, which the JS regex does not.
_DATE_RE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})')
_HHMM_RE = re.compile(r'([0-9]{2}):([0-9]{2})')


class AttendanceRangeError(ValueError):
    """A malformed or out-of-range date or time. Mirrors the TS RangeError."""


def parse_hhmm(s: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    m = _HHMM_RE.fullmatch(s)
    if not m:
        raise AttendanceRangeError(f'Time must be HH:MM, got {s}')
    h, mi = int(m[1]), int(m[2])
    if h > 23 or mi > 59:
        raise AttendanceRangeError(f'Time out of range: {s}')
    return h * 60 + mi


def local_midnight_ms(local_date: str) -> int:
    """Epoch ms of local (NPT) midnight for 'YYYY-MM-DD'."""
    m = _DATE_RE.fullmatch(local_date)
    if not m:
        raise AttendanceRangeError(f'Date must be YYYY-MM-DD, got {local_date}')
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    # The TS round-trips through Date.UTC, which reads years 0-99 as 1900-1999, so those never round-trip either.
    try:
        if y < 100:
            raise ValueError
        days = date(y, mo, d).toordinal() - _EPOCH_ORDINAL
    except ValueError:
        raise AttendanceRangeError(f'Invalid date {local_date}') from None
    return days * _DAY_MS - NPT_OFFSET_MIN * MIN_MS


def local_time_ms(local_date: str, hhmm: str, day_offset: int = 0) -> int:
    """Epoch ms for 'HH:MM' on a local date, optionally shifted by whole days."""
    return local_midnight_ms(local_date) + (parse_hhmm(hhmm) + day_offset * DAY_MIN) * MIN_MS


def _civil_from_days(z: int) -> tuple[int, int, int]:
    """Proleptic Gregorian (y, m, d) for days since 1970-01-01, any year (H. Hinnant's algorithm)."""
    z += 719_468
    era = z // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return yoe + era * 400 + (1 if m <= 2 else 0), m, d


def _iso_date_prefix(t: float) -> str:
    """new Date(t).toISOString().slice(0, 10), including its RangeError and expanded-year form."""
    if not math.isfinite(t) or abs(t) > 8.64e15:
        raise AttendanceRangeError('Invalid time value')
    y, m, d = _civil_from_days(math.trunc(t) // _DAY_MS)
    year = f'{y:04d}' if 0 <= y <= 9999 else f'{"+" if y > 0 else "-"}{abs(y):06d}'
    return f'{year}-{m:02d}-{d:02d}'[:10]


def local_date_of(epoch_ms: float) -> str:
    """Local (NPT) calendar date of an instant."""
    return _iso_date_prefix(epoch_ms + NPT_OFFSET_MIN * MIN_MS)


def npt_today(now: float | None = None) -> str:
    """Today's calendar date in Nepal (never the UTC date: between 00:00 and 05:45 NPT those differ)."""
    return local_date_of(_time.time_ns() // 1_000_000 if now is None else now)


def minutes_between(a_ms: float, b_ms: float) -> int:
    """Whole minutes from a to b (floor). Negative if b < a."""
    return math.floor((b_ms - a_ms) / MIN_MS)


def js_round(x: float) -> int:
    """Math.round: nearest integer, halves toward +infinity (Python's round() is half-to-even)."""
    r = math.floor(x)
    return r + 1 if x - r >= 0.5 else r


def round_minutes(minutes: float, step: float, direction: str) -> float:
    if step <= 0:
        return minutes
    q = minutes / step
    r = math.floor(q) if direction == 'down' else math.ceil(q) if direction == 'up' else js_round(q)
    return r * step
