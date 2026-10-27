"""
Access decorators for views. Deny by default; admin always passes a role check.

- signed_in: any valid session (the TS requireAuth). Only for pages that must work before the password-change and
  two-step gates: change password, two-step setup, sign-in security, sign out.
- login_required: signed in, password changed and two-step sign-in set up when the company requires it.
- require_role(*roles) / require_module(area): login_required plus a role check ('requires role hr or manager').
- require_feature(key): the company feature switch must be on; a page redirects home (FeatureRoute), anything else
  answers 404 'feature off' (featureGate).
"""
from functools import wraps
from urllib.parse import quote

from django.shortcuts import redirect

from hajiri.core.auth import MODULE_ROLES, TWO_FACTOR_SETUP_MESSAGE, has_role
from hajiri.core.errors import FeatureOffError, ForbiddenError, UnauthorizedError


def _page_get(request) -> bool:
    """A browser asking for a page (not HTMX, not an API client): it gets redirected instead of an error."""
    from .middleware import wants_json
    return request.method == 'GET' and not request.headers.get('HX-Request') and not wants_json(request)


def _to_login(request):
    nxt = request.get_full_path()
    return redirect('/login' if nxt == '/' else f'/login?next={quote(nxt, safe="/")}')


def signed_in(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.hajiri_user:
            if request.auth_method is None and _page_get(request):
                return _to_login(request)
            raise UnauthorizedError()
        return view(request, *args, **kwargs)
    return wrapped


def login_required(view):
    """Signed in, password changed and two-step sign-in set up when the company requires it."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.hajiri_user
        if not user:
            if request.auth_method is None and _page_get(request):
                return _to_login(request)
            raise UnauthorizedError()
        if user.must_change_password:
            if _page_get(request):
                return redirect('/change-password')
            raise ForbiddenError('password change required')
        if user.must_set_up_two_factor:
            if _page_get(request):
                return redirect('/setup-two-step')
            raise ForbiddenError(TWO_FACTOR_SETUP_MESSAGE)
        return view(request, *args, **kwargs)
    return wrapped


def require_role(*roles):
    def deco(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not has_role(request.hajiri_user, *roles):
                raise ForbiddenError(f'requires role {" or ".join(roles)}')
            return view(request, *args, **kwargs)
        return wrapped
    return deco


def require_module(module):
    return require_role(*MODULE_ROLES[module])


def require_feature(key):
    """Put it under login_required/require_role (the user's company decides)."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            from .nav import feature_on
            if request.hajiri_user and request.hajiri_user.company_id and not feature_on(request, key):
                if _page_get(request):
                    return redirect('/')
                raise FeatureOffError(key)
            return view(request, *args, **kwargs)
        return wrapped
    return deco
