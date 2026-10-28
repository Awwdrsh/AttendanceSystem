"""
The request contract of time tracking (the zod schemas of apps/api/src/modules/timesheets/routes.ts) as DRF
serializers. Services validate every TS-shaped body or query with these through hajiri.core.validation.validate, so
page posts (read by forms.py) and JSON clients (api.py) are refused with the same rules and messages.
"""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField
from hajiri.db.models import TimeProject, Timesheet

DAY_LIMIT_MIN = 1440
ROUNDING_STEPS = (0, 5, 6, 10, 15)
ROUNDING_MODES = ('nearest', 'up', 'down')
SCOPES = ('own', 'team', 'all')
GROUP_BY = ('project', 'client', 'employee', 'activity', 'day')
STATUSES = tuple(Timesheet.Status.values)


def _note(**kw) -> serializers.CharField:
    """An optional free-text note: trimmed, at most 500 characters, '' and null allowed (zod .trim().max(500).nullish())."""
    return serializers.CharField(max_length=500, required=False, allow_null=True, allow_blank=True, **kw)


def _uuid_nullish() -> serializers.UUIDField:
    return serializers.UUIDField(required=False, allow_null=True)


# ---------------------------------------------------------------- policy

@extend_schema_field(OpenApiTypes.INT)
class RoundingStepField(serializers.Field):
    """One of the rounding steps as a number (booleans and strings are refused, as the TS refine did)."""
    default_error_messages = {'invalid': 'Invalid input'}

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, (int, float)) or data not in ROUNDING_STEPS:
            self.fail('invalid')
        return int(data)

    def to_representation(self, value):
        return value


class TimePolicyIn(serializers.Serializer):
    enabled = serializers.BooleanField(default=True)
    requireProject = serializers.BooleanField(default=False)
    # Jibble-style rounding applied to each entry in approvals and reports; the raw minutes are always kept.
    roundingStep = RoundingStepField(default=0)
    roundingMode = serializers.ChoiceField(ROUNDING_MODES, default='nearest')
    fridayReminder = serializers.BooleanField(default=True)
    # A timer left running longer than this is stopped at this length and the employee is told.
    autoStopHours = serializers.IntegerField(min_value=1, max_value=24, default=12)


# ---------------------------------------------------------------- catalogue

class ClientIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=120)
    notes = _note()


class ClientPatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=120, required=False)
    notes = _note()
    archived = serializers.BooleanField(required=False)


class ProjectIn(serializers.Serializer):
    """POST /time/projects; validated with partial=True for PATCH (zod .partial())."""
    code = serializers.RegexField(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$',
                                  error_messages={'invalid': 'letters, digits, - or _ (max 20)'})
    name = serializers.CharField(min_length=1, max_length=120)
    clientId = _uuid_nullish()
    color = serializers.RegexField(r'^#[0-9a-fA-F]{6}$', required=False, trim_whitespace=False)
    billable = serializers.BooleanField(required=False)
    billRate = serializers.FloatField(min_value=0, max_value=1_000_000, required=False, allow_null=True)
    budgetHours = serializers.FloatField(min_value=0, max_value=1_000_000, required=False, allow_null=True)
    allMembers = serializers.BooleanField(required=False)
    memberIds = serializers.ListField(child=serializers.UUIDField(), max_length=1000, required=False)
    status = serializers.ChoiceField(TimeProject.Status.values, required=False)

    def validate_budgetHours(self, v):
        if v is not None and not v > 0:
            raise serializers.ValidationError('Too small: expected number to be >0')
        return v


class ActivityQueryIn(serializers.Serializer):
    projectId = serializers.UUIDField(required=False)
    all = serializers.ChoiceField(('0', '1'), required=False)


class ActivityIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    projectId = _uuid_nullish()


class ActivityPatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    archived = serializers.BooleanField(required=False)


# ---------------------------------------------------------------- timer and entries

class TimerIn(serializers.Serializer):
    projectId = _uuid_nullish()
    activityId = _uuid_nullish()
    note = _note()
    billable = serializers.BooleanField(required=False)


class StopIn(serializers.Serializer):
    note = _note()


class EntryIn(serializers.Serializer):
    """POST /time/entries; validated with partial=True for PATCH."""
    date = IsoDateField()
    start = HhmmField(required=False)
    end = HhmmField(required=False)
    durationMin = serializers.IntegerField(min_value=1, max_value=DAY_LIMIT_MIN, required=False)
    projectId = _uuid_nullish()
    activityId = _uuid_nullish()
    note = _note()
    billable = serializers.BooleanField(required=False)


class EntriesQueryIn(serializers.Serializer):
    # `from` is a Python keyword: the field is declared below under its TS name.
    to = IsoDateField()
    employeeId = serializers.UUIDField(required=False)


EntriesQueryIn._declared_fields = {'from': IsoDateField(), **EntriesQueryIn._declared_fields}


# ---------------------------------------------------------------- weeks and timesheets

class WeekQueryIn(serializers.Serializer):
    weekStart = IsoDateField(required=False)
    date = IsoDateField(required=False)
    employeeId = serializers.UUIDField(required=False)


class SubmitIn(serializers.Serializer):
    weekStart = IsoDateField()
    note = _note()


class TimesheetsQueryIn(serializers.Serializer):
    status = serializers.ChoiceField(STATUSES, required=False)
    to = IsoDateField(required=False)
    scope = serializers.ChoiceField(SCOPES, required=False)
    employeeId = serializers.UUIDField(required=False)


TimesheetsQueryIn._declared_fields = {'from': IsoDateField(required=False), **TimesheetsQueryIn._declared_fields}


class TeamWeekQueryIn(serializers.Serializer):
    weekStart = IsoDateField(required=False)
    scope = serializers.ChoiceField(('team', 'all'), required=False)


class ReportQueryIn(serializers.Serializer):
    to = IsoDateField()
    groupBy = serializers.ChoiceField(GROUP_BY, default='project')
    employeeId = serializers.UUIDField(required=False)
    projectId = serializers.UUIDField(required=False)
    clientId = serializers.UUIDField(required=False)
    scope = serializers.ChoiceField(SCOPES, required=False)
    format = serializers.ChoiceField(('json', 'csv'), required=False)


ReportQueryIn._declared_fields = {'from': IsoDateField(), **ReportQueryIn._declared_fields}
