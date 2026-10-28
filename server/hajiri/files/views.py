"""Upload endpoint for forms (JSON data URL or multipart) and the view that serves an upload back before it is
attached. Both are DRF views answering JSON errors in the TS shape; the upload widget reads the returned file id."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response


from . import service
from .parsers import json_parser
from .serializers import StoredFileOut, UploadIn
from .store import stored_file_response

# A base64 data URL of a 4 MB file, with room for the JSON around it (express.json({ limit: '6mb' }) on /files).
JSON_UPLOAD_MAX = 6 * 1024 * 1024


@extend_schema(request=UploadIn, responses={201: StoredFileOut}, summary='Upload a file to attach to a form')
@api_view(['POST'])
@parser_classes([json_parser(JSON_UPLOAD_MAX), MultiPartParser])
def upload(request):
    user = request.user
    if request.content_type and request.content_type.startswith('multipart/'):
        f = service.upload_file(user, request.POST.get('purpose', ''), request.FILES.get('file'), request.POST.get('filename'))
    else:
        f = service.upload(user, request.data)
    return Response({'file': f}, status=status.HTTP_201_CREATED)


@extend_schema(responses={(200, 'application/octet-stream'): OpenApiTypes.BINARY}, summary='Open an upload not attached yet')
@api_view(['GET'])
def serve(request, file_id):
    f = service.open_unattached(request.user, str(file_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')
