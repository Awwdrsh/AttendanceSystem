"""
The request contract of the learning module (the zod schemas of apps/api/src/modules/learning/routes.ts) as DRF
serializers; `hajiri.core.validation.validate` reports problems in the zod wording. A PATCH validates the same
serializer with partial=True, so only the fields sent change.
"""
from __future__ import annotations

import re

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from hajiri.core.validation import IsoDateField, MoneyField
from hajiri.db.models import LrnCourse, LrnEnrollment, LrnSession
from hajiri.performance.serializers import IsoDateTimeField, NumberField, _text


class HttpUrlField(serializers.CharField):
    """An http(s) address up to 500 characters, like z.url({protocol: /^https?$/}); '' means none."""
    pattern = re.compile(r'https?://[^\s/$.?#][^\s]*', re.IGNORECASE)

    def __init__(self, **kw):
        super().__init__(required=False, allow_null=True, allow_blank=True, **kw)

    def to_internal_value(self, data):
        v = super().to_internal_value(data)
        if v == '':
            return None
        if not self.pattern.fullmatch(v):
            raise serializers.ValidationError('Invalid URL', code='custom')
        if len(v) > 500:
            raise serializers.ValidationError('Too big: expected string to have <=500 characters', code='custom')
        return v


class CourseIn(serializers.Serializer):
    code = _text(30)
    title = _text(200)
    description = _text(4000, required=False)
    category = _text(60, required=False)
    delivery = serializers.ChoiceField(LrnCourse.Delivery.values, default=LrnCourse.Delivery.CLASSROOM)
    source = serializers.ChoiceField(LrnCourse.Source.values, default=LrnCourse.Source.INTERNAL)
    provider = _text(120, required=False)
    durationHours = NumberField(min_value=0, max_value=10000, required=False, allow_null=True)
    url = HttpUrlField()
    cost = MoneyField(default='0')
    mandatory = serializers.BooleanField(default=False)
    mandatoryFor = serializers.ListField(child=_text(120), max_length=100, default=list)
    validityMonths = serializers.IntegerField(min_value=1, max_value=120, required=False, allow_null=True)
    passMark = serializers.IntegerField(min_value=1, max_value=100, required=False, allow_null=True)
    active = serializers.BooleanField(default=True)


class SessionIn(serializers.Serializer):
    startsAt = IsoDateTimeField()
    endsAt = IsoDateTimeField()
    venue = _text(200, required=False)
    trainer = _text(120, required=False)
    capacity = serializers.IntegerField(min_value=1, max_value=10000, required=False, allow_null=True)
    status = serializers.ChoiceField(LrnSession.Status.values, default=LrnSession.Status.SCHEDULED)


@extend_schema_serializer(component_name='LearningQuestionIn')
class QuestionIn(serializers.Serializer):
    prompt = _text(1000)
    options = serializers.ListField(child=_text(300), min_length=2, max_length=6)
    correctIndex = serializers.IntegerField(min_value=0)

    def validate(self, attrs):
        if attrs['correctIndex'] >= len(attrs['options']):
            raise serializers.ValidationError('the correct answer must be one of the options')
        return attrs


@extend_schema_serializer(component_name='LearningQuestionsIn')
class QuestionsIn(serializers.Serializer):
    questions = serializers.ListField(child=QuestionIn(), max_length=50)


class EnrollmentQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(('mine', 'team', 'all'), default='mine')
    courseId = serializers.UUIDField(required=False)
    status = serializers.CharField(max_length=30, required=False, allow_blank=True)


class SelfEnrollIn(serializers.Serializer):
    courseId = serializers.UUIDField()
    sessionId = serializers.UUIDField(required=False)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True)


@extend_schema_serializer(component_name='LearningAssignIn')
class AssignIn(serializers.Serializer):
    courseId = serializers.UUIDField()
    sessionId = serializers.UUIDField(required=False)
    employeeIds = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)


class AttendanceIn(serializers.Serializer):
    attended = serializers.BooleanField()


class AttemptIn(serializers.Serializer):
    answers = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=10), max_length=50)


class CourseFeedbackIn(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = _text(2000, required=False)


class CertificateIn(serializers.Serializer):
    fileId = serializers.UUIDField()
    issuedOn = IsoDateField(required=False)
    expiresOn = IsoDateField(required=False, allow_null=True)


class ExpiringIn(serializers.Serializer):
    days = serializers.IntegerField(min_value=0, max_value=365, default=60)


class PeriodIn(serializers.Serializer):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['from'] = IsoDateField()     # `from` is a Python keyword
        self.fields['to'] = IsoDateField()


ENROLL_STATUSES = tuple(LrnEnrollment.Status.values)
