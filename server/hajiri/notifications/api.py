"""JSON inbox for Bearer-token clients (the TS /api/v1/notifications routes): the signed-in person's in-app
notifications in the company they are working in, with mark-read actions."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication
from hajiri.core.validation import parse_uuid

from . import service


class NotificationViewSet(viewsets.ViewSet):
    """GET /api/v1/notifications, POST /api/v1/notifications/read-all, POST /api/v1/notifications/<id>/read."""
    authentication_classes = [SameOriginAuthentication]     # the React app's cookie requests: Origin check, as in TS

    @extend_schema(parameters=[OpenApiParameter('limit', int), OpenApiParameter('before', str),
                               OpenApiParameter('unreadOnly', bool)], responses=OpenApiTypes.OBJECT, summary='My notifications (active company)')
    def list(self, request):
        return Response(service.inbox_from_query(request.user.id, request.query_params.dict(),
                                                 company_id=request.user.company_id))

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Mark every notification read')
    @action(detail=False, methods=['post'], url_path='read-all')
    def read_all(self, request):
        return Response({'marked': service.mark_all_read(request.user.id, company_id=request.user.company_id)})

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Mark one notification read',
                   parameters=[OpenApiParameter('id', OpenApiTypes.UUID, OpenApiParameter.PATH)])
    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        return Response(service.mark_read(request.user.id, parse_uuid(pk), company_id=request.user.company_id))
