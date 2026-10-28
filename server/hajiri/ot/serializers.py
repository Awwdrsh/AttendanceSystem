"""
The overtime request contract (the zod schemas of apps/api/src/modules/ot/routes.ts) as DRF serializers. Field names
are the TS body and query keys, so JSON clients, the page forms and the services speak the same shape;
`hajiri.core.validation.validate` reports problems in the TS issue list.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import OtRequest, OtSlip

# Labour Act 2074 Sec 30(1): 4 h/day, 24 h/week. Never relaxed by settings.
CAP_DAY_MIN = 240
CAP_WEEK_MIN = 1440

SCOPES = ('own', 'team', 'all')
REGISTER_FORMATS = ('json', 'csv')


class OtRequestIn(serializers.Serializer):
    """POST /ot/requests."""
    employeeId = serializers.UUIDField(required=False)
    localDate = IsoDateField()
    requestedMin = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, trim_whitespace=False)


class _DateRange(serializers.Serializer):
    """Adds the `from` key (a Python keyword, so not a class attribute) before the declared fields."""
    from_required = False

    def get_fields(self):
        return {'from': IsoDateField(required=self.from_required), **super().get_fields()}


class OtRequestsQueryIn(_DateRange):
    """GET /ot/requests."""
    employeeId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(OtRequest.Status.values, required=False)
    to = IsoDateField(required=False)
    scope = serializers.ChoiceField(SCOPES, required=False)


class OtSlipsQueryIn(_DateRange):
    """GET /ot/slips."""
    employeeId = serializers.UUIDField(required=False)
    to = IsoDateField(required=False)
    status = serializers.ChoiceField(OtSlip.Status.values, required=False)


class OtApproveSlipIn(serializers.Serializer):
    """POST /ot/slips/:id/approve: without approvedMin the detected minutes are approved."""
    approvedMin = serializers.IntegerField(min_value=0, max_value=CAP_DAY_MIN, required=False)


class OtRejectSlipIn(serializers.Serializer):
    """POST /ot/slips/:id/reject: the employee sees the reason."""
    reason = serializers.CharField(min_length=3, max_length=500, trim_whitespace=False)


class OtRegisterQueryIn(_DateRange):
    """GET /ot/register."""
    from_required = True
    to = IsoDateField()
    format = serializers.ChoiceField(REGISTER_FORMATS, default='json')



# ---------------------------------------------------------------- response shapes (for the API schema)

class OtRequestOut(serializers.Serializer):
    id = serializers.UUIDField()
    employee_id = serializers.UUIDField()
    emp_code = serializers.CharField()
    name_en = serializers.CharField()
    local_date = serializers.DateField()
    requested_min = serializers.IntegerField()
    approved_min = serializers.IntegerField(allow_null=True)
    reason = serializers.CharField(allow_null=True)
    status = serializers.ChoiceField(OtRequest.Status.values)
    workflow_instance_id = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    decided_at = serializers.DateTimeField(allow_null=True)


class OtSlipOut(serializers.Serializer):
    id = serializers.UUIDField()
    employee_id = serializers.UUIDField()
    emp_code = serializers.CharField()
    name_en = serializers.CharField()
    local_date = serializers.DateField()
    detected_min = serializers.IntegerField()
    approved_min = serializers.IntegerField(allow_null=True)
    multiplier = serializers.DecimalField(max_digits=6, decimal_places=3)
    day_type = serializers.ChoiceField(OtSlip.DayType.values)
    status = serializers.ChoiceField(OtSlip.Status.values)
    posted_payslip_id = serializers.UUIDField(allow_null=True)
    reviewed_by = serializers.UUIDField(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()


class OtRequestsOut(serializers.Serializer):
    requests = OtRequestOut(many=True)


class OtSlipsOut(serializers.Serializer):
    slips = OtSlipOut(many=True)


class OtCreatedOut(serializers.Serializer):
    id = serializers.UUIDField()


class OtOkOut(serializers.Serializer):
    ok = serializers.BooleanField()


class OtApprovedOut(OtOkOut):
    approvedMin = serializers.IntegerField()


class OtCompOffOut(OtOkOut):
    days = serializers.FloatField()
    expiresOn = serializers.DateField()
