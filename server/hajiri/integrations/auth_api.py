"""
JSON sign-in for non-browser clients (Bearer-token integrations): the /api/v1/auth endpoints of the TS API that such a
client needs. Browser sign-in pages are the web app's. Same rules as apps/api/src/modules/auth/routes.ts: a per-address
throttle on failed sign-ins, the session cookie set as well as the token returned, two-step sign-in in two calls.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from hajiri.core import auth, users
from hajiri.core.api import HasRole, IsSignedIn, SameOriginAuthentication, SignedInAnyState, same_origin  # noqa: F401
from hajiri.core.crypto import password_policy_error
from hajiri.core.errors import BadRequestError, HttpError, TooManyRequestsError
from hajiri.core.validation import parse_uuid, validate

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


class OriginCheckedAuthentication(SameOriginAuthentication):
    """The session cookie or a Bearer token, with the TS API's Origin check for cookie requests instead of a CSRF
    token: these endpoints serve JSON clients on other origins (CORS_ORIGINS) that never see the CSRF cookie."""


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


# ---------------------------------------------------------------- two-step sign-in (self)
# Like the TS routes these work before the password-change and two-step gates (SignedInAnyState), so a person who
# must set it up can do so.

class TwoFactorCodeIn(serializers.Serializer):
    code = serializers.CharField(min_length=6, max_length=10)


class RecoveryCodesIn(serializers.Serializer):
    code = serializers.CharField(min_length=6, max_length=20)


class DisableTwoFactorIn(serializers.Serializer):
    password = serializers.CharField(min_length=1, max_length=128, trim_whitespace=False)
    code = serializers.CharField(min_length=6, max_length=20)


class ResetTwoFactorIn(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=300)


def _status_json(s: auth.TwoFactorStatus) -> dict:
    return {'enabled': s.enabled, 'enabledAt': s.enabled_at, 'recoveryCodesLeft': s.recovery_codes_left,
            'required': s.required, 'setupPending': s.setup_pending}


@extend_schema(responses=OpenApiTypes.OBJECT, summary='Two-step sign-in status')
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def two_factor(request):
    return Response(_status_json(auth.two_factor_status(request.user)))


@extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Start setting up two-step sign-in')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def two_factor_setup(request):
    if request.user.must_change_password:
        raise BadRequestError('choose a new password first')
    s = auth.begin_setup(request.user)
    return Response({'secret': s['secret'], 'otpauthUri': s['otpauth_uri']})


@extend_schema(request=TwoFactorCodeIn, responses=OpenApiTypes.OBJECT, summary='Turn two-step sign-in on')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def two_factor_enable(request):
    b = validate(TwoFactorCodeIn, request.data)
    codes = auth.confirm_setup(request.user, b['code'])
    from hajiri.core.crypto import sha256_hex
    from hajiri.db.models import Session
    # The current session has just proved the second factor; sessions opened before it existed may be someone else's.
    if request.auth:
        Session.objects.filter(token_hash=sha256_hex(request.auth)).update(two_factor=True)
    auth.revoke_other_sessions(request.user.id, request.auth)
    return Response({'recoveryCodes': codes})


@extend_schema(request=RecoveryCodesIn, responses=OpenApiTypes.OBJECT, summary='New recovery codes')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def two_factor_recovery_codes(request):
    b = validate(RecoveryCodesIn, request.data)
    return Response({'recoveryCodes': auth.regenerate_recovery_codes(request.user, b['code'])})


@extend_schema(request=DisableTwoFactorIn, responses=OpenApiTypes.OBJECT, summary='Turn two-step sign-in off')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def two_factor_disable(request):
    b = validate(DisableTwoFactorIn, request.data)
    auth.disable_two_factor(request.user, b['password'], b['code'])
    return Response({'ok': True})


# ---------------------------------------------------------------- my sessions

def _session_json(s: auth.SessionInfo) -> dict:
    return {'id': s.id, 'createdAt': s.created_at, 'lastSeenAt': s.last_seen_at, 'expiresAt': s.expires_at, 'ip': s.ip,
            'device': s.device, 'twoFactor': s.two_factor, 'current': s.current}


@extend_schema(responses=OpenApiTypes.OBJECT, summary='My signed-in sessions')
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def sessions(request):
    return Response({'sessions': [_session_json(s) for s in auth.list_sessions(request.user.id, request.auth)]})


@extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Sign out every other session')
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def sessions_revoke_others(request):
    return Response({'revoked': auth.revoke_other_sessions(request.user.id, request.auth)})


@extend_schema(request=None, responses=OpenApiTypes.OBJECT, summary='Sign out one other session')
@api_view(['DELETE'])
@authentication_classes(AUTH)
@permission_classes([SignedInAnyState])
def session_revoke(request, session_id):
    auth.revoke_session(request.user.id, parse_uuid(session_id), request.auth)
    return Response({'ok': True})


# ---------------------------------------------------------------- account administration
# Needs a real (not temporary) password and two-step sign-in set up when required: HasRole builds on IsSignedIn.

@extend_schema(request=ResetTwoFactorIn, responses=OpenApiTypes.OBJECT, summary="Reset someone's two-step sign-in (admin)")
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([HasRole('admin')])
def user_two_factor_reset(request, user_id):
    target = parse_uuid(user_id)
    b = validate(ResetTwoFactorIn, request.data)
    auth.admin_reset_two_factor(request.user, target, b['reason'])
    return Response({'ok': True})


@extend_schema(methods=['GET'], responses=OpenApiTypes.OBJECT, summary='Users of the company (HR)')
@extend_schema(methods=['POST'], request=users.CreateUserIn, responses={201: OpenApiTypes.OBJECT}, summary='Create a user (admin)')
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes([IsSignedIn])
def user_list(request):
    # The services check the role: listing is for HR (and admin), creating for admin.
    if request.method == 'POST':
        return Response({'user': user_json(users.admin_create_user(request.user, request.data))}, status=201)
    return Response({'users': users.list_users(request.user)})


@extend_schema(request=users.UpdateUserIn, responses=OpenApiTypes.OBJECT, summary='Change roles, status or employee link (admin)')
@api_view(['PATCH'])
@authentication_classes(AUTH)
@permission_classes([HasRole('admin')])
def user_detail(request, user_id):
    return Response({'user': users.admin_update_user(request.user, parse_uuid(user_id), request.data)})
