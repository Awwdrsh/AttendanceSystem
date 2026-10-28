"""
List filters for claims and trips (django-filter). The query is first validated with the serializers in
serializers.py (the TS wording for a bad value); the FilterSet then narrows the active company's queryset. `scope`
(own / team / all) needs the signed-in user, so it is passed as the request.
"""
from __future__ import annotations

import django_filters
from django.db.models import F, Q

from hajiri.core.auth import has_role, manages
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.db.models import ExpenseClaim, TravelRequest


def default_scope(user) -> str:
    return 'all' if has_role(user, 'hr') else 'team' if has_role(user, 'manager') else 'own'


def scope_q(user, scope: str) -> Q:
    """Own records, a manager's reports (and own), or the whole company for HR."""
    if scope == 'all':
        if not has_role(user, 'hr'):
            raise ForbiddenError('only HR can see everyone')
        return Q()
    if scope == 'team':
        if not has_role(user, 'manager', 'hr') or not user.employee_id:
            raise ForbiddenError('only managers can see their team')
        return Q(employee_id=user.employee_id) | Q(manages(user.employee_id, F('employee_id')))
    if not user.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    return Q(employee_id=user.employee_id)


class _ScopedFilter(django_filters.FilterSet):
    """`scope` defaults by role: HR everyone, managers their team, others their own."""
    scope = django_filters.CharFilter(method='filter_scope')

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if not self.form.cleaned_data.get('scope'):
            queryset = self.filter_scope(queryset, 'scope', default_scope(self.request))
        return queryset

    def filter_scope(self, queryset, name, value):
        return queryset.filter(scope_q(self.request, value))


class ClaimFilter(_ScopedFilter):
    status = django_filters.ChoiceFilter(choices=ExpenseClaim.Status.choices)
    employeeId = django_filters.UUIDFilter(field_name='employee_id')

    class Meta:
        model = ExpenseClaim
        fields = ['scope', 'status', 'employeeId']

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        # Drafts are private to the employee.
        me = self.request.employee_id
        return queryset.filter(~Q(status=ExpenseClaim.Status.DRAFT) | Q(employee_id=me)) if me \
            else queryset.exclude(status=ExpenseClaim.Status.DRAFT)


class TripFilter(_ScopedFilter):
    status = django_filters.ChoiceFilter(choices=TravelRequest.Status.choices)

    class Meta:
        model = TravelRequest
        fields = ['scope', 'status']


def apply(filterset_class, user, queryset, query: dict):
    """The filtered queryset; `user` (the AuthUser) stands in for the request."""
    return filterset_class(query, queryset=queryset, request=user).qs
