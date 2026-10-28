"""
Django REST framework integration: how JSON endpoints authenticate, check access, answer errors and render.

Configured as the REST_FRAMEWORK defaults in config/settings.py, so a view only names what is special about it:

    from rest_framework import serializers
    from rest_framework.decorators import api_view, permission_classes
    from rest_framework.response import Response

    from hajiri.core.api import FeatureEnabled, HasRole

    @api_view(['GET'])
    @permission_classes([HasRole('hr'), FeatureEnabled('helpdesk')])
    def ticket_stats(request):
        return Response(services.stats(request.user))      # request.user is the AuthUser (active company)

- HajiriAuthentication: the `hajiri_session` cookie (CSRF enforced, as Django does for its own session auth) or an
  `Authorization: Bearer` token (no CSRF: browsers never add that header by themselves). The session is the one
  AuthMiddleware already resolved, so it is read once per request.
- Permissions (default IsSignedIn): IsSignedIn (signed in, password changed, two-step set up when required),
  SignedInAnyState (only the session, for password change and two-step setup), HasRole(*roles) (admin always
  passes), HasModule(area) (MODULE_ROLES), FeatureEnabled(key) (the company's feature switch). Refusals raise the
  typed errors with the TS messages ('requires role hr or manager', 'password change required', 'feature off').
- exception_handler: every error in the TS JSON shape, `{"error": message}` (+ details), validation as
  `{"error": "validation", "issues": [{path, message}]}`, database errors mapped like the TS error handler.
- JsonRenderer: dates ISO, timestamps ISO 8601 UTC with milliseconds (JSON.stringify), decimals and UUIDs as strings.
- Pagination: ?page=&pageSize= on list views, answered as {"items", "total", "page", "pageSize"}.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
import uuid

from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig
from django.db import DatabaseError
from django.http import Http404
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import exceptions as drf_exc
from rest_framework import permissions
from rest_framework.authentication import BaseAuthentication
from rest_framework.pagination import PageNumberPagination
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from .auth import MODULE_ROLES, SESSION_COOKIE, TWO_FACTOR_SETUP_MESSAGE, has_role, resolve_session
from .errors import (
    FeatureOffError, ForbiddenError, HttpError, NotFoundError, UnauthorizedError, ValidationError, error_body,
    from_db_error,
)
from .validation import issues_from

CSRF_REFUSED = 'cross-site request refused'


# ---------------------------------------------------------------- JSON

class ApiEncoder(json.JSONEncoder):
    """Dates as ISO strings, timestamps as ISO 8601 UTC with milliseconds (like JSON.stringify), numerics as strings."""

    def default(self, o):
        if isinstance(o, _dt.datetime):
            if o.tzinfo:
                o = o.astimezone(_dt.timezone.utc)
                return o.strftime('%Y-%m-%dT%H:%M:%S.') + f'{o.microsecond // 1000:03d}Z'
            return o.isoformat()
        if isinstance(o, (_dt.date, _dt.time)):
            return o.isoformat()
        if isinstance(o, (decimal.Decimal, uuid.UUID)):
            return str(o)
        if isinstance(o, (bytes, memoryview)):
            return None
        if isinstance(o, (set, frozenset, tuple)):
            return list(o)
        if hasattr(o, 'to_dict'):
            return o.to_dict()
        return super().default(o)


class JsonRenderer(JSONRenderer):
    encoder_class = ApiEncoder
    ensure_ascii = False


# ---------------------------------------------------------------- authentication

def _bearer(request) -> str | None:
    header = request.headers.get('Authorization', '')
    return header[7:] if header.startswith('Bearer ') else None


class _CsrfCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


class HajiriAuthentication(BaseAuthentication):
    """The hajiri_session cookie or a Bearer token (same sessions as the TS API). Returns (AuthUser, token)."""

    def authenticate(self, request):
        raw = request._request
        user = getattr(raw, 'hajiri_user', None)
        token = getattr(raw, 'session_token', None)
        method = getattr(raw, 'auth_method', None)
        if user is None:
            # AuthMiddleware did not run (a test calling the view directly): resolve the token here.
            bearer = _bearer(raw)
            token = bearer or raw.COOKIES.get(SESSION_COOKIE)
            user = resolve_session(token) if token else None
            if user is None:
                return None
            method = 'bearer' if bearer else 'cookie'
            raw.hajiri_user, raw.session_token, raw.auth_method = user, token, method
        if method == 'cookie':
            self.enforce_csrf(raw)
        return user, token

    def enforce_csrf(self, request):
        check = _CsrfCheck(lambda r: None)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise ForbiddenError(CSRF_REFUSED)

    def authenticate_header(self, request):
        return 'Bearer'


# ---------------------------------------------------------------- permissions

def _user(request):
    u = getattr(request, 'user', None)
    return u if u is not None and hasattr(u, 'roles') else None


class SignedInAnyState(permissions.BasePermission):
    """Any valid session, before the password-change and two-step gates (password change, two-step setup)."""

    def has_permission(self, request, view):
        if _user(request) is None:
            raise UnauthorizedError()
        return True


class IsSignedIn(SignedInAnyState):
    """Signed in, password changed and two-step sign-in set up when the company requires it."""

    def has_permission(self, request, view):
        super().has_permission(request, view)
        user = _user(request)
        if user.must_change_password:
            raise ForbiddenError('password change required')
        if user.must_set_up_two_factor:
            raise ForbiddenError(TWO_FACTOR_SETUP_MESSAGE)
        return True


class _HasRole(IsSignedIn):
    roles: tuple[str, ...] = ()

    def has_permission(self, request, view):
        super().has_permission(request, view)
        if not has_role(_user(request), *self.roles):
            raise ForbiddenError(f'requires role {" or ".join(self.roles)}')
        return True


def HasRole(*roles: str) -> type[permissions.BasePermission]:
    """Permission class: signed in with any of the roles in the ACTIVE company (admin always passes)."""
    return type(f'HasRole_{"_".join(roles)}', (_HasRole,), {'roles': tuple(roles)})


def HasModule(area: str) -> type[permissions.BasePermission]:
    """Permission class: one of the roles that work in an HR area (MODULE_ROLES: payroll, attendance, hiring, insights)."""
    return HasRole(*MODULE_ROLES[area])


class _FeatureEnabled(permissions.BasePermission):
    key = ''

    def has_permission(self, request, view):
        from hajiri.web.nav import feature_on
        user = _user(request)
        if user is not None and user.company_id and not feature_on(request._request, self.key):
            raise FeatureOffError(self.key)
        return True


def FeatureEnabled(key: str) -> type[permissions.BasePermission]:
    """Permission class: the active company's feature switch is on, else 404 {"error": "feature off", "feature"}."""
    return type(f'FeatureEnabled_{key}', (_FeatureEnabled,), {'key': key})


# ---------------------------------------------------------------- errors

class _TooLarge(HttpError):
    status, default_message = 413, 'request body too large'


def _as_http_error(exc, context) -> HttpError | None:
    if isinstance(exc, HttpError):
        return exc
    if isinstance(exc, DatabaseError):
        return from_db_error(exc)
    if isinstance(exc, (Http404, ObjectDoesNotExist, drf_exc.NotFound)):
        return NotFoundError()
    if isinstance(exc, RequestDataTooBig):
        return _TooLarge()
    if isinstance(exc, drf_exc.ValidationError):
        field = None
        view = context.get('view') if context else None
        if view is not None and hasattr(view, 'get_serializer'):
            try:
                field = view.get_serializer()
            except Exception:  # noqa: BLE001 - only used to word the messages
                field = None
        return ValidationError(issues_from(exc.detail, field))
    if isinstance(exc, (drf_exc.NotAuthenticated, drf_exc.AuthenticationFailed)):
        return UnauthorizedError()
    if isinstance(exc, drf_exc.PermissionDenied):
        msg = str(exc.detail)
        return ForbiddenError(CSRF_REFUSED if msg.startswith('CSRF Failed') else 'forbidden' if msg == drf_exc.PermissionDenied.default_detail else msg)
    if isinstance(exc, drf_exc.MethodNotAllowed):
        return NotFoundError()          # Express answered an unrouted method with 404
    if isinstance(exc, drf_exc.ParseError):
        e = HttpError('invalid JSON body')
        e.status = 400
        return e
    if isinstance(exc, drf_exc.UnsupportedMediaType):
        e = HttpError('unsupported media type')
        e.status = 415
        return e
    if isinstance(exc, drf_exc.Throttled):
        e = HttpError('too many requests')
        e.status = 429
        return e
    if isinstance(exc, drf_exc.APIException):
        e = HttpError(str(exc.detail))
        e.status = exc.status_code
        return e
    return None


def exception_handler(exc, context):
    """REST_FRAMEWORK EXCEPTION_HANDLER: the TS API's error bodies. Unknown exceptions propagate (500)."""
    from rest_framework.views import set_rollback   # not at import: rest_framework.views reads these settings
    err = _as_http_error(exc, context)
    if err is None:
        return None
    set_rollback()
    return Response(error_body(err), status=err.status)


# ---------------------------------------------------------------- pagination

class Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'pageSize'
    max_page_size = 500

    def get_paginated_response(self, data):
        return Response({'items': data, 'total': self.page.paginator.count, 'page': self.page.number,
                         'pageSize': self.get_page_size(self.request)})

    def get_paginated_response_schema(self, schema):
        return {'type': 'object', 'properties': {
            'items': schema, 'total': {'type': 'integer'}, 'page': {'type': 'integer'}, 'pageSize': {'type': 'integer'}}}


# ---------------------------------------------------------------- OpenAPI (drf-spectacular)

try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class HajiriAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = 'hajiri.core.api.HajiriAuthentication'
        name = ['session', 'bearer']

        def get_security_definition(self, auto_schema):
            return [{'type': 'apiKey', 'in': 'cookie', 'name': SESSION_COOKIE},
                    {'type': 'http', 'scheme': 'bearer'}]
except ImportError:  # pragma: no cover
    pass
