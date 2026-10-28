"""
Approvals inbox (/approvals), request history (/leave/history), approval flow set-up (/settings/approvals) and their
HTMX fragments under /_/workflow/... (the JSON endpoints Bearer clients use for one-tap approvals are in api.py).
Every query runs in the user's active company.
"""
from __future__ import annotations

import re

import django_filters
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_module
from hajiri.core.errors import BadRequestError, ForbiddenError, HttpError, NotFoundError, ValidationError
from hajiri.core.validation import plain, validate
from hajiri.db.models import (AppUser, Branch, Designation, Employee, LeaveRequest, OrgUnit, OtRequest, RegularisationRequest,
                              UserGroup)
from hajiri.web.decorators import login_required, require_module, require_role
from hajiri.web.format import fmt_date_short, fmt_money, fmt_time, minutes_to_hm
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast
from hajiri.web.tables import Column, Table, table_response

from . import flow_admin, service
from .cards import card
from .flows import REQUEST_TYPE_BY_KEY, REQUEST_TYPES, kind_of, to_step_input
from .forms import BulkApproveForm, DecisionForm, DelegateForm, FlowForm, GroupForm, steps_from_post  # noqa: F401
from .serializers import BulkApproveIn, DecisionPageIn

KIND_OPTIONS = [('manager', 'Line manager'), ('manager2', "Manager's manager"), ('department_head', 'Department head'),
                ('branch_head', 'Branch head'), ('role', 'Everyone with a role'), ('user', 'A named person'),
                ('group', 'Anyone in a group')]
ROLE_OPTIONS = [('hr', 'HR'), ('payroll_admin', 'Payroll admin'), ('attendance_admin', 'Attendance admin'),
                ('recruiter', 'Recruiter'), ('admin', 'Admin')]
CONDITION_FIELDS = [('orgUnitIds', 'Departments (and below)', 'units', 'Any department'),
                    ('branchIds', 'Branches', 'branches', 'Any branch'),
                    ('designationIds', 'Designations', 'designations', 'Any designation'),
                    ('groupIds', 'User groups', 'groups', 'Any group')]


def _ok(request, message: str, *refreshes: str, close: bool = False) -> HttpResponse:
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message)


def _page_body(serializer, body: dict, *bare: str) -> dict:
    """Validate a page post. Problems with the `bare` fields are shown without the field name ('Too small: expected
    array to have >=1 items', not 'ids: ...'), as these buttons always reported them."""
    try:
        return plain(validate(serializer, body))
    except ValidationError as e:
        for issue in e.issues:
            head, _, rest = str(issue.get('path') or '').partition('.')
            if head in bare:
                issue['path'] = rest
        raise


# ================================================================ approvals inbox

def _inbox_ctx(request) -> dict:
    user = request.hajiri_user
    items = service.inbox_for(user)
    kind = request.GET.get('kind') or 'all'
    kinds = list(dict.fromkeys(i['objectType'] for i in items))
    if kind != 'all' and kind not in kinds:
        kind = 'all'
    shown = items if kind == 'all' else [i for i in items if i['objectType'] == kind]
    for i in shown:
        i['card'] = card(i)
        i['name'] = (i['employee'] or {}).get('nameEn') or 'Unknown employee'
    from .cards import TYPE_LABEL
    pills = [{'value': 'all', 'label': f'All ({len(items)})'}] + [
        {'value': k, 'label': f"{TYPE_LABEL.get(k, k)} ({sum(1 for i in items if i['objectType'] == k)})"} for k in kinds]
    n = len(items)
    subtitle = (f"{n} {'request is' if n == 1 else 'requests are'} waiting for your decision." if n
                else 'Leave, attendance correction and overtime requests that need your decision.')
    return {'items': items, 'shown': shown, 'kind': kind, 'pills': pills if len(kinds) > 1 else [], 'subtitle': subtitle,
            'bulk_message': f'{n} requests will be approved in one go. Reject any that need it first.', 'bulk_label': f'Approve {n}',
            'delegation': flow_admin.active_delegation(user) if user.company_id else None,
            # Group of companies: approvals waiting in the person's other companies (switch company to decide them).
            'elsewhere': [{**c, 'label': f"{c['name']} ({c['count']})"} for c in service.inbox_elsewhere(user)]
            if user.multi_company else []}


@require_GET
@login_required
def approvals(request):
    ctx = _inbox_ctx(request)
    if is_htmx(request) and request.headers.get('HX-Target') == 'approvals':
        return render(request, 'workflow/partials/inbox.html', ctx)
    return render(request, 'workflow/approvals.html', ctx)


@require_GET
@login_required
def inbox_partial(request):
    return render(request, 'workflow/partials/inbox.html', _inbox_ctx(request))


@require_GET
@login_required
def detail(request, id):
    d = service.detail(request.hajiri_user, str(id))
    inst = d['instance']
    return render(request, 'workflow/partials/detail.html', {
        'inst': inst, 'chain': ', then '.join(str(s.get('role')) for s in inst['steps']),
        'state': inst['state'][:1].upper() + inst['state'][1:], 'actions': d['actions']})


@require_POST
@login_required
def approve(request, id):
    b = _page_body(DecisionPageIn, DecisionForm(request.POST).body(), 'approvedMin', 'onBehalfOf')
    service.decide(request.hajiri_user, str(id), 'approve', reason=b.get('reason'), on_behalf_of=b.get('onBehalfOf'),
                   approved_min=b.get('approvedMin'))
    return _ok(request, 'Approved.', 'approvals', close=True)


@require_http_methods(['GET', 'POST'])
@login_required
def reject(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        inst = service.instance_in(user.company_id, id)
        # The reason box names the employee: only people who may read the approval get it.
        if not inst or not service.can_view(user, inst):
            raise NotFoundError('approval not found')
        name = (Employee.objects.for_company(user.company_id).filter(pk=inst['employee_id']).values_list('name_en', flat=True)
                .first() if inst.get('employee_id') else None)
        return render(request, 'workflow/partials/reject.html', {'id': inst['id'], 'name': name or 'This employee'})
    b = _page_body(DecisionPageIn, {'reason': request.POST.get('reason') or ''})
    service.decide(user, str(id), 'reject', reason=b.get('reason') or '')
    return _ok(request, 'Rejected.', 'approvals', close=True)


@require_POST
@login_required
def bulk_approve(request):
    ids = _page_body(BulkApproveIn, BulkApproveForm(request.POST).body(), 'ids')['ids']
    results = service.bulk_approve(request.hajiri_user, ids)
    ok = sum(1 for r in results if r.get('state'))
    failed = len(results) - ok
    return _ok(request, f"{ok} approved{f', {failed} failed' if failed else ''}.", 'approvals')


# ---------------------------------------------------------------- delegation (DelegateApprovals.tsx)

def _delegates_ctx(user) -> dict:
    mine = [d for d in flow_admin.list_delegates(user) if d['user_id'] == user.id]
    return {'mine': mine, 'people': flow_admin.delegate_candidates(user), 'today': service.npt_today()}


@require_GET
@login_required
def delegates(request):
    return render(request, 'workflow/partials/delegates.html', _delegates_ctx(request.hajiri_user))


@require_POST
@login_required
def delegate_new(request):
    flow_admin.add_delegate(request.hajiri_user, DelegateForm(request.POST).body())
    resp = render(request, 'workflow/partials/delegates.html', _delegates_ctx(request.hajiri_user))
    refresh(resp, 'delegate-button')
    return toast(request, resp, 'Your approvals will go to your delegate on those days.')


@require_POST
@login_required
def delegate_cancel(request, id):
    flow_admin.cancel_delegate(request.hajiri_user, str(id))
    resp = render(request, 'workflow/partials/delegates.html', _delegates_ctx(request.hajiri_user))
    refresh(resp, 'delegate-button')
    return resp


@require_GET
@login_required
def delegate_button(request):
    return render(request, 'workflow/partials/delegate_button.html', {'delegation': flow_admin.active_delegation(request.hajiri_user)})


# ---------------------------------------------------------------- who will approve this (ApprovalChainPreview.tsx)

@require_GET
@login_required
def preview(request):
    q = {'objectType': request.GET.get('object_type') or request.GET.get('objectType') or ''}
    emp = request.GET.get('employee_id') or request.GET.get('employeeId')
    if request.GET.get('pick') == '1' and not emp:
        return HttpResponse(format_html('<p class="hj-sm hj-muted" style="margin:0">{}</p>', 'Pick an employee to see their chain.'))
    if emp:
        q['employeeId'] = emp
    amount = request.GET.get('amount')
    try:
        if amount not in (None, '') and float(amount) > 0:
            q['amount'] = amount
    except ValueError:
        pass
    try:
        data = flow_admin.preview(request.hajiri_user, q)
    except HttpError:
        return HttpResponse('')   # silent when it cannot be worked out
    if not data['steps']:
        return HttpResponse('')
    for s in data['steps']:
        a = s['approvers']
        s['text'] = s['label'] if not a else ' or '.join(x['name'] for x in a) if len(a) <= 2 else f"{s['label']} ({len(a)} people)"
    notes = list(dict.fromkeys(n for s in data['steps'] for n in s['notes']))
    return render(request, 'workflow/partials/chain.html', {'steps': data['steps'], 'notes': '. '.join(notes)})


# ================================================================ request history (RequestsPage.tsx)

STATUS_OPTS = [(c.value, c.label) for c in LeaveRequest.Status]


class RequestFilter(django_filters.FilterSet):
    """?status= on the request history (blank: every status)."""
    status = django_filters.ChoiceFilter(choices=STATUS_OPTS)


_HISTORY = {
    'leave': (LeaveRequest, '-created_at', ('id', 'employee_id', 'from_date', 'to_date', 'days_requested', 'status', 'created_at'),
              {'type_name': F('leave_type__name_en')}),
    'reg': (RegularisationRequest, '-created_at', ('id', 'employee_id', 'local_date', 'kind', 'punch_time', 'punch_direction',
                                                   'reason', 'status', 'created_at'), {}),
    'ot': (OtRequest, '-local_date', ('id', 'employee_id', 'local_date', 'requested_min', 'approved_min', 'reason', 'status'), {}),
}


HISTORY_LIMIT = 500


def history_queryset(user, kind: str, status: str | None):
    """leave/requests, regularisation and ot/requests with scope=all (attendance area), newest first."""
    if not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    model, order, fields, extra = _HISTORY[kind if kind in _HISTORY else 'ot']
    qs = RequestFilter({'status': status or ''}, queryset=model.objects.for_company(company_id_of(user))).qs
    return qs.order_by(order, '-id').values(*fields, emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), **extra)


def history_rows(user, kind: str, status: str | None) -> list[dict]:
    """The request history rows of one tab, at most 500."""
    return list(history_queryset(user, kind, status)[:HISTORY_LIMIT])


def _name_cell(r):
    return format_html('<div class="hj-strong hj-sm">{}</div><div class="hj-small hj-muted">{}</div>', r['name_en'], r['emp_code'])


def _reg_kind(r) -> str:
    base = {'wfh': 'Work from home', 'missed_punch': 'Missed punch', 'on_duty': 'On duty elsewhere', 'half_day': 'Half day',
            'shift_change': 'Shift change'}.get(r['kind'], (r['kind'] or '').replace('_', ' '))
    if not r.get('punch_time'):
        return base
    d = r.get('punch_direction')
    direction = 'clock-in' if (d or '').lower() == 'in' else 'clock-out' if d else ''
    return f"{base}, {direction + ' ' if direction else ''}at {fmt_time(r['punch_time'])}"


def _leave_dates(r) -> str:
    return fmt_date_short(r['from_date']) + (f" to {fmt_date_short(r['to_date'])}" if r['to_date'] != r['from_date'] else '')


def _cancel_url(r) -> str | None:
    try:
        return reverse('leave:cancel', args=[r['id']])
    except NoReverseMatch:
        return None


def _leave_actions(r):
    url = _cancel_url(r)
    if r['status'] not in ('pending', 'approved') or not url:
        return ''
    msg = (f"{r['name_en']}'s {r['type_name'].lower()} for {_leave_dates(r)} will be cancelled and the days returned to "
           'their balance.')
    from hajiri.web.templatetags.hj import confirm_attrs
    return format_html('<button class="hj-btn danger subtle xs" hx-post="{}" hx-vals=\'{{"reason": "cancelled by HR"}}\' {}>'
                       'Cancel leave</button>', url, confirm_attrs(msg, title='Cancel leave?', label='Cancel leave'))


LEAVE_COLS = [Column('name_en', 'Employee', render=_name_cell, sortable=True),
              Column('type_name', 'Leave type'),
              Column('from_date', 'Dates', render=_leave_dates, sortable=True),
              Column('days_requested', 'Days', align='right', render=lambda r: format_html('<span class="num">{}</span>', r['days_requested'])),
              Column('status', 'Status', format='status'),
              Column('id', '', render=_leave_actions, csv=False, align='right')]
REG_COLS = [Column('name_en', 'Employee', render=_name_cell, sortable=True),
            Column('local_date', 'Date', render=lambda r: fmt_date_short(r['local_date']), sortable=True),
            Column('kind', 'Correction', render=_reg_kind),
            Column('reason', 'Reason', muted=True),
            Column('status', 'Status', format='status')]
OT_COLS = [Column('name_en', 'Employee', render=_name_cell, sortable=True),
           Column('local_date', 'Date', render=lambda r: fmt_date_short(r['local_date']), sortable=True),
           Column('requested_min', 'Requested', align='right', render=lambda r: minutes_to_hm(r['requested_min'])),
           Column('approved_min', 'Approved', align='right',
                  render=lambda r: '—' if r['approved_min'] is None else minutes_to_hm(r['approved_min'])),
           Column('status', 'Status', format='status')]


@require_GET
@require_module('attendance')
def history(request):
    user = request.hajiri_user
    status = request.GET.get('status', 'pending')
    if status not in dict(STATUS_OPTS):
        status = ''
    tab = request.GET.get('tab') if request.GET.get('tab') in ('leave', 'reg', 'ot') else 'leave'
    word = f"{dict(STATUS_OPTS)[status].lower()} " if status else ''
    counts = {k: history_queryset(user, k, status or None)[:HISTORY_LIMIT].count() for k in ('leave', 'reg', 'ot')}
    cols, empty = {'leave': (LEAVE_COLS, f'No {word}leave requests'), 'reg': (REG_COLS, f'No {word}attendance corrections'),
                   'ot': (OT_COLS, f'No {word}overtime requests')}[tab]
    table = Table(request, history_rows(user, tab, status or None), cols, id='requests', empty_title=empty,
                  empty_hint='Try another status filter.', extra_params={'tab': tab, 'status': status})
    tabs = [{'key': 'leave', 'label': 'Leave'}, {'key': 'reg', 'label': 'Attendance corrections'}, {'key': 'ot', 'label': 'Overtime'}]
    return table_response(request, table, 'workflow/requests.html', {
        'table': table, 'tab': tab, 'status': status, 'status_opts': STATUS_OPTS, 'counts': counts, 'tabs': tabs})


# ================================================================ approval flows (ApprovalFlowsPage.tsx)

def lookups(user) -> dict:
    c = company_id_of(user)

    def opt(qs, label):
        return [{'value': str(r['id']), 'label': label(r)} for r in qs]
    return {
        'users': opt(AppUser.objects.for_company(c).filter(status=AppUser.Status.ACTIVE).order_by('name').values('id', 'name'),
                     lambda r: r['name']),
        'groups': opt(UserGroup.objects.for_company(c).order_by('name').values('id', 'name'), lambda r: r['name']),
        'employees': opt(Employee.objects.for_company(c).filter(status=Employee.Status.ACTIVE).order_by('name_en')
                         .values('id', 'emp_code', 'name_en'), lambda r: f"{r['name_en']} ({r['emp_code']})"),
        'designations': opt(Designation.objects.for_company(c).order_by('name_en').values('id', 'name_en'), lambda r: r['name_en']),
        'units': opt(OrgUnit.objects.for_company(c).order_by('name_en').values('id', 'name_en'), lambda r: r['name_en']),
        'branches': opt(Branch.objects.for_company(c).order_by('name_en').values('id', 'name_en'), lambda r: r['name_en']),
    }


def _label(options: list[dict], value) -> str | None:
    return next((o['label'] for o in options if o['value'] == str(value)), None)


def step_text(s: dict, lk: dict) -> str:
    kind = kind_of(s)
    t = dict(KIND_OPTIONS).get(kind, kind)
    if kind == 'role':
        t = dict(ROLE_OPTIONS).get(s.get('role'), s.get('role'))
    if kind == 'user':
        t = _label(lk['users'], s.get('userId')) or 'A named person'
    if kind == 'group':
        t = f"Group: {_label(lk['groups'], s.get('groupId')) or '?'}"
    if s.get('minAmount') is not None:
        return f"{t} (from Rs {fmt_money(s['minAmount'], currency=False, decimals=0)})"
    return t


def condition_text(c: dict, lk: dict) -> str:
    c = c or {}
    names = lambda ids, lst: [_label(lst, i) or '?' for i in (ids or [])]  # noqa: E731
    parts = []
    if c.get('employeeIds'):
        parts.append(', '.join(names(c['employeeIds'], lk['employees'])))
    if c.get('groupIds'):
        parts.append('group ' + ', '.join(names(c['groupIds'], lk['groups'])))
    if c.get('designationIds'):
        parts.append(', '.join(names(c['designationIds'], lk['designations'])))
    if c.get('orgUnitIds'):
        parts.append('in ' + ', '.join(names(c['orgUnitIds'], lk['units'])))
    if c.get('branchIds'):
        parts.append('at ' + ', '.join(names(c['branchIds'], lk['branches'])))
    return ' · '.join(parts) if parts else 'Everyone'


def editor_ctx(prefix: str, steps: list[dict], money: bool, lk: dict) -> dict:
    return {'prefix': prefix, 'steps': steps, 'money': money, 'lk': lk, 'kinds': KIND_OPTIONS, 'roles': ROLE_OPTIONS,
            'can_add': len(steps) < 6, 'last': len(steps) - 1}


@require_POST
@require_role('hr')
def steps_editor(request):
    """Add, move, remove a step or change its kind; answers the re-rendered editor."""
    prefix = request.POST.get('prefix') or 'steps'
    if not re.fullmatch(r'[a-z_]+', prefix):
        raise BadRequestError('bad editor')
    steps = steps_from_post(request.POST, prefix)
    op, _, arg = (request.POST.get('op') or '').partition(':')
    i = int(arg) if arg.isdigit() else -1
    if op == 'add' and len(steps) < 6:
        steps.append({'kind': 'role', 'role': 'hr'})
    elif op == 'remove' and 0 <= i < len(steps) and len(steps) > 1:
        steps.pop(i)
    elif op == 'kind' and 0 <= i < len(steps):
        steps[i].pop('label', None)   # a new kind starts fresh, keeping only the amount threshold (StepsEditor onChange)
    elif op in ('up', 'down') and 0 <= i < len(steps):
        j = i - 1 if op == 'up' else i + 1
        if 0 <= j < len(steps):
            steps[i], steps[j] = steps[j], steps[i]
    money = request.POST.get('money') == '1'
    return render(request, 'workflow/partials/steps_editor.html', {'e': editor_ctx(prefix, steps, money, lookups(request.hajiri_user))})


@require_GET
@require_role('hr')
def flows_page(request):
    user = request.hajiri_user
    tab = request.GET.get('tab') if request.GET.get('tab') in ('flows', 'groups', 'check') else 'flows'
    cfg = flow_admin.flows_config(user)
    lk = lookups(user)
    types = []
    for t in cfg['types']:
        default = [to_step_input(s) for s in t['defaultSteps']]
        types.append({**t, 'summary': ' → '.join(step_text(s, lk) for s in t['defaultSteps'])
                      + (f" · {len(t['flows'])} more flow{'' if len(t['flows']) == 1 else 's'}" if t['flows'] else ''),
                      'editor': editor_ctx(t['objectType'], default, t['money'], lk),
                      'flow_rows': [{**f, 'cond': condition_text(f['conditions'], lk),
                                     'chain': ' → '.join(step_text(s, lk) for s in f['steps'])} for f in t['flows']]})
    return render(request, 'workflow/flows.html', {
        'tab': tab, 'types': types, 'groups': cfg['groups'], 'lk': lk, 'open': request.GET.get('open'),
        'type_options': [(t.type, t.label) for t in REQUEST_TYPES],
        'money_types': [t.type for t in REQUEST_TYPES if t.money]})


@require_POST
@require_role('hr')
def set_default(request, object_type):
    steps = steps_from_post(request.POST, object_type)
    flow_admin.set_default(request.hajiri_user, object_type, {'steps': steps})
    t = REQUEST_TYPE_BY_KEY[object_type]
    return toast(request, HttpResponse(status=204), f'{t.label}: approval chain saved.')


def _flow_form(request, type_key: str, flow: dict | None, status: int = 200):
    lk = lookups(request.hajiri_user)
    t = REQUEST_TYPE_BY_KEY[type_key]
    cond = {k: [] for k in ('employeeIds', 'groupIds', 'designationIds', 'orgUnitIds', 'branchIds')}
    cond.update({k: [str(x) for x in v] for k, v in ((flow or {}).get('conditions') or {}).items() if k in cond})
    steps = [to_step_input(s) for s in flow['steps']] if flow else [{'kind': 'manager'}]
    fields = [{'key': k, 'label': lab, 'options': lk[src], 'placeholder': ph, 'selected': cond[k]} for k, lab, src, ph in CONDITION_FIELDS]
    return render(request, 'workflow/partials/flow_form.html', {
        'type': t, 'flow': flow, 'fields': fields, 'employees': {'options': lk['employees'], 'selected': cond['employeeIds']},
        'any': any(cond.values()), 'editor': editor_ctx('flow', steps, t.money, lk)}, status=status)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def flow_new(request):
    type_key = request.GET.get('type') or request.POST.get('objectType') or ''
    flow_admin.parse_type(type_key)
    if request.method == 'GET':
        return _flow_form(request, type_key, None)
    flow_admin.create_flow(request.hajiri_user, {'objectType': type_key, **FlowForm(request.POST).body()})
    return _ok(request, 'Flow saved.', 'flows', close=True)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def flow_edit(request, id):
    flow = flow_admin.get_flow(request.hajiri_user, str(id))
    if request.method == 'GET':
        return _flow_form(request, flow['object_type'], flow)
    flow_admin.update_flow(request.hajiri_user, str(id), FlowForm(request.POST).body())
    return _ok(request, 'Flow saved.', 'flows', close=True)


@require_POST
@require_role('hr')
def flow_active(request, id):
    flow_admin.update_flow(request.hajiri_user, str(id), {'active': request.POST.get('active') in ('1', 'on', 'true')})
    resp = HttpResponse(status=204)
    return refresh(resp, 'flows')


@require_POST
@require_role('hr')
def flow_delete(request, id):
    flow_admin.delete_flow(request.hajiri_user, str(id))
    return _ok(request, 'Flow deleted.', 'flows')


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def group_form(request, id=None):
    user = request.hajiri_user
    group = None
    if id is not None:
        group = flow_admin.get_group(user, str(id))
    if request.method == 'GET':
        return render(request, 'workflow/partials/group_form.html', {
            'group': group, 'users': lookups(user)['users'], 'members': [m['id'] for m in (group or {}).get('members', [])]})
    b = GroupForm(request.POST).body()
    if group:
        flow_admin.update_group(user, group['id'], b)
    else:
        flow_admin.create_group(user, b)
    return _ok(request, 'Group saved.', 'flows', close=True)


@require_POST
@require_role('hr')
def group_delete(request, id):
    flow_admin.delete_group(request.hajiri_user, str(id))
    return _ok(request, 'Group deleted.', 'flows')


@require_GET
@require_role('hr')
def verify(request):
    ot = request.GET.get('object_type') or None
    result = flow_admin.verify(request.hajiri_user, {'objectType': ot} if ot else {})
    return render(request, 'workflow/partials/verify.html', {'result': result, 'problems': result['problems'][:500]})
