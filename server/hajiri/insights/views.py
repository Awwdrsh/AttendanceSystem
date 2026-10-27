"""
Insights (/insights) and the Reports hub (/reports catalogue, /reports/builder) with their HTMX fragments and CSV
downloads under /_/insights/... The data comes from hajiri.insights.services (workforce trends) and hajiri.reports
(attendance and People record reports, the ad-hoc builder and saved reports).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from django.http import HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from engines.bs_calendar import ad_to_bs

from hajiri.core.auth import company_id_of
from hajiri.core.clock import npt_today
from hajiri.core.db import rows
from hajiri.core.errors import BadRequestError, HttpError, NotFoundError
from hajiri.reports import service as reports
from hajiri.reports.csv import csv_response, table_response
from hajiri.reports.people import people_report
from hajiri.web import charts as ch
from hajiri.web.decorators import require_module, require_role
from hajiri.web.format import add_days, fmt_date, fmt_money, fmt_num
from hajiri.web.htmx import close_modal, flash, is_htmx, toast
from hajiri.web.middleware import error_message
from hajiri.web.nav import feature_on

from . import catalog, services

VIZ = [f'var(--hj-viz-{i})' for i in range(1, 9)]


def _today() -> str:
    return npt_today().isoformat()


# ================================================================ insights

RANGES = (('6', '6 months'), ('12', '12 months'), ('24', '24 months'))


def range_of(months: int, today: str | None = None) -> dict:
    """The last `months` BS months up to this one, as YYYY-MM keys (InsightsPage rangeOf)."""
    now = ad_to_bs(today or _today())
    y, m = now.year, now.month
    to = f'{y}-{m:02d}'
    for _ in range(1, months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return {'from': f'{y}-{m:02d}', 'to': to}


def _unit_text(v, unit: str) -> str:
    if v is None:
        return '—'
    if unit == 'NPR':
        return fmt_money(v, decimals=0)
    if unit == '%':
        return f'{fmt_num(v)}%'
    if unit == 'h':
        return f'{fmt_num(v)} h'
    if unit == 'days':
        return f'{fmt_num(v)} d'
    return fmt_num(v)


def kpi_stats(d: dict) -> list[dict]:
    """The headline tiles with the change against the month before (kpiStats)."""
    prev_label = d['months'][-2]['label'].split(' ')[0] if len(d['months']) > 1 else None
    out = []
    for k in d['kpis']:
        delta = k['delta']
        tone = None
        if not (delta is None or delta == 0 or k['goodWhen'] == 'neutral'):
            tone = 'ok' if (k['goodWhen'] == 'down') == (delta < 0) else 'warn'
        vs = k.get('vs') or prev_label
        hint = None
        if delta is not None and vs:
            sign = '+' if delta > 0 else '−' if delta < 0 else '±'
            mag = f'{fmt_num(abs(delta))} pts' if k['unit'] == '%' else _unit_text(abs(delta), 'NPR' if k['unit'] == 'NPR' else 'h' if k['unit'] == 'h' else '')
            hint = f'{sign}{mag} vs {vs}'
        s = {'key': k['key'], 'label': k['label'], 'value': _unit_text(k['value'], k['unit'])}
        if hint:
            s['hint'] = hint
        if tone:
            s['tone'] = tone
        out.append(s)
    return out


def _card(title, dataset, kind, geo, *, subtitle=None, legend=None, table=None, note=None, empty=None, empty_hint=None):
    return {'title': title, 'dataset': dataset, 'template': f'components/charts/{kind}.html', 'c': geo, 'subtitle': subtitle,
            'legend': legend or [], 'table': table, 'note': note, 'empty': empty, 'empty_hint': empty_hint}


def insight_cards(d: dict) -> tuple[list[dict], list[dict]]:
    """The two grids of chart cards on the Insights page, in the TS order."""
    labels = [m['short'] for m in d['months']]
    money = lambda n: fmt_money(n, decimals=0)  # noqa: E731
    no_cost = all(not x['locked'] for x in d['cost'])
    hc, att, ot, cost = d['headcount'], d['attendance'], d['overtime'], d['cost']
    split = d['attrition']['split']
    joined = sum(h['joins'] for h in hc)
    main = [
        _card('Headcount', 'headcount', 'column',
              ch.column_chart(labels, [{'key': 'hc', 'label': 'Headcount', 'values': [h['headcount'] for h in hc]}], aria_label='Headcount by month'),
              subtitle='Active employees at the end of each month',
              table={'columns': ['Month', 'Headcount', 'Joined', 'Left'], 'rows': [[h['label'], h['headcount'], h['joins'], h['exits']] for h in hc]}),
        _card('Joiners and leavers', 'headcount', 'column',
              ch.column_chart(labels, [{'key': 'j', 'label': 'Joined', 'values': [h['joins'] for h in hc], 'color': VIZ[0]},
                                       {'key': 'x', 'label': 'Left', 'values': [h['exits'] for h in hc], 'color': VIZ[2]}],
                              aria_label='Joiners and leavers by month'),
              subtitle=f"{joined} joined, {d['attrition']['exits']} left in this period",
              legend=[{'label': 'Joined', 'color': VIZ[0]}, {'label': 'Left', 'color': VIZ[2]}],
              table={'columns': ['Month', 'Joined', 'Left', 'Net'], 'rows': [[h['label'], h['joins'], h['exits'], h['net']] for h in hc]}),
        _card('Attrition', 'headcount', 'line',
              ch.line_chart(labels, [{'key': 'a', 'label': 'Attrition', 'values': [h['attritionPct'] for h in hc]}], suffix='%',
                            aria_label='Monthly attrition rate'),
              subtitle=(f"{fmt_num(d['attrition']['monthlyAvgPct'])}% a month on average, {fmt_num(d['attrition']['annualisedPct'])}% a year"
                        + (f" · {split['voluntary']} resigned, {split['involuntary']} let go" if split else '')),
              table={'columns': ['Month', 'Attrition %', 'Left', *(['Voluntary', 'Involuntary'] if split else [])],
                     'rows': [[h['label'], h['attritionPct'], h['exits'], *([h['voluntary'], h['involuntary']] if split else [])] for h in hc]},
              note=None if d['exitReasonsAvailable'] else 'Record exit reasons to see voluntary and involuntary leavers separately.'),
        _card('People cost', 'cost', 'column',
              ch.column_chart(labels, [{'key': 'g', 'label': 'Gross pay', 'values': [x['gross'] for x in cost], 'color': VIZ[0]},
                                       {'key': 'e', 'label': 'Employer contributions', 'values': [x['employerContributions'] for x in cost], 'color': VIZ[1]}],
                              stacked=True, money=True, format=money, aria_label='People cost by month'),
              subtitle='Gross pay plus employer SSF, PF and gratuity, from locked pay runs',
              legend=[{'label': 'Gross pay', 'color': VIZ[0]}, {'label': 'Employer contributions', 'color': VIZ[1]}],
              table={'columns': ['Month', 'Gross', 'Employer', 'Total', 'People paid'],
                     'rows': [[x['label'], x['gross'], x['employerContributions'], x['totalCost'], x['people']] for x in cost]},
              empty='No locked pay runs in this period' if no_cost else None,
              empty_hint='Costs appear once a pay run is approved and locked.'),
        _card('Absenteeism and late arrivals', 'attendance', 'line',
              ch.line_chart(labels, [{'key': 'abs', 'label': 'Absent', 'values': [a['absenteeismPct'] for a in att], 'color': VIZ[2]},
                                     {'key': 'late', 'label': 'Late', 'values': [a['latePct'] for a in att], 'color': VIZ[3]}],
                            suffix='%', aria_label='Absenteeism and late arrival rates'),
              subtitle='Share of working days absent, and of days worked that started late',
              legend=[{'label': 'Absent', 'color': VIZ[2]}, {'label': 'Late', 'color': VIZ[3]}],
              table={'columns': ['Month', 'Absent %', 'Late %', 'Absent days', 'Late days'],
                     'rows': [[a['label'], a['absenteeismPct'], a['latePct'], a['absentDays'], a['lateDays']] for a in att]}),
        _card('Average working day', 'attendance', 'line',
              ch.line_chart(labels, [{'key': 'h', 'label': 'Average hours', 'values': [a['avgWorkedHours'] for a in att]}], suffix=' h',
                            aria_label='Average hours worked per day'),
              subtitle='Hours worked on days with time recorded',
              table={'columns': ['Month', 'Average hours', 'Days worked'], 'rows': [[a['label'], a['avgWorkedHours'], a['presentDays']] for a in att]}),
        _card('Overtime', 'overtime', 'column',
              ch.column_chart(labels, [{'key': 'd', 'label': 'Detected', 'values': [o['detectedHours'] for o in ot], 'color': VIZ[1]},
                                       {'key': 'a', 'label': 'Approved', 'values': [o['approvedHours'] for o in ot], 'color': VIZ[0]}],
                              format=lambda n: f'{fmt_num(n)} h', aria_label='Overtime hours by month'),
              subtitle='Hours detected by attendance, and hours approved for pay',
              legend=[{'label': 'Detected', 'color': VIZ[1]}, {'label': 'Approved', 'color': VIZ[0]}],
              table={'columns': ['Month', 'Detected (h)', 'Approved (h)'], 'rows': [[o['label'], o['detectedHours'], o['approvedHours']] for o in ot]}),
        _card('Leave taken by type', 'leave', 'hbars',
              ch.hbars([{'label': x['type'], 'value': x['days']} for x in d['leave']], format=lambda n: f'{fmt_num(n)} d'),
              subtitle=f"Days taken in this period · {fmt_num(services.js_round(sum(m['days'] for m in d['leaveMonthly']), 10))} days in total",
              table={'columns': ['Leave type', 'Days', 'Per employee'], 'rows': [[x['type'], x['days'], x['perEmployee']] for x in d['leave']]},
              empty=None if d['leave'] else 'No leave taken in this period'),
    ]
    people = [
        _card('Gender', 'gender', 'donut', ch.donut(d['gender'], size=140, center_label='people', aria_label='Employees by gender'),
              table={'columns': ['Gender', 'People'], 'rows': [[g['label'], g['count']] for g in d['gender']]}),
        _card('Tenure', 'tenure', 'hbars', ch.hbars([{'label': t['label'], 'value': t['count']} for t in d['tenure']]),
              subtitle='Years with the company', table={'columns': ['Tenure', 'People'], 'rows': [[t['label'], t['count']] for t in d['tenure']]}),
        _card('Age', 'age', 'hbars', ch.hbars([{'label': a['label'], 'value': a['count']} for a in d['age']]),
              table={'columns': ['Age', 'People'], 'rows': [[a['label'], a['count']] for a in d['age']]}),
        _card('Departments', 'departments', 'hbars', ch.hbars([{'label': x['label'], 'value': x['count']} for x in d['departments']]),
              table={'columns': ['Department', 'People'], 'rows': [[x['label'], x['count']] for x in d['departments']]}),
        _card('Branches', 'branches', 'hbars', ch.hbars([{'label': x['label'], 'value': x['count']} for x in d['branches']]),
              table={'columns': ['Branch', 'People'], 'rows': [[x['label'], x['count']] for x in d['branches']]}),
        _card('People cost by department', 'costByDepartment', 'hbars',
              ch.hbars([{'label': x['label'], 'value': x['totalCost']} for x in d['costByDepartment']], format=money),
              subtitle='Whole period, locked pay runs',
              table={'columns': ['Department', 'Total cost'], 'rows': [[x['label'], x['totalCost']] for x in d['costByDepartment']]},
              empty=None if d['costByDepartment'] else 'No locked pay runs in this period'),
    ]
    return main, people


def _insights_query(request) -> tuple[str, dict]:
    rng = request.GET.get('range') if request.GET.get('range') in dict(RANGES) else '12'
    q = range_of(int(rng))
    for k in ('branchId', 'orgUnitId'):
        if request.GET.get(k):
            q[k] = request.GET[k]
    return rng, q


@require_GET
@require_module('insights')
def insights_page(request):
    """Workforce insights: trends by Bikram Sambat month, filtered by branch and department, every chart downloadable."""
    user = request.hajiri_user
    rng, q = _insights_query(request)
    ctx = {'range': rng, 'ranges': RANGES, 'branch_id': q.get('branchId', ''), 'org_unit_id': q.get('orgUnitId', ''),
           'filters': services.filter_options(user), 'error': None, 'd': None}
    status = 200
    try:
        d = services.insights(user, q)
    except BadRequestError as e:
        ctx['error'] = error_message(e)
        status = 422 if is_htmx(request) else 400
    else:
        main, people = insight_cards(d)
        qs = urlencode({k: v for k, v in q.items()})
        ctx.update({'d': d, 'stats': kpi_stats(d), 'main_cards': main, 'people_cards': people, 'export_qs': qs,
                    'period': f"{d['months'][0]['label']} to {d['months'][-1]['label']} · people counts as of {fmt_date(d['asOf'])} "
                              '· changes compare the last month with the one before'})
    if is_htmx(request) and request.headers.get('HX-Target') == 'insights-body':
        return render(request, 'insights/partials/insights_body.html', ctx, status=status)
    return render(request, 'insights/insights.html', ctx, status=status)


@require_GET
@require_module('insights')
def insights_export(request, dataset):
    """One dataset as CSV, for the same filters as the page (GET /insights/export/:dataset)."""
    try:
        filename, columns, out = services.export_dataset(request.hajiri_user, dataset, request.GET.dict())
    except BadRequestError as e:
        if is_htmx(request) or request.headers.get('Accept', '').startswith('application/json'):
            raise
        resp = redirect(f"/insights?{request.GET.urlencode()}" if request.GET else '/insights')
        return flash(resp, error_message(e), 'err', 'Download failed')
    return csv_response(filename, columns, out)


# ================================================================ reports catalogue

def _visible_entries(request) -> list[catalog.Entry]:
    return [e for e in catalog.ENTRIES if (not e.feature or feature_on(request, e.feature)) and catalog.available(e)]


@require_GET
@require_role('hr')
def reports_page(request):
    """Reports, Catalogue: every report opens on screen or downloads as a CSV file for Excel."""
    today = _today()
    bs = ad_to_bs(today)
    shown = _visible_entries(request)
    groups = [{'category': c, 'entries': [e for e in shown if e.category == c]} for c in catalog.CATEGORIES]
    groups = [g for g in groups if g['entries']]
    return render(request, 'insights/reports.html', {
        'groups': groups, 'categories': [g['category'] for g in groups], 'today': today, 'from': add_days(today, -30),
        'bs_year': bs.year, 'bs_month': bs.month,
    })


def _back_to_reports(message: str, tone: str = 'info', title: str | None = None, request=None) -> HttpResponse:
    """A refused export: a toast where the person is (HTMX), or back to the catalogue with it."""
    if request is not None and is_htmx(request):
        return toast(request, HttpResponse(status=204), message, tone, title)
    return flash(redirect('/reports'), message, tone, title)


def _download(request) -> HttpResponse:
    """After an HTMX check: send the browser to the same address as a plain download (it stays on the page)."""
    resp = HttpResponse(status=204)
    resp['HX-Redirect'] = f'{request.path}?{request.GET.urlencode()}' if request.GET else request.path
    return resp


def _entry(key: str, kind: str) -> catalog.Entry:
    e = catalog.ENTRY_BY_KEY.get(key)
    if not e or not getattr(e, kind):
        raise NotFoundError('not found')
    return e


def _check_entry(request, e: catalog.Entry) -> None:
    if e.feature and not feature_on(request, e.feature):
        from hajiri.core.errors import FeatureOffError
        raise FeatureOffError(e.feature)
    if not catalog.available(e):
        raise NotFoundError('not found')


@require_GET
@require_role('hr')
def report_csv(request, key):
    """The "Export CSV" buttons of the catalogue: attendance reports with their inputs (sendReport CSV)."""
    e = _entry(key, 'csv')
    _check_entry(request, e)
    g = request.GET
    q: dict = {'format': 'csv'}
    names: dict = {}
    for inp in e.csv_inputs:
        if inp == 'month':
            q.update({'bsYear': g.get('bs_year', ''), 'bsMonth': g.get('bs_month', '')})
            names.update({'y': g.get('bs_year', ''), 'm': g.get('bs_month', '')})
        elif inp == 'date':
            q['date'] = g.get('date', '')
            names['date'] = q['date']
        elif inp == 'range':
            q.update({'from': g.get('from', ''), 'to': g.get('to', '')})
            names.update({'from': q['from'], 'to': q['to']})
        elif inp == 'employee':
            if not g.get('employee_id'):
                return _back_to_reports('Pick an employee first, then export.', request=request)
            q['employeeId'] = g['employee_id']
            names['emp'] = q['employeeId']
    if any(v in (None, '') for v in q.values()):
        return _back_to_reports('Pick the dates first, then export.', request=request)
    filename = e.csv_file.format(**names)
    try:
        if e.csv.startswith('source:'):
            t = catalog.as_table(catalog.call_source(catalog.source_for(e), request.hajiri_user, q), filename)
        else:
            t = reports.attendance_report(request.hajiri_user, e.csv, q)
    except HttpError as exc:
        return _back_to_reports(error_message(exc), 'err', 'Export failed', request=request)
    if is_htmx(request):
        # The button is HTMX so a refusal stays a toast on the page (TS toast.err); a good export goes on to download.
        return _download(request)
    return csv_response(filename, t.columns, t.rows)


def _view_values(e: catalog.Entry, g: QueryDict) -> dict:
    """The report's inputs from the query string, with the TS defaults (TableReportModal init)."""
    today = _today()
    vals: dict = {}
    for p in e.params:
        if p.kind == 'date':
            vals[p.key] = g.get(p.key, today)
        elif p.kind == 'range':
            vals['from'] = g.get('from', add_days(today, -30))
            vals['to'] = g.get('to', today)
        elif p.kind == 'days':
            raw = g.get(p.key)
            try:
                n = int(raw) if raw not in (None, '') else p.default
            except ValueError:
                n = p.default
            vals[p.key] = str(min(p.max, max(1, n)))
        elif p.kind == 'select':
            v = g.get(p.key)
            vals[p.key] = v if v in dict(p.options) else p.default
        elif p.kind == 'placement':
            for k in ('branchId', 'orgUnitId'):
                if g.get(k):
                    vals[k] = g[k]
            if p.status:
                vals['status'] = g.get('status') if g.get('status') in ('active', 'exited', 'all') else 'active'
    return {k: v for k, v in vals.items() if v not in (None, '')}


def _run_table(request, e: catalog.Entry, vals: dict, *, csv: bool = False):
    user = request.hajiri_user
    q = {**vals, **({'format': 'csv'} if csv else {})}
    if e.table.startswith('people:'):
        t = people_report(user, e.table[len('people:'):], q)
    else:
        t = catalog.call_source(catalog.source_for(e), user, q)
    return catalog.as_table(t, e.file)


def _lookups(user) -> dict:
    c = company_id_of(user)
    return {'branches': rows('SELECT id, name_en FROM branch WHERE company_id = %s ORDER BY name_en', [c]),
            'units': rows('SELECT id, name_en, kind FROM org_unit WHERE company_id = %s ORDER BY name_en', [c])}


@require_GET
@require_role('hr')
def report_view(request, key):
    """A report on screen (TableReportModal): its inputs, branch/department/status filters, the table and a CSV link."""
    e = _entry(key, 'table')
    _check_entry(request, e)
    vals = _view_values(e, request.GET)
    ctx = {'e': e, 'vals': vals, 'csv_qs': urlencode(vals), 'error': None, 'table': None}
    try:
        t = _run_table(request, e, vals)
        ctx['table'] = {'columns': [c.to_dict() for c in t.columns], 'rows': t.rows, 'n': len(t.rows)}
    except HttpError as exc:
        if request.headers.get('HX-Target') == 'report-result':
            # TS: the table empties and the error is a toast (toast.err(e, 'Could not run the report')).
            resp = render(request, 'insights/partials/report_result.html', ctx, status=422)
            return toast(request, resp, error_message(exc), 'err', 'Could not run the report')
        ctx['error'] = error_message(exc)
    if request.headers.get('HX-Target') == 'report-result':
        return render(request, 'insights/partials/report_result.html', ctx)
    ctx.update(_lookups(request.hajiri_user))
    ctx['has_placement'] = any(p.kind == 'placement' for p in e.params)
    return render(request, 'insights/partials/report_view.html', ctx)


@require_GET
@require_role('hr')
def report_view_csv(request, key):
    """The modal's "Export CSV": the same view, dates in Bikram Sambat (sendTable)."""
    e = _entry(key, 'table')
    _check_entry(request, e)
    try:
        t = _run_table(request, e, _view_values(e, request.GET), csv=True)
    except HttpError as exc:
        return _back_to_reports(error_message(exc), 'err', 'Export failed', request=request)
    if is_htmx(request):
        return _download(request)
    return table_response(t)


# ================================================================ report builder

OP_LABEL = {'eq': 'is', 'neq': 'is not', 'contains': 'contains', 'starts': 'starts with', 'gt': 'is more than', 'gte': 'is at least',
            'lt': 'is less than', 'lte': 'is at most', 'empty': 'is empty', 'not_empty': 'is not empty'}
OPS_FOR = {'text': ['eq', 'neq', 'contains', 'starts', 'empty', 'not_empty'],
           'number': ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
           'date': ['eq', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
           'bool': ['eq', 'neq']}
DATE_OP_LABEL = {'eq': 'is on', 'gt': 'is after', 'gte': 'is on or after', 'lt': 'is before', 'lte': 'is on or before'}
MAX_FILTERS, MAX_COLUMNS = 15, 30


def _datasets() -> list[dict]:
    return reports.adhoc_datasets()['datasets']


def _blank(ds: dict) -> dict:
    return {'dataset': ds['key'], 'columns': list(ds['defaultColumns']), 'filters': [], 'sort': None, 'includeExited': False}


def _ds_of(datasets: list[dict], key: str | None) -> dict:
    return next((d for d in datasets if d['key'] == key), datasets[0])


def definition_from(data: QueryDict, datasets: list[dict] | None = None) -> dict:
    """The builder form as an ad-hoc definition (the TS page's `def` state)."""
    datasets = datasets or _datasets()
    ds = _ds_of(datasets, data.get('dataset'))
    types = {c['key']: c['type'] for c in ds['columns']}
    filters = []
    i = 0
    while f'f{i}_column' in data and i < 100:
        col = data.get(f'f{i}_column')
        op = data.get(f'f{i}_op') or 'eq'
        raw = data.get(f'f{i}_value', '')
        value = (raw != 'false') if types.get(col) == 'bool' else raw
        filters.append({'column': col, 'op': op, 'value': value})
        i += 1
    sort_col = data.get('sort_column')
    return {'dataset': data.get('dataset') or ds['key'], 'columns': [c for c in data.getlist('columns') if c],
            'filters': filters,
            'sort': {'column': sort_col, 'dir': data.get('sort_dir') or 'asc'} if sort_col else None,
            'includeExited': data.get('include_exited') in ('1', 'on', 'true')}


def _form_ctx(d: dict, datasets: list[dict], current: dict | None) -> dict:
    ds = _ds_of(datasets, d.get('dataset'))
    labels = {c['key']: c['label'] for c in ds['columns']}
    types = {c['key']: c['type'] for c in ds['columns']}
    chosen = [c for c in d.get('columns') or [] if c in labels]
    filters = []
    for i, f in enumerate(d.get('filters') or []):
        t = types.get(f.get('column'), 'text')
        ops = OPS_FOR.get(t, OPS_FOR['text'])
        filters.append({
            'i': i, 'value_name': f'f{i}_value', 'value_id': f'bf-{i}', 'column': f.get('column'), 'op': f.get('op'), 'value': f.get('value'), 'type': t,
            'ops': [(o, (DATE_OP_LABEL.get(o) if t == 'date' else None) or OP_LABEL[o]) for o in ops],
            'needs_value': f.get('op') not in ('empty', 'not_empty'),
            'bool_value': 'false' if f.get('value') is False else 'true',
            'text_value': '' if f.get('value') is None or isinstance(f.get('value'), bool) else str(f.get('value')),
        })
    sort = d.get('sort') or None
    return {'datasets': datasets, 'ds': ds, 'def': d, 'chosen': [{'key': c, 'label': labels[c]} for c in chosen],
            'addable': [c for c in ds['columns'] if c['key'] not in chosen], 'filters': filters,
            'sort_column': (sort or {}).get('column') or '', 'sort_dir': (sort or {}).get('dir') or 'asc',
            'include_exited': bool(d.get('includeExited')), 'current': current,
            'can_add_filter': len(filters) < MAX_FILTERS, 'can_add_column': len(chosen) < MAX_COLUMNS,
            'no_filters_text': f"No filters: every {'' if d.get('includeExited') else 'current '}employee is included."}


def _saved_list(user, current_id: str | None = None) -> list[dict]:
    out = []
    for s in reports.list_saved(user):
        s = dict(s)
        s['id'] = str(s['id'])
        s['badge'] = ('Shared' if s['mine'] else s.get('owner_name') or 'Shared') if s['shared'] else None
        s['active'] = s['id'] == current_id
        out.append(s)
    return out


def _current_of(user, report_id: str | None) -> dict | None:
    if not report_id:
        return None
    return next((s for s in _saved_list(user) if s['id'] == report_id), None)


def _result_ctx(result: dict | None) -> dict:
    if not result:
        return {'result': None}
    return {'result': {**result, 'shown': len(result['rows']),
                       'rows_label': f"{result['total']} {'row' if result['total'] == 1 else 'rows'}"}}


def _builder_ctx(request, d: dict, current: dict | None, result: dict | None) -> dict:
    datasets = _datasets()
    ctx = _form_ctx(d, datasets, current)
    ctx.update(_result_ctx(result))
    ctx['saved'] = _saved_list(request.hajiri_user, current['id'] if current else None)
    ctx['save_label'] = 'Save changes' if current and current.get('mine') else 'Save'
    return ctx


@require_GET
@require_role('hr')
def builder_page(request):
    """Reports, Build a report: pick a dataset, columns, filters and a sort; preview 100 rows; export or save it."""
    user = request.hajiri_user
    datasets = _datasets()
    current, result, err = None, None, None
    sid = request.GET.get('saved')
    if sid:
        current = _current_of(user, sid)
        if not current:
            raise NotFoundError('report not found')
        d = current['definition'] if isinstance(current['definition'], dict) else _blank(datasets[0])
        try:
            result = reports.run_saved(user, sid)
        except HttpError as e:
            err = error_message(e)
    else:
        ds = _ds_of(datasets, request.GET.get('dataset'))
        d = _blank(ds)
    ctx = _builder_ctx(request, d, current, result)
    target = request.headers.get('HX-Target')
    ctx['oob'] = is_htmx(request) and target == 'builder'
    resp = render(request, 'insights/partials/builder.html' if is_htmx(request) and target == 'builder' else 'insights/builder.html', ctx)
    if err:
        toast(request, resp, err, 'err', 'Could not run the report')
    return resp


@require_POST
@require_role('hr')
def builder_form(request):
    """Re-draws the builder form after a structural change: dataset, columns, sort, filters."""
    user = request.hajiri_user
    datasets = _datasets()
    data = request.POST
    action = data.get('action', '')
    current = _current_of(user, data.get('current_id') or None)
    d = definition_from(data, datasets)
    ds = _ds_of(datasets, d['dataset'])
    types = {c['key']: c['type'] for c in ds['columns']}
    if action == 'dataset':
        d = _blank(ds)
        current = None
    elif action == 'add_column' and data.get('add_column') in types and len(d['columns']) < MAX_COLUMNS:
        if data['add_column'] not in d['columns']:
            d['columns'].append(data['add_column'])
    elif action.startswith('remove_column:'):
        col = action.split(':', 1)[1]
        d['columns'] = [c for c in d['columns'] if c != col]
    elif action == 'add_filter' and len(d['filters']) < MAX_FILTERS:
        d['filters'].append({'column': ds['columns'][0]['key'], 'op': 'eq', 'value': ''})
    elif action.startswith('remove_filter:'):
        i = int(action.split(':', 1)[1]) if action.split(':', 1)[1].isdigit() else -1
        d['filters'] = [f for j, f in enumerate(d['filters']) if j != i]
    elif action.startswith('filter_column:'):
        i = int(action.split(':', 1)[1]) if action.split(':', 1)[1].isdigit() else -1
        if 0 <= i < len(d['filters']):
            nt = types.get(d['filters'][i]['column'], 'text')
            d['filters'][i] = {'column': d['filters'][i]['column'], 'op': (OPS_FOR.get(nt) or ['eq'])[0],
                               'value': True if nt == 'bool' else ''}
    ctx = _form_ctx(d, datasets, current)
    ctx['save_label'] = 'Save changes' if current and current.get('mine') else 'Save'
    resp = render(request, 'insights/partials/builder_form.html', ctx)
    if action == 'dataset':
        resp.write(render(request, 'insights/partials/builder_oob.html',
                          {**ctx, 'clear_result': True, 'saved': _saved_list(user)}).content)
    return resp


@require_POST
@require_role('hr')
def builder_run(request):
    """Run: the preview (at most 100 rows, with the total)."""
    d = definition_from(request.POST)
    try:
        result = reports.run_adhoc(request.hajiri_user, d)
    except HttpError as e:
        return toast(request, HttpResponse(status=204), error_message(e), 'err', 'Could not run the report')
    return render(request, 'insights/partials/builder_result.html', _result_ctx(result))


def _export_name(request, d: dict) -> str:
    current = _current_of(request.hajiri_user, request.POST.get('current_id') or request.GET.get('current_id') or None)
    label = current['name'] if current else _ds_of(_datasets(), d['dataset'])['label']
    return re.sub(r'[^a-zA-Z0-9 _-]', '', label)


@require_POST
@require_role('hr')
def builder_export(request):
    """Export CSV: checks the definition, then sends the browser to the download (every row, up to 10,000)."""
    d = definition_from(request.POST)
    try:
        from hajiri.reports.adhoc import build_adhoc_query, parse_definition
        build_adhoc_query(parse_definition(d), company_id_of(request.hajiri_user), _today(), 1)
    except HttpError as e:
        return toast(request, HttpResponse(status=204), error_message(e), 'err', 'Export failed')
    params = request.POST.copy()
    for k in ('csrfmiddlewaretoken', 'action'):
        params.pop(k, None)
    resp = HttpResponse(status=204)
    resp['HX-Redirect'] = f"/_/insights/builder/csv?{params.urlencode()}"
    return resp


@require_GET
@require_role('hr')
def builder_csv(request):
    d = definition_from(request.GET)
    name = _export_name(request, d)
    try:
        return reports.run_adhoc(request.hajiri_user, d, csv=True, name=name)
    except HttpError as e:
        resp = redirect('/reports/builder')
        return flash(resp, error_message(e), 'err', 'Export failed')


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def builder_save(request):
    """The save dialog: Save (a new report, or the changes to my own) and Save as new."""
    user = request.hajiri_user
    data = request.GET if request.method == 'GET' else request.POST
    current = _current_of(user, data.get('current_id') or None)
    mine = current if current and current.get('mine') else None
    if request.method == 'GET':
        d = definition_from(data)
        return render(request, 'insights/partials/builder_save.html', {
            'current': mine, 'definition': json.dumps(d), 'name': mine['name'] if mine else '',
            'description': (mine or {}).get('description') or '', 'shared': bool(mine and mine['shared'])})
    try:
        d = json.loads(request.POST.get('definition') or '{}')
    except ValueError:
        raise BadRequestError('definition is not valid')
    body = {'name': (request.POST.get('name') or '').strip(), 'description': (request.POST.get('description') or '').strip() or None,
            'shared': request.POST.get('shared') in ('1', 'on', 'true'), 'definition': d}
    if mine and request.POST.get('as_new') != '1':
        saved = reports.update_saved(user, mine['id'], body)
    else:
        saved = reports.create_saved(user, body)
    saved_id = str(saved['id'])
    cur = _current_of(user, saved_id)
    resp = render(request, 'insights/partials/builder_oob.html', {
        'saved': _saved_list(user, saved_id), 'current': cur,
        'save_label': 'Save changes' if cur and cur.get('mine') else 'Save'})
    close_modal(resp)
    return toast(request, resp, f'"{saved["name"]}" saved')


@require_GET
@require_role('hr')
def builder_saved(request):
    """The Saved reports panel (reloads on hj:refresh-builder-saved)."""
    return render(request, 'insights/partials/builder_saved.html',
                  {'saved': _saved_list(request.hajiri_user, request.GET.get('current_id') or None)})


@require_POST
@require_role('hr')
def builder_saved_delete(request, id):
    user = request.hajiri_user
    reports.delete_saved(user, str(id))
    current_id = request.POST.get('current_id') or None
    cleared = current_id == str(id)
    resp = render(request, 'insights/partials/builder_oob.html', {
        'saved': _saved_list(user, None if cleared else current_id), 'only_saved': not cleared,
        'current': None if cleared else _current_of(user, current_id), 'save_label': 'Save' if cleared else None})
    return toast(request, resp, 'Report deleted')
