"""
The kiosk tablet API's request contract (the zod schemas of apps/api/src/modules/kiosk/routes.ts) as DRF serializers.
`identify_body` is the TS z.union: an employee code with a PIN, or a scanned QR badge.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.errors import ValidationError
from hajiri.core.validation import validate
from hajiri.field.serializers import pin_field
from hajiri.punch.serializers import photo_field


class PairIn(serializers.Serializer):
    code = serializers.CharField(min_length=4, max_length=20, trim_whitespace=False)


class PinIdentifyIn(serializers.Serializer):
    empCode = serializers.CharField(min_length=1, max_length=32)
    pin = pin_field()


class QrIdentifyIn(serializers.Serializer):
    qr = serializers.CharField(min_length=10, max_length=200, trim_whitespace=False)


def identify_body(body) -> dict:
    """The first variant that fits (code and PIN, then QR), else the zod union's 'Invalid input'."""
    for variant in (PinIdentifyIn, QrIdentifyIn):
        try:
            return validate(variant, body)
        except ValidationError:
            continue
    raise ValidationError([{'path': '', 'message': 'Invalid input'}])


class KioskPunchIn(serializers.Serializer):
    ticket = serializers.CharField(min_length=20, max_length=200, trim_whitespace=False)
    selfie = photo_field()
