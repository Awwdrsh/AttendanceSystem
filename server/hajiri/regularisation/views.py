"""
Attendance corrections on the web: the Fix a punch page (/attendance/fix, FixPunchPage.tsx), the correction request
form (MyAttendancePage.tsx CorrectionForm) and the work-from-home request (HomePage.tsx) as HTMX fragments other pages
embed, a request list with Cancel, the offline clock-in flush (lib/offlinePunch.ts) and the JSON API
(/api/v1/regularisation) the TS clients used.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.http import api_view, json_response, read_json
from hajiri.web.decorators import login_required, require_role
from hajiri.web.htmx import close_modal, refresh, toast, trigger
from hajiri.web.nav import area
from hajiri.web.templatetags.hj import status_label

from . import services
from .forms import CorrectionForm, FixPunchForm, WfhForm

FILED_EVENT = 'hj:regularisation-filed'


def _today() -> date:
    return date.fromisoformat(services.npt_today())


# ================================================================ Fix a punch (/attendance/fix)

def _fix_ctx(request, form) -> dict:
    is_hr = has_role(request.hajiri_user, 'hr')
    return {'form': form, 'is_hr': is_hr, 'today': _today(),
            'subtitle': ('Add a missed clock-in or clock-out. HR entries apply immediately and are written to the audit log.'
                         if is_hr else
                         'Add a missed clock-in or clock-out. It is sent as an attendance correction and approved through the normal chain.')}


@require_GET
@require_role('manager', *area('attendance'))
def fix_punch(request):
    form = FixPunchForm(initial={'local_date': _today(), 'time': '18:00', 'direction': 'out'})
    return render(request, 'regularisation/fix.html', _fix_ctx(request, form))


@require_POST
@require_role('manager', *area('attendance'))
def fix_punch_submit(request):
    form = FixPunchForm(request.POST)
    if not form.is_valid():
        return render(request, 'regularisation/partials/fix_form.html', _fix_ctx(request, form), status=422)
    d = form.cleaned_data
    user = request.hajiri_user
    local_date = d['local_date'].isoformat()
    if has_role(user, 'hr'):
        r = services.manual_punch(user, {'employeeId': str(d['employee_id']), 'localDate': local_date, 'time': d['time'],
                                         'direction': d['direction'], 'reason': d['reason']})
        status = getattr(r.get('day'), 'status', None)
        message = ('That punch already existed.' if r['duplicate'] else
                   f"Punch saved. The day is now {status_label(status).lower() if status else 'recalculated'}.")
    else:
        services.create_request(user, {'employeeId': str(d['employee_id']), 'localDate': local_date, 'kind': 'missed_punch',
                                       'punchTime': f"{local_date}T{d['time']}:00+05:45", 'punchDirection': d['direction'],
                                       'reason': d['reason']})
        message = 'Correction request sent for approval.'
    # The page keeps who, when and which punch, and clears the reason (setReason('')).
    fresh = FixPunchForm(initial={'employee_id': d['employee_id'], 'local_date': d['local_date'], 'time': d['time'],
                                  'direction': d['direction']})
    resp = render(request, 'regularisation/partials/fix_form.html', _fix_ctx(request, fresh))
    trigger(resp, FILED_EVENT, True)
    return toast(request, resp, message)


# ================================================================ correction request (CorrectionForm)

def _correction_ctx(request, form, employee_id: str | None, local_date: str | None) -> dict:
    user = request.hajiri_user
    return {'form': form, 'employee_id': employee_id or '', 'fixed_date': local_date or '', 'today': _today(),
            'shifts': services.shift_options(company_id_of(user)),
            'chain_employee': employee_id or (user.employee_id or '')}


@require_http_methods(['GET', 'POST'])
@login_required
def correction_new(request):
    """GET ?employee_id=&date= answers the form (in a modal or inline); POST files the request."""
    src = request.POST if request.method == 'POST' else request.GET
    employee_id = src.get('employee_id') or None
    fixed_date = src.get('date') or None
    if request.method == 'GET':
        form = CorrectionForm(initial={'employee_id': employee_id, 'local_date': fixed_date, 'kind': 'missed_punch',
                                       'direction': 'out', 'time': '18:00'})
        return render(request, 'regularisation/partials/correction_form.html',
                      _correction_ctx(request, form, employee_id, fixed_date))
    data = request.POST.copy()
    if fixed_date and not data.get('local_date'):
        data['local_date'] = fixed_date
    form = CorrectionForm(data)
    if not form.is_valid():
        return render(request, 'regularisation/partials/correction_form.html',
                      _correction_ctx(request, form, employee_id, fixed_date), status=422)
    d = form.cleaned_data
    local_date = d['local_date'].isoformat()
    body = {'localDate': local_date, 'kind': d['kind'], 'reason': d['reason']}
    if d.get('employee_id'):
        body['employeeId'] = str(d['employee_id'])
    if d['kind'] == 'missed_punch':
        body.update({'punchTime': f"{local_date}T{d['time']}:00+05:45", 'punchDirection': d['direction']})
    if d['kind'] == 'shift_change' and d.get('shift_id'):
        body['shiftId'] = str(d['shift_id'])
    services.create_request(request.hajiri_user, body)
    resp = HttpResponse('')                    # the form goes away (onDone)
    close_modal(resp)
    refresh(resp, 'regularisation-requests')
    trigger(resp, FILED_EVENT, True)
    return toast(request, resp, 'Correction request sent. Your manager will see it in their inbox.')


# ================================================================ work from home (HomePage.tsx)

@require_http_methods(['GET', 'POST'])
@login_required
def wfh_new(request):
    today = _today()
    ctx = {'today': today, 'max': today + timedelta(days=60)}
    if request.method == 'GET':
        return render(request, 'regularisation/partials/wfh_form.html', {**ctx, 'form': WfhForm(initial={'local_date': today})})
    form = WfhForm(request.POST)
    if not form.is_valid():
        return render(request, 'regularisation/partials/wfh_form.html', {**ctx, 'form': form}, status=422)
    services.create_request(request.hajiri_user, {'localDate': form.cleaned_data['local_date'].isoformat(), 'kind': 'wfh',
                                                  'reason': form.cleaned_data['reason']})
    resp = HttpResponse('')
    close_modal(resp)
    refresh(resp, 'regularisation-requests')
    trigger(resp, FILED_EVENT, True)
    return toast(request, resp, 'Request sent to your manager.')


# ================================================================ request list and cancel

KIND_LABEL = {'missed_punch': 'Missed punch', 'wfh': 'Work from home', 'on_duty': 'On duty', 'half_day': 'Half day',
              'shift_change': 'Shift change'}


@require_GET
@login_required
def request_list(request):
    """The user's correction requests (own, team or all as GET /regularisation decides), with Cancel while pending."""
    q = {k: request.GET.get(k) for k in ('employee_id', 'status', 'from', 'to', 'scope')}
    items = services.list_requests(request.hajiri_user, {'employeeId': q['employee_id'], 'status': q['status'],
                                                         'from': q['from'], 'to': q['to'], 'scope': q['scope']})
    for r in items:
        r['kind_label'] = KIND_LABEL.get(r['kind'], r['kind'])
    return render(request, 'regularisation/partials/requests.html',
                  {'items': items, 'query': request.GET.urlencode(), 'show_employee': any(
                      str(r['employee_id']) != str(request.hajiri_user.employee_id) for r in items)})


@require_POST
@login_required
def cancel(request, id):
    services.cancel_request(request.hajiri_user, str(id))
    resp = HttpResponse(status=204)
    refresh(resp, 'regularisation-requests')
    return toast(request, resp, 'Request cancelled.')


# ================================================================ offline clock-ins (app.js -> data-offline-flush-url)

@api_view(methods=('POST',))
def offline_flush(request):
    """One clock-in queued while offline: 201 when filed, 4xx {"error"} to drop it with that message."""
    return json_response(services.flush_offline_punch(request.hajiri_user, read_json(request)), status=201)


# ================================================================ JSON (/api/v1/regularisation, the TS routes)

@api_view(methods=('GET', 'POST'))
def api_requests(request):
    if request.method == 'POST':
        return json_response(services.create_request(request.hajiri_user, read_json(request)), status=201)
    return {'requests': services.list_requests(request.hajiri_user, request.GET.dict())}


@api_view(methods=('POST',))
def api_cancel(request, id):
    return services.cancel_request(request.hajiri_user, id)

