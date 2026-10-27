"""
Leave pages: Time off (/me/leave), the Leave hub (/leave team calendar, /leave/balances, /leave/policies), their HTMX
fragments under /_/leave/..., and the JSON API under /api/v1/leave/... (documented in docs/api.md for Bearer-token
integrations, same paths and shapes as the TS API).
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, HttpError, NotFoundError, ValidationError, error_body
from hajiri.core.http import api_view, json_response, read_json
from hajiri.core.schema import is_uuid, parse_uuid
from hajiri.web import format as fmt
from hajiri.web.decorators import login_required, require_module, require_role
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast, trigger
from hajiri.web.tables import Column, Table, table_response

from . import services
from .forms import adjust_body, apply_body, preview_query, type_body

LEAVE_HUB_ROLES = ('manager', 'hr', 'attendance_admin')

CITATION = {
    'HOME': 'Labour Act 2074 Sec 43, 49: 1 day per 20 worked, cap 90', 'SICK': 'Sec 44: 12/yr, cap 45, certificate over 3 days',
    'MATERNITY': 'Sec 45: 14 weeks, 60 paid days', 'PATERNITY': 'Sec 45(7): 15 days', 'MOURNING': 'Sec 48: 13 days',
    'SUBSTITUTE': 'Sec 42: within 21 days', 'SPECIAL_UNPAID': 'Sec 80(3)',
}


def _days(n) -> str:
    """fmtDays: "2 days", "1 day", "1.5 days"."""
    f = float(n)
    text = str(int(f)) if f.is_integer() else f'{f:.1f}'
    return f"{text} {'day' if f == 1 else 'days'}"


def _num_text(v) -> str:
    """Number(x) as JavaScript prints it."""
    return services.js(float(v)) if v is not None else ''


def _date_range(frm, to) -> str:
    """LeavePage dateRange: one date, or "12 Bhadra to 14 Bhadra 2083"."""
    return fmt.fmt_date(frm) if fmt.iso_of(frm) == fmt.iso_of(to) else f'{fmt.fmt_date_short(frm)} to {fmt.fmt_date(to)}'


def _half_label(h) -> str:
    return 'first half' if h == 'first_half' else 'second half'


def _holidays_for(company_id: str) -> dict[str, str]:
    """
    Holidays 60 days back to 120 ahead for the date pickers. LeaveApplyForm read GET /shifts/calendar, which lists every
    holiday of the company in the window (all branches, gender scopes and optional ones), names joined with ', '.
    """
    today = fmt.today_npt()
    hol = rows("""SELECT local_date, name_en FROM holiday WHERE company_id = %s AND local_date BETWEEN %s::date - 60 AND %s::date + 120
                   ORDER BY local_date, name_en""", [company_id, today, today])
    out: dict[str, list[str]] = {}
    for h in hol:
        out.setdefault(fmt.iso_of(h['local_date']), []).append(h['name_en'])
    return {d: ', '.join(names) for d, names in out.items()}


# ================================================================ Time off (/me/leave)

def _request_dates(r) -> str:
    text = format_html('{}', _date_range(r['from_date'], r['to_date']))
    if r.get('half_day'):
        text = format_html('{}<span class="hj-sm hj-muted"> ({})</span>', text, _half_label(r['half_day']))
    if r.get('from_time'):
        text = format_html('{}<span class="hj-sm hj-muted num"> {}–{}</span>', text, r['from_time'], r['to_time'])
    return text


def _request_days(r):
    hours = format_html('<span class="hj-small hj-muted"> ({} h)</span>', _num_text(r['hours'])) if r.get('hours') else ''
    return format_html('<span class="num">{}{}</span>', _num_text(r['days_requested']), hours)


def _type_cell(r):
    return format_html('<button type="button" class="hj-link-btn hj-strong" hx-get="{}" hx-target="#hj-modal-body">{}</button>',
                       reverse('leave:request_detail', args=[r['id']]), r['type_name'])


def _my_actions(r):
    if not (r['status'] == 'pending' or (r['status'] == 'approved' and fmt.iso_of(r['from_date']) > fmt.today_npt())):
        return ''
    return format_html('<button class="hj-btn danger subtle xs" hx-post="{}" hx-swap="none">Cancel request</button>',
                       reverse('leave:cancel', args=[r['id']]))


MY_COLUMNS = [
    Column('type_name', 'Type', render=_type_cell, csv=lambda r: r['type_name']),
    Column('from_date', 'Dates', render=_request_dates, csv=lambda r: fmt.iso_of(r['from_date'])),
    Column('days_requested', 'Days', align='right', render=_request_days, csv=lambda r: r['days_requested']),
    Column('status', 'Status', format='status'),
    Column('actions', '', render=_my_actions, csv=False, align='right'),
]


def _my_stats(balances: list[dict]) -> list[dict]:
    shown = [b for b in balances if b['paid'] or b['balance'] != 0]
    return [{'label': b['nameEn'], 'value': services.fixed(b['available'], 1),
             'hint': f"days available, {services.js(b['pending'])} pending" if b['pending'] > 0 else 'days available'} for b in shown]


@require_GET
@login_required
def my_leave(request):
    user = request.hajiri_user
    company_id_of(user)
    reqs = services.list_requests(user, {'scope': 'own'}) if user.employee_id else []
    table = Table(request, reqs, MY_COLUMNS, id='my-leave', page_size=25, empty_title='No time off requested yet',
                  empty_hint='Use Request time off to ask for leave. Your manager is notified straight away.')
    if table.is_partial or table.wants_csv:
        return table_response(request, table, 'leave/mine.html')
    bal = services.get_balances(user)['balances'] if user.employee_id else []
    return table_response(request, table, 'leave/mine.html', {'table': table, 'stats': _my_stats(bal),
                                                               'has_employee': bool(user.employee_id)})


@require_GET
@login_required
def my_balances(request):
    """The balance strip, reloaded after a request or a cancellation."""
    user = request.hajiri_user
    bal = services.get_balances(user)['balances'] if user.employee_id else []
    return render(request, 'leave/partials/my_balances.html', {'stats': _my_stats(bal)})


@require_GET
@login_required
def request_detail(request, id):
    r = services.get_request(request.hajiri_user, str(id))
    days = _num_text(r['days_requested'])
    if r['half_day']:
        days += f" ({_half_label(r['half_day'])})"
    if r['from_time_hm']:
        days += f" ({r['from_time_hm']}–{r['to_time_hm']}, {_num_text(r['hours'])} h)"
    return render(request, 'leave/partials/request_detail.html', {
        'r': r, 'dates': _date_range(r['from_date'], r['to_date']), 'days': days,
        'decided_title': 'Waiting for your manager' if r['status'] == 'pending' else r['status'].capitalize(),
    })


@require_POST
@login_required
def cancel(request, id):
    """Cancel a request (the employee, their manager or HR). Used by Time off and by Request history."""
    reason = (request.POST.get('reason') or '').strip()
    services.cancel_request(request.hajiri_user, str(id), {'reason': reason} if reason else {})
    resp = HttpResponse(status=204)
    for target in ('my-leave', 'my-balances', 'requests', 'leave-balances'):
        refresh(resp, target)
    trigger(resp, 'hj:leave-changed')
    return toast(request, resp, 'Request cancelled.')


# ================================================================ apply (drawer on Time off, inline on the employee page)

def _apply_ctx(request, employee_id: str, inline: bool, values: dict | None = None, suggestion: dict | None = None) -> dict:
    user = request.hajiri_user
    types = services.list_types(user)
    bal = {b['leaveTypeId']: b for b in services.get_balances(user, employee_id)['balances']}
    v = values or {}
    today = fmt.today_npt()
    return {
        'employee_id': employee_id, 'inline': inline,
        'types': [{'id': t['id'], 'label': t['name_en'] + (f" ({services.fixed(bal[t['id']]['available'], 1)} days left)" if t['id'] in bal else ''),
                   'allow_hourly': t['allow_hourly']} for t in types],
        'hourly_ids': [t['id'] for t in types if t['allow_hourly']],
        'v': {'leave_type_id': v.get('leave_type_id', ''), 'from': v.get('from') or today, 'to': v.get('to') or v.get('from') or today,
              'half': v.get('half') == 'on', 'half_which': v.get('half_which') or 'first_half', 'hourly': v.get('hourly') == 'on',
              'from_time': v.get('from_time') or '10:00', 'to_time': v.get('to_time') or '12:00',
              'reason': v.get('reason', ''), 'attachment': v.get('attachment', '')},
        'holidays': _holidays_for(user.company_id),
        'suggestion': suggestion,
    }


@require_http_methods(['GET', 'POST'])
@login_required
def apply(request):
    user = request.hajiri_user
    company_id_of(user)
    employee_id = request.GET.get('employee_id') or request.POST.get('employee_id') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    if not is_uuid(employee_id):
        raise NotFoundError('employee not found')
    inline = (request.GET.get('inline') or request.POST.get('inline')) == '1'
    emp = services.get_employee_lite(employee_id)
    if not emp or emp['company_id'] != user.company_id:
        raise NotFoundError('employee not found')
    services.assert_manages(user, employee_id, 'attendance')
    if request.method == 'GET':
        return render(request, 'leave/partials/apply.html', _apply_ctx(request, employee_id, inline))
    try:
        r = services.create_request(user, apply_body(request.POST, employee_id))
    except services.LeaveSuggestionError as e:
        ctx = _apply_ctx(request, employee_id, inline, request.POST.dict(),
                         {'msg': e.message, 'paidDays': e.suggestion.get('paidDays'),
                          'lwpDays': e.suggestion.get('lwpDays', e.suggestion.get('remainingDays'))})
        return render(request, 'leave/partials/apply.html', ctx, status=422)
    except HttpError as e:
        resp = HttpResponse(status=204)
        resp['HX-Reswap'] = 'none'
        return toast(request, resp, _err_text(e), tone='err', title='Request not sent')
    msg = r['explanation'] + (f" {', '.join(r['teamOverlap'])} are also off; your manager will see this." if r['teamOverlap'] else '')
    if inline:
        resp = render(request, 'leave/partials/apply.html', _apply_ctx(request, employee_id, True))
    else:
        resp = HttpResponse(status=204)
        close_modal(resp)
    for target in ('my-leave', 'my-balances', 'leave-balances'):
        refresh(resp, target)
    trigger(resp, 'hj:leave-changed')
    return toast(request, resp, msg, title='Time off requested')


def _err_text(e: HttpError) -> str:
    if isinstance(e, ValidationError):
        return '; '.join(f"{i['path']}: {i['message']}" if i.get('path') else i['message'] for i in e.issues)
    return e.message


@require_GET
@login_required
def preview(request):
    """The deduction line under the apply form; nothing when the inputs are incomplete or not allowed (TS: setPreview(null))."""
    user = request.hajiri_user
    q = preview_query(request.GET)
    if not q:
        return HttpResponse('')
    try:
        p = services.preview(user, q)
    except HttpError:
        return HttpResponse('')
    if p.get('hours'):
        head = f"{services.js(p['hours'])} {'hour' if p['hours'] == 1 else 'hours'} ({services.js(p['days'])} of a day) will be deducted"
    else:
        head = f"{_days(p['days'])} will be deducted"
    bridge = p.get('bridge')
    bridge_text = f"Also, {bridge['explanation'][:1].lower()}{bridge['explanation'][1:]}." if bridge else None
    return render(request, 'leave/partials/preview.html', {
        'p': p, 'head': head, 'bridge_text': bridge_text, 'tone': 'warn' if p['balanceAfter'] < 0 else 'info',
        'after': services.fixed(p['balanceAfter'], 1), 'available': services.fixed(p['available'], 1)})


# ================================================================ Leave hub: team calendar (/leave)

OFF_TYPE_LWP = 'LWP'


@require_GET
@require_role(*LEAVE_HUB_ROLES)
def calendar(request):
    user = request.hajiri_user
    company = company_id_of(user)
    today = fmt.today_npt()
    now_bs = fmt.safe(today)
    try:
        y = int(request.GET.get('y') or (now_bs.year if now_bs else 2083))
        m = int(request.GET.get('m') or (now_bs.month if now_bs else 1))
        if not (1 <= m <= 12) or not fmt.in_range(y):
            raise ValueError
    except ValueError:
        y, m = (now_bs.year, now_bs.month) if now_bs else (2083, 1)
    view = 'list' if request.GET.get('view') == 'list' else 'grid'
    cells = fmt.bs_month_grid(y, m)
    start, end = cells[0].ad, cells[-1].ad
    off_map = {d['date']: d['off'] for d in services.team_calendar(user, start, end)}
    hol: dict[str, list[str]] = {}
    for h in rows("""SELECT local_date, name_en FROM holiday WHERE company_id = %s AND local_date BETWEEN %s AND %s
                      ORDER BY local_date, name_en""", [company, start, end]):
        hol.setdefault(fmt.iso_of(h['local_date']), []).append(h['name_en'])
    in_month = [c for c in cells if c.in_month]
    grid = []
    for i, c in enumerate(cells):
        off = off_map.get(c.ad, [])
        ad_day = int(c.ad[8:10])
        grid.append({
            'c': c, 'i': i, 'off': [_chip(o) for o in off[:3]], 'more': len(off) - 3 if len(off) > 3 else 0,
            'more_names': ', '.join(o['nameEn'] for o in off[3:]), 'holidays': hol.get(c.ad, []), 'today': c.ad == today,
            'ad_label': f"{fmt.AD_MONTHS[int(c.ad[5:7]) - 1]} {ad_day}" if ad_day == 1 or i == 0 else str(ad_day),
        })
    list_days = [{'date': c.ad, 'today': c.ad == today, 'holidays': hol.get(c.ad, []),
                  'off': off_map.get(c.ad, [])} for c in in_month if off_map.get(c.ad) or hol.get(c.ad)]
    prev, nxt = fmt.prev_month(y, m), fmt.next_month(y, m)
    return render(request, 'leave/calendar.html', {
        'y': y, 'm': m, 'view': view, 'grid': grid, 'list_days': list_days, 'weekdays': ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'),
        'span': f'{fmt.ad_span_label(in_month[0].ad, in_month[-1].ad)}, {len(in_month)} days' if in_month else '',
        'prev': prev, 'next': nxt, 'now': {'year': now_bs.year, 'month': now_bs.month} if now_bs else {'year': y, 'month': m},
    })


def _chip(o: dict) -> dict:
    pending = o['status'] == 'pending'
    return {**o, 'pending': pending, 'first': o['nameEn'].split(' ')[0], 'color': 'var(--hj-idle)' if o['type'] == OFF_TYPE_LWP else 'var(--hj-info)',
            'tip': f"{o['nameEn']}, {o['type']}{' (half day)' if o.get('halfDay') else ''}{', pending' if pending else ''}"}


# ================================================================ Leave hub: balances and adjustments (/leave/balances)

def _ledger_days(r):
    v = float(r['delta_days'])
    cls = 'hj-bad-text' if v < 0 else 'hj-ok-text' if v > 0 else ''
    return format_html('<span class="num hj-strong {}">{}{}</span>', cls, '+' if v > 0 else '', r['delta_days'])


LEDGER_COLUMNS = [
    Column('effective_date', 'Date', format='bs_date', sortable=True),
    Column('code', 'Leave', mono=True),
    Column('delta_days', 'Days', align='right', render=_ledger_days, csv=lambda r: r['delta_days']),
    Column('reason', 'Reason'),
    Column('note', 'Note', muted=True),
]


def _balances_ctx(request, employee_id: str | None) -> dict:
    user = request.hajiri_user
    ctx: dict = {'employee_id': employee_id or '', 'types': services.list_types(user), 'today': fmt.today_npt(),
                 'kinds': [('adjustment', 'Adjustment'), ('allocation', 'Allocation'), ('carry_forward', 'Carry forward')]}
    if employee_id:
        ctx['balances'] = services.get_balances(user, employee_id)['balances']
        for b in ctx['balances']:
            b['balance_text'] = services.fixed(b['balance'], 2)
            b['available_text'] = services.fixed(b['available'], 2)
        ctx['ledger'] = Table(request, services.ledger(user, employee_id), LEDGER_COLUMNS, id='leave-ledger', page_size=25,
                              default_sort='-effective_date', empty_title='No ledger entries yet',
                              empty_hint='Entries appear here when leave is allocated, taken or adjusted.',
                              extra_params={'employee_id': employee_id})
    return ctx


@require_GET
@require_module('attendance')
def balances(request):
    employee_id = request.GET.get('employee_id') or None
    if employee_id and not is_uuid(employee_id):
        employee_id = None
    ctx = _balances_ctx(request, employee_id)
    if employee_id and ctx['ledger'].wants_csv:
        return ctx['ledger'].csv_response()
    if employee_id and ctx['ledger'].is_partial:
        return render(request, 'components/data_table.html', {'table': ctx['ledger']})
    if is_htmx(request) and request.headers.get('HX-Target') == 'leave-balances':
        return render(request, 'leave/partials/balances_panel.html', ctx)
    return render(request, 'leave/balances.html', ctx)


@require_POST
@require_module('attendance')
def adjust(request):
    r = services.adjust(request.hajiri_user, adjust_body(request.POST))
    resp = HttpResponse(status=204)
    refresh(resp, 'leave-balances')
    return toast(request, resp, f"Adjustment posted. New balance is {services.fixed(r['balance'], 2)} days.")


@require_POST
@require_module('attendance')
def accrual_run(request):
    r = services.run_accrual(request.hajiri_user)
    resp = HttpResponse(status=204)
    refresh(resp, 'leave-balances')
    return toast(request, resp, f"Accrual catch-up done: {r['postings']} posting(s).")


# ================================================================ Leave hub: policies (/leave/policies)

def _accrual_text(t) -> str:
    days = services.js(float(t['accrual_days']))
    if t['accrual_basis'] == 'worked_days':
        return f"{days} day per {t['accrual_per_worked_days']} worked"
    if t['accrual_basis'] == 'monthly':
        return f'{days} days a month'
    if t['accrual_basis'] == 'annual':
        return f'{days} days a year'
    return 'Allocated manually'


@require_GET
@require_module('attendance')
def policies(request):
    types = services.list_types(request.hajiri_user)
    names = {t['id']: t['name_en'] for t in types}
    for i, t in enumerate(types):
        t['first'], t['last'] = i == 0, i == len(types) - 1
        t['club_name'] = names.get(t['club_with_id'])
        t['accrual_text'] = _accrual_text(t)
        t['cite'] = CITATION.get(t['code'], 'Company policy')
    ctx = {'types': types}
    if is_htmx(request) and request.headers.get('HX-Target') == 'leave-types':
        return render(request, 'leave/partials/types_table.html', ctx)
    return render(request, 'leave/policies.html', ctx)


def _type_form_ctx(request, t: dict | None, values: dict | None = None) -> dict:
    types = services.list_types(request.hajiri_user)
    tid = t['id'] if t else None
    has_followers = bool(tid) and any(x['club_with_id'] == tid for x in types)
    club_options = [] if has_followers else [x for x in types if x['id'] != tid and not x['club_with_id']]
    if t:
        e = {'code': t['code'], 'nameEn': t['name_en'], 'nameNe': t['name_ne'] or '', 'paid': t['paid'],
             'accrualBasis': t['accrual_basis'], 'accrualDays': services.js(float(t['accrual_days'])),
             'accrualPerWorkedDays': t['accrual_per_worked_days'] if t['accrual_per_worked_days'] is not None else '',
             'maxBalance': services.js(float(t['max_balance'])) if t['max_balance'] is not None else '',
             'carryForward': t['carry_forward'], 'encashable': t['encashable'],
             'certificateAfterDays': t['certificate_after_days'] if t['certificate_after_days'] is not None else '',
             'allowHalfDay': t['allow_half_day'], 'allowHourly': bool(t['allow_hourly']),
             'maxConsecutiveDays': t['max_consecutive_days'] if t['max_consecutive_days'] is not None else '',
             'minServiceDays': t['min_service_days'], 'negativeBalanceLimit': services.js(float(t['negative_balance_limit'])),
             'genderScope': t['gender_scope'], 'sandwichRule': t['sandwich_rule'],
             'expiryDays': t['expiry_days'] if t['expiry_days'] is not None else '', 'clubWithId': t['club_with_id'] or ''}
    else:
        e = {'code': '', 'nameEn': '', 'nameNe': '', 'paid': True, 'accrualBasis': 'annual', 'accrualDays': '0',
             'accrualPerWorkedDays': '', 'maxBalance': '', 'carryForward': False, 'encashable': False, 'certificateAfterDays': '',
             'allowHalfDay': True, 'allowHourly': False, 'maxConsecutiveDays': '', 'minServiceDays': 0, 'negativeBalanceLimit': '0',
             'genderScope': 'all', 'sandwichRule': False, 'expiryDays': '', 'clubWithId': ''}
    return {'t': t, 'e': e, 'locked': bool(t and t['statutory']), 'citation': CITATION.get(t['code'], 'Set by the Labour Act') if t else '',
            'club_options': club_options,
            'bases': [('none', 'None (allocated manually)'), ('worked_days', 'Per worked days'), ('monthly', 'Monthly'),
                      ('annual', 'Annual (on Shrawan 1)')],
            'genders': [('all', 'Everyone'), ('female', 'Women'), ('male', 'Men')]}


def _type_saved(request) -> HttpResponse:
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, 'leave-types')
    return toast(request, resp, 'Leave policy saved.')


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def type_new(request):
    if request.method == 'GET':
        return render(request, 'leave/partials/type_form.html', _type_form_ctx(request, None))
    services.create_type(request.hajiri_user, type_body(request.POST, locked=False, partial=False))
    return _type_saved(request)


def _own_type(request, id) -> dict:
    t = one('SELECT * FROM leave_type WHERE id = %s AND company_id = %s', [str(id), request.hajiri_user.company_id])
    if not t:
        raise NotFoundError('leave type not found')
    return services._type_row(t)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def type_edit(request, id):
    t = _own_type(request, id)
    if request.method == 'GET':
        return render(request, 'leave/partials/type_form.html', _type_form_ctx(request, t))
    services.update_type(request.hajiri_user, t['id'], type_body(request.POST, locked=t['statutory'], partial=True))
    return _type_saved(request)


@require_POST
@require_role('hr')
def type_move(request, id):
    """Move a type one place up or down in the leave order (the TS page posted the whole order)."""
    types = services.list_types(request.hajiri_user)
    ids = [t['id'] for t in types]
    if str(id) not in ids:
        raise NotFoundError('leave type not found')
    i = ids.index(str(id))
    j = i + (-1 if request.POST.get('dir') == 'up' else 1)
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        services.reorder_types(request.hajiri_user, ids)
    resp = HttpResponse(status=204)
    return refresh(resp, 'leave-types')


# ================================================================ JSON API (/api/v1/leave, TS paths and shapes)

def _api_error(e: HttpError):
    body = error_body(e)
    if isinstance(e, services.LeaveSuggestionError):
        body['suggestion'] = e.suggestion
    return json_response(body, status=e.status)


def _api(methods):
    def deco(fn):
        @api_view(methods=methods)
        def view(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except services.LeaveSuggestionError as e:
                return _api_error(e)
        view.__name__ = fn.__name__
        return view
    return deco


def _module_gate(request, area='attendance'):
    services._require_module(request.hajiri_user, area)


@_api(('GET', 'POST'))
def api_types(request):
    if request.method == 'GET':
        return {'types': services.list_types(request.hajiri_user)}
    _module_gate(request)
    return json_response({'id': services.create_type(request.hajiri_user, read_json(request))}, status=201)


@_api(('PATCH',))
def api_type(request, id):
    _module_gate(request)
    services.update_type(request.hajiri_user, parse_uuid(id), read_json(request))
    return {'ok': True}


@_api(('POST',))
def api_types_order(request):
    from hajiri.core.schema import Arr, Obj, Uuid, parse
    if not services.has_role(request.hajiri_user, 'hr'):
        from hajiri.core.errors import ForbiddenError
        raise ForbiddenError('requires role hr')
    b = parse(Obj({'ids': Arr(Uuid(), min=1, max=100)}), read_json(request))
    services.reorder_types(request.hajiri_user, b['ids'])
    return {'ok': True}


def _query(request, keys) -> dict:
    return {k: request.GET[k] for k in keys if k in request.GET}


@_api(('GET',))
def api_balances(request):
    from hajiri.core.schema import IsoDate, Obj, Uuid, parse
    q = parse(Obj({'employeeId': Uuid(optional=True), 'asOf': IsoDate(optional=True)}), _query(request, ('employeeId', 'asOf')))
    return services.get_balances(request.hajiri_user, q.get('employeeId'), q.get('asOf'))


@_api(('GET',))
def api_preview(request):
    return services.preview(request.hajiri_user, _query(request, ('employeeId', 'leaveTypeId', 'from', 'to', 'halfDay', 'fromTime', 'toTime')))


@_api(('GET', 'POST'))
def api_requests(request):
    if request.method == 'GET':
        return {'requests': services.list_requests(request.hajiri_user, _query(request, ('employeeId', 'status', 'from', 'to', 'scope')))}
    return json_response(services.create_request(request.hajiri_user, read_json(request)), status=201)


@_api(('POST',))
def api_cancel(request, id):
    services.cancel_request(request.hajiri_user, parse_uuid(id), read_json(request) or {})
    return {'ok': True}


@_api(('POST',))
def api_accrual_run(request):
    _module_gate(request)
    return services.run_accrual(request.hajiri_user)


@_api(('POST',))
def api_adjust(request):
    _module_gate(request)
    return json_response(services.adjust(request.hajiri_user, read_json(request)), status=201)


@_api(('GET',))
def api_ledger(request):
    from hajiri.core.schema import Obj, Uuid, parse
    q = parse(Obj({'employeeId': Uuid(optional=True), 'leaveTypeId': Uuid(optional=True)}), _query(request, ('employeeId', 'leaveTypeId')))
    return {'ledger': services.ledger(request.hajiri_user, q.get('employeeId'), q.get('leaveTypeId'))}


@_api(('GET',))
def api_team_calendar(request):
    q = _query(request, ('from', 'to'))
    return {'days': services.team_calendar(request.hajiri_user, q.get('from'), q.get('to'))}

