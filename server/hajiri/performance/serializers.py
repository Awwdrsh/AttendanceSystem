"""
The request contract of the performance module (the zod schemas of apps/api/src/modules/performance/*.ts) as DRF
serializers. Field names are the TS body keys; `hajiri.core.validation.validate` reports problems in the zod wording.
A PATCH validates the same serializer with partial=True, so only the fields sent change and no defaults are filled in.
"""
from __future__ import annotations

import math

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import PerfCompetency, PerfGoal, PerfJournal, PerfKeyResult, PerfOneOnOne, PerfOneOnOneItem

MEASURES = tuple(PerfKeyResult.Measure.values)
GOAL_STATUSES = tuple(PerfGoal.Status.values)
CADENCES = tuple(PerfOneOnOne.Cadence.values)
TAGS = tuple(PerfJournal.Tag.values)
BIG = 1e13


class NumberField(serializers.FloatField):
    """A number, also from a form string ('' counts as 0, like z.coerce.number()); whole numbers come back as int."""

    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail('invalid')
        if isinstance(data, str):
            data = data.strip() or 0
        v = super().to_internal_value(data)
        if not math.isfinite(v):
            self.fail('invalid')
        return int(v) if v.is_integer() else v


def _text(max_length: int, *, required: bool = True, trim: bool = True, **kw):
    """A required text (at least one character after trimming), or an optional one (`nullish`) when required=False."""
    if required:
        return serializers.CharField(max_length=max_length, trim_whitespace=trim, **kw)
    return serializers.CharField(max_length=max_length, trim_whitespace=trim, required=False, allow_null=True, allow_blank=True, **kw)


def number(lo=-BIG, hi=BIG, **kw):
    return NumberField(min_value=lo, max_value=hi, **kw)


def uuid_opt(**kw):
    return serializers.UUIDField(required=False, allow_null=True, **kw)


def date_opt():
    return IsoDateField(required=False, allow_null=True)


# ---------------------------------------------------------------- goals and KPIs

def _positive(v):
    if v <= 0:
        raise serializers.ValidationError('Too small: expected number to be >0')
    return v


class KeyResultIn(serializers.Serializer):
    title = _text(200)
    measure = serializers.ChoiceField(MEASURES, default=PerfKeyResult.Measure.NUMBER)
    startValue = number(default=0)
    targetValue = number(default=100)
    currentValue = number(required=False)
    unit = _text(20, required=False)
    weight = number(0, 1000, default=1, validators=[_positive])


class GoalIn(serializers.Serializer):
    scope = serializers.ChoiceField(PerfGoal.Scope.values, default=PerfGoal.Scope.INDIVIDUAL)
    employeeId = serializers.UUIDField(required=False)
    orgUnitId = serializers.UUIDField(required=False)
    parentId = uuid_opt()
    title = _text(200)
    description = _text(4000, required=False)
    weight = number(0, 100, default=0)
    startDate = date_opt()
    dueDate = date_opt()
    status = serializers.ChoiceField((PerfGoal.Status.DRAFT, PerfGoal.Status.ACTIVE), default=PerfGoal.Status.ACTIVE)
    keyResults = serializers.ListField(child=KeyResultIn(), max_length=20, default=list)


class GoalPatch(serializers.Serializer):
    """Only the fields sent change (the zod partial() also filled in defaults, which reset the weight to 0 on every
    status change; that was never the intent). Validated with partial=True."""
    parentId = uuid_opt()
    title = _text(200)
    description = _text(4000, required=False)
    weight = number(0, 100)
    startDate = date_opt()
    dueDate = date_opt()
    status = serializers.ChoiceField(GOAL_STATUSES)
    progress = number(0, 100)


class CheckinIn(serializers.Serializer):
    keyResultId = serializers.UUIDField(required=False)
    value = number(required=False)
    progress = number(0, 100, required=False)
    confidence = serializers.IntegerField(min_value=1, max_value=3, required=False)
    comment = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs.get('value') is None and attrs.get('progress') is None and not attrs.get('comment'):
            raise serializers.ValidationError('add a value, progress or a comment')
        return attrs


class KpiIn(serializers.Serializer):
    designation = _text(120, required=False)
    title = _text(200)
    description = _text(2000, required=False)
    measure = serializers.ChoiceField(MEASURES, default=PerfKeyResult.Measure.NUMBER)
    defaultTarget = number(default=100)
    unit = _text(20, required=False)
    active = serializers.BooleanField(default=True)


class ApplyKpiIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    dueDate = date_opt()
    weight = number(0, 100, default=0)
    target = number(required=False)


class GoalQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(('mine', 'team', 'company', 'employee', 'all'), default='mine')
    employeeId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(GOAL_STATUSES, required=False)


class SearchIn(serializers.Serializer):
    q = serializers.CharField(max_length=60, required=False, allow_blank=True)


class DesignationIn(serializers.Serializer):
    designation = serializers.CharField(max_length=120, required=False, allow_blank=True)


# ---------------------------------------------------------------- journal, feedback, 1-on-1s

class JournalQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    # `from` is a Python keyword: declared in __init__.

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['from'] = IsoDateField(required=False)
        self.fields['to'] = IsoDateField(required=False)


class JournalIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    noteDate = IsoDateField()
    tag = serializers.ChoiceField(TAGS, default=PerfJournal.Tag.NEUTRAL)
    note = _text(4000)


class FeedbackQueryIn(serializers.Serializer):
    box = serializers.ChoiceField(('received', 'given', 'requests', 'about'), default='received')
    employeeId = serializers.UUIDField(required=False)


class GiveFeedbackIn(serializers.Serializer):
    toEmployeeId = serializers.UUIDField()
    body = _text(4000)
    privateToManager = serializers.BooleanField(default=False)


class RequestFeedbackIn(serializers.Serializer):
    fromEmployeeIds = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=10)
    aboutEmployeeId = serializers.UUIDField(required=False)
    prompt = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class AnswerFeedbackIn(serializers.Serializer):
    body = _text(4000)
    privateToManager = serializers.BooleanField(default=False)


class IsoDateTimeField(serializers.RegexField):
    """An ISO instant with an offset ('2026-10-14T11:00:00+05:45', '...Z'), kept as the string."""

    def __init__(self, **kw):
        kw.setdefault('error_messages', {'invalid': 'Invalid ISO datetime'})
        super().__init__(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$', **kw)


class MeetingIn(serializers.Serializer):
    withEmployeeId = serializers.UUIDField()
    scheduledAt = IsoDateTimeField()
    cadence = serializers.ChoiceField(CADENCES, default=PerfOneOnOne.Cadence.NONE)
    agenda = serializers.ListField(child=_text(1000), max_length=20, default=list)


class MeetingPatch(serializers.Serializer):
    scheduledAt = IsoDateTimeField(required=False)
    cadence = serializers.ChoiceField(CADENCES, required=False)
    sharedNotes = _text(8000, required=False, trim=False)
    privateNotes = _text(8000, required=False, trim=False)
    status = serializers.ChoiceField(PerfOneOnOne.Status.values, required=False)


class MeetingItemIn(serializers.Serializer):
    kind = serializers.ChoiceField(PerfOneOnOneItem.Kind.values)
    text = _text(1000)
    ownerEmployeeId = uuid_opt()


class MeetingItemPatch(serializers.Serializer):
    done = serializers.BooleanField(required=False)
    text = serializers.CharField(max_length=1000, required=False)


# ---------------------------------------------------------------- competencies and cycles

class IndicatorIn(serializers.Serializer):
    level = serializers.IntegerField(min_value=1, max_value=5)
    text = _text(500)


class CompetencyIn(serializers.Serializer):
    kind = serializers.ChoiceField(PerfCompetency.Kind.values)
    name = _text(120)
    description = _text(2000, required=False)
    indicators = serializers.ListField(child=IndicatorIn(), max_length=5, default=list)
    designations = serializers.ListField(child=_text(120), max_length=100, default=list)
    grades = serializers.ListField(child=_text(40), max_length=50, default=list)
    active = serializers.BooleanField(default=True)


class CycleFilterIn(serializers.Serializer):
    orgUnitIds = serializers.ListField(child=serializers.UUIDField(), max_length=200, required=False)
    branchIds = serializers.ListField(child=serializers.UUIDField(), max_length=200, required=False)
    joinedBefore = IsoDateField(required=False)


class CycleIn(serializers.Serializer):
    name = _text(120)
    periodStart = IsoDateField()
    periodEnd = IsoDateField()
    goalsWeight = number(0, 100, default=60)
    competencyWeight = number(0, 100, default=40)
    ratingLabels = serializers.ListField(child=_text(40), min_length=5, max_length=5, required=False)
    peerEnabled = serializers.BooleanField(default=False)
    peerMin = serializers.IntegerField(min_value=1, max_value=10, default=3)
    selfDue = date_opt()
    managerDue = date_opt()
    peerDue = date_opt()
    calibrationDue = date_opt()
    filter = CycleFilterIn(default=dict)


class RecommendationIn(serializers.Serializer):
    incrementPct = number(0, 100, required=False, allow_null=True)
    promotion = serializers.BooleanField(default=False)
    pip = serializers.BooleanField(default=False)
    trainingCourseIds = serializers.ListField(child=serializers.UUIDField(), max_length=10, default=list)
    notes = _text(2000, required=False)


class ItemRatingIn(serializers.Serializer):
    id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5, allow_null=True)
    comment = _text(2000, required=False)


class SelfReviewIn(serializers.Serializer):
    items = serializers.ListField(child=ItemRatingIn(), max_length=100, default=list)
    summary = _text(4000, required=False)
    submit = serializers.BooleanField(default=False)


class ManagerReviewIn(SelfReviewIn):
    recommendation = RecommendationIn(required=False)


class CalibrateIn(serializers.Serializer):
    finalRating = number(1, 5)
    reason = _text(1000)

    def validate_reason(self, v):
        if len(v) < 5:
            raise serializers.ValidationError('explain the change in a few words')
        return v


class AcknowledgeIn(serializers.Serializer):
    comment = _text(2000, required=False)


class ReviewScopeIn(serializers.Serializer):
    scope = serializers.ChoiceField(('mine', 'team'), default='mine')


class NominatePeersIn(serializers.Serializer):
    employeeIds = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=10)


class DecidePeerIn(serializers.Serializer):
    approve = serializers.BooleanField()


class PeerResponseIn(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    strengths = _text(2000, required=False)
    improvements = _text(2000, required=False)

