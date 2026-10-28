"""
List filters of employee relations (django-filter). The services validate a query with its serializer first (so a bad
value is refused with the zod wording the TS API used) and then apply these to the company-scoped queryset; the
FilterSets take the TS query keys (employeeId, status), and status 'all' means no status filter.
"""
from __future__ import annotations

import django_filters as filters

from hajiri.db.models import DisciplinaryCase, EmployeeTempAssignment, Grievance, RegisteredDevice


def _status_unless_all(qs, name, value):
    return qs if value in (None, '', 'all') else qs.filter(**{name: value})


class GrievanceFilter(filters.FilterSet):
    status = filters.CharFilter(field_name='status')

    class Meta:
        model = Grievance
        fields = ['status']


class CaseFilter(filters.FilterSet):
    employeeId = filters.UUIDFilter(field_name='employee_id')
    status = filters.CharFilter(field_name='status')

    class Meta:
        model = DisciplinaryCase
        fields = ['employeeId', 'status']


class AssignmentFilter(filters.FilterSet):
    employeeId = filters.UUIDFilter(field_name='employee_id')
    status = filters.CharFilter(field_name='status', method=_status_unless_all)

    class Meta:
        model = EmployeeTempAssignment
        fields = ['employeeId', 'status']


class DeviceFilter(filters.FilterSet):
    status = filters.CharFilter(field_name='status', method=_status_unless_all)

    class Meta:
        model = RegisteredDevice
        fields = ['status']


def apply(filterset: type[filters.FilterSet], query: dict, queryset):
    """The queryset narrowed by the (already validated) query."""
    return filterset({k: v for k, v in query.items() if v is not None}, queryset=queryset).qs
