"""
Overtime pages: the Overtime review tab of the Attendance hub (/attendance/overtime, OtSlipsPage.tsx) with its approve,
reject and comp-off actions and the register CSV; overtime pre-approval request fragments (request, list, cancel)
under /_/ot/...; and the JSON API under /api/v1/ot/... with the TS paths and shapes for Bearer-token integrations.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_module, has_role
from hajiri.core.db import one
from hajiri.core.errors import NotFoundError
from hajiri.core.http import api_view, json_response, read_json
from hajiri.web import format as fmt
from hajiri.web.decorators import login_required, require_module, require_role
from hajiri.web.htmx import close_modal, refresh, toast
from hajiri.web.tables import Column, Table, table_response

from . import services
from .forms import ApproveSlipForm, OtRequestForm, RejectSlipForm

STATUS_OPTS = [('detected', 'Needs review'), ('approved', 'Approved'), ('rejected', 'Rejected'),
               ('posted', 'Posted to payroll'), ('comp_off', 'Comp-off')]
REQUEST_STATUS_OPTS = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')]


def day_type_label(t: str | None) -> str:
    """OtSlipsPage dayType: 'weekly_off' -> 'Weekly off', 'publicHoliday' -> 'Public holiday'."""
    import re
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', (t or '').replace('_', ' ')).lower()
    return s[:1].upper() + s[1:]


def _name_cell(r):
    return format_html('<div class="hj-strong hj-sm">{}</div><div class="hj-small hj-muted">{}</div>', r['name_en'], r['emp_code'])


def _date_cell(r):
    return format_html('<div class="hj-sm" style="white-space:nowrap">{}</div><div class="hj-small hj-muted">{}</div>',
                       fmt.fmt_date_long(r['local_date']), day_type_label(r['day_type']))


def _num(text):
    return format_html('<span class="num">{}</span>', text)


SLIP_COLUMNS = [
    Column('name_en', 'Employee', render=_name_cell, csv=False),
    Column('local_date', 'Date', render=_date_cell),
    Column('detected_min', 'Detected', align='right', render=lambda r: _num(fmt.minutes_to_hm(r['detected_min']))),
    Column('approved_min', 'Approved', align='right',
           render=lambda r: _num('—' if r['approved_min'] is None else fmt.minutes_to_hm(r['approved_min']))),
    Column('status', 'Status', format='status'),
    Column('id', '', align='right', template='ot/partials/slip_actions.html', csv=False),
]


def _slip_filters(request) -> dict:
    """From 30 days back to today, status "Needs review" until the user picks another (empty means any status)."""
    g = request.GET
    today = fmt.today_npt()
    frm = g.get('from', fmt.add_days(today, -30))
    to = g.get('to', today)
    status = g.get('status', 'detected')
    return {'from': frm or '', 'to': to or '', 'status': status if status in dict(STATUS_OPTS) else ''}


@require_GET
@require_module('attendance')
def overtime(request):
    """OtSlipsPage: overtime detected from punches, for review."""
    f = _slip_filters(request)
    query = {k: v for k, v in (('from', f['from']), ('to', f['to']), ('status', f['status'])) if v}
    slips = services.list_slips(request.hajiri_user, query)
    waiting = f['status'] == 'detected'
    table = Table(request, slips, SLIP_COLUMNS, id='ot-slips', page_size=50,
                  empty_title='No overtime waiting for review' if waiting else 'No overtime in this range',
                  empty_hint='Overtime detected from punches shows up here for approval.' if waiting else 'Try a wider date range or another status.',
                  extra_params={'from': f['from'], 'to': f['to'], 'status': f['status']})
    return table_response(request, table, 'ot/overtime.html', {
        'table': table, 'f': f, 'status_opts': STATUS_OPTS, 'count': len(slips),
    })


@require_GET
@require_module('attendance')
def register_csv(request):
    """Export CSV: the inspector-ready OT register for the chosen range (otApi.registerCsv)."""
    _, data = services.register(request.hajiri_user, {'from': request.GET.get('from', ''), 'to': request.GET.get('to', ''), 'format': 'csv'})
    resp = HttpResponse(services.register_csv(data), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="ot-register-{request.GET["from"]}_{request.GET["to"]}.csv"'
    return resp


def _slip_row(user, slip_id) -> dict:
    s = one("""SELECT s.id, s.employee_id, s.local_date, s.detected_min, s.approved_min, s.status, e.name_en
                 FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE s.id = %s AND e.company_id = %s""",
            [str(slip_id), company_id_of(user)])
    if not s:
        raise NotFoundError('OT slip not found')
    return s


def _done(request, message: str) -> HttpResponse:
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, 'ot-slips')
    return toast(request, resp, message)


@require_http_methods(['GET', 'POST'])
@require_role(*services.SLIP_REVIEWERS)
def slip_approve(request, id):
    """Approve overtime modal: minutes capped at the detected minutes and the 4 h daily cap."""
    s = _slip_row(request.hajiri_user, id)
    cap = min(services.CAP_DAY_MIN, s['detected_min'])
    if request.method == 'GET':
        return render(request, 'ot/partials/approve.html', {'s': s, 'cap': cap, 'value': s['detected_min']})
    form = ApproveSlipForm(request.POST)
    if not form.is_valid():
        return render(request, 'ot/partials/approve.html', {'s': s, 'cap': cap, 'value': request.POST.get('approved_min'),
                                                            'form': form}, status=422)
    services.approve_slip(request.hajiri_user, str(id), form.body())
    return _done(request, 'Overtime approved.')


@require_http_methods(['GET', 'POST'])
@require_role(*services.SLIP_REVIEWERS)
def slip_reject(request, id):
    """Reject overtime modal: the employee sees the reason."""
    s = _slip_row(request.hajiri_user, id)
    if request.method == 'GET':
        return render(request, 'ot/partials/reject.html', {'s': s})
    form = RejectSlipForm(request.POST)
    if not form.is_valid():
        return render(request, 'ot/partials/reject.html', {'s': s, 'form': form, 'reason': request.POST.get('reason', '')}, status=422)
    services.reject_slip(request.hajiri_user, str(id), form.body())
    return _done(request, 'Overtime rejected.')


@require_POST
@require_role(*services.SLIP_REVIEWERS)
def slip_comp_off(request, id):
    """Give comp-off: substitute leave instead of pay."""
    r = services.comp_off_slip(request.hajiri_user, str(id))
    days = r['days']
    return _done(request, f"Credited {days} {'day' if float(days) == 1 else 'days'} of comp-off, expires {fmt.fmt_date(r['expiresOn'])}.")


# ---------------------------------------------------------------- pre-approval requests (fragments)

REQUEST_COLUMNS = [
    Column('name_en', 'Employee', render=_name_cell, csv=False),
    Column('local_date', 'Date', format='bs_date_short'),
    Column('requested_min', 'Requested', align='right', render=lambda r: _num(fmt.minutes_to_hm(r['requested_min']))),
    Column('approved_min', 'Approved', align='right',
           render=lambda r: _num('—' if r['approved_min'] is None else fmt.minutes_to_hm(r['approved_min']))),
    Column('status', 'Status', format='status'),
    Column('id', '', align='right', template='ot/partials/request_actions.html', csv=False),
]


@require_GET
@login_required
def requests_list(request):
    """Overtime requests in the user's scope (own, team or all, as GET /ot/requests), for embedding with hx-get."""
    g = request.GET
    status = g.get('status') if g.get('status') in dict(REQUEST_STATUS_OPTS) else None
    query = {k: v for k, v in (('status', status), ('scope', g.get('scope')), ('employeeId', g.get('employee_id'))) if v}
    data = services.list_requests(request.hajiri_user, query)
    word = f'{dict(REQUEST_STATUS_OPTS)[status].lower()} ' if status else ''
    table = Table(request, data, REQUEST_COLUMNS, id='ot-requests', page_size=25,
                  empty_title=f'No {word}overtime requests', empty_hint='Try another status filter.',
                  extra_params={k: v for k, v in (('status', status), ('scope', g.get('scope')), ('employee_id', g.get('employee_id'))) if v})
    return render(request, 'components/data_table.html', {'table': table})


def _may_pick_employee(user) -> bool:
    return has_role(user, 'manager') or has_module(user, 'attendance')


@require_http_methods(['GET', 'POST'])
@login_required
def request_new(request):
    """Ask for overtime pre-approval (for yourself, or for a report / anyone as manager or HR)."""
    user = request.hajiri_user
    ctx = {'pick': _may_pick_employee(user), 'today': fmt.today_npt(), 'cap': services.CAP_DAY_MIN}
    if request.method == 'GET':
        return render(request, 'ot/partials/request_form.html', {**ctx, 'form': OtRequestForm()})
    form = OtRequestForm(request.POST)
    if not form.is_valid():
        return render(request, 'ot/partials/request_form.html', {**ctx, 'form': form}, status=422)
    services.create_request(user, form.body())
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, 'ot-requests')
    return toast(request, resp, 'Overtime request sent for approval.')


@require_POST
@login_required
def request_cancel(request, id):
    services.cancel_request(request.hajiri_user, str(id))
    return toast(request, refresh(HttpResponse(status=204), 'ot-requests'), 'Overtime request cancelled.')


# ================================================================ JSON API (/api/v1/ot, TS paths and shapes)

def _q(request, keys) -> dict:
    return {k: request.GET[k] for k in keys if k in request.GET}


@api_view(methods=('GET', 'POST'))
def api_requests(request):
    if request.method == 'GET':
        return {'requests': services.list_requests(request.hajiri_user, _q(request, ('employeeId', 'status', 'from', 'to', 'scope')))}
    return json_response(services.create_request(request.hajiri_user, read_json(request)), status=201)


@api_view(methods=('POST',))
def api_request_cancel(request, id):
    return services.cancel_request(request.hajiri_user, id)


@api_view(methods=('GET',))
def api_slips(request):
    return {'slips': services.list_slips(request.hajiri_user, _q(request, ('employeeId', 'from', 'to', 'status')))}


@api_view(methods=('POST',))
def api_slip_approve(request, id):
    return services.approve_slip(request.hajiri_user, id, read_json(request) or {})


@api_view(methods=('POST',))
def api_slip_reject(request, id):
    return services.reject_slip(request.hajiri_user, id, read_json(request))


@api_view(methods=('POST',))
def api_slip_comp_off(request, id):
    return services.comp_off_slip(request.hajiri_user, id)


@api_view(methods=('GET',))
def api_register(request):
    format_, data = services.register(request.hajiri_user, _q(request, ('from', 'to', 'format')))
    if format_ == 'csv':
        return HttpResponse(services.register_csv(data), content_type='text/csv; charset=utf-8')
    return {'register': data}
