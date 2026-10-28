"""django-filter FilterSets for the shifts lists (GET /shifts/holidays). Services validate the TS query first (zod
wording, hajiri.core.validation) and hand the cleaned values to the FilterSet, which only filters."""
from __future__ import annotations

import django_filters
from django.db.models import Q

from hajiri.db.models import Holiday


class HolidayFilter(django_filters.FilterSet):
    """?from=&to=&branchId=: a branch sees its own holidays plus the company-wide ones (branch_id NULL)."""
    date_from = django_filters.DateFilter(field_name='local_date', lookup_expr='gte')
    date_to = django_filters.DateFilter(field_name='local_date', lookup_expr='lte')
    branch = django_filters.UUIDFilter(method='for_branch')

    class Meta:
        model = Holiday
        fields: list[str] = []

    def for_branch(self, queryset, name, value):
        return queryset.filter(Q(branch__isnull=True) | Q(branch_id=value))
