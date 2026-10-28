"""django-filter FilterSets for the overtime lists. The services validate the TS query first (serializers.py, zod
wording); these sets then apply the validated keys to the company-scoped querysets."""
from __future__ import annotations

import django_filters

from hajiri.db.models import OtRequest, OtSlip


class _OtFilter(django_filters.FilterSet):
    employeeId = django_filters.UUIDFilter(field_name='employee_id')
    to = django_filters.DateFilter(field_name='local_date', lookup_expr='lte')
    # `from` is a Python keyword, so it goes into the class namespace by name.
    vars()['from'] = django_filters.DateFilter(field_name='local_date', lookup_expr='gte')


class OtRequestFilter(_OtFilter):
    status = django_filters.ChoiceFilter(choices=OtRequest.Status.choices)

    class Meta:
        model = OtRequest
        fields = ()


class OtSlipFilter(_OtFilter):
    status = django_filters.ChoiceFilter(choices=OtSlip.Status.choices)

    class Meta:
        model = OtSlip
        fields = ()
