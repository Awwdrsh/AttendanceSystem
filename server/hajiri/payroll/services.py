"""
Payroll services, one function per route of payroll/routes.ts: (user, body or query, path params) -> the TS response
body. Access rules, validation messages, company scoping (always the ACTIVE company) and audit entries follow the TS
routes. The JSON API (api.py) and the pages (views.py) both call these; the run engine is in runs.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import localcontext

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import BooleanField, DateField, ExpressionWrapper, F, Func, JSONField, Max, OuterRef, Prefetch, Q, Value
from django.db.models.functions import Coalesce, Now

from engines.bs_calendar import BsRangeError
from engines.payroll import formula_references, validate_formula
from engines.payroll.numeric import ENGINE_CONTEXT, D, ceil

from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid, exists_in_company
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (Employee, Loan, LoanInstallment, PayrollHold, PayrollInput, PayrollRun, Payslip, PayslipLine, RuleVersion,
                              SalaryAssignment, SalaryComponent, SalaryStructure, SalaryStructureVersion, TaxDeclaration)

from . import runs as svc
from .filters import AssignmentFilter, LoanFilter
from .numbers import column_row, range_text, strs
from .payslip_html import render_payslip_html
from .queries import department_on, sum_of
from .rules import STATUTORY_KEYS, rules_in_force
from .salary_sheet import salary_sheet, ssf_sheet_csv
from .serializers import (CODE_RE, DECL_FIELDS, RUN_TYPES, AssignmentIn, BonusRunIn, ComponentIn, ComponentPatchIn, ComputeIn,
                          DeclarationIn, DeclarationQueryIn, DeclarationVerifyIn, EmployeeQueryIn, InputsIn, LoanIn, PayslipsQueryIn,
                          PeriodIn, ReviewIn, RuleIn, RulesQueryIn, RunIn, SheetQueryIn, StructureIn, StructureVersionIn)
from .tax_savings import DECL_COLUMNS, decide_tax_savings, lock_employee, pending_tax_savings, submit_tax_savings, verify

RunType, RunStatus = PayrollRun.RunType, PayrollRun.Status
ACTIVE_INPUT_TYPES = (SalaryComponent.Type.EARNING, SalaryComponent.Type.DEDUCTION)
INPUT_STATUSES = (RunStatus.DRAFT, RunStatus.PREFLIGHT_FAILED, RunStatus.COMPUTED, RunStatus.REVIEWED)

__all__ = ['CODE_RE', 'RUN_TYPES', 'require_payroll', 'list_rules', 'create_rule', 'approve_rule', 'list_components', 'check_formulas',
           'create_component', 'update_component', 'list_structures', 'create_structure', 'add_structure_version', 'list_assignments',
           'create_assignment', 'get_declaration', 'put_declaration', 'verify_declaration', 'list_loans', 'create_loan', 'list_periods',
           'create_period', 'list_runs', 'create_run', 'create_bonus_run', 'run_payslips', 'run_detail', 'run_preflight',
           'run_lock_attendance', 'add_inputs', 'list_inputs', 'compute', 'review', 'approve', 'lock', 'publish', 'ssf_sheet',
           'salary_sheet_for', 'can_see_payslip', 'list_payslips', 'load_payslip', 'payslip_detail', 'payslip_html', 'has_nul']


def require_payroll(user: AuthUser) -> None:
    if not has_module(user, 'payroll'):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES['payroll'])}")


def _cid(user: AuthUser) -> str:
    return company_id_of(user)


def _employee_in(company_id: str, employee_id) -> bool:
    return exists_in_company(Employee, company_id, pk=as_uuid(employee_id))


# ---------------------------------------------------------------- rule versions (plan §6.13)

RULE_FIELDS = ('id', 'rule_key', 'jurisdiction', 'valid_from_ad', 'valid_to_ad', 'valid_from_bs', 'valid_to_bs', 'value', 'source_url', 'note',
               'approved_by_id', 'approved_at', 'supersedes_id', 'created_at')


def list_rules(user: AuthUser, query: dict) -> dict:
    """Rule versions are jurisdiction-wide (shared by every company): every payroll user may read them."""
    require_payroll(user)
    qy = validate(RulesQueryIn, query)
    qs = RuleVersion.objects.all()
    if qy.get('key') is not None:
        qs = qs.filter(rule_key=qy['key'])
    if qy.get('asOf') is not None:
        qs = qs & rules_in_force(qy['asOf'])
    r = qs.order_by('rule_key', '-valid_from_ad').values(*RULE_FIELDS)
    return {'rules': [{**strs(column_row(RuleVersion, x_), 'id', 'approved_by', 'supersedes_id'), 'locked': x_['rule_key'] in STATUTORY_KEYS}
                      for x_ in r]}


@transaction.atomic
def create_rule(user: AuthUser, body: dict) -> dict:
    """Rule versions are jurisdiction-wide (not per company), so only an administrator may add them."""
    if not has_role(user):
        raise ForbiddenError('requires role admin')
    b = plain(validate(RuleIn, body))
    r = RuleVersion.objects.create(
        rule_key=b['ruleKey'], valid_from_ad=b['validFromAd'], valid_to_ad=b.get('validToAd'), valid_from_bs=b['validFromBs'],
        valid_to_bs=b.get('validToBs'), value=b['value'] if b.get('value') is not None else Value(None, JSONField()),
        source_url=b.get('sourceUrl'), note=b.get('note'), supersedes_id=b.get('supersedesId'))
    if b.get('supersedesId'):
        day_before = date.fromisoformat(b['validFromAd']) - timedelta(days=1)
        RuleVersion.objects.filter(pk=b['supersedesId']).update(valid_to_ad=Coalesce(F('valid_to_ad'), Value(day_before)))
    audit('create', 'rule_version', actor_id=user.id, row_id=str(r.pk), after=b)
    return {'id': str(r.pk)}


@transaction.atomic
def approve_rule(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    rid = parse_uuid(id_)
    if not RuleVersion.objects.filter(pk=rid).update(approved_by_id=user.id, approved_at=Now()):
        raise NotFoundError('rule not found')
    audit('approve', 'rule_version', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- components, structures, assignments (plan §9.2)

COMPONENT_FIELDS = ('code', 'company_id', 'name_en', 'name_ne', 'type', 'formula', 'condition', 'depends_on_payment_days', 'taxable',
                    'statutory', 'rounding', 'sort_order', 'active')


def components(company_id: str):
    return SalaryComponent.objects.for_company(company_id)


def list_components(user: AuthUser) -> dict:
    require_payroll(user)
    r = components(_cid(user)).order_by('sort_order', 'code').values(*COMPONENT_FIELDS)
    return {'components': [strs(c, 'company_id') for c in r]}


_MISSING = object()


def check_formulas(company_id: str, code: str, formula=_MISSING, condition=_MISSING) -> None:
    all_ = list(components(company_id).values('code', 'formula', 'condition'))
    known = [c['code'] for c in all_] + [code]
    for label, src in (('formula', formula), ('condition', condition)):
        if src is not _MISSING and src:
            err = validate_formula(src, known)
            if err:
                raise BadRequestError(f'{label}: {err}')
    # A component may not depend on itself, directly or through others.
    codes = set(known)
    graph: dict[str, list] = {c['code']: [c['formula'], c['condition']] for c in all_}
    cur = graph.get(code, [None, None])
    graph[code] = [formula if formula is not _MISSING else cur[0], condition if condition is not _MISSING else cur[1]]
    deps: dict[str, list[str]] = {}
    for c, srcs in graph.items():
        refs: list[str] = []
        for src in srcs:
            if src:
                try:
                    for r in formula_references(src):
                        if r in codes and r not in refs:
                            refs.append(r)
                except Exception:  # noqa: BLE001 - an invalid stored formula is reported when it runs
                    pass
        deps[c] = refs
    path: list[str] = []
    state: dict[str, int] = {}

    def visit(c: str):
        if state.get(c) == 2:
            return None
        if state.get(c) == 1:
            return [*path[path.index(c):], c]
        state[c] = 1
        path.append(c)
        for d in deps.get(c, []):
            cyc = visit(d)
            if cyc:
                return cyc
        path.pop()
        state[c] = 2
        return None
    cycle = visit(code)
    if cycle:
        raise BadRequestError(f"formula refers back to itself: {' → '.join(cycle)}")


@transaction.atomic
def create_component(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = validate(ComponentIn, body)
    cid = _cid(user)
    check_formulas(cid, b['code'], b.get('formula'), b.get('condition'))
    SalaryComponent.objects.create(
        company_id=cid, code=b['code'], name_en=b['nameEn'], name_ne=b.get('nameNe'), type=b['type'], formula=b.get('formula'),
        condition=b.get('condition'), depends_on_payment_days=b['dependsOnPaymentDays'], taxable=b['taxable'], rounding=b['rounding'],
        sort_order=b['sortOrder'], active=b['active'])
    audit('create', 'salary_component', actor_id=user.id, row_id=b['code'], after=b)
    return {'code': b['code']}


@transaction.atomic
def update_component(user: AuthUser, code: str, body: dict) -> dict:
    require_payroll(user)
    b = validate(ComponentPatchIn, body)
    cid = _cid(user)
    found = components(cid).filter(code=code)
    cur = found.values(*COMPONENT_FIELDS).first()
    if not cur:
        raise NotFoundError('component not found')
    if cur['statutory'] and ('formula' in b or 'type' in b):
        raise ForbiddenError('statutory components are locked (plan §13.8)')
    check_formulas(cid, code, b['formula'] if 'formula' in b else _MISSING, b['condition'] if 'condition' in b else _MISSING)
    changes = {col: b[key] for key, col in (('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('dependsOnPaymentDays', 'depends_on_payment_days'),
                                            ('taxable', 'taxable'), ('rounding', 'rounding'), ('sortOrder', 'sort_order'), ('active', 'active'))
               if b.get(key) is not None}
    # formula and condition may be cleared (null); the others keep their value when left out.
    changes.update({k: b[k] for k in ('formula', 'condition') if k in b})
    if changes:
        found.update(**changes)
    audit('update', 'salary_component', actor_id=user.id, row_id=code, before=strs(dict(cur), 'company_id'), after=b)
    return {'ok': True}


def structures(company_id: str):
    return SalaryStructure.objects.for_company(company_id)


def list_structures(user: AuthUser) -> dict:
    require_payroll(user)
    versions = SalaryStructureVersion.objects.order_by('version')
    out = []
    for s in structures(_cid(user)).order_by('code').prefetch_related(Prefetch('salary_structure_versions', queryset=versions)):
        vs = [{'id': str(v.id), 'version': v.version, 'components': v.components, 'frozenAt': v.frozen_at}
              for v in s.salary_structure_versions.all()]
        out.append({'id': str(s.id), 'code': s.code, 'name_en': s.name_en, 'versions': vs or None})
    return {'structures': out}


@transaction.atomic
def create_structure(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = plain(validate(StructureIn, body))
    s = SalaryStructure.objects.create(company_id=_cid(user), code=b['code'], name_en=b['nameEn'])
    v = SalaryStructureVersion.objects.create(structure=s, version=1, components=b['components'])
    audit('create', 'salary_structure', actor_id=user.id, row_id=str(s.pk), after=b)
    return {'id': str(s.pk), 'versionId': str(v.pk)}


@transaction.atomic
def add_structure_version(user: AuthUser, id_: str, body: dict) -> dict:
    require_payroll(user)
    sid = parse_uuid(id_)
    b = plain(validate(StructureVersionIn, body))
    # The structure row is locked so two new versions cannot take the same number.
    if not structures(_cid(user)).select_for_update(no_key=True).filter(pk=sid).exists():
        raise NotFoundError('structure not found')
    last = SalaryStructureVersion.objects.filter(structure_id=sid).aggregate(n=Max('version'))['n'] or 0
    v = SalaryStructureVersion.objects.create(structure_id=sid, version=last + 1, components=b['components'])
    audit('create', 'salary_structure_version', actor_id=user.id, row_id=str(v.pk), after=b)
    return {'id': str(v.pk), 'version': v.version}


def list_assignments(user: AuthUser, query: dict) -> dict:
    require_payroll(user)
    qy = plain(validate(EmployeeQueryIn, query))
    qs = AssignmentFilter(qy, queryset=SalaryAssignment.objects.for_company(_cid(user))).qs
    r = (qs.order_by('employee__emp_code', '-valid__startswith')
         .values('employee_id', 'structure_version_id', 'valid', 'base_amounts', 'created_at', emp_code=F('employee__emp_code'),
                 name_en=F('employee__name_en'), structure_code=F('structure_version__structure__code'),
                 version=F('structure_version__version')))
    out = []
    for a in r:
        a['valid'] = range_text(a['valid'])
        out.append(strs(a, 'employee_id', 'structure_version_id'))
    return {'assignments': out}


def create_assignment(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = plain(validate(AssignmentIn, body))
    cid = _cid(user)
    if not _employee_in(cid, b['employeeId']):
        raise NotFoundError('employee not found')
    sv = SalaryStructureVersion.objects.for_company(cid).filter(pk=b['structureVersionId']).values_list('components', flat=True).first()
    if sv is None:
        raise NotFoundError('salary structure version not found')
    in_structure = {c['code'] for c in sv}
    unknown = [c for c in b['baseAmounts'] if c not in in_structure]
    if unknown:
        raise BadRequestError(f"not components of this salary structure: {', '.join(unknown)}")
    if not b['baseAmounts']:
        raise BadRequestError('baseAmounts needs at least one component amount')
    start = b['from']
    with transaction.atomic():
        # Close the current open assignment the day before and open the new one (effective-dated, plan §9.1).
        mine = SalaryAssignment.objects.filter(employee_id=b['employeeId'])
        (mine.filter(valid__upper_inf=True, valid__startswith__lt=start)
         .update(valid=Func(Func(F('valid'), function='lower'), Value(start, output_field=DateField()), function='daterange',
                            output_field=SalaryAssignment._meta.get_field('valid'))))
        mine.filter(valid__startswith__gte=start).delete()
        SalaryAssignment.objects.create(employee_id=b['employeeId'], structure_version_id=b['structureVersionId'],
                                        valid=DateRange(start, None), base_amounts=b['baseAmounts'], created_by_id=user.id)
        (SalaryStructureVersion.objects.filter(pk=b['structureVersionId'])
         .update(frozen_at=Coalesce(F('frozen_at'), Now())))
        audit('create', 'salary_assignment', actor_id=user.id, row_id=b['employeeId'], after={**b, 'baseAmounts': '***'})
    return {'ok': True}


# ---------------------------------------------------------------- declarations and loans

DECL_ROW_FIELDS = ('id', 'employee_id', 'fiscal_year_start', 'life_premium', 'health_premium', 'cit_monthly', 'medical_cost',
                   'other_retirement_annual', 'status', 'verified_by_id', 'verified_at', 'updated_at', 'donation', 'cit_from_month',
                   'cit_prev_monthly')


def _decl_row(r: dict | None) -> dict | None:
    if not r:
        return None
    r = strs(column_row(TaxDeclaration, r), 'id', 'employee_id', 'verified_by')
    for k in ('life_premium', 'health_premium', 'cit_monthly', 'medical_cost', 'other_retirement_annual', 'donation', 'cit_prev_monthly'):
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def _own_or_payroll(user: AuthUser, employee_id: str | None) -> str:
    employee_id = employee_id or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    assert_manages(user, employee_id)
    if employee_id != user.employee_id and not has_module(user, 'payroll'):
        raise ForbiddenError()
    return employee_id


def get_declaration(user: AuthUser, query: dict) -> dict:
    qy = plain(validate(DeclarationQueryIn, query))
    employee_id = _own_or_payroll(user, qy.get('employeeId'))
    cid = _cid(user)
    d = _decl_row(TaxDeclaration.objects.for_company(cid).filter(employee_id=employee_id, fiscal_year_start=qy['fiscalYearStart'])
                  .values(*DECL_ROW_FIELDS).first())
    pending = pending_tax_savings(employee_id, qy['fiscalYearStart']) if d or _employee_in(cid, employee_id) else None
    return {'declaration': d, 'pending': pending}


def put_declaration(user: AuthUser, body: dict) -> dict:
    """
    HR entering a declaration for someone else records it as verified (HR is the verifier). Everyone's own
    declaration, including an HR user's, goes to HR as a tax savings request and only counts once approved.
    """
    b = plain(validate(DeclarationIn, body))
    employee_id = _own_or_payroll(user, b.get('employeeId'))
    cid = _cid(user)
    if not _employee_in(cid, employee_id):
        raise NotFoundError('employee not found')
    values = {k: b[k] for k in DECL_FIELDS}
    fy = b['fiscalYearStart']
    if employee_id == user.employee_id:
        if not b['submit']:
            raise BadRequestError('drafts are not kept; submit the declaration when it is ready')
        with transaction.atomic():
            lock_employee(employee_id)
            req = submit_tax_savings(user, cid, employee_id, fy, values)
        return {'ok': True, 'requestId': req['id'], 'status': 'pending'}
    columns = {DECL_COLUMNS[k]: v for k, v in values.items()}
    with transaction.atomic():
        if b['submit']:
            verify(employee_id, fy, columns, user.id)
        else:
            TaxDeclaration.objects.update_or_create(
                employee_id=employee_id, fiscal_year_start=fy,
                defaults={**columns, 'cit_from_month': None, 'cit_prev_monthly': 0, 'status': TaxDeclaration.Status.DRAFT,
                          'verified_by_id': None, 'verified_at': None, 'updated_at': Now()})
        audit('upsert', 'tax_declaration', actor_id=user.id, row_id=employee_id, after=b)
    return {'ok': True, 'status': TaxDeclaration.Status.VERIFIED if b['submit'] else TaxDeclaration.Status.DRAFT}


def verify_declaration(user: AuthUser, employee_id: str, body: dict) -> dict:
    require_payroll(user)
    eid = parse_uuid(employee_id)
    b = validate(DeclarationVerifyIn, body)
    cid = _cid(user)
    if not _employee_in(cid, eid):
        raise NotFoundError('declaration not found')
    pending = pending_tax_savings(eid, b['fiscalYearStart'])
    with transaction.atomic():
        if pending:
            decide_tax_savings(user, pending, 'approved' if b['decision'] == 'verified' else 'rejected', b.get('reason'))
            return {'ok': True}
        n = (TaxDeclaration.objects.for_company(cid).filter(employee_id=eid, fiscal_year_start=b['fiscalYearStart'])
             .update(status=b['decision'], verified_by_id=user.id, verified_at=Now()))
        if not n:
            raise NotFoundError('declaration not found')
        audit(b['decision'], 'tax_declaration', actor_id=user.id, row_id=eid)
    return {'ok': True}


LOAN_FIELDS = ('id', 'employee_id', 'kind', 'principal', 'emi', 'start_bs_year', 'start_bs_month', 'installments', 'status', 'note',
               'created_by_id', 'created_at')


def list_loans(user: AuthUser, query: dict) -> dict:
    require_payroll(user)
    qy = plain(validate(EmployeeQueryIn, query))
    repaid = sum_of(LoanInstallment.objects.filter(loan_id=OuterRef('pk')), 'amount', group='loan_id')
    qs = LoanFilter(qy, queryset=Loan.objects.for_company(_cid(user))).qs
    r = qs.annotate(repaid=repaid).order_by('-created_at').values(*LOAN_FIELDS, 'repaid', emp_code=F('employee__emp_code'))
    out = []
    for ln in r:
        ln = column_row(Loan, ln)
        ln['repaid'] = str(ln['repaid'])
        out.append(strs(ln, 'id', 'employee_id', 'created_by'))
    return {'loans': out}


@transaction.atomic
def create_loan(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = plain(validate(LoanIn, body))
    with localcontext(ENGINE_CONTEXT):
        if not D(b['emi']) > 0 or not D(b['principal']) > 0:
            raise BadRequestError('principal and emi must be positive')
        installments = int(ceil(D(b['principal']) / D(b['emi'])))
    if not _employee_in(_cid(user), b['employeeId']):
        raise NotFoundError('employee not found')
    r = Loan.objects.create(employee_id=b['employeeId'], kind=b['kind'], principal=b['principal'], emi=b['emi'],
                            start_bs_year=b['startBsYear'], start_bs_month=b['startBsMonth'], installments=installments,
                            note=b.get('note'), created_by_id=user.id)
    audit('create', 'loan', actor_id=user.id, row_id=str(r.pk), after=b)
    return {'id': str(r.pk), 'installments': installments}


# ---------------------------------------------------------------- periods and runs (plan §13.6 wizard)

def list_periods(user: AuthUser) -> dict:
    require_payroll(user)
    r = svc.periods(_cid(user)).order_by('-bs_year', '-bs_month').values()
    return {'periods': [svc._period(p) for p in r]}


@transaction.atomic
def create_period(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = validate(PeriodIn, body)
    try:
        return {'period': svc.ensure_period(_cid(user), b['bsYear'], b['bsMonth'])}
    except BsRangeError as e:
        raise BadRequestError(f"BS {b['bsYear']}-{b['bsMonth']} is outside the supported calendar: {e}")


RUN_LIST_FIELDS = ('id', 'period_id', 'run_type', 'status', 'totals', 'maker_id', 'checker_id', 'computed_at', 'approved_at', 'locked_at',
                   'disbursed_at', 'filed_at', 'created_at')


def list_runs(user: AuthUser) -> dict:
    require_payroll(user)
    r = (svc.runs(_cid(user)).order_by('-created_at')
         .values(*RUN_LIST_FIELDS, bs_year=F('period__bs_year'), bs_month=F('period__bs_month')))
    return {'runs': [strs(x_, 'id', 'period_id', 'maker_id', 'checker_id') for x_ in r]}


@transaction.atomic
def create_run(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = plain(validate(RunIn, body))
    cid = _cid(user)
    p = svc.get_period(cid, b['periodId'])
    if b['runType'] == RunType.REGULAR:
        dup = (svc.runs(cid).filter(period_id=p['id'], run_type=RunType.REGULAR).order_by('-created_at')
               .values('id', 'locked_at').first())
        if dup:
            raise ConflictError('a locked regular run already exists for this period; use a supplementary or arrears run' if dup['locked_at']
                                else f"a regular run already exists for this period ({dup['id']}); continue that run instead")
    if b['runType'] == RunType.ARREARS and not b.get('referencesRunId'):
        raise BadRequestError('arrears runs need referencesRunId')
    emp_ids = b.get('employeeIds')
    if b['runType'] == RunType.FINAL_SETTLEMENT:
        if not (emp_ids and len(emp_ids) == 1):
            raise BadRequestError('final settlement runs need exactly one employeeId')
        ex = Employee.objects.for_company(cid).filter(pk=emp_ids[0]).values('exit_date').first()
        if not ex:
            raise NotFoundError('employee not found')
        if not ex['exit_date']:
            raise BadRequestError('final settlement needs an exit date on the employee record')
        if ex['exit_date'].isoformat() > p['end_ad']:
            raise BadRequestError('the exit date is after this period; pick the period containing the exit date')
    if emp_ids:
        own = Employee.objects.for_company(cid).filter(pk__in=emp_ids).count()
        if own != len(set(emp_ids)):
            raise NotFoundError('one or more employees not found')
    if b.get('referencesRunId'):
        ref = svc.get_run(cid, b['referencesRunId'])
        if not ref['locked_at']:
            raise BadRequestError('referenced run must be locked')
    params = {**({'employeeIds': emp_ids} if emp_ids is not None else {}), **({'noticePay': b['noticePay']} if b.get('noticePay') else {})}
    r = PayrollRun.objects.create(company_id=cid, period_id=p['id'], run_type=b['runType'], references_run_id=b.get('referencesRunId'),
                                  params=params, notes=b.get('notes'), maker_id=user.id)
    audit('create', 'payroll_run', actor_id=user.id, row_id=str(r.pk), after=b)
    return {'id': str(r.pk)}


@transaction.atomic
def create_bonus_run(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = plain(validate(BonusRunIn, body))
    cid = _cid(user)
    p = svc.get_period(cid, b['periodId'])
    alloc = svc.bonus_allocation(cid, b['fiscalYearStart'], b['netProfit'])
    params = {'fiscalYearStart': b['fiscalYearStart'], 'netProfit': b['netProfit'], 'pool': alloc['pool'], 'bonusByEmployee': alloc['byEmployee'],
              'detail': alloc['detail'], 'employeeIds': list(alloc['byEmployee'])}
    r = PayrollRun.objects.create(company_id=cid, period_id=p['id'], run_type=RunType.BONUS, params=params, maker_id=user.id)
    audit('create', 'payroll_run', actor_id=user.id, row_id=str(r.pk), after={'runType': 'bonus', 'pool': alloc['pool']})
    return {'id': str(r.pk), 'pool': alloc['pool'], 'allocation': alloc['detail']}


def run_payslips(run_id: str) -> list[dict]:
    """The payslips of a run (already loaded through the company), flagged ones first."""
    flagged = ExpressionWrapper(~Q(flags=[]), output_field=BooleanField())
    r = (Payslip.objects.filter(run_id=run_id).order_by(flagged.desc(), 'employee__emp_code')
         .values('id', 'employee_id', 'paid_days', 'lop_days', 'ot_hours', 'gross', 'tds', 'employee_statutory', 'employer_statutory',
                 'total_deductions', 'net', 'flags', 'published_at', emp_code=F('employee__emp_code'), name_en=F('employee__name_en')))
    return [strs(s, 'id', 'employee_id') for s in r]


def run_detail(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    cid = _cid(user)
    run = svc.get_run(cid, parse_uuid(id_))
    p = svc.get_period(cid, run['period_id'])
    return {'run': run, 'period': p, 'payslips': run_payslips(run['id'])}


def run_preflight(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    cid = _cid(user)
    with transaction.atomic():
        run = svc.get_run(cid, parse_uuid(id_))
        pf = svc.preflight(cid, run)
        if run['status'] in (RunStatus.DRAFT, RunStatus.PREFLIGHT_FAILED):
            svc.runs(cid).filter(pk=run['id']).update(status=RunStatus.DRAFT if pf['ok'] else RunStatus.PREFLIGHT_FAILED, preflight=pf)
    return pf


@transaction.atomic
def run_lock_attendance(user: AuthUser, id_: str) -> dict:
    from hajiri.attendance.service import lock_attendance
    require_payroll(user)
    cid = _cid(user)
    run = svc.get_run(cid, parse_uuid(id_))
    p = svc.get_period(cid, run['period_id'])
    n = lock_attendance(cid, p['start_ad'], p['end_ad'])
    svc.periods(cid).filter(pk=p['id']).update(attendance_locked_at=Coalesce(F('attendance_locked_at'), Now()))
    audit('lock_attendance', 'payroll_period', actor_id=user.id, row_id=p['id'], after={'locked': n})
    return {'locked': n}


def get_input_run(user: AuthUser, id_: str) -> dict:
    """The run inputs are added to (404 for another company's run, before any form is drawn)."""
    require_payroll(user)
    return svc.get_run(_cid(user), parse_uuid(id_))


def add_inputs(user: AuthUser, id_: str, body: dict) -> dict:
    run = get_input_run(user, id_)
    cid = _cid(user)
    if run['status'] not in INPUT_STATUSES:
        raise ConflictError(f"run is {run['status']}; inputs can no longer change")
    b = plain(validate(InputsIn, body))
    codes = set(components(cid).filter(active=True, type__in=ACTIVE_INPUT_TYPES).values_list('code', flat=True))
    unknown = [c for c in dict.fromkeys(i['componentCode'] for i in b['inputs']) if c not in codes]
    if unknown:
        raise BadRequestError(f"unknown or inactive component code(s): {', '.join(unknown)}")
    with localcontext(ENGINE_CONTEXT):
        for i in b['inputs']:
            if not D(i['amount']) > 0:
                raise BadRequestError('input amounts must be positive')
    # A run scoped to named employees only pays them; an input for anyone else would be silently dropped.
    scoped_ids = run['params'].get('employeeIds')
    scoped = set(str(e) for e in scoped_ids) if isinstance(scoped_ids, list) else None
    outside = [e for e in dict.fromkeys(i['employeeId'] for i in b['inputs']) if e not in scoped] if scoped is not None else []
    if outside:
        n = len(scoped)
        raise BadRequestError(f"this run only includes {n} named {'employee' if n == 1 else 'employees'}; {len(outside)} input "
                              f"{'employee is' if len(outside) == 1 else 'employees are'} not in it")
    mine = {str(e) for e in Employee.objects.for_company(cid).filter(pk__in={i['employeeId'] for i in b['inputs']})
            .values_list('id', flat=True)}
    with transaction.atomic():
        rows = [PayrollInput(run_id=run['id'], employee_id=i['employeeId'], component_code=i['componentCode'], amount=i['amount'],
                             note=i.get('note'), created_by_id=user.id) for i in b['inputs'] if i['employeeId'] in mine]
        if len(rows) != len(b['inputs']):
            raise NotFoundError('one or more employees do not belong to this company')
        PayrollInput.objects.bulk_create(rows)
        added = len(rows)
        # New inputs invalidate a maker review; the run must be recomputed and reviewed again.
        if run['status'] == RunStatus.REVIEWED:
            svc.runs(cid).filter(pk=run['id']).update(status=RunStatus.COMPUTED, reviewed_at=None)
        audit('inputs', 'payroll_run', actor_id=user.id, row_id=run['id'], after={'count': added})
    return {'added': added}


def list_inputs(user: AuthUser, id_: str) -> dict:
    run = get_input_run(user, id_)
    r = (PayrollInput.objects.filter(run_id=run['id']).order_by('employee__emp_code')
         .values('id', 'employee_id', 'component_code', 'amount', 'note', emp_code=F('employee__emp_code')))
    return {'inputs': [strs(i, 'id', 'employee_id') for i in r]}


def compute(user: AuthUser, id_: str, body: dict | None) -> dict:
    require_payroll(user)
    b = validate(ComputeIn, body or {})
    override = {'reason': b['overrideReason'], 'roles': list(user.roles)} if b.get('overrideReason') else None
    return svc.compute_run(_cid(user), parse_uuid(id_), user.id, override)


def review(user: AuthUser, id_: str, body: dict | None) -> dict:
    require_payroll(user)
    b = validate(ReviewIn, body or {})
    svc.review_run(_cid(user), parse_uuid(id_), user.id, b.get('notes'))
    return {'ok': True}


def approve(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return {'ok': True, **svc.approve_run(_cid(user), parse_uuid(id_), user.id, user.roles)}


def lock(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return svc.lock_run(_cid(user), parse_uuid(id_), user.id)


def publish(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return {'published': svc.publish_run(_cid(user), parse_uuid(id_), user.id)}


# ---------------------------------------------------------------- outputs (plan §11.10)

def ssf_sheet(user: AuthUser, id_: str) -> tuple[str, str]:
    require_payroll(user)
    run = svc.get_run(_cid(user), parse_uuid(id_))
    return f"ssf-{run['id']}.csv", ssf_sheet_csv(run['id'])


def salary_sheet_for(user: AuthUser, id_: str, query: dict) -> tuple[str, dict]:
    """(format, sheet). JSON, ?format=csv for Excel, ?format=html to print. Audited like the TS route."""
    require_payroll(user)
    fmt = validate(SheetQueryIn, query)['format']
    sheet = salary_sheet(_cid(user), parse_uuid(id_))
    if not sheet:
        raise NotFoundError('payroll run not found')
    if not sheet['rows'] and fmt != 'json':
        raise ConflictError('this run has no payslips yet; calculate it first')
    audit('salary_sheet', 'payroll_run', actor_id=user.id, row_id=sheet['run']['id'], after={'format': fmt})
    return fmt, sheet


# ---------------------------------------------------------------- payslips (employee self-service, plan §13.7)

def can_see_payslip(user: AuthUser, employee_id: str) -> bool:
    return has_module(user, 'payroll') or user.employee_id == str(employee_id)


def payslips(company_id: str):
    """Payslips of the company's runs."""
    return Payslip.objects.filter(run__company_id=company_id)


def list_payslips(user: AuthUser, query: dict) -> dict:
    qy = plain(validate(PayslipsQueryIn, query))
    employee_id = qy.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    assert_manages(user, employee_id)   # 404 outside the company, 403 for unrelated employees
    if not can_see_payslip(user, employee_id):
        raise ForbiddenError('payslips are visible to the employee and to HR only')
    qs = payslips(_cid(user)).filter(employee_id=employee_id)
    if not has_module(user, 'payroll'):
        qs = qs.filter(published_at__isnull=False)
    fy = qy.get('fiscalYearStart')
    if fy is not None:
        qs = qs.filter(Q(run__period__bs_month__gte=4, run__period__bs_year=fy) | Q(run__period__bs_month__lt=4, run__period__bs_year=fy + 1))
    r = (qs.order_by('-run__period__bs_year', '-run__period__bs_month', '-created_at')
         .values('id', 'run_id', 'paid_days', 'lop_days', 'ot_hours', 'gross', 'tds', 'total_deductions', 'net', 'published_at',
                 run_type=F('run__run_type'), bs_year=F('run__period__bs_year'), bs_month=F('run__period__bs_month'),
                 locked_at=F('run__locked_at')))
    return {'payslips': [strs(s, 'id', 'run_id') for s in r]}


PAYSLIP_FIELDS = ('id', 'run_id', 'employee_id', 'inputs_snapshot', 'rule_version_ids', 'bs_days_in_month', 'paid_days', 'lop_days',
                  'ot_hours', 'gross', 'taxable', 'tds', 'employee_statutory', 'employer_statutory', 'total_deductions', 'net', 'ytd',
                  'explain', 'flags', 'published_at', 'created_at')


def load_payslip(user: AuthUser, id_: str) -> tuple[dict, list[dict]]:
    pid = parse_uuid(id_)
    row = (payslips(company_id_of(user)).filter(pk=pid)
           .annotate(department=department_on(OuterRef('run__period__end_ad'), 'employee_id'))
           .values(*PAYSLIP_FIELDS, 'department', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                   name_ne=F('employee__name_ne'), designation=F('employee__designation'), ssf_id=F('employee__ssf_id'),
                   bank_account_last4=F('employee__bank_account_last4'), pan_enc=F('employee__pan_enc'),
                   company_en=F('run__company__name_en'), company_ne=F('run__company__name_ne'), bs_year=F('run__period__bs_year'),
                   bs_month=F('run__period__bs_month'), start_ad=F('run__period__start_ad'), end_ad=F('run__period__end_ad'),
                   locked_at=F('run__locked_at'), company_id=F('run__company_id'))
           .first())
    if not row:
        raise NotFoundError('payslip not found')
    row = strs(row, 'id', 'run_id', 'employee_id', 'company_id')
    row['start_ad'], row['end_ad'] = row['start_ad'].isoformat(), row['end_ad'].isoformat()
    if not can_see_payslip(user, row['employee_id']):
        raise ForbiddenError()
    if not has_module(user, 'payroll') and not row['published_at']:
        raise NotFoundError('payslip not found')
    lines = list(PayslipLine.objects.filter(payslip_id=pid).order_by('sort_order')
                 .values('type', 'name_en', 'name_ne', 'amount', 'basis', 'sort_order', code=F('component_code')))
    hold = (PayrollHold.objects.filter(run_id=row['run_id'], employee_id=row['employee_id'],
                                       status__in=(PayrollHold.Status.HELD, PayrollHold.Status.RELEASING))
            .values('status', 'reason').first())
    row['held'] = {'status': hold['status'], 'reason': hold['reason'] if has_module(user, 'payroll') else None} if hold else None
    return row, lines


def payslip_detail(user: AuthUser, id_: str) -> dict:
    row, lines = load_payslip(user, id_)
    rest = {k: v for k, v in row.items() if k not in ('pan_enc', 'inputs_snapshot')}
    return {'payslip': rest, 'lines': lines, 'tax': ((row.get('inputs_snapshot') or {}).get('result') or {}).get('tax')}


def payslip_html(user: AuthUser, id_: str) -> str:
    row, lines = load_payslip(user, id_)
    pol = get_setting(_cid(user), 'payslip.policy', {}) or {}
    numerals = 'devanagari' if pol.get('numerals') == 'devanagari' else 'latin'
    pan = field_cipher().decrypt(bytes(row['pan_enc']), f"employee:{row['employee_id']}:pan") if row['pan_enc'] is not None else None
    ytd = row.get('ytd') or {}
    return render_payslip_html({
        'company': {'nameEn': row['company_en'], 'nameNe': row['company_ne']}, 'payslipId': row['id'], 'numerals': numerals,
        'employee': {'code': row['emp_code'], 'nameEn': row['name_en'], 'nameNe': row['name_ne'], 'designation': row['designation'],
                     'department': row['department'], 'panMasked': f'*****{pan[-4:]}' if pan else None,
                     'ssfMasked': f"*******{str(row['ssf_id'])[-4:]}" if row['ssf_id'] else None,
                     'bankMasked': f"****{row['bank_account_last4']}" if row['bank_account_last4'] else None},
        'period': {'bsYear': row['bs_year'], 'bsMonth': row['bs_month'], 'startAd': row['start_ad'], 'endAd': row['end_ad']},
        'paidDays': str(row['paid_days']), 'lopDays': str(row['lop_days']), 'otHours': str(row['ot_hours']),
        'lines': [{'code': ln['code'], 'type': ln['type'], 'nameEn': ln['name_en'], 'nameNe': ln['name_ne'], 'amount': str(ln['amount'])} for ln in lines],
        'gross': str(row['gross']), 'totalDeductions': str(row['total_deductions']), 'employerStatutory': str(row['employer_statutory']),
        'net': str(row['net']), 'ytd': {'gross': ytd.get('gross') or '0', 'tds': ytd.get('tds') or '0', 'statutory': ytd.get('statutory') or '0'},
        'explain': row.get('explain') or [], 'held': bool(row['held']),
    })


def has_nul(v) -> bool:
    if isinstance(v, str):
        return '\x00' in v
    if isinstance(v, dict):
        return any(has_nul(k) or has_nul(x_) for k, x_ in v.items())
    if isinstance(v, (list, tuple)):
        return any(has_nul(x_) for x_ in v)
    return False
