"""
A small request-schema library with the semantics of the zod schemas in the TS API: defaults for missing keys,
optional and nullable fields, unknown keys stripped (or refused with strict=True), and every problem reported as a
ValidationError (400, {'error': 'validation', 'issues': [{path, message}]}).

    body = parse(Obj({'code': Str(min=1, max=20), 'nameNe': Str(max=120, optional=True)}), request_json)
"""
from __future__ import annotations

import math
import re
import uuid as _uuid
from datetime import date
from typing import Any, Callable

from .errors import ValidationError

MISSING: Any = type('Missing', (), {'__repr__': lambda s: 'MISSING', '__bool__': lambda s: False})()


class _Issue(Exception):
    def __init__(self, path: list, message: str):
        self.path, self.message = path, message


class _Issues(Exception):
    def __init__(self, issues: list[_Issue]):
        self.issues = issues


def _fail(path: list, message: str):
    raise _Issues([_Issue(path, message)])


class S:
    """Base schema. `default` fills a missing (or None-free undefined) value; `optional` lets it be absent; `nullable`
    accepts None; `nullish` is both. `transform` runs on the parsed value."""

    def __init__(self, *, default: Any = MISSING, optional: bool = False, nullable: bool = False,
                 nullish: bool = False, transform: Callable[[Any], Any] | None = None,
                 refine: tuple[Callable[[Any], bool], str] | None = None):
        self.default = default
        self.optional = optional or nullish
        self.nullable = nullable or nullish
        self.transform = transform
        self.refine = refine

    def run(self, v: Any, path: list) -> Any:
        if v is MISSING:
            if self.default is not MISSING:
                d = self.default() if callable(self.default) else self.default
                return self.transform(d) if self.transform else d
            if self.optional:
                return MISSING
            _fail(path, 'Required')
        if v is None:
            if self.nullable:
                return None
            _fail(path, 'Expected a value, received null')
        out = self.check(v, path)
        if self.refine and not self.refine[0](out):
            _fail(path, self.refine[1])
        return self.transform(out) if self.transform else out

    def check(self, v: Any, path: list) -> Any:
        return v


class Any_(S):
    pass


class Str(S):
    def __init__(self, *, min: int | None = None, max: int | None = None, regex: str | None = None,
                 regex_message: str | None = None, trim: bool = False, email: bool = False, **kw):
        super().__init__(**kw)
        self.min, self.max, self.trim, self.email = min, max, trim, email
        self.regex = re.compile(regex) if regex else None
        self.regex_message = regex_message or 'Invalid string'

    def check(self, v, path):
        if not isinstance(v, str):
            _fail(path, f'Expected string, received {_kind(v)}')
        if self.trim:
            v = v.strip()
        if self.min is not None and len(v) < self.min:
            _fail(path, f'Too small: expected string to have >={self.min} characters')
        if self.max is not None and len(v) > self.max:
            _fail(path, f'Too big: expected string to have <={self.max} characters')
        if self.email and not re.fullmatch(r"[A-Za-z0-9_'+\-.]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", v):
            _fail(path, 'Invalid email address')
        if self.regex and not self.regex.search(v):
            _fail(path, self.regex_message)
        return v


class Num(S):
    def __init__(self, *, min: float | None = None, max: float | None = None, int_: bool = False,
                 coerce: bool = False, **kw):
        super().__init__(**kw)
        self.min, self.max, self.int_, self.coerce = min, max, int_, coerce

    def check(self, v, path):
        if self.coerce and isinstance(v, str):
            try:
                v = float(v) if v.strip() else 0.0
            except ValueError:
                _fail(path, 'Expected number, received NaN')
            if v.is_integer():
                v = int(v)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            _fail(path, f'Expected number, received {_kind(v)}')
        if self.int_ and not float(v).is_integer():
            _fail(path, 'Expected integer, received float')
        if self.int_:
            v = int(v)
        if self.min is not None and v < self.min:
            _fail(path, f'Too small: expected number to be >={self.min}')
        if self.max is not None and v > self.max:
            _fail(path, f'Too big: expected number to be <={self.max}')
        return v


def Int(**kw) -> Num:
    return Num(int_=True, **kw)


class Bool(S):
    def __init__(self, *, coerce: bool = False, **kw):
        super().__init__(**kw)
        self.coerce = coerce

    def check(self, v, path):
        if self.coerce:
            return bool(v)  # z.coerce.boolean(): any non-empty string is true
        if not isinstance(v, bool):
            _fail(path, f'Expected boolean, received {_kind(v)}')
        return v


class Enum(S):
    def __init__(self, choices, **kw):
        super().__init__(**kw)
        self.choices = tuple(choices)

    def check(self, v, path):
        if not isinstance(v, str) or v not in self.choices:
            _fail(path, 'Invalid option: expected one of ' + '|'.join(f'"{c}"' for c in self.choices))
        return v


_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
                   r'|^00000000-0000-0000-0000-000000000000$|^ffffffff-ffff-ffff-ffff-ffffffffffff$')


class Uuid(S):
    def check(self, v, path):
        if not isinstance(v, str) or not _UUID.match(v):
            _fail(path, 'Invalid UUID')
        return v.lower()


def is_uuid(v: Any) -> bool:
    return isinstance(v, str) and bool(_UUID.match(v))


def valid_iso_date(s: Any) -> bool:
    if not isinstance(s, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return False
    try:
        return date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


class IsoDate(S):
    """'YYYY-MM-DD' that is a real calendar date (rejects 2026-13-40)."""

    def check(self, v, path):
        if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            _fail(path, 'YYYY-MM-DD')
        if not valid_iso_date(v):
            _fail(path, 'not a valid date')
        return v


class Hhmm(Str):
    def __init__(self, **kw):
        super().__init__(regex=r'^([01]\d|2[0-3]):[0-5]\d$', regex_message='HH:MM', **kw)


class Money(Str):
    """Decimal amount as a string (money never passes through a float)."""

    def __init__(self, **kw):
        super().__init__(regex=r'^-?\d{1,12}(\.\d{1,2})?$', regex_message='amount with up to 2 decimals', **kw)


class DateTimeStr(Str):
    def __init__(self, **kw):
        super().__init__(regex=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$',
                         regex_message='Invalid ISO datetime', **kw)


class Arr(S):
    def __init__(self, item: S, *, min: int | None = None, max: int | None = None, **kw):
        super().__init__(**kw)
        self.item, self.min, self.max = item, min, max

    def check(self, v, path):
        if not isinstance(v, (list, tuple)):
            _fail(path, f'Expected array, received {_kind(v)}')
        if self.min is not None and len(v) < self.min:
            _fail(path, f'Too small: expected array to have >={self.min} items')
        if self.max is not None and len(v) > self.max:
            _fail(path, f'Too big: expected array to have <={self.max} items')
        out, issues = [], []
        for i, x in enumerate(v):
            try:
                out.append(self.item.run(x, path + [i]))
            except _Issues as e:
                issues.extend(e.issues)
        if issues:
            raise _Issues(issues)
        return out


class Union(S):
    def __init__(self, *options: S, **kw):
        super().__init__(**kw)
        self.options = options

    def check(self, v, path):
        for o in self.options:
            try:
                return o.run(v, path)
            except _Issues:
                continue
        _fail(path, 'Invalid input')


class Record(S):
    """An object with arbitrary keys; `keys` limits them (z.partialRecord(z.enum(...), value))."""

    def __init__(self, value: S, *, keys: tuple[str, ...] | None = None, **kw):
        super().__init__(**kw)
        self.value, self.keys = value, keys

    def check(self, v, path):
        if not isinstance(v, dict):
            _fail(path, f'Expected object, received {_kind(v)}')
        out, issues = {}, []
        for k, x in v.items():
            if self.keys is not None and k not in self.keys:
                issues.append(_Issue(path + [k], 'Invalid key in record'))
                continue
            try:
                out[k] = self.value.run(x, path + [k])
            except _Issues as e:
                issues.extend(e.issues)
        if issues:
            raise _Issues(issues)
        return out


class Obj(S):
    def __init__(self, fields: dict[str, S], *, strict: bool = False, **kw):
        super().__init__(**kw)
        self.fields, self.strict = fields, strict

    def extend(self, fields: dict[str, S]) -> 'Obj':
        return Obj({**self.fields, **fields}, strict=self.strict)

    def check(self, v, path):
        if not isinstance(v, dict):
            _fail(path, f'Expected object, received {_kind(v)}')
        out, issues = {}, []
        if self.strict:
            for k in v:
                if k not in self.fields:
                    issues.append(_Issue(path, f'Unrecognized key: "{k}"'))
        for k, s in self.fields.items():
            try:
                x = s.run(v.get(k, MISSING), path + [k])
            except _Issues as e:
                issues.extend(e.issues)
                continue
            if x is not MISSING:
                out[k] = x
        if issues:
            raise _Issues(issues)
        return out


def _kind(v: Any) -> str:
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, (int, float)):
        return 'number'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, (list, tuple)):
        return 'array'
    if isinstance(v, dict):
        return 'object'
    return type(v).__name__


def parse(schema: S, data: Any) -> Any:
    """Validate and normalise `data`; raises ValidationError listing every problem."""
    try:
        out = schema.run(MISSING if data is MISSING else data, [])
    except _Issues as e:
        raise ValidationError([{'path': '.'.join(str(p) for p in i.path), 'message': i.message} for i in e.issues])
    return None if out is MISSING else out


def safe_parse(schema: S, data: Any) -> tuple[bool, Any]:
    try:
        return True, parse(schema, data)
    except ValidationError as e:
        return False, e


def parse_uuid(v: Any, what: str = 'id') -> str:
    """A path parameter that must be a UUID (z.uuid().parse(req.params.id))."""
    return parse(Uuid(), v)


def new_uuid() -> str:
    return str(_uuid.uuid4())
