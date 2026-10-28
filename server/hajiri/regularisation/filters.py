"""django-filter FilterSet for correction request lists (GET /regularisation, the request list partial). The query is
validated first with serializers.CorrectionQueryIn (zod messages); the FilterSet then narrows the company-scoped queryset."""
from __future__ import annotations

import django_filters

from hajiri.db.models import RegularisationRequest


class RequestFilter(django_filters.FilterSet):
    employeeId = django_filters.UUIDFilter(field_name='employee_id')
    status = django_filters.ChoiceFilter(choices=RegularisationRequest.Status.choices)
    to = django_filters.DateFilter(field_name='local_date', lookup_expr='lte')

    class Meta:
        model = RegularisationRequest
        fields: list[str] = []


# ?from= ('from' is a Python keyword, so the filter is added after the class body).
RequestFilter.base_filters['from'] = django_filters.DateFilter(field_name='local_date', lookup_expr='gte')
