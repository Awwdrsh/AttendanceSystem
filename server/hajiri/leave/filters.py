"""django-filter filter sets for leave lists (the query parameters are validated first by serializers.ListQueryIn)."""
from __future__ import annotations

import django_filters

from hajiri.db.models import LeaveRequest


class LeaveRequestFilter(django_filters.FilterSet):
    """employee, status, and the overlap with a date window: requests ending on or after `ends_from` and starting on or
    before `starts_to` (the TS ?from=&to=)."""
    employee = django_filters.UUIDFilter(field_name='employee_id')
    status = django_filters.ChoiceFilter(choices=LeaveRequest.Status.choices)
    ends_from = django_filters.DateFilter(field_name='to_date', lookup_expr='gte')
    starts_to = django_filters.DateFilter(field_name='from_date', lookup_expr='lte')

    class Meta:
        model = LeaveRequest
        fields = ['employee', 'status', 'ends_from', 'starts_to']
