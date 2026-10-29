"""
Attendance-correction JSON API (the TS routes under /api/v1/regularisation) and the offline clock-in flush the page
script posts to. DRF views with the project defaults (hajiri.core.api): IsSignedIn, the TS error shape and
JsonRenderer. The /api/v1 views authenticate with SameOriginAuthentication (the React app sends the cookie with an
Origin check, never a CSRF token); the page script's offline flush keeps HajiriAuthentication (cookie with CSRF, or
Bearer). Rules and validation stay in services.py.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication

from . import services
from .serializers import CorrectionCreatedOut, CorrectionQueryIn, OfflineClockIn, CorrectionCancelOut, CorrectionIn, CorrectionListOut


class RequestsView(APIView):
    """/api/v1/regularisation: list (own, team or all) and file a correction."""
    authentication_classes = [SameOriginAuthentication]     # the React app's cookie requests: Origin check, as in TS

    @extend_schema(parameters=[CorrectionQueryIn], responses=CorrectionListOut)
    def get(self, request):
        return Response({'requests': services.list_requests(request.user, request.query_params.dict())})

    @extend_schema(request=CorrectionIn, responses={201: CorrectionCreatedOut})
    def post(self, request):
        return Response(services.create_request(request.user, request.data), status=status.HTTP_201_CREATED)


class CancelView(APIView):
    """/api/v1/regularisation/<id>/cancel: withdraw a pending correction."""
    authentication_classes = [SameOriginAuthentication]

    @extend_schema(request=None, responses=CorrectionCancelOut)
    def post(self, request, id):
        return Response(services.cancel_request(request.user, id))


class OfflineFlushView(APIView):
    """/_/regularisation/offline (app.js data-offline-flush-url): one clock-in queued while offline. 201 when filed,
    4xx {"error"} to drop it with that message."""

    @extend_schema(request=OfflineClockIn, responses={201: CorrectionCreatedOut})
    def post(self, request):
        return Response(services.flush_offline_punch(request.user, request.data), status=status.HTTP_201_CREATED)
