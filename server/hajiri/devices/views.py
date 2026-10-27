"""Settings -> Devices (DevicesPage.tsx) and the biometric actions other pages (My data, employee detail) post to."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.errors import ConflictError
from hajiri.core.http import ApiEncoder
from hajiri.web.decorators import login_required, require_role
from hajiri.web.forms import first_error
from hajiri.web.format import fmt_date, fmt_time, relative_time, today_npt
from hajiri.web.htmx import close_modal, refresh, toast
from hajiri.web.tables import Column, Table, table_response
from hajiri.web.templatetags.hj import status_badge_html

from . import biometrics, services
from .forms import ActivateForm, ConsentForm, MapForm


def _device_cell(d):
    from hajiri.web.templatetags.hj import tone
    return format_html(
        '<button type="button" class="hj-link-btn hj-row nowrap hj-gap-sm" hx-get="/_/devices/{}" hx-target="#hj-drawer-body" '
        'style="text-align:left"><span class="hj-dot" style="background:var(--hj-{})" aria-hidden="true"></span>'
        '<span style="min-width:0"><span class="hj-sm hj-strong hj-truncate" style="display:block">{}</span>'
        '<span class="hj-small hj-muted mono">{}</span></span></button>',
        d['id'], tone(services.live_status(d)), d['name'] or 'Unnamed terminal', d['serial_no'])


def _vendor_cell(d):
    return format_html('<div class="hj-sm">{}</div><div class="hj-small hj-muted">{}</div>',
                       services.VENDOR.get(d['vendor'], d['vendor']), services.PROTOCOL.get(d['protocol'], d['protocol']))


DEVICE_COLUMNS = [
    Column('name', 'Device', render=_device_cell, csv=lambda d: d['name'] or 'Unnamed terminal'),
    Column('vendor', 'Vendor', render=_vendor_cell),
    Column('status', 'Status', render=lambda d: status_badge_html(services.live_status(d)), csv=services.live_status),
    Column('last_seen_at', 'Last seen', render=lambda d: relative_time(d['last_seen_at']) if d['last_seen_at'] else
           format_html('<span class="hj-muted">{}</span>', 'Never')),
    Column('mapped_users', 'Mapped PINs', align='right', format='num'),
    Column('fw_version', 'Firmware', muted=True, render=lambda d: d['fw_version'] or d['push_version'] or '—'),
    Column('actions', '', template='devices/partials/device_actions.html', csv=False, align='right', width='90px'),
]

UNMAPPED_COLUMNS = [
    Column('device_user_pin', 'PIN', mono=True),
    Column('device_sn', 'Device', mono=True, muted=True),
    Column('punched_at', 'Last punch', render=lambda p: format_html('<div class="hj-sm">{}</div><div class="hj-small hj-muted num">{}</div>',
                                                                      fmt_date(p['local_date']), fmt_time(p['punched_at']))),
    Column('total_for_pin', 'Punches for PIN', align='right', format='num'),
    Column('actions', '', template='devices/partials/unmapped_actions.html', csv=False, align='right'),
]


def _physical(devs):
    out = [d for d in devs if d['vendor'] not in services.PSEUDO_VENDORS]
    for d in out:
        d['disable_title'] = f"Disable {d['name'] or d['serial_no'] or 'device'}?"
    return out


@require_GET
@require_role('hr')
def device_list(request):
    user = request.hajiri_user
    devices = _physical(services.list_devices(user))
    unmapped = services.unmapped_punches(user)
    t_dev = Table(request, devices, DEVICE_COLUMNS, id='devices', page_size=50, row_key='id',
                  empty_title='No terminals have contacted the gateway yet',
                  empty_hint='Point a ZKTeco/eSSL or Hikvision terminal at the gateway (see docs/gateway.md). It shows up here within a minute.')
    t_un = Table(request, unmapped, UNMAPPED_COLUMNS, id='unmapped', page_size=50,
                 empty_title='Every punch is attributed',
                 empty_hint='Punches from PINs that are not mapped to an employee will appear here.')
    tab = 'unmapped' if request.GET.get('tab') == 'unmapped' else 'devices'
    for t in (t_dev, t_un):
        if t.wants_csv or t.is_partial:
            return table_response(request, t, 'devices/list.html', {})
    return render(request, 'devices/list.html', {'devices': t_dev, 'unmapped': t_un, 'tab': tab,
                                                 'device_count': len(devices), 'unmapped_count': len(unmapped)})


def valid_label(valid: str) -> str:
    """A mapping's validity, '[2026-09-01,2026-10-01)', in Bikram Sambat: "16 Bhadra 2083 to 14 Ashwin 2083" or
    "From 16 Bhadra 2083". The upper bound is exclusive, so the last day shown is the day before it."""
    m = re.fullmatch(r'[\[(](\d{4}-\d{2}-\d{2})?,(\d{4}-\d{2}-\d{2})?[\])]', (valid or '').strip())
    if not m:
        return valid or ''
    lo, hi = m.group(1), m.group(2)
    if hi:
        last = (date.fromisoformat(hi) - timedelta(days=1)).isoformat()
        return f'{fmt_date(lo)} to {fmt_date(last)}' if lo else f'Until {fmt_date(last)}'
    return f'From {fmt_date(lo)}' if lo else 'Always'


def _mappings(user, id):
    return [{**m, 'valid_label': valid_label(m['valid'])} for m in services.mappings(user, id)]


@require_GET
@require_role('hr')
def device_detail(request, id):
    user = request.hajiri_user
    d = services.get_device(user, id)
    return render(request, 'devices/partials/detail.html', {
        'd': d, 'status': services.live_status(d), 'vendor': services.VENDOR.get(d['vendor'], d['vendor']),
        'protocol': services.PROTOCOL.get(d['protocol'], d['protocol']), 'mappings': _mappings(user, id),
        'commands': services.commands(user, id)[:10], 'page_commands': services.PAGE_COMMANDS})


@require_GET
@require_role('hr')
def device_commands(request, id):
    return render(request, 'devices/partials/commands.html', {
        'd': {'id': id}, 'commands': services.commands(request.hajiri_user, id)[:10]})


@require_GET
@require_role('hr')
def device_mappings(request, id):
    return render(request, 'devices/partials/mappings.html', {'d': {'id': id}, 'mappings': _mappings(request.hajiri_user, id)})


def _done(request, message, *targets, close=True):
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for t in targets:
        refresh(resp, t)
    return toast(request, resp, message)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def device_activate(request, id):
    user = request.hajiri_user
    d = services.get_device(user, id)
    ctx = {'d': d, 'branches': services.branches(user)}
    if request.method == 'GET':
        return render(request, 'devices/partials/activate.html', {**ctx, 'form': ActivateForm(initial={'name': d['name'] or ''})})
    form = ActivateForm(request.POST)
    if not form.is_valid():
        return render(request, 'devices/partials/activate.html', {**ctx, 'form': form}, status=422)
    body = {}
    if form.cleaned_data['name']:
        body['name'] = form.cleaned_data['name']
    if form.cleaned_data['branch_id']:
        body['branchId'] = str(form.cleaned_data['branch_id'])
    services.activate(user, id, body)
    return _done(request, 'Device activated.', 'devices')


@require_POST
@require_role('hr')
def device_disable(request, id):
    services.disable(request.hajiri_user, id)
    return _done(request, 'Device disabled.', 'devices', close=False)


@require_POST
@require_role('hr')
def device_key(request, id):
    user = request.hajiri_user
    d = services.get_device(user, id)
    key = services.issue_key(user, id)['key']
    return render(request, 'devices/partials/key.html', {'d': d, 'key': key}, status=201)


@require_POST
@require_role('hr')
def device_sync(request, id):
    r = services.sync_users(request.hajiri_user, id)
    return _done(request, f"{r['queued']} user records queued for the terminal.", 'device-commands', close=False)


@require_POST
@require_role('hr')
def device_command(request, id):
    cmd = request.POST.get('command') or ''
    if not cmd:   # the picker's placeholder
        return HttpResponse(status=204)
    services.queue_command(request.hajiri_user, id, {'command': cmd})
    resp = _done(request, f'{cmd} queued.', 'device-commands', close=False)
    resp.status_code = 201
    return resp


def _map_ctx(request, form, *, conflict=None, replace=False):
    data = form.data if form.is_bound else form.initial
    return {'form': form, 'conflict': conflict, 'replace': replace, 'pin': data.get('pin', ''),
            'device_id': data.get('device_id', ''), 'employee_id': data.get('employee_id') or None,
            'valid_from': data.get('valid_from') or None}


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def device_map(request):
    user = request.hajiri_user
    if request.method == 'GET':
        g = request.GET
        services.get_device(user, g.get('device', ''))
        form = MapForm(initial={'device_id': g.get('device'), 'pin': g.get('pin', ''), 'valid_from': g.get('from') or today_npt()})
        return render(request, 'devices/partials/map.html', _map_ctx(request, form))
    form = MapForm(request.POST)
    # The confirmation belongs to the PIN it was asked for: editing the PIN afterwards asks again (DevicesPage.tsx
    # drops `replace` when the PIN field changes).
    replace = request.POST.get('replace') in ('1', 'true', 'True', 'on') and \
        request.POST.get('replace_pin', request.POST.get('pin', '')) == request.POST.get('pin', '')
    if not form.is_valid():
        return render(request, 'devices/partials/map.html', {**_map_ctx(request, form, replace=replace),
                                                              'error': first_error(form)}, status=422)
    c = form.cleaned_data
    body = {'pin': c['pin'], 'employeeId': str(c['employee_id']), 'from': c['valid_from'].isoformat()}
    if replace:
        body['replace'] = True
    try:
        r = services.map_pin(user, c['device_id'], body)
    except ConflictError as e:
        if replace:
            raise
        # The PIN already belongs to someone else from that date: ask before moving it (and their punches) across.
        return render(request, 'devices/partials/map.html', _map_ctx(request, form, conflict=e.message, replace=True), status=422)
    return _done(request, f"PIN {c['pin']} mapped. {r['reattributed']} earlier day(s) re-attributed and recalculated.",
                 'unmapped', 'device-mappings', 'devices')


# ---------------------------------------------------------------- biometrics (posted from My data and employee pages)

@require_POST
@login_required
def consent_grant(request):
    form = ConsentForm(request.POST)
    if not form.is_valid():
        from hajiri.core.errors import BadRequestError
        raise BadRequestError(first_error(form))
    c = form.cleaned_data
    body = {'employeeId': str(c['employee_id']), 'modality': c['modality'], 'noticeVersion': c['notice_version']}
    if c['purpose']:
        body['purpose'] = c['purpose']
    if c['signature']:
        body['signature'] = c['signature']
    biometrics.grant_consent(request.hajiri_user, body)
    resp = _done(request, 'Consent recorded.', 'biometric-consents', close=False)
    resp.status_code = 201
    return resp


@require_POST
@login_required
def consent_revoke(request, id):
    r = biometrics.revoke_consent(request.hajiri_user, id)
    return _done(request, f"Consent withdrawn. {r['templatesShredded']} template(s) deleted.", 'biometric-consents',
                 'biometric-templates', close=False)


@require_POST
@require_role('hr')
def template_shred(request, id):
    r = biometrics.shred_template(request.hajiri_user, id)
    n = r['shredded']
    return _done(request, f"{n} {'template' if n == 1 else 'templates'} shredded.", 'biometric-templates', close=False)


@require_POST
@require_role('hr')
def employee_shred_all(request, id):
    r = biometrics.shred_all(request.hajiri_user, id)
    n = r['shredded']
    return _done(request, f"{n} {'template' if n == 1 else 'templates'} shredded.", 'biometric-templates',
                 'biometric-consents', close=False)


@require_POST
@require_role('hr', 'admin')
def retention_run(request):
    grace = request.POST.get('template_grace_days')
    body = {'templateGraceDays': int(grace)} if grace and grace.isdigit() else {}
    r = biometrics.run_retention(request.hajiri_user, body)
    return _done(request, f"Retention run: {r['templatesShredded']} template(s) shredded for {r['employees']} former employee(s).",
                 'biometric-templates', close=False)


@require_GET
@login_required
def consents_fragment(request):
    rows = biometrics.consents(request.hajiri_user, {'employeeId': request.GET.get('employeeId', '')})
    return render(request, 'devices/partials/consents.html', {'consents': rows, 'employee_id': request.GET.get('employeeId')})


@require_GET
@login_required
def templates_fragment(request):
    rows = biometrics.templates(request.hajiri_user, {'employeeId': request.GET.get('employeeId', '')})
    return render(request, 'devices/partials/templates.html', {'templates': rows, 'employee_id': request.GET.get('employeeId'),
                                                                'can_shred': 'hr' in request.hajiri_user.roles or 'admin' in request.hajiri_user.roles})


@require_GET
@login_required
def dsar_download(request):
    """Download my data: the DSAR export as a JSON file (bioApi.dsar)."""
    emp = request.GET.get('employeeId', '')
    data = biometrics.dsar(request.hajiri_user, {'employeeId': emp})
    resp = HttpResponse(json.dumps(data, cls=ApiEncoder, ensure_ascii=False, indent=2), content_type='application/json; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="my-data-{data["employee"]["id"]}.json"'
    return resp


@require_GET
@login_required
def transparency_fragment(request):
    """What is stored, for how long and who sees it (bilingual), for the My data page."""
    return render(request, 'devices/partials/transparency.html', {'t': biometrics.transparency()})
