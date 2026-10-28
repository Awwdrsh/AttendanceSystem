"""
Employees hub pages (apps/web/src/pages/hr/EmployeesPage.tsx, EmployeeDetailPage.tsx, components/EmployeeForm.tsx):
the HR directory with add and CSV import, and one employee's record with its tabs. Fragments under /_/employees/...
"""
from __future__ import annotations

from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of
from hajiri.core.clock import npt_today
from django.db.models import F

from hajiri.db.models import Branch, Payslip, Shift
from hajiri.core.errors import NotFoundError
from hajiri.web.decorators import require_role
from hajiri.web.format import fmt_date
from hajiri.web.htmx import close_modal, hx_redirect, refresh, toast
from hajiri.web.nav import url_of
from hajiri.web.tables import Column, Table, table_response

from . import service as svc
from .forms import EmployeeForm, ImportForm

DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
EMPLOYMENT = {'regular': 'Regular', 'work_based': 'Work-based', 'time_based': 'Time-based', 'casual': 'Casual', 'part_time': 'Part-time'}
EMPLOYMENT_FORM = [('regular', 'Regular'), ('work_based', 'Work-based'), ('time_based', 'Time-based'),
                   ('casual', 'Casual (up to 7 days a month)'), ('part_time', 'Part-time (under 35 h a week)')]
REGIME = {'ssf': 'SSF', 'pf': 'PF and gratuity', 'none': 'None'}
FISCAL_MONTHS = ['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra']
BANKS = [('NMB', 'NMB'), ('NABIL', 'NABIL'), ('NIC_ASIA', 'NIC ASIA'), ('EVEREST', 'EVEREST'), ('PRABHU', 'PRABHU'),
         ('GLOBAL_IME', 'GLOBAL IME'), ('OTHER', 'Other')]
TABS = [('overview', 'Overview'), ('profile', 'Profile'), ('career', 'Career'), ('leave', 'Leave'), ('history', 'Employment history'),
        ('lifecycle', 'Lifecycle'), ('relations', 'Relations'), ('pay', 'Pay'), ('documents', 'Documents'), ('biometrics', 'Biometrics'),
        ('changes', 'Change history'), ('audit', 'Audit')]


def humanize(v) -> str:
    if v is None or v == '':
        return '—'
    s = str(v).replace('_', ' ')
    return s[:1].upper() + s[1:]


def _ok(request, message: str, *refreshes: str, close: bool = True) -> HttpResponse:
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message)


# ================================================================ directory

def _name_cell(r):
    from django.template.loader import render_to_string
    return render_to_string('employees/partials/name_cell.html', {'r': r})


COLUMNS = [
    Column('name_en', 'Name', sortable=True, render=_name_cell, csv=lambda r: r['name_en']),
    Column('emp_code', 'Code', sortable=True, mono=True, muted=True),
    Column('join_date', 'Joined', format='bs_date', sortable=True),
    Column('status', 'Status', format='status'),
]


@require_GET
@require_role('hr')
def employee_list(request):
    status = 'exited' if request.GET.get('status') == 'exited' else 'active'
    data = svc.employees_queryset(request.hajiri_user, {'status': status})
    table = Table(request, data, COLUMNS, id='employees', search=('name_en', 'emp_code', 'name_ne'), search_placeholder='Search by name or code',
                  default_sort='emp_code', export='employees', row_url=lambda r: reverse('employees:detail', args=[r['id']]),
                  empty_title='No exited employees' if status == 'exited' else 'No employees yet',
                  empty_hint=('People appear here once an exit date is set on their profile.' if status == 'exited'
                              else 'Add your first employee, or import a CSV with emp_code and name_en columns.'),
                  empty_search_title='No employees match your search', empty_search_hint='Try a different name or code.',
                  extra_params={'status': status})
    n = data.count()
    return table_response(request, table, 'employees/list.html', {'table': table, 'status': status,
                                                                  'count': f"{n} {'person' if n == 1 else 'people'}"})


def _form_options(user) -> dict:
    company = company_id_of(user)
    branches = [(str(b.id), f'{b.name_en} ({b.code})') for b in Branch.objects.for_company(company).order_by('code')]
    shifts = [(str(s.id), f"{s.code}, {s.start_time.strftime('%H:%M')} to {s.end_time.strftime('%H:%M')}")
              for s in Shift.objects.for_company(company).order_by('code')]
    return {'branches': branches, 'shifts': shifts, 'dow': list(enumerate(DAY_NAMES)), 'months': list(enumerate(FISCAL_MONTHS, start=1)),
            'banks': BANKS, 'employment': EMPLOYMENT_FORM}


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def employee_new(request):
    if request.method == 'POST':
        new_id = svc.create_employee(request.hajiri_user, EmployeeForm(request.POST).body())
        resp = hx_redirect(request, reverse('employees:detail', args=[new_id]))
        from hajiri.web.htmx import flash
        return flash(resp, 'Employee added.')
    initial = {'regime': 'ssf', 'ot_eligible': True, 'weekly_off_days': [6], 'employment_type': 'regular', 'is_resident': True}
    return render(request, 'employees/partials/form.html', {'e': initial, 'new': True, 'action': reverse('employees:new'),
                                                            'title': 'Add employee', 'submit': 'Add employee', **_form_options(request.hajiri_user)})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def employee_edit(request, id):
    user = request.hajiri_user
    e = svc.get_employee(user, str(id))
    if request.method == 'POST':
        svc.update_employee(user, str(id), EmployeeForm(request.POST, initial_row=e).body())
        # The TS page reloads the employee, so the header (name, designation, status) changes too: reload the page,
        # staying on the tab the person was on.
        from urllib.parse import urlsplit
        detail = reverse('employees:detail', args=[id])
        cur = urlsplit(request.headers.get('HX-Current-URL') or '')
        url = f"{detail}?{cur.query}" if cur.path == detail and cur.query else detail
        from hajiri.web.htmx import flash
        return flash(hx_redirect(request, url), 'Changes saved.')
    return render(request, 'employees/partials/form.html', {'e': e, 'new': False, 'action': reverse('employees:edit', args=[id]),
                                                            'title': 'Edit profile', 'submit': 'Save changes', **_form_options(user)})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def employee_import(request):
    if request.method == 'GET':
        return render(request, 'employees/partials/import.html', {'csv': '', 'result': None})
    form = ImportForm(request.POST, request.FILES)
    text = form.text()
    dry = form.dry_run
    from hajiri.core.errors import HttpError
    try:
        status, body = svc.import_csv(request.hajiri_user, text, dry_run=dry)
    except HttpError as err:
        status, body = 400, {'imported': 0, 'errors': [{'row': 0, 'message': err.message}]}
    errors = body.get('errors') or []
    n = body.get('valid', body.get('imported', 0))
    resp = render(request, 'employees/partials/import.html', {
        'csv': text, 'result': body, 'errors': errors[:20], 'more': max(0, len(errors) - 20), 'err_count': len(errors),
        'valid_label': f"{n} {'row' if n == 1 else 'rows'} valid",
        'err_title': f"{len(errors)} {'row needs' if len(errors) == 1 else 'rows need'} fixing before you can import",
    }, status=200)
    if not dry and status == 201:
        # EmployeesPage.tsx keeps the modal open with the result (skipped codes listed) and reloads the list.
        refresh(resp, 'employees')
        return toast(request, resp, f"{body['imported']} employees imported.")
    return resp


# ================================================================ one employee

def _fmt_range(lo, hi) -> str:
    return f"{fmt_date(lo) if lo else 'Start'} to {fmt_date(hi) if hi else 'present'}"


@require_GET
@require_role('hr')
def employee_detail(request, id):
    user = request.hajiri_user
    try:
        e = svc.get_employee(user, str(id))
    except NotFoundError:
        return render(request, 'employees/not_found.html', status=404)
    tab = request.GET.get('tab') or 'overview'
    if tab not in dict(TABS):
        tab = 'overview'
    sub = ', '.join(x for x in [e['designation'], f"Code {e['emp_code']}", f"Joined {fmt_date(e['join_date'])}" if e['join_date'] else None] if x)
    ctx = {'e': e, 'tab': tab, 'tabs': TABS, 'subtitle': sub, 'id': str(id)}
    ctx.update(_tab_ctx(request, e, tab))
    return render(request, 'employees/detail.html', ctx)


def _rev(name: str, *args) -> str | None:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _tab_ctx(request, e: dict, tab: str) -> dict:
    user = request.hajiri_user
    eid = e['id']
    if tab == 'overview':
        return _overview_ctx(e)
    if tab == 'lifecycle':
        # LifecycleSection.tsx shows the API's error in place; with the switch off that is 'feature off'.
        from hajiri.web.nav import feature_on
        return {'lifecycle_on': feature_on(request, 'lifecycle')}
    if tab == 'leave':
        bal = []
        try:
            from hajiri.leave.service import balances, get_employee_lite
            lite = get_employee_lite(eid)
            bal = balances(lite, npt_today().isoformat()) if lite else []
        except ImportError:
            pass
        return {'balances': bal, 'apply_url': url_of('leave:apply')}
    if tab == 'history':
        out = svc.assignments(user, eid)
        for a in out:
            a['period'] = _fmt_range(a['valid_from'], a['valid_to'])
            a['type'] = EMPLOYMENT.get(a['employment_type'], humanize(a['employment_type']))
        return {'assignments': out}
    if tab == 'pay':
        slips = list(Payslip.objects.filter(employee_id=eid, run__company_id=company_id_of(user))
                     .annotate(run_type=F('run__run_type'), bs_year=F('run__period__bs_year'), bs_month=F('run__period__bs_month'))
                     .order_by('-bs_year', '-bs_month', '-created_at')
                     .values('id', 'net', 'published_at', 'run_type', 'bs_year', 'bs_month'))
        for s in slips:
            s['extra'] = humanize(s['run_type']) if s['run_type'] and s['run_type'] != 'regular' else ''
        return {'payslips': slips}
    if tab == 'documents':
        return {'documents_url': _rev('documents:employee_tab', eid)}
    if tab == 'relations':
        return {'relations_url': _rev('relations:employee_section', eid)}
    if tab == 'biometrics':
        return {'consents_url': url_of('devices:consents'), 'templates_url': url_of('devices:templates'), 'consent_url': url_of('devices:consent'),
                'shred_url': _rev('devices:shred_all', eid)}
    if tab == 'audit':
        ev = svc.audit_events(user, eid)
        for x in ev:
            x['what'] = humanize(x['action'])
        return {'events': ev}
    return {}


def _overview_ctx(e: dict) -> dict:
    return {'ov': [
        ('Email', e['email'] or '—'), ('Phone', e['phone'] or '—'), ('Gender', humanize(e['gender'])), ('Date of birth', fmt_date(e['dob'])),
        ('Join date', fmt_date(e['join_date'])), ('Probation ends', fmt_date(e['probation_end'])),
        ('Employment type', EMPLOYMENT.get(e['employment_type'], humanize(e['employment_type'])) if e['employment_type'] else '—'),
        ('Grade', e['grade'] or '—'),
        ('Weekly off', ', '.join(DAY_NAMES[d] if 0 <= d < 7 else str(d) for d in e['weekly_off_days']) if e['weekly_off_days'] else 'None'),
        ('Overtime', 'Eligible' if e['ot_eligible'] else 'Not eligible'), ('Work from home', 'Allowed' if e['work_from_home'] else 'Office only'),
        ('Retirement scheme', REGIME.get(e['regime'], e['regime'])), ('Exit date', fmt_date(e['exit_date'])),
    ]}


@require_GET
@require_role('hr')
def overview(request, id):
    e = svc.get_employee(request.hajiri_user, str(id))
    return render(request, 'employees/partials/overview.html', {'e': e, 'id': str(id), **_overview_ctx(e)})


@require_POST
@require_role('hr')
def sensitive(request, id):
    s = svc.sensitive(request.hajiri_user, str(id))
    return render(request, 'employees/partials/sensitive.html', {'s': s})


@require_GET
def legacy_list(request):
    return HttpResponseRedirect(reverse('employees:list'))


@require_GET
def legacy_detail(request, id):
    return HttpResponseRedirect(reverse('employees:detail', args=[id]))

