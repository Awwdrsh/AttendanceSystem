"""
The request contracts of the device and biometric services (the zod schemas of devices/routes.ts and
biometrics/routes.ts), as DRF serializers validated with hajiri.core.validation.validate. The TS strings were not
trimmed, so no CharField here trims either: a PIN of '7\n' must be refused, not cleaned up.
"""
from rest_framework import serializers

from hajiri.core.validation import IsoDateField

from .constants import COMMANDS, PIN_MESSAGE, PIN_RE

MODALITIES = ('face', 'fingerprint', 'palm', 'card')


class ActivateIn(serializers.Serializer):
    branchId = serializers.UUIDField(required=False)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, trim_whitespace=False)


class MapIn(serializers.Serializer):
    pin = serializers.RegexField(PIN_RE, trim_whitespace=False, error_messages={'invalid': PIN_MESSAGE})
    employeeId = serializers.UUIDField()
    syncToDevice = serializers.BooleanField(default=True)
    replace = serializers.BooleanField(default=False)

    def get_fields(self):
        # `from` is a Python keyword, so it cannot be a class attribute.
        return {**super().get_fields(), 'from': IsoDateField()}


class CommandIn(serializers.Serializer):
    command = serializers.ChoiceField(COMMANDS)


class ConsentIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    purpose = serializers.CharField(min_length=1, max_length=200, default='attendance', trim_whitespace=False)
    modality = serializers.ChoiceField(MODALITIES)
    noticeVersion = serializers.CharField(min_length=1, max_length=40, trim_whitespace=False)
    signature = serializers.CharField(max_length=4000, required=False, allow_blank=True, trim_whitespace=False)


class EmployeeQuery(serializers.Serializer):
    employeeId = serializers.UUIDField()


class RetentionRunIn(serializers.Serializer):
    templateGraceDays = serializers.IntegerField(min_value=0, max_value=365, default=30)


__all__ = ['ActivateIn', 'MapIn', 'CommandIn', 'ConsentIn', 'EmployeeQuery', 'RetentionRunIn', 'MODALITIES']
