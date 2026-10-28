"""JSON versions of the group dashboard and reports for Bearer-token clients (DRF). Access rules are the services'
(group_scope): anyone outside group views gets 403, a company outside the scope 404."""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import IsSignedIn
from hajiri.core.errors import NotFoundError
from hajiri.core.validation import validate

from . import services
from .serializers import REPORT_KINDS, ReportQueryIn


class DashboardView(APIView):
    """GET /api/v1/group/dashboard: the consolidated dashboard (per company and totals)."""
    permission_classes = [IsSignedIn]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.dashboard(request.user))


class ReportView(APIView):
    """GET /api/v1/group/reports/<kind>: one consolidated report as {title, period, columns, rows}."""
    permission_classes = [IsSignedIn]

    @extend_schema(parameters=[OpenApiParameter('kind', str, OpenApiParameter.PATH, enum=list(REPORT_KINDS))],
                   responses=dict)
    def get(self, request, kind):
        if kind not in REPORT_KINDS:
            raise NotFoundError('report not found')
        q = validate(ReportQueryIn, request.query_params.dict())
        period = services.period_of(q['period'], bs_year=q.get('bsYear'), bs_month=q.get('bsMonth'), fy=q.get('fy'))
        cid = str(q['companyId']) if q.get('companyId') else None
        return Response(services.report(request.user, kind, period, cid).to_dict())
