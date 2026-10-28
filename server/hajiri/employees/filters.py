"""django-filter FilterSet of the employee list (status, search, branch, PAN lookup through the blind index). The
query is validated first (serializers.EmployeeListQuery) so bad values get the TS messages."""
from __future__ import annotations

import django_filters
from django.db.models import Q

from hajiri.core.crypto import field_cipher


class EmployeeFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    search = django_filters.CharFilter(method='by_text')
    branchId = django_filters.UUIDFilter(field_name='branch_id')
    pan = django_filters.CharFilter(method='by_pan')

    def by_text(self, qs, name, value):
        """Code or name (English or Nepali); typed % and _ are matched literally."""
        if not value:
            return qs
        return qs.filter(Q(emp_code__icontains=value) | Q(name_en__icontains=value) | Q(name_ne__icontains=value))

    def by_pan(self, qs, name, value):
        return qs.filter(pan_bidx=field_cipher().blind_index(value)) if value else qs
