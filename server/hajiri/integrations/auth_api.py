"""
JSON sign-in for non-browser clients (Bearer-token integrations): the /api/v1/auth endpoints of the TS API that such a
client needs. Browser sign-in pages are the web app's. Same rules as apps/api/src/modules/auth/routes.ts: a per-address
throttle on failed sign-ins, the session cookie set as well as the token returned, two-step sign-in in two calls.
"""
from __future__ import annotations

import threading
import time

from django.conf import settings

from hajiri.core import auth
from hajiri.core.crypto import password_policy_error
from hajiri.core.errors import BadRequestError, HttpError, TooManyRequestsError
from hajiri.core.http import api_view, json_response, read_json
from hajiri.core.schema import Obj, Str, parse

MAX_FAILURES_PER_IP = 30
_WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}   # ip -> [count, reset_at]
_lock = threading.Lock()


def _throttled(ip: str) -> bool:
    with _lock:
        a = _failures.get(ip)
        return bool(a) and a[1] >= time.time() and a[0] >= MAX_FAILURES_PER_IP


def _record_failure(ip: str) -> None:
    now = time.time()
    with _lock:
        # Drop expired windows so a flood of distinct addresses cannot grow the map without bound.
        if len(_failures) > 10_000:
            for k in [k for k, v in _failures.items() if v[1] < now]:
                del _failures[k]
        a = _failures.get(ip)
        if not a or a[1] < now:
            _failures[ip] = [1, now + _WINDOW]
        else:
            a[0] += 1


def reset_throttle() -> None:
    with _lock:
        _failures.clear()


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


LOGIN = Obj({'email': Str(email=True, max=200), 'password': Str(min=1, max=128)})
LOGIN_2FA = Obj({'challenge': Str(min=20, max=100), 'code': Str(trim=True, min=6, max=20)})
CHANGE_PASSWORD = Obj({'currentPassword': Str(max=128), 'newPassword': Str(max=256)})


@api_view(methods=('POST',), auth=False)
def login(request):
    b = parse(LOGIN, read_json(request))
    ip = _ip(request)
    if _throttled(ip):
        raise TooManyRequestsError('too many failed sign-in attempts; try again in 15 minutes')
    try:
        r = auth.login(b['email'], b['password'], _meta(request))
    except HttpError:
        _record_failure(ip)
        raise
    if r.two_factor_required:
        return {'twoFactorRequired': True, 'challenge': r.challenge}
    return _with_cookie(json_response({'user': user_json(r.user), 'token': r.token}), r.token)


@api_view(methods=('POST',), auth=False)
def login_2fa(request):
    """Second step: the 6-digit code from the authenticator app, or a one-time recovery code."""
    b = parse(LOGIN_2FA, read_json(request))
    ip = _ip(request)
    if _throttled(ip):
        raise TooManyRequestsError('too many failed sign-in attempts; try again in 15 minutes')
    try:
        r = auth.complete_two_factor_login(b['challenge'], b['code'], _meta(request))
    except HttpError:
        _record_failure(ip)
        raise
    body = {'user': user_json(r.user), 'token': r.token, **({'usedRecoveryCode': True} if r.method == 'recovery' else {})}
    return _with_cookie(json_response(body), r.token)


@api_view(methods=('POST',), gates=False)
def logout(request):
    if request.session_token:
        auth.logout(request.session_token)
    res = json_response({'ok': True})
    res.delete_cookie(auth.SESSION_COOKIE, path='/')
    return res


@api_view(methods=('GET',), gates=False)
def me(request):
    return {'user': user_json(request.hajiri_user)}


@api_view(methods=('POST',), gates=False)
def change_password(request):
    b = parse(CHANGE_PASSWORD, read_json(request))
    err = password_policy_error(b['newPassword'])
    if err:
        raise BadRequestError(err)
    auth.change_password(request.hajiri_user.id, b['currentPassword'], b['newPassword'])
    res = json_response({'ok': True})
    res.delete_cookie(auth.SESSION_COOKIE, path='/')
    return res
