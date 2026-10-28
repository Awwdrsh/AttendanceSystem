"""
JSON sign-in for non-browser clients (Bearer-token integrations): the /api/v1/auth endpoints of the TS API that such a
client needs. Browser sign-in pages are the web app's. Same rules as apps/api/src/modules/auth/routes.ts: a per-address
throttle on failed sign-ins, the session cookie set as well as the token returned, two-step sign-in in two calls.
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from hajiri.core import auth
from hajiri.core.api import HajiriAuthentication, SignedInAnyState
from hajiri.core.crypto import password_policy_error
from hajiri.core.errors import BadRequestError, ForbiddenError, HttpError, TooManyRequestsError
from hajiri.core.cors import cors_origins
from hajiri.core.validation import validate

MAX_FAILURES_PER_IP = 30
_WINDOW = 15 * 60
_generation = 0          # bumped by reset_throttle, so old counters are no longer read


def _key(ip: str) -> str:
    return f'hajiri:auth-api:failures:{_generation}:{ip}'


def _throttled(ip: str) -> bool:
    """Too many failed sign-ins from this address in the current 15-minute window (Django's cache framework)."""
    return (cache.get(_key(ip)) or 0) >= MAX_FAILURES_PER_IP


def _record_failure(ip: str) -> None:
    # The window starts at the first failure: add() sets the timeout once, incr() keeps it.
    cache.add(_key(ip), 0, _WINDOW)
    try:
        cache.incr(_key(ip))
    except ValueError:              # expired between the two calls: start a new window
        cache.set(_key(ip), 1, _WINDOW)


def reset_throttle() -> None:
    global _generation
    _generation += 1


def _ip(request) -> str:
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _meta(request) -> auth.SessionMeta:
    return auth.SessionMeta(ip=_ip(request), user_agent=request.headers.get('User-Agent', ''))


def user_json(u: auth.AuthUser) -> dict:
    return {'id': u.id, 'companyId': u.company_id, 'employeeId': u.employee_id, 'email': u.email, 'name': u.name,
            'roles': list(u.roles), 'mustChangePassword': u.must_change_password, 'twoFactorEnabled': u.two_factor_enabled,
            'mustSetUpTwoFactor': u.must_set_up_two_factor}


def _with_cookie(res, token: str):
    res.set_cookie(auth.SESSION_COOKIE, token, max_age=int(auth.SESSION_TTL.total_seconds()), path='/', httponly=True,
                   samesite='Lax', secure=getattr(settings, 'SESSION_COOKIE_SECURE', False))
    return res


class LoginIn(serializers.Serializer):
    email = serializers.EmailField(max_length=200, trim_whitespace=False)
    password = serializers.CharField(min_length=1, max_length=128, trim_whitespace=False)


class Login2faIn(serializers.Serializer):
    challenge = serializers.CharField(min_length=20, max_length=100, trim_whitespace=False)
    code = serializers.CharField(min_length=6, max_length=20)


class ChangePasswordIn(serializers.Serializer):
    currentPassword = serializers.CharField(max_length=128, allow_blank=True, trim_whitespace=False)
    newPassword = serializers.CharField(max_length=256, allow_blank=True, trim_whitespace=False)


def same_origin(request, allowed: tuple[str, ...] = ()) -> bool:
    """A state-changing request carried by the session cookie must come from this site's own origin or a configured
    CORS origin (the TS sameOriginGuard); Bearer requests and reads always pass."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    origin = request.headers.get('Origin')
    if not origin or origin == 'null':
        ref = request.headers.get('Referer')
        if not ref:
            return origin != 'null'
        u = urlparse(ref)
        origin = f'{u.scheme}://{u.netloc}'
    return origin == f'{request.scheme}://{request.get_host()}' or origin in allowed


class OriginCheckedAuthentication(HajiriAuthentication):
    """The session cookie or a Bearer token, with the TS API's Origin check for cookie requests instead of a CSRF
    token: these endpoints serve JSON clients on other origins (CORS_ORIGINS) that never see the CSRF cookie."""

    def enforce_csrf(self, request):
        if not same_origin(request, cors_origins()):
            raise ForbiddenError('cross-site request refused')


AUTH = [OriginCheckedAuthentication]


class OriginCheckedAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = 'hajiri.integrations.auth_api.OriginCheckedAuthentication'
    name = ['sessionOriginChecked', 'bearerToken']   # distinct from the default scheme's names

    def get_security_definition(self, auto_schema):
        return [{'type': 'apiKey', 'in': 'cookie', 'name': auth.SESSION_COOKIE}, {'type': 'http', 'scheme': 'bearer'}]


@extend_schema(request=LoginIn, responses=OpenApiTypes.OBJECT, summary='Sign in with email and password')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([AllowAny])
def login(request):
    b = validate(LoginIn, request.data)
    ip = _ip(request)
    if _throttled(ip):
        raise TooManyRequestsError('too many failed sign-in attempts; try again in 15 minutes')
    try:
        r = auth.login(b['email'], b['password'], _meta(request))
    except HttpError:
        _record_failure(ip)
        raise
    if r.two_factor_required:
        return Response({'twoFactorRequired': True, 'challenge': r.challenge})
    return _with_cookie(Response({'user': user_json(r.user), 'token': r.token}), r.token)


@extend_schema(request=Login2faIn, responses=OpenApiTypes.OBJECT, summary='Second step of two-step sign-in')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([AllowAny])
def login_2fa(request):
    """Second step: the 6-digit code from the authenticator app, or a one-time recovery code."""
    b = validate(Login2faIn, request.data)
    ip = _ip(request)
    if _throttled(ip):
        raise TooManyRequestsError('too many failed sign-in attempts; try again in 15 minutes')
    try:
        r = auth.complete_two_factor_login(b['challenge'], b['code'], _meta(request))
    except HttpError:
        _record_failure(ip)
        raise
    body = {'user': user_json(r.user), 'token': r.token, **({'usedRecoveryCode': True} if r.method == 'recovery' else {})}
    return _with_cookie(Response(body), r.token)


@extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Sign out')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def logout(request):
    if request.auth:
        auth.logout(request.auth)
    res = Response({'ok': True})
    res.delete_cookie(auth.SESSION_COOKIE, path='/')
    return res


@extend_schema(responses=OpenApiTypes.OBJECT, summary='The signed-in account (active company)')
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def me(request):
    return Response({'user': user_json(request.user)})


@extend_schema(request=ChangePasswordIn, responses=OpenApiTypes.OBJECT, summary='Change the password (signs out everywhere)')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def change_password(request):
    b = validate(ChangePasswordIn, request.data)
    err = password_policy_error(b['newPassword'])
    if err:
        raise BadRequestError(err)
    auth.change_password(request.user.id, b['currentPassword'], b['newPassword'])
    res = Response({'ok': True})
    res.delete_cookie(auth.SESSION_COOKIE, path='/')
    return res
