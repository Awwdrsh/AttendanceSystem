"""
Request validation with DRF serializers, reporting problems in the TS API's shape.

Services and views validate a request body (a JSON object, or a dict built from a form post) with a DRF serializer
and get a plain dict back; every problem raises hajiri.core.errors.ValidationError, which the middleware and the DRF
exception handler answer as 400 `{"error": "validation", "issues": [{"path": "from", "message": "Required"}]}` (the
zod shape the TS clients read). DRF's default English messages are rewritten to the zod wording the TS API used
("Required", "Too big: expected string to have <=160 characters", "Invalid option: expected one of ...", "Invalid
UUID"); a message you write yourself is kept as is.

    class TicketIn(serializers.Serializer):
        categoryId = serializers.UUIDField(source='category_id')
        subject = serializers.CharField(min_length=3, max_length=160)       # trims, like zod .trim()
        priority = serializers.ChoiceField(HelpdeskTicket.Priority.choices, default='normal')
        fileIds = serializers.ListField(child=serializers.UUIDField(), max_length=5, default=list, source='file_ids')

    data = validate(TicketIn, body)        # {'category_id': UUID(...), 'subject': '...', 'priority': 'normal', ...}

Notes for zod parity:
- CharField trims surrounding whitespace by default; pass trim_whitespace=False where the zod schema had no .trim().
- A string with no minimum must allow '' (allow_blank=True); `nullish` is required=False + allow_null=True.
- Unknown keys are ignored (zod's default strip).
- Use the field helpers below for the TS string formats (IsoDateField, HhmmField, MoneyField).
"""
from __future__ import annotations

import re
from typing import Any

from rest_framework import fields as drf_fields
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

from .errors import ValidationError

__all__ = ['validate', 'issues_from', 'plain', 'parse_uuid', 'IsoDateField', 'HhmmField', 'MoneyField', 'choice_message',
           'Serializer']

Serializer = serializers.Serializer


def _default_message(field: drf_fields.Field, code: str) -> str | None:
    """The message DRF would use for this code if the field did not override it."""
    for klass in type(field).__mro__:
        msgs = getattr(klass, 'default_error_messages', None) or {}
        if code in msgs:
            return str(msgs[code])
    return None


def choice_message(choices) -> str:
    return 'Invalid option: expected one of ' + '|'.join(f'"{c}"' for c in choices)


def _zod(field: drf_fields.Field | None, err: ErrorDetail) -> str:
    """Rewrite DRF's default text for this error to the zod wording; custom messages pass through."""
    code = getattr(err, 'code', None)
    text = str(err)
    if field is None or code is None:
        return text
    own = field.error_messages.get(code)
    default = _default_message(field, code)
    if own is not None and default is not None and str(own) != default:
        return text                                     # the field declared its own message
    if code == 'required':
        return 'Required'
    if code == 'null':
        return 'Expected a value, received null'
    is_list = isinstance(field, (drf_fields.ListField, serializers.ListSerializer))
    if code == 'blank':
        return 'Too small: expected string to have >=1 characters'
    if code in ('max_length', 'min_length'):
        n = getattr(field, code, None)
        what = 'array to have' if is_list else 'string to have'
        unit = 'items' if is_list else 'characters'
        if code == 'max_length':
            return f'Too big: expected {what} <={n} {unit}'
        return f'Too small: expected {what} >={n} {unit}'
    if code == 'empty' and is_list:
        return 'Too small: expected array to have >=1 items'
    if code == 'max_value':
        return f'Too big: expected number to be <={_num(field.max_value)}'
    if code == 'min_value':
        return f'Too small: expected number to be >={_num(field.min_value)}'
    if code == 'invalid_choice':
        return choice_message(list(getattr(field, 'choices', {}) or {}))
    if code == 'invalid':
        if isinstance(field, drf_fields.UUIDField):
            return 'Invalid UUID'
        if isinstance(field, drf_fields.EmailField):
            return 'Invalid email address'
        if isinstance(field, drf_fields.URLField):
            return 'Invalid URL'
        if isinstance(field, drf_fields.BooleanField):
            return 'Expected boolean'
        if isinstance(field, (drf_fields.IntegerField, drf_fields.FloatField, drf_fields.DecimalField)):
            return 'Expected number, received NaN' if not isinstance(field, drf_fields.IntegerField) else 'Expected integer'
        if isinstance(field, drf_fields.CharField):
            return 'Expected string'
        if isinstance(field, drf_fields.DictField) or isinstance(field, serializers.Serializer):
            return 'Expected object'
    if code == 'not_a_list':
        return 'Expected array'
    if code == 'not_a_dict':
        return 'Expected object'
    return text


def _num(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def _child(field, key):
    if isinstance(field, serializers.Serializer):
        return field.fields.get(key) if isinstance(key, str) else None
    if isinstance(field, serializers.ListSerializer):
        return field.child
    if isinstance(field, (drf_fields.ListField, drf_fields.DictField)):
        return field.child
    return None


def issues_from(errors: Any, field: Any = None, path: tuple = ()) -> list[dict]:
    """Flatten DRF's nested error structure into [{path, message}] (dotted paths, list indexes as numbers)."""
    out: list[dict] = []
    if isinstance(errors, dict):
        for key, sub in errors.items():
            if key in ('non_field_errors', drf_fields.api_settings.NON_FIELD_ERRORS_KEY):
                out += issues_from(sub, None, path)
                continue
            child = _child(field, key) if field is not None else None
            out += issues_from(sub, child, path + (key,))
    elif isinstance(errors, list):
        for i, sub in enumerate(errors):
            if isinstance(sub, (dict, list)):
                child = _child(field, i) if field is not None else None
                out += issues_from(sub, child, path + (i,))
            else:
                out.append({'path': '.'.join(str(p) for p in path), 'message': _zod(field, sub)})
    else:
        out.append({'path': '.'.join(str(p) for p in path), 'message': _zod(field, errors)})
    return out


def validate(serializer: type[serializers.Serializer] | serializers.Serializer, data: Any, *, partial: bool = False,
             context: dict | None = None) -> dict:
    """Validated data (keys renamed by `source=`), or ValidationError with every problem in the TS issue shape."""
    s = serializer if isinstance(serializer, serializers.BaseSerializer) else serializer(data=data, partial=partial, context=context or {})
    if data is not None and not isinstance(data, dict) and not getattr(s, 'many', False):
        raise ValidationError([{'path': '', 'message': 'Expected object'}])
    if not s.is_valid():
        raise ValidationError(issues_from(s.errors, s))
    return dict(s.validated_data)


# ---------------------------------------------------------------- the TS string formats

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class IsoDateField(serializers.DateField):
    """'YYYY-MM-DD' that is a real calendar date (rejects 2026-13-40). The value is a datetime.date; pass
    as_string=True to keep the ISO string (what most TS-shaped services expect)."""

    def __init__(self, *, as_string: bool = True, **kw):
        self.as_string = as_string
        super().__init__(input_formats=['%Y-%m-%d'], **kw)

    def to_internal_value(self, value):
        if not isinstance(value, str) or not _ISO.match(value):
            self.fail('format')
        d = super().to_internal_value(value)
        return d.isoformat() if self.as_string else d

    default_error_messages = {'format': 'YYYY-MM-DD', 'invalid': 'not a valid date'}


class HhmmField(serializers.RegexField):
    """'HH:MM', 00:00 to 23:59."""

    def __init__(self, **kw):
        kw.setdefault('error_messages', {'invalid': 'HH:MM'})
        super().__init__(r'^([01]\d|2[0-3]):[0-5]\d$', **kw)


class MoneyField(serializers.RegexField):
    """A decimal amount as a string with up to 2 decimals (money never passes through a float)."""

    def __init__(self, **kw):
        kw.setdefault('error_messages', {'invalid': 'amount with up to 2 decimals'})
        super().__init__(r'^-?\d{1,12}(\.\d{1,2})?$', **kw)


# ---------------------------------------------------------------- small helpers

def plain(value: Any) -> Any:
    """Validated data with UUIDs as strings (lists and dicts too), the shape the TS services passed around."""
    import uuid as _uuid
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def parse_uuid(value: Any) -> str:
    """A path parameter that must be a UUID (z.uuid().parse(req.params.id)): the lower-case string, or
    ValidationError 'Invalid UUID'."""
    import uuid as _uuid
    try:
        return str(_uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError([{'path': '', 'message': 'Invalid UUID'}]) from None
