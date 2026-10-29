"""JSON endpoints of the punch module under /api/v1/punch (the TS paths): DRF views with the project defaults
(IsSignedIn, the TS error shape) and SameOriginAuthentication: the React app's session cookie with an Origin check
(no CSRF token, as against the TS API) or a Bearer token for the phone app. The pages call the same service
functions."""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, authentication_classes, parser_classes
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication
from hajiri.files.parsers import json_parser
from hajiri.web.views import client_ip

from . import services
from .serializers import ManualIn, PunchIn, RangeIn

# The React app sends the session cookie with fetch() and an Origin check, never a CSRF token (as against the TS API).
AUTH = [SameOriginAuthentication]
BODY_MAX = 2_000_000          # a selfie travels as base64 inside the JSON body


@extend_schema(request=PunchIn, responses={201: dict, 200: dict})
@api_view(['POST'])
@authentication_classes(AUTH)
@parser_classes([json_parser(BODY_MAX)])
def punch(request):
    status, body = services.punch(request.user, request.data, device_token=request.headers.get('X-Device-Token'),
                                  ip=client_ip(request))
    return Response(body, status=status)


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def today(request):
    return Response(services.today(request.user))


@extend_schema(parameters=[RangeIn], responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def remote(request):
    return Response(services.remote(request.user, request.query_params.dict()))


def photo_response(p: dict) -> HttpResponse:
    res = HttpResponse(p['data'], content_type=p['mime'])
    res['Cache-Control'] = 'private, max-age=300'
    res['X-Content-Type-Options'] = 'nosniff'
    res['Content-Disposition'] = 'inline'
    return res


@extend_schema(responses={(200, 'image/jpeg'): bytes})
@api_view(['GET'])
@authentication_classes(AUTH)
def photo(request, id):
    return photo_response(services.photo(request.user, id))


@extend_schema(request=ManualIn, responses={201: dict, 200: dict})
@api_view(['POST'])
@authentication_classes(AUTH)
def manual(request):
    status, body = services.manual(request.user, request.data)
    return Response(body, status=status)
