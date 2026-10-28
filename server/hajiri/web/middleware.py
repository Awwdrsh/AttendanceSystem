"""
Request middleware:

- BearerCsrfMiddleware: requests that carry `Authorization: Bearer` skip Django's CSRF check (browsers never send that
  header by themselves, so it cannot be forged cross-site; the TS sameOriginGuard skipped them too).
- AuthMiddleware: loads the signed-in user from the `hajiri_session` cookie or a Bearer token (the same sessions as
  the TS API).
- HttpErrorMiddleware: turns HttpError (and the database errors the TS error handler mapped: unique, overlap, foreign
  key, check, bad format, RAISE EXCEPTION) into JSON for API clients in the TS shape, an HTMX toast or redirect, or a
  friendly error page. A signed-in browser asking for an address no app serves goes home, like the App.tsx catch-all.
- FlashMiddleware: reads the one-time toast cookie set by hajiri.web.htmx.flash and clears it once a page shows it.
- SecurityHeadersMiddleware: the same strict Content-Security-Policy as the TS API (helmet), plus nosniff.
"""
from __future__ import annotations

import logging
from urllib.parse import quote, urlparse

from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import Resolver404, get_resolver

from hajiri.core.auth import SESSION_COOKIE, TWO_FACTOR_SETUP_MESSAGE, resolve_session
from hajiri.core.errors import HttpError, ValidationError, error_body, from_db_error

from .htmx import FLASH_COOKIE, is_htmx, read_flash, trigger

log = logging.getLogger(__name__)
PASSWORD_CHANGE_MESSAGE = 'password change required'

# Alpine.js evaluates its attribute expressions with Function(), hence 'unsafe-eval'; there are no inline scripts.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "form-action 'self'",
    "base-uri 'self'",
])


def _bearer(request) -> str | None:
    header = request.headers.get('Authorization', '')
    return header[7:] if header.startswith('Bearer ') else None


class BearerCsrfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _bearer(request):
            request._dont_enforce_csrf_checks = True
        return self.get_response(request)


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


def wants_json(request) -> bool:
    return (getattr(request, 'auth_method', None) == 'bearer'
            or request.headers.get('Accept', '').startswith('application/json')
            or request.content_type == 'application/json')


def _current_path(request) -> str:
    """The page the user is on: for HTMX the browser URL (HX-Current-URL), else this request's path."""
    cur = request.headers.get('HX-Current-URL')
    if cur:
        u = urlparse(cur)
        return u.path + (f'?{u.query}' if u.query else '')
    return request.get_full_path()


def login_url(request) -> str:
    nxt = _current_path(request)
    return '/login' if nxt in ('/', '/login') or nxt.startswith('/login?') else f'/login?next={quote(nxt, safe="/")}'


ERROR_TITLES = {
    400: 'That did not work', 401: 'Please sign in', 403: 'Not allowed', 404: 'Not found', 409: 'Already changed',
    413: 'Too large', 429: 'Too many tries', 500: 'Something went wrong', 503: 'Unavailable',
}


ROLE_TITLE = 'Not available for your role'
ROLE_HINT = 'Ask HR if you believe you need access to this page.'


def error_message(exc: HttpError) -> str:
    """The text a person sees: validation issues joined with their paths (api/client.ts normalizeError)."""
    if isinstance(exc, ValidationError) and exc.issues:
        return '; '.join(f"{i.get('path')}: {i.get('message')}" if i.get('path') else str(i.get('message'))
                         for i in exc.issues)
    return exc.message


def _hub_redirect(request) -> str | None:
    """Hub.tsx: asking for a tab you may not see lands on the first tab of that hub you may see."""
    user = getattr(request, 'hajiri_user', None)
    if not user:
        return None
    from . import nav
    path = (request.path.rstrip('/') or '/')
    for it in nav.all_items():
        url = nav.url_of(it.url_name)
        if url and (url.rstrip('/') or '/') == path:
            landing = nav.hub_landing(user, it.hub)
            return landing if landing and (landing.rstrip('/') or '/') != path else None
    return None


def error_response(request, status: int, message: str, details=None, exc: HttpError | None = None) -> HttpResponse:
    """The same error, shaped for whoever asked: JSON client, HTMX swap or a full page."""
    if wants_json(request):
        if exc is None:
            exc = HttpError(message, details)
            exc.status = status
        return JsonResponse(error_body(exc), status=status, json_dumps_params={'default': str})
    if exc is not None:
        message = error_message(exc)
    redirect_to = None
    if status == 401:
        redirect_to = login_url(request)
    elif status == 403 and message == PASSWORD_CHANGE_MESSAGE:
        redirect_to = '/change-password'
    elif status == 403 and message == TWO_FACTOR_SETUP_MESSAGE:
        redirect_to = '/setup-two-step'
    if is_htmx(request):
        if redirect_to:
            r = HttpResponse(status=status)
            r.headers['HX-Redirect'] = redirect_to
            return r
        r = HttpResponse(message, status=status, content_type='text/plain; charset=utf-8')
        r.headers['HX-Reswap'] = 'none'
        title = 'Not saved' if request.method not in ('GET', 'HEAD') else 'Could not load'
        return trigger(r, 'hj:toast', {'message': message, 'tone': 'err', 'title': title})
    page_get = request.method in ('GET', 'HEAD')
    if page_get and status == 403 and message.startswith('requires role'):
        redirect_to = _hub_redirect(request)
        if not redirect_to:
            # RequireRole.tsx: the page is not for this role.
            return render(request, 'web/error.html', {'status': 403, 'message': ROLE_HINT, 'title': ROLE_TITLE,
                                                       'details': None}, status=403)
    if redirect_to and page_get:
        return HttpResponseRedirect(redirect_to)
    return render(request, 'web/error.html', {'status': status, 'message': message, 'details': details,
                                               'title': ERROR_TITLES.get(status, 'Something went wrong')}, status=status)


_NOT_PAGES = ('/_/', '/api/', '/static/', '/media/')


def _unknown_page(request) -> bool:
    """A browser asking for a page at an address no app serves. App.tsx's `path="*"` sits inside RequireRole: a
    signed-in user goes home, a signed-out visitor goes to the sign-in page first."""
    if request.method != 'GET' or is_htmx(request) or wants_json(request):
        return False
    path = request.path_info
    if path == '/' or path.startswith(_NOT_PAGES):
        return False
    resolver = get_resolver(getattr(request, 'urlconf', None))
    for p in (path, path if path.endswith('/') else path + '/'):
        try:
            resolver.resolve(p)
            return False
        except Resolver404:
            continue
    return True


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
        if _unknown_page(request):
            return HttpResponseRedirect('/' if getattr(request, 'hajiri_user', None) else login_url(request))
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


class FlashMiddleware:
    """Toasts carried across a redirect. A full HTML page (not HTMX, not a redirect) shows them and clears the cookie."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.hj_flash = read_flash(request) if FLASH_COOKIE in request.COOKIES else []
        response = self.get_response(request)
        shown = (request.hj_flash and not is_htmx(request) and not (300 <= response.status_code < 400)
                 and response.get('Content-Type', '').startswith('text/html'))
        if shown and not getattr(response, '_hj_flash_items', None):
            response.delete_cookie(FLASH_COOKIE, path='/', samesite='Lax')
        return response


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault('Content-Security-Policy', CSP)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        return response


def csrf_failure(request, reason=''):
    """CSRF_FAILURE_VIEW: a refused cross-site or token-less form post (TS: 'cross-site request refused')."""
    log.info('csrf refused: %s', reason)
    return error_response(request, 403, 'cross-site request refused')


def _static_middleware():
    from whitenoise.middleware import WhiteNoiseMiddleware

    class StaticFilesMiddleware(WhiteNoiseMiddleware):
        """WhiteNoise, quiet about a STATIC_ROOT that collectstatic has not filled yet (development and tests read the
        files from STATICFILES_DIRS through the finders)."""

        def add_files(self, root, prefix=None):
            from pathlib import Path
            if Path(root).is_dir():
                super().add_files(root, prefix)

    return StaticFilesMiddleware


StaticFilesMiddleware = _static_middleware()
