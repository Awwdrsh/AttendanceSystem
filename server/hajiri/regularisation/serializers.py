"""
The attendance-correction request contract (the zod schemas of apps/api/src/modules/regularisation/routes.ts and the
HR manual punch of punch/routes.ts) as DRF serializers. Field names are the TS body keys, so the JSON endpoints, the
page forms and the services speak one shape; `hajiri.core.validation.validate` reports problems in the zod wording.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField
from hajiri.db.models import RegularisationRequest

Kind = RegularisationRequest.Kind
Direction = RegularisationRequest.PunchDirection
Status = RegularisationRequest.Status
SCOPES = ('own', 'team', 'all')

# z.iso.datetime({offset: true}): a date-time with seconds optional, fraction optional and Z or an offset.
ISO_DATETIME = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$'


def _text(min_length: int, max_length: int, **kw) -> serializers.CharField:
    """z.string().min().max() without .trim(): the text is kept as sent; '' gets zod's own minimum message."""
    return serializers.CharField(min_length=min_length, max_length=max_length, trim_whitespace=False,
                                 error_messages={'blank': f'Too small: expected string to have >={min_length} characters'},
                                 **kw)


class DateTimeStrField(serializers.RegexField):
    """An ISO 8601 instant as a string (the value stays a string; the service parses it)."""

    def __init__(self, **kw):
        kw.setdefault('error_messages', {'invalid': 'Invalid ISO datetime'})
        super().__init__(ISO_DATETIME, trim_whitespace=False, **kw)


class CorrectionIn(serializers.Serializer):
    """POST /regularisation."""
    employeeId = serializers.UUIDField(required=False)
    localDate = IsoDateField()
    kind = serializers.ChoiceField(Kind.values)
    punchTime = DateTimeStrField(required=False)
    punchDirection = serializers.ChoiceField(Direction.values, required=False)
    shiftId = serializers.UUIDField(required=False)
    reason = _text(3, 1000)
    attachmentKey = serializers.CharField(max_length=200, required=False, allow_blank=True, trim_whitespace=False)


# GET /regularisation query; 'from' is a Python keyword, so the class is built with type().
CorrectionQueryIn = type('CorrectionQueryIn', (serializers.Serializer,), {
    '__doc__': 'GET /regularisation: ?employeeId=&status=&from=&to=&scope=',
    'employeeId': serializers.UUIDField(required=False),
    'status': serializers.ChoiceField(Status.values, required=False),
    'from': IsoDateField(required=False),
    'to': IsoDateField(required=False),
    'scope': serializers.ChoiceField(SCOPES, required=False),
})


class HrPunchIn(serializers.Serializer):
    """HR manual entry (TS POST /punch/manual)."""
    employeeId = serializers.UUIDField()
    localDate = IsoDateField()
    time = HhmmField()
    direction = serializers.ChoiceField(Direction.values)
    reason = _text(3, 300)


class OfflineClockIn(serializers.Serializer):
    """One clock-in queued on the phone while offline (lib/offlinePunch.ts)."""
    id = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    direction = serializers.ChoiceField(Direction.values)
    at = DateTimeStrField()
    localDate = IsoDateField()
    userId = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)


# ---------------------------------------------------------------- responses (for the schema)

class CorrectionCreatedOut(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField((Status.PENDING, Status.APPROVED))


class CorrectionOut(serializers.Serializer):
    id = serializers.UUIDField()
    employee_id = serializers.UUIDField()
    emp_code = serializers.CharField()
    name_en = serializers.CharField()
    local_date = serializers.DateField()
    kind = serializers.ChoiceField(Kind.values)
    punch_time = serializers.DateTimeField(allow_null=True)
    punch_direction = serializers.ChoiceField(Direction.values, allow_null=True)
    shift_id = serializers.UUIDField(allow_null=True)
    reason = serializers.CharField()
    attachment_key = serializers.CharField(allow_null=True)
    status = serializers.ChoiceField(Status.values)
    workflow_instance_id = serializers.UUIDField(allow_null=True)
    resulting_punch_id = serializers.UUIDField(allow_null=True)
    created_by = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    decided_at = serializers.DateTimeField(allow_null=True)


class CorrectionListOut(serializers.Serializer):
    requests = CorrectionOut(many=True)


class CorrectionCancelOut(serializers.Serializer):
    ok = serializers.BooleanField()
