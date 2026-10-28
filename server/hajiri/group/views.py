"""
Group pages: /group (the consolidated dashboard plus group administration), /group/people (the directory across
companies with headcount breakdowns) and /group/reports (consolidated reports with company as a column). Rules and
queries live in services.py; every link into a company's own pages goes through the company switcher.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from hajiri.core import companies
from hajiri.core.auth import company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.errors import ForbiddenError
from hajiri.reports.csv import csv_response, table_response
from hajiri.web.charts import SERIES_VARS
from hajiri.web.decorators import login_required
from hajiri.web.format import fmt_money
from hajiri.web.format import minutes_to_hm
from hajiri.web.htmx import is_htmx, toast
from hajiri.web.tables import Column, Table, table_response as data_table_response

from . import services
from .filters import DirectoryFilter
from .forms import MONTH_CHOICES, PayPolicyForm, ReportForm, fiscal_year_choices

PALETTE = SERIES_VARS


# ================================================================ /group

def _admin_ctx(user) -> dict:
    """Group administration on the overview (the foundation's sections): add a company, group roles, access."""
    summary = companies.group_summary(user)
    is_admin = has_role(user, 'admin')
    is_group_admin = companies.is_group_admin(user)
    ctx = {'summary': summary, 'is_admin': is_admin, 'is_group_admin': is_group_admin,
           # A standalone company's admin starts a group; afterwards only group admins add companies.
           'can_add_company': is_admin and (not user.group_id or is_group_admin),
           'group_subtitle': (f'{summary.group_name}: every company of the group you can see, today.'
                              if summary.group_name else '')}
    if is_admin:
        ctx['members'] = companies.list_memberships(user)
        ctx['pay_policy'] = services.pay_policy(company_id_of(user)) if user.group_id else None
        ctx['active_company_name'] = next((c.name for c in summary.companies if c.active), '')
    if is_group_admin:
        ctx['group_roles'] = companies.list_group_roles(user)
    return ctx


def _stats(d: dict) -> list[dict]:
    t = d['totals']
    stats = [
        {'label': 'Companies', 'value': len(d['companies'])},
        {'label': 'People', 'value': t['headcount'], 'hint': f"+{t['joiners']} joined, {t['leavers']} left this month"},
        {'label': 'In today', 'value': t['present'], 'hint': f"{t['late']} late, {t['absent']} absent, {t['onLeave']} on leave"},
        {'label': 'Waiting for approval', 'value': t['pendingApprovals'], 'tone': 'warn' if t['pendingApprovals'] else 'ok'},
        {'label': 'Open exceptions', 'value': t['exceptions'], 'tone': 'warn' if t['exceptions'] else 'ok'},
    ]
    if t['payrollCost'] is not None:
        stats.append({'label': 'Latest payroll cost', 'value': f"NPR {_whole(t['payrollCost'])}",
                      'hint': 'Latest locked run of each company' + (' you may see' if t['payHidden'] else '')})
    return stats


def _whole(amount: str) -> str:
    return fmt_money(amount, currency=False, decimals=0)


def _attrition_chart(d: dict) -> dict:
    months = d['attrition']
    labels = [m['short'] for m in months]
    series = [{'key': 'group', 'label': 'Group', 'values': [m['attritionPct'] for m in months], 'color': PALETTE[0]}]
    if len(d['companies']) > 1:
        for i, c in enumerate(d['companies']):
            series.append({'key': c['code'], 'label': c['name'], 'color': PALETTE[(i + 1) % len(PALETTE)],
                           'values': [m['byCompany'].get(c['id']) for m in months]})
    table = {'columns': ['Month', 'Leavers', 'Attrition %'],
             'rows': [[m['label'], m['exits'], m['attritionPct']] for m in months]}
    return {'labels': labels, 'series': series, 'table': table,
            'legend': [{'label': s['label'], 'color': s['color']} for s in series],
            'empty': not any(m['start'] or m['end'] for m in months)}


@require_GET
@login_required
def overview(request):
    """/group: the consolidated dashboard of every company of the group the person may see, and (for admins) group
    administration. A standalone company's admin lands here to add a sister company."""
    user = request.hajiri_user
    can_view = services.can_view(user)
    if not can_view and not has_role(user, 'admin'):
        raise ForbiddenError(services.FORBIDDEN)
    ctx = _admin_ctx(user)
    ctx['d'] = None
    if can_view:
        d = services.dashboard(user, fresh=request.GET.get('refresh') == '1')
        for c in d['companies']:
            c['ot_hm'] = minutes_to_hm(c['otMinutes'])
        d['totals']['ot_hm'] = minutes_to_hm(d['totals']['otMinutes'])
        ctx.update({'d': d, 'stats': _stats(d), 'attrition': _attrition_chart(d)})
    return render(request, 'group/overview.html', ctx)


@require_POST
@login_required
def pay_policy(request):
    """The active company's admin: hide (or show) its pay figures from executives in group views."""
    form = PayPolicyForm(request.POST)
    form.is_valid()
    hide = bool(form.cleaned_data.get('hide'))
    services.set_pay_policy(request.hajiri_user, hide)
    msg = 'Executives no longer see pay figures of this company.' if hide else 'Executives see pay figures of this company.'
    return toast(request, HttpResponse(status=204), msg)


# ================================================================ /group/people

TYPE_LABEL = services.EMPLOYMENT_LABEL
GENDER_LABEL = services.GENDER_LABEL

COLUMNS = [
    Column('emp_code', 'Code', sortable=True, mono=True),
    Column('name_en', 'Name', sortable=True),
    Column('company_name', 'Company', sortable=True),
    Column('designation', 'Designation', sortable=True, hide_below='md'),
    Column('department', 'Department', sortable=True, hide_below='md'),
    Column('branch_name', 'Branch', sortable=True, hide_below='md'),
    Column('employment_type', 'Employment type', format=lambda v: TYPE_LABEL.get(v, v) if v else '—',
           csv=lambda r: TYPE_LABEL.get(r['employment_type'], r['employment_type'] or '')),
    Column('gender', 'Gender', format=lambda v: GENDER_LABEL.get(v, v) if v else '—', hide_below='md',
           csv=lambda r: GENDER_LABEL.get(r['gender'], r['gender'] or '')),
    Column('join_date', 'Joined', format='bs_date', sortable=True, csv=lambda r: bs_label(r['join_date']) if r['join_date'] else ''),
    Column('status', 'Status', format='status'),
]
FIELDS = ('id', 'emp_code', 'name_en', 'company_id', 'company_name', 'designation', 'department', 'branch_name',
          'employment_type', 'gender', 'join_date', 'status', 'email')


def _people_ctx(request):
    user = request.hajiri_user
    scope = services.group_scope(user)
    f = DirectoryFilter(request.GET, services.directory(user), scope=scope)
    qs = f.qs
    table = Table(request, qs.values(*FIELDS), COLUMNS, id='group-people', search=('emp_code', 'name_en', 'email'),
                  search_placeholder='Search name, code or email', default_sort='name_en', page_size=50,
                  export='group-people', empty_title='No people match these filters.',
                  extra_params={k: v for k, v in request.GET.items() if k in f.filters})
    return scope, f, qs, table


@require_GET
@login_required
def people(request):
    """/group/people: everyone in the companies of the group, filterable, with headcount by company, branch,
    department, gender and employment type."""
    scope, f, qs, table = _people_ctx(request)
    if request.GET.get('export') == 'breakdowns':
        rows = [{'dimension': services.DIMENSIONS[dim][0], 'value': r['label'], 'company': r['company'],
                 'headcount': r['value']} for dim, rs in services.breakdowns(qs).items() for r in rs]
        return csv_response('group-headcount-breakdowns', [{'key': 'dimension', 'header': 'Breakdown'},
                                                           {'key': 'value', 'header': 'Value'},
                                                           {'key': 'company', 'header': 'Company'},
                                                           {'key': 'headcount', 'header': 'Headcount'}], rows)
    if table.wants_csv or table.is_partial:
        return data_table_response(request, table, 'group/people.html')
    b = services.breakdowns(qs)
    cards = [{'key': dim, 'title': title, 'bars': services.breakdown_totals(b[dim]) if dim != 'company' else b[dim],
              'rows': b[dim]} for dim, (title, _) in services.DIMENSIONS.items()]
    ctx = {'filter': f, 'table': table, 'cards': cards, 'scope': scope, 'total': sum(r['value'] for r in b['company']),
           'query': request.GET.urlencode()}
    return data_table_response(request, table, 'group/people.html', ctx)


# ================================================================ /group/reports

@require_GET
@login_required
def reports(request):
    """/group/reports: one consolidated report at a time, on screen or as CSV (?format=csv)."""
    user = request.hajiri_user
    scope = services.group_scope(user)
    form = ReportForm(request.GET, scope)
    q = form.query()
    today = npt_today()
    period = services.period_of(q['period'], bs_year=q['bs_year'], bs_month=q['bs_month'], fy=q['fy'], today=today)
    table = services.report(user, q['report'], period, q['company'])
    if request.GET.get('format') == 'csv':
        table.filename = f"{table.filename}-{period.months[0].key}" + (f"-to-{period.months[-1].key}" if len(period.months) > 1 else '')
        return table_response(table)
    cur = services.current_month(today)
    ctx = {'form': form, 'q': q, 'table': table, 'period': period, 'reports': services.REPORTS,
           'companies': scope.companies, 'bs_year': q['bs_year'] or cur.year, 'bs_month': q['bs_month'] or cur.month,
           'fy': q['fy'] or services.current_fiscal_start(today), 'fy_choices': fiscal_year_choices(today),
           'month_choices': MONTH_CHOICES, 'columns': [c.to_dict() for c in table.columns],
           'csv_query': _with(request, format='csv'), 'report_desc': services.REPORTS[q['report']][1]}
    if is_htmx(request) and request.headers.get('HX-Target') == 'group-report':
        return render(request, 'group/partials/report.html', ctx)
    return render(request, 'group/reports.html', ctx)


def _with(request, **changes) -> str:
    params = request.GET.copy()
    for k, v in changes.items():
        params[k] = v
    return params.urlencode()

