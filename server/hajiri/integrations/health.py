"""GET /health for load balancers and the start scripts: {'ok': true, 'db': 'up'}, or 503 when the database is down."""
from django.db import DatabaseError, connection
from django.http import JsonResponse

# JSON.stringify output, as the TS API sent it.
COMPACT = {'separators': (',', ':')}


def health(request):
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')  # Raw SQL: a liveness probe of the database itself, no table involved
        return JsonResponse({'ok': True, 'db': 'up'}, json_dumps_params=COMPACT)
    except DatabaseError:
        return JsonResponse({'ok': False, 'db': 'down'}, status=503, json_dumps_params=COMPACT)
