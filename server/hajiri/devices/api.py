"""
JSON endpoints of the devices and biometrics modules under /api/v1/devices and /api/v1/biometrics (the TS paths of
devices/routes.ts and biometrics/routes.ts): DRF views with the project defaults (HajiriAuthentication, IsSignedIn,
the TS error shape, JsonRenderer). Roles, company scoping and validation live in services.py and biometrics.py.

The TS API returned PostgreSQL bigint values (count(*), device_command.id) as strings; the views keep that shape.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication

from . import biometrics as bio
from . import services
from .serializers import ActivateIn, CommandIn, ConsentIn, MapIn, RetentionRunIn

# The React app sends the session cookie with fetch() and no CSRF token: the Origin is checked instead (the TS
# sameOriginGuard). Bearer requests pass unchanged.
AUTH = [SameOriginAuthentication]


def _text(row: dict, *keys: str) -> dict:
    return {**row, **{k: None if row.get(k) is None else str(row[k]) for k in keys}}


def _body(request) -> dict:
    return request.data if request.data is not None else {}


# ---------------------------------------------------------------- devices (HR and admin)

@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def devices(request):
    rows = services.list_devices(request.user)
    return Response({'devices': [_text(d, 'mapped_users', 'queued_commands') for d in rows]})


@extend_schema(request=ActivateIn, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def activate(request, id):
    return Response(services.activate(request.user, id, _body(request)))


@extend_schema(request=None, responses={201: dict})
@api_view(['POST'])
@authentication_classes(AUTH)
def key(request, id):
    return Response(services.issue_key(request.user, id), status=201)


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def disable(request, id):
    return Response(services.disable(request.user, id))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def mappings(request, id):
    return Response({'mappings': services.mappings(request.user, id)})


@extend_schema(request=MapIn, responses={201: dict})
@api_view(['POST'])
@authentication_classes(AUTH)
def map_pin(request, id):
    return Response(services.map_pin(request.user, id, _body(request)), status=201)


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def unmapped_punches(request):
    return Response({'punches': [_text(p, 'total_for_pin') for p in services.unmapped_punches(request.user)]})


@extend_schema(request=CommandIn, responses={201: dict})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
def commands(request, id):
    if request.method == 'POST':
        return Response(services.queue_command(request.user, id, _body(request)), status=201)
    return Response({'commands': [_text(c, 'id') for c in services.commands(request.user, id)]})


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def sync_users(request, id):
    return Response(services.sync_users(request.user, id))


# ---------------------------------------------------------------- biometrics

@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def transparency(request):
    return Response(bio.transparency())


@extend_schema(request=ConsentIn, responses={201: dict})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
def consent(request):
    if request.method == 'POST':
        return Response(bio.grant_consent(request.user, _body(request)), status=201)
    return Response({'consents': bio.consents(request.user, request.query_params.dict())})


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def consent_revoke(request, id):
    return Response(bio.revoke_consent(request.user, id))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def templates(request):
    return Response({'templates': bio.templates(request.user, request.query_params.dict())})


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def templates_unmapped(request):
    return Response({'templates': bio.unmapped_templates(request.user)})


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def template_shred(request, id):
    return Response(bio.shred_template(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def employee_shred_all(request, id):
    return Response(bio.shred_all(request.user, id))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def dsar(request):
    return Response(bio.dsar(request.user, request.query_params.dict()))


@extend_schema(request=RetentionRunIn, responses=dict)
@api_view(['POST'])
@authentication_classes(AUTH)
def retention_run(request):
    return Response(bio.run_retention(request.user, _body(request)))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def stats(request):
    return Response({'byModality': bio.stats(request.user)})
