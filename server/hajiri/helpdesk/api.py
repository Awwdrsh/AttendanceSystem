"""
Help desk JSON API for Bearer-token clients (the TS routes under /api/v1/helpdesk). DRF views with the project
defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn, the TS error shape and JsonRenderer. Each view adds the
feature switch and, where the TS route did, a role. Rules and validation stay in services.py.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, Pagination
from hajiri.core.validation import plain

from . import services
from .serializers import (
    CategoryIn, CategoryOut, CreatedTicketOut, MessageIn, PatchIn, QueueQueryIn, RateIn, TicketIn, TicketOut,
)


class HelpdeskViewSet(viewsets.ViewSet):
    """/api/v1/helpdesk/tickets[...]: raise, list, read and work tickets."""
    permission_classes = [IsSignedIn, FeatureEnabled('helpdesk')]
    lookup_value_regex = '[0-9a-fA-F-]{36}'

    @extend_schema(request=None, parameters=[QueueQueryIn], responses=TicketOut(many=True))
    def list(self, request):
        """The queue (HR: every ticket; owners and assignees: theirs), paginated: ?page=&pageSize=."""
        rows = services.ticket_values(services.queue_queryset(request.user, request.query_params.dict()))
        paginator = Pagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response([plain(r) for r in page])

    @extend_schema(request=TicketIn, responses={201: CreatedTicketOut})
    def create(self, request):
        return Response(services.create_ticket(request.user, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(responses=dict)
    def retrieve(self, request, pk=None):
        return Response(services.ticket_detail(request.user, pk))

    @extend_schema(request=PatchIn, responses=dict)
    def partial_update(self, request, pk=None):
        return Response(services.update_ticket(request.user, pk, request.data))

    @extend_schema(responses=TicketOut(many=True))
    @action(detail=False)
    def mine(self, request):
        return Response(services.my_tickets(request.user))

    @extend_schema(request=MessageIn, responses={201: dict})
    @action(detail=True, methods=['post'])
    def messages(self, request, pk=None):
        return Response(services.add_message(request.user, pk, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=dict)
    @action(detail=True, methods=['post'])
    def reopen(self, request, pk=None):
        return Response(services.reopen_ticket(request.user, pk))

    @extend_schema(request=None, responses=dict)
    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        return Response(services.close_ticket(request.user, pk))

    @extend_schema(request=RateIn, responses=dict)
    @action(detail=True, methods=['post'])
    def rate(self, request, pk=None):
        return Response(services.rate_ticket(request.user, pk, request.data))


class CategoryViewSet(viewsets.ViewSet):
    """/api/v1/helpdesk/categories: everyone lists the active topics; HR adds them."""
    permission_classes = [IsSignedIn, FeatureEnabled('helpdesk')]
    serializer_class = CategoryOut   # for the schema only; services shape the rows

    @extend_schema(responses=CategoryOut(many=True))
    def list(self, request):
        return Response(services.list_categories(request.user))

    def get_permissions(self):
        if self.action == 'create':
            return [HasRole('hr')(), FeatureEnabled('helpdesk')()]
        return super().get_permissions()

    @extend_schema(request=CategoryIn, responses={201: dict})
    def create(self, request):
        return Response(services.create_category(request.user, request.data), status=status.HTTP_201_CREATED)


class StatsViewSet(viewsets.ViewSet):
    permission_classes = [HasRole('hr'), FeatureEnabled('helpdesk')]

    @extend_schema(responses=dict)
    def list(self, request):
        return Response(services.stats(request.user))
