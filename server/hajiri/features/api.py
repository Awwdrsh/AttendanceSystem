"""Feature switches for JSON clients (the TS /api/v1/settings/features routes): every signed-in user reads them (they
drive the menu); HR or admin changes them for the active company."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication

from . import service


@extend_schema(methods=['PUT'], request=service.FeaturesIn, responses=OpenApiTypes.OBJECT, summary='Switch modules on or off (HR)')
@extend_schema(methods=['GET'], responses=OpenApiTypes.OBJECT, summary='Which modules are switched on')
@api_view(['GET', 'PUT'])
@authentication_classes([SameOriginAuthentication])
def features(request):
    if request.method == 'PUT':
        return Response(service.set_features(request.user, request.data))
    return Response(service.get_features(request.user))
