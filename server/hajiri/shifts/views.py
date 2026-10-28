"""
Shifts (/settings/shifts), holidays (/settings/holidays) and the weekly roster with shift swaps (/attendance/rosters),
rebuilt from ShiftsPage.tsx, HolidaysPage.tsx and field/RostersPage.tsx. Fragments and form posts live under
/_/shifts/...; every rule is in services.py.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from engines.bs_calendar import BS_MONTH_NAMES_EN, BsRangeError, bs_month_bounds
from hajiri.core.auth import MODULE_ROLES, company_id_of
from hajiri.db.models import ShiftSwap
from hajiri.web.decorators import login_required, require_module, require_role
from hajiri.web.format import fmt_date_short, fmt_gregorian, minutes_to_hm, safe_ad_to_bs, today_npt
from hajiri.web.htmx import close_modal, htmx_target, refresh, toast
from hajiri.web.tables import Column, Table, table_response

from . import services
from .forms import AssignForm, HolidayForm, HolidayImportForm, RosterCellForm, ShiftForm

WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
SwapStatus = ShiftSwap.Status
SWAP_LABEL = {SwapStatus.AWAITING_COLLEAGUE: 'Waiting for colleague', SwapStatus.PENDING: 'Waiting for approval',
              SwapStatus.APPROVED: 'Approved', SwapStatus.REJECTED: 'Not approved', SwapStatus.DECLINED: 'Declined',
              SwapStatus.CANCELLED: 'Cancelled'}
SWAP_TONE = {SwapStatus.APPROVED: 'ok', SwapStatus.PENDING: 'info'}


def _done(request, message: str | None, *refreshes: str, close: bool = False) -> HttpResponse:
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message) if message else resp


def _weekday(iso: str) -> int:
    return (date.fromisoformat(iso).weekday() + 1) % 7


def _add_days(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


# ================================================================ shifts

def _hours(s: dict):
    extra = mark_safe(' <span class="hj-small hj-muted">next day</span>') if s['crosses_midnight'] else ''
    sub = f"{s['grace_in_min']} min grace" + (f", {s['break_minutes']} min break" if s['break_minutes'] > 0 else '')
    return format_html('<div class="hj-sm num hj-strong">{} – {}{}</div><div class="hj-small hj-muted">{}</div>',
                       s['start'][:5], s['end'][:5], extra, sub)


def _name(s: dict):
    return format_html('<button type="button" class="hj-link-btn hj-strong" hx-get="{}" hx-target="#hj-drawer-body">{}</button>'
                       '<div class="hj-small hj-muted mono">{}</div>', reverse('shifts:edit', args=[s['id']]), s['name_en'], s['code'])


SHIFT_COLUMNS = [
    Column('name_en', 'Shift', render=_name, csv=lambda s: s['name_en']),
    Column('start', 'Hours', render=_hours, csv=lambda s: f"{s['start'][:5]}-{s['end'][:5]}"),
    Column('thr', 'Half day / absent under', muted=True,
           render=lambda s: f"{minutes_to_hm(s['half_day_threshold_min'])} / {minutes_to_hm(s['absent_threshold_min'])}", csv=False),
    Column('assigned_employees', 'Employees', align='right', format='num'),
    Column('id', '', template='shifts/partials/shift_actions.html', csv=False, align='right', width='132px'),
]


@require_GET
@require_role('hr')
def shift_list(request):
    rows = services.list_shifts(company_id_of(request.hajiri_user))
    for s in rows:
        n = s['assigned_employees']
        s['delete_title'] = f"Delete {s['name_en']}?"
        s['delete_message'] = (f'{n} employee(s) are on this shift. Move them to another shift first, otherwise the delete is refused.'
                               if n > 0 else 'Nobody is on this shift. Past attendance keeps its data.')
    table = Table(request, rows, SHIFT_COLUMNS, id='shifts', page_size=None,
                  empty_title='No shifts yet', empty_hint='Create your first shift. Most companies start with a general 10:00 – 18:00 shift.')
    return table_response(request, table, 'shifts/list.html', {'table': table})


def _shift_form(request, form, shift=None, status=200):
    return render(request, 'shifts/partials/shift_form.html', {'form': form, 'shift': shift}, status=status)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def shift_new(request):
    if request.method == 'GET':
        return _shift_form(request, ShiftForm(initial=ShiftForm.BLANK))
    form = ShiftForm(request.POST)
    if not form.is_valid():
        return _shift_form(request, form, status=422)
    services.create_shift(request.hajiri_user, form.body())
    return _done(request, 'Shift saved.', 'shifts', close=True)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def shift_edit(request, id):
    shift = services.get_shift(request.hajiri_user, str(id))
    if request.method == 'GET':
        return _shift_form(request, ShiftForm(initial=ShiftForm.initial_from(shift)), shift)
    form = ShiftForm(request.POST)
    if not form.is_valid():
        return _shift_form(request, form, shift, status=422)
    services.update_shift(request.hajiri_user, str(id), form.body())
    return _done(request, 'Shift saved.', 'shifts', close=True)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def shift_assign(request, id):
    shift = services.get_shift(request.hajiri_user, str(id))
    if request.method == 'GET':
        return render(request, 'shifts/partials/assign_form.html', {'shift': shift, 'from_date': today_npt()})
    form = AssignForm(request.POST)
    if not form.is_valid():
        return render(request, 'shifts/partials/assign_form.html',
                      {'shift': shift, 'form': form, 'from_date': request.POST.get('from_date') or '',
                       'employee_id': request.POST.get('employee_id') or ''}, status=422)
    services.assign_shift(request.hajiri_user, {'employeeId': str(form.cleaned_data['employee_id']), 'shiftId': str(id),
                                                'from': form.cleaned_data['from_date'].isoformat()})
    return _done(request, 'Shift assigned.', 'shifts', close=True)


@require_POST
@login_required
def shift_delete(request, id):
    services.delete_shift(request.hajiri_user, str(id))
    return _done(request, 'Shift deleted.', 'shifts')


# ================================================================ holidays

def _bs_year_span(year: int) -> dict:
    """{from, to}: the AD dates of the BS year (1 Baishakh to the last of Chaitra); {} outside the BS table."""
    try:
        return {'from': bs_month_bounds(year, 1)['startAd'], 'to': bs_month_bounds(year, 12)['endAd']}
    except BsRangeError:
        return {}


def _holidays_ctx(request) -> dict:
    user = request.hajiri_user
    today = today_npt()
    cur = safe_ad_to_bs(today)
    try:
        year = int(request.GET.get('year') or (cur.year if cur else 2083))
    except ValueError:
        year = cur.year if cur else 2083
    in_year = []
    for h in services.holidays(user, _bs_year_span(year)):
        iso = h['local_date'].isoformat() if isinstance(h['local_date'], date) else str(h['local_date'])
        b = safe_ad_to_bs(iso)
        if not b or b.year != year:
            continue
        in_year.append({**h, 'id': str(h['id']), 'iso': iso, 'bs': b, 'weekday': WEEKDAY[_weekday(iso)],
                        'gregorian': fmt_gregorian(iso), 'past': iso < today, 'delete_title': f"Delete {h['name_en']}?"})
    in_year.sort(key=lambda h: h['iso'])
    months: dict[int, list] = {}
    for h in in_year:
        months.setdefault(h['bs'].month, []).append(h)
    n = len(in_year)
    observed = sum(1 for h in in_year if not h['optional'])
    return {'year': year, 'prev_year': year - 1, 'next_year': year + 1,
            'months': [{'label': f'{BS_MONTH_NAMES_EN[m - 1]} {year}', 'count': len(v), 'items': v} for m, v in sorted(months.items())],
            'empty_title': f'No holidays for {year} yet',
            'import_attrs': format_html('hx-post="{}" hx-vals=\'{{"bs_year": "2083"}}\' hx-swap="none"', reverse('shifts:holiday_import')),
            'summary': f"{n} {'holiday' if n == 1 else 'holidays'}, {observed} observed by everyone", 'total': n,
            'empty_hint': ('Import the government list for 2083, then remove the days your company does not observe.' if year == 2083
                           else 'Add the days your company observes this year.')}


@require_GET
@require_role('hr')
def holidays_page(request):
    ctx = _holidays_ctx(request)
    if htmx_target(request) == 'holidays':
        return render(request, 'shifts/partials/holidays.html', ctx)
    return render(request, 'shifts/holidays.html', ctx)


def _holiday_form(request, form, holiday=None, status=200):
    return render(request, 'shifts/partials/holiday_form.html', {'form': form, 'holiday': holiday}, status=status)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def holiday_new(request):
    if request.method == 'GET':
        return _holiday_form(request, HolidayForm(initial={'local_date': today_npt(), 'gender_scope': 'all', 'optional': False}))
    form = HolidayForm(request.POST)
    if not form.is_valid():
        return _holiday_form(request, form, status=422)
    services.create_holiday(request.hajiri_user, form.body())
    return _done(request, 'Holiday saved. Affected days are being recalculated.', 'holidays', close=True)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def holiday_edit(request, id):
    h = services.get_holiday(request.hajiri_user, str(id))
    if request.method == 'GET':
        return _holiday_form(request, HolidayForm(initial={'local_date': h['local_date'], 'name_en': h['name_en'], 'name_ne': h['name_ne'] or '',
                                                           'gender_scope': h['gender_scope'], 'optional': h['optional']}), h)
    form = HolidayForm(request.POST)
    if not form.is_valid():
        return _holiday_form(request, form, h, status=422)
    services.update_holiday(request.hajiri_user, str(id), form.body())
    return _done(request, 'Holiday saved. Affected days are being recalculated.', 'holidays', close=True)


@require_POST
@login_required
def holiday_delete(request, id):
    services.delete_holiday(request.hajiri_user, str(id))
    return _done(request, None, 'holidays')


@require_POST
@login_required
def holiday_import(request):
    r = services.import_default_holidays(request.hajiri_user, HolidayImportForm(request.POST).body())
    return _done(request, f"{r['inserted']} of {r['total']} public holidays added.", 'holidays')


# ================================================================ roster

ROSTER_ROLES = ('manager', *MODULE_ROLES['attendance'])


def _week_start(request) -> str:
    raw = request.GET.get('week') or request.POST.get('week') or ''
    try:
        d = date.fromisoformat(raw).isoformat()
    except ValueError:
        d = today_npt()
    return _add_days(d, -_weekday(d))


def _grid_ctx(request, week: str) -> dict:
    user = request.hajiri_user
    week_end = _add_days(week, 6)
    today = today_npt()
    days = [_add_days(week, i) for i in range(7)]
    shift_list = services.list_shifts(user.company_id)
    by_id = {s['id']: s for s in shift_list}
    data = services.roster(user, {'from': week, 'to': week_end})
    cells = {(c['employee_id'], c['local_date']): c['shift_id'] for c in data['roster']}
    options = [{'value': s['id'], 'label': f"{s['code']} {s['start'][:5]}–{s['end'][:5]}"} for s in shift_list]
    employees = []
    for e in data['employees']:
        default = by_id.get(e['default_shift_id']) if e['default_shift_id'] else None
        row = []
        for d in days:
            has = (e['id'], d) in cells
            value = (cells[(e['id'], d)] or 'off') if has else ''
            normal = 'Weekly off' if _weekday(d) in e['weekly_off_days'] else (f"Normal ({default['code']})" if default else 'Normal')
            row.append({'date': d, 'value': value, 'has': has, 'normal': normal})
        employees.append({**e, 'cells': row})
    return {'week': week, 'week_end': week_end, 'options': options, 'employees': employees,
            'days': [{'iso': d, 'weekday': WEEKDAY[_weekday(d)], 'label': fmt_date_short(d), 'today': d == today} for d in days]}


@require_GET
@require_role(*ROSTER_ROLES)
def rosters(request):
    week = _week_start(request)
    user = request.hajiri_user
    swaps = services.team_swaps(user, week, _add_days(week, 34))
    for s in swaps:
        iso = s['local_date'].isoformat() if isinstance(s['local_date'], date) else str(s['local_date'])
        s['day'] = fmt_date_short(iso)
        s['label'] = SWAP_LABEL.get(s['status'], s['status'])
        s['tone'] = SWAP_TONE.get(s['status'], 'idle')
        s['tip'] = f"Decided {fmt_date_short(s['decided_at'])}" if s.get('decided_at') else 'Open'
    ctx = _grid_ctx(request, week)
    ctx.update({'swaps': swaps, 'prev_week': _add_days(week, -7), 'next_week': _add_days(week, 7),
                'week_label': f"{fmt_date_short(week)} to {fmt_date_short(ctx['week_end'])}",
                'copy_message': (f'Copy the roster of {fmt_date_short(_add_days(week, -7))} to {fmt_date_short(_add_days(week, -1))} '
                                 'onto this week? Cells already set this week are overwritten.'),
                'publish_message': (f"Tell everyone on this roster about their shifts from {fmt_date_short(week)} to "
                                    f"{fmt_date_short(ctx['week_end'])}?")})
    return render(request, 'shifts/rosters.html', ctx)


@require_GET
@require_role(*ROSTER_ROLES)
def roster_grid(request):
    return render(request, 'shifts/partials/roster_grid.html', _grid_ctx(request, _week_start(request)))


@require_POST
@login_required
def roster_cell(request):
    """One cell changed: '' follows the normal shift (the row is removed), 'off' rosters the person off, else a shift id."""
    user = request.hajiri_user
    form = RosterCellForm(request.POST)
    if form.is_clear():
        services.clear_roster(user, form.clear_body())
    else:
        services.save_roster(user, form.save_body())
    return render(request, 'shifts/partials/roster_grid.html', _grid_ctx(request, _week_start(request)))


@require_POST
@login_required
def roster_copy_week(request):
    r = services.copy_week(request.hajiri_user, {'weekStart': _week_start(request)})
    n = r['copied']
    return _done(request, f"{n} {'cell' if n == 1 else 'cells'} copied.", 'roster-grid')


@require_POST
@login_required
def roster_publish(request):
    week = _week_start(request)
    r = services.publish_roster(request.hajiri_user, {'from': week, 'to': _add_days(week, 6)})
    n = r['notified']
    return _done(request, f"Roster published. {n} {'person was' if n == 1 else 'people were'} notified.")
