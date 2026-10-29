"""
Request middleware for the JSON API and the React app it serves:

- SecurityHeadersMiddleware: the Content-Security-Policy the TS API sent (helmet in apps/api/src/app.ts) for the web
  app and the API's own HTML (payslips, letters, salary sheets), plus nosniff and the referrer and opener policies.
- StaticFilesMiddleware: WhiteNoise. /static/ (collectstatic) and the React build at the site root (WHITENOISE_ROOT:
  /assets/* content-hashed and cached for a year; index.html, sw.js and the manifest never cached).
- ApiCorsMiddleware: CORS for /api/ (CORS_ORIGINS), answered before anything else so a preflight never reaches a view.
- AuthMiddleware: loads the signed-in user from the `hajiri_session` cookie or a Bearer token (the TS sessions).
- SameOriginMiddleware: the TS sameOriginGuard. A state-changing request carried by the session cookie must come from
  this site's origin or a CORS origin; Bearer requests and signed-out requests (sign-in) pass. It replaces Django's
  CSRF token check: the React app sends the cookie with fetch() and no token, exactly as it did against the TS API.
- HttpErrorMiddleware: HttpError (and the database errors the TS error handler mapped) from a non-DRF view as the TS
  JSON error body. DRF views use hajiri.core.api.exception_handler, which gives the same shape.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse

from hajiri.core.auth import SESSION_COOKIE, resolve_session
from hajiri.core.errors import HttpError, ValidationError, error_body, from_db_error

log = logging.getLogger(__name__)

CSRF_REFUSED = 'cross-site request refused'

# apps/api/src/app.ts helmet({contentSecurityPolicy: {directives}}) plus the helmet default it kept
# (script-src-attr 'none'). helmet's upgrade-insecure-requests is only sent on HTTPS installs: most installs run on
# a LAN over plain HTTP, where it would send the browser to an https:// address nothing answers.
_CSP = [
    "default-src 'self'",
    "base-uri 'self'",
    "connect-src 'self'",
    "font-src 'self' data:",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self' data: blob:",
    "object-src 'none'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "style-src 'self' 'unsafe-inline'",
]
CSP = '; '.join(_CSP)


def content_security_policy() -> str:
    https = getattr(settings, 'SECURE_SSL_REDIRECT', False) or getattr(settings, 'SESSION_COOKIE_SECURE', False)
    return CSP + ('; upgrade-insecure-requests' if https else '')


def _bearer(request) -> str | None:
    header = request.headers.get('Authorization', '')
    return header[7:] if header.startswith('Bearer ') else None


class AuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        bearer = _bearer(request)
        token = bearer or request.COOKIES.get(SESSION_COOKIE)
        request.hajiri_user = None
        request.session_token = None
        request.auth_method = None
        if token:
            user = resolve_session(token)
            if user:
                request.hajiri_user, request.session_token = user, token
                request.auth_method = 'bearer' if bearer else 'cookie'
        return self.get_response(request)


def error_message(exc: HttpError) -> str:
    """The text a person sees: validation issues joined with their paths (frontend/src/api/client.js normalizeError)."""
    if isinstance(exc, ValidationError) and exc.issues:
        return '; '.join(f"{i.get('path')}: {i.get('message')}" if i.get('path') else str(i.get('message'))
                         for i in exc.issues)
    return exc.message


def error_response(request, status: int, message: str, details=None, exc: HttpError | None = None) -> HttpResponse:
    """The TS error body: {"error": message} (+ details), validation {"error": "validation", "issues": [...]}."""
    if exc is None:
        exc = HttpError(message, details)
        exc.status = status
    return JsonResponse(error_body(exc), status=status, json_dumps_params={'default': str})


class SameOriginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, 'auth_method', None) == 'cookie' and request.hajiri_user is not None:
            from hajiri.core.api import same_origin
            from hajiri.core.cors import cors_origins
            if not same_origin(request, tuple(cors_origins())):
                return error_response(request, 403, CSRF_REFUSED)
        # Django's CsrfViewMiddleware is not installed; code that runs its check directly (HajiriAuthentication)
        # leaves cookie requests to the origin check above.
        request._dont_enforce_csrf_checks = True
        return self.get_response(request)


class ApiCorsMiddleware:
    """CORS for the JSON API under /api/: origins listed in CORS_ORIGINS may call it with credentials (the TS corsOrigins
    option). A preflight from an allowed origin is answered here with 204."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path_info.startswith('/api/'):
            return self.get_response(request)
        from hajiri.core.cors import cors_headers, cors_origins
        allowed = cors_origins()
        origin = request.headers.get('Origin')
        if not (allowed and origin and origin in allowed):
            return self.get_response(request)
        response = HttpResponse(status=204) if request.method == 'OPTIONS' else self.get_response(request)
        for k, v in cors_headers(origin).items():
            response[k] = v
        return response


class HttpErrorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exc):
        if isinstance(exc, DatabaseError):
            mapped = from_db_error(exc)
            if mapped is None:
                return None
            exc = mapped
        if not isinstance(exc, HttpError):
            return None
        return error_response(request, exc.status, exc.message, exc.details, exc=exc)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault('Content-Security-Policy', content_security_policy())
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        # JSON answers carry pay, attendance and personal data: never kept in a browser's disk cache (a shared office
        # computer) or a proxy. Endpoints that choose their own caching (photos, public pages) keep it.
        if request.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'private, no-store')
        return response


def csrf_failure(request, reason=''):
    """CSRF_FAILURE_VIEW, for any code that still runs Django's CSRF check (TS: 'cross-site request refused')."""
    log.info('csrf refused: %s', reason)
    return error_response(request, 403, CSRF_REFUSED)


# ---------------------------------------------------------------- static files and the React build

_NO_CACHE = {'index.html', 'sw.js', 'manifest.webmanifest'}


def static_headers(headers, path, url):
    """WHITENOISE_ADD_HEADERS_FUNCTION: the app shell files are revalidated on every load; the service worker may
    control the whole site."""
    name = url.rsplit('/', 1)[-1]
    if url.count('/') == 1 and name in _NO_CACHE:
        headers['Cache-Control'] = 'no-cache'
    if url == '/sw.js':
        headers['Service-Worker-Allowed'] = '/'


def _static_middleware():
    from whitenoise.middleware import WhiteNoiseMiddleware

    class StaticFilesMiddleware(WhiteNoiseMiddleware):
        """WhiteNoise, quiet about a folder that does not exist yet (STATIC_ROOT before collectstatic, the React
        build before the first `vite build` of a fresh clone)."""

        def add_files(self, root, prefix=None):
            from pathlib import Path
            if Path(root).is_dir():
                super().add_files(root, prefix)

        def __call__(self, request):
            # A file listed at startup that a later `vite build` deleted (old hashed assets): forget it and let the
            # URLconf answer (hajiri.web.views.build_asset: the file from the folder, or a 404), instead of a 500.
            static_file = None if self.autorefresh else self.files.get(request.path_info)
            if static_file is not None:
                try:
                    return self.serve(static_file, request)
                except FileNotFoundError:
                    self.files.pop(request.path_info, None)
                    return self.get_response(request)
            return super().__call__(request)

    return StaticFilesMiddleware


StaticFilesMiddleware = _static_middleware()
