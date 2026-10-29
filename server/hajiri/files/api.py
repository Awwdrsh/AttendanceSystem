"""The TS /api/v1/files routes for the React app and Bearer-token clients: POST uploads one encrypted file (a JSON data
URL, or multipart) and returns its id; GET serves it back to the uploader (or HR) until a module attaches it. Same
service as the page endpoints in views.py; cookie requests are checked by Origin like the TS API
(SameOriginAuthentication)."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from hajiri.core.api import SameOriginAuthentication
from hajiri.core.validation import parse_uuid

from . import service
from .parsers import json_parser
from .serializers import StoredFileOut, UploadIn
from .store import stored_file_response
from .views import JSON_UPLOAD_MAX


@extend_schema(operation_id='files_upload', request=UploadIn, responses={201: StoredFileOut}, summary='Upload a file to attach to a form')
@api_view(['POST'])
@authentication_classes([SameOriginAuthentication])
@parser_classes([json_parser(JSON_UPLOAD_MAX), MultiPartParser])
def upload(request):
    if request.content_type and request.content_type.startswith('multipart/'):
        f = service.upload_file(request.user, request.POST.get('purpose', ''), request.FILES.get('file'),
                                request.POST.get('filename'))
    else:
        f = service.upload(request.user, request.data)
    return Response({'file': f}, status=status.HTTP_201_CREATED)


@extend_schema(operation_id='files_get', responses={(200, 'application/octet-stream'): OpenApiTypes.BINARY},
               summary='Open an upload not attached yet')
@api_view(['GET'])
@authentication_classes([SameOriginAuthentication])
def serve(request, file_id):
    f = service.open_unattached(request.user, parse_uuid(file_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')
