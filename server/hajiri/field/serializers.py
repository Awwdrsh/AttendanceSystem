"""
The field module's request contract (the zod schemas of apps/api/src/modules/field: routes.ts, policy.ts, swaps.ts)
as DRF serializers. Field names are the TS body keys; `hajiri.core.validation.validate` reports problems in the TS
issue shape. The kiosk PIN rule is shared with the kiosk tablet API.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import WorkBreak
from hajiri.punch.serializers import GeoIn, RangeIn, lat_field, lng_field, photo_field

BREAK_KINDS = tuple(WorkBreak.Kind.values)                 # lunch, tea, prayer, personal
BREAK_DEFAULTS = {'lunch': (False, 60), 'tea': (True, 15), 'prayer': (True, 15), 'personal': (False, 15)}
GEOFENCE_MODES = ('off', 'flag', 'block')


def default_breaks() -> dict:
    return {k: {'enabled': True, 'paid': paid, 'maxMin': max_min} for k, (paid, max_min) in BREAK_DEFAULTS.items()}


def _break_rule(kind: str) -> serializers.Serializer:
    paid, max_min = BREAK_DEFAULTS[kind]
    rule = type(f'{kind.title()}BreakIn', (serializers.Serializer,), {
        'enabled': serializers.BooleanField(default=True),
        'paid': serializers.BooleanField(default=paid),
        'maxMin': serializers.IntegerField(min_value=5, max_value=180, default=max_min),
    })
    return rule(default=lambda: {'enabled': True, 'paid': paid, 'maxMin': max_min})


class BreaksIn(serializers.Serializer):
    lunch = _break_rule('lunch')
    tea = _break_rule('tea')
    prayer = _break_rule('prayer')
    personal = _break_rule('personal')


class FieldPolicyIn(serializers.Serializer):
    geofenceMode = serializers.ChoiceField(GEOFENCE_MODES, default='flag')
    breaks = BreaksIn(default=default_breaks)
    fieldPhotoRequired = serializers.BooleanField(default=False)
    fieldVisitClockIn = serializers.BooleanField(default=True)


class BreakStartIn(serializers.Serializer):
    kind = serializers.ChoiceField(BREAK_KINDS)


class ReportQueryIn(RangeIn):
    """?from=&to=&employeeId=&format=json|csv for the breaks and visits reports."""
    format = serializers.ChoiceField(('json', 'csv'), required=False)


class BreaksQueryIn(ReportQueryIn):
    overOnly = serializers.ChoiceField(('1', 'true'), required=False)


class VisitStartIn(GeoIn):
    place = serializers.CharField(min_length=1, max_length=160)
    purpose = serializers.CharField(max_length=500, required=False, allow_blank=True)
    photo = photo_field()


class VisitEndIn(GeoIn):
    outcome = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class FieldLocationIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    address = serializers.CharField(max_length=300, required=False, allow_null=True, allow_blank=True)
    branchId = serializers.UUIDField(required=False, allow_null=True)
    lat = lat_field()
    lng = lng_field()
    radiusM = serializers.IntegerField(min_value=20, max_value=5000, default=150)
    active = serializers.BooleanField(default=True)


class StaffIn(serializers.Serializer):
    fieldWork = serializers.BooleanField(required=False)
    locationIds = serializers.ListField(child=serializers.UUIDField(), max_length=50, required=False)


class KioskIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    branchId = serializers.UUIDField(required=False, allow_null=True)
    locationId = serializers.UUIDField(required=False, allow_null=True)
    selfieRequired = serializers.BooleanField(default=True)


def pin_field() -> serializers.RegexField:
    return serializers.RegexField(r'^\d{4,6}$', error_messages={'invalid': 'PIN is 4 to 6 digits'}, trim_whitespace=False)


class PinIn(serializers.Serializer):
    pin = pin_field()


class RosterQueryIn(serializers.Serializer):
    def get_fields(self):
        return {'from': IsoDateField(), 'to': IsoDateField()}


class DateQueryIn(serializers.Serializer):
    date = IsoDateField()


class SwapsQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(('mine', 'team'), default='mine')

    def get_fields(self):
        return {**super().get_fields(), 'from': IsoDateField(required=False), 'to': IsoDateField(required=False)}


class SwapIn(serializers.Serializer):
    colleagueId = serializers.UUIDField()
    localDate = IsoDateField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class SwapNoteIn(serializers.Serializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)
