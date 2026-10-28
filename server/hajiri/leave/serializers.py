"""
Request contracts of the leave API (the TS zod schemas of modules/leave/routes.ts) as DRF serializers, validated with
hajiri.core.validation.validate so problems come back in the zod wording. Pages build the same bodies in forms.py, so
a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

import math

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField
from hajiri.db.models import LeaveLedger, LeaveRequest, LeaveType


def _kind(v) -> str:
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


def _n(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


@extend_schema_field(OpenApiTypes.NUMBER)
class NumberField(serializers.Field):
    """A JSON number (zod z.number()): strings and booleans are refused; `integer=True` is z.number().int()."""

    def __init__(self, *, min_value=None, max_value=None, integer: bool = False, **kw):
        self.min_value, self.max_value, self.integer = min_value, max_value, integer
        super().__init__(**kw)

    def to_internal_value(self, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise serializers.ValidationError(f'Expected number, received {_kind(v)}')
        if self.integer and not float(v).is_integer():
            raise serializers.ValidationError('Expected integer, received float')
        if self.integer:
            v = int(v)
        if self.min_value is not None and v < self.min_value:
            raise serializers.ValidationError(f'Too small: expected number to be >={_n(self.min_value)}')
        if self.max_value is not None and v > self.max_value:
            raise serializers.ValidationError(f'Too big: expected number to be <={_n(self.max_value)}')
        return v

    def to_representation(self, value):
        return value


@extend_schema_field(OpenApiTypes.BOOL)
class BoolField(serializers.Field):
    """z.boolean(): only true and false."""

    def to_internal_value(self, v):
        if not isinstance(v, bool):
            raise serializers.ValidationError(f'Expected boolean, received {_kind(v)}')
        return v

    def to_representation(self, value):
        return value


def _text(max_length: int, min_length: int | None = None, **kw) -> serializers.CharField:
    """z.string() with limits; zod did not trim these."""
    return serializers.CharField(max_length=max_length, min_length=min_length, allow_blank=min_length is None,
                                 trim_whitespace=False, **kw)


def _nullish(field_cls, **kw):
    return field_cls(required=False, allow_null=True, **kw)


HALF_DAYS = LeaveRequest.HalfDay.values
STATUSES = LeaveRequest.Status.values
SCOPES = ('own', 'team', 'all')
ADJUST_KINDS = tuple(r.value for r in (LeaveLedger.Reason.ADJUSTMENT, LeaveLedger.Reason.ALLOCATION,
                                      LeaveLedger.Reason.CARRY_FORWARD))


# ---------------------------------------------------------------- leave types

class TypeIn(serializers.Serializer):
    """typeBody. Validate with partial=True for PATCH: only the keys that were sent are kept, no defaults."""
    code = serializers.CharField(min_length=1, max_length=20, trim_whitespace=False)
    nameEn = serializers.CharField(min_length=1, max_length=80, trim_whitespace=False)
    nameNe = _text(80, required=False)
    paid = BoolField(default=True)
    accrualBasis = serializers.ChoiceField(LeaveType.AccrualBasis.values, default=LeaveType.AccrualBasis.NONE.value)
    accrualDays = NumberField(min_value=0, default=0)
    accrualPerWorkedDays = _nullish(NumberField, integer=True, min_value=1)
    maxBalance = _nullish(NumberField, min_value=0)
    carryForward = BoolField(default=True)
    encashable = BoolField(default=False)
    certificateAfterDays = _nullish(NumberField, integer=True, min_value=0)
    allowHalfDay = BoolField(default=True)
    maxConsecutiveDays = _nullish(NumberField, integer=True, min_value=1)
    minServiceDays = NumberField(integer=True, min_value=0, default=0)
    negativeBalanceLimit = NumberField(min_value=0, default=0)
    genderScope = serializers.ChoiceField(LeaveType.GenderScope.values, default=LeaveType.GenderScope.ALL.value)
    sandwichRule = BoolField(default=False)
    expiryDays = _nullish(NumberField, integer=True, min_value=1)
    eventDays = _nullish(NumberField, min_value=0.5, max_value=365)
    allowHourly = BoolField(default=False)
    clubWithId = _nullish(serializers.UUIDField)
    sortOrder = NumberField(integer=True, min_value=0, max_value=9999, required=False)


class OrderIn(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=100)


# ---------------------------------------------------------------- balances, preview, requests

class BalancesQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    asOf = IsoDateField(required=False)


class PreviewQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    leaveTypeId = serializers.UUIDField()
    to = IsoDateField()
    halfDay = serializers.ChoiceField(HALF_DAYS, required=False)
    fromTime = HhmmField(required=False)
    toTime = HhmmField(required=False)

    def get_fields(self):
        fields = super().get_fields()
        fields['from'] = IsoDateField()            # a Python keyword, so declared here
        return fields


class RequestIn(PreviewQueryIn):
    reason = _text(1000, required=False)
    attachmentKey = _text(200, required=False)
    override = BoolField(required=False)


class ListQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(STATUSES, required=False)
    to = IsoDateField(required=False)
    scope = serializers.ChoiceField(SCOPES, required=False)

    def get_fields(self):
        fields = super().get_fields()
        fields['from'] = IsoDateField(required=False)
        return fields


class CancelIn(serializers.Serializer):
    reason = _text(500, required=False)


class AdjustIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    leaveTypeId = serializers.UUIDField()
    deltaDays = NumberField(min_value=-365, max_value=365)
    reason = _text(500, min_length=3)
    effectiveDate = IsoDateField(required=False)
    kind = serializers.ChoiceField(ADJUST_KINDS, default=LeaveLedger.Reason.ADJUSTMENT.value)

    def validate_deltaDays(self, v):
        if v == 0:
            raise serializers.ValidationError('deltaDays cannot be 0')
        return v


class LedgerQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    leaveTypeId = serializers.UUIDField(required=False)


class TeamCalendarQueryIn(serializers.Serializer):
    to = IsoDateField()

    def get_fields(self):
        fields = super().get_fields()
        fields['from'] = IsoDateField()
        return fields
