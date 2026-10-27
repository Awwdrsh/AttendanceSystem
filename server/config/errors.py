"""Site-wide error views. Paths under /api/ answer JSON {'error': 'not found'}, as app.ts did for unknown API routes;
everything else gets the friendly 404 page."""
from django.http import JsonResponse
from django.views.defaults import page_not_found


def not_found(request, exception=None):
    if request.path_info.startswith('/api/'):
        return JsonResponse({'error': 'not found'}, status=404)
    return page_not_found(request, exception)
