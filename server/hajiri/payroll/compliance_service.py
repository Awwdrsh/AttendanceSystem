"""
Payroll hooks for the compliance extras (port of payroll/compliance-service.ts): opening balances in year-to-date, a
CIT amount that starts from a chosen month, and held pay (a held net is released once, through a later run as a
non-taxable line, or outside payroll).
"""
from __future__ import annotations

from django.db.models import Exists, F, OuterRef, Q, Subquery, UUIDField, Value
from django.db.models.functions import Coalesce, Now

from engines.payroll.numeric import D

from hajiri.core.audit import audit
from hajiri.db.models import PayrollHold, PayrollOpeningBalance, Payslip, PayslipLine, SalaryComponent

from .numbers import fx

Status = PayrollHold.Status

OPENING_FIELDS = ('id', 'company_id', 'employee_id', 'fiscal_year_start', 'gross', 'taxable', 'tds_paid', 'retirement_employee',
                  'retirement_employer', 'festival_paid', 'months', 'source', 'note')
OPENING_AMOUNTS = ('gross', 'taxable', 'tds_paid', 'retirement_employee', 'retirement_employer')


def opening_balance(employee_id: str, fiscal_year_start: int) -> dict | None:
    """The employee's opening balance for a fiscal year, amounts as strings (the employee id is already scoped)."""
    r = (PayrollOpeningBalance.objects.filter(employee_id=employee_id, fiscal_year_start=fiscal_year_start)
         .values(*OPENING_FIELDS).first())
    if r:
        for k in OPENING_AMOUNTS:
            r[k] = str(r[k])
    return r


def effective_cit(d: dict | None, month_index_in_fy: int) -> str:
    """The CIT for a month of the fiscal year: the amount applied from `cit_from_month` onward, the earlier amount before it."""
    if not d:
        return '0'
    if d.get('cit_from_month') and month_index_in_fy < d['cit_from_month']:
        return fx(d.get('cit_prev_monthly') or 0)
    return fx(d['cit_monthly'])


# ---------------------------------------------------------------- held pay

RELEASE_COMPONENT = {'code': 'HELD_RELEASE', 'type': 'earning', 'nameEn': 'Released pay (held earlier)', 'nameNe': None,
                     'dependsOnPaymentDays': False, 'taxable': False, 'rounding': 'paisa', 'sortOrder': 98}
# Runs that can carry a release; arrears and bonus runs replace their inputs and never do.
RELEASING_RUNS = frozenset({'regular', 'supplementary', 'off_cycle', 'final_settlement'})


def releasable(company_id: str, run_id: str):
    """Holds HR released through payroll that this run may pay: not claimed by another run, held in an earlier run."""
    return (PayrollHold.objects.for_company(company_id)
            .filter(Q(release_run__isnull=True) | Q(release_run_id=run_id), status=Status.RELEASING,
                    release_mode=PayrollHold.ReleaseMode.PAYROLL)
            .exclude(run_id=run_id))


def release_run_claims(run_id: str) -> None:
    """Start of a (re)compute: releases this run had claimed go back to the queue before they are claimed again."""
    PayrollHold.objects.filter(release_run_id=run_id, status=Status.RELEASING).update(release_run=None)


def claim_releases(company_id: str, run_id: str, run_type: str, employee_id: str) -> list[dict]:
    """Claim the employee's held pay that HR released through payroll; returned as one-time inputs."""
    if run_type not in RELEASING_RUNS:
        return []
    claim = releasable(company_id, run_id).filter(employee_id=employee_id)
    holds = list(claim.select_for_update(of=('self',))
                 .values('id', 'amount', bs_year=F('run__period__bs_year'), bs_month=F('run__period__bs_month')))
    if holds:
        PayrollHold.objects.filter(pk__in=[h['id'] for h in holds]).update(release_run_id=run_id)
    return [{'code': 'HELD_RELEASE', 'amount': fx(h['amount']), 'note': f"pay held in {h['bs_year']}-{h['bs_month']:02d}"}
            for h in holds if D(h['amount']) > 0]


def with_release_component(company_id: str, components: list[dict]) -> list[dict]:
    if any(c['code'] == RELEASE_COMPONENT['code'] for c in components):
        return components
    custom = (SalaryComponent.objects.for_company(company_id).filter(code=RELEASE_COMPONENT['code'], active=True)
              .values_list('name_en', flat=True).first())
    return [*components, {**RELEASE_COMPONENT, 'nameEn': custom or RELEASE_COMPONENT['nameEn']}]


def _run_payslip(run_id: str):
    return Payslip.objects.filter(run_id=run_id, employee_id=OuterRef('employee_id'))


def sync_run_holds(run_id: str) -> None:
    """After a (re)compute: held amounts follow the new net; a hold whose employee is no longer in the run is cancelled."""
    held = PayrollHold.objects.filter(run_id=run_id, status=Status.HELD)
    held.filter(Exists(_run_payslip(run_id))).update(amount=Subquery(_run_payslip(run_id).values('net')[:1]))
    held.exclude(Exists(_run_payslip(run_id))).update(status=Status.CANCELLED, release_note='employee no longer in the run')


def settle_run_releases(run_id: str, actor_id: str) -> None:
    """Lock: releases paid by this run are done."""
    paid_line = PayslipLine.objects.filter(payslip__run_id=run_id, payslip__employee_id=OuterRef('employee_id'),
                                           component_code='HELD_RELEASE')
    paid = PayrollHold.objects.filter(Exists(paid_line), release_run_id=run_id, status=Status.RELEASING)
    ids = list(paid.select_for_update().values_list('id', flat=True))
    if ids:
        PayrollHold.objects.filter(pk__in=ids).update(
            status=Status.RELEASED, released_at=Now(),
            released_by_id=Coalesce(F('released_by_id'), Value(actor_id, output_field=UUIDField()), output_field=UUIDField()))
    for hid in ids:
        audit('release', 'payroll_hold', actor_id=actor_id, row_id=str(hid), after={'runId': str(run_id)})
    # A release claimed by a run that ended up without the line (employee left the run) waits for the next run.
    release_run_claims(run_id)


def hold_preflight(company_id: str, run: dict, employee_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    held = list(PayrollHold.objects.for_company(company_id).filter(run_id=run['id'], status=Status.HELD)
                .order_by('employee__emp_code').values_list('employee__emp_code', 'amount'))
    if held:
        out.append({'code': 'pay_held', 'message': 'Pay is on hold for these employees in this run: their payslip is calculated but the net is not paid',
                    'count': len(held), 'employees': [f'{code}: NPR {amount}' for code, amount in held]})
    if run['run_type'] in RELEASING_RUNS and employee_ids:
        rel = list(releasable(company_id, run['id']).filter(employee_id__in=employee_ids)
                   .order_by('employee__emp_code').values_list('employee__emp_code', 'amount'))
        if rel:
            out.append({'code': 'held_pay_release', 'message': 'Pay held earlier is released through this run (not taxed again)',
                        'count': len(rel), 'employees': [f'{code}: NPR {amount}' for code, amount in rel]})
    return out
