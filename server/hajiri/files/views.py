"""Upload endpoint for forms (JSON data URL or multipart) and the view that serves an upload back before it is
attached. Both answer JSON errors; the upload widget reads the returned file id."""
from hajiri.core.http import api_view, json_response, read_json

from . import service
from .store import stored_file_response

# A base64 data URL of a 4 MB file, with room for the JSON around it (express.json({ limit: '6mb' }) on /files).
JSON_UPLOAD_MAX = 6 * 1024 * 1024


@api_view(methods=('POST',))
def upload(request):
    user = request.hajiri_user
    if request.content_type and request.content_type.startswith('multipart/'):
        f = service.upload_file(user, request.POST.get('purpose', ''), request.FILES.get('file'), request.POST.get('filename'))
    else:
        f = service.upload(user, read_json(request, max_bytes=JSON_UPLOAD_MAX))
    return json_response({'file': f}, status=201)


@api_view(methods=('GET',))
def serve(request, file_id):
    f = service.open_unattached(request.hajiri_user, str(file_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')
