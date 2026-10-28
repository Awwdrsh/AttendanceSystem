"""List filters of the learning pages (django-filter), applied to company-scoped querysets."""
from __future__ import annotations

import django_filters

from hajiri.db.models import LrnEnrollment


class EnrollmentFilter(django_filters.FilterSet):
    """The enrolment list's course and status filters (?courseId=&status=)."""
    courseId = django_filters.UUIDFilter(field_name='course_id')
    status = django_filters.CharFilter(field_name='status')

    class Meta:
        model = LrnEnrollment
        fields = ['courseId', 'status']
