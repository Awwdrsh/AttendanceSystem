"""django-filter FilterSets for the people lists (directory). The query is validated first (serializers.py) so a bad
value is refused with the TS message; the FilterSet then narrows the company-scoped queryset."""
from __future__ import annotations

import django_filters
from django.db.models import Q


class DirectoryFilter(django_filters.FilterSet):
    """Directory search: name, code, designation, department or a skill; department; branch (current assignment)."""
    search = django_filters.CharFilter(method='by_text')
    orgUnitId = django_filters.UUIDFilter(field_name='org_unit_id')
    branchId = django_filters.UUIDFilter(field_name='branch_id')

    def by_text(self, qs, name, value):
        if not value:
            return qs
        return qs.filter(Q(name_en__icontains=value) | Q(emp_code__icontains=value) | Q(designation__icontains=value)
                         | Q(unit_name__icontains=value) | Q(skills_text__icontains=value))
