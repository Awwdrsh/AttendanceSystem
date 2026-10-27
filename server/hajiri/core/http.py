"""
JSON helpers for the few endpoints non-browser clients use (Bearer-token integrations, kiosk tablets, devices). Pages
and HTMX partials do not use these. `api_view` gives an endpoint the TS API's behaviour: JSON errors in the same shape,
database errors mapped to 400/409, a same-origin check for cookie sessions (CSRF) and the sign-in gates.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
import os
import uuid
from functools import wraps
from urllib.parse import urlparse

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .auth import TWO_FACTOR_SETUP_MESSAGE
from .errors import BadRequestError, ForbiddenError, HttpError, UnauthorizedError, error_body, from_db_error


class ApiEncoder(json.JSONEncoder):
    """Dates as ISO strings, timestamps as ISO 8601 UTC with milliseconds (like JSON.stringify), numerics as strings."""

    def default(self, o):
        if isinstance(o, _dt.datetime):
            if o.tzinfo:
                o = o.astimezone(_dt.timezone.utc)
                return o.strftime('%Y-%m-%dT%H:%M:%S.') + f'{o.microsecond // 1000:03d}Z'
            return o.isoformat()
        if isinstance(o, _dt.date):
            return o.isoformat()
        if isinstance(o, _dt.time):
            return o.isoformat()
        if isinstance(o, decimal.Decimal):
            return str(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, (bytes, memoryview)):
            return None
        if isinstance(o, (set, frozenset, tuple)):
            return list(o)
        if hasattr(o, 'to_dict'):
            return o.to_dict()
        return super().default(o)


def json_response(data, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status, encoder=ApiEncoder, safe=False, json_dumps_params={'ensure_ascii': False})


def error_response(exc: HttpError) -> JsonResponse:
    return json_response(error_body(exc), status=exc.status)


def read_json(request, max_bytes: int | None = None) -> dict:
    """The JSON body (an empty body is {}). Invalid JSON is a 400, an oversized body a 413. `max_bytes` raises the
    limit above DATA_UPLOAD_MAX_MEMORY_SIZE for one endpoint (the TS API allowed 6 MB on /files for base64 uploads)."""
    if max_bytes is not None and not hasattr(request, '_body'):
        try:
            length = int(request.META.get('CONTENT_LENGTH') or 0)
        except ValueError:
            length = 0
        if length > max_bytes:
            raise _TooLarge()
        raw = request.read(max_bytes + 1) if length else b''
        if len(raw) > max_bytes:
            raise _TooLarge()
        request._body = raw
    else:
        try:
            raw = request.body
        except RequestDataTooBig:
            raise _TooLarge()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequestError('invalid JSON body')


class _TooLarge(HttpError):
    status, default_message = 413, 'request body too large'


def same_origin_ok(request, allowed_origins: tuple[str, ...] = ()) -> bool:
    """A state-changing request carried by the session cookie must come from this site's own origin."""
    if request.method in ('GET', 'HEAD', 'OPTIONS') or getattr(request, 'auth_method', None) != 'cookie':
        return True
    origin = request.headers.get('Origin')
    if not origin or origin == 'null':
        ref = request.headers.get('Referer')
        if not ref:
            return origin != 'null'
        try:
            u = urlparse(ref)
            origin = f'{u.scheme}://{u.netloc}'
        except ValueError:
            return False
    own = f'{request.scheme}://{request.get_host()}'
    return origin == own or origin in allowed_origins


def cors_origins() -> tuple[str, ...]:
    """CORS_ORIGINS (comma-separated, from settings or the environment): other origins allowed to call the JSON API
    with credentials, as the TS API's corsOrigins option."""
    from django.conf import settings
    raw = getattr(settings, 'CORS_ORIGINS', None)
    if raw is None:
        raw = os.environ.get('CORS_ORIGINS', '')
    items = raw.split(',') if isinstance(raw, str) else list(raw)
    return tuple(o.strip() for o in items if o and o.strip())


def _cors(request, res: HttpResponse, allowed: tuple[str, ...]) -> HttpResponse:
    origin = request.headers.get('Origin')
    if origin and origin in allowed:
        res['Access-Control-Allow-Origin'] = origin
        res['Vary'] = 'Origin'
        res['Access-Control-Allow-Credentials'] = 'true'
        res['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        res['Access-Control-Allow-Methods'] = 'GET,POST,PUT,PATCH,DELETE,OPTIONS'
    return res


def api_view(*, methods: tuple[str, ...] = ('GET',), auth: bool = True, gates: bool = True):
    """Decorator for a JSON endpoint. `auth`: a session or Bearer token is required. `gates`: a temporary password or
    a missing required two-step sign-in blocks the call (as requirePasswordChanged did for everything but /auth)."""
    def deco(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            allowed = cors_origins()
            if allowed and request.method == 'OPTIONS':
                return _cors(request, HttpResponse(status=204), allowed)
            return _cors(request, _run(request, allowed, *args, **kwargs), allowed)

        def _run(request, allowed, *args, **kwargs):
            try:
                if request.method not in methods:
                    return json_response({'error': 'not found'}, status=404)
                user = getattr(request, 'hajiri_user', None)
                if user and not same_origin_ok(request, allowed):
                    raise ForbiddenError('cross-site request refused')
                if auth:
                    if not user:
                        raise UnauthorizedError()
                    if gates and user.must_change_password:
                        raise ForbiddenError('password change required')
                    if gates and user.must_set_up_two_factor:
                        raise ForbiddenError(TWO_FACTOR_SETUP_MESSAGE)
                res = view(request, *args, **kwargs)
                if isinstance(res, HttpResponse):
                    return res
                return json_response(res)
            except HttpError as e:
                return error_response(e)
            except DatabaseError as e:
                mapped = from_db_error(e)
                if mapped is None:
                    raise
                return error_response(mapped)
        return wrapped
    return deco
