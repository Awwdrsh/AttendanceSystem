"""
Request contracts of the people app (the zod schemas of apps/api/src/modules/people), as DRF serializers. Services
validate TS-shaped bodies with hajiri.core.validation.validate(...), which reports problems in the zod wording.

Shared helpers for the slice (employees and lifecycle use them too):
- StrictSerializer: zod .strict(), unknown keys are refused with 'Unrecognized key: "x"'.
- text(n) / opt_text(n) / nullish_text(n): the trimmed string shapes the TS schemas use everywhere.
- phone(), regex(...), uuid(...), choice(...), iso_date(...).
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework import serializers

from hajiri.core.errors import ValidationError
from hajiri.core.validation import IsoDateField, choice_message
from hajiri.db.models import CareerEvent, Employee, OrgUnit

# ---------------------------------------------------------------- shared building blocks


class StrictSerializer(serializers.Serializer):
    """zod `.strict()`: keys the schema does not know are refused (reported next to any field problems)."""

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


def text(n: int, *, min_length: int = 1, **kw) -> serializers.CharField:
    """z.string().trim().min(1).max(n)"""
    return serializers.CharField(max_length=n, min_length=min_length if min_length > 1 else None, **kw)


def opt_text(n: int, **kw) -> serializers.CharField:
    """z.string().trim().max(n).optional(): may be left out or empty."""
    return serializers.CharField(max_length=n, required=False, allow_blank=True, **kw)


def nullish_text(n: int, *, trim: bool = True, **kw) -> serializers.CharField:
    """z.string().max(n).nullish() (trimmed unless trim=False)."""
    return serializers.CharField(max_length=n, required=False, allow_null=True, allow_blank=True, trim_whitespace=trim, **kw)


def raw_text(n: int, *, min_length: int | None = None, **kw) -> serializers.CharField:
    """z.string().max(n) without .trim()."""
    kw.setdefault('allow_blank', not min_length)
    return serializers.CharField(max_length=n, min_length=min_length, trim_whitespace=False, **kw)


class PatternField(serializers.RegexField):
    """z.string().regex(re, message): an empty string fails the pattern too (with the pattern's message)."""

    def run_validation(self, data=serializers.empty):
        if isinstance(data, str) and (data == '' or (self.trim_whitespace and data.strip() == '')):
            self.fail('invalid')
        return super().run_validation(data)


def regex(pattern: str, message: str, **kw) -> PatternField:
    """z.string().regex(re, message); trimmed unless trim_whitespace=False is passed."""
    return PatternField(pattern, error_messages={'invalid': message}, **kw)


def nullish(field_cls, *args, **kw):
    return field_cls(*args, required=False, allow_null=True, **kw)


def one_of(values, value):
    """z.enum(values).parse(value) for a single query value: the value, or ValidationError in the zod wording."""
    if value not in values:
        raise ValidationError([{'path': '', 'message': choice_message(values)}])
    return value


def choice(values, **kw) -> serializers.ChoiceField:
    return serializers.ChoiceField(list(values), **kw)


def uuid(**kw) -> serializers.UUIDField:
    return serializers.UUIDField(**kw)


def iso_date(**kw) -> IsoDateField:
    return IsoDateField(**kw)


class NumberField(serializers.FloatField):
    """z.number(): whole numbers stay integers (as JSON.parse gives them), so stored JSON matches the TS output."""

    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail('invalid')
        v = super().to_internal_value(data)
        return int(v) if float(v).is_integer() and not isinstance(data, float) else v


PHONE_RE = r'^\+?[0-9 ()-]{6,20}$'


def phone_number(**kw) -> serializers.RegexField:
    return regex(PHONE_RE, 'phone number', **kw)


PAN_RE = r'^\d{9}$'

# ---------------------------------------------------------------- org structure


class BranchIn(StrictSerializer):
    code = text(20)
    nameEn = text(120)
    address = nullish_text(300)
    phone = nullish_text(30)
    headEmployeeId = nullish(serializers.UUIDField)


class UnitIn(StrictSerializer):
    kind = choice(OrgUnit.Kind.values)
    code = text(20)
    nameEn = text(120)
    parentId = nullish(serializers.UUIDField)
    branchId = nullish(serializers.UUIDField)
    headEmployeeId = nullish(serializers.UUIDField)
    description = nullish_text(500)


class DesignationIn(StrictSerializer):
    code = text(20)
    nameEn = text(120)
    grade = nullish_text(40)
    jobLevel = nullish(serializers.IntegerField, min_value=1, max_value=20)
    orgUnitId = nullish(serializers.UUIDField)
    description = nullish_text(500)
    active = serializers.BooleanField(required=False)


class PolicyIn(StrictSerializer):
    makerChecker = serializers.BooleanField(default=False)


# ---------------------------------------------------------------- directory, change requests, photos


class DirectoryQuery(serializers.Serializer):
    search = opt_text(100)
    orgUnitId = serializers.UUIDField(required=False)
    branchId = serializers.UUIDField(required=False)


class AnyValueField(serializers.Field):
    """z.any(): anything, including null."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        kw.setdefault('allow_null', True)
        super().__init__(**kw)

    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


class ChangeIn(StrictSerializer):
    section = serializers.CharField(max_length=40, trim_whitespace=False, allow_blank=True)
    value = AnyValueField()
    reason = opt_text(500)
    proofFileId = serializers.UUIDField(required=False)


class AvatarIn(StrictSerializer):
    fileId = serializers.UUIDField()


REQUEST_STATUSES = ('pending', 'approved', 'rejected', 'cancelled', 'all')




# ---------------------------------------------------------------- career events

SEPARATION_TYPES = ('resignation', 'termination', 'retirement', 'contract_end', 'death', 'other')
EMPLOYMENT_TYPES = ('regular', 'work_based', 'time_based', 'casual', 'part_time')


class CareerDetailsIn(StrictSerializer):
    designationId = serializers.UUIDField(required=False)
    designation = text(120, required=False)
    grade = text(40, required=False)
    branchId = serializers.UUIDField(required=False)
    orgUnitId = nullish(serializers.UUIDField)
    managerId = nullish(serializers.UUIDField)
    employmentType = choice(EMPLOYMENT_TYPES, required=False)
    separationType = choice(SEPARATION_TYPES, required=False)
    revisionPercent = NumberField(min_value=-100, max_value=1000, required=False)
    reason = opt_text(500)
    endDate = iso_date(required=False)


class CareerIn(serializers.Serializer):
    kind = choice(CareerEvent.Kind.values)
    effectiveDate = iso_date()
    details = CareerDetailsIn(default=dict)
    note = opt_text(1000)


# ---------------------------------------------------------------- profile sections (profile.ts)


class AddressIn(StrictSerializer):
    province = nullish_text(60)
    district = nullish_text(60)
    municipality = nullish_text(80)
    ward = nullish(serializers.IntegerField, min_value=1, max_value=40)
    street = nullish_text(160)


class PersonalIn(StrictSerializer):
    gender = nullish(serializers.ChoiceField, Employee.Gender.values)
    dob = nullish(IsoDateField)
    maritalStatus = nullish(serializers.ChoiceField, Employee.MaritalStatus.values)
    bloodGroup = nullish(serializers.ChoiceField, Employee.BloodGroup.values)
    nationality = nullish_text(60)


class ContactIn(StrictSerializer):
    personalEmail = nullish(serializers.EmailField, max_length=160)
    personalPhone = phone_number(required=False, allow_null=True)
    workPhone = phone_number(required=False, allow_null=True)
    permanentAddress = AddressIn(required=False, allow_null=True)
    temporaryAddress = AddressIn(required=False, allow_null=True)


class BankIn(StrictSerializer):
    bankCode = nullish_text(20)
    bankBranch = nullish_text(80)
    bankAccount = regex(r'^[0-9A-Za-z-]{4,40}$', 'account number', required=False)


class IdsIn(StrictSerializer):
    pan = regex(PAN_RE, 'PAN must be 9 digits', required=False, allow_null=True)
    citizenshipNo = regex(r'^[0-9A-Za-z/ -]{3,30}$', 'citizenship number', required=False, allow_null=True)
    citizenshipDistrict = nullish_text(60)
    citizenshipIssuedOn = nullish(IsoDateField)
    passportNo = regex(r'^[0-9A-Za-z]{5,20}$', 'passport number', required=False, allow_null=True)
    passportExpiry = nullish(IsoDateField)
    licenceNo = regex(r'^[0-9A-Za-z/ -]{4,30}$', 'licence number', required=False, allow_null=True)
    licenceExpiry = nullish(IsoDateField)
    ssfId = nullish_text(20)
    citId = nullish_text(40)
    pfId = nullish_text(40)


class ContactPersonIn(StrictSerializer):
    name = text(120)
    relation = text(40)
    phone = phone_number()
    altPhone = phone_number(required=False, allow_null=True)
    address = nullish_text(200)


class EmergencyIn(StrictSerializer):
    contacts = ContactPersonIn(many=True, max_length=5)


class PositiveShare(serializers.FloatField):
    default_error_messages = {'positive': 'Too small: expected number to be >0'}

    def to_internal_value(self, data):
        v = super().to_internal_value(data)
        if v <= 0:
            self.fail('positive')
        return int(v) if float(v).is_integer() else v


FAMILY_RELATIONS = ('spouse', 'father', 'mother', 'son', 'daughter', 'brother', 'sister', 'father_in_law', 'mother_in_law',
                    'grandfather', 'grandmother', 'other')


class FamilyMemberIn(StrictSerializer):
    name = text(120)
    relation = choice(FAMILY_RELATIONS)
    dob = nullish(IsoDateField)
    occupation = nullish_text(80)
    phone = phone_number(required=False, allow_null=True)
    isDependent = serializers.BooleanField(default=False)
    isNominee = serializers.BooleanField(default=False)
    nomineeShare = PositiveShare(max_value=100, required=False, allow_null=True)


class FamilyIn(StrictSerializer):
    members = FamilyMemberIn(many=True, max_length=20)

    def validate(self, attrs):
        members = attrs.get('members') or []
        nominees = [m for m in members if m.get('isNominee')]
        problems = []
        if any(not m.get('nomineeShare') for m in nominees):
            problems.append('every nominee needs a share')
        elif nominees and abs(sum(m.get('nomineeShare') or 0 for m in nominees) - 100) > 0.001:
            problems.append('nominee shares must add up to 100%')
        if any(not m.get('isNominee') and m.get('nomineeShare') for m in members):
            problems.append('only nominees have a share')
        if problems:
            raise serializers.ValidationError({'members': problems})
        return attrs


def _year(**kw) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=1950, max_value=2200, **kw)


EDUCATION_LEVELS = ('see', 'plus_two', 'diploma', 'bachelor', 'master', 'mphil', 'phd', 'other')


class EducationItemIn(StrictSerializer):
    level = choice(EDUCATION_LEVELS)
    degree = nullish_text(120)
    field = nullish_text(120)
    institution = text(160)
    board = nullish_text(160)
    startYear = _year(required=False, allow_null=True)
    endYear = _year(required=False, allow_null=True)
    result = nullish_text(40)

    def validate(self, attrs):
        if attrs.get('startYear') and attrs.get('endYear') and attrs['endYear'] < attrs['startYear']:
            raise serializers.ValidationError('end year is before start year')
        return attrs


class EducationIn(StrictSerializer):
    items = EducationItemIn(many=True, max_length=15)


class ExperienceItemIn(StrictSerializer):
    organisation = text(160)
    designation = nullish_text(120)
    fromDate = nullish(IsoDateField)
    toDate = nullish(IsoDateField)
    responsibilities = nullish_text(1000)
    reasonForLeaving = nullish_text(300)

    def validate(self, attrs):
        if attrs.get('fromDate') and attrs.get('toDate') and attrs['toDate'] < attrs['fromDate']:
            raise serializers.ValidationError('end date is before start date')
        return attrs


class ExperienceIn(StrictSerializer):
    items = ExperienceItemIn(many=True, max_length=20)


class SkillsIn(StrictSerializer):
    skills = serializers.ListField(child=text(60), max_length=30)
    languages = serializers.ListField(child=text(40), max_length=15)


SECTION_SERIALIZERS: dict[str, type[serializers.Serializer]] = {
    'personal': PersonalIn, 'contact': ContactIn, 'bank': BankIn, 'ids': IdsIn, 'emergency': EmergencyIn,
    'family': FamilyIn, 'education': EducationIn, 'experience': ExperienceIn, 'skills': SkillsIn,
}
