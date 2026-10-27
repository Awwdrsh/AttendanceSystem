"""The few JavaScript semantics the TS engine relies on that Python does not share.

Plain JS objects inherit Object.prototype, so obj['toString'] is a function rather than undefined; that changes
error paths when HR-defined codes or formula names collide with prototype members. Strings are indexed in UTF-16
code units. These helpers reproduce exactly the cases the engine can reach.
"""
from __future__ import annotations

import math
from typing import Any

from .numeric import UNDEFINED, Decimal

# Members of Object.prototype: reading them from a plain object gives a function (or, for __proto__, an object).
OBJECT_PROTO_NAMES = frozenset({
    'constructor', '__defineGetter__', '__defineSetter__', 'hasOwnProperty', '__lookupGetter__', '__lookupSetter__',
    'isPrototypeOf', 'propertyIsEnumerable', 'toString', 'valueOf', '__proto__', 'toLocaleString',
})

# Methods inherited by primitives and arrays (on top of Object.prototype), for rule paths such as "bands.all.map".
_NUMBER_PROTO = frozenset({'toExponential', 'toFixed', 'toPrecision', 'toLocaleString'})
_STRING_PROTO = frozenset({
    'at', 'charAt', 'charCodeAt', 'codePointAt', 'concat', 'endsWith', 'includes', 'indexOf', 'lastIndexOf',
    'localeCompare', 'match', 'matchAll', 'normalize', 'padEnd', 'padStart', 'repeat', 'replace', 'replaceAll', 'search',
    'slice', 'split', 'startsWith', 'substring', 'substr', 'toLocaleLowerCase', 'toLocaleUpperCase', 'toLowerCase',
    'toUpperCase', 'trim', 'trimStart', 'trimEnd', 'trimLeft', 'trimRight', 'anchor', 'big', 'blink', 'bold', 'fixed',
    'fontcolor', 'fontsize', 'italics', 'link', 'small', 'strike', 'sub', 'sup', 'isWellFormed', 'toWellFormed',
})
_ARRAY_PROTO = frozenset({
    'at', 'concat', 'copyWithin', 'entries', 'every', 'fill', 'filter', 'find', 'findIndex', 'findLast', 'findLastIndex',
    'flat', 'flatMap', 'forEach', 'includes', 'indexOf', 'join', 'keys', 'lastIndexOf', 'map', 'pop', 'push', 'reduce',
    'reduceRight', 'reverse', 'shift', 'slice', 'some', 'sort', 'splice', 'toReversed', 'toSorted', 'toSpliced',
    'unshift', 'values', 'with',
})


class JsFunction:
    """Stands for a function value read off a prototype (only its type matters)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f'function {self.name}() {{ [native code] }}'


class JsObject:
    """Stands for Object.prototype itself (obj.__proto__)."""

    def __str__(self) -> str:
        return '[object Object]'


def js_get(obj: dict[str, Any], key: str) -> Any:
    """obj[key] on a plain JS object: own key, else the inherited prototype member, else undefined."""
    if key in obj:
        return obj[key]
    if key == '__proto__':
        return JsObject()
    if key in OBJECT_PROTO_NAMES:
        return JsFunction(key)
    return UNDEFINED


def js_member(v: Any, seg: str) -> Any:
    """(v as Record)?.[seg] for the values a rule path can walk through: objects, arrays and strings."""
    if v is None or v is UNDEFINED:
        return UNDEFINED
    if isinstance(v, dict):
        return js_get(v, seg)
    if isinstance(v, (list, str)):
        items = list(to_utf16(v)) if isinstance(v, str) else v
        if seg == 'length':
            return len(items)
        if seg.isdigit() and seg.isascii() and (seg == '0' or not seg.startswith('0')):
            i = int(seg)
            return items[i] if i < len(items) else UNDEFINED
        if seg in (_STRING_PROTO if isinstance(v, str) else _ARRAY_PROTO):
            return JsFunction(seg)
    elif isinstance(v, (int, float)) and not isinstance(v, bool) and seg in _NUMBER_PROTO:
        return JsFunction(seg)
    if seg == '__proto__':
        return JsObject()
    if seg in OBJECT_PROTO_NAMES:
        return JsFunction(seg)
    return UNDEFINED


def js_truthy(v: Any) -> bool:
    """JS truthiness: objects (including Decimal and empty lists/dicts) are truthy."""
    if v is None or v is UNDEFINED or v is False:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, Decimal):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


def nullish(v: Any, default: Any) -> Any:
    """v ?? default."""
    return default if v is None or v is UNDEFINED else v


def typeof(v: Any) -> str:
    if v is UNDEFINED:
        return 'undefined'
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, (int, float)):
        return 'number'
    if isinstance(v, JsFunction):
        return 'function'
    return 'object'


def js_mod(a: int, b: int) -> int:
    """a % b with the sign of the dividend, as in JS."""
    return int(math.fmod(a, b))


def to_utf16(s: str) -> str:
    """Re-express s as UTF-16 code units (astral characters become surrogate pairs) so indexes match JS."""
    if all(ord(ch) <= 0xFFFF for ch in s):
        return s
    out = []
    for ch in s:
        o = ord(ch)
        if o > 0xFFFF:
            o -= 0x10000
            out.append(chr(0xD800 + (o >> 10)))
            out.append(chr(0xDC00 + (o & 0x3FF)))
        else:
            out.append(ch)
    return ''.join(out)


def from_utf16(s: str) -> str:
    """Inverse of to_utf16: rejoin surrogate pairs; lone surrogates are kept as they are."""
    if all(not 0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        return s
    return s.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass')
