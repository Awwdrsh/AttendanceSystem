"""JSON endpoints of the dashboard module under /api/v1/dashboard (the TS paths of dashboard/routes.ts): the executive
dashboard (HR, managers, executives) and the employee home. Rules live in services.py."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication

from . import services

# The React app sends the session cookie with fetch() and no CSRF token: the Origin is checked instead (the TS
# sameOriginGuard). Bearer requests pass unchanged.
AUTH = [SameOriginAuthentication]


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def executive(request):
    return Response(services.executive(request.user))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
def me(request):
    return Response(services.me(request.user))
