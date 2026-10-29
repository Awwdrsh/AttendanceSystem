"""
The request contract of the employee relations API (the zod schemas of apps/api/src/modules/relations/routes.ts) as DRF
serializers. Field names are the TS body keys; services validate with hajiri.core.validation.validate, which reports
problems in the zod wording, so a page post and a JSON body are refused with the same messages.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import (
    DisciplinaryCase, DisciplinaryStage, EmployeeTempAssignment, Grievance, KyeCampaign, KyeResponse, RegisteredDevice,
)

KYE_SECTIONS = ('personal', 'contact', 'emergency', 'family', 'education', 'experience', 'ids', 'bank', 'photo')
DEFAULT_SERVICE_YEARS = (1, 5, 10, 15, 20, 25, 30)


def Text(max_length: int, **kw) -> serializers.CharField:
    """z.string().trim().min(1).max(n)"""
    return serializers.CharField(max_length=max_length, **kw)


def OptionalText(max_length: int) -> serializers.CharField:
    """z.string().trim().max(n).nullish()"""
    return serializers.CharField(max_length=max_length, required=False, allow_null=True, allow_blank=True)


def Uuids(max_length: int | None = None, **kw) -> serializers.ListField:
    return serializers.ListField(child=serializers.UUIDField(), max_length=max_length, **kw)


def NullableUuid() -> serializers.UUIDField:
    """z.uuid().nullish()"""
    return serializers.UUIDField(required=False, allow_null=True)


class IsoDateTimeField(serializers.RegexField):
    """z.iso.datetime({offset: true}); the value stays the string the client sent."""

    def __init__(self, **kw):
        kw.setdefault('error_messages', {'invalid': 'Invalid ISO datetime'})
        super().__init__(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$', trim_whitespace=False, **kw)


class StrictSerializer(serializers.Serializer):
    """z.object(...).strict(): keys the schema does not name are refused."""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = [k for k in data if k not in self.fields]
            if unknown:
                raise serializers.ValidationError({'non_field_errors': [f'Unrecognized key: "{k}"' for k in unknown]})
        return super().to_internal_value(data)


# ---------------------------------------------------------------- policy

@extend_schema_serializer(component_name='RelationsPolicyIn')
class PolicyIn(serializers.Serializer):
    retirementAge = serializers.IntegerField(min_value=40, max_value=80, default=58)
    serviceYears = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=60), max_length=20,
                                         default=lambda: list(DEFAULT_SERVICE_YEARS))
    registeredDevicesOnly = serializers.BooleanField(default=False)
    appealDays = serializers.IntegerField(min_value=1, max_value=90, default=15)


# ---------------------------------------------------------------- grievances

class CommitteeIn(serializers.Serializer):
    userIds = Uuids(max_length=20)


class GrievanceIn(serializers.Serializer):
    category = serializers.ChoiceField(Grievance.Category.values)
    subject = Text(160)
    description = Text(8000)
    concerns = OptionalText(500)
    fileIds = Uuids(max_length=5, default=list)
    excludeUserIds = Uuids(max_length=10, default=list)


class GrievanceQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(('mine', 'handle'), default='mine')
    status = serializers.CharField(max_length=40, required=False, trim_whitespace=False)


@extend_schema_serializer(component_name='RelationsNoteIn')
class NoteIn(serializers.Serializer):
    body = Text(4000)
    shared = serializers.BooleanField(default=False)


class GrievanceUpdateIn(StrictSerializer):
    status = serializers.ChoiceField((Grievance.Status.UNDER_REVIEW, Grievance.Status.HEARING_SCHEDULED), required=False)
    hearingAt = IsoDateTimeField(required=False, allow_null=True)
    hearingPlace = OptionalText(200)
    excludeUserIds = Uuids(max_length=20, required=False)


class GrievanceDecisionIn(serializers.Serializer):
    outcome = serializers.ChoiceField(Grievance.Outcome.values)
    findings = Text(8000)


class AppealIn(serializers.Serializer):
    reason = Text(4000)


class AppealDecisionIn(serializers.Serializer):
    outcome = serializers.ChoiceField(Grievance.AppealOutcome.values)
    note = Text(4000)
    newOutcome = serializers.ChoiceField(Grievance.Outcome.values, required=False)


# ---------------------------------------------------------------- disciplinary action

class StageIn(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    code = serializers.RegexField(r'^[a-z0-9_]{2,40}$', trim_whitespace=False, error_messages={'invalid': 'Invalid string'})
    name = Text(80)
    severity = serializers.IntegerField(min_value=1, max_value=9)
    effect = serializers.ChoiceField(DisciplinaryStage.Effect.values)
    letterTemplateCode = OptionalText(40)
    active = serializers.BooleanField(default=True)


class StagesIn(serializers.Serializer):
    stages = StageIn(many=True, min_length=1, max_length=20)


class CasesQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    status = serializers.CharField(max_length=20, required=False, trim_whitespace=False)


class CaseIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    category = serializers.ChoiceField(DisciplinaryCase.Category.values)
    incidentDate = IsoDateField()
    incident = Text(8000)


class ShowCauseIn(serializers.Serializer):
    text = Text(8000)
    responseDue = IsoDateField()


class CaseResponseIn(serializers.Serializer):
    response = Text(8000)


class CaseDecisionIn(serializers.Serializer):
    stageId = serializers.UUIDField(allow_null=True)
    note = Text(8000)
    suspensionFrom = IsoDateField(required=False)
    suspensionTo = IsoDateField(required=False)
    lastWorkingDay = IsoDateField(required=False)
    # Decide before the response deadline (e.g. the employee admitted the incident in a hearing).
    force = serializers.BooleanField(default=False)


class LetterLinkIn(serializers.Serializer):
    letterId = serializers.UUIDField()


class ReasonIn(serializers.Serializer):
    reason = Text(1000)


# ---------------------------------------------------------------- completeness, milestones, contract

class EmployeeQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)


class MilestonesQueryIn(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=366, default=60)


class ContractIn(serializers.Serializer):
    contractEnd = IsoDateField(allow_null=True)


# ---------------------------------------------------------------- acting and deputation

ASSIGNMENT_STATUSES = (*EmployeeTempAssignment.Status.values, 'all')


class AssignmentsQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(ASSIGNMENT_STATUSES, default=EmployeeTempAssignment.Status.ACTIVE)


@extend_schema_serializer(component_name='RelationsAssignmentIn')
class AssignmentIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    kind = serializers.ChoiceField(EmployeeTempAssignment.Kind.values)
    designationId = NullableUuid()
    designation = OptionalText(120)
    branchId = NullableUuid()
    orgUnitId = NullableUuid()
    startDate = IsoDateField()
    endDate = IsoDateField()
    note = OptionalText(500)


class AssignmentEndIn(serializers.Serializer):
    endDate = IsoDateField(required=False)
    cancel = serializers.BooleanField(default=False)


# ---------------------------------------------------------------- Know Your Employee

class CampaignIn(serializers.Serializer):
    title = Text(160)
    message = OptionalText(2000)
    sections = serializers.ListField(child=serializers.ChoiceField(KYE_SECTIONS), min_length=1)
    audienceKind = serializers.ChoiceField(KyeCampaign.AudienceKind.values, default=KyeCampaign.AudienceKind.ALL)
    audienceId = NullableUuid()
    dueDate = IsoDateField()


class KyeResponseIn(serializers.Serializer):
    status = serializers.ChoiceField((KyeResponse.Status.CONFIRMED, KyeResponse.Status.CHANGES_REQUESTED))
    note = OptionalText(1000)


# ---------------------------------------------------------------- pop-ups and devices

class DismissIn(serializers.Serializer):
    acknowledge = serializers.BooleanField(default=False)


class DeviceIn(serializers.Serializer):
    token = serializers.RegexField(r'^[A-Za-z0-9_-]+$', min_length=16, max_length=200, trim_whitespace=False,
                                   error_messages={'invalid': 'Invalid string'})
    label = Text(80)


DEVICE_STATUSES = (*RegisteredDevice.Status.values, 'all')
DEVICE_ACTIONS = ('approve', 'reject', 'revoke')


class DevicesQueryIn(serializers.Serializer):
    status = serializers.ChoiceField(DEVICE_STATUSES, default='all')
