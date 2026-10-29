"""Site-wide error views, in the TS API's JSON shape (apps/api/src/app.ts): an address nothing serves is
{"error": "not found"} (page addresses never get here: the React app's index.html answers them), a crash is
{"error": "internal error"}."""
from django.http import JsonResponse


def not_found(request, exception=None):
    return JsonResponse({'error': 'not found'}, status=404)


def bad_request(request, exception=None):
    return JsonResponse({'error': 'bad request'}, status=400)


def forbidden(request, exception=None):
    return JsonResponse({'error': 'forbidden'}, status=403)


def server_error(request):
    return JsonResponse({'error': 'internal error'}, status=500)
