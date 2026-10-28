"""
The attendance tools' request contract (the zod schemas of apps/api/src/modules/attendance-tools: policy.ts and
routes.ts) as DRF serializers. Field names are the TS body keys; `hajiri.core.validation.validate` reports problems
in the TS issue shape. `nudge_body` is the TS z.union of the two reminder kinds.
"""
from __future__ import annotations

from datetime import date

from rest_framework import serializers

from hajiri.core.errors import ValidationError
from hajiri.core.validation import IsoDateField, validate


class NumberField(serializers.FloatField):
    """A JSON number kept as the client sent it: whole numbers stay integers (1, not 1.0), as in the TS policy."""

    def to_internal_value(self, data):
        v = super().to_internal_value(data)
        return int(v) if float(v).is_integer() else v


class LatePenaltyIn(serializers.Serializer):
    enabled = serializers.BooleanField(default=False)
    lateCount = serializers.IntegerField(min_value=1, max_value=31, default=3)
    minLateMinutes = serializers.IntegerField(min_value=1, max_value=240, default=1)
    penaltyDays = NumberField(min_value=0.25, max_value=5, default=0.5)
    leaveTypeIds = serializers.ListField(child=serializers.UUIDField(), max_length=5, default=list)

    def validate_penaltyDays(self, v):
        if abs(v * 4 - round(v * 4)) >= 1e-9:
            raise serializers.ValidationError('Invalid number: must be a multiple of 0.25')
        return v


def default_late_penalty() -> dict:
    return validate(LatePenaltyIn, {})


class ExtrasPolicyIn(serializers.Serializer):
    blockLateClockIn = serializers.BooleanField(default=False)
    blockAfterMin = serializers.IntegerField(min_value=0, max_value=240, default=0)
    whosOutScope = serializers.ChoiceField(('team', 'department', 'company'), default='team')
    latePenalty = LatePenaltyIn(default=default_late_penalty)


def _year(**kw) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=2000, max_value=2100, **kw)


def _month(**kw) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=1, max_value=12, **kw)


class MonthIn(serializers.Serializer):
    bsYear = _year()
    bsMonth = _month()


class MonthPartialIn(serializers.Serializer):
    bsYear = _year(required=False)
    bsMonth = _month(required=False)


class ApplyPenaltiesIn(MonthIn):
    employeeIds = serializers.ListField(child=serializers.UUIDField(), max_length=2000, required=False)


class ReverseIn(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=300)


class IssueRangeIn(serializers.Serializer):
    """?from=&to= of at most 92 days."""

    def get_fields(self):
        return {'from': IsoDateField(), 'to': IsoDateField()}

    def validate(self, attrs):
        if attrs['from'] > attrs['to']:
            raise serializers.ValidationError('from must be on or before to')
        if (date.fromisoformat(attrs['to']) - date.fromisoformat(attrs['from'])).days > 92:
            raise serializers.ValidationError('pick at most 92 days')
        return attrs


class EmployeeNudgeIn(serializers.Serializer):
    target = serializers.ChoiceField(('employee',))
    employeeId = serializers.UUIDField()

    def get_fields(self):
        return {**super().get_fields(), 'from': IsoDateField(), 'to': IsoDateField()}


class ApproverNudgeIn(serializers.Serializer):
    target = serializers.ChoiceField(('approver',))
    instanceId = serializers.UUIDField()


def nudge_body(body) -> dict:
    """A reminder to an employee (fix the days) or to the approvers of a waiting request."""
    for variant in (EmployeeNudgeIn, ApproverNudgeIn):
        try:
            return validate(variant, body)
        except ValidationError:
            continue
    raise ValidationError([{'path': '', 'message': 'Invalid input'}])


class AttendanceUploadIn(serializers.Serializer):
    csv = serializers.CharField(min_length=1, max_length=1_000_000, trim_whitespace=False)
    filename = serializers.CharField(max_length=200, required=False, allow_blank=True, trim_whitespace=False)


class ImportIn(AttendanceUploadIn):
    reason = serializers.CharField(min_length=5, max_length=300)


class WhosOutIn(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=14, default=7)
