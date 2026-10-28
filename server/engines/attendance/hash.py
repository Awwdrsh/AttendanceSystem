"""Canonical JSON and SHA-256 (port of packages/attendance-engine/src/hash.ts).

inputs_hash values are stored in the database, so canonical_json must give the exact bytes JSON.stringify gives after
the TS sortKeys: keys sorted by UTF-16 code units, no whitespace, JS number formatting (2.0 -> "2", 1e21 -> "1e+21",
NaN/Infinity -> null, -0 -> "0") and JS string escaping (only '"', '\\' and control characters are escaped; non-ASCII
is written raw; lone surrogates as lowercase \\udxxx).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

_SURROGATE = re.compile('[\ud800-\udfff]')
_ESC = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}


def _js_number(x: int | float) -> str:
    """Number::toString for the double the value would be in JS."""
    if isinstance(x, int):
        if -(2 ** 53) <= x <= 2 ** 53:
            return str(x)
        try:
            x = float(x)
        except OverflowError:
            return 'null'
    if not math.isfinite(x):
        return 'null'
    if x == 0:
        return '0'
    # repr gives the shortest round-trip digits, as JS does; re-lay them out by the ECMAScript rules.
    s = repr(abs(x))
    mant, _, exp = s.partition('e')
    ip, _, fp = mant.partition('.')
    all_digits = ip + fp
    lead = len(all_digits) - len(all_digits.lstrip('0'))
    digits = all_digits.strip('0')
    k = len(digits)
    n = len(ip) + (int(exp) if exp else 0) - lead  # value = 0.digits * 10**n
    if k <= n <= 21:
        out = digits + '0' * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + '.' + digits[n:]
    elif -6 < n <= 0:
        out = '0.' + '0' * -n + digits
    else:
        e = n - 1
        out = (digits if k == 1 else digits[0] + '.' + digits[1:]) + 'e' + ('+' if e >= 0 else '-') + str(abs(e))
    return '-' + out if x < 0 else out


def _js_string(s: str) -> str:
    # Without surrogate code points, json.dumps(ensure_ascii=False) escapes exactly what JSON.stringify does ('"',
    # '\\', \b \f \n \r \t, other C0 controls as lowercase \u00xx, everything else raw). Surrogates (only from
    # strings decoded with 'surrogatepass') need JS's pairing rules below.
    if not _SURROGATE.search(s):
        return json.dumps(s, ensure_ascii=False)
    out = ['"']
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        o = ord(c)
        if c in _ESC:
            out.append(_ESC[c])
        elif o < 0x20:
            out.append(f'\\u{o:04x}')
        elif 0xD800 <= o <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            # A surrogate pair held as two code points is one well-formed character in JS.
            out.append(chr(0x10000 + ((o - 0xD800) << 10) + (ord(s[i + 1]) - 0xDC00)))
            i += 1
        elif 0xD800 <= o <= 0xDFFF:
            out.append(f'\\u{o:04x}')
        else:
            out.append(c)
        i += 1
    out.append('"')
    return ''.join(out)


def _utf16_key(s: str) -> bytes:
    # Big-endian UTF-16 bytes compare in code-unit order, which is what Array.prototype.sort() uses on strings.
    return s.encode('utf-16-be', 'surrogatepass')


def _dump(v: Any, out: list[str]) -> None:
    if v is None:
        out.append('null')
    elif v is True:
        out.append('true')
    elif v is False:
        out.append('false')
    elif isinstance(v, (int, float)):
        out.append(_js_number(v))
    elif isinstance(v, str):
        out.append(_js_string(v))
    elif isinstance(v, (list, tuple)):
        out.append('[')
        for i, x in enumerate(v):
            if i:
                out.append(',')
            _dump(x, out)
        out.append(']')
    elif isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise TypeError(f'canonical_json: object keys must be str, got {k!r}')
        out.append('{')
        for i, k in enumerate(sorted(v, key=_utf16_key)):
            if i:
                out.append(',')
            out.append(_js_string(k))
            out.append(':')
            _dump(v[k], out)
        out.append('}')
    else:
        raise TypeError(f'canonical_json: unsupported type {type(v).__name__}')


def canonical_json(value: Any) -> str:
    """Deterministic JSON: object keys sorted recursively, arrays kept in order. Takes plain JSON values (dict, list,
    tuple, str, int, float, bool, None); convert dataclasses with to_dict() first."""
    out: list[str] = []
    _dump(value, out)
    return ''.join(out)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()
