"""
Payroll JSON API under /api/v1/payroll (the TS paths), for Bearer-token integrations and the printable pages the TS web
app opened directly (payslip, salary sheet, certificates). DRF views with the project defaults (hajiri.core.api):
HajiriAuthentication (cookie with CSRF, or Bearer), the TS error shape and JsonRenderer. HR-only routes declare
HasModule('payroll'); routes employees use themselves (payslips, tax savings, certificates) check access in the
service. Bodies are JSON only; rules and validation stay in the services.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from hajiri.core.api import HasModule, IsSignedIn
from hajiri.core.errors import BadRequestError

from . import compliance, services, tax_savings
from . import serializers as s
from .responses import csv_download, html_response, report_csv, sheet_response
from .services import list_components  # noqa: F401 - hajiri.home.services imports it from here

NUL = 'text contains a character that is not allowed'
PAYROLL = [IsSignedIn, HasModule('payroll')]
SIGNED_IN = [IsSignedIn]
JSON = [JSONParser]


class ReportFormatNegotiation(DefaultContentNegotiation):
    """`?format=json|csv|html` picks the report format (the TS query parameter), not a DRF renderer: answers are JSON
    unless the view returns a download itself."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


def report_format(view):
    """Marks a view whose `format` query parameter is the report format."""
    view.content_negotiation_class = ReportFormatNegotiation
    return view


def _body(request) -> dict:
    b = request.data
    if services.has_nul(b):
        raise BadRequestError(NUL)
    return b


def _query(request) -> dict:
    qd = request.query_params.dict()
    if services.has_nul(qd):
        raise BadRequestError(NUL)
    return qd


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


def report_response(r: dict):
    """A compliance report: CSV download, or {columns, rows}."""
    return report_csv(r) if r['format'] == 'csv' else Response({'columns': r['columns'], 'rows': r['rows']})


# ---------------------------------------------------------------- rules, components, structures, assignments

@extend_schema(methods=['GET'], parameters=[s.RulesQueryIn], responses=dict)
@extend_schema(methods=['POST'], request=s.RuleIn, responses={201: dict})
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
@parser_classes(JSON)
def rules(request):
    """Statutory rule versions (shared by every company); only an administrator adds one."""
    if request.method == 'POST':
        return _created(services.create_rule(request.user, _body(request)))
    return Response(services.list_rules(request.user, _query(request)))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def rule_approve(request, id):
    return Response(services.approve_rule(request.user, id))


@extend_schema(methods=['POST'], request=s.ComponentIn, responses={201: dict})
@extend_schema(methods=['GET'], responses=dict)
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def components(request):
    if request.method == 'POST':
        return _created(services.create_component(request.user, _body(request)))
    return Response(services.list_components(request.user))


@extend_schema(request=s.ComponentPatchIn, responses=dict)
@api_view(['PATCH'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def component(request, code):
    if '\x00' in code:
        raise BadRequestError(NUL)
    return Response(services.update_component(request.user, code, _body(request)))


@extend_schema(methods=['POST'], request=s.StructureIn, responses={201: dict})
@extend_schema(methods=['GET'], responses=dict)
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def structures(request):
    if request.method == 'POST':
        return _created(services.create_structure(request.user, _body(request)))
    return Response(services.list_structures(request.user))


@extend_schema(request=s.StructureVersionIn, responses={201: dict})
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def structure_versions(request, id):
    return _created(services.add_structure_version(request.user, id, _body(request)))


@extend_schema(methods=['GET'], parameters=[s.EmployeeQueryIn], responses=dict)
@extend_schema(methods=['POST'], request=s.AssignmentIn, responses={201: dict})
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def assignments(request):
    if request.method == 'POST':
        return _created(services.create_assignment(request.user, _body(request)))
    return Response(services.list_assignments(request.user, _query(request)))


# ---------------------------------------------------------------- declarations, loans, tax savings

@extend_schema(methods=['GET'], parameters=[s.DeclarationQueryIn], responses=dict)
@extend_schema(methods=['PUT'], request=s.DeclarationIn, responses=dict)
@api_view(['GET', 'PUT'])
@permission_classes(SIGNED_IN)
@parser_classes(JSON)
def declarations(request):
    """Everyone's own declaration (sent to HR as a request); HR's entries for others are recorded as verified."""
    if request.method == 'PUT':
        return Response(services.put_declaration(request.user, _body(request)))
    return Response(services.get_declaration(request.user, _query(request)))


@extend_schema(request=s.DeclarationVerifyIn, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def declaration_verify(request, employee_id):
    return Response(services.verify_declaration(request.user, employee_id, _body(request)))


@extend_schema(methods=['GET'], parameters=[s.EmployeeQueryIn], responses=dict)
@extend_schema(methods=['POST'], request=s.LoanIn, responses={201: dict})
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def loans(request):
    if request.method == 'POST':
        return _created(services.create_loan(request.user, _body(request)))
    return Response(services.list_loans(request.user, _query(request)))


@extend_schema(methods=['GET'], parameters=[s.SavingsQueryIn], responses=dict)
@extend_schema(methods=['POST'], request=s.SavingsIn, responses={201: dict})
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
@parser_classes(JSON)
def tax_savings_root(request):
    if request.method == 'POST':
        return _created(tax_savings.submit_savings(request.user, _body(request)))
    return Response(tax_savings.get_savings(request.user, _query(request)))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def tax_savings_cancel(request, id):
    return Response(tax_savings.cancel_savings(request.user, id))


@extend_schema(request=s.EstimateIn, responses=dict)
@api_view(['POST'])
@permission_classes(SIGNED_IN)
@parser_classes(JSON)
def tax_savings_estimate(request):
    return Response(tax_savings.estimate(request.user, _body(request)))


@extend_schema(responses={200: bytes})
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def tax_savings_file(request, id, file_id):
    from hajiri.files.store import stored_file_response
    f = tax_savings.proof_file(request.user, id, file_id)
    return stored_file_response(f, download=request.query_params.get('download') == '1')


# ---------------------------------------------------------------- periods and runs

@extend_schema(methods=['POST'], request=s.PeriodIn, responses={201: dict})
@extend_schema(methods=['GET'], responses=dict)
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def periods(request):
    if request.method == 'POST':
        return _created(services.create_period(request.user, _body(request)))
    return Response(services.list_periods(request.user))


@extend_schema(methods=['POST'], request=s.RunIn, responses={201: dict})
@extend_schema(methods=['GET'], responses=dict, operation_id='api_v1_payroll_runs_list')
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def runs(request):
    if request.method == 'POST':
        return _created(services.create_run(request.user, _body(request)))
    return Response(services.list_runs(request.user))


@extend_schema(request=s.BonusRunIn, responses={201: dict})
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def bonus_run(request):
    return _created(services.create_bonus_run(request.user, _body(request)))


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
def run(request, id):
    return Response(services.run_detail(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def run_preflight(request, id):
    return Response(services.run_preflight(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def run_lock_attendance(request, id):
    return Response(services.run_lock_attendance(request.user, id))


@extend_schema(methods=['POST'], request=s.InputsIn, responses={201: dict})
@extend_schema(methods=['GET'], responses=dict)
@api_view(['GET', 'POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def run_inputs(request, id):
    if request.method == 'POST':
        return _created(services.add_inputs(request.user, id, _body(request)))
    return Response(services.list_inputs(request.user, id))


@extend_schema(request=s.ComputeIn, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def run_compute(request, id):
    return Response(services.compute(request.user, id, _body(request)))


@extend_schema(request=s.ReviewIn, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def run_review(request, id):
    return Response(services.review(request.user, id, _body(request)))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def run_approve(request, id):
    return Response(services.approve(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def run_lock(request, id):
    return Response(services.lock(request.user, id))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def run_publish(request, id):
    return Response(services.publish(request.user, id))


@extend_schema(request=s.HoldIn, responses={201: dict})
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def run_holds(request, id):
    return _created(compliance.hold_pay(request.user, id, _body(request)))


@extend_schema(responses={200: str})
@api_view(['GET'])
@permission_classes(PAYROLL)
def run_ssf_sheet(request, id):
    name, text = services.ssf_sheet(request.user, id)
    return csv_download(name, text)


@extend_schema(parameters=[s.SheetQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def run_salary_sheet(request, id):
    out = sheet_response(*services.salary_sheet_for(request.user, id, _query(request)))
    return Response(out) if isinstance(out, dict) else out


# ---------------------------------------------------------------- payslips

@extend_schema(parameters=[s.PayslipsQueryIn], responses=dict, operation_id='api_v1_payroll_payslips_list')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def payslips(request):
    return Response(services.list_payslips(request.user, _query(request)))


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def payslip(request, id):
    return Response(services.payslip_detail(request.user, id))


@extend_schema(responses={200: str})
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def payslip_html(request, id):
    return html_response(services.payslip_html(request.user, id))


# ---------------------------------------------------------------- compliance

@extend_schema(methods=['GET'], parameters=[s.OpeningsQueryIn], responses=dict)
@extend_schema(methods=['PUT'], request=s.OpeningIn, responses=dict)
@api_view(['GET', 'PUT'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def opening_balances(request):
    if request.method == 'PUT':
        return Response(compliance.put_opening(request.user, _body(request)))
    return Response(compliance.list_openings(request.user, _query(request)))


@extend_schema(request=None, responses=dict)
@api_view(['DELETE'])
@permission_classes(PAYROLL)
def opening_balance(request, id):
    return Response(compliance.delete_opening(request.user, id))


@extend_schema(request={'text/csv': str, 'application/json': dict}, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def opening_import(request):
    """CSV as a text/csv (or text/plain) body, or JSON {"csv": "..."}."""
    if (request.content_type or '').split(';')[0].strip() in ('text/csv', 'text/plain'):
        text = request.body.decode('utf-8', errors='replace')
    else:
        text = _body(request).get('csv')
        text = text if isinstance(text, str) else None
    return Response(compliance.import_openings(request.user, text))


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def optimum_me(request):
    return Response(compliance.optimum_me(request.user))


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
def optimum_all(request):
    return Response(compliance.optimum_all(request.user))


@extend_schema(request=s.ApplyCitIn, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def optimum_apply(request):
    return Response(compliance.apply_cit(request.user, _body(request)))


@extend_schema(parameters=[s.HoldsQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
def holds(request):
    return Response(compliance.list_holds(request.user, _query(request)))


@extend_schema(request=s.ReleaseIn, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
@parser_classes(JSON)
def hold_release(request, id):
    return Response(compliance.release_hold(request.user, id, _body(request)))


@extend_schema(request=None, responses=dict)
@api_view(['POST'])
@permission_classes(PAYROLL)
def hold_cancel(request, id):
    return Response(compliance.cancel_hold(request.user, id))


@extend_schema(parameters=[s.CertificateQueryIn], responses={200: str})
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def certificate(request, employee):
    return html_response(compliance.certificate(request.user, employee, _query(request)))


@extend_schema(parameters=[s.AnnualQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def report_annual_tax(request):
    return report_response(compliance.annual_tax(request.user, _query(request)))


@extend_schema(parameters=[s.MonthQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def report_tax_deposit(request):
    return report_response(compliance.tax_deposit(request.user, _query(request)))


@extend_schema(parameters=[s.MonthQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def report_retirement_deposit(request):
    return report_response(compliance.retirement_deposit(request.user, _query(request)))


@extend_schema(parameters=[s.VarianceQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def report_pay_variance(request):
    r = compliance.pay_variance(request.user, _query(request))
    if r['format'] == 'json':
        return Response({'previous': r['previous'], 'rows': r['rows']})
    return report_csv(r)


@extend_schema(parameters=[s.LiabilityQueryIn], responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
@report_format
def report_leave_liability(request):
    r = compliance.leave_liability(request.user, _query(request))
    if r['format'] == 'json':
        return Response({'asOf': r['asOf'], 'divisor': r['divisor'], 'total': r['total'], 'byDepartment': r['byDepartment'], 'rows': r['rows']})
    return report_csv(r)


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes(PAYROLL)
def compliance_years(request):
    return Response(compliance.compliance_years(request.user))
