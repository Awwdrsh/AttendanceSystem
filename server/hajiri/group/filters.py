"""django-filter FilterSet for the group directory (/group/people). The company choices come from the viewer's group
scope, so a company outside it is not a valid choice and filters nothing in."""
from __future__ import annotations

import django_filters
from django.db.models import Q

from hajiri.db.models import Employee, EmployeeAssignment

from . import services


class DirectoryFilter(django_filters.FilterSet):
    company = django_filters.ChoiceFilter(field_name='company_id', empty_label='All companies')
    status = django_filters.ChoiceFilter(choices=[*Employee.Status.choices, ('all', 'Everyone')], empty_label=None,
                                         method='by_status')
    gender = django_filters.ChoiceFilter(choices=Employee.Gender.choices, empty_label='Any gender')
    employment_type = django_filters.ChoiceFilter(choices=EmployeeAssignment.EmploymentType.choices,
                                                  empty_label='Any employment type')
    branch = django_filters.CharFilter(field_name='branch_name', lookup_expr='iexact')
    department = django_filters.CharFilter(field_name='department', lookup_expr='iexact')

    class Meta:
        model = Employee
        fields: list[str] = []

    def __init__(self, data=None, queryset=None, *, scope: services.GroupScope, **kw):
        data = data.copy() if data is not None else {}
        # The directory opens on active people; ?status=all shows everyone.
        if 'status' not in data:
            data['status'] = Employee.Status.ACTIVE
        super().__init__(data, queryset, **kw)
        self.filters['company'].extra['choices'] = [(c.id, c.name) for c in scope.companies]

    def by_status(self, queryset, name, value):
        return queryset if value in ('', 'all') else queryset.filter(Q(status=value))

    @property
    def qs(self):
        # A value that does not validate (a company outside the scope, a made-up status) drops out of cleaned_data
        # and filters nothing, instead of turning the page into an error.
        if not hasattr(self, '_qs'):
            self.is_valid()
            self._qs = self.filter_queryset(self.queryset.all())
        return self._qs
