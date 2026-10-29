"""
Employee master JSON API (the routes of apps/api/src/modules/employees/routes.ts under /api/v1/employees), used by the
React app and Bearer-token clients. DRF views with the project defaults (hajiri.core.api: HajiriAuthentication,
IsSignedIn, the TS error shape, JsonRenderer). Roles, company scoping and validation stay in service.py, so the API
and the pages answer the same way; ids of another company are 404, a malformed id the TS 400 "Invalid UUID".
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication

from . import service as svc
from .serializers import EmployeeIn, EmployeeListQuery


class ApiView(APIView):
    """The session cookie with the TS Origin check (the React app sends no CSRF token) or a Bearer token."""
    authentication_classes = [SameOriginAuthentication]


class EmployeesView(ApiView):
    """/api/v1/employees: the employee master of the active company."""

    @extend_schema(operation_id='employees_list', parameters=[EmployeeListQuery], responses=dict)
    def get(self, request):
        """HR and the payroll and attendance admins: the whole company; a manager: their people and themselves.
        ?status=&search=&branchId=&pan= (PAN lookup: HR, audited)."""
        return Response({'employees': svc.list_employees(request.user, request.query_params.dict())})

    @extend_schema(operation_id='employees_create', request=EmployeeIn, responses={201: dict})
    def post(self, request):
        return Response({'id': svc.create_employee(request.user, request.data)}, status=status.HTTP_201_CREATED)


class EmployeeView(ApiView):
    @extend_schema(operation_id='employees_get', responses=dict)
    def get(self, request, id):
        return Response({'employee': svc.get_employee(request.user, id)})

    @extend_schema(operation_id='employees_update', request=EmployeeIn, responses=dict)
    def patch(self, request, id):
        return Response({'employee': svc.update_employee(request.user, id, request.data)})


class SensitiveView(ApiView):
    @extend_schema(operation_id='employees_sensitive', responses=dict)
    def get(self, request, id):
        """PAN and the full bank account, decrypted for HR and audited."""
        return Response(svc.sensitive(request.user, id))


class AssignmentsView(ApiView):
    @extend_schema(operation_id='employees_assignments', responses=dict)
    def get(self, request, id):
        return Response({'assignments': svc.assignments(request.user, id)})


class AuditView(ApiView):
    @extend_schema(operation_id='employees_audit', responses=dict)
    def get(self, request, id):
        # audit_log.id is a bigint, which the TS API (node-postgres) sent as a string.
        return Response({'events': [{**e, 'id': str(e['id'])} for e in svc.audit_events(request.user, id)]})


class ImportView(ApiView):
    @extend_schema(operation_id='employees_import', request=dict, parameters=[OpenApiParameter('dryRun', str, required=False)],
                   responses={200: dict, 201: dict, 400: dict})
    def post(self, request):
        """CSV as text/csv or {"csv": "..."}; ?dryRun=true only validates."""
        if (request.content_type or '').startswith('text/'):
            text = request.body.decode('utf-8-sig', errors='replace')       # text/csv is not one of DRF's parsers
        else:
            data = request.data
            text = data.get('csv') if hasattr(data, 'get') else None
            text = text if isinstance(text, str) else None
        code, body = svc.import_csv(request.user, text, dry_run=request.query_params.get('dryRun') == 'true')
        return Response(body, status=code)
