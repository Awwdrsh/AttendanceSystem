"""Small JavaScript-semantics helpers, so the gateway answers devices exactly as the TS gateway did.

Terminals send odd input (padded fields, hex ids, BOMs, over-long values). These helpers reproduce what
String.prototype.trim, Number(), Number.isSafeInteger, Date.UTC and Date.toISOString do with them.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

# ECMAScript WhiteSpace + LineTerminator (what String.prototype.trim removes).
_JS_WS = '\t\n\v\f\r                  　﻿'

MAX_SAFE_INTEGER = 2 ** 53 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def js_trim(s: str) -> str:
    return s.strip(_JS_WS)


def utf16_len(s: str) -> int:
    """String.length: UTF-16 code units."""
    return len(s.encode('utf-16-le')) // 2


def js_truthy(v) -> bool:
    """JS truthiness: empty arrays and objects are truthy, unlike Python."""
    if v is None or v is False:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


_DEC_RE = re.compile(r'[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)')


def js_number(s) -> float:
    """Number(x) for a string (or null, which is 0)."""
    if s is None:
        return 0.0
    t = js_trim(str(s))
    if t == '':
        return 0.0
    for prefix, base in (('0x', 16), ('0o', 8), ('0b', 2)):
        if t[:2].lower() == prefix:
            try:
                return float(int(t[2:], base)) if t[2:] and '_' not in t else math.nan
            except ValueError:
                return math.nan
    if _DEC_RE.fullmatch(t):
        return float(t.replace('Infinity', 'inf'))
    return math.nan


def is_safe_integer(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and float(x).is_integer() and abs(x) <= MAX_SAFE_INTEGER


def js_string_of_number(v: int | float) -> str:
    """String(n) for the numbers a JSON payload can carry."""
    if isinstance(v, int):
        return str(v)
    if math.isnan(v):
        return 'NaN'
    if math.isinf(v):
        return 'Infinity' if v > 0 else '-Infinity'
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if 'e' in r:
        mant, exp = r.split('e')
        sign = '-' if exp.startswith('-') else '+'
        r = f'{mant.removesuffix(".0")}e{sign}{exp.lstrip("+-").lstrip("0") or "0"}'
    return r


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def utc_ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """Date.UTC(y, mo - 1, d, h, mi, s): out-of-range parts roll over; years 0..99 mean 1900..1999."""
    if 0 <= y <= 99:
        y += 1900
    m0 = mo - 1
    y += m0 // 12
    m0 %= 12
    days = _days_from_civil(y, m0 + 1, 1) + (d - 1)
    return ((days * 24 + h) * 60 + mi) * 60_000 + s * 1000


def utc_parts(ms: int) -> tuple[int, int, int, int]:
    """(year, month 1-12, day, hour) of an epoch-ms instant, in UTC."""
    t = _EPOCH + timedelta(milliseconds=ms)
    return t.year, t.month, t.day, t.hour


def iso_ms(ms: int) -> str:
    """Date.prototype.toISOString."""
    t = _EPOCH + timedelta(milliseconds=ms)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


def dt_from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def ms_from_dt(t: datetime) -> int:
    return int((t - _EPOCH) // timedelta(milliseconds=1))


def js_round(x: float) -> int:
    """Math.round: halves go towards +infinity."""
    return math.floor(x + 0.5)
