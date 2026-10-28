"""
Leave JSON API for Bearer-token clients (the TS routes under /api/v1/leave, same paths and shapes; docs/api.md). DRF
views with the project defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn, the TS error shape and
JsonRenderer. Rules, scoping and validation stay in services.py; a balance shortfall answers {error, suggestion}.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import HasModule, HasRole, IsSignedIn
from hajiri.core.api import exception_handler as default_handler
from hajiri.core.validation import parse_uuid, plain, validate

from . import services
from .serializers import (
    AdjustIn, BalancesQueryIn, CancelIn, LedgerQueryIn, OrderIn, PreviewQueryIn, RequestIn, TeamCalendarQueryIn, TypeIn,
)


def exception_handler(exc, context):
    """The project handler, plus the suggestion of a leave shortfall."""
    resp = default_handler(exc, context)
    if resp is not None and isinstance(exc, services.LeaveSuggestionError):
        resp.data['suggestion'] = exc.suggestion
    return resp


def _query(request) -> dict:
    return request.query_params.dict()


class LeaveApiView(APIView):
    permission_classes = [IsSignedIn]

    def get_exception_handler(self):
        return exception_handler


class TypesView(LeaveApiView):
    """GET /leave/types (everyone in the company); POST creates one (attendance area)."""

    def get_permissions(self):
        if self.request.method == 'POST':
            return [HasModule('attendance')()]
        return super().get_permissions()

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'types': services.list_types(request.user)})

    @extend_schema(request=TypeIn, responses={201: dict})
    def post(self, request):
        return Response({'id': services.create_type(request.user, request.data)}, status=status.HTTP_201_CREATED)


class TypeView(LeaveApiView):
    permission_classes = [HasModule('attendance')]

    @extend_schema(request=TypeIn, responses=dict)
    def patch(self, request, id):
        services.update_type(request.user, parse_uuid(id), request.data)
        return Response({'ok': True})


class TypesOrderView(LeaveApiView):
    permission_classes = [HasRole('hr')]

    @extend_schema(request=OrderIn, responses=dict)
    def post(self, request):
        b = plain(validate(OrderIn, request.data))
        services.reorder_types(request.user, b['ids'])
        return Response({'ok': True})


class BalancesView(LeaveApiView):
    @extend_schema(parameters=[BalancesQueryIn], responses=dict)
    def get(self, request):
        q = plain(validate(BalancesQueryIn, _query(request)))
        return Response(services.get_balances(request.user, q.get('employeeId'), q.get('asOf')))


class PreviewView(LeaveApiView):
    @extend_schema(parameters=[PreviewQueryIn], responses=dict)
    def get(self, request):
        return Response(services.preview(request.user, _query(request)))


class RequestsView(LeaveApiView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'requests': services.list_requests(request.user, _query(request))})

    @extend_schema(request=RequestIn, responses={201: dict})
    def post(self, request):
        return Response(services.create_request(request.user, request.data), status=status.HTTP_201_CREATED)


class CancelView(LeaveApiView):
    @extend_schema(request=CancelIn, responses=dict)
    def post(self, request, id):
        services.cancel_request(request.user, parse_uuid(id), request.data or {})
        return Response({'ok': True})


class AccrualRunView(LeaveApiView):
    permission_classes = [HasModule('attendance')]

    @extend_schema(request=None, responses=dict)
    def post(self, request):
        return Response(services.run_accrual(request.user))


class AdjustView(LeaveApiView):
    permission_classes = [HasModule('attendance')]

    @extend_schema(request=AdjustIn, responses={201: dict})
    def post(self, request):
        return Response(services.adjust(request.user, request.data), status=status.HTTP_201_CREATED)


class LedgerView(LeaveApiView):
    @extend_schema(parameters=[LedgerQueryIn], responses=dict)
    def get(self, request):
        q = plain(validate(LedgerQueryIn, _query(request)))
        return Response({'ledger': services.ledger(request.user, q.get('employeeId'), q.get('leaveTypeId'))})


class TeamCalendarView(LeaveApiView):
    @extend_schema(parameters=[TeamCalendarQueryIn], responses=dict)
    def get(self, request):
        q = _query(request)
        return Response({'days': services.team_calendar(request.user, q.get('from'), q.get('to'))})
