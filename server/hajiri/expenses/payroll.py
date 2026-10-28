"""
Payroll side of expenses and travel (port of apps/api/src/modules/expenses/payroll.ts). Approved claims, travel
advances and settlements become expense_payout rows; computing a run claims the employee's pending payouts for that
run and adds them as one-time inputs, recomputing releases and re-claims them, and locking the run marks them paid.
A payout is held by at most one run at a time, so it is paid exactly once.

The payroll app calls, inside its own transactions:

    release_run_payouts(run_id)                                   # start of a (re)compute
    claim_payouts(company_id, run_id, run_type, employee_id)      # -> [{'code', 'amount', 'note'}] one-time inputs
    with_payout_components(company_id, components, inputs)        # structure + EXP_REIMB / TRAVEL_ADV / EXP_RECOVERY
    payout_preview(company_id, run_id, run_type, employee_ids)    # pre-flight figures
    preview_message(preview)                                      # the pre-flight warning text
    settle_run_payouts(run_id)                                    # on lock
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Now

from hajiri.db.models import ExpenseClaim, ExpensePayout, PayslipLine, SalaryComponent, TravelRequest

from .services import D, money, rs

Payout = ExpensePayout.Status
Source = ExpensePayout.SourceType
Code = ExpensePayout.ComponentCode

PAYOUT_COMPONENTS: dict[str, dict] = {
    'EXP_REIMB': {'code': 'EXP_REIMB', 'type': 'earning', 'nameEn': 'Expense reimbursement', 'nameNe': None,
                  'dependsOnPaymentDays': False, 'taxable': False, 'rounding': 'paisa', 'sortOrder': 96},
    'TRAVEL_ADV': {'code': 'TRAVEL_ADV', 'type': 'earning', 'nameEn': 'Travel advance', 'nameNe': None,
                   'dependsOnPaymentDays': False, 'taxable': False, 'rounding': 'paisa', 'sortOrder': 97},
    'EXP_RECOVERY': {'code': 'EXP_RECOVERY', 'type': 'deduction', 'nameEn': 'Travel advance recovery', 'nameNe': None,
                     'dependsOnPaymentDays': False, 'taxable': False, 'rounding': 'paisa', 'sortOrder': 272},
}

# Run types that pay reimbursements; arrears and bonus runs replace their inputs and never carry them.
PAYING_RUNS = frozenset({'regular', 'supplementary', 'off_cycle', 'final_settlement'})


def release_run_payouts(run_id: str) -> None:
    """Start of a (re)compute: everything this run held goes back to the pool before it is claimed again."""
    ExpensePayout.objects.filter(payroll_run_id=run_id, status=Payout.PENDING).update(payroll_run=None)


def _claimable(company_id: str, run_id: str):
    """Pending payouts no other run holds (free, or already held by this run)."""
    return (ExpensePayout.objects.for_company(company_id).filter(status=Payout.PENDING)
            .filter(Q(payroll_run__isnull=True) | Q(payroll_run_id=run_id)))


def claim_payouts(company_id: str, run_id: str, run_type: str, employee_id: str) -> list[dict]:
    """Claim the employee's pending payouts for this run and return them as payroll inputs."""
    if run_type not in PAYING_RUNS:
        return []
    with transaction.atomic():
        held = list(_claimable(company_id, run_id).filter(employee_id=employee_id).select_for_update()
                    .order_by('created_at', 'id').values('id', 'component_code', 'amount', 'note'))
        ExpensePayout.objects.filter(pk__in=[p['id'] for p in held]).update(payroll_run_id=run_id)
    return [{'code': p['component_code'], 'amount': str(p['amount']), 'note': p['note'] or ''} for p in held]


def with_payout_components(company_id: str, components: list[dict], inputs: list[dict]) -> list[dict]:
    """The employee's structure plus any payout component it does not already have (the company may rename them)."""
    needed = [c for c in dict.fromkeys(i['code'] for i in inputs)
              if c in PAYOUT_COMPONENTS and not any(x['code'] == c for x in components)]
    if not needed:
        return components
    custom = dict(SalaryComponent.objects.for_company(company_id).filter(code__in=needed, active=True)
                  .values_list('code', 'name_en'))
    return [*components, *({**PAYOUT_COMPONENTS[c], 'nameEn': custom.get(c, PAYOUT_COMPONENTS[c]['nameEn'])} for c in needed)]


def payout_preview(company_id: str, run_id: str, run_type: str, employee_ids: list[str]) -> dict:
    """Pre-flight: what this run will pay out (or recover) for claims and travel."""
    if run_type not in PAYING_RUNS or not employee_ids:
        return {'count': 0, 'reimburse': '0.00', 'recover': '0.00', 'employees': []}
    r = list(_claimable(company_id, run_id).filter(employee_id__in=[str(e) for e in employee_ids])
             .order_by('employee__emp_code', 'created_at', 'id').values('component_code', 'amount', emp_code=F('employee__emp_code')))
    reimburse = sum((x['amount'] for x in r if x['component_code'] != Code.EXP_RECOVERY), Decimal(0))
    recover = sum((x['amount'] for x in r if x['component_code'] == Code.EXP_RECOVERY), Decimal(0))
    by_emp: dict[str, int] = {}
    for x in r:
        by_emp[x['emp_code']] = by_emp.get(x['emp_code'], 0) + 1
    return {'count': len(r), 'reimburse': money(reimburse), 'recover': money(recover),
            'employees': [f"{c}: {n} {'item' if n == 1 else 'items'}" for c, n in by_emp.items()]}


def preview_message(p: dict) -> str:
    parts = [f"{rs(p['reimburse'])} of approved expenses and travel to pay" if D(p['reimburse']) > 0 else '',
             f"{rs(p['recover'])} of unspent travel advances to recover" if D(p['recover']) > 0 else '']
    return f"This run includes {' and '.join(x for x in parts if x)} (not taxed)."


def settle_run_payouts(run_id: str) -> None:
    """
    Lock: the run's payouts are paid. A recovery the pay could not fully cover (deductions are capped at the pay left)
    keeps the rest outstanding as a new payout for the next run, and the source records are marked paid.
    """
    with transaction.atomic():
        held = list(ExpensePayout.objects.filter(payroll_run_id=run_id, status=Payout.PENDING).select_for_update()
                    .order_by('created_at', 'id'))
        if not held:
            return
        available = {str(x['payslip__employee_id']): x['amount'] for x in (
            PayslipLine.objects.filter(payslip__run_id=run_id, component_code=Code.EXP_RECOVERY)
            .values('payslip__employee_id').annotate(amount=Sum('amount')).order_by())}
        balances = []
        for p in held:
            paid = p.amount
            if p.component_code == Code.EXP_RECOVERY:
                emp = str(p.employee_id)
                left = available.get(emp, Decimal(0))
                paid = min(left, paid)
                available[emp] = left - paid
                rest = p.amount - paid
                if rest > 0:
                    balances.append(ExpensePayout(
                        company_id=p.company_id, employee_id=p.employee_id, source_type=p.source_type, source_id=p.source_id,
                        component_code=p.component_code, amount=money(rest), note=f"{p.note or ''} (balance)"[:200],
                        parent_payout_id=p.id))
            ExpensePayout.objects.filter(pk=p.id).update(status=Payout.PAID, paid_amount=money(paid), paid_at=Now())
        ExpensePayout.objects.bulk_create(balances)
        mark_sources_paid([{'source_type': p.source_type, 'source_id': p.source_id} for p in held])


def mark_sources_paid(payouts: list[dict]) -> None:
    """A claim is paid once none of its payouts is still pending; a travel advance once its payout is paid."""
    claims = {str(p['source_id']) for p in payouts if p['source_type'] == Source.EXPENSE_CLAIM}
    advances = {str(p['source_id']) for p in payouts if p['source_type'] == Source.TRAVEL_ADVANCE}
    if claims:
        still_pending = ExpensePayout.objects.filter(source_type=Source.EXPENSE_CLAIM, source_id=OuterRef('pk'),
                                                     status=Payout.PENDING)
        (ExpenseClaim.objects.filter(pk__in=claims, status=ExpenseClaim.Status.APPROVED).exclude(Exists(still_pending))
         .update(status=ExpenseClaim.Status.PAID, paid_at=Now(), updated_at=Now()))
    if advances:
        (TravelRequest.objects.filter(pk__in=advances, advance_status=TravelRequest.AdvanceStatus.PENDING)
         .update(advance_status=TravelRequest.AdvanceStatus.PAID))


__all__ = ['PAYOUT_COMPONENTS', 'PAYING_RUNS', 'release_run_payouts', 'claim_payouts', 'with_payout_components', 'payout_preview',
           'preview_message', 'settle_run_payouts', 'mark_sources_paid']
