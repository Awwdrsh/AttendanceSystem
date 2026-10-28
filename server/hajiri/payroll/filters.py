"""django-filter FilterSets for the payroll lists (query keys are the TS ones; values are validated by the serializers
first, so the filters only narrow the company-scoped queryset)."""
from __future__ import annotations

import django_filters

from hajiri.db.models import Loan, PayrollHold, SalaryAssignment


class HoldFilter(django_filters.FilterSet):
    """?status=held|releasing|released|cancelled, or open (held or being released through payroll)."""
    status = django_filters.CharFilter(method='by_status')

    class Meta:
        model = PayrollHold
        fields: list[str] = []

    def by_status(self, qs, name, value):
        if value == 'open':
            return qs.filter(status__in=(PayrollHold.Status.HELD, PayrollHold.Status.RELEASING))
        return qs.filter(status=value)


class EmployeeFilter(django_filters.FilterSet):
    """?employeeId=<uuid>: one employee's rows."""
    employeeId = django_filters.UUIDFilter(field_name='employee_id')


class LoanFilter(EmployeeFilter):
    class Meta:
        model = Loan
        fields: list[str] = []


class AssignmentFilter(EmployeeFilter):
    class Meta:
        model = SalaryAssignment
        fields: list[str] = []
