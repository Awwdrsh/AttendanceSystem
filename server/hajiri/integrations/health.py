"""GET /health for load balancers and the start scripts: {'ok': true, 'db': 'up'}, or 503 when the database is down."""
from django.db import DatabaseError, connection
from django.http import JsonResponse


def health(request):
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')  # Raw SQL: a liveness probe of the database itself, no table involved
        return JsonResponse({'ok': True, 'db': 'up'})
    except DatabaseError:
        return JsonResponse({'ok': False, 'db': 'down'}, status=503)
