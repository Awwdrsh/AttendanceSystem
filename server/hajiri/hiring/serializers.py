"""
The hiring request contract (the zod schemas of apps/api/src/modules/hiring/routes.ts and careers/routes.ts) as DRF
serializers. Field names are the TS body keys, so the page forms, the public careers JSON API and the services speak
one shape; `hajiri.core.validation.validate` turns problems into the TS issue list.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from hajiri.core.validation import HhmmField, IsoDateField, MoneyField
from hajiri.db.models import HiringApplication, HiringInterview, HiringJob, HiringRequisition, HiringScorecard

DEFAULT_STAGES = ['Applied', 'Screening', 'Interview', 'Offer', 'Hired']
SOURCES = tuple(HiringApplication.Source.values)
CAREERS_SOURCES = ('careers', 'job_board', 'referral', 'other')
WORK_MODES = tuple(HiringJob.WorkMode.values)
EMPLOYMENT_TYPES = tuple(HiringJob.EmploymentType.values)
RECOMMENDATIONS = tuple(HiringScorecard.Recommendation.values)
HIRE_EMPLOYMENT_TYPES = ('regular', 'work_based', 'time_based', 'casual', 'part_time')
GENDERS = ('male', 'female', 'other')


def _kind(v) -> str:
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, (int, float)):
        return 'number'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, list):
        return 'array'
    return 'object'


def _n(v) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


# ---------------------------------------------------------------- fields with the zod semantics

@extend_schema_field(OpenApiTypes.NUMBER)
class NumberField(serializers.Field):
    """A JSON number (z.number()): strings are refused unless `coerce` (z.coerce.number() for query strings).
    Integral values come back as int; `integer=True` refuses fractions."""

    def __init__(self, *, min_value=None, max_value=None, integer: bool = False, coerce: bool = False, **kw):
        self.min_value, self.max_value, self.integer, self.coerce = min_value, max_value, integer, coerce
        super().__init__(**kw)

    def to_internal_value(self, v):
        if self.coerce and isinstance(v, str):
            try:
                v = float(v) if v.strip() else 0.0
            except ValueError:
                raise serializers.ValidationError('Expected number, received NaN') from None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise serializers.ValidationError(f'Expected number, received {_kind(v)}')
        if self.integer and not float(v).is_integer():
            raise serializers.ValidationError('Expected integer, received float')
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if self.min_value is not None and v < self.min_value:
            raise serializers.ValidationError(f'Too small: expected number to be >={_n(self.min_value)}')
        if self.max_value is not None and v > self.max_value:
            raise serializers.ValidationError(f'Too big: expected number to be <={_n(self.max_value)}')
        return v

    def to_representation(self, value):
        return value


@extend_schema_field(OpenApiTypes.BOOL)
class StrictBooleanField(serializers.Field):
    """z.boolean(): only true/false."""

    def to_internal_value(self, v):
        if not isinstance(v, bool):
            raise serializers.ValidationError(f'Expected boolean, received {_kind(v)}')
        return v

    def to_representation(self, value):
        return value


class StrictSerializer(serializers.Serializer):
    """zod `.strict()`: keys the schema does not know are refused ('Unrecognized key: "x"')."""

    def to_internal_value(self, data):
        extra = [k for k in data if k not in self.fields] if isinstance(data, Mapping) else []
        problems = [f'Unrecognized key: "{k}"' for k in extra]
        try:
            out = super().to_internal_value(data)
        except serializers.ValidationError as e:
            if problems:
                detail = e.detail if isinstance(e.detail, dict) else {'non_field_errors': e.detail}
                raise serializers.ValidationError({'non_field_errors': problems, **detail}) from None
            raise
        if problems:
            raise serializers.ValidationError({'non_field_errors': problems})
        return out


def text(max_length: int, *, min_length: int | None = None, **kw) -> serializers.CharField:
    """z.string().trim() with limits."""
    return serializers.CharField(max_length=max_length, min_length=min_length, **kw)


def opt_text(max_length: int) -> serializers.CharField:
    """z.string().trim().max(n).nullish()"""
    return serializers.CharField(max_length=max_length, required=False, allow_null=True, allow_blank=True)


def raw_text(max_length: int, **kw) -> serializers.CharField:
    """z.string().max(n) without .trim()"""
    return serializers.CharField(max_length=max_length, allow_blank=True, trim_whitespace=False, **kw)


def uuid_or_null() -> serializers.UUIDField:
    return serializers.UUIDField(required=False, allow_null=True)


def money_or_null() -> MoneyField:
    return MoneyField(required=False, allow_null=True)


def number_or_null(**kw) -> NumberField:
    return NumberField(required=False, allow_null=True, **kw)


class RecordField(serializers.DictField):
    """z.record(key, value): `key_max` refuses longer keys ('Invalid key in record' at the key)."""

    def __init__(self, *, key_max: int | None = None, key_min: int | None = None, **kw):
        self.key_max, self.key_min = key_max, key_min
        super().__init__(**kw)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError(f'Expected object, received {_kind(data)}')
        out = super().to_internal_value(data)
        bad = {k: ['Invalid key in record'] for k in out
               if (self.key_max is not None and len(k) > self.key_max) or (self.key_min is not None and len(k.strip()) < self.key_min)}
        if bad:
            raise serializers.ValidationError(bad)
        return out


@extend_schema_field(OpenApiTypes.ANY)
class AnyField(serializers.Field):
    """z.any() / z.unknown(): the value as sent."""

    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


# ---------------------------------------------------------------- jobs

class RuleIn(StrictSerializer):
    equals = serializers.ChoiceField(('yes', 'no'), required=False)
    min = NumberField(min_value=0, max_value=1_000_000, required=False)


class QuestionIn(serializers.Serializer):
    id = serializers.RegexField(r'^q[0-9a-z]{1,12}$', error_messages={'invalid': 'question id like q1'}, trim_whitespace=False)
    kind = serializers.ChoiceField(('yes_no', 'number', 'text'))
    label = text(200, min_length=3)
    required = StrictBooleanField(default=True)
    knockout = RuleIn(required=False)
    preferred = RuleIn(required=False)


class StagesField(serializers.ListField):
    """stagesSchema: 2 to 10 different names, starting with "Applied" and ending with "Hired"."""

    def __init__(self, **kw):
        super().__init__(child=text(40, min_length=2), min_length=2, max_length=10, **kw)

    def to_internal_value(self, data):
        s = super().to_internal_value(data)
        problems = []
        if len({x.lower() for x in s}) != len(s):
            problems.append('stage names must be different')
        if s[0] != 'Applied':
            problems.append('the first stage must be "Applied"')
        if s[-1] != 'Hired':
            problems.append('the last stage must be "Hired"')
        if problems:
            raise serializers.ValidationError(problems)
        return s


def _job_fields(patch: bool) -> dict:
    """jobBody, or jobBody.partial() for a patch: every field optional and no defaults (only sent keys change)."""
    def d(value):
        return {'required': False} if patch else {'default': value}
    opt = {'required': False} if patch else {}
    return {
        'requisitionId': uuid_or_null(),
        'title': text(120, min_length=2, **opt),
        'description': raw_text(10_000, **d('')),
        'requirements': raw_text(10_000, **d('')),
        'category': opt_text(60),
        'location': opt_text(120),
        'workMode': serializers.ChoiceField(WORK_MODES, **d(HiringJob.WorkMode.ONSITE)),
        'employmentType': serializers.ChoiceField(EMPLOYMENT_TYPES, **d(HiringJob.EmploymentType.FULL_TIME)),
        'experienceMin': number_or_null(min_value=0, max_value=60),
        'experienceMax': number_or_null(min_value=0, max_value=60),
        'salaryMin': money_or_null(),
        'salaryMax': money_or_null(),
        'showSalary': StrictBooleanField(**d(False)),
        'openings': NumberField(integer=True, min_value=1, max_value=500, **d(1)),
        'deadline': IsoDateField(required=False, allow_null=True),
        'orgUnitId': uuid_or_null(),
        'branchId': uuid_or_null(),
        'hiringManagerId': uuid_or_null(),
        'stages': StagesField(required=False),
        'questions': QuestionIn(many=True, required=False, max_length=15),
    }


JobIn = type('JobIn', (serializers.Serializer,), _job_fields(False))
JobPatchIn = type('JobPatchIn', (serializers.Serializer,), _job_fields(True))


class JobsQueryIn(serializers.Serializer):
    status = serializers.ChoiceField(tuple(HiringJob.Status.values), required=False)


class BoardQueryIn(serializers.Serializer):
    include = serializers.ChoiceField(('active', 'all'), default='active')


class PolicyIn(serializers.Serializer):
    retentionDays = NumberField(integer=True, min_value=30, max_value=1095)
    careersEnabled = StrictBooleanField()


# ---------------------------------------------------------------- requisitions

class RequisitionIn(serializers.Serializer):
    title = text(120, min_length=2)
    designation = opt_text(120)
    orgUnitId = uuid_or_null()
    branchId = uuid_or_null()
    headcount = NumberField(integer=True, min_value=1, max_value=500, default=1)
    salaryMin = money_or_null()
    salaryMax = money_or_null()
    kind = serializers.ChoiceField(tuple(HiringRequisition.Kind.values), default=HiringRequisition.Kind.NEW)
    replacingEmployeeId = uuid_or_null()
    justification = text(4000, min_length=10)


# ---------------------------------------------------------------- applications

PHONE = r'^[0-9+\- ()]{6,30}$'


class AddCandidateIn(serializers.Serializer):
    name = text(120, min_length=2)
    email = serializers.EmailField(max_length=200)
    phone = serializers.RegexField(PHONE, required=False, allow_null=True, error_messages={'invalid': 'phone number'})
    source = serializers.ChoiceField(SOURCES, default=HiringApplication.Source.MANUAL)
    cvFileId = uuid_or_null()
    coverNote = opt_text(4000)
    experienceYears = number_or_null(min_value=0, max_value=60)
    currentEmployer = opt_text(120)
    answers = RecordField(child=AnyField(), required=False)


class UpdateApplicationIn(serializers.Serializer):
    rating = number_or_null(integer=True, min_value=1, max_value=5)
    tags = serializers.ListField(child=text(30, min_length=1), max_length=10, required=False)
    ownerUserId = uuid_or_null()


class MoveIn(serializers.Serializer):
    stage = text(40, min_length=1)


class RejectIn(serializers.Serializer):
    reason = text(500, min_length=3)
    notifyCandidate = StrictBooleanField(default=True)


class NoteIn(serializers.Serializer):
    body = text(4000, min_length=1)


# ---------------------------------------------------------------- interviews and scorecards

def _panel(**kw):
    return serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=10, **kw)


class InterviewIn(serializers.Serializer):
    title = text(80, min_length=2, default='Interview')
    date = IsoDateField()
    time = HhmmField()
    durationMin = NumberField(integer=True, min_value=10, max_value=480, default=45)
    mode = serializers.ChoiceField(tuple(HiringInterview.Mode.values))
    location = opt_text(300)
    panel = _panel()
    notifyCandidate = StrictBooleanField(default=True)


class InterviewPatchIn(serializers.Serializer):
    status = serializers.ChoiceField(tuple(HiringInterview.Status.values), required=False)
    date = IsoDateField(required=False)
    time = HhmmField(required=False)
    location = opt_text(300)
    panel = _panel(required=False)


# 'from' is a Python keyword, so these two are built with type().
InterviewsQueryIn = type('InterviewsQueryIn', (serializers.Serializer,), {
    'scope': serializers.ChoiceField(('mine', 'all'), default='mine'),
    'from': IsoDateField(required=False), 'to': IsoDateField(required=False)})
ReportsQueryIn = type('ReportsQueryIn', (serializers.Serializer,), {
    'from': IsoDateField(required=False), 'to': IsoDateField(required=False)})


class RatingsField(RecordField):
    def __init__(self, **kw):
        super().__init__(child=NumberField(integer=True, min_value=1, max_value=5), key_min=2, key_max=None, **kw)

    def to_internal_value(self, data):
        out = super().to_internal_value(data)
        bad = {k: ['Invalid key in record'] for k in out if len(k.strip()) > 40}
        if bad:
            raise serializers.ValidationError(bad)
        if not (1 <= len(out) <= 10):
            raise serializers.ValidationError('rate between 1 and 10 criteria')
        return {k.strip(): v for k, v in out.items()}


class ScorecardIn(serializers.Serializer):
    ratings = RatingsField()
    recommendation = serializers.ChoiceField(RECOMMENDATIONS)
    notes = opt_text(4000)


# ---------------------------------------------------------------- offers and hire

class OfferIn(serializers.Serializer):
    designation = text(120, min_length=2)
    salary = MoneyField()
    joinDate = IsoDateField()
    expiresOn = IsoDateField(required=False, allow_null=True)
    notes = opt_text(4000)

    def validate_salary(self, v):
        if Decimal(v) <= 0:
            raise serializers.ValidationError('salary must be above zero')
        return v


class RespondIn(serializers.Serializer):
    response = serializers.ChoiceField(('accepted', 'declined'))


class HireIn(serializers.Serializer):
    empCode = text(32, min_length=1)
    joinDate = IsoDateField(required=False)
    designation = text(120, required=False, allow_blank=True)
    branchId = serializers.UUIDField(required=False)
    orgUnitId = uuid_or_null()
    managerId = uuid_or_null()
    shiftId = serializers.UUIDField(required=False)
    employmentType = serializers.ChoiceField(HIRE_EMPLOYMENT_TYPES, default='regular')
    gender = serializers.ChoiceField(GENDERS, required=False)
    probationEnd = IsoDateField(required=False, allow_null=True)


# ---------------------------------------------------------------- public careers

@extend_schema_field(OpenApiTypes.ANY)
class AnswerField(serializers.Field):
    """z.union([z.string().max(1000), z.number(), z.boolean()])"""

    def to_internal_value(self, v):
        if isinstance(v, bool) or (isinstance(v, (int, float)) and math.isfinite(v)):
            return v
        if isinstance(v, str) and len(v) <= 1000:
            return v
        raise serializers.ValidationError('Invalid input')

    def to_representation(self, value):
        return value


@extend_schema_field(OpenApiTypes.BOOL)
class ConsentField(serializers.Field):
    """z.literal(true) with the TS message: anything but true is refused."""

    def to_internal_value(self, v):
        if v is not True:
            raise serializers.ValidationError('please agree to the processing of your data')
        return v

    def to_representation(self, value):
        return value


class ApplyIn(StrictSerializer):
    name = text(120, min_length=2)
    email = serializers.EmailField(max_length=200)
    phone = serializers.RegexField(PHONE, error_messages={'invalid': 'enter a valid phone number'})
    experienceYears = number_or_null(min_value=0, max_value=60)
    currentEmployer = opt_text(120)
    coverNote = opt_text(4000)
    answers = RecordField(child=AnswerField(), key_max=20, required=False)
    cvFilename = text(200, min_length=1)
    cvDataUrl = serializers.CharField(min_length=20, max_length=6_000_000, trim_whitespace=False)
    consent = ConsentField()
    source = serializers.ChoiceField(CAREERS_SOURCES, default='careers')
    website = raw_text(200, required=False)   # honeypot: hidden from people, filled in by bots


class CareersQueryIn(serializers.Serializer):
    company = serializers.CharField(max_length=32, required=False, trim_whitespace=False)
    q = text(100, required=False, allow_blank=True)
    workMode = serializers.ChoiceField(WORK_MODES, required=False)
    category = serializers.CharField(max_length=60, required=False, trim_whitespace=False)
    experience = NumberField(coerce=True, min_value=0, max_value=60, required=False)


class ApplyFormIn(ApplyIn):
    """The careers page's form: the same fields, with the CV sent as a file upload instead of a data URL."""
    cvDataUrl = None
