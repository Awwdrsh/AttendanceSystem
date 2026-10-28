"""django-filter FilterSets of the letters register and the letter requests list. Queries are validated first with
the serializers (LettersQuery, RequestsQuery), so the filters only narrow an already company-scoped queryset."""
from __future__ import annotations

import django_filters
from django.db.models import Q

from hajiri.db.models import LetterRequest

OPEN_REQUEST = (LetterRequest.Status.PENDING, LetterRequest.Status.APPROVED)


class LetterFilter(django_filters.FilterSet):
    employeeId = django_filters.UUIDFilter(field_name='employee_id')
    q = django_filters.CharFilter(method='by_text')

    def by_text(self, qs, name, value):
        term = (value or '').strip()
        if not term:
            return qs
        return qs.filter(Q(ref_no__icontains=term) | Q(employee__name_en__icontains=term)
                         | Q(employee__emp_code__icontains=term) | Q(subject__icontains=term))


class LetterRequestFilter(django_filters.FilterSet):
    employeeId = django_filters.UUIDFilter(field_name='employee_id')
    status = django_filters.CharFilter(method='by_status')

    def by_status(self, qs, name, value):
        if not value:
            return qs
        if value == 'open':
            return qs.filter(status__in=OPEN_REQUEST)
        return qs.filter(status=value)
