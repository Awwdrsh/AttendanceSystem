"""
Payroll pages (apps/web/src/pages/payroll and employee/{PayslipsPage,PayslipDetailPage,MyTaxPage}): the Payroll hub
(pay runs, salary structures, employee salaries, tax declarations, loans, reports, optimum CIT, held pay, opening
balances, rules), the pay run page (/payroll/runs/<id>) and the Payslips hub (/me/payslips, /me/payslips/tax,
/me/payslips/<id>). HTMX fragments and form posts live under /_/payroll/...; every rule is in api.py, compliance.py
and tax_savings.py, the same functions the JSON endpoints call.
"""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from engines.bs_calendar import BS_MONTH_NAMES_EN, ad_to_bs

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.errors import BadRequestError, HttpError, NotFoundError
from hajiri.web.decorators import login_required, require_module
from hajiri.web.format import add_days, bs_month_label, fmt_date, fmt_money, fmt_time, today_npt
from hajiri.web.htmx import close_modal, flash, hx_redirect, is_htmx, refresh, toast

from . import api, compliance, tax_savings
from .api_views import csv_download, html_response, sheet_response

RUN_TYPE_LABEL = {'regular': 'Regular', 'supplementary': 'Supplementary', 'arrears': 'Arrears', 'off_cycle': 'Off-cycle',
                  'final_settlement': 'Final settlement', 'bonus': 'Bonus'}
RUN_TITLE = {'regular': 'Regular run', 'supplementary': 'Supplementary run', 'arrears': 'Arrears run', 'off_cycle': 'Off-cycle run',
             'final_settlement': 'Final settlement', 'bonus': 'Bonus run'}
RUN_TYPE_OPTIONS = [('regular', 'Regular monthly'), ('supplementary', 'Supplementary (correction after lock)'),
                    ('arrears', 'Arrears (salary change in a locked period)'), ('off_cycle', 'Off-cycle'),
                    ('final_settlement', 'Final settlement (one employee)')]
ORDER = ['draft', 'preflight_failed', 'computed', 'reviewed', 'approved', 'locked', 'disbursed', 'filed']
STEP_OF = {'draft': 0, 'preflight_failed': 0, 'computed': 2, 'reviewed': 3, 'approved': 3, 'locked': 4, 'disbursed': 4, 'filed': 4}
COMPONENT_TYPES = [('earning', 'Earning'), ('deduction', 'Deduction'), ('employer_contribution', 'Employer contribution'),
                   ('statistical', 'Statistical (not paid)')]
MONEY_KEYS = re.compile(r'^(gross|taxable|sst|incomeTax|tds|SSF_EE|SSF_ER|PF_EE|PF_ER|GRATUITY|CIT|ssfTotal|pfTotal|retirementEmployee|'
                        r'retirementEmployer|taxableAfterDeductions|annualTax|balance|previousNet|net|difference|dailyRate|amount)$')
HOLD_STATUS = {'held': ('On hold', 'warn'), 'releasing': ('Paid with next run', 'info'), 'released': ('Released', 'ok'),
               'cancelled': ('Cancelled', 'idle')}
OPENING_SOURCE = {'previous_employer': 'Previous employer', 'go_live': 'Before Hajiri', 'other': 'Other'}
OPENING_TEMPLATE = 'emp_code,fiscal_year_start,gross,taxable,tds_paid,retirement_employee,retirement_employer,festival_paid,months,source,note\n'
CIT_REASON = {'already_optimal': 'Already lowest tax', 'no_tax_left': 'No tax left to save', 'net_pay': 'Limited by net pay',
              'limit': 'Fills the deduction limit'}
REPORT_KINDS = [('tax', 'Tax deposit'), ('funds', 'Retirement funds'), ('variance', 'Pay variance'), ('liability', 'Leave liability'),
                ('annual', 'Annual tax'), ('certs', 'Certificates')]
AMOUNT_RE = re.compile(r'^\d+(\.\d{1,2})?$')


# ---------------------------------------------------------------- helpers

def failed(request, exc: HttpError, title: str) -> HttpResponse:
    """toast.err(e, title) of the TS page: the error with the page's own title."""
    from hajiri.web.middleware import error_message
    if exc.status == 401 or not is_htmx(request):
        raise exc
    msg = error_message(exc)
    resp = HttpResponse(msg, status=exc.status, content_type='text/plain; charset=utf-8')
    resp.headers['HX-Reswap'] = 'none'
    return toast(request, resp, msg, tone='err', title=title)


def money(v) -> str:
    return fmt_money(v)


def money0(v) -> str:
    """fmtMoney(v, { decimals: 0 })."""
    return fmt_money(v if v not in (None, '') else None, decimals=0)


def run_type_label(t: str) -> str:
    return RUN_TYPE_LABEL.get(t, t.replace('_', ' '))


def current_bs():
    return ad_to_bs(today_npt())


def current_fy() -> int:
    b = current_bs()
    return b.year if b.month >= 4 else b.year - 1


def fy_options(years) -> list[tuple[int, str]]:
    return [(y, f'FY {y}/{str(y + 1)[-2:]}') for y in years]


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _str(v) -> str:
    return v.strip() if isinstance(v, str) else ''


def _num_str(v, default='0') -> str:
    """A number field as the decimal string the API expects (Number(v).toFixed(2) in the React forms)."""
    s = _str(v).replace(',', '')
    if not s:
        return default
    try:
        return f'{Decimal(s):.2f}'
    except InvalidOperation:
        return s


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, '') else 0))
    except InvalidOperation:
        return Decimal(0)


def done(request, message: str, *, url: str | None = None, refresh_id: str | None = None, title: str | None = None):
    """Success: close the modal and reload the page (or one block), with the TS toast."""
    if url:
        r = hx_redirect(request, url)
        return flash(r, message, 'ok', title)
    r = HttpResponse(status=204)
    close_modal(r)
    if refresh_id:
        refresh(r, refresh_id)
    return toast(request, r, message, 'ok', title)


def _current_url(request, default: str) -> str:
    cur = request.headers.get('HX-Current-URL')
    if cur:
        u = urlparse(cur)
        return u.path + (f'?{u.query}' if u.query else '')
    return default


# ---------------------------------------------------------------- Pay runs (PayrollHubPage)

@require_GET
@require_module('payroll')
def runs(request):
    user = request.hajiri_user
    lst = api.list_runs(user)['runs']
    periods = api.list_periods(user)['periods']
    rows = []
    for r in lst:
        t = r.get('totals') or {}
        rows.append({**r, 'period_label': bs_month_label(r['bs_year'], r['bs_month']), 'type_label': run_type_label(r['run_type']),
                     'headcount': t.get('headcount') or '—', 'gross_label': money(t['gross']) if t.get('gross') else '—',
                     'net_label': money(t['net']) if t.get('net') else '—'})
    return render(request, 'payroll/runs.html', {'runs': rows, 'period_count': len(periods)})


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def run_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        b = current_bs()
        locked = [r for r in api.list_runs(user)['runs'] if r['locked_at']]
        return render(request, 'payroll/partials/run_form.html', {
            'y': b.year, 'm': b.month, 'types': RUN_TYPE_OPTIONS,
            'locked_runs': [(r['id'], f"{bs_month_label(r['bs_year'], r['bs_month'])}, {run_type_label(r['run_type']).lower()}") for r in locked]})
    p = request.POST
    try:
        period = api.create_period(user, {'bsYear': _int(p.get('bs_year')), 'bsMonth': _int(p.get('bs_month'))})['period']
        body = {'periodId': period['id'], 'runType': p.get('run_type') or 'regular'}
        if p.get('references_run_id') and body['runType'] in ('arrears', 'supplementary'):
            body['referencesRunId'] = p['references_run_id']
        if body['runType'] == 'final_settlement':
            if p.get('employee_id'):
                body['employeeIds'] = [p['employee_id']]
            if _str(p.get('notice_pay')):
                body['noticePay'] = _str(p.get('notice_pay'))
        r = api.create_run(user, body)
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Pay run created. Start with the pre-flight checks.', url=f"/payroll/runs/{r['id']}")


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def bonus_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        b = current_bs()
        return render(request, 'payroll/partials/bonus_form.html', {'y': b.year, 'm': b.month, 'fy': current_fy() - 1})
    p = request.POST
    try:
        period = api.create_period(user, {'bsYear': _int(p.get('bs_year')), 'bsMonth': _int(p.get('bs_month'))})['period']
        r = api.create_bonus_run(user, {'periodId': period['id'], 'fiscalYearStart': _int(p.get('fiscal_year_start')),
                                        'netProfit': _num_str(p.get('net_profit'))})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, f"Bonus run created with a pool of {money(r['pool'])}.", url=f"/payroll/runs/{r['id']}")


# ---------------------------------------------------------------- the pay run (RunWizardPage)

def _issue_line(b: dict) -> str:
    out = f"{b['count']} affected" if b.get('count') is not None else ''
    emps = b.get('employees') or []
    if b.get('count') is not None and emps:
        out += ': '
    if emps:
        out += ', '.join(emps[:5]) + (f' and {len(emps) - 5} more' if len(emps) > 5 else '')
    return out


def _pf_ctx(pf: dict | None) -> dict | None:
    if not pf:
        return None
    return {**pf, 'blocker_items': [{**b, 'line': _issue_line(b)} for b in pf.get('blockers') or []],
            'warning_items': [{**b, 'line': _issue_line(b)} for b in pf.get('warnings') or []]}


def _stamp(v) -> str:
    return f'{fmt_date(v)} at {fmt_time(v)}'


def _signed(v) -> str:
    d = _dec(v)
    if d == 0:
        return ''
    return f"{'+' if d > 0 else '−'}{fmt_money(abs(d), currency=False)}"


def _sheet_ctx(s: dict) -> dict:
    c = s['columns']
    m = lambda v: fmt_money(v if v is not None else '0', currency=False)  # noqa: E731
    rows = []
    for r in s['rows']:
        rows.append({**r, 'earn': [m(r['earnings'][x['code']]) if r['earnings'].get(x['code']) else None for x in c['earnings']],
                     'ded': [m(r['deductions'][x['code']]) if r['deductions'].get(x['code']) else None for x in c['deductions']],
                     'emp': [m(r['employer'][x['code']]) if r['employer'].get(x['code']) else None for x in c['employer']],
                     'held': 'PayHeld' in r['flags'], 'rounding_label': _signed(r['rounding']), 'lop': _dec(r['lopDays']) != 0,
                     'gross_l': m(r['gross']), 'ded_l': m(r['totalDeductions']), 'net_l': m(r['net']), 'emp_total_l': m(r['employerTotal']),
                     'ctc_l': m(r['costToCompany'])})
    t = s['totals']
    final = s['run']['final']
    return {**s, 'rows': rows, 'earn_cols': c['earnings'], 'ded_cols': c['deductions'], 'emp_cols': c['employer'], 'rounding': c['rounding'],
            'earn_span': len(c['earnings']) + 1, 'ded_span': len(c['deductions']) + 1, 'emp_span': len(c['employer']) + 2,
            'mid_span': 2 if c['rounding'] else 1,
            'tot_earn': [m(t['earnings'][x['code']]) for x in c['earnings']], 'tot_ded': [m(t['deductions'][x['code']]) for x in c['deductions']],
            'tot_emp': [m(t['employer'][x['code']]) for x in c['employer']], 'tot_rounding': _signed(t['rounding']),
            'tot': {k: m(t[k]) for k in ('gross', 'totalDeductions', 'net', 'employerTotal', 'costToCompany')},
            'hint': ('Final. The run is locked, so these figures will not change. Download it for payment or print it for signatures.' if final
                     else 'Draft. Figures can still change until the run is approved and locked.'),
            'stats': [{'label': 'Employees', 'value': str(len(s['rows']))}, {'label': 'Gross pay', 'value': money(t['gross'])},
                      {'label': 'Deductions', 'value': money(t['totalDeductions'])}, {'label': 'Net pay', 'value': money(t['net'])},
                      {'label': 'Cost to company', 'value': money(t['costToCompany'])}]}


def _run_ctx(request, id_) -> dict:
    user = request.hajiri_user
    d = api.run_detail(user, str(id_))
    run, period, slips = d['run'], d['period'], d['payslips']
    inputs = api.list_inputs(user, str(id_))['inputs']
    totals = run.get('totals') or {}
    variance = run.get('variance') or None
    by_emp = {v['employeeId']: v for v in (variance or {}).get('byEmployee', [])}

    def s(k):
        return sum((_dec(x.get(k)) for x in slips), Decimal(0))
    headcount = totals.get('payslips') or totals.get('headcount') or str(len(slips))
    total_gross = totals.get('gross') or s('gross')
    total_tds = totals.get('tds') or s('tds')
    total_net = totals.get('net') or s('net')
    rows = []
    for x in slips:
        v = by_emp.get(x['employee_id'])
        if not v or v.get('previousNet') is None:
            delta, warn = 'New', False
        else:
            prev = _dec(v['previousNet'])
            dd = _dec(v['net']) - prev
            pct = int((dd / prev * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if prev else 0
            delta, warn = f"{'+' if dd >= 0 else '−'}{abs(pct)}%", abs(pct) > 10
        rows.append({**x, 'flagged': bool(x.get('flags')), 'delta': delta, 'delta_warn': warn})
    status = run['status']
    auto = STEP_OF.get(status, 0)
    allow_next = status in ORDER and ORDER.index(status) >= 2
    step = auto
    req = _int(request.GET.get('step'))
    if req is not None and 0 <= req <= 4:
        # The step buttons (Continue, Back, Preview) open any step; the step strip itself follows Stepper's rules.
        step = req
    locked = bool(run['locked_at'])
    published = bool(slips) and all(x.get('published_at') for x in slips)
    pf = _pf_ctx(run.get('preflight'))
    steps = [
        {'label': 'Pre-flight', 'desc': ('Clear' if pf['ok'] else f"{len(pf['blockers'])} {'blocker' if len(pf['blockers']) == 1 else 'blockers'}") if pf else 'Not run'},
        {'label': 'Inputs', 'desc': f'{len(inputs)} items'},
        {'label': 'Calculate & review', 'desc': f'{len(slips)} payslips' if run.get('computed_at') else ''},
        {'label': 'Approve & lock', 'desc': 'Locked' if locked else 'Approved' if run.get('approved_at') else ''},
        {'label': 'Salary sheet', 'desc': 'Payslips published' if published else 'Final' if locked else 'Draft' if run.get('computed_at') else ''},
    ]
    for i, st in enumerate(steps):
        st.update({'index': i, 'number': i + 1, 'active': i == step, 'done': i < auto, 'clickable': i <= auto or allow_next})
    holds = [h for h in compliance.list_holds(user, {'status': 'open'})['holds'] if h['run_id'] == str(id_)] if run.get('computed_at') else []
    ctx = {
        'run': run, 'period': period, 'slips': rows, 'inputs': inputs, 'totals': totals, 'variance': variance, 'step': step, 'steps': steps,
        'locked': locked, 'published': published, 'pf': pf, 'can_compute': status in ('draft', 'preflight_failed', 'computed', 'reviewed'),
        'is_hr': has_role(user, 'hr'), 'title': bs_month_label(period['bs_year'], period['bs_month']),
        'subtitle': f"{RUN_TITLE.get(run['run_type'], run['run_type'].replace('_', ' '))}, {fmt_date(period['start_ad'])} to "
                    f"{fmt_date(period['end_ad'])} ({period['days']} days)",
        'headcount': headcount, 'total_gross': total_gross, 'total_tds': total_tds, 'total_net': total_net,
        'employer_statutory': totals.get('employerStatutory') or totals.get('employer_statutory'),
        'review_ready': status in ('reviewed', 'approved', 'locked', 'disbursed', 'filed'), 'holds': holds,
        'can_hold': status in ('computed', 'reviewed', 'approved', 'locked'),
        'computed_stamp': _stamp(run['computed_at']) if run.get('computed_at') else None,
        'approved_stamp': _stamp(run['approved_at']) if run.get('approved_at') else None,
        'locked_stamp': _stamp(run['locked_at']) if run.get('locked_at') else None,
        'signature': (run.get('approval_signature') or '')[:20],
        'flagged_word': 'row' if (variance or {}).get('flagged') == 1 else 'rows',
    }
    ctx['summary'] = [{'label': 'Headcount', 'value': headcount}, {'label': 'Gross pay', 'value': money(total_gross)},
                      {'label': 'TDS', 'value': money(total_tds)}, {'label': 'Net pay', 'value': money(total_net)}]
    ctx['calc_stats'] = [{'label': 'Payslips', 'value': headcount}, {'label': 'Gross pay', 'value': money(total_gross)},
                         {'label': 'Employer SSF/PF', 'value': money(ctx['employer_statutory'])}, {'label': 'TDS', 'value': money(total_tds)},
                         {'label': 'Net pay', 'value': money(total_net)}]
    if step == 4:
        try:
            _fmt, sheet = api.salary_sheet_for(user, str(id_), {})
            ctx['sheet'] = _sheet_ctx(sheet)
        except HttpError as e:
            ctx['sheet_error'] = e.message
    return ctx


@require_GET
@require_module('payroll')
def run(request, id):
    try:
        ctx = _run_ctx(request, id)
    except NotFoundError as e:
        return render(request, 'payroll/run_missing.html', {'message': e.message}, status=404)
    return render(request, 'payroll/run.html', ctx)


def run_status_can_compute(rid: str, user) -> bool:
    return api.run_detail(user, rid)['run']['status'] in ('draft', 'preflight_failed', 'computed', 'reviewed')


ACTION_OK = {
    'lock_attendance': 'Attendance locked for the period.', 'compute': 'Payslips calculated.', 'review': 'Marked as reviewed.',
    'approve': 'Run approved.', 'lock': 'Run locked. Attendance for the period is now read-only.', 'publish': 'Payslips published and employees notified.',
}


@require_POST
@require_module('payroll')
def run_action(request, id, action):
    user = request.hajiri_user
    rid = str(id)
    try:
        if action == 'preflight':
            pf = api.run_preflight(user, rid)
            r = render(request, 'payroll/partials/preflight.html', {'pf': _pf_ctx(pf), 'run': api.run_detail(user, rid)['run'],
                                                                    'is_hr': has_role(user, 'hr'),
                                                                    'can_compute': run_status_can_compute(rid, user)})
            return toast(request, r, 'Pre-flight checks complete.')
        if action == 'lock_attendance':
            api.run_lock_attendance(user, rid)
        elif action == 'compute':
            api.compute(user, rid, {})
        elif action == 'review':
            notes = _str(request.POST.get('notes'))
            api.review(user, rid, {'notes': notes} if notes else {})
        elif action == 'approve':
            api.approve(user, rid)
        elif action == 'lock':
            api.lock(user, rid)
        elif action == 'publish':
            api.publish(user, rid)
        else:
            raise NotFoundError('not found')
    except HttpError as e:
        return failed(request, e, 'Not saved')
    stay = {'lock_attendance': 0, 'publish': 4}.get(action)
    return done(request, ACTION_OK[action], url=f'/payroll/runs/{rid}' + (f'?step={stay}' if stay is not None else ''))


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def run_override(request, id):
    if request.method == 'GET':
        return render(request, 'payroll/partials/override_form.html', {'run_id': id})
    try:
        api.compute(request.hajiri_user, str(id), {'overrideReason': _str(request.POST.get('reason'))})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Payslips calculated with an override.', url=f'/payroll/runs/{id}?step=2')


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def input_new(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        comps = [c for c in api.list_components(user)['components'] if c['active'] and c['type'] != 'statistical']
        return render(request, 'payroll/partials/input_form.html', {'run_id': id, 'components': comps})
    p = request.POST
    item = {'employeeId': p.get('employee_id') or '', 'componentCode': p.get('component_code') or 'ALLOWANCE', 'amount': _str(p.get('amount'))}
    if _str(p.get('note')):
        item['note'] = _str(p.get('note'))
    try:
        api.add_inputs(user, str(id), {'inputs': [item]})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return hx_redirect(request, f'/payroll/runs/{id}?step=1')


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def hold_new(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        slips = api.run_detail(user, str(id))['payslips']
        held = {h['employee_id'] for h in compliance.list_holds(user, {'status': 'open'})['holds'] if h['run_id'] == str(id)}
        options = [(s['employee_id'], f"{s['name_en']} ({s['emp_code']}) · {money(s['net'])}") for s in slips
                   if s['employee_id'] not in held and _dec(s['net']) > 0]
        return render(request, 'payroll/partials/hold_form.html', {'run_id': id, 'options': options})
    try:
        compliance.hold_pay(user, str(id), {'employeeId': request.POST.get('employee_id') or '', 'reason': _str(request.POST.get('reason'))})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Pay held. Release it under Payroll, Held pay once the issue is settled.', url=f'/payroll/runs/{id}?step=2')


@require_GET
@require_module('payroll')
def salary_sheet_file(request, id, fmt):
    if fmt not in ('csv', 'html'):
        raise NotFoundError('not found')
    return sheet_response(*api.salary_sheet_for(request.hajiri_user, str(id), {'format': fmt}))


@require_GET
@require_module('payroll')
def ssf_sheet_file(request, id):
    name, text = api.ssf_sheet(request.hajiri_user, str(id))
    return csv_download(name, text)


# ---------------------------------------------------------------- Salary structures (StructuresPage)

@require_GET
@require_module('payroll')
def structures(request):
    user = request.hajiri_user
    comps = api.list_components(user)['components']
    structs = api.list_structures(user)['structures']
    for st in structs:
        st['versions'] = st.get('versions') or []
        for v in st['versions']:
            v['codes'] = ', '.join(c['code'] for c in v['components'])
    labels = dict(COMPONENT_TYPES)
    for c in comps:
        c['type_label'] = labels.get(c['type'], c['type'])
    tab = request.GET.get('tab') if request.GET.get('tab') in ('components', 'structures') else 'components'
    return render(request, 'payroll/structures.html', {'components': comps, 'structures': structs, 'tab': tab})


def _component_body(p, *, is_new: bool, statutory: bool) -> dict:
    b = {'nameEn': p.get('name_en') or '', 'condition': p.get('condition') or '', 'dependsOnPaymentDays': p.get('depends') == 'on',
         'taxable': p.get('taxable') == 'on', 'rounding': p.get('rounding') or 'paisa', 'sortOrder': _int(p.get('sort_order'), 0),
         'active': p.get('active') == 'on', 'nameNe': p.get('name_ne') or ''}
    if is_new:
        b['code'] = re.sub(r'[^A-Z0-9_]', '', (p.get('code') or '').upper())
    if not statutory:
        # Statutory components keep their type and formula: the fields are disabled, so the browser does not send them.
        b['type'] = p.get('type') or 'earning'
        b['formula'] = p.get('formula') or ''
    return b


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def component_new(request):
    if request.method == 'GET':
        return render(request, 'payroll/partials/component_form.html', {
            'c': {'code': '', 'name_en': '', 'name_ne': '', 'type': 'earning', 'formula': '', 'condition': '', 'depends_on_payment_days': False,
                  'taxable': True, 'rounding': 'paisa', 'sort_order': 100, 'active': True}, 'is_new': True, 'types': COMPONENT_TYPES})
    try:
        api.create_component(request.hajiri_user, _component_body(request.POST, is_new=True, statutory=False))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Component saved.', url='/payroll/structures')


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def component_edit(request, code):
    user = request.hajiri_user
    c = next((x for x in api.list_components(user)['components'] if x['code'] == code), None)
    if not c:
        raise NotFoundError('component not found')
    if request.method == 'GET':
        return render(request, 'payroll/partials/component_form.html', {'c': c, 'is_new': False, 'types': COMPONENT_TYPES})
    try:
        api.update_component(user, code, _component_body(request.POST, is_new=False, statutory=bool(c['statutory'])))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Component saved.', url='/payroll/structures')


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def structure_new(request):
    user = request.hajiri_user
    structs = api.list_structures(user)['structures']
    if request.method == 'GET':
        code = request.GET.get('code') or ''
        st = next((x for x in structs if x['code'] == code), None)
        picked = [c['code'] for c in (st.get('versions') or [{}])[-1].get('components', [])] if st else ['BASIC']
        return render(request, 'payroll/partials/structure_form.html', {
            'code': code, 'name_en': (st or {}).get('name_en', ''), 'picked': picked,
            'components': [c['code'] for c in api.list_components(user)['components']],
            'existing_codes': [x['code'] for x in structs]})
    p = request.POST
    code = (p.get('code') or '').upper()
    picked = [{'code': c} for c in p.getlist('components')]
    try:
        existing = next((x for x in structs if x['code'] == code), None)
        if existing:
            api.add_structure_version(user, existing['id'], {'components': picked})
        else:
            api.create_structure(user, {'code': code, 'nameEn': p.get('name_en') or '', 'components': picked})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Structure saved.', url='/payroll/structures?tab=structures')


# ---------------------------------------------------------------- Employee salaries (AssignmentsPage)

def valid_label(rng: str) -> str:
    """"[2026-01-01,2026-04-01)" -> "<BS> to <BS>"; open-ended ranges read "From <BS>"."""
    m = re.match(r'^([\[(])(\d{4}-\d{2}-\d{2})?,\s*(\d{4}-\d{2}-\d{2})?([)\]])$', (rng or '').strip())
    if not m:
        return rng
    frm = (add_days(m.group(2), 1) if m.group(1) == '(' else m.group(2)) if m.group(2) else None
    to = (add_days(m.group(3), -1) if m.group(4) == ')' else m.group(3)) if m.group(3) else None
    if frm and to:
        return f'{fmt_date(frm)} to {fmt_date(to)}'
    if frm:
        return f'From {fmt_date(frm)}'
    return f'Until {fmt_date(to)}' if to else 'Always'


def _versions(user) -> list[dict]:
    out = []
    for st in api.list_structures(user)['structures']:
        for v in st.get('versions') or []:
            out.append({'id': v['id'], 'label': f"{st['code']} v{v['version']}", 'codes': [c['code'] for c in v['components']]})
    return out


@require_GET
@require_module('payroll')
def assignments(request):
    user = request.hajiri_user
    emp = request.GET.get('employee_id') or request.GET.get('employeeId') or None
    rows = api.list_assignments(user, {'employeeId': emp} if emp else {})['assignments']
    for a in rows:
        a['valid_label'] = valid_label(a['valid'])
        a['amounts'] = [(k, fmt_money(v, currency=False)) for k, v in (a['base_amounts'] or {}).items()]
    ctx = {'assignments': rows, 'employee_id': emp}
    if request.headers.get('HX-Target') == 'assignments':
        return render(request, 'payroll/partials/assignments_table.html', ctx)
    return render(request, 'payroll/assignments.html', ctx)


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def assignment_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'payroll/partials/assignment_form.html', {
            'versions': _versions(user), 'employee_id': request.GET.get('employee_id') or '', 'today': today_npt()})
    p = request.POST
    base = {k[len('amount_'):]: _str(v) for k, v in p.items() if k.startswith('amount_') and AMOUNT_RE.match(_str(v))}
    try:
        api.create_assignment(user, {'employeeId': p.get('employee_id') or '', 'structureVersionId': p.get('structure_version_id') or '',
                                     'from': p.get('from') or '', 'baseAmounts': base})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Salary assigned.', url=_current_url(request, '/payroll/assignments'))


@require_GET
@require_module('payroll')
def assignment_amounts(request):
    v = next((x for x in _versions(request.hajiri_user) if x['id'] == request.GET.get('structure_version_id')), None)
    return render(request, 'payroll/partials/assignment_amounts.html', {'codes': v['codes'] if v else []})


# ---------------------------------------------------------------- Tax declarations (DeclarationsPage)

DECL_INPUTS = [
    ('lifePremium', 'life_premium', 'Life insurance premium', 'Deductible up to NPR 40,000 a year'),
    ('healthPremium', 'health_premium', 'Health insurance premium', 'Deductible up to NPR 20,000 a year'),
    ('citMonthly', 'cit_monthly', 'CIT contribution (monthly)', 'Counts toward the retirement limit: the lower of one third of income or NPR 500,000'),
    ('otherRetirementAnnual', 'other_retirement_annual', 'Other approved retirement fund', 'Shares the CIT retirement limit'),
    ('medicalCost', 'medical_cost', 'Approved medical costs', '15% tax credit, up to NPR 1,500'),
    ('donation', 'donation', 'Donations to approved organisations',
     'Deductible up to NPR 1,00,000 or 5% of taxable income (Sec 12); confirm with your accountant'),
]


def _plain(v) -> str:
    d = _dec(v)
    s = f'{d:f}'
    return (s.rstrip('0').rstrip('.') if '.' in s else s) or '0'


def _decl_fields(source: dict | None, inputs=DECL_INPUTS) -> list[dict]:
    return [{'key': key, 'name': col, 'label': label, 'hint': hint, 'value': _plain((source or {}).get(col))} for key, col, label, hint in inputs]


@require_GET
@require_module('payroll')
def declarations(request):
    user = request.hajiri_user
    cur = current_fy()
    fy = _int(request.GET.get('fy'), cur)
    is_verifier = has_role(user, 'hr')
    employee_id = (request.GET.get('employee_id') if is_verifier and 'employee_id' in request.GET else user.employee_id) or None
    ctx = {'fy': fy, 'fy_options': fy_options([cur - 2, cur - 1, cur, cur + 1]), 'employee_id': employee_id, 'is_verifier': is_verifier}
    if employee_id:
        d = api.get_declaration(user, {'employeeId': employee_id, 'fiscalYearStart': str(fy)})
        pending, decl = d['pending'], d['declaration']
        status = 'submitted' if pending else (decl or {}).get('status')
        ctx.update({'pending': pending, 'declaration': decl, 'status': status, 'fields': _decl_fields(pending or decl),
                    'can_verify': is_verifier and status == 'submitted' and employee_id != user.employee_id,
                    'for_someone_else': is_verifier and employee_id != user.employee_id})
    if request.headers.get('HX-Target') == 'declaration':
        return render(request, 'payroll/partials/declaration.html', ctx)
    return render(request, 'payroll/declarations.html', ctx)


@require_POST
@require_module('payroll')
def declaration_save(request):
    user = request.hajiri_user
    p = request.POST
    employee_id = p.get('employee_id') or user.employee_id
    body = {'fiscalYearStart': _int(p.get('fy')), 'submit': True, **{key: _num_str(p.get(col)) for key, col, _l, _h in DECL_INPUTS}}
    if employee_id != user.employee_id:
        body['employeeId'] = employee_id
    try:
        api.put_declaration(user, body)
    except HttpError as e:
        return failed(request, e, 'Not saved')
    msg = ('Declaration saved and verified. Payroll uses it from the next run.' if employee_id != user.employee_id
           else 'Declaration sent to HR for verification.')
    return done(request, msg, refresh_id='declaration')


@require_POST
@require_module('payroll')
def declaration_verify(request):
    p = request.POST
    decision = 'verified' if p.get('decision') == 'verified' else 'rejected'
    try:
        api.verify_declaration(request.hajiri_user, p.get('employee_id') or '', {'fiscalYearStart': _int(p.get('fy')), 'decision': decision})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    if decision == 'verified':
        return done(request, 'Declaration verified.', refresh_id='declaration')
    return refresh(HttpResponse(status=204), 'declaration')


# ---------------------------------------------------------------- Loans & advances (LoansPage)

@require_GET
@require_module('payroll')
def loans(request):
    user = request.hajiri_user
    emp = request.GET.get('employee_id') or request.GET.get('employeeId') or None
    rows = api.list_loans(user, {'employeeId': emp} if emp else {})['loans']
    for ln in rows:
        ln['kind_label'] = {'advance': 'Advance', 'loan': 'Loan'}.get(ln['kind'], ln['kind'])
        ln['start_label'] = bs_month_label(ln['start_bs_year'], ln['start_bs_month'])
    ctx = {'loans': rows, 'employee_id': emp}
    if request.headers.get('HX-Target') == 'loans':
        return render(request, 'payroll/partials/loans_table.html', ctx)
    return render(request, 'payroll/loans.html', ctx)


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def loan_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        b = current_bs()
        return render(request, 'payroll/partials/loan_form.html', {'y': b.year, 'm': b.month, 'months': list(enumerate(BS_MONTH_NAMES_EN, 1))})
    p = request.POST
    kind = p.get('kind') or 'loan'
    body = {'employeeId': p.get('employee_id') or '', 'kind': kind, 'principal': _num_str(p.get('principal')), 'emi': _num_str(p.get('emi')),
            'startBsYear': _int(p.get('start_bs_year')), 'startBsMonth': _int(p.get('start_bs_month'))}
    if _str(p.get('note')):
        body['note'] = _str(p.get('note'))
    try:
        r = api.create_loan(user, body)
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, f"{'Advance' if kind == 'advance' else 'Loan'} created with {r['installments']} instalments.",
                url=_current_url(request, '/payroll/loans'))


# ---------------------------------------------------------------- Tax & SSF rules (RulesPage)

def _effective(r: dict) -> str:
    return f"{fmt_date(r['valid_from_ad'])} to {fmt_date(r['valid_to_ad'])}" if r.get('valid_to_ad') else f"From {fmt_date(r['valid_from_ad'])}"


@require_GET
@require_module('payroll')
def rules(request):
    rows = api.list_rules(request.hajiri_user, {})['rules']
    for r in rows:
        r['effective'] = _effective(r)
    return render(request, 'payroll/rules.html', {'rules': rows, 'is_hr': has_role(request.hajiri_user, 'hr')})


@require_GET
@require_module('payroll')
def rule_view(request, id):
    r = next((x for x in api.list_rules(request.hajiri_user, {})['rules'] if x['id'] == str(id)), None)
    if not r:
        raise NotFoundError('rule not found')
    return render(request, 'payroll/partials/rule_view.html', {'r': {**r, 'effective': _effective(r)},
                                                               'value': json.dumps(r['value'], indent=2, ensure_ascii=False)})


@require_POST
@require_module('payroll')
def rule_approve(request, id):
    try:
        api.approve_rule(request.hajiri_user, str(id))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Rule approved.', url='/payroll/rules')


# ---------------------------------------------------------------- Payroll reports (PayrollReportsPage)

def report_table(columns: list[dict], rows: list[dict]) -> dict:
    """ReportTable: money columns right-aligned, totals for money columns at the bottom."""
    cols = [c for c in columns if c['key'] != 'employeeId']

    def is_money(k):
        return bool(MONEY_KEYS.match(k))
    body = [[{'text': fmt_money(r.get(c['key']), currency=False) if is_money(c['key']) and r.get(c['key']) not in ('', None)
              else ('' if r.get(c['key']) is None else r.get(c['key'])), 'money': is_money(c['key'])} for c in cols] for r in rows]
    foot = []
    for i, c in enumerate(cols):
        if i == 0:
            foot.append({'text': f'Total ({len(rows)})', 'money': is_money(c['key'])})
        elif is_money(c['key']) and c['key'] not in ('dailyRate', 'percent'):
            foot.append({'text': fmt_money(sum((_dec(r.get(c['key'])) for r in rows), Decimal(0)), currency=False), 'money': True})
        else:
            foot.append({'text': '', 'money': is_money(c['key'])})
    return {'cols': [{**c, 'money': is_money(c['key'])} for c in cols], 'rows': body, 'foot': foot}


def _report_ctx(request, kind: str) -> dict:
    user = request.hajiri_user
    g = request.GET
    b = current_bs()
    cur = current_fy()
    ctx: dict = {'kind': kind, 'kinds': REPORT_KINDS, 'fy_options': fy_options([cur - 2, cur - 1, cur])}
    if kind in ('tax', 'funds'):
        y = _int(g.get('bs_year'), b.year)
        m = _int(g.get('bs_month'), 12 if b.month == 1 else b.month - 1)
        fn = compliance.tax_deposit if kind == 'tax' else compliance.retirement_deposit
        report = 'tax-deposit' if kind == 'tax' else 'retirement-deposit'
        try:
            res = fn(user, {'bsYear': str(y), 'bsMonth': str(m)})
            ctx['table'] = report_table(res['columns'], res['rows'])
        except HttpError as e:
            ctx['error'] = e.message
        ctx.update({'y': y, 'm': m, 'month_label': bs_month_label(y, m),
                    'csv_url': f'/api/v1/payroll/reports/{report}?bsYear={y}&bsMonth={m}&format=csv'})
    elif kind == 'variance':
        regular = [r for r in api.list_runs(user)['runs'] if r['run_type'] == 'regular' and r['computed_at']]
        opts = [(r['id'], (bs_month_label(r['bs_year'], r['bs_month']) if r['bs_year'] and r['bs_month'] else str(r['created_at'])[:10])
                 + ('' if r['locked_at'] else ' (not locked)')) for r in regular]
        rid = g.get('run_id') if g.get('run_id') in {o[0] for o in opts} else (opts[0][0] if opts else None)
        ctx.update({'run_options': opts, 'run_id': rid})
        if rid:
            res = compliance.pay_variance(user, {'runId': rid})
            ctx['previous_label'] = bs_month_label(res['previous']['bsYear'], res['previous']['bsMonth']) if res['previous'] else None
            cols = [{'key': 'empCode', 'header': 'Code'}, {'key': 'name', 'header': 'Name'}, {'key': 'previousNet', 'header': 'Previous net'},
                    {'key': 'net', 'header': 'Net'}, {'key': 'difference', 'header': 'Difference'}, {'key': 'percent', 'header': 'Change %'},
                    {'key': 'reasons', 'header': 'Why'}]
            ctx['table'] = report_table(cols, res['rows'])
            ctx['csv_url'] = f'/api/v1/payroll/reports/pay-variance?runId={rid}&format=csv'
    elif kind == 'liability':
        as_of = g.get('as_of') or today_npt()
        res = compliance.leave_liability(user, {'asOf': as_of})
        cols = [{'key': 'empCode', 'header': 'Code'}, {'key': 'name', 'header': 'Name'}, {'key': 'department', 'header': 'Department'},
                {'key': 'leaveType', 'header': 'Leave type'}, {'key': 'balance', 'header': 'Days'}, {'key': 'dailyRate', 'header': 'Daily rate'},
                {'key': 'amount', 'header': 'Amount'}]
        ctx.update({'as_of': as_of, 'divisor': res['divisor'], 'table': report_table(cols, res['rows']),
                    'stats': [{'label': 'Total liability', 'value': money0(res['total'])},
                              *[{'label': d['department'], 'value': money0(d['amount'])} for d in res['byDepartment'][:3]]],
                    'csv_url': f'/api/v1/payroll/reports/leave-liability?asOf={as_of}&format=csv'})
    elif kind == 'annual':
        fy = _int(g.get('fy'), cur)
        res = compliance.annual_tax(user, {'fiscalYearStart': str(fy)})
        ctx.update({'fy': fy, 'table': report_table(res['columns'], res['rows']),
                    'csv_url': f'/api/v1/payroll/reports/annual-tax?fiscalYearStart={fy}&format=csv'})
    else:
        ctx['fy'] = _int(g.get('fy'), cur)
    return ctx


@require_GET
@require_module('payroll')
def reports(request):
    kind = request.GET.get('kind') if request.GET.get('kind') in dict(REPORT_KINDS) else 'tax'
    return render(request, 'payroll/reports.html', _report_ctx(request, kind))


@require_GET
@require_module('payroll')
def report(request, kind):
    if kind not in dict(REPORT_KINDS):
        raise NotFoundError('not found')
    return render(request, 'payroll/partials/report.html', _report_ctx(request, kind))


@require_GET
@login_required
def certificate(request, employee):
    """The printable tax or salary certificate (MyCertificates and the Reports tab open it in a new tab)."""
    qd = request.GET.dict()
    picked = qd.pop('employee_id', '')
    if employee == 'pick':
        employee = picked
        if not employee:
            raise BadRequestError('choose an employee')
    qd = {k: v for k, v in qd.items() if v != ''}
    return html_response(compliance.certificate(request.hajiri_user, employee, qd))


# ---------------------------------------------------------------- Optimum CIT (OptimumCitPage, OptimumCitCard)

def _cit_months() -> list[tuple[int, str]]:
    b = current_bs()
    out, m = [], b.month
    for _ in range(12):
        out.append((m, BS_MONTH_NAMES_EN[m - 1]))
        if m == 3:
            break
        m = 1 if m == 12 else m + 1
    return out


@require_GET
@require_module('payroll')
def optimum(request):
    res = compliance.optimum_all(request.hajiri_user)
    rows = sorted(res['employees'], key=lambda r: _dec(r.get('annualSaving')), reverse=True)
    with_saving = [r for r in rows if _dec(r.get('annualSaving')) > 0]
    total = sum((_dec(r['annualSaving']) for r in with_saving), Decimal(0))
    for r in rows:
        r['saving'] = _dec(r.get('annualSaving')) > 0
        r['note'] = CIT_REASON.get(r.get('reason') or '', '') if r['available'] else ''
        for k in ('currentMonthly', 'optimumMonthly', 'optimumAnnualTax', 'currentAnnualTax', 'annualSaving'):
            r[f'{k}_l'] = money0(r.get(k)) if r.get(k) is not None else ''
    stats = [{'label': 'Can save tax', 'value': str(len(with_saving)), 'hint': f'of {len(rows)} employees'},
             {'label': 'Tax that could be saved', 'value': money0(total), 'hint': f"FY {res['fiscalYearStart']}",
              **({'tone': 'ok'} if total > 0 else {})}]
    return render(request, 'payroll/optimum_cit.html', {'rows': rows, 'stats': stats})


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def optimum_apply(request, employee_id):
    user = request.hajiri_user
    cid = company_id_of(user)
    emp = compliance.assert_employee(cid, str(employee_id))
    if request.method == 'GET':
        row = compliance.optimum_for(cid, str(employee_id), tax_savings.current_fiscal_year_start())
        return render(request, 'payroll/partials/cit_form.html', {
            'employee_id': employee_id, 'name': emp['name_en'], 'amount': _plain(row.get('optimumMonthly')),
            'saving': money0(row.get('annualSaving')), 'months': _cit_months(), 'month': current_bs().month})
    p = request.POST
    month = _int(p.get('from_bs_month'))
    amount = _num_str(p.get('monthly_cit'))
    try:
        compliance.apply_cit(user, {'employeeId': str(employee_id), 'monthlyCit': amount, **({'fromBsMonth': month} if month else {})})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    m_name = BS_MONTH_NAMES_EN[(month or current_bs().month) - 1]
    return done(request, f"{emp['name_en']}'s CIT is now {money0(amount)} a month from {m_name}. They were notified.", url='/payroll/optimum-cit')


@require_GET
@login_required
def optimum_card(request):
    """OptimumCitCard on Tax savings: the CIT that would bring this year's tax lowest."""
    try:
        o = compliance.optimum_me(request.hajiri_user)
    except HttpError:
        return HttpResponse('')
    if not o.get('available'):
        return HttpResponse('')
    keys = ('annualSaving', 'optimumMonthly', 'currentMonthly', 'currentAnnualTax', 'optimumAnnualTax', 'deductionLimit')
    return render(request, 'payroll/partials/optimum_card.html', {
        'o': o, 'saving': _dec(o.get('annualSaving')) > 0, 'm': {k: money0(o.get(k)) for k in keys},
        'months_word': 'month' if o.get('remainingMonths') == 1 else 'months', 'optimum_plain': _plain(o.get('optimumMonthly'))})


# ---------------------------------------------------------------- Held pay (HeldPayPage)

@require_GET
@require_module('payroll')
def held_pay(request):
    tab = request.GET.get('status') if request.GET.get('status') in ('open', 'released', 'cancelled') else 'open'
    rows = compliance.list_holds(request.hajiri_user, {'status': tab})['holds']
    for h in rows:
        h['status_label'], h['status_tone'] = HOLD_STATUS.get(h['status'], (h['status'], 'idle'))
        h['held_in'] = bs_month_label(h['bs_year'], h['bs_month'])
        h['release_in'] = bs_month_label(h['release_bs_year'], h['release_bs_month']) if h.get('release_bs_year') else None
    return render(request, 'payroll/held_pay.html', {'holds': rows, 'tab': tab,
                                                     'tabs': [('open', 'Open'), ('released', 'Released'), ('cancelled', 'Cancelled')]})


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def hold_release(request, id):
    user = request.hajiri_user
    h = next((x for x in compliance.list_holds(user, {'status': 'held'})['holds'] if x['id'] == str(id)), None)
    if request.method == 'GET':
        if not h:
            raise NotFoundError('hold not found')
        return render(request, 'payroll/partials/release_form.html', {'h': h, 'held_in': bs_month_label(h['bs_year'], h['bs_month']),
                                                                       'amount': fmt_money(h['amount'], currency=False)})
    mode = 'outside' if request.POST.get('mode') == 'outside' else 'payroll'
    note = _str(request.POST.get('note'))
    try:
        compliance.release_hold(user, str(id), {'mode': mode, **({'note': note} if note else {})})
    except HttpError as e:
        return failed(request, e, 'Not saved')
    name = (h or {}).get('name_en', '')
    return done(request, f"{name}'s held pay will be paid in the next pay run." if mode == 'payroll' else 'Recorded as paid outside payroll.',
                url=_current_url(request, '/payroll/held-pay'))


@require_POST
@require_module('payroll')
def hold_cancel(request, id):
    try:
        compliance.cancel_hold(request.hajiri_user, str(id))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Hold removed.', url=_current_url(request, '/payroll/held-pay'))


# ---------------------------------------------------------------- Opening balances (OpeningBalancesPage)

OPENING_NUMS = [('gross', 'Gross pay', 'Everything paid in the year before Hajiri'),
                ('taxable', 'Taxable pay', "Including the employer's SSF/PF share if it was taxed"),
                ('tds_paid', 'Tax deducted (TDS and SST)', None)]
OPENING_RET = [('retirement_employee', 'Retirement, employee', 'SSF/PF employee share and CIT'),
               ('retirement_employer', 'Retirement, employer', 'SSF/PF employer share and gratuity')]


@require_GET
@require_module('payroll')
def openings(request):
    cur = current_fy()
    fy = _int(request.GET.get('fy'), cur)
    res = compliance.list_openings(request.hajiri_user, {'fiscalYearStart': str(fy)})
    for r in res['balances']:
        r['source_label'] = OPENING_SOURCE.get(r['source'], r['source'])
        r['ret_label'] = f"{fmt_money(r['retirement_employee'], decimals=0)} / {fmt_money(r['retirement_employer'], decimals=0)}"
    return render(request, 'payroll/opening_balances.html', {'balances': res['balances'], 'fy': fy, 'fy_str': str(fy),
                                                             'fy_options': fy_options([cur - 1, cur, cur + 1])})


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def opening_edit(request):
    user = request.hajiri_user
    if request.method == 'GET':
        fy = _int(request.GET.get('fy'), current_fy())
        emp = request.GET.get('employee_id') or ''
        row = next((r for r in compliance.list_openings(user, {'fiscalYearStart': str(fy)})['balances'] if r['employee_id'] == emp), None) if emp else None
        v = row or {'employee_id': '', 'gross': '0', 'taxable': '0', 'tds_paid': '0', 'retirement_employee': '0', 'retirement_employer': '0',
                    'festival_paid': False, 'months': 0, 'source': 'previous_employer', 'note': ''}
        return render(request, 'payroll/partials/opening_form.html', {
            'v': v, 'fy': fy, 'sources': list(OPENING_SOURCE.items()),
            'nums': [(k, label, hint, _plain(v.get(k))) for k, label, hint in OPENING_NUMS],
            'rets': [(k, label, hint, _plain(v.get(k))) for k, label, hint in OPENING_RET]})
    p = request.POST
    body = {'employeeId': p.get('employee_id') or '', 'fiscalYearStart': _int(p.get('fy')), 'gross': _num_str(p.get('gross')),
            'taxable': _num_str(p.get('taxable')), 'tdsPaid': _num_str(p.get('tds_paid')), 'retirementEmployee': _num_str(p.get('retirement_employee')),
            'retirementEmployer': _num_str(p.get('retirement_employer')), 'festivalPaid': p.get('festival_paid') == 'on',
            'months': _int(p.get('months'), 0), 'source': p.get('source') or 'previous_employer', 'note': _str(p.get('note')) or None}
    try:
        compliance.put_opening(user, body)
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Opening balance saved. It counts from the next pay run.', url=_current_url(request, '/payroll/opening-balances'))


@require_http_methods(['GET', 'POST'])
@require_module('payroll')
def opening_import(request):
    if request.method == 'GET':
        return render(request, 'payroll/partials/opening_import.html', {'csv': OPENING_TEMPLATE, 'errors': []})
    text = request.POST.get('csv') or ''
    try:
        r = compliance.import_openings(request.hajiri_user, text)
    except BadRequestError as e:
        errs = e.details.get('errors') if isinstance(e.details, dict) else None
        if errs:
            return render(request, 'payroll/partials/opening_import.html', {'csv': text, 'errors': errs[:12], 'more': max(0, len(errs) - 12)},
                          status=422)
        return failed(request, e, 'Not saved')
    n = r['imported']
    return done(request, f"{n} opening balance{'' if n == 1 else 's'} imported.", url=_current_url(request, '/payroll/opening-balances'))


@require_POST
@require_module('payroll')
def opening_delete(request, id):
    try:
        compliance.delete_opening(request.hajiri_user, str(id))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Deleted.', url=_current_url(request, '/payroll/opening-balances'))


# ---------------------------------------------------------------- Payslips hub (PayslipsPage, PayslipDetailPage, MyTaxPage)

@require_GET
@login_required
def my_payslips(request):
    user = request.hajiri_user
    employee_id = request.GET.get('employeeId') or user.employee_id or ''
    cur = current_fy()
    fy = _int(request.GET.get('fy'), cur)
    rows, error = [], None
    if employee_id:
        try:
            rows = api.list_payslips(user, {'employeeId': employee_id, 'fiscalYearStart': str(fy)})['payslips']
        except HttpError as e:
            if e.status == 401:
                raise
            error = e.message
    for r in rows:
        r['month_label'] = bs_month_label(r['bs_year'], r['bs_month'])
        rt = r.get('run_type') or ''
        r['run_word'] = (rt[:1].upper() + rt[1:].replace('_', ' ') + ' run') if rt and rt != 'regular' else None
        r['paid'] = _plain(r['paid_days'])
        r['lop'] = _plain(r['lop_days']) if _dec(r['lop_days']) > 0 else None

    def total(k):
        return sum((_dec(r.get(k)) for r in rows), Decimal(0))
    stats = ([{'label': 'Net pay this year', 'value': money0(total('net'))}, {'label': 'Gross pay this year', 'value': money0(total('gross'))},
              {'label': 'Tax deducted', 'value': money0(total('tds'))}, {'label': 'Payslips', 'value': len(rows)}] if rows else [])
    return render(request, 'payroll/my_payslips.html', {
        'rows': rows, 'stats': stats, 'fy': fy, 'fy_options': fy_options([cur - 2, cur - 1, cur]), 'error': error,
        'own': bool(user.employee_id) and employee_id == user.employee_id, 'employee_id': employee_id,
        'other_employee': employee_id if employee_id != user.employee_id else '', 'fy_label': f'{fy}/{str(fy + 1)[-2:]}'})


@require_GET
@login_required
def payslip(request, id):
    user = request.hajiri_user
    try:
        d = api.payslip_detail(user, str(id))
    except HttpError as e:
        if e.status == 401:
            raise
        return render(request, 'payroll/payslip_missing.html', {'message': e.message}, status=e.status)
    p, lines = d['payslip'], d['lines']
    explain = {e.get('code'): e.get('text') for e in (p.get('explain') or []) if isinstance(e, dict)}

    def group(t):
        return [{**ln, 'note': explain.get(ln['code']), 'label': fmt_money(ln['amount'], currency=False)} for ln in lines
                if ln['type'] == t and _dec(ln['amount']) != 0]
    employer = group('employer_contribution')
    ytd = p.get('ytd') or {}
    items = []
    for e in p.get('explain') or []:
        items.append(str(e.get('text') or e.get('line') or json.dumps(e)) if isinstance(e, dict) else str(e))
    return render(request, 'payroll/payslip.html', {
        'p': p, 'earnings': group('earning'), 'deductions': group('deduction'), 'employer': employer,
        'employer_total': sum((_dec(ln['amount']) for ln in employer), Decimal(0)), 'title': bs_month_label(p['bs_year'], p['bs_month']),
        'paid': _plain(p['paid_days']), 'lop': _plain(p['lop_days']), 'ot': _plain(p['ot_hours']), 'ytd': ytd, 'explain_items': items})


@require_GET
@login_required
def payslip_print(request, id):
    return html_response(api.payslip_html(request.hajiri_user, str(id)))


TAX_INPUTS = [
    ('lifePremium', 'life_premium', 'Life insurance premium', 'Deductible up to NPR 40,000 a year'),
    ('healthPremium', 'health_premium', 'Health insurance premium', 'Deductible up to NPR 20,000 a year'),
    ('citMonthly', 'cit_monthly', 'Extra CIT contribution (monthly)', 'Counts toward the retirement limit with SSF/PF'),
    ('otherRetirementAnnual', 'other_retirement_annual', 'Other approved retirement fund (yearly)', 'Shares the retirement limit'),
    ('medicalCost', 'medical_cost', 'Medical costs (yearly)', '15% tax credit, up to NPR 1,500'),
    ('donation', 'donation', 'Donations (yearly)', 'To organisations approved by the tax office; up to NPR 1,00,000 or 5% of taxable income'),
]


def _history_declared(r: dict) -> str:
    parts = (('Life', 'life_premium'), ('Health', 'health_premium'), ('CIT/month', 'cit_monthly'), ('Retirement', 'other_retirement_annual'),
             ('Medical', 'medical_cost'))
    out = [f'{k} {fmt_money(r.get(col), currency=False, decimals=0)}' for k, col in parts if _dec(r.get(col)) > 0]
    return ', '.join(out) or 'Nothing'


@require_GET
@login_required
def my_tax(request):
    user = request.hajiri_user
    cur = current_fy()
    fy = _int(request.GET.get('fy'), cur)
    opts = fy_options([cur - 1, cur, cur + 1])
    try:
        data = tax_savings.get_savings(user, {'fiscalYearStart': str(fy)})
    except HttpError as e:
        if e.status == 401:
            raise
        return render(request, 'payroll/my_tax.html', {'error': e.message, 'fy': fy, 'fy_options': opts})
    pending = data['pending']
    verified = data['declaration'] if (data['declaration'] or {}).get('status') == 'verified' else None
    fields = _decl_fields(pending or verified, TAX_INPUTS)
    for h in data['history']:
        h['declared'] = _history_declared(h)
    return render(request, 'payroll/my_tax.html', {
        'fy': fy, 'fy_options': opts, 'current': fy == cur, 'pending': pending, 'verified': verified, 'fields': fields,
        'base_json': json.dumps({f['key']: f['value'] for f in fields}), 'history': data['history'], 'slots': list(range(10))})


def _tax_values(p) -> dict:
    """The six figures; a negative entry counts as 0 (Math.max(0, ...) in MyTaxPage)."""
    out = {}
    for key, col, _l, _h in TAX_INPUTS:
        v = _num_str(p.get(col))
        try:
            if Decimal(v) < 0:
                v = '0.00'
        except InvalidOperation:
            pass
        out[key] = v
    return out


@require_POST
@login_required
def tax_estimate(request):
    """The what-if estimate under the figures (MyTaxPage's StatStrip and the deductions line)."""
    user = request.hajiri_user
    p = request.POST
    fy = _int(p.get('fy'), current_fy())
    if fy != current_fy():
        return render(request, 'payroll/partials/tax_estimate.html', {'not_current': True})
    proposed = _tax_values(p)
    try:
        est = tax_savings.estimate(user, {'fiscalYearStart': fy, 'proposed': proposed})
    except HttpError:
        return HttpResponse('')
    ctx: dict = {'est': est}
    if est['available']:
        cur, prop = est['current'], est.get('proposed')
        try:
            base = json.loads(p.get('base') or '{}')
        except ValueError:
            base = {}
        changed = any(_dec(proposed[k]) != _dec(base.get(k)) for k in proposed)
        saving = _dec(est.get('annualSaving'))
        stats = [{'label': 'Projected taxable income', 'value': money0(cur['projectedAnnualTaxable']), 'hint': f'For FY {fy}/{str(fy + 1)[-2:]}'},
                 {'label': 'Tax for the year', 'value': money0(cur['annualTax']), 'hint': 'With what HR has verified'},
                 {'label': 'Tax this month', 'value': money0(cur['tdsThisMonth']), 'hint': bs_month_label(est['bsYear'], est['bsMonth'])}]
        if prop and changed:
            stats.append({'label': 'With your new figures', 'value': money0(prop['tdsThisMonth']),
                          'hint': f"a month; saves {money0(est.get('annualSaving'))} this year" if saving > 0 else 'a month',
                          **({'tone': 'ok'} if saving > 0 else {})})
            medical = f", medical credit {money0(prop['medicalCredit'])}" if _dec(prop['medicalCredit']) > 0 else ''
            ctx['line'] = (f"Deductions with these figures: insurance {money0(prop['insuranceDeduction'])}, retirement "
                           f"{money0(prop['retirementDeduction'])}{medical}. Tax for the year {money0(prop['annualTax'])} instead of "
                           f"{money0(cur['annualTax'])}.")
        ctx['stats'] = stats
    return render(request, 'payroll/partials/tax_estimate.html', ctx)


@require_POST
@login_required
def tax_submit(request):
    user = request.hajiri_user
    p = request.POST
    body = {**_tax_values(p), 'fiscalYearStart': _int(p.get('fy')), 'proofFileIds': [f for f in p.getlist('proof_file_ids') if f]}
    if _str(p.get('note')):
        body['note'] = _str(p.get('note'))
    try:
        tax_savings.submit_savings(user, body)
    except HttpError as e:
        return failed(request, e, 'Not sent')
    return done(request, 'Sent to HR. Your tax changes once they verify it.', url=_current_url(request, '/me/payslips/tax'),
                title='Tax savings submitted')


@require_POST
@login_required
def tax_cancel(request, id):
    try:
        tax_savings.cancel_savings(request.hajiri_user, str(id))
    except HttpError as e:
        return failed(request, e, 'Not saved')
    return done(request, 'Request withdrawn.', url=_current_url(request, '/me/payslips/tax'))


@require_GET
@login_required
def tax_file(request, id, file_id):
    from hajiri.files.store import stored_file_response
    f = tax_savings.proof_file(request.hajiri_user, str(id), str(file_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')
