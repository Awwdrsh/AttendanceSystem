"""Request contracts of the group JSON endpoints (DRF serializers)."""
from __future__ import annotations

from rest_framework import serializers

from engines.bs_calendar import MAX_YEAR, MIN_YEAR
from hajiri.core.auth import GROUP_ROLES
from hajiri.db.models import Employee, EmployeeAssignment

from . import services


class ReportQueryIn(serializers.Serializer):
    """GET /api/v1/group/reports/<kind>?companyId=&period=month|fy&bsYear=&bsMonth=&fy="""
    companyId = serializers.UUIDField(required=False, allow_null=True)
    period = serializers.ChoiceField(('month', 'fy'), default='month')
    bsYear = serializers.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)
    bsMonth = serializers.IntegerField(min_value=1, max_value=12, required=False)
    fy = serializers.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)

    def validate(self, attrs):
        if attrs.get('period', 'month') == 'month' and ('bsYear' in attrs) != ('bsMonth' in attrs):
            raise serializers.ValidationError({'bsMonth': 'bsYear and bsMonth go together'})
        return attrs


REPORT_KINDS = tuple(services.REPORTS)


class DirectoryQueryIn(serializers.Serializer):
    """GET /api/v1/group/people?companyId=&status=&gender=&employmentType=&branch=&department=&q=&page=&pageSize="""
    companyId = serializers.UUIDField(required=False, allow_null=True)
    status = serializers.ChoiceField([*Employee.Status.values, 'all'], default=Employee.Status.ACTIVE)
    gender = serializers.ChoiceField(Employee.Gender.values, required=False)
    employmentType = serializers.ChoiceField(EmployeeAssignment.EmploymentType.values, required=False)
    branch = serializers.CharField(max_length=120, required=False)
    department = serializers.CharField(max_length=120, required=False)
    q = serializers.CharField(max_length=100, required=False)
    sort = serializers.ChoiceField(('name', 'code', 'company', 'joined', '-joined'), default='name')
    page = serializers.IntegerField(min_value=1, default=1)
    pageSize = serializers.IntegerField(min_value=1, max_value=500, default=50)
    format = serializers.ChoiceField(('json', 'csv'), default='json')
    export = serializers.ChoiceField(('people', 'breakdowns'), default='people')


class SwitchIn(serializers.Serializer):
    companyId = serializers.UUIDField()


class GroupRoleIn(serializers.Serializer):
    email = serializers.EmailField(max_length=200)
    roles = serializers.ListField(child=serializers.ChoiceField(GROUP_ROLES), min_length=1)


class PayPolicyIn(serializers.Serializer):
    """PUT /api/v1/group/pay-policy {hidePayFromExecutives}"""
    hidePayFromExecutives = serializers.BooleanField()
