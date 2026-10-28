"""
The request contract of the shifts routes (the zod schemas of apps/api/src/modules/shifts/routes.ts) as DRF
serializers, with the TS body keys as field names. Services validate with `hajiri.core.validation.validate`; PATCH
bodies validate with `partial=True`, which (like zod `.partial()`) applies no defaults to fields that were not sent.

zod parity: strings are not trimmed (the TS schemas had no `.trim()`), numbers and booleans must be JSON numbers and
booleans (zod refused "5" and "true").
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField
from hajiri.db.models import Holiday, Shift


class StrictIntegerField(serializers.IntegerField):
    """z.number().int(): a JSON number, not a numeric string or a boolean."""

    def to_internal_value(self, data):
        if isinstance(data, (str, bool)):
            self.fail('invalid')
        return super().to_internal_value(data)


class StrictFloatField(serializers.FloatField):
    """z.number(): a JSON number."""

    def to_internal_value(self, data):
        if isinstance(data, (str, bool)):
            self.fail('invalid')
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """z.boolean(): true or false only."""

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail('invalid')
        return data


def _text(max_length: int, *, required: bool = True) -> serializers.CharField:
    """z.string().min(1).max(n) when required, z.string().max(n).optional() otherwise."""
    return serializers.CharField(max_length=max_length, trim_whitespace=False, required=required, allow_blank=not required)


def _minutes(max_value: int, default: int) -> StrictIntegerField:
    return StrictIntegerField(min_value=0, max_value=max_value, default=default)


# ---------------------------------------------------------------- shifts

class ShiftIn(serializers.Serializer):
    code = _text(20)
    nameEn = _text(80)
    nameNe = _text(80, required=False)
    start = HhmmField()
    end = HhmmField()
    graceInMin = _minutes(240, 0)
    graceOutMin = _minutes(240, 0)
    earlyInWindowMin = _minutes(720, 60)
    lateOutWindowMin = _minutes(720, 360)
    breakMinutes = _minutes(240, 0)
    halfDayThresholdMin = _minutes(720, 240)
    absentThresholdMin = _minutes(720, 120)
    pairingMode = serializers.ChoiceField(Shift.PairingMode.values, default=Shift.PairingMode.ALTERNATING)
    roundingMin = _minutes(60, 0)
    roundingDirection = serializers.ChoiceField(Shift.RoundingDirection.values, default=Shift.RoundingDirection.NEAREST)


class _KeywordFields(serializers.Serializer):
    """`from` is a Python keyword and cannot be a class attribute: subclasses list such fields in `keyword_fields`."""
    keyword_fields: dict = {}

    def get_fields(self):
        fields = super().get_fields()
        for name, make in self.keyword_fields.items():
            fields[name] = make()
        return fields


class AssignIn(_KeywordFields):
    employeeId = serializers.UUIDField()
    shiftId = serializers.UUIDField()
    keyword_fields = {'from': IsoDateField}


# ---------------------------------------------------------------- holidays

class HolidayIn(serializers.Serializer):
    localDate = IsoDateField()
    nameEn = _text(120)
    nameNe = _text(120, required=False)
    branchId = serializers.UUIDField(required=False, allow_null=True)
    genderScope = serializers.ChoiceField(Holiday.GenderScope.values, default=Holiday.GenderScope.ALL)
    optional = StrictBooleanField(default=False)


class HolidayQueryIn(_KeywordFields):
    branchId = serializers.UUIDField(required=False)
    keyword_fields = {'from': lambda: IsoDateField(required=False), 'to': lambda: IsoDateField(required=False)}


class ImportDefaultIn(serializers.Serializer):
    bsYear = StrictIntegerField()


# ---------------------------------------------------------------- locations

class LocationIn(serializers.Serializer):
    name = _text(80)
    branchId = serializers.UUIDField(required=False, allow_null=True)
    lat = StrictFloatField(min_value=-90, max_value=90)
    lng = StrictFloatField(min_value=-180, max_value=180)
    radiusM = StrictIntegerField(min_value=20, max_value=5000, default=150)


# ---------------------------------------------------------------- calendar and roster

class PublishIn(_KeywordFields):
    """{from, to}: both required."""
    keyword_fields = {'from': IsoDateField, 'to': IsoDateField}


class CalendarQueryIn(PublishIn):
    employeeId = serializers.UUIDField(required=False)


class RosterQueryIn(PublishIn):
    branchId = serializers.UUIDField(required=False)


class RosterCellIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    localDate = IsoDateField()
    shiftId = serializers.UUIDField(allow_null=True)


class RosterPutIn(serializers.Serializer):
    rows = serializers.ListField(child=RosterCellIn(), min_length=1, max_length=5000)


class RosterDeleteIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    localDate = IsoDateField()


class CopyWeekIn(serializers.Serializer):
    weekStart = IsoDateField()
