"""
Tax savings self-service (port of payroll/tax-savings.ts): an employee declares insurance premiums, retirement
contributions, medical costs and donations with proofs; HR approves (workflow 'tax_declaration'); only then is
tax_declaration updated (status 'verified'), and payroll reads only verified declarations.
"""
from __future__ import annotations

from datetime import date
from decimal import localcontext

from django.db import transaction
from django.db.models.functions import Now

from engines.bs_calendar import ad_to_bs, bs_month_bounds, fiscal_year_of
from engines.payroll import compute_payslip
from engines.payroll.numeric import ENGINE_CONTEXT, D, to_fixed

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_module
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Employee, TaxDeclaration, TaxDeclarationRequest
from hajiri.notifications.service import notify, user_id_for_employee

from .compliance_service import effective_cit, opening_balance
from .numbers import column_row, fx, round_half_up
from .rules import resolve_rules
from .serializers import DECL_FIELDS, EstimateIn, SavingsIn, SavingsQueryIn

Request = TaxDeclarationRequest
DECL_COLUMNS = {'lifePremium': 'life_premium', 'healthPremium': 'health_premium', 'citMonthly': 'cit_monthly', 'medicalCost': 'medical_cost',
                'otherRetirementAnnual': 'other_retirement_annual', 'donation': 'donation'}


def fy_label(fy: int) -> str:
    return f'{fy}/{str(fy + 1)[-2:]}'


def _req(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = column_row(TaxDeclarationRequest, r)
    for k in ('id', 'company_id', 'employee_id', 'workflow_instance_id', 'created_by', 'decided_by'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['proof_file_ids'] = [str(f) for f in (out.get('proof_file_ids') or [])]
    for k in ('life_premium', 'health_premium', 'cit_monthly', 'medical_cost', 'other_retirement_annual', 'donation'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def _decl(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = column_row(TaxDeclaration, r)
    for k in ('id', 'employee_id', 'verified_by'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    for k in ('life_premium', 'health_premium', 'cit_monthly', 'medical_cost', 'other_retirement_annual', 'donation', 'cit_prev_monthly'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def to_input(v: dict | None, month_index_in_fy: int | None = None) -> dict:
    v = v or {}
    return {'lifePremium': v.get('life_premium') or '0', 'healthPremium': v.get('health_premium') or '0',
            'citMonthly': effective_cit(v or None, month_index_in_fy) if month_index_in_fy else (v.get('cit_monthly') or '0'),
            'medicalCost': v.get('medical_cost') or '0', 'otherRetirementAnnual': v.get('other_retirement_annual') or '0',
            'donation': v.get('donation') or '0'}


def current_fiscal_year_start() -> int:
    """Current fiscal year (BS start year) in Nepal time."""
    return fiscal_year_of(ad_to_bs(npt_today().isoformat())).start_year


# ---------------------------------------------------------------- decision handler

def requests(company_id: str | None = None):
    """Tax savings requests (of one company when given)."""
    return Request.objects.for_company(company_id) if company_id else Request.objects.all()


def verify(employee_id: str, fiscal_year_start: int, values: dict, actor_id: str) -> None:
    """Record a declaration as verified by `actor_id` (HR approval, or HR entering it); a later CIT change starts over."""
    TaxDeclaration.objects.update_or_create(
        employee_id=employee_id, fiscal_year_start=fiscal_year_start,
        defaults={**values, 'cit_from_month': None, 'cit_prev_monthly': 0, 'status': TaxDeclaration.Status.VERIFIED,
                  'verified_by_id': actor_id, 'verified_at': Now(), 'updated_at': Now()})


def apply_tax_declaration_decision(request_id: str, state: str, ctx) -> None:
    """Workflow decision handler for 'tax_declaration' (the workflow engine checks the approver's access)."""
    req = _req(requests().select_for_update().filter(pk=as_uuid(request_id)).values().first())
    if not req:
        raise NotFoundError('tax declaration request not found')
    if req['status'] != Request.Status.PENDING:
        raise ConflictError(f"request already {req['status']}")
    requests().filter(pk=req['id']).update(status=state, decided_by_id=ctx.actor_id, decided_at=Now(), decision_note=ctx.reason)
    if state == Request.Status.APPROVED:
        verify(req['employee_id'], req['fiscal_year_start'], {col: req[col] for col in DECL_COLUMNS.values()}, ctx.actor_id)
    audit(state, 'tax_declaration_request', actor_id=ctx.actor_id, row_id=request_id,
          after={'fiscalYearStart': req['fiscal_year_start'], 'reason': ctx.reason})
    fy = req['fiscal_year_start']
    notify(company_id=req['company_id'], user_id=user_id_for_employee(req['employee_id']), employee_id=req['employee_id'],
           template=f'tax_declaration.{state}',
           title='Tax savings approved' if state == 'approved' else 'Tax savings not accepted',
           body=(f'Your tax savings for FY {fy_label(fy)} were accepted; your monthly tax is recalculated from the next payroll.'
                 if state == 'approved' else
                 f"HR did not accept your tax savings for FY {fy_label(fy)}{f': {ctx.reason}' if ctx.reason else ''}."),
           payload={'requestId': str(request_id)}, channels=('push',))


def verified(employee_id: str, fiscal_year_start: int, *fields: str) -> dict | None:
    return _decl(TaxDeclaration.objects.filter(employee_id=employee_id, fiscal_year_start=fiscal_year_start,
                                               status=TaxDeclaration.Status.VERIFIED).values(*fields).first())


def tax_declaration_summary(inst: dict) -> dict:
    x_ = _req(requests().filter(pk=as_uuid(inst['object_id'])).values().first())
    if not x_:
        return {}
    cur = verified(x_['employee_id'], x_['fiscal_year_start'], *DECL_COLUMNS.values())
    return {'fiscalYearStart': x_['fiscal_year_start'], 'lifePremium': x_['life_premium'], 'healthPremium': x_['health_premium'],
            'citMonthly': x_['cit_monthly'], 'medicalCost': x_['medical_cost'], 'otherRetirementAnnual': x_['other_retirement_annual'],
            'donation': x_['donation'], 'note': x_['note'], 'proofs': len(x_['proof_file_ids']), 'current': to_input(cur) if cur else None}


# ---------------------------------------------------------------- requests

def submit_tax_savings(user: AuthUser, company_id: str, employee_id: str, fiscal_year_start: int, values: dict, *,
                       note: str | None = None, proof_file_ids: list[str] | None = None) -> dict:
    """Call inside a transaction. One pending request per year: a new submission replaces the undecided one."""
    from hajiri.files.store import attach_file
    from hajiri.workflow.service import cancel_workflow, start_workflow

    pending = requests(company_id).filter(employee_id=employee_id, fiscal_year_start=fiscal_year_start, status=Request.Status.PENDING)
    for pid in pending.select_for_update().values_list('id', flat=True):
        requests().filter(pk=pid).update(status=Request.Status.CANCELLED, decided_at=Now(), decision_note='replaced by a newer submission')
        cancel_workflow('tax_declaration', str(pid))
    proofs = list(dict.fromkeys(proof_file_ids or []))
    ins = Request.objects.create(
        company_id=company_id, employee_id=employee_id, fiscal_year_start=fiscal_year_start, note=note, proof_file_ids=proofs,
        created_by_id=user.id, **{DECL_COLUMNS[k]: values[k] for k in DECL_FIELDS if k != 'donation'},
        donation=values.get('donation') or '0')
    rid = str(ins.pk)
    for f in proofs:
        attach_file(company_id=company_id, file_id=f, user_id=user.id, purpose=['employee_document', 'tax_proof'],
                    attached_to=f'tax_declaration_request:{rid}')
    inst = start_workflow(company_id=company_id, object_type='tax_declaration', object_id=rid, employee_id=employee_id,
                          title='Tax savings to verify',
                          body=f"Tax savings declaration for FY {fy_label(fiscal_year_start)}{f' with {len(proofs)} proof file(s)' if proofs else ''}.")
    requests().filter(pk=rid).update(workflow_instance_id=inst['id'])
    audit('create', 'tax_declaration_request', actor_id=user.id, row_id=rid,
          after={'fiscalYearStart': fiscal_year_start, **values, 'proofs': len(proofs)})
    return {**_req(requests().filter(pk=rid).values().get()), 'workflow_instance_id': str(inst['id'])}


def pending_tax_savings(employee_id: str, fiscal_year_start: int) -> dict | None:
    return _req(requests().filter(employee_id=employee_id, fiscal_year_start=fiscal_year_start, status=Request.Status.PENDING)
                .values().first())


def decide_tax_savings(user: AuthUser, req: dict, decision: str, reason: str | None) -> None:
    """HR decision from the declarations screen; through the workflow engine when the request has an instance."""
    from hajiri.workflow.service import DecisionContext, act
    if req.get('workflow_instance_id'):
        act(instance_id=req['workflow_instance_id'], user=user, decision='approve' if decision == 'approved' else 'reject',
            reason=reason if reason is not None else ('Not accepted by HR' if decision == 'rejected' else None))
        return
    if user.employee_id == req['employee_id']:
        raise ForbiddenError('you cannot verify your own declaration')
    apply_tax_declaration_decision(req['id'], decision, DecisionContext(user.id, reason, {}))


# ---------------------------------------------------------------- tax estimate

def _summary(t: dict, declaration: dict) -> dict:
    return {'projectedAnnualTaxable': t['projectedAnnualTaxable'], 'retirementDeduction': t['retirementDeduction'],
            'insuranceDeduction': t['insuranceDeduction'], 'donationDeduction': t.get('donationDeduction') or '0.00',
            'taxableAfterDeductions': t['taxableAfterDeductions'], 'medicalCredit': t['medicalCredit'], 'annualTax': t['annualTax'],
            'tdsThisMonth': t['tdsThisMonth'], 'remainingMonths': t['remainingMonths'], 'declaration': declaration}


def estimate_input(company_id: str, employee_id: str, fiscal_year_start: int) -> dict:
    """
    The payroll engine input for this month's payslip of one employee, as a real run would build it, with full
    attendance, no one-offs and the verified declaration. Returns {input, bsYear, bsMonth} or {reason}.
    """
    from .runs import active_employees, components_for, payroll_policy, year_to_date
    today = npt_today().isoformat()
    bs = ad_to_bs(today)
    fy = fiscal_year_of(bs)
    if fy.start_year != fiscal_year_start:
        return {'reason': 'Estimates are available for the current fiscal year only.'}
    b = bs_month_bounds(bs.year, bs.month)
    p = {'id': 'estimate', 'company_id': company_id, 'bs_year': bs.year, 'bs_month': bs.month, 'start_ad': b['startAd'], 'end_ad': b['endAd'],
         'days': b['days'], 'attendance_locked_at': None}
    emps = active_employees(company_id, p, {'params': {'employeeIds': [employee_id]}})
    emp = emps[0] if emps else None
    if not emp or not emp['assignment']:
        return {'reason': 'The salary is not set up in Hajiri yet, so tax cannot be estimated.'}
    components = components_for(company_id, str(emp['assignment']['structure_version_id']))
    policy = payroll_policy(company_id)
    rules, _ = resolve_rules(p['end_ad'])
    y = year_to_date(employee_id, fy.start_ad, p['start_ad'], inclusive=False)
    ob = opening_balance(employee_id, fy.start_year)
    declared = verified(employee_id, fiscal_year_start, *DECL_COLUMNS.values(), 'cit_from_month', 'cit_prev_monthly')
    month_index = ((p['bs_month'] - 4 + 12) % 12) + 1
    service_from = emp['join_date'] if emp['join_date'] and emp['join_date'] > fy.start_ad else fy.start_ad
    months = 12 if service_from == fy.start_ad else min(12, max(1, round_half_up(
        (date.fromisoformat(fy.end_ad) - date.fromisoformat(service_from)).days / 30.44)))

    def plus(a, b2):
        return fx(D(a) + D(b2 if b2 is not None else 0))
    input_ = {
        'employee': {'id': emp['id'], 'code': emp['emp_code'], 'nameEn': emp['name_en'], 'nameNe': emp['name_ne'], 'gender': emp['gender'],
                     'maritalStatus': emp['marital_status'], 'isResident': emp['is_resident'], 'regime': emp['regime'],
                     'remoteAreaClass': emp['remote_area_class'], 'disability': emp['disability'], 'otEligible': emp['ot_eligible'],
                     'festivalMonth': emp['festival_month']},
        'period': {'bsYear': p['bs_year'], 'bsMonth': p['bs_month'], 'daysInMonth': p['days'], 'fiscalYearStart': fy.start_year,
                   'monthIndexInFy': month_index},
        'components': components, 'baseAmounts': emp['assignment']['base_amounts'], 'inputs': [],
        'attendance': {'paidDays': str(p['days']), 'lopDays': '0', 'otHours': '0'},
        'ytd': {'taxable': plus(y['taxable'], (ob or {}).get('taxable')), 'tdsPaid': plus(y['tds'], (ob or {}).get('tds_paid')),
                'retirementEmployee': plus(y['ee'], (ob or {}).get('retirement_employee')),
                'retirementEmployer': plus(y['er'], (ob or {}).get('retirement_employer')),
                'festivalPaid': bool(y['festival']) or bool((ob or {}).get('festival_paid'))},
        'declaration': to_input(declared, month_index), 'loans': [], 'rules': rules, 'policy': policy,
        'extras': {'monthsOfServiceInFy': months},
    }
    return {'input': input_, 'bsYear': bs.year, 'bsMonth': bs.month}


def estimate_tax(company_id: str, employee_id: str, fiscal_year_start: int, proposed: dict | None) -> dict:
    """What the engine would project for this month's payslip with the verified declaration and with a proposed one."""
    built = estimate_input(company_id, employee_id, fiscal_year_start)
    if 'reason' in built:
        return {'available': False, 'reason': built['reason'], 'fiscalYearStart': fiscal_year_start}

    def run(declaration):
        return compute_payslip({**built['input'], 'declaration': declaration})
    current_decl = built['input']['declaration']
    cur = _summary(run(current_decl)['tax'], current_decl)
    out = {'available': True, 'fiscalYearStart': fiscal_year_start, 'bsYear': built['bsYear'], 'bsMonth': built['bsMonth'], 'current': cur}
    if proposed:
        prop = _summary(run(proposed)['tax'], proposed)
        out['proposed'] = prop
        with localcontext(ENGINE_CONTEXT):
            out['annualSaving'] = to_fixed(D(cur['annualTax']) - D(prop['annualTax']), 2)
            out['monthlySaving'] = to_fixed(D(cur['tdsThisMonth']) - D(prop['tdsThisMonth']), 2)
    return out


# ---------------------------------------------------------------- routes (/payroll/tax-savings)

def _employee_exists(company_id: str, employee_id: str) -> bool:
    return Employee.objects.for_company(company_id).filter(pk=as_uuid(employee_id)).exists()


def lock_employee(employee_id: str) -> None:
    """Serialise submissions of one employee (a row lock that does not block other writes referencing the employee)."""
    Employee.objects.select_for_update(no_key=True).filter(pk=employee_id).first()


def get_savings(user: AuthUser, query: dict) -> dict:
    from hajiri.files.store import file_meta
    qy = plain(validate(SavingsQueryIn, query))
    company_id = company_id_of(user)
    employee_id = qy.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('this account is not linked to an employee')
    if employee_id != user.employee_id and not has_module(user, 'payroll'):
        raise ForbiddenError()
    if not _employee_exists(company_id, employee_id):
        raise NotFoundError('employee not found')
    fy = qy.get('fiscalYearStart') or current_fiscal_year_start()
    declaration = _decl(TaxDeclaration.objects.for_company(company_id).filter(employee_id=employee_id, fiscal_year_start=fy).values().first())
    history = [_req(h) for h in requests(company_id).filter(employee_id=employee_id, fiscal_year_start=fy).order_by('-created_at', '-id')
               .values()[:20]]
    files: dict[str, dict] = {}
    for h in history:
        for f in h['proof_file_ids']:
            m = file_meta(company_id, f)
            if m:
                files[f] = {'id': str(m['id']), 'filename': m['filename'], 'mime': m['mime'], 'bytes': m['bytes']}
    return {'fiscalYearStart': fy, 'declaration': declaration,
            'pending': next((h for h in history if h['status'] == Request.Status.PENDING), None),
            'history': [{**h, 'proofs': [files[f] for f in h['proof_file_ids'] if f in files]} for h in history]}


def submit_savings(user: AuthUser, body: dict) -> dict:
    b = plain(validate(SavingsIn, body))
    employee_id = user.employee_id
    if not employee_id:
        raise BadRequestError('this account is not linked to an employee')
    company_id = company_id_of(user)
    cur = current_fiscal_year_start()
    if b['fiscalYearStart'] < cur - 1 or b['fiscalYearStart'] > cur + 1:
        raise BadRequestError('declarations can be made for the current, previous or next fiscal year only')
    values = {k: b[k] for k in DECL_FIELDS}
    with transaction.atomic():
        lock_employee(employee_id)
        row = submit_tax_savings(user, company_id, employee_id, b['fiscalYearStart'], values, note=b.get('note'),
                                 proof_file_ids=b['proofFileIds'])
    return {'request': row}


def cancel_savings(user: AuthUser, request_id: str) -> dict:
    from hajiri.workflow.service import cancel_workflow
    rid = parse_uuid(request_id)
    with transaction.atomic():
        r = _req(requests(company_id_of(user)).select_for_update().filter(pk=rid).values().first())
        if not r or r['employee_id'] != user.employee_id:
            raise NotFoundError('request not found')
        if r['status'] != Request.Status.PENDING:
            raise ConflictError(f"request already {r['status']}")
        requests().filter(pk=rid).update(status=Request.Status.CANCELLED, decided_at=Now())
        cancel_workflow('tax_declaration', rid)
        audit('cancel', 'tax_declaration_request', actor_id=user.id, row_id=rid)
    return {'ok': True}


def estimate(user: AuthUser, body: dict) -> dict:
    b = plain(validate(EstimateIn, body or {}))
    company_id = company_id_of(user)
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('this account is not linked to an employee')
    if employee_id != user.employee_id and not has_module(user, 'payroll'):
        raise ForbiddenError()
    if not _employee_exists(company_id, employee_id):
        raise NotFoundError('employee not found')
    return estimate_tax(company_id, employee_id, b.get('fiscalYearStart') or current_fiscal_year_start(),
                        dict(b['proposed']) if b.get('proposed') else None)


def proof_file(user: AuthUser, request_id: str, file_id: str) -> dict:
    """A proof file: the employee who submitted it and HR. HR views are audited."""
    from hajiri.files.store import read_file
    rid, fid = parse_uuid(request_id), parse_uuid(file_id)
    company_id = company_id_of(user)
    r = _req(requests(company_id).filter(pk=rid).values().first())
    own = bool(r) and r['employee_id'] == user.employee_id
    if not r or (not own and not has_module(user, 'payroll')) or fid not in r['proof_file_ids']:
        raise NotFoundError('file not found')
    f = read_file(company_id, fid)
    if not own:
        audit('view_file', 'tax_declaration_request', actor_id=user.id, row_id=rid, after={'fileId': fid})
    return f
