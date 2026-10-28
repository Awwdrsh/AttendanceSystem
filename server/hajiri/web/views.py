"""
Sign-in pages (LoginPage, ChangePasswordPage, TwoStepSetupPage, SecurityPage) and the auth routes they use
(modules/auth/routes.ts: login, login/2fa, logout, change-password, 2fa/*, sessions/*), plus the PWA files, the
employee search behind EmployeePicker and the component gallery.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.db.models import Q
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core import auth
from hajiri.core.crypto import password_policy_error, sha256_hex
from hajiri.db.models import Employee, Session
from hajiri.core.errors import BadRequestError, ForbiddenError, HttpError, UnauthorizedError, ValidationError

from . import format as fmt
from .decorators import login_required, signed_in
from .forms import ChangePasswordForm, CodeForm, DisableForm, EnableForm, LoginForm, TwoFactorLoginForm, first_error
from .htmx import hx_redirect, is_htmx, toast, trigger
from .middleware import wants_json

STATIC_DIR = Path(settings.BASE_DIR) / 'static'

# ---------------------------------------------------------------- per-IP throttle on failed sign-ins
# Successful sign-ins are not counted, so a whole office behind one NAT address can sign in at 9 am; guessing from one
# address still stops at 30 failures in 15 minutes.
MAX_FAILURES_PER_IP = 30
_WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}   # ip -> [count, reset_at]
_lock = threading.Lock()
THROTTLED = 'too many failed sign-in attempts; try again in 15 minutes'


def client_ip(request) -> str:
    """REMOTE_ADDR, or the first X-Forwarded-For hop when the request came through a proxy on this machine."""
    remote = request.META.get('REMOTE_ADDR') or 'unknown'
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    if fwd and remote in ('127.0.0.1', '::1'):
        return fwd.split(',')[0].strip() or remote
    return remote


def throttled(ip: str) -> bool:
    with _lock:
        a = _failures.get(ip)
        return bool(a) and a[1] >= time.time() and a[0] >= MAX_FAILURES_PER_IP


def record_failure(ip: str) -> None:
    now = time.time()
    with _lock:
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


# ---------------------------------------------------------------- helpers

def _meta(request) -> auth.SessionMeta:
    return auth.SessionMeta(ip=client_ip(request), user_agent=request.headers.get('User-Agent', ''))


def _safe_next(request, nxt: str | None) -> str:
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()) \
            and nxt.startswith('/') and not nxt.startswith('/login'):
        return nxt
    return '/'


def next_url(request, user: auth.AuthUser, nxt: str | None = None) -> str:
    """Where to go after signing in: the password change, the two-step setup, or where the user was heading."""
    if user.must_change_password:
        return '/change-password'
    if user.must_set_up_two_factor:
        return '/setup-two-step'
    return _safe_next(request, nxt)


def set_session_cookie(response: HttpResponse, token: str) -> HttpResponse:
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=int(auth.SESSION_TTL.total_seconds()), path='/',
                        httponly=True, samesite='Lax', secure=settings.SESSION_COOKIE_SECURE)
    return response


def clear_session_cookie(response: HttpResponse) -> HttpResponse:
    response.delete_cookie(auth.SESSION_COOKIE, path='/', samesite='Lax')
    return response


def user_json(u: auth.AuthUser) -> dict:
    return {'id': u.id, 'companyId': u.company_id, 'employeeId': u.employee_id, 'email': u.email, 'name': u.name,
            'roles': list(u.roles), 'mustChangePassword': u.must_change_password,
            'twoFactorEnabled': u.two_factor_enabled, 'mustSetUpTwoFactor': u.must_set_up_two_factor}


def _issues(form) -> ValidationError:
    """A failed form as the TS API's zod answer: {'error': 'validation', 'issues': [{path, message}]}."""
    return ValidationError([{'path': '' if name == '__all__' else name, 'message': str(e)}
                            for name, errs in form.errors.items() for e in errs])


def _json_body(request) -> dict:
    import json
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        raise BadRequestError('invalid JSON body')
    if not isinstance(data, dict):
        raise BadRequestError('invalid JSON body')
    return data


# ---------------------------------------------------------------- sign in

def sample_morning() -> dict:
    """A fixed, made-up morning used as the login illustration: arrivals cluster just before a 10:00 start."""
    seed = 7

    def rnd():
        nonlocal seed
        seed = (seed * 16807) % 2147483647
        return seed / 2147483647
    marks = []
    for _ in range(64):
        g = (rnd() + rnd() + rnd()) / 3
        m = fmt.round_half_up(9 * 60 + 10 + g * 75)
        marks.append((m, m > 10 * 60 + 15))
    marks.sort(key=lambda k: k[0])
    frm, to = 8 * 60 + 30, 11 * 60
    span = to - frm
    stacks: dict[int, int] = {}
    out = []
    for m, late in marks:
        b = m // 4
        level = stacks.get(b, 0)
        stacks[b] = level + 1
        out.append({'left': round((b * 4 - frm) / span * 100, 3), 'bottom': 28 + level * 10, 'late': late,
                    'opacity': 0.9 if late else round(0.55 + (level % 4) * 0.12, 2)})
    return {'marks': out, 'zone_left': round((10 * 60 - frm) / span * 100, 3), 'zone_width': round(15 / span * 100, 3),
            'hours': [{'label': f'{h:02d}:00', 'left': round((h * 60 - frm) / span * 100, 3)} for h in (9, 10, 11)]}


def _login_page(request, *, form=None, error=None, status=200, challenge=None, recovery=False, nxt=''):
    ctx = {'form': form or LoginForm(), 'error': error, 'challenge': challenge, 'recovery': recovery, 'next': nxt,
           'morning': None if challenge else sample_morning()}
    return render(request, 'web/login.html', ctx, status=status)


@require_http_methods(['GET', 'POST'])
def login_view(request):
    nxt = request.POST.get('next') or request.GET.get('next') or ''
    if request.method == 'GET':
        if request.hajiri_user:
            return HttpResponseRedirect(next_url(request, request.hajiri_user, nxt))
        return _login_page(request, nxt=nxt)

    json_client = wants_json(request)
    data = _json_body(request) if request.content_type == 'application/json' else request.POST
    form = LoginForm(data)
    if not form.is_valid():
        if json_client:
            raise _issues(form)
        return _login_page(request, form=form, error=first_error(form), status=400, nxt=nxt)
    ip = client_ip(request)
    if throttled(ip):
        if json_client:
            return JsonResponse({'error': THROTTLED}, status=429)
        return _login_page(request, form=form, error=THROTTLED, status=429, nxt=nxt)
    try:
        result = auth.login(form.cleaned_data['email'].strip(), form.cleaned_data['password'], _meta(request))
    except UnauthorizedError as e:
        record_failure(ip)
        if json_client:
            raise
        return _login_page(request, form=LoginForm(initial={'email': form.cleaned_data['email']}), error=e.message,
                           status=401, nxt=nxt)
    if result.two_factor_required:
        if json_client:
            return JsonResponse({'twoFactorRequired': True, 'challenge': result.challenge})
        return _login_page(request, challenge=result.challenge, nxt=nxt)
    if json_client:
        return set_session_cookie(JsonResponse({'user': user_json(result.user), 'token': result.token}), result.token)
    return set_session_cookie(HttpResponseRedirect(next_url(request, result.user, nxt)), result.token)


@require_POST
def login_two_factor(request):
    """Second sign-in step: the 6-digit code from the authenticator app, or a one-time recovery code."""
    json_client = wants_json(request)
    data = _json_body(request) if request.content_type == 'application/json' else request.POST
    form = TwoFactorLoginForm(data)
    nxt = (data.get('next') or '') if hasattr(data, 'get') else ''
    recovery = str(data.get('recovery', '')).lower() in ('1', 'true', 'on')
    challenge = str(data.get('challenge', ''))[:100]
    if not form.is_valid():
        if json_client:
            raise _issues(form)
        if 'challenge' in form.errors:
            return _login_page(request, error=auth._EXPIRED, status=401, nxt=nxt)
        return _login_page(request, challenge=challenge, recovery=recovery, error=first_error(form), status=400, nxt=nxt)
    ip = client_ip(request)
    if throttled(ip):
        if json_client:
            return JsonResponse({'error': THROTTLED}, status=429)
        return _login_page(request, challenge=challenge, recovery=recovery, error=THROTTLED, status=429, nxt=nxt)
    try:
        result = auth.complete_two_factor_login(form.cleaned_data['challenge'], form.cleaned_data['code'].strip(), _meta(request))
    except UnauthorizedError as e:
        record_failure(ip)
        if json_client:
            raise
        # An expired or used-up challenge means starting again from the password.
        if 'expired' in e.message.lower():
            return _login_page(request, error=e.message, status=401, nxt=nxt)
        return _login_page(request, challenge=challenge, recovery=recovery, error=e.message, status=401, nxt=nxt)
    if json_client:
        body = {'user': user_json(result.user), 'token': result.token, **({'usedRecoveryCode': True} if result.method == 'recovery' else {})}
        return set_session_cookie(JsonResponse(body), result.token)
    resp = set_session_cookie(HttpResponseRedirect(next_url(request, result.user, nxt)), result.token)
    if result.method == 'recovery':
        toast(request, resp, 'That recovery code is now used up. Make new ones under Profile, Sign-in security if you are '
              'running low.', 'info', 'Signed in with a recovery code')
    return resp


@require_POST
def logout_view(request):
    if not request.hajiri_user:
        if wants_json(request):
            raise UnauthorizedError()
        # The session already ended (idle timeout): signing out just goes to the sign-in page (AuthProvider.logout).
        return clear_session_cookie(hx_redirect(request, '/login'))
    if request.session_token:
        auth.logout(request.session_token)
    if wants_json(request):
        return clear_session_cookie(JsonResponse({'ok': True}))
    return clear_session_cookie(hx_redirect(request, '/login'))


@require_GET
@signed_in
def me_view(request):
    """GET /me: the signed-in user as JSON (for scripts and the kiosk/field clients that check their session)."""
    return JsonResponse({'user': user_json(request.hajiri_user)})


# ---------------------------------------------------------------- change password

@require_http_methods(['GET', 'POST'])
@signed_in
def change_password_view(request):
    user = request.hajiri_user
    json_client = wants_json(request)
    if request.method == 'GET':
        return render(request, 'web/change_password.html', {'form': ChangePasswordForm(), 'forced': user.must_change_password})
    data = _json_body(request) if request.content_type == 'application/json' else request.POST
    if json_client:
        data = {'current_password': data.get('currentPassword', data.get('current_password', '')),
                'new_password': data.get('newPassword', data.get('new_password', ''))}
    form = ChangePasswordForm(data)
    error = None
    if not form.is_valid():
        error = first_error(form)
    elif not json_client and form.cleaned_data['new_password'] != form.cleaned_data.get('new_password_again'):
        # The page asks twice (ChangePasswordPage.tsx); an empty repeat does not match either.
        error = 'The two new passwords do not match. Type the same password in both fields.'
    else:
        error = password_policy_error(form.cleaned_data['new_password'])
    if not error:
        try:
            auth.change_password(user.id, form.cleaned_data['current_password'], form.cleaned_data['new_password'])
        except HttpError as e:
            if json_client:
                raise
            return render(request, 'web/change_password.html', {'form': form, 'forced': user.must_change_password,
                                                                 'error': e.message}, status=e.status)
        if json_client:
            return clear_session_cookie(JsonResponse({'ok': True}))
        resp = clear_session_cookie(HttpResponseRedirect('/login'))
        return toast(request, resp, 'Password changed. Please sign in again.')
    if json_client:
        raise BadRequestError(error)
    return render(request, 'web/change_password.html', {'form': form, 'forced': user.must_change_password, 'error': error},
                  status=400)


# ---------------------------------------------------------------- two-step sign-in

@require_GET
@signed_in
def two_step_setup_page(request):
    """The company requires two-step sign-in for this role: set it up before using Hajiri."""
    if not request.hajiri_user.must_set_up_two_factor:
        return HttpResponseRedirect('/')
    return render(request, 'web/two_step_setup.html', {'done_url': '/'})


def _setup_partial(request, ctx, status=200):
    return render(request, 'web/partials/two_factor_setup.html', ctx, status=status)


@require_POST
@signed_in
def two_factor_setup(request):
    user = request.hajiri_user
    done_url = _done_url(request)
    if user.must_change_password:
        return _setup_partial(request, {'error': 'choose a new password first', 'done_url': done_url}, status=422)
    try:
        s = auth.begin_setup(user)
    except HttpError as e:
        return _setup_partial(request, {'error': e.message, 'done_url': done_url}, status=422)
    grouped = ' '.join(s['secret'][i:i + 4] for i in range(0, len(s['secret']), 4))
    if wants_json(request):
        return JsonResponse({'secret': s['secret'], 'otpauthUri': s['otpauth_uri']})
    return _setup_partial(request, {'secret': s['secret'], 'grouped': grouped, 'uri': s['otpauth_uri'], 'done_url': done_url})


def _done_url(request) -> str:
    return '/' if request.POST.get('context') == 'required' else '/me/profile/security'


@require_POST
@signed_in
def two_factor_enable(request):
    user = request.hajiri_user
    form = EnableForm(request.POST)
    done_url = _done_url(request)
    ctx = {'done_url': done_url, 'secret': request.POST.get('secret', ''), 'uri': request.POST.get('uri', ''),
           'grouped': request.POST.get('grouped', '')}
    if not form.is_valid():
        return _setup_partial(request, {**ctx, 'error': first_error(form)}, status=422)
    try:
        codes = auth.confirm_setup(user, form.cleaned_data['code'])
    except HttpError as e:
        return _setup_partial(request, {**ctx, 'error': e.message}, status=422)
    # The current session has just proved the second factor; sessions opened before it existed may be someone else's.
    if request.session_token:
        Session.objects.filter(token_hash=sha256_hex(request.session_token)).update(two_factor=True)
    auth.revoke_other_sessions(user.id, request.session_token)
    resp = render(request, 'web/partials/recovery_codes.html', {'codes': codes, 'done_url': done_url,
                                                                'done_label': 'I have saved these codes'})
    return toast(request, resp, 'Two-step sign-in is on.')


@require_POST
@signed_in
def two_factor_recovery_codes(request):
    form = CodeForm(request.POST)
    if not form.is_valid():
        return toast(request, HttpResponse(status=400, headers={'HX-Reswap': 'none'}), first_error(form), 'err', 'Not changed')
    try:
        codes = auth.regenerate_recovery_codes(request.hajiri_user, form.cleaned_data['code'])
    except HttpError as e:
        return toast(request, HttpResponse(status=e.status, headers={'HX-Reswap': 'none'}), e.message, 'err', 'Not changed')
    return render(request, 'web/partials/recovery_codes.html', {'codes': codes, 'done_url': '/me/profile/security',
                                                                'done_label': 'Done'})


@require_POST
@signed_in
def two_factor_disable(request):
    form = DisableForm(request.POST)
    if not form.is_valid():
        return toast(request, HttpResponse(status=400, headers={'HX-Reswap': 'none'}), first_error(form), 'err', 'Still on')
    try:
        auth.disable_two_factor(request.hajiri_user, form.cleaned_data['password'], form.cleaned_data['code'])
    except HttpError as e:
        return toast(request, HttpResponse(status=e.status, headers={'HX-Reswap': 'none'}), e.message, 'err', 'Still on')
    return _flash_redirect(request, '/me/profile/security', 'Two-step sign-in is off.')


def _flash_redirect(request, url: str, message: str, tone: str = 'ok') -> HttpResponse:
    """HTMX full-page redirect that shows a toast on the page it lands on."""
    from .htmx import flash
    return flash(hx_redirect(request, url), message, tone)


# ---------------------------------------------------------------- sign-in security page

def _sessions_ctx(request):
    sessions = auth.list_sessions(request.hajiri_user.id, request.session_token)
    return {'sessions': sessions, 'others': sum(1 for s in sessions if not s.current)}


@require_GET
@signed_in
def security_page(request):
    st = auth.two_factor_status(request.hajiri_user)
    return render(request, 'web/security.html', {'st': st, **_sessions_ctx(request)})


@require_POST
@signed_in
def session_revoke(request, session_id):
    import uuid
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise BadRequestError('invalid session id')
    listed = {s.id: s for s in auth.list_sessions(request.hajiri_user.id, request.session_token)}
    try:
        auth.revoke_session(request.hajiri_user.id, str(session_id), request.session_token)
    except HttpError as e:
        if wants_json(request) or not is_htmx(request):
            raise
        return toast(request, HttpResponse(e.message, status=e.status, headers={'HX-Reswap': 'none'}), e.message, 'err',
                     'Not signed out')
    if wants_json(request):
        return JsonResponse({'ok': True})
    device = listed[str(session_id)].device if str(session_id) in listed else 'that session'
    resp = render(request, 'web/partials/sessions.html', _sessions_ctx(request))
    return toast(request, resp, f'Signed out {device}.')


@require_POST
@signed_in
def sessions_revoke_others(request):
    n = auth.revoke_other_sessions(request.hajiri_user.id, request.session_token)
    if wants_json(request):
        return JsonResponse({'revoked': n})
    resp = render(request, 'web/partials/sessions.html', _sessions_ctx(request))
    msg = f'Signed out {n} other {"session" if n == 1 else "sessions"}.' if n else 'No other sessions.'
    return toast(request, resp, msg)


# ---------------------------------------------------------------- employee search (EmployeePicker)

READ_ALL = ('payroll_admin', 'attendance_admin')


def search_employees(user: auth.AuthUser, q: str = '', status: str | None = 'active', limit: int = 20) -> list[dict]:
    """Same scope as GET /employees: HR and the read-all roles company-wide, managers the people they manage, and self."""
    if not auth.has_role(user, 'hr', 'manager', *READ_ALL):
        raise ForbiddenError(f'requires role {" or ".join(("hr", "manager", *READ_ALL))}')
    qs = Employee.objects.for_company(auth.company_id_of(user))
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(emp_code__icontains=q) | Q(name_en__icontains=q) | Q(name_ne__icontains=q))
    if not auth.has_role(user, 'hr', *READ_ALL):
        qs = qs.filter(Q(pk=user.employee_id) | auth.manages(user.employee_id)) if user.employee_id else qs.none()
    return list(qs.order_by('name_en', 'emp_code').values('id', 'emp_code', 'name_en', 'status')[:limit])


@require_GET
@login_required
def employee_search(request):
    q = (request.GET.get('q') or '').strip()[:100]
    status = request.GET.get('status', 'active')
    status = None if status == 'all' else status if status in ('active', 'exited') else 'active'
    found = search_employees(request.hajiri_user, q, status)
    if wants_json(request):
        return JsonResponse({'employees': found}, json_dumps_params={'default': str})
    return render(request, 'components/employee_options.html', {'employees': found, 'q': q})


# ---------------------------------------------------------------- BS calendar data for the date picker

@require_GET
def bs_calendar_js(request):
    """The BS month table as a small script the date picker reads (cached; it only changes with a release)."""
    import json

    from engines.bs_calendar import ANCHOR_AD, ANCHOR_BS, BS_MONTH_DAYS, BS_MONTH_NAMES_EN, PROJECTED_BS_YEARS
    data = {'anchorAd': ANCHOR_AD.isoformat(), 'anchorBs': list(ANCHOR_BS),
            'months': {str(y): list(d) for y, d in BS_MONTH_DAYS.items()},
            'names': list(BS_MONTH_NAMES_EN), 'projected': sorted(PROJECTED_BS_YEARS)}
    body = f'window.HJ_BS={json.dumps(data, separators=(",", ":"))};'
    resp = HttpResponse(body, content_type='text/javascript; charset=utf-8')
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


# ---------------------------------------------------------------- PWA files at the site root

_ROOT_FILES = {
    'manifest.webmanifest': 'application/manifest+json',
    'sw.js': 'text/javascript; charset=utf-8',
    'favicon.svg': 'image/svg+xml',
    'icon-192.png': 'image/png',
    'icon-512.png': 'image/png',
    'apple-touch-icon.png': 'image/png',
}


@require_GET
def root_file(request, name):
    if name not in _ROOT_FILES:
        raise Http404()
    path = STATIC_DIR / ('pwa' if name in ('manifest.webmanifest', 'sw.js') else '') / name
    if not path.exists():
        raise Http404()
    resp = FileResponse(open(path, 'rb'), content_type=_ROOT_FILES[name])
    if name == 'sw.js':
        resp['Cache-Control'] = 'no-cache'
        resp['Service-Worker-Allowed'] = '/'
    else:
        resp['Cache-Control'] = 'public, max-age=86400'
    return resp


@require_GET
def health(request):
    try:
        Session.objects.exists()            # one round trip to the database
        return JsonResponse({'ok': True, 'db': 'up'})
    except Exception:  # noqa: BLE001
        return JsonResponse({'ok': False, 'db': 'down'}, status=503)


# ---------------------------------------------------------------- component gallery

@require_GET
@login_required
def components_demo(request):
    if request.GET.get('partial') == 'modal':
        return render(request, 'web/partials/demo_modal.html')
    from .demo import demo_context
    ctx = demo_context(request)
    if request.GET.get('export') == 'chart':
        from .csv import csv_response
        t = ctx['chart_table']
        cols = [{'key': str(i), 'header': h} for i, h in enumerate(t['columns'])]
        return csv_response('payroll-cost', cols, [{str(i): v for i, v in enumerate(r)} for r in t['rows']])
    from .tables import table_response
    return table_response(request, ctx['table'], 'web/components.html', ctx)


@require_POST
@login_required
def components_demo_action(request):
    """Echo endpoint for the gallery's HTMX buttons: toasts in each tone, a 422 form re-render, an error."""
    kind = request.POST.get('kind', 'ok')
    if kind == 'error':
        raise BadRequestError('This is what an error from the server looks like.')
    if kind == 'form':
        name = (request.POST.get('name') or '').strip()
        if len(name) < 2:
            return render(request, 'web/partials/demo_form.html', {'error': 'Name: at least 2 characters', 'name': name}, status=422)
        resp = render(request, 'web/partials/demo_form.html', {'saved': name})
        return toast(request, resp, f'Saved {name}.')
    if kind == 'modal':
        resp = HttpResponse(status=204)
        trigger(resp, 'hj:close-modal', True)
        return toast(request, resp, 'Modal action done.')
    tone = kind if kind in ('ok', 'info', 'err', 'warn') else 'ok'
    return toast(request, HttpResponse(status=204), f'A {tone} toast from the server.', tone, {'ok': 'Saved', 'info': 'For your information', 'err': 'Not saved', 'warn': 'Heads up'}[tone])


@require_GET
def bs_grid_partial(request):
    """Not used by the picker (it draws client-side); kept for server-rendered month calendars in other slices."""
    try:
        y, m = int(request.GET.get('year')), int(request.GET.get('month'))
        cells = fmt.bs_month_grid(y, m)
    except (TypeError, ValueError):
        raise BadRequestError('year and month are required')
    return render(request, 'components/bs_month_grid.html', {'cells': cells, 'year': y, 'month': m,
                                                             'today': fmt.today_npt()})
