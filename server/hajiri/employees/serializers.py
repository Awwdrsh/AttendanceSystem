"""
Request contracts of the employee master (the zod schemas of apps/api/src/modules/employees/routes.ts) as DRF
serializers. The TS schema does not trim these strings, so neither do the fields here.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import Employee, EmployeeAssignment
from hajiri.people.serializers import PAN_RE, PatternField, raw_text

EMPLOYMENT_TYPES = tuple(EmployeeAssignment.EmploymentType.values)


def _opt(n: int, **kw) -> serializers.CharField:
    return raw_text(n, required=False, **kw)


def _pan(**kw) -> PatternField:
    return PatternField(PAN_RE, error_messages={'invalid': 'PAN must be 9 digits'}, trim_whitespace=False, **kw)


class EmployeeIn(serializers.Serializer):
    """POST /employees (and, with partial=True, PATCH /employees/:id)."""
    empCode = raw_text(32, min_length=1)
    nameEn = raw_text(120, min_length=1)
    nameNe = _opt(120)
    gender = serializers.ChoiceField(Employee.Gender.values, required=False)
    dob = IsoDateField(required=False)
    maritalStatus = serializers.ChoiceField(Employee.MaritalStatus.values, required=False)
    joinDate = IsoDateField(required=False)
    exitDate = IsoDateField(required=False, allow_null=True)
    designation = _opt(120)
    grade = _opt(40)
    phone = _opt(30)
    email = serializers.EmailField(required=False, trim_whitespace=False)
    otEligible = serializers.BooleanField(required=False)
    workFromHome = serializers.BooleanField(required=False)
    weeklyOffDays = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), required=False)
    regime = serializers.ChoiceField(Employee.Regime.values, required=False)
    ssfId = _opt(20)
    pfId = _opt(40)
    citId = _opt(40)
    pan = _pan(required=False)
    bankCode = _opt(20)
    bankBranch = _opt(80)
    bankAccount = raw_text(40, min_length=4, required=False)
    isResident = serializers.BooleanField(required=False)
    remoteAreaClass = serializers.ChoiceField(Employee.RemoteAreaClass.values, required=False, allow_null=True)
    disability = serializers.BooleanField(required=False)
    festivalMonth = serializers.IntegerField(min_value=1, max_value=12, required=False, allow_null=True)
    probationEnd = IsoDateField(required=False, allow_null=True)
    branchId = serializers.UUIDField(required=False)
    orgUnitId = serializers.UUIDField(required=False, allow_null=True)
    managerId = serializers.UUIDField(required=False, allow_null=True)
    employmentType = serializers.ChoiceField(EMPLOYMENT_TYPES, required=False)
    shiftId = serializers.UUIDField(required=False)


class EmployeeListQuery(serializers.Serializer):
    status = serializers.ChoiceField(Employee.Status.values, required=False)
    search = _opt(100)
    branchId = serializers.UUIDField(required=False)
    pan = _pan(required=False)
