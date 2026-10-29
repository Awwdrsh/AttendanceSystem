"""
Request contracts of the lifecycle routes (apps/api/src/modules/lifecycle/routes.ts and service.ts) as DRF
serializers, validated with hajiri.core.validation.validate (zod wording). Strings the TS schemas did not trim
(descriptions, notes, links) are not trimmed here either.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from hajiri.core.validation import IsoDateField, MoneyField
from hajiri.db.models import Asset, EmployeeExit, ExitInterviewQuestion, LifecycleCase, LifecycleTask, ProbationReview
from hajiri.people.serializers import PatternField, nullish_text, raw_text, text

ASSIGNEES = tuple(LifecycleTask.AssigneeType.values)
CASE_KINDS = tuple(LifecycleCase.Kind.values)
CASE_STATUSES = tuple(LifecycleCase.Status.values)
TASK_STATUSES = tuple(LifecycleTask.Status.values)
# Kinds HR records here; a transfer to a sister company is written only by hajiri.group_admin.transfers.
EXIT_KINDS = tuple(k for k in EmployeeExit.Kind.values if k != EmployeeExit.Kind.TRANSFER)
CONDITIONS = tuple(Asset.Condition.values)


def _nullish_raw(n: int) -> serializers.CharField:
    return raw_text(n, required=False, allow_null=True)


def _page_link(**kw) -> PatternField:
    """A page inside Hajiri (z.string().max(200).regex(...))."""
    return PatternField(r'^/[A-Za-z0-9/_?=&-]*$', max_length=200, trim_whitespace=False,
                        error_messages={'invalid': 'a page inside Hajiri, starting with /'}, **kw)


def _nullish_uuid() -> serializers.UUIDField:
    return serializers.UUIDField(required=False, allow_null=True)


def _nullish_date() -> IsoDateField:
    return IsoDateField(required=False, allow_null=True)


@extend_schema_serializer(component_name='LifecyclePolicyIn')
class PolicyIn(serializers.Serializer):
    # Notice an employee gives when resigning, in calendar days.
    noticeDays = serializers.IntegerField(min_value=0, max_value=180, default=30)
    # Start the default onboarding checklist when HR adds an employee.
    autoStartOnboarding = serializers.BooleanField(default=True)
    # Days before probation ends on which the manager is reminded.
    probationReminderDays = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=90), max_length=4,
                                                  default=lambda: [14, 7])
    # Let leavers fill in the exit interview themselves.
    selfExitInterview = serializers.BooleanField(default=True)


class TemplateTaskIn(serializers.Serializer):
    title = text(160)
    description = _nullish_raw(1000)
    department = _nullish_raw(60)
    assigneeType = serializers.ChoiceField(ASSIGNEES)
    assigneeUserId = _nullish_uuid()
    dueOffsetDays = serializers.IntegerField(min_value=-90, max_value=365, default=0)
    link = _page_link(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs['assigneeType'] == 'user' and not attrs.get('assigneeUserId'):
            raise serializers.ValidationError('choose the person for this task')
        return attrs


class TemplateIn(serializers.Serializer):
    kind = serializers.ChoiceField(CASE_KINDS)
    name = text(120)
    description = _nullish_raw(1000)
    branchId = _nullish_uuid()
    orgUnitId = _nullish_uuid()
    isDefault = serializers.BooleanField(default=False)
    active = serializers.BooleanField(default=True)
    tasks = TemplateTaskIn(many=True, max_length=60)


class StartCaseIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    templateId = _nullish_uuid()
    anchorDate = IsoDateField(required=False)


class AddTaskIn(serializers.Serializer):
    title = text(160)
    description = _nullish_raw(1000)
    department = _nullish_raw(60)
    assigneeType = serializers.ChoiceField(ASSIGNEES)
    assigneeUserId = _nullish_uuid()
    dueDate = _nullish_date()
    link = _page_link(required=False, allow_null=True)


class UpdateTaskIn(serializers.Serializer):
    status = serializers.ChoiceField(TASK_STATUSES, required=False)
    note = _nullish_raw(1000)
    dueDate = _nullish_date()
    assigneeType = serializers.ChoiceField(ASSIGNEES, required=False)
    assigneeUserId = _nullish_uuid()


@extend_schema_serializer(component_name='LifecycleCommentIn')
class CommentIn(serializers.Serializer):
    body = text(2000)


class ProbationQuery(serializers.Serializer):
    within = serializers.IntegerField(min_value=1, max_value=365, default=60)


@extend_schema_serializer(component_name='LifecycleReviewIn')
class ReviewIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    recommendation = serializers.ChoiceField(ProbationReview.Recommendation.values)
    extendTo = _nullish_date()
    lastWorkingDay = _nullish_date()
    comments = text(4000, min_length=3)


class ConfirmIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    confirmedOn = IsoDateField()


class ResignIn(serializers.Serializer):
    proposedLastWorkingDay = IsoDateField()
    reasonCategory = nullish_text(60)
    reason = nullish_text(2000)


class RecordExitIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    kind = serializers.ChoiceField(EXIT_KINDS)
    lastWorkingDay = IsoDateField()
    voluntary = serializers.BooleanField(required=False)
    reasonCategory = nullish_text(60)
    reason = nullish_text(2000)
    templateId = _nullish_uuid()


class UpdateExitIn(serializers.Serializer):
    lastWorkingDay = IsoDateField(required=False)
    voluntary = serializers.BooleanField(required=False)
    reasonCategory = nullish_text(60)
    reason = nullish_text(2000)


class CancelExitIn(serializers.Serializer):
    reason = text(500, min_length=3)


class CompleteExitIn(serializers.Serializer):
    skipOpenTasks = serializers.BooleanField(default=False)


class QuestionIn(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    question = text(300)
    kind = serializers.ChoiceField(ExitInterviewQuestion.Kind.values)
    options = serializers.ListField(child=text(80), max_length=12, default=list)
    active = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if attrs['kind'] == 'choice' and len(attrs['options']) < 2:
            raise serializers.ValidationError('a choice question needs at least two options')
        return attrs


class QuestionsIn(serializers.Serializer):
    questions = QuestionIn(many=True, max_length=30)


class AnswerField(serializers.Field):
    """z.union([z.string().max(3000), z.number().int().min(1).max(5)])"""
    default_error_messages = {'invalid': 'Invalid input'}

    def to_internal_value(self, data):
        if isinstance(data, str) and len(data) <= 3000:
            return data
        if isinstance(data, (int, float)) and not isinstance(data, bool) and float(data).is_integer() and 1 <= data <= 5:
            return int(data)
        self.fail('invalid')

    def to_representation(self, value):
        return value


class AnswerIn(serializers.Serializer):
    questionId = serializers.UUIDField()
    answer = AnswerField()


class InterviewIn(serializers.Serializer):
    answers = AnswerIn(many=True, min_length=1, max_length=30)


class AssetTypeIn(serializers.Serializer):
    name = text(80)


class AssetQuery(serializers.Serializer):
    status = serializers.ChoiceField(Asset.Status.values, required=False)
    employeeId = serializers.UUIDField(required=False)
    search = raw_text(80, required=False)


class AssetIn(serializers.Serializer):
    tag = text(40)
    name = text(120)
    typeId = _nullish_uuid()
    serial = nullish_text(80)
    condition = serializers.ChoiceField(CONDITIONS, default=Asset.Condition.GOOD)
    purchaseDate = _nullish_date()
    cost = MoneyField(required=False, allow_null=True)
    notes = _nullish_raw(1000)


class AssetUpdateIn(AssetIn):
    """PATCH /assets/:id (validated with partial=True): any field of AssetIn, plus available/retired."""
    status = serializers.ChoiceField((Asset.Status.AVAILABLE, Asset.Status.RETIRED), required=False)


class AssignAssetIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    assignedOn = IsoDateField(required=False)
    notes = _nullish_raw(500)


class ReturnAssetIn(serializers.Serializer):
    returnedOn = IsoDateField(required=False)
    condition = serializers.ChoiceField(CONDITIONS)
    notes = _nullish_raw(500)

