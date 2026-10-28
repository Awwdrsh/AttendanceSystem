"""
People JSON API for Bearer-token clients (the read routes of apps/api/src/modules/people under /api/v1/people): the
directory, one person's card, the org chart and the org structure of the active company. DRF views with the project
defaults (hajiri.core.api); the rules stay in service.py.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from hajiri.core.validation import plain

from . import service as svc
from .serializers import DirectoryQuery


class PeopleViewSet(viewsets.ViewSet):
    """/api/v1/people[...]: who works here (everyone signed in)."""
    lookup_value_regex = '[0-9a-fA-F-]{36}'

    @extend_schema(operation_id='api_v1_people_list', request=None, parameters=[DirectoryQuery], responses=dict)
    def list(self, request):
        """The directory: ?search=&orgUnitId=&branchId=."""
        return Response(plain(svc.directory(request.user, request.query_params.dict())))

    @extend_schema(responses=dict)
    def retrieve(self, request, pk=None):
        """One person's card with their direct reports."""
        return Response(plain(svc.card(request.user, pk)))

    @extend_schema(responses=dict)
    @action(detail=False, url_path='org-chart')
    def org_chart(self, request):
        return Response(plain(svc.org_chart(request.user)))

    @extend_schema(responses=dict)
    @action(detail=False)
    def structure(self, request):
        return Response(plain(svc.structure(request.user)))

    @extend_schema(responses=dict)
    @action(detail=False)
    def summary(self, request):
        return Response(plain(svc.summary(request.user)))
