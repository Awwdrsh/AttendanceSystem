"""
The punch module's request contract (the zod schemas of apps/api/src/modules/punch/routes.ts) as DRF serializers.
Field names are the TS body keys; `hajiri.core.validation.validate` reports problems in the TS issue shape. The field
and kiosk modules reuse the geo fields and the photo limit from here.
"""
from __future__ import annotations

import math

from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField
from hajiri.db.models import PunchEvent

PHOTO_MAX_BYTES = 300 * 1024
PHOTO_FIELD_MAX = math.ceil(PHOTO_MAX_BYTES * 4 / 3) + 64

ISO_DATETIME = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$'


def lat_field(**kw) -> serializers.FloatField:
    return serializers.FloatField(min_value=-90, max_value=90, **kw)


def lng_field(**kw) -> serializers.FloatField:
    return serializers.FloatField(min_value=-180, max_value=180, **kw)


def accuracy_field(**kw) -> serializers.FloatField:
    return serializers.FloatField(min_value=0, max_value=10_000, **kw)


def photo_field() -> serializers.CharField:
    """A `data:image/...;base64,` photo (checked by services.parse_photo); untrimmed, like the zod string."""
    return serializers.CharField(max_length=PHOTO_FIELD_MAX, required=False, allow_blank=True, trim_whitespace=False)


class GeoIn(serializers.Serializer):
    """Optional position of a phone or browser (lat, lng, accuracyM)."""
    lat = lat_field(required=False)
    lng = lng_field(required=False)
    accuracyM = accuracy_field(required=False)


class PunchIn(GeoIn):
    direction = serializers.ChoiceField(PunchEvent.Direction.values, default=PunchEvent.Direction.UNKNOWN)
    clientTime = serializers.RegexField(ISO_DATETIME, error_messages={'invalid': 'Invalid ISO datetime'})
    idempotencyKey = serializers.CharField(min_length=8, max_length=64, trim_whitespace=False)
    mocked = serializers.BooleanField(required=False)
    selfie = photo_field()
    source = serializers.ChoiceField(('mobile', 'web'))
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, trim_whitespace=False)


class RangeIn(serializers.Serializer):
    """?from=&to=&employeeId= (inclusive Nepal dates). Subclasses add their own query fields."""
    employeeId = serializers.UUIDField(required=False)

    def get_fields(self):
        # `from` is a Python keyword, so it cannot be a class attribute.
        return {'from': IsoDateField(), 'to': IsoDateField(), **super().get_fields()}


class ManualIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    localDate = IsoDateField()
    time = HhmmField()
    direction = serializers.ChoiceField((PunchEvent.Direction.IN, PunchEvent.Direction.OUT))
    reason = serializers.CharField(min_length=3, max_length=300, trim_whitespace=False)
