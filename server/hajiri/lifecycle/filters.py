"""django-filter FilterSet of the asset register (status, holder, search by tag, name or serial). The query is
validated first (serializers.AssetQuery)."""
from __future__ import annotations

import django_filters
from django.db.models import Q


class AssetFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    employeeId = django_filters.UUIDFilter(field_name='g__employee_id')
    search = django_filters.CharFilter(method='by_text')

    def by_text(self, qs, name, value):
        if not value:
            return qs
        return qs.filter(Q(tag__icontains=value) | Q(name__icontains=value) | Q(serial__icontains=value))
