"""
Pages of the home slice: Home (/), Notifications, and the Settings hub tabs Company & rules, Users & roles, Features and
Setup checklist. Each view parses the form, calls the owning module's service and renders, as the React page did.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import ROLES, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, HttpError, ValidationError
from hajiri.core.scoping import get_or_404
from hajiri.db.models import AppUser
from hajiri.web.decorators import login_required, require_role
from hajiri.web.format import fmt_date_long, fmt_time, greeting
from hajiri.web.htmx import close_modal, flash, hx_redirect, is_htmx, refresh, toast, trigger
from hajiri.web.nav import ROLE_HELP, ROLE_LABEL, feature_on
from hajiri.web.tables import Column, Table, table_response

from . import services as s
from .forms import (AttendanceRulesForm, CodeNameForm, FeaturesForm, PunchForm, ResetTwoFactorForm, SecurityForm,
                    UserNewForm, UserUpdateForm, generic_body)


def _url(name: str, *args) -> str | None:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _plain_page(request) -> dict:
    """Home is a page of its own, not a hub tab: its greeting is the title (App.tsx renders HomePage outside any Hub).
    The layout draws a hub header above any hub tab, so the navigation is given without a current tab."""
    from dataclasses import replace

    from hajiri.web import nav
    return {'hj_nav': replace(nav.build(request.hajiri_user, request.path), current_tab=None), 'embedded': False}


def _first_name(user) -> str:
    parts = (user.name or '').strip().split()
    return parts[0] if parts else ''


# ================================================================ Home

def _home_data(user) -> dict:
    from hajiri.dashboard.services import me
    from hajiri.punch.services import today
    from hajiri.shifts.services import shifts
    t = today(user)
    m = me(user)
    card = s.today_card(t, m, shifts(user))
    card['idempotency_key'] = str(uuid.uuid4())
    return {'card': card, 'me': m}


def _requests_ctx(me: dict) -> list[dict]:
    from hajiri.web.format import relative_time
    out = []
    for r in me.get('pendingRequests') or []:
        d = s.describe_request(r)
        out.append({**d, 'raw_detail': r.get('detail'), 'ago': relative_time(r['created_at'])})
    return out


@require_GET
@login_required
def index(request):
    """App.tsx Home: employees land on their own day, HR on the overview, managers on approvals."""
    user = request.hajiri_user
    if not user.employee_id:
        if has_role(user, 'hr'):
            return HttpResponseRedirect('/overview')
        if has_role(user, 'manager'):
            return HttpResponseRedirect('/approvals')
        return render(request, 'home/home.html', {
            **_plain_page(request), 'linked': False, 'title': f"{greeting()}{', ' + _first_name(user) if _first_name(user) else ''}",
            'subtitle': fmt_date_long(npt_today().isoformat())})
    data = _home_data(user)
    card, me = data['card'], data['me']
    month = me.get('month')
    ctx = {
        **_plain_page(request), 'linked': True, 'card': card, 'me': me, 'title': f"{greeting()}, {_first_name(user) or 'there'}",
        'subtitle': card['date_label'], 'month': month, 'month_stats': s.month_stats(month) if month else None,
        'balances': [{**b, 'days': f"{float(b['balance']):.1f}"} for b in me.get('leaveBalances') or []],
        'requests': _requests_ctx(me),
        'cards': {
            'relations': _url('relations:home_card'),
            'checklist': _url('lifecycle:my_checklist') if feature_on(request, 'lifecycle') else None,
            'timer': _url('timesheets:home_timer') if feature_on(request, 'time') else None,
            'field': _url('field:home_card') if feature_on(request, 'field') else None,
            'todo': _url('tasks:todo') if feature_on(request, 'tasks') else None,
            'whos_out': _url('attendance_tools:whos_out'),
            'company': _url('engage:home_card'),
        },
        'wfh_url': _url('regularisation:wfh'),
    }
    return render(request, 'home/home.html', ctx)


@require_GET
@login_required
def today_fragment(request):
    """The clock-in card again (after a punch elsewhere on the page, or every 30 s for the worked time)."""
    if not request.hajiri_user.employee_id:
        return HttpResponse('')
    return render(request, 'home/partials/today.html', {'card': _home_data(request.hajiri_user)['card']})


@require_GET
@login_required
def requests_fragment(request):
    """This month, time off balance and pending requests (they change after a punch or a new request)."""
    from hajiri.dashboard.services import me
    if not request.hajiri_user.employee_id:
        return HttpResponse('')
    m = me(request.hajiri_user)
    return render(request, 'home/partials/requests.html', {
        'requests': _requests_ctx(m), 'balances': [{**b, 'days': f"{float(b['balance']):.1f}"} for b in m.get('leaveBalances') or []]})


@require_GET
@login_required
def month_fragment(request):
    from hajiri.dashboard.services import me
    if not request.hajiri_user.employee_id:
        return HttpResponse('')
    month = me(request.hajiri_user).get('month')
    return render(request, 'home/partials/month.html', {'month': month, 'month_stats': s.month_stats(month) if month else None})


def _punch_body(post) -> dict:
    from hajiri.punch.services import utc_now_iso
    return PunchForm(post).body(utc_now_iso())


@require_POST
@login_required
def punch(request):
    """HomePage doPunch: clock in or out from the browser, with location, the work-from-home photo and the rules."""
    from hajiri.punch.services import punch as do_punch
    from hajiri.web.views import client_ip
    user = request.hajiri_user
    body = _punch_body(request.POST)
    direction = body['direction']
    try:
        status, res = do_punch(user, body, device_token=request.headers.get('X-Device-Token'), ip=client_ip(request))
    except HttpError as e:
        code = (e.details or {}).get('code') if isinstance(e.details, dict) else None
        if e.status == 400 and code == 'photo_required':
            # Clocking in from home needs a photo: open the camera and retry with it.
            r = HttpResponse(status=204)
            close_modal(r)
            trigger(r, 'hj:home-camera', {'direction': direction, 'reason': body.get('reason') or ''})
            if not body.get('selfie'):
                toast(request, r, 'You are clocking in from home, so a quick photo is needed.', 'info')
            return r
        if e.status == 400 and 'reason' in e.message.lower() and not isinstance(e, ValidationError):
            r = render(request, 'home/partials/reason.html', {'message': e.message, 'direction': direction,
                                                              'key': body['idempotencyKey']})
            r['HX-Retarget'] = '#hj-modal-body'
            r['HX-Reswap'] = 'innerHTML'
            return r
        if e.status == 409 and code == 'late_blocked':
            r = render(request, 'attendance_tools/partials/late_blocked.html', {'message': e.message})
            r['HX-Retarget'] = '#hj-modal-body'
            r['HX-Reswap'] = 'innerHTML'
            return r
        from hajiri.web.middleware import error_message
        r = HttpResponse(status=e.status)
        r['HX-Reswap'] = 'none'
        return toast(request, r, error_message(e), 'err', 'Clock-in not saved' if direction == 'in' else 'Clock-out not saved')
    p = res['punch']
    resp = render(request, 'home/partials/today.html', {'card': _home_data(user)['card']})
    resp['HX-Retarget'] = '#home-today'
    resp['HX-Reswap'] = 'outerHTML'
    if res.get('duplicate'):
        toast(request, resp, f"Already recorded: {s.direction_label(p.get('direction')).lower()} at {fmt_time(p['punched_at'])}.", 'info')
    else:
        toast(request, resp, res.get('message') or f"{s.direction_label(direction)} at {fmt_time(p['punched_at'])}.")
    if 'lat' not in body:
        toast(request, resp, 'Location was not available, so this was recorded without it.', 'info')
    close_modal(resp)
    trigger(resp, 'hj:home-punched', True)
    return resp


# ================================================================ Notifications

def _notifications_ctx(user) -> dict:
    from hajiri.notifications.service import inbox
    from hajiri.web.format import relative_time
    q = inbox(user.id, limit=50, company_id=user.company_id)
    for n in q['notifications']:
        n['ago'] = relative_time(n['created_at'])
    unread = q['unread']
    return {'items': q['notifications'], 'unread': unread,
            'subtitle': f'{unread} unread' if unread else 'You are all caught up'}


@require_GET
@login_required
def notifications(request):
    ctx = _notifications_ctx(request.hajiri_user)
    if is_htmx(request) and request.headers.get('HX-Target') == 'notifications-list':
        return render(request, 'home/partials/notifications.html', ctx)
    return render(request, 'home/notifications.html', ctx)


# ================================================================ Settings: Company & rules

def _settings_values(user) -> dict:
    from hajiri.company_settings.services import list_settings
    data = list_settings(user)
    return {x['key']: (x['value'] or {}) for x in data['settings']}


def _settings_ctx(request, tab: str, values: dict | None = None) -> dict:
    user = request.hajiri_user
    values = values if values is not None else _settings_values(user)
    ctx = {'tab': tab, 'tabs': s.SETTINGS_TABS}
    if tab == 'attendance':
        p = values.get('attendance.policy') or {}
        ctx.update(p=p, penalty=p.get('latePenalty') or {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5},
                   holiday_work=s.HOLIDAY_WORK)
    elif tab in s.GENERIC:
        key, title, desc, fields = s.GENERIC[tab]
        v = values.get(key) or {}
        ctx.update(section_title=title, section_description=desc, fields=[{**f, 'value': v.get(f['key'])} for f in fields])
    elif tab == 'security':
        roles = (values.get('security.policy') or {}).get('require2faRoles') or []
        ctx.update(roles=s.SECURITY_ROLES, picked=roles, my_roles=json.dumps(list(user.roles)),
                   two_factor=user.two_factor_enabled)
    elif tab == 'time':
        # TimeTrackingSettings renders nothing when its settings cannot load (the 'time' module switched off).
        ctx['time_url'] = _url('timesheets:settings') if feature_on(request, 'time') else None
    elif tab == 'org':
        ctx.update(_org_ctx(user))
    return ctx


def _org_ctx(user) -> dict:
    from hajiri.company_settings.services import list_branches, list_org_units
    return {'branches': list_branches(user), 'units': list_org_units(user)}


@require_GET
@require_role('hr')
def settings_page(request):
    tab = request.GET.get('tab') or 'attendance'
    if tab not in dict(s.SETTINGS_TABS):
        tab = 'attendance'
    ctx = _settings_ctx(request, tab)
    if is_htmx(request) and request.headers.get('HX-Target') == 'settings-panel':
        return render(request, 'home/partials/settings_panel.html', ctx)
    return render(request, 'home/settings.html', ctx)


@require_POST
@require_role('hr')
def settings_attendance(request):
    from hajiri.company_settings.services import put_setting
    user = request.hajiri_user
    stored = _settings_values(user).get('attendance.policy') or {}
    put_setting(user, 'attendance.policy', AttendanceRulesForm(request.POST).body(stored))
    return toast(request, HttpResponse(status=204), 'Attendance rules saved. They apply to future recalculations.')


@require_POST
@require_role('hr')
def settings_preview(request):
    from hajiri.company_settings.services import preview_attendance
    user = request.hajiri_user
    stored = _settings_values(user).get('attendance.policy') or {}
    today = npt_today()
    r = preview_attendance(user, {'policy': AttendanceRulesForm(request.POST).body(stored),
                                  'from': (today - timedelta(days=30)).isoformat(), 'to': today.isoformat()})
    text = f"{r['summary']} {r['affectedEmployees']} of {r['employees']} employees would change."
    return render(request, 'home/partials/preview.html', {'text': text})


@require_POST
@require_role('hr')
def settings_generic(request, tab):
    from hajiri.company_settings.services import put_setting
    if tab not in s.GENERIC:
        raise BadRequestError('unknown settings section')
    key, _title, _desc, fields = s.GENERIC[tab]
    user = request.hajiri_user
    stored = _settings_values(user).get(key) or {}
    put_setting(user, key, generic_body(fields, stored, request.POST))
    return toast(request, HttpResponse(status=204), 'Settings saved.')


@require_POST
@require_role('hr')
def settings_security(request):
    from hajiri.company_settings.services import put_setting
    user = request.hajiri_user
    roles = SecurityForm(request.POST).roles()
    put_setting(user, 'security.policy', {'require2faRoles': roles})
    if s.affects_me(user, roles):
        # The next page asks for two-step set-up before anything else (RequireRole after refresh()).
        r = hx_redirect(request, '/setup-two-step')
        return flash(r, 'Security settings saved.')
    return toast(request, HttpResponse(status=204), 'Security settings saved.')


@require_POST
@require_role('hr')
def settings_branch(request):
    from hajiri.company_settings.services import create_branch
    user = request.hajiri_user
    create_branch(user, CodeNameForm(request.POST).branch())
    return render(request, 'home/partials/org.html', _org_ctx(user))


@require_POST
@require_role('hr')
def settings_unit(request):
    from hajiri.company_settings.services import create_org_unit
    user = request.hajiri_user
    create_org_unit(user, CodeNameForm(request.POST).unit())
    return render(request, 'home/partials/org.html', _org_ctx(user))


# ================================================================ Settings: Users & roles

def _users_table(request) -> Table:
    from hajiri.core.companies import is_group_admin
    from hajiri.core.users import list_users
    from hajiri.db.models import GroupMember
    user = request.hajiri_user
    admin = s.is_admin(user)
    rows = list_users(user)
    # Members who also hold a group role are reset by the group admin only (see _resettable_users).
    group_roles = (set() if not admin or is_group_admin(user) else
                   {str(i) for i in GroupMember.objects.filter(group__companies=user.company_id).values_list('user_id', flat=True)})
    for u in rows:
        u['id'] = str(u['id'])
        u['is_admin_view'] = admin
        u['can_reset'] = admin and not (u.get('membership') and u['id'] in group_roles)
        u['is_me'] = u['id'] == user.id
        u['role_text'] = ', '.join(ROLE_LABEL.get(r, r) for r in u['roles'])
        u['role_options'] = [{'value': r, 'label': ROLE_LABEL[r], 'on': r in u['roles']} for r in ROLES]
        # Group members: their account belongs to another company; roles and status here are this company's only.
        u['from_text'] = f"From {u.get('home_company') or 'another company'}" if u.get('membership') else None
    cols = [
        Column('name', 'Name', template='home/partials/user_name.html', csv=lambda r: r['name']),
        Column('roles', 'Roles', template='home/partials/user_roles.html', width='280px', csv=lambda r: r['role_text']),
        Column('employee_id', 'Employee record', render=lambda r: 'Linked' if r['employee_id'] else 'Not linked', csv=False),
        Column('last_login_at', 'Last sign-in', template='home/partials/user_last.html', csv=False),
        Column('two_factor', 'Two-step', template='home/partials/user_two.html', csv=False),
        Column('status', 'Status', format='status'),
        Column('id', '', template='home/partials/user_actions.html', align='right', csv=False),
    ]
    return Table(request, rows, cols, id='users', empty_title='No users yet',
                 empty_hint='Add a user so they can sign in.' if admin else None, page_size=100)


def _group_access_ctx(user) -> dict:
    """People who work in this company through a group role (managed on the group pages, listed here read-only), and
    whether the company is in a group, so its admin can give people of the other companies access."""
    from hajiri.core.companies import is_group_admin
    from hajiri.core.collation import sorted_by_name
    from hajiri.core.users import group_role_holders
    holders = [{'id': str(uid), 'name': u.name, 'email': u.email,
                'role_text': ', '.join(ROLE_LABEL.get(r, r) for r in roles)}
               for uid, roles, u in group_role_holders(user.company_id)] if user.group_id else []
    return {'in_group': bool(user.group_id), 'group_holders': sorted_by_name(holders, key=lambda r: r['name']),
            'group_admin': is_group_admin(user)}


@require_GET
@require_role('hr')
def users_page(request):
    table = _users_table(request)
    return table_response(request, table, 'home/users.html', {'table': table, 'is_admin': s.is_admin(request.hajiri_user),
                                                              **_group_access_ctx(request.hajiri_user)})


@require_http_methods(['GET', 'POST'])
@require_role('admin')
def user_access_new(request):
    """Modal: give a person from another company of the group access to this company, with roles here (and an
    employee record here). Granting again updates the roles of an existing access."""
    from hajiri.core.companies import grant_access
    from hajiri.web.group import AccessForm
    form = AccessForm(request.POST or None)
    ctx = {'form': form, 'role_help': ROLE_HELP}
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'home/partials/user_access.html', ctx, status=422)
        d = form.cleaned_data
        grant_access(request.hajiri_user, {'email': d['email'], 'roles': d['roles'],
                                           'employee_id': str(d['employee_id']) if d['employee_id'] else None})
        r = HttpResponse(status=204)
        close_modal(r)
        refresh(r, 'users')
        return toast(request, r, 'Access given.')
    return render(request, 'home/partials/user_access.html', ctx)


@require_POST
@require_role('admin')
def user_access_revoke(request, id):
    """Take away a group member's access to this company (their own account and home company are untouched)."""
    from hajiri.core.companies import revoke_access
    revoke_access(request.hajiri_user, str(id))
    return toast(request, refresh(HttpResponse(status=204), 'users'), 'Access removed.')


@require_POST
@require_role('admin')
def user_update(request, id):
    """Roles (from the role picker) or status (Disable / Activate)."""
    from hajiri.core.users import admin_update_user
    body = UserUpdateForm(request.POST).body()
    if 'roles' in body and not body['roles']:
        r = refresh(HttpResponse(status=204), 'users')
        return toast(request, r, 'A user needs at least one role. Disable the user instead.', 'info')
    admin_update_user(request.hajiri_user, str(id), body)
    return refresh(HttpResponse(status=204), 'users')


def _resettable_users(actor):
    """Whose two-step sign-in this company's admin may reset (hajiri.core.auth.resettable_users)."""
    from hajiri.core.auth import resettable_users
    return resettable_users(actor)


@require_http_methods(['GET', 'POST'])
@require_role('admin')
def user_reset(request, id):
    """Reset two-step sign-in for a lost or replaced phone."""
    from hajiri.core.auth import admin_reset_two_factor
    target = get_or_404(_resettable_users(request.hajiri_user).values('id', 'name'), company_id_of(request.hajiri_user),
                        'user not found', pk=id)
    if request.method == 'GET':
        return render(request, 'home/partials/user_reset.html', {'target': target})
    reason = ResetTwoFactorForm(request.POST).reason_text()
    admin_reset_two_factor(request.hajiri_user, str(id), reason)
    r = HttpResponse(status=204)
    close_modal(r)
    refresh(r, 'users')
    return toast(request, r, f"Two-step sign-in reset for {target['name']}.")


def _new_user_ctx(post=None, error=None) -> dict:
    post = post or {}
    picked = post.getlist('roles') if hasattr(post, 'getlist') else ['employee']
    return {'roles': [{'value': r, 'label': ROLE_LABEL[r], 'help': ROLE_HELP[r], 'on': r in picked} for r in ROLES],
            'v': post, 'employee_id': post.get('employee_id') or None, 'error': error,
            'picked_json': json.dumps(picked)}


@require_http_methods(['GET', 'POST'])
@require_role('admin')
def user_new(request):
    from hajiri.core.users import admin_create_user
    if request.method == 'GET':
        return render(request, 'home/partials/user_new.html', _new_user_ctx())
    admin_create_user(request.hajiri_user, UserNewForm(request.POST).body())
    r = HttpResponse(status=204)
    close_modal(r)
    refresh(r, 'users')
    return toast(request, r, 'User added.')


# ================================================================ Settings: Features

def _features_ctx(user) -> dict:
    from hajiri.features.services import get_features
    data = get_features(user)
    groups: dict[str, list] = {}
    for f in data['features']:
        groups.setdefault(f['group'], []).append(f)
    return {'groups': list(groups.items()), 'saved_json': json.dumps(data['enabled'])}


@require_GET
@require_role('hr')
def features_page(request):
    return render(request, 'home/features.html', _features_ctx(request.hajiri_user))


@require_POST
@require_role('hr')
def features_save(request):
    from hajiri.features.services import FEATURE_KEYS, get_features, set_features
    user = request.hajiri_user
    current = get_features(user)['enabled']
    on = FeaturesForm(request.POST).switched_on()
    changed = {k: (k in on) for k in FEATURE_KEYS if (k in on) != current[k]}
    if not changed:
        raise BadRequestError('nothing to save')
    set_features(user, {'enabled': changed})
    # The menu changes with the switches, so the whole page reloads (FeaturesProvider reload()).
    r = hx_redirect(request, reverse('home:features'))
    return flash(r, 'Everyone sees the change the next time they open a page.', title='Features saved')


# ================================================================ Settings: Setup checklist

def _setup_ctx(user) -> dict:
    steps = s.setup_steps(s.setup_counts(user))
    done = sum(1 for x in steps if x['done'])
    return {'steps': steps, 'done': done, 'total': len(steps), 'pct': round(done / len(steps) * 100)}


@require_GET
@require_role('hr')
def setup_page(request):
    ctx = _setup_ctx(request.hajiri_user)
    if is_htmx(request) and request.headers.get('HX-Target') == 'setup-steps':
        return render(request, 'home/partials/setup_steps.html', ctx)
    return render(request, 'home/setup.html', ctx)


@require_POST
@require_role('hr')
def setup_action(request, action):
    from hajiri.shifts.services import create_shift, import_default_holidays
    user = request.hajiri_user
    if action == 'holidays':
        res = import_default_holidays(user, {'bsYear': s.DEFAULT_HOLIDAY_YEAR})
        message = f"{res['inserted']} holidays added."
    elif action == 'shift':
        create_shift(user, dict(s.GENERAL_SHIFT))
        message = 'General shift created.'
    else:
        raise BadRequestError('unknown step')
    r = render(request, 'home/partials/setup_steps.html', _setup_ctx(user))
    return toast(request, r, message)
