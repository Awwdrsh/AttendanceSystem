"""django-filter FilterSets for the time lists (GET /time/timesheets and the reports). Services validate the TS query
first (zod wording, hajiri.core.validation) and hand the cleaned values to the FilterSet, which only filters."""
from __future__ import annotations

from datetime import timedelta

import django_filters

from hajiri.core.errors import BadRequestError
from hajiri.db.models import TimeEntry, Timesheet


class TimesheetFilter(django_filters.FilterSet):
    """?status=&from=&to=&employeeId=. `from` keeps the weeks that end on or after it (a week that started up to six
    days before still overlaps)."""
    status = django_filters.ChoiceFilter(choices=Timesheet.Status.choices)
    week_from = django_filters.DateFilter(method='ending_after')
    week_to = django_filters.DateFilter(field_name='week_start', lookup_expr='lte')
    employee = django_filters.UUIDFilter(field_name='employee_id')

    class Meta:
        model = Timesheet
        fields: list[str] = []

    def ending_after(self, queryset, name, value):
        return queryset.filter(week_start__gte=value - timedelta(days=6))


class ReportEntryFilter(django_filters.FilterSet):
    """?employeeId=&projectId=&clientId= on a report's entries."""
    employee = django_filters.UUIDFilter(field_name='employee_id')
    project = django_filters.UUIDFilter(field_name='project_id')
    client = django_filters.UUIDFilter(field_name='project__client_id')

    class Meta:
        model = TimeEntry
        fields: list[str] = []


def apply(filterset_class, data: dict, queryset):
    """Filter a queryset with already validated values (keys without a value are left out)."""
    fs = filterset_class({k: v for k, v in data.items() if v not in (None, '')}, queryset=queryset)
    if not fs.is_valid():                       # the serializer ran first; never filter on unreadable input
        raise BadRequestError('invalid value format')
    return fs.qs
