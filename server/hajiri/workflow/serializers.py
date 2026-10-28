"""
Request contracts of the workflow app (the TS zod schemas of modules/workflow/routes.ts and flow-routes.ts) as DRF
serializers, validated with hajiri.core.validation.validate so problems come back in the zod wording.
"""
from __future__ import annotations

import math

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from hajiri.core.validation import IsoDateField

APPROVER_KINDS = ('manager', 'manager2', 'department_head', 'branch_head', 'role', 'user', 'group')
STEP_ROLES = ('hr', 'admin', 'payroll_admin', 'attendance_admin', 'recruiter')
CONDITION_KEYS = ('employeeIds', 'groupIds', 'designationIds', 'orgUnitIds', 'branchIds')


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


@extend_schema_field(OpenApiTypes.NUMBER)
class NumberField(serializers.Field):
    """A JSON number (zod z.number()): strings are refused unless `coerce` (z.coerce.number() for query strings).
    Integral values come back as int, `integer=True` refuses fractions."""

    def __init__(self, *, min_value=None, max_value=None, integer: bool = False, coerce: bool = False, **kw):
        self.min_value, self.max_value, self.integer, self.coerce = min_value, max_value, integer, coerce
        super().__init__(**kw)

    def to_internal_value(self, v):
        if self.coerce and isinstance(v, str):
            try:
                v = float(v) if v.strip() else 0.0
            except ValueError:
                raise serializers.ValidationError('Expected number, received NaN') from None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise serializers.ValidationError(f'Expected number, received {_kind(v)}')
        if self.integer and not float(v).is_integer():
            raise serializers.ValidationError('Expected integer, received float')
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if self.min_value is not None and v < self.min_value:
            raise serializers.ValidationError(f'Too small: expected number to be >={_n(self.min_value)}')
        if self.max_value is not None and v > self.max_value:
            raise serializers.ValidationError(f'Too big: expected number to be <={_n(self.max_value)}')
        return v

    def to_representation(self, value):
        return value


def _n(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


@extend_schema_field(OpenApiTypes.BOOL)
class StrictBooleanField(serializers.Field):
    """z.boolean(): only true/false."""

    def to_internal_value(self, v):
        if not isinstance(v, bool):
            raise serializers.ValidationError(f'Expected boolean, received {_kind(v)}')
        return v

    def to_representation(self, value):
        return value


def _uuids(max_length: int, **kw):
    return serializers.ListField(child=serializers.UUIDField(), max_length=max_length, **kw)


# ---------------------------------------------------------------- flows

class StepIn(serializers.Serializer):
    """One step of a chain: who approves, plus the field its kind needs (checked in flows.parse_steps)."""
    kind = serializers.ChoiceField(APPROVER_KINDS)
    role = serializers.ChoiceField(STEP_ROLES, required=False)
    userId = serializers.UUIDField(required=False)
    groupId = serializers.UUIDField(required=False)
    minAmount = NumberField(min_value=0, max_value=1e10, required=False)
    label = serializers.CharField(max_length=60, required=False, allow_blank=True)


class ConditionsIn(serializers.Serializer):
    """Who a conditional flow is for; an empty list matches everyone."""
    employeeIds = _uuids(500, default=list)
    groupIds = _uuids(50, default=list)
    designationIds = _uuids(100, default=list)
    orgUnitIds = _uuids(100, default=list)
    branchIds = _uuids(100, default=list)


class FlowIn(serializers.Serializer):
    """POST /workflow/flows (objectType and steps are read separately)."""
    name = serializers.CharField(min_length=1, max_length=100)
    conditions = ConditionsIn(required=False)
    priority = NumberField(min_value=-100, max_value=100, integer=True, default=0)
    active = StrictBooleanField(default=True)


class FlowPatchIn(serializers.Serializer):
    """PATCH /workflow/flows/:id: any of the fields, none defaulted."""
    name = serializers.CharField(min_length=1, max_length=100, required=False)
    conditions = ConditionsIn(required=False)
    priority = NumberField(min_value=-100, max_value=100, integer=True, required=False)
    active = StrictBooleanField(required=False)


class DefaultStepsIn(serializers.Serializer):
    steps = serializers.ListField(child=serializers.DictField())


# ---------------------------------------------------------------- user groups

class GroupIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    description = serializers.CharField(max_length=300, required=False, allow_null=True, allow_blank=True)
    userIds = _uuids(500, default=list)


class GroupPatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    description = serializers.CharField(max_length=300, required=False, allow_null=True, allow_blank=True)
    userIds = _uuids(500, required=False)


# ---------------------------------------------------------------- delegation

class DelegateIn(serializers.Serializer):
    userId = serializers.UUIDField(required=False)
    delegateId = serializers.UUIDField()
    locals()['from'] = IsoDateField()          # `from` is a Python keyword
    to = IsoDateField()
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


# ---------------------------------------------------------------- preview, verify, decisions

class PreviewQueryIn(serializers.Serializer):
    objectType = serializers.CharField(trim_whitespace=False, allow_blank=True)
    employeeId = serializers.UUIDField(required=False)
    amount = NumberField(min_value=0, max_value=1e10, coerce=True, required=False)


class DecisionIn(serializers.Serializer):
    """POST /workflow/:id/approve|reject."""
    reason = serializers.CharField(max_length=1000, required=False, trim_whitespace=False, allow_blank=True)
    approvedMin = NumberField(min_value=0, max_value=240, integer=True, required=False)
    onBehalfOf = serializers.UUIDField(required=False)


class DecisionPageIn(DecisionIn):
    """The Approve/Reject buttons of the Approvals page: the OT minutes box posts text."""
    approvedMin = NumberField(min_value=0, max_value=240, integer=True, coerce=True, required=False)


class BulkApproveIn(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=200)


# ---------------------------------------------------------------- responses (for the API schema)

class StateOut(serializers.Serializer):
    state = serializers.ChoiceField(('pending', 'approved', 'rejected', 'cancelled'))


class BulkResultOut(serializers.Serializer):
    id = serializers.CharField()
    state = serializers.CharField(required=False)
    error = serializers.CharField(required=False)


class BulkOut(serializers.Serializer):
    results = BulkResultOut(many=True)


class InboxItemOut(serializers.Serializer):
    instanceId = serializers.CharField()
    objectType = serializers.CharField()
    objectId = serializers.CharField()
    step = serializers.IntegerField()
    totalSteps = serializers.IntegerField()
    createdAt = serializers.DateTimeField()
    employee = serializers.DictField(allow_null=True)
    summary = serializers.DictField()
    routing = serializers.ListField(child=serializers.CharField())


class InboxOut(serializers.Serializer):
    items = InboxItemOut(many=True)
