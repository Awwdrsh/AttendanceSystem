"""
JSON endpoints for kiosk tablets under /api/v1/kiosk (the TS paths), as DRF views. The tablet has no session: after
pairing it sends its kiosk key in the X-Kiosk-Key header (KioskKeyAuthentication), so these views use neither the
session cookie nor CSRF. request.user is the paired kiosk (its row as a dict, company included).
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from hajiri.files.parsers import json_parser
from hajiri.web.views import client_ip

from . import services
from .serializers import KioskPunchIn, PairIn, PinIdentifyIn

BODY_MAX = 2_000_000          # the selfie travels as base64 inside the JSON body


class KioskKeyAuthentication(BaseAuthentication):
    """X-Kiosk-Key: a paired, active kiosk of a company with field work on (401 / 404 'feature off' otherwise)."""

    def authenticate(self, request):
        kiosk = services.authenticate(request.headers.get(services.KIOSK_HEADER))
        return kiosk, None

    def authenticate_header(self, request):
        return services.KIOSK_HEADER


def kiosk_view(methods):
    """A tablet endpoint: authenticated by its kiosk key alone."""
    def deco(fn):
        return api_view(methods)(authentication_classes([KioskKeyAuthentication])(permission_classes([AllowAny])(fn)))
    return deco


@extend_schema(request=PairIn, responses=dict)
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def pair(request):
    return Response(services.pair(request.data, client_ip(request)))


@extend_schema(responses=dict)
@kiosk_view(['GET'])
def session(request):
    return Response(services.session(request.user))


@extend_schema(request=PinIdentifyIn, responses=dict)
@kiosk_view(['POST'])
def identify(request):
    return Response(services.identify(request.user, request.data))


@extend_schema(request=KioskPunchIn, responses={201: dict, 200: dict})
@kiosk_view(['POST'])
@parser_classes([json_parser(BODY_MAX)])
def punch(request):
    status, body = services.punch(request.user, request.data, client_ip(request))
    return Response(body, status=status)


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class KioskKeyScheme(OpenApiAuthenticationExtension):
        target_class = 'hajiri.kiosk.api.KioskKeyAuthentication'
        name = 'kioskKey'

        def get_security_definition(self, auto_schema):
            return {'type': 'apiKey', 'in': 'header', 'name': services.KIOSK_HEADER}
except ImportError:  # pragma: no cover
    pass
