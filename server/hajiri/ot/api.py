"""
Overtime JSON API (the TS routes under /api/v1/ot), called by the React app (frontend/src/api/index.js `ot`) and by
Bearer-token clients. DRF views with SameOriginAuthentication (the session cookie checked against the Origin, as the
TS sameOriginGuard did, since the React app sends no CSRF token) and the project defaults: IsSignedIn, the TS error
shape and JsonRenderer. Roles and validation are checked by the services, with the TS messages.
"""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication
from hajiri.core.validation import plain

from . import services
from .serializers import (OtApprovedOut, OtApproveSlipIn, OtCompOffOut, OtCreatedOut, OtOkOut, OtRegisterQueryIn,
                          OtRejectSlipIn, OtRequestIn, OtRequestsOut, OtRequestsQueryIn, OtSlipsOut, OtSlipsQueryIn)


def _query(request) -> dict:
    return request.query_params.dict()


class _OtView(APIView):
    """Cookie requests from the React app pass the Origin check; Bearer clients need none."""
    authentication_classes = [SameOriginAuthentication]


class RequestsView(_OtView):
    """/api/v1/ot/requests: list the requests in the user's scope, or ask for overtime pre-approval."""

    @extend_schema(parameters=[OtRequestsQueryIn], responses=OtRequestsOut)
    def get(self, request):
        return Response({'requests': plain(services.list_requests(request.user, _query(request)))})

    @extend_schema(request=OtRequestIn, responses={201: OtCreatedOut})
    def post(self, request):
        return Response(services.create_request(request.user, request.data), status=status.HTTP_201_CREATED)


class RequestCancelView(_OtView):
    @extend_schema(request=None, responses=OtOkOut)
    def post(self, request, id):
        return Response(services.cancel_request(request.user, id))


class SlipsView(_OtView):
    """/api/v1/ot/slips: overtime detected from punches (HR all, a manager their team, an employee their own)."""

    @extend_schema(parameters=[OtSlipsQueryIn], responses=OtSlipsOut)
    def get(self, request):
        return Response({'slips': plain(services.list_slips(request.user, _query(request)))})


class SlipApproveView(_OtView):
    @extend_schema(request=OtApproveSlipIn, responses=OtApprovedOut)
    def post(self, request, id):
        return Response(services.approve_slip(request.user, id, request.data or {}))


class SlipRejectView(_OtView):
    @extend_schema(request=OtRejectSlipIn, responses=OtOkOut)
    def post(self, request, id):
        return Response(services.reject_slip(request.user, id, request.data))


class SlipCompOffView(_OtView):
    @extend_schema(request=None, responses=OtCompOffOut)
    def post(self, request, id):
        return Response(services.comp_off_slip(request.user, id))


class _JsonOnly(BaseContentNegotiation):
    """Always the JSON renderer: `?format=` is the register's own parameter (json or csv), not DRF's format
    override, and the CSV answer is built by the view."""

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class RegisterView(_OtView):
    """/api/v1/ot/register: the inspector-ready overtime register (JSON or CSV)."""
    content_negotiation_class = _JsonOnly

    @extend_schema(parameters=[OtRegisterQueryIn],
                   responses={200: OpenApiResponse(description='{"register": [...]} or text/csv')})
    def get(self, request):
        format_, data = services.register(request.user, _query(request))
        if format_ == 'csv':
            return HttpResponse(services.register_csv(data), content_type='text/csv; charset=utf-8')
        return Response({'register': plain(data)})
