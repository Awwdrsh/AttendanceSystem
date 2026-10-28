"""
Insights JSON API for Bearer-token clients (the TS routes under /api/v1/insights): the computation, the filter
options and every dataset as CSV. DRF views with the project defaults (hajiri.core.api); the area's roles (HR and
executives) are checked first, like the TS router's requireModule('insights'). The rules stay in services.py.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import HasModule
from hajiri.reports.csv import csv_response

from . import services
from .serializers import FilterOptionsOut, InsightsFilterIn


class InsightsView(APIView):
    """GET /api/v1/insights?from=&to=&branchId=&orgUnitId=: every figure of the Insights page for the active company."""
    permission_classes = [HasModule('insights')]

    @extend_schema(parameters=[InsightsFilterIn], responses=dict)
    def get(self, request):
        return Response(services.insights(request.user, request.query_params.dict()))


class InsightsFiltersView(APIView):
    """GET /api/v1/insights/filters: the active company's branches and departments."""
    permission_classes = [HasModule('insights')]

    @extend_schema(responses=FilterOptionsOut)
    def get(self, request):
        return Response(services.filter_options(request.user))


class InsightsExportView(APIView):
    """GET /api/v1/insights/export/<dataset>: one dataset as CSV for the same filters."""
    permission_classes = [HasModule('insights')]

    @extend_schema(parameters=[InsightsFilterIn], responses={(200, 'text/csv'): OpenApiResponse(description='CSV file')})
    def get(self, request, dataset):
        filename, columns, out = services.export_dataset(request.user, dataset, request.query_params.dict())
        return csv_response(filename, columns, out)
