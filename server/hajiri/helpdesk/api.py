"""
Help desk JSON API: the TS routes under /api/v1/helpdesk (apps/api/src/modules/helpdesk/routes.ts), which the React
app calls with the cookie session. Same paths, methods, bodies, JSON shapes and status codes. DRF views with
hajiri.core.api's SameOriginAuthentication (cookie + Origin check, no CSRF token), IsSignedIn, the TS error shape and JsonRenderer. The
feature switch is checked before any role (featureGate was mounted ahead of the routes' requireRole). Rules and
validation stay in services.py.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import services
from .serializers import CategoryIn, MessageIn, PatchIn, PolicyIn, QueueQueryIn, RateIn, TicketIn

FEATURE = FeatureEnabled('helpdesk')
HR = HasRole('hr')
# The React app sends the session cookie with fetch() and no CSRF token (as against the TS API, whose
# sameOriginGuard checked the Origin): the default HajiriAuthentication would refuse its POST/PUT/PATCH.
AUTH = [SameOriginAuthentication]


class _View(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, FEATURE]


class _HrView(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, FEATURE, HR]


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- settings and categories

class SettingsView(_View):
    """GET /helpdesk/settings (everyone) -> {policy}; PUT (HR) -> {policy}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.get_settings(request.user))

    @extend_schema(request=PolicyIn, responses=dict)
    def put(self, request):
        return Response(services.put_settings(request.user, request.data))


class CategoriesView(_View):
    """GET /helpdesk/categories -> {categories}; POST (HR) -> 201 {id}."""

    @extend_schema(operation_id='api_v1_helpdesk_categories_list', responses=dict)
    def get(self, request):
        return Response({'categories': services.list_categories(request.user)})

    @extend_schema(request=CategoryIn, responses={201: dict})
    def post(self, request):
        return _created(services.create_category(request.user, request.data))


class CategoryView(_View):
    """PUT /helpdesk/categories/:id (HR) -> {ok}."""

    @extend_schema(request=CategoryIn, responses=dict)
    def put(self, request, category_id):
        return Response(services.update_category(request.user, category_id, request.data))


class AgentsView(_HrView):
    """GET /helpdesk/agents (HR) -> {agents}: people HR can assign tickets to."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'agents': services.list_agents(request.user)})


class StatsView(_HrView):
    """GET /helpdesk/stats (HR) -> {stats, byCategory}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.stats(request.user))


# ---------------------------------------------------------------- tickets

class TicketsView(_View):
    """GET /helpdesk/tickets -> {tickets} (the queue, at most 500); POST -> 201 {id, number, slaDueAt}."""

    @extend_schema(operation_id='api_v1_helpdesk_tickets_list', parameters=[QueueQueryIn], responses=dict)
    def get(self, request):
        return Response({'tickets': services.queue(request.user, request.query_params.dict())})

    @extend_schema(request=TicketIn, responses={201: dict})
    def post(self, request):
        return _created(services.create_ticket(request.user, request.data))


class MyTicketsView(_View):
    """GET /helpdesk/tickets/mine -> {tickets}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'tickets': services.my_tickets(request.user)})


class TicketView(_View):
    """GET /helpdesk/tickets/:id -> {ticket, role, messages, files, canReopen}; PATCH (the team) -> {ok}."""

    @extend_schema(responses=dict)
    def get(self, request, ticket_id):
        return Response(services.ticket_detail(request.user, ticket_id))

    @extend_schema(request=PatchIn, responses=dict)
    def patch(self, request, ticket_id):
        return Response(services.update_ticket(request.user, ticket_id, request.data))


class MessagesView(_View):
    @extend_schema(request=MessageIn, responses={201: dict})
    def post(self, request, ticket_id):
        return _created(services.add_message(request.user, ticket_id, request.data))


class ReopenView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, ticket_id):
        return Response(services.reopen_ticket(request.user, ticket_id))


class CloseView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, ticket_id):
        return Response(services.close_ticket(request.user, ticket_id))


class RateView(_View):
    @extend_schema(request=RateIn, responses=dict)
    def post(self, request, ticket_id):
        return Response(services.rate_ticket(request.user, ticket_id, request.data))


class _AnyAccept(DefaultContentNegotiation):
    """The file answer is not rendered, so whatever the browser accepts must not end in 406."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class TicketFileView(_View):
    """GET /helpdesk/tickets/:id/files/:fileId[?download=1]: an attachment the user may see."""
    content_negotiation_class = _AnyAccept

    @extend_schema(parameters=[OpenApiParameter('download', str, required=False)],
                   responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, ticket_id, file_id):
        f = services.ticket_file(request.user, ticket_id, file_id)
        return stored_file_response(f, download=request.query_params.get('download') == '1')
