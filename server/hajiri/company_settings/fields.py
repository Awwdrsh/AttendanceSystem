"""
DRF fields for JSON bodies that must keep the TS (zod) strictness: a JSON number is not a string, a boolean is not
'yes', and an integer stays an int (a stored policy hashes the same way in both stacks). DRF's own IntegerField,
FloatField and BooleanField coerce strings, which the TS API refused.

    class PolicyIn(StrictSerializer):
        payDay = JsonNumber(integer=True, min_value=1, max_value=32, default=25)
        separateApprover = JsonBool(default=False)
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


def kind(v) -> str:
    """The JSON type name zod reports ('received string')."""
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
    return 'object'


def _n(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


@extend_schema_field(OpenApiTypes.BOOL)
class JsonBool(serializers.Field):
    """z.boolean(): only true or false."""

    def to_internal_value(self, v):
        if not isinstance(v, bool):
            raise serializers.ValidationError(f'Expected boolean, received {kind(v)}')
        return v

    def to_representation(self, value):
        return value


@extend_schema_field(OpenApiTypes.NUMBER)
class JsonNumber(serializers.Field):
    """z.number() (integer=True: .int()) with optional bounds. The value keeps its JSON type; `clamp_max` lowers a
    larger value to that cap instead of refusing it (statutory caps that policy may lower but never raise)."""

    def __init__(self, *, integer: bool = False, min_value=None, max_value=None, clamp_max=None, **kw):
        self.integer, self.min_value, self.max_value, self.clamp_max = integer, min_value, max_value, clamp_max
        super().__init__(**kw)

    def to_internal_value(self, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise serializers.ValidationError(f'Expected number, received {kind(v)}')
        if self.integer:
            if not float(v).is_integer():
                raise serializers.ValidationError('Expected integer, received float')
            v = int(v)
        if self.min_value is not None and v < self.min_value:
            raise serializers.ValidationError(f'Too small: expected number to be >={_n(self.min_value)}')
        if self.max_value is not None and v > self.max_value:
            raise serializers.ValidationError(f'Too big: expected number to be <={_n(self.max_value)}')
        if self.clamp_max is not None:
            v = min(v, self.clamp_max)
        return v

    def to_representation(self, value):
        return value


class JsonString(serializers.CharField):
    """z.string(): a JSON string only (DRF's CharField would turn 12 into '12'). No trimming unless asked;
    `pattern` is z.string().regex() ('Invalid string' unless `pattern_message`)."""

    def __init__(self, *, pattern: str | None = None, pattern_message: str = 'Invalid string', **kw):
        kw.setdefault('trim_whitespace', False)
        kw.setdefault('allow_blank', True)
        self.pattern = re.compile(pattern) if pattern else None
        self.pattern_message = pattern_message
        super().__init__(**kw)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError(f'Expected string, received {kind(data)}')
        out = super().to_internal_value(data)
        if self.pattern and not self.pattern.search(out):
            # A code of its own: the shared message mapper rewrites code 'invalid' on a CharField to 'Expected string'.
            raise serializers.ValidationError(self.pattern_message, code='invalid_format')
        return out


def plain_dict(v):
    """Validated data as plain dicts and lists (nested serializers give OrderedDicts)."""
    if isinstance(v, dict):
        return {k: plain_dict(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain_dict(x) for x in v]
    return v


class StrictSerializer(serializers.Serializer):
    """zod `.strict()`: keys the schema does not know are refused ('Unrecognized key: "x"')."""

    def to_internal_value(self, data):
        extra = [k for k in data if k not in self.fields] if isinstance(data, Mapping) else []
        problems = [f'Unrecognized key: "{k}"' for k in extra]
        try:
            out = super().to_internal_value(data)
        except serializers.ValidationError as e:
            if problems:
                detail = e.detail if isinstance(e.detail, dict) else {'non_field_errors': e.detail}
                raise serializers.ValidationError({'non_field_errors': problems, **detail}) from None
            raise
        if problems:
            raise serializers.ValidationError({'non_field_errors': problems})
        return out
