"""The TS /api/v1/reports routes (reports/routes.ts and catalog.ts) for the React app and Bearer-token clients: the
attendance reports, the People record reports and the ad-hoc report builder with saved reports. JSON `{columns, rows}`
by default, `?format=csv` for Excel. Every route needs HR or a manager (as the TS router did); the services narrow it
further (HR-only reports, the builder) and scope everything to the ACTIVE company."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import HasRole, SameOriginAuthentication
from hajiri.core.validation import validate

from . import service
from .csv import csv_response, send_table
from .people import people_report

FORMAT = OpenApiParameter('format', str, enum=['json', 'csv'], required=False)


class FormatIn(serializers.Serializer):
    format = serializers.ChoiceField(('json', 'csv'), required=False)


def _wants_csv(request) -> bool:
    return validate(FormatIn, {k: v for k, v in request.query_params.items() if k == 'format'}).get('format') == 'csv'


def _table(out):
    """A Table as JSON ({columns, rows}), or an HttpResponse (CSV) as is."""
    return Response(out.to_dict()) if hasattr(out, 'to_dict') else out


class _Api(APIView):
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [HasRole('hr', 'manager')]


class AttendanceReportView(_Api):
    @extend_schema(operation_id='reports_attendance', parameters=[FORMAT], responses=OpenApiTypes.OBJECT,
                   summary='daily, register, ot, late-early, leave-balances, headcount, exceptions-summary, devices, punch-audit')
    def get(self, request, name):
        q = request.query_params.dict()
        t = service.attendance_report(request.user, name, q)
        if q.get('format') == 'csv':
            return csv_response(t.filename, t.columns, t.rows)       # TS sendReport: values as stored, no BS dates
        return Response(t.to_dict())


class PeopleReportView(_Api):
    @extend_schema(operation_id='reports_people', parameters=[FORMAT], responses=OpenApiTypes.OBJECT, summary='People record reports')
    def get(self, request, name):
        q = request.query_params.dict()
        return _table(send_table(q.get('format'), people_report(request.user, name, q)))


class DatasetsView(_Api):
    @extend_schema(operation_id='reports_adhoc_datasets', responses=OpenApiTypes.OBJECT, summary='Report builder datasets (HR)')
    def get(self, request):
        return Response(service.builder_datasets(request.user))


class AdhocRunView(_Api):
    @extend_schema(operation_id='reports_adhoc_run', parameters=[FORMAT], request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT,
                   summary='Run a report definition (HR): a preview of 100 rows, or CSV')
    def post(self, request):
        out = service.run_adhoc(request.user, request.data, csv=_wants_csv(request))
        return out if not isinstance(out, dict) else Response(out)


class SavedReportsView(_Api):
    @extend_schema(operation_id='reports_saved_list', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'reports': service.list_saved(request.user)})

    @extend_schema(operation_id='reports_saved_create', request=service.SavedReportIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        return Response({'report': service.create_saved(request.user, request.data)}, status=status.HTTP_201_CREATED)


class SavedReportView(_Api):
    @extend_schema(operation_id='reports_saved_update', request=service.SavedReportIn, responses=OpenApiTypes.OBJECT)
    def put(self, request, id):
        return Response({'report': service.update_saved(request.user, id, request.data)})

    @extend_schema(operation_id='reports_saved_delete', responses=OpenApiTypes.OBJECT)
    def delete(self, request, id):
        service.delete_saved(request.user, id)
        return Response({'ok': True})


class SavedReportRunView(_Api):
    @extend_schema(operation_id='reports_saved_run', parameters=[FORMAT], responses=OpenApiTypes.OBJECT)
    def get(self, request, id):
        out = service.run_saved(request.user, id, csv=_wants_csv(request))
        return out if not isinstance(out, dict) else Response(out)
