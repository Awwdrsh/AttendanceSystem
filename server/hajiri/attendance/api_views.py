"""The TS /api/v1/attendance routes (apps/api/src/modules/attendance/routes.ts) as DRF views for the React app and
Bearer-token clients. The rules (roles, manager scope, the ACTIVE company) are the functions in api.py, which the
pages call too; cookie requests are checked by Origin like the TS API."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication

from . import api


class _Api(APIView):
    authentication_classes = [SameOriginAuthentication]


class RecomputeView(_Api):
    @extend_schema(operation_id='attendance_recompute', request=api.RecomputeIn, responses=OpenApiTypes.OBJECT,
                   summary='Recompute one day now')
    def post(self, request):
        return Response(api.recompute(request.user, request.data))


class RecomputeRangeView(_Api):
    @extend_schema(operation_id='attendance_recompute_range', request=api.RecomputeRangeIn, responses=OpenApiTypes.OBJECT,
                   summary='Recompute a range (small ranges at once, larger ones in the worker)')
    def post(self, request):
        return Response(api.recompute_range(request.user, request.data))


class DaysView(_Api):
    @extend_schema(operation_id='attendance_days', parameters=[api.EmployeeRangeIn], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'days': api.days(request.user, request.query_params.dict())})


class PunchesView(_Api):
    @extend_schema(operation_id='attendance_punches', parameters=[api.EmployeeRangeIn], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'punches': api.punches(request.user, request.query_params.dict())})


class ExceptionsView(_Api):
    @extend_schema(operation_id='attendance_exceptions', parameters=[api.ExceptionsIn], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'exceptions': api.exceptions(request.user, request.query_params.dict())})


class TodayView(_Api):
    @extend_schema(operation_id='attendance_today', parameters=[api.TodayIn], responses=OpenApiTypes.OBJECT, summary='Team Today')
    def get(self, request):
        return Response(api.today(request.user, request.query_params.dict()))
