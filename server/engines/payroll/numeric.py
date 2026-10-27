"""Decimal arithmetic with decimal.js semantics (port of src/decimal.ts).

Money never goes through floats (plan §11.7). decimal.js is configured with 30 significant digits and half-up
rounding; ENGINE_CONTEXT reproduces that, with no traps so that overflow gives Infinity and 0/0 gives NaN as in
decimal.js. Public engine functions run inside it via @engine_context, so callers' own decimal context is untouched.

A few decimal.js behaviours differ from Python's decimal module and are emulated here: string parsing (D), signed
zero in toFixed/toString, Decimal.min/max tie handling, exact toDecimalPlaces/floor/ceil regardless of precision, and
the ±9e15 exponent limits.
"""
from __future__ import annotations

import functools
import math
import re
from decimal import (
    MAX_EMAX, MIN_EMIN, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal, localcontext,
)
from typing import Any, Callable, Iterable, TypeVar

__all__ = [
    'Decimal', 'DecimalError', 'ENGINE_CONTEXT', 'EXP_LIMIT', 'UNDEFINED', 'D', 'ZERO', 'rupee', 'paisa', 'money',
    'dsum', 'max0', 'dmax', 'dmin', 'to_dp', 'to_fixed', 'js_str', 'js_num_str', 'is_neg', 'is_integer', 'floor',
    'ceil', 'engine_context', 'finalise',
]

# decimal.js EXP_LIMIT: exponents above it overflow to Infinity, below it underflow to zero.
EXP_LIMIT = 9 * 10**15

ENGINE_CONTEXT = Context(prec=30, rounding=ROUND_HALF_UP, Emax=EXP_LIMIT, Emin=-EXP_LIMIT, traps=[])

F = TypeVar('F', bound=Callable[..., Any])


def engine_context(fn: F) -> F:
    """Run fn under ENGINE_CONTEXT (nested calls are fine)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with localcontext(ENGINE_CONTEXT):
            return fn(*args, **kwargs)
    return wrapper  # type: ignore[return-value]


class DecimalError(Exception):
    """decimal.js '[DecimalError] Invalid argument: x' (a plain Error in TS)."""


class _Undefined:
    """JS undefined, for a missing key (None stands for JSON null)."""
    _instance: '_Undefined | None' = None

    def __new__(cls) -> '_Undefined':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return 'undefined'

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()

_IS_DECIMAL = re.compile(r'^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$')
_IS_OTHER = {
    16: re.compile(r'^0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)(?:[pP][+-]?[0-9]+)?$'),
    2: re.compile(r'^0[bB](?:[01]+(?:\.[01]*)?|\.[01]+)(?:[pP][+-]?[0-9]+)?$'),
    8: re.compile(r'^0[oO](?:[0-7]+(?:\.[0-7]*)?|\.[0-7]+)(?:[pP][+-]?[0-9]+)?$'),
}
_UNDERSCORE = re.compile(r'([0-9])_(?=[0-9])')


def _js_string(v: Any) -> str:
    """String(v) for the error message, as JS would print it."""
    if v is UNDEFINED:
        return 'undefined'
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, dict):
        return '[object Object]'
    if isinstance(v, list):
        return ','.join('' if x is None or x is UNDEFINED else _js_string(x) for x in v)
    return str(v)


def finalise(x: Decimal) -> Decimal:
    """decimal.js limits: exponent above EXP_LIMIT is ±Infinity, below -EXP_LIMIT is a signed zero."""
    if x.is_finite() and not x.is_zero():
        e = x.adjusted()
        if e > EXP_LIMIT:
            return Decimal('-Infinity') if x.is_signed() else Decimal('Infinity')
        if e < -EXP_LIMIT:
            return Decimal('-0') if x.is_signed() else Decimal(0)
    return x


def _parse_other(sign: str, s: str) -> Decimal:
    """Hex/binary/octal literals with optional fraction and binary exponent (decimal.js parseOther), computed exactly."""
    for base, rx in _IS_OTHER.items():
        if rx.match(s):
            break
    else:
        raise DecimalError(f'[DecimalError] Invalid argument: {s}')
    body = s[2:]
    p = 0
    m = re.search(r'[pP]', body)
    if m:
        p = int(body[m.start() + 1:])
        body = body[:m.start()]
    int_part, _, frac = body.partition('.')
    digits = int_part + frac
    n = int(digits, base) if digits else 0
    with localcontext(Context(prec=MAX_EMAX, Emax=MAX_EMAX, Emin=MIN_EMIN)):
        # base is a power of two, so n / base^len(frac) * 2^p has a finite decimal expansion.
        value = Decimal(n) * (Decimal(2) ** p) / (Decimal(base) ** len(frac)) if (frac or p) else Decimal(n)
    if value.is_zero():
        return Decimal(sign + '0')
    return finalise(-value if sign == '-' else value)


def D(v: Any) -> Decimal:
    """new Decimal(v): Decimal, int, float or a decimal.js-syntax string; anything else is a DecimalError."""
    if isinstance(v, Decimal):
        return finalise(v)
    if isinstance(v, bool) or v is None or v is UNDEFINED or isinstance(v, (dict, list, tuple)):
        raise DecimalError(f'[DecimalError] Invalid argument: {_js_string(v)}')
    if isinstance(v, int):
        return finalise(Decimal(v))
    if isinstance(v, float):
        # Same digits as JS Number.prototype.toString (both print the shortest round-trip form).
        if math.isnan(v):
            return Decimal('NaN')
        if math.isinf(v):
            return Decimal('Infinity') if v > 0 else Decimal('-Infinity')
        return finalise(Decimal(repr(v)))
    if not isinstance(v, str):
        raise DecimalError(f'[DecimalError] Invalid argument: {_js_string(v)}')
    s = v
    sign = ''
    if s[:1] == '-':
        sign, s = '-', s[1:]
    elif s[:1] == '+':
        s = s[1:]
    if _IS_DECIMAL.match(s):
        return finalise(Decimal(sign + s))
    if '_' in s:
        s = _UNDERSCORE.sub(r'\1', s)
        if _IS_DECIMAL.match(s):
            return finalise(Decimal(sign + s))
    elif s in ('Infinity', 'NaN'):
        return Decimal(sign + s) if s == 'Infinity' else Decimal('NaN')
    return _parse_other(sign, s)


ZERO = Decimal(0)


def is_neg(x: Decimal) -> bool:
    """decimal.js isNegative: true for -0, false for NaN."""
    return x.is_signed() and not x.is_nan()


def is_integer(x: Decimal) -> bool:
    return x.is_finite() and x == x.to_integral_value()


def to_dp(x: Decimal, dp: int, rounding: str = ROUND_HALF_UP) -> Decimal:
    """toDecimalPlaces: exact rounding to dp places, not limited by the 30-digit precision."""
    if not x.is_finite() or x.as_tuple().exponent >= -dp:  # type: ignore[operator]
        return x
    prec = max(1, x.adjusted() + dp + 2)
    return x.quantize(Decimal(1).scaleb(-dp), rounding=rounding, context=Context(prec=prec, Emax=MAX_EMAX, Emin=MIN_EMIN))


def floor(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_FLOOR) if x.is_finite() else x


def ceil(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_CEILING) if x.is_finite() else x


def _non_finite(x: Decimal) -> str:
    if x.is_nan():
        return 'NaN'
    return '-Infinity' if x.is_signed() else 'Infinity'


def to_fixed(x: Decimal, dp: int) -> str:
    """toFixed(dp): plain notation, half-up; the sign comes from the value before rounding (so -0.004 is '-0.00')."""
    if not x.is_finite():
        return _non_finite(x)
    y = to_dp(x, dp)
    _, digits, exp = y.as_tuple()
    ds = ''.join(map(str, digits))
    assert isinstance(exp, int)
    if exp >= 0:
        int_part, frac = ds + '0' * exp, ''
    else:
        n = -exp
        ds = ds.rjust(n + 1, '0')
        int_part, frac = ds[:-n], ds[-n:]
    int_part = int_part.lstrip('0') or '0'
    frac = frac.ljust(dp, '0')
    s = int_part + ('.' + frac if dp else '')
    return '-' + s if is_neg(x) and not x.is_zero() else s


def js_str(x: Decimal) -> str:
    """Decimal.prototype.toString: shortest digits, exponent form when the exponent is <= -7 or >= 21."""
    if not x.is_finite():
        return _non_finite(x)
    if x.is_zero():
        return '0'
    _, digits, _exp = x.as_tuple()
    ds = ''.join(map(str, digits)).rstrip('0')
    e = x.adjusted()
    if e <= -7 or e >= 21:
        s = ds[0] + ('.' + ds[1:] if len(ds) > 1 else '') + ('e' if e < 0 else 'e+') + str(e)
    elif e < 0:
        s = '0.' + '0' * (-e - 1) + ds
    elif e + 1 >= len(ds):
        s = ds + '0' * (e + 1 - len(ds))
    else:
        s = ds[:e + 1] + '.' + ds[e + 1:]
    return '-' + s if x.is_signed() else s


def js_num_str(n: Any) -> str:
    """String(n) for a JS number (template literals): 30.0 prints as '30'."""
    if isinstance(n, bool):
        return 'true' if n else 'false'
    if isinstance(n, float):
        if n.is_integer() and abs(n) < 1e21:
            return str(int(n))
        r = repr(n)
        return re.sub(r'e([+-])0*(\d)', r'e\1\2', r)
    return _js_string(n)


def _min_max(args: tuple[Any, ...], n: int) -> Decimal:
    """decimal.js maxOrMin: NaN wins; ties replace x when x's sign is n (so max(-0, 0) = 0, min(0, -0) = -0)."""
    x = D(args[0])
    for a in args[1:]:
        y = D(a)
        if y.is_nan():
            return y
        if x.is_nan():
            continue
        k = (x > y) - (x < y)
        if k == n or (k == 0 and (-1 if x.is_signed() else 1) == n):
            x = y
    return x


def dmax(*args: Any) -> Decimal:
    return _min_max(args, -1)


def dmin(*args: Any) -> Decimal:
    return _min_max(args, 1)


@engine_context
def rupee(v: Any) -> Decimal:
    """Half-up to the rupee (plan §11.7: statutory and net)."""
    return to_dp(D(v), 0)


@engine_context
def paisa(v: Any) -> Decimal:
    """Half-up to the paisa (internal lines)."""
    return to_dp(D(v), 2)


@engine_context
def money(v: Any) -> str:
    """'NNN.NN' string for storage in numeric(14,2)."""
    return to_fixed(paisa(v), 2)


@engine_context
def dsum(xs: Iterable[Any]) -> Decimal:
    t = ZERO
    for x in xs:
        t = t + D(x)
    return t


@engine_context
def max0(v: Any) -> Decimal:
    return dmax(0, v)
