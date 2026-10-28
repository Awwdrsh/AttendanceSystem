"""
Attendance corrections on the web: the Fix a punch page (/attendance/fix, FixPunchPage.tsx), the correction request
form (MyAttendancePage.tsx CorrectionForm) and the work-from-home request (HomePage.tsx) as HTMX fragments other pages
embed, and a request list with Cancel. The offline clock-in flush and the JSON API (/api/v1/regularisation) are DRF
views in api.py.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.db.models import RegularisationRequest
from hajiri.web.decorators import login_required, require_role
from hajiri.web.htmx import close_modal, refresh, toast, trigger
from hajiri.web.nav import area
from hajiri.web.templatetags.hj import status_label

from . import services
from .forms import CorrectionForm, FixPunchForm, WfhForm

FILED_EVENT = 'hj:regularisation-filed'


def _today() -> date:
    return npt_today()


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
    if has_role(user, 'hr'):
        r = services.manual_punch(user, form.manual_body())
        status = getattr(r.get('day'), 'status', None)
        message = ('That punch already existed.' if r['duplicate'] else
                   f"Punch saved. The day is now {status_label(status).lower() if status else 'recalculated'}.")
    else:
        services.create_request(user, form.request_body())
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
        if employee_id:
            # The form is for one employee: an id of another company is not found before anything is drawn.
            services.assert_employee(request.hajiri_user, employee_id)
        form = CorrectionForm(initial={'employee_id': employee_id, 'local_date': fixed_date, 'kind': RegularisationRequest.Kind.MISSED_PUNCH,
                                       'direction': RegularisationRequest.PunchDirection.OUT, 'time': '18:00'})
        return render(request, 'regularisation/partials/correction_form.html',
                      _correction_ctx(request, form, employee_id, fixed_date))
    data = request.POST.copy()
    if fixed_date and not data.get('local_date'):
        data['local_date'] = fixed_date
    form = CorrectionForm(data)
    if not form.is_valid():
        return render(request, 'regularisation/partials/correction_form.html',
                      _correction_ctx(request, form, employee_id, fixed_date), status=422)
    services.create_request(request.hajiri_user, form.request_body())
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
    services.create_request(request.hajiri_user, form.request_body())
    resp = HttpResponse('')
    close_modal(resp)
    refresh(resp, 'regularisation-requests')
    trigger(resp, FILED_EVENT, True)
    return toast(request, resp, 'Request sent to your manager.')


# ================================================================ request list and cancel

@require_GET
@login_required
def request_list(request):
    """The user's correction requests (own, team or all as GET /regularisation decides), with Cancel while pending."""
    q = {k: request.GET.get(k) for k in ('employee_id', 'status', 'from', 'to', 'scope')}
    items = services.list_requests(request.hajiri_user, {'employeeId': q['employee_id'], 'status': q['status'],
                                                         'from': q['from'], 'to': q['to'], 'scope': q['scope']})
    for r in items:
        r['kind_label'] = RegularisationRequest.Kind(r['kind']).label
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
