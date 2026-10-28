"""
Employee master JSON API for Bearer-token clients (the TS routes under /api/v1/employees): DRF views with the project
defaults (hajiri.core.api: HajiriAuthentication, IsSignedIn, the TS error shape, JsonRenderer). Roles, company scoping
and validation stay in service.py, so the API and the pages answer the same way; ids of another company are 404.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from hajiri.core.api import Pagination
from hajiri.core.audit import audit
from hajiri.core.validation import plain

from . import service as svc
from .serializers import EmployeeIn, EmployeeListQuery


class EmployeeViewSet(viewsets.ViewSet):
    """/api/v1/employees[...]: the employee master of the active company."""
    lookup_value_regex = '[0-9a-fA-F-]{36}'

    @extend_schema(operation_id='api_v1_employees_list', request=None, parameters=[EmployeeListQuery], responses=dict)
    def list(self, request):
        """HR and the payroll and attendance admins: the whole company; a manager: their people. ?status=&search=&
        branchId=&pan= (HR), paginated with ?page=&pageSize=."""
        query = request.query_params.dict()
        rows = svc.employees_queryset(request.user, query)
        paginator = Pagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        out = [plain(svc._row(dict(r))) for r in page]
        if query.get('pan'):
            # As list_employees: one audit entry per PAN lookup, with every match counted (not only this page).
            audit('pan_lookup', 'employee', actor_id=request.user.id, after={'matches': paginator.page.paginator.count})
        return paginator.get_paginated_response(out)

    @extend_schema(request=EmployeeIn, responses={201: dict})
    def create(self, request):
        return Response({'id': svc.create_employee(request.user, request.data)}, status=status.HTTP_201_CREATED)

    @extend_schema(responses=dict)
    def retrieve(self, request, pk=None):
        return Response(plain(svc.get_employee(request.user, pk)))

    @extend_schema(request=EmployeeIn, responses=dict)
    def partial_update(self, request, pk=None):
        return Response(plain(svc.update_employee(request.user, pk, request.data)))

    @extend_schema(responses=dict)
    @action(detail=True)
    def sensitive(self, request, pk=None):
        """PAN and the full bank account, decrypted for HR and audited."""
        return Response(svc.sensitive(request.user, pk))

    @extend_schema(responses=dict)
    @action(detail=True)
    def assignments(self, request, pk=None):
        return Response(plain(svc.assignments(request.user, pk)))

    @extend_schema(responses=dict)
    @action(detail=True)
    def audit(self, request, pk=None):
        return Response(plain(svc.audit_events(request.user, pk)))

    @extend_schema(request=dict, responses=dict)
    @action(detail=False, methods=['post'], url_path='import')
    def import_csv(self, request):
        """CSV as text/csv or {"csv": "..."}; ?dryRun=true only validates."""
        if (request.content_type or '').startswith('text/'):
            text = request.body.decode('utf-8-sig', errors='replace')       # text/csv is not one of DRF's parsers
        else:
            text = request.data.get('csv') if hasattr(request.data, 'get') else None
        code, body = svc.import_csv(request.user, text, dry_run=request.query_params.get('dryRun') == 'true')
        return Response(body, status=code)
