"""
JSON endpoints under /api/v1/payroll (the TS paths), for Bearer-token integrations and the printable pages the TS
web app opened directly (payslip, salary sheet, certificates). The pages call the same functions in api.py,
compliance.py and tax_savings.py.
"""
from __future__ import annotations

from django.http import HttpResponse

from hajiri.core.errors import BadRequestError
from hajiri.core.http import api_view, json_response, read_json
from hajiri.web.csv import to_csv

from . import api, compliance, tax_savings

NUL = 'text contains a character that is not allowed'


def _body(request) -> dict:
    b = read_json(request)
    if api.has_nul(b):
        raise BadRequestError(NUL)
    return b


def _query(request) -> dict:
    qd = request.GET.dict()
    if api.has_nul(qd):
        raise BadRequestError(NUL)
    return qd


def html_response(html: str) -> HttpResponse:
    res = HttpResponse(html, content_type='text/html; charset=utf-8')
    res['Cache-Control'] = 'private, no-store'
    return res


def csv_download(filename: str, text: str) -> HttpResponse:
    res = HttpResponse(text, content_type='text/csv; charset=utf-8')
    res['Content-Disposition'] = f'attachment; filename="{filename}"'
    return res


def report_response(r: dict) -> HttpResponse | dict:
    """sendReport: CSV download, or {columns, rows}."""
    if r['format'] == 'csv':
        return csv_download(f"{r['filename']}.csv", to_csv(r['columns'], r['rows']))
    return {'columns': r['columns'], 'rows': r['rows']}


# ---------------------------------------------------------------- rules, components, structures, assignments

@api_view(methods=('GET', 'POST'))
def rules(request):
    if request.method == 'POST':
        return json_response(api.create_rule(request.hajiri_user, _body(request)), status=201)
    return api.list_rules(request.hajiri_user, _query(request))


@api_view(methods=('POST',))
def rule_approve(request, id):
    return api.approve_rule(request.hajiri_user, id)


@api_view(methods=('GET', 'POST'))
def components(request):
    if request.method == 'POST':
        return json_response(api.create_component(request.hajiri_user, _body(request)), status=201)
    return api.list_components(request.hajiri_user)


@api_view(methods=('PATCH',))
def component(request, code):
    if '\x00' in code:
        raise BadRequestError(NUL)
    return api.update_component(request.hajiri_user, code, _body(request))


@api_view(methods=('GET', 'POST'))
def structures(request):
    if request.method == 'POST':
        return json_response(api.create_structure(request.hajiri_user, _body(request)), status=201)
    return api.list_structures(request.hajiri_user)


@api_view(methods=('POST',))
def structure_versions(request, id):
    return json_response(api.add_structure_version(request.hajiri_user, id, _body(request)), status=201)


@api_view(methods=('GET', 'POST'))
def assignments(request):
    if request.method == 'POST':
        return json_response(api.create_assignment(request.hajiri_user, _body(request)), status=201)
    return api.list_assignments(request.hajiri_user, _query(request))


# ---------------------------------------------------------------- declarations, loans, tax savings

@api_view(methods=('GET', 'PUT'))
def declarations(request):
    if request.method == 'PUT':
        return api.put_declaration(request.hajiri_user, _body(request))
    return api.get_declaration(request.hajiri_user, _query(request))


@api_view(methods=('POST',))
def declaration_verify(request, employee_id):
    return api.verify_declaration(request.hajiri_user, employee_id, _body(request))


@api_view(methods=('GET', 'POST'))
def loans(request):
    if request.method == 'POST':
        return json_response(api.create_loan(request.hajiri_user, _body(request)), status=201)
    return api.list_loans(request.hajiri_user, _query(request))


@api_view(methods=('GET', 'POST'))
def tax_savings_root(request):
    if request.method == 'POST':
        return json_response(tax_savings.submit_savings(request.hajiri_user, _body(request)), status=201)
    return tax_savings.get_savings(request.hajiri_user, _query(request))


@api_view(methods=('POST',))
def tax_savings_cancel(request, id):
    return tax_savings.cancel_savings(request.hajiri_user, id)


@api_view(methods=('POST',))
def tax_savings_estimate(request):
    return tax_savings.estimate(request.hajiri_user, _body(request))


@api_view()
def tax_savings_file(request, id, file_id):
    from hajiri.files.store import stored_file_response
    f = tax_savings.proof_file(request.hajiri_user, id, file_id)
    return stored_file_response(f, download=request.GET.get('download') == '1')


# ---------------------------------------------------------------- periods and runs

@api_view(methods=('GET', 'POST'))
def periods(request):
    if request.method == 'POST':
        return json_response(api.create_period(request.hajiri_user, _body(request)), status=201)
    return api.list_periods(request.hajiri_user)


@api_view(methods=('GET', 'POST'))
def runs(request):
    if request.method == 'POST':
        return json_response(api.create_run(request.hajiri_user, _body(request)), status=201)
    return api.list_runs(request.hajiri_user)


@api_view(methods=('POST',))
def bonus_run(request):
    return json_response(api.create_bonus_run(request.hajiri_user, _body(request)), status=201)


@api_view()
def run(request, id):
    return api.run_detail(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_preflight(request, id):
    return api.run_preflight(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_lock_attendance(request, id):
    return api.run_lock_attendance(request.hajiri_user, id)


@api_view(methods=('GET', 'POST'))
def run_inputs(request, id):
    if request.method == 'POST':
        return json_response(api.add_inputs(request.hajiri_user, id, _body(request)), status=201)
    return api.list_inputs(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_compute(request, id):
    return api.compute(request.hajiri_user, id, _body(request))


@api_view(methods=('POST',))
def run_review(request, id):
    return api.review(request.hajiri_user, id, _body(request))


@api_view(methods=('POST',))
def run_approve(request, id):
    return api.approve(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_lock(request, id):
    return api.lock(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_publish(request, id):
    return api.publish(request.hajiri_user, id)


@api_view(methods=('POST',))
def run_holds(request, id):
    return json_response(compliance.hold_pay(request.hajiri_user, id, _body(request)), status=201)


@api_view()
def run_ssf_sheet(request, id):
    name, text = api.ssf_sheet(request.hajiri_user, id)
    return csv_download(name, text)


def sheet_response(fmt: str, sheet: dict):
    from .salary_sheet import salary_sheet_csv, salary_sheet_html, sheet_filename
    if fmt == 'csv':
        return csv_download(sheet_filename(sheet), salary_sheet_csv(sheet))
    if fmt == 'html':
        return html_response(salary_sheet_html(sheet))
    return sheet


@api_view()
def run_salary_sheet(request, id):
    return sheet_response(*api.salary_sheet_for(request.hajiri_user, id, _query(request)))


# ---------------------------------------------------------------- payslips

@api_view()
def payslips(request):
    return api.list_payslips(request.hajiri_user, _query(request))


@api_view()
def payslip(request, id):
    return api.payslip_detail(request.hajiri_user, id)


@api_view()
def payslip_html(request, id):
    return html_response(api.payslip_html(request.hajiri_user, id))


# ---------------------------------------------------------------- compliance

@api_view(methods=('GET', 'PUT'))
def opening_balances(request):
    if request.method == 'PUT':
        return compliance.put_opening(request.hajiri_user, _body(request))
    return compliance.list_openings(request.hajiri_user, _query(request))


@api_view(methods=('DELETE',))
def opening_balance(request, id):
    return compliance.delete_opening(request.hajiri_user, id)


@api_view(methods=('POST',))
def opening_import(request):
    if request.content_type in ('text/csv', 'text/plain'):
        text = request.body.decode('utf-8', errors='replace')
    else:
        text = _body(request).get('csv')
        text = text if isinstance(text, str) else None
    return compliance.import_openings(request.hajiri_user, text)


@api_view()
def optimum_me(request):
    return compliance.optimum_me(request.hajiri_user)


@api_view()
def optimum_all(request):
    return compliance.optimum_all(request.hajiri_user)


@api_view(methods=('POST',))
def optimum_apply(request):
    return compliance.apply_cit(request.hajiri_user, _body(request))


@api_view()
def holds(request):
    return compliance.list_holds(request.hajiri_user, _query(request))


@api_view(methods=('POST',))
def hold_release(request, id):
    return compliance.release_hold(request.hajiri_user, id, _body(request))


@api_view(methods=('POST',))
def hold_cancel(request, id):
    return compliance.cancel_hold(request.hajiri_user, id)


@api_view()
def certificate(request, employee):
    return html_response(compliance.certificate(request.hajiri_user, employee, _query(request)))


@api_view()
def report_annual_tax(request):
    return report_response(compliance.annual_tax(request.hajiri_user, _query(request)))


@api_view()
def report_tax_deposit(request):
    return report_response(compliance.tax_deposit(request.hajiri_user, _query(request)))


@api_view()
def report_retirement_deposit(request):
    return report_response(compliance.retirement_deposit(request.hajiri_user, _query(request)))


@api_view()
def report_pay_variance(request):
    r = compliance.pay_variance(request.hajiri_user, _query(request))
    if r['format'] == 'json':
        return {'previous': r['previous'], 'rows': r['rows']}
    return report_response({**r, 'format': 'csv'})


@api_view()
def report_leave_liability(request):
    r = compliance.leave_liability(request.hajiri_user, _query(request))
    if r['format'] == 'json':
        return {'asOf': r['asOf'], 'divisor': r['divisor'], 'total': r['total'], 'byDepartment': r['byDepartment'], 'rows': r['rows']}
    return report_response(r)


@api_view()
def compliance_years(request):
    return compliance.compliance_years(request.hajiri_user)
