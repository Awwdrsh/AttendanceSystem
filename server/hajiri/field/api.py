"""JSON endpoints of the field module under /api/v1/field (the TS paths): DRF views with the project defaults
(HajiriAuthentication, IsSignedIn, the TS error shape). Breaks, visits, kiosks and the kiosk PIN/badge also need the
company's field feature (FeatureEnabled('field'): 404 'feature off', the TS featureGate). Rules live in services.py."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response

from hajiri.core.api import FeatureEnabled, IsSignedIn
from hajiri.files.parsers import json_parser
from hajiri.punch.api import photo_response
from hajiri.web.csv import csv_response
from hajiri.web.views import client_ip

from . import serializers as s
from . import services

GATED = [IsSignedIn, FeatureEnabled('field')]


class _JsonOnly(DefaultContentNegotiation):
    """The reports take ?format=json|csv as their own parameter (the TS query), not DRF's renderer override."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


def own_format_param(view):
    view.content_negotiation_class = _JsonOnly
    return view


def _report(out):
    if isinstance(out, dict) and 'filename' in out and 'columns' in out:
        return csv_response(f"{out['filename']}.csv", out['columns'], out['rows'])
    return Response(out)


@extend_schema(request=s.FieldPolicyIn, responses=dict)
@api_view(['GET', 'PUT'])
def policy(request):
    if request.method == 'PUT':
        return Response(services.put_policy(request.user, request.data))
    return Response(services.get_policy(request.user))


@extend_schema(responses=dict)
@api_view(['GET'])
def me(request):
    return Response(services.me(request.user))


@extend_schema(request=s.BreakStartIn, responses={201: dict})
@api_view(['POST'])
@permission_classes(GATED)
def breaks_start(request):
    return Response(services.start_break(request.user, request.data), status=201)


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(GATED)
def breaks_end(request):
    return Response(services.end_break(request.user))


@extend_schema(parameters=[s.BreaksQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(GATED)
@own_format_param
def breaks(request):
    return _report(services.list_breaks(request.user, request.query_params.dict()))


@extend_schema(request=s.VisitStartIn, responses={201: dict})
@api_view(['POST'])
@permission_classes(GATED)
@parser_classes([json_parser(2_000_000)])
def visits_start(request):
    return Response(services.start_visit(request.user, request.data, client_ip(request)), status=201)


@extend_schema(request=s.VisitEndIn, responses=dict)
@api_view(['POST'])
@permission_classes(GATED)
def visit_end(request, id):
    return Response(services.end_visit(request.user, id, request.data))


@extend_schema(parameters=[s.ReportQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(GATED)
@own_format_param
def visits(request):
    return _report(services.list_visits(request.user, request.query_params.dict()))


@extend_schema(responses={(200, 'image/jpeg'): bytes})
@api_view(['GET'])
@permission_classes(GATED)
def visit_photo(request, id):
    return photo_response(services.visit_photo(request.user, id))


@extend_schema(request=s.FieldLocationIn, responses=dict)
@api_view(['GET', 'POST'])
def locations(request):
    if request.method == 'POST':
        return Response(services.create_location(request.user, request.data), status=201)
    return Response(services.locations(request.user))


@extend_schema(request=s.FieldLocationIn, responses=dict)
@api_view(['PUT', 'DELETE'])
def location(request, id):
    if request.method == 'DELETE':
        return Response(services.delete_location(request.user, id))
    return Response(services.update_location(request.user, id, request.data))


@extend_schema(responses=dict)
@api_view(['GET'])
def staff(request):
    return Response(services.staff(request.user))


@extend_schema(request=s.StaffIn, responses=dict)
@api_view(['PUT'])
def staff_update(request, employee_id):
    return Response(services.update_staff(request.user, employee_id, request.data))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
def staff_kiosk_reset(request, employee_id):
    return Response(services.reset_kiosk_pin(request.user, employee_id))


@extend_schema(request=s.KioskIn, responses=dict)
@api_view(['GET', 'POST'])
@permission_classes(GATED)
def kiosks(request):
    if request.method == 'POST':
        return Response(services.create_kiosk(request.user, request.data), status=201)
    return Response(services.kiosks(request.user))


@extend_schema(request=s.KioskIn, responses=dict)
@api_view(['PUT'])
@permission_classes(GATED)
def kiosk(request, id):
    return Response(services.update_kiosk(request.user, id, request.data))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(GATED)
def kiosk_pairing_code(request, id):
    return Response(services.kiosk_pairing_code(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(GATED)
def kiosk_revoke(request, id):
    return Response(services.revoke_kiosk(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(GATED)
def kiosk_unlock(request, id):
    return Response(services.unlock_kiosk(request.user, id))


@extend_schema(request=s.PinIn, responses=dict)
@api_view(['PUT'])
@permission_classes(GATED)
def kiosk_pin(request):
    return Response(services.set_kiosk_pin(request.user, request.data))


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(GATED)
def kiosk_qr(request):
    return Response(services.kiosk_qr(request.user), headers={'Cache-Control': 'no-store'})


@extend_schema(parameters=[s.RosterQueryIn], responses=dict)
@api_view(['GET'])
def my_roster(request):
    return Response(services.my_roster(request.user, request.query_params.dict()))


@extend_schema(parameters=[s.DateQueryIn], responses=dict)
@api_view(['GET'])
def swap_colleagues(request):
    return Response(services.swap_colleagues(request.user, request.query_params.dict()))


@extend_schema(request=s.SwapIn, parameters=[s.SwapsQueryIn], responses=dict)
@api_view(['GET', 'POST'])
def swaps(request):
    if request.method == 'POST':
        return Response(services.request_swap(request.user, request.data), status=201)
    return Response(services.swaps(request.user, request.query_params.dict()))


@extend_schema(request=s.SwapNoteIn, responses=dict)
@api_view(['POST'])
def swap_accept(request, id):
    return Response(services.accept_swap(request.user, id, request.data))


@extend_schema(request=s.SwapNoteIn, responses=dict)
@api_view(['POST'])
def swap_decline(request, id):
    return Response(services.decline_swap(request.user, id, request.data))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
def swap_cancel(request, id):
    return Response(services.cancel_swap(request.user, id))
