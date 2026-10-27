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

from hajiri.core.db import execute, rows

from .services import D, money, rs

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
    execute("UPDATE expense_payout SET payroll_run_id = NULL WHERE payroll_run_id = %s AND status = 'pending'", [str(run_id)])


def claim_payouts(company_id: str, run_id: str, run_type: str, employee_id: str) -> list[dict]:
    """Claim the employee's pending payouts for this run and return them as payroll inputs."""
    if run_type not in PAYING_RUNS:
        return []
    r = rows("""UPDATE expense_payout SET payroll_run_id = %s
                 WHERE id IN (SELECT id FROM expense_payout WHERE company_id = %s AND employee_id = %s AND status = 'pending'
                                AND (payroll_run_id IS NULL OR payroll_run_id = %s) FOR UPDATE)
                 RETURNING component_code, amount::text, note""", [str(run_id), str(company_id), str(employee_id), str(run_id)])
    return [{'code': p['component_code'], 'amount': p['amount'], 'note': p['note'] or ''} for p in r]


def with_payout_components(company_id: str, components: list[dict], inputs: list[dict]) -> list[dict]:
    """The employee's structure plus any payout component it does not already have (the company may rename them)."""
    needed = [c for c in dict.fromkeys(i['code'] for i in inputs)
              if c in PAYOUT_COMPONENTS and not any(x['code'] == c for x in components)]
    if not needed:
        return components
    custom = {x['code']: x['name_en'] for x in rows(
        'SELECT code, name_en FROM salary_component WHERE company_id = %s AND code = ANY(%s) AND active', [str(company_id), needed])}
    return [*components, *({**PAYOUT_COMPONENTS[c], 'nameEn': custom.get(c, PAYOUT_COMPONENTS[c]['nameEn'])} for c in needed)]


def payout_preview(company_id: str, run_id: str, run_type: str, employee_ids: list[str]) -> dict:
    """Pre-flight: what this run will pay out (or recover) for claims and travel."""
    if run_type not in PAYING_RUNS or not employee_ids:
        return {'count': 0, 'reimburse': '0.00', 'recover': '0.00', 'employees': []}
    r = rows("""SELECT e.emp_code, p.component_code, p.amount::text FROM expense_payout p JOIN employee e ON e.id = p.employee_id
                 WHERE p.company_id = %s AND p.employee_id = ANY(%s::uuid[]) AND p.status = 'pending'
                   AND (p.payroll_run_id IS NULL OR p.payroll_run_id = %s) ORDER BY e.emp_code""",
             [str(company_id), [str(e) for e in employee_ids], str(run_id)])
    reimburse = sum((D(x['amount']) for x in r if x['component_code'] != 'EXP_RECOVERY'), Decimal(0))
    recover = sum((D(x['amount']) for x in r if x['component_code'] == 'EXP_RECOVERY'), Decimal(0))
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
    held = rows("""SELECT id::text, company_id::text, employee_id::text, source_type, source_id::text, component_code, amount::text, note
                     FROM expense_payout WHERE payroll_run_id = %s AND status = 'pending' ORDER BY created_at FOR UPDATE""", [str(run_id)])
    if not held:
        return
    recovered = rows("""SELECT ps.employee_id::text, coalesce(sum(l.amount), 0)::text AS amount FROM payslip ps
                          JOIN payslip_line l ON l.payslip_id = ps.id
                         WHERE ps.run_id = %s AND l.component_code = 'EXP_RECOVERY' GROUP BY ps.employee_id""", [str(run_id)])
    available = {x['employee_id']: D(x['amount']) for x in recovered}
    for p in held:
        paid = D(p['amount'])
        if p['component_code'] == 'EXP_RECOVERY':
            left = available.get(p['employee_id'], Decimal(0))
            paid = left if left < paid else paid
            available[p['employee_id']] = left - paid
            rest = D(p['amount']) - paid
            if rest > 0:
                execute("""INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note,
                                                       parent_payout_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        [p['company_id'], p['employee_id'], p['source_type'], p['source_id'], p['component_code'], money(rest),
                         f"{p['note'] or ''} (balance)"[:200], p['id']])
        execute("UPDATE expense_payout SET status = 'paid', paid_amount = %s, paid_at = now() WHERE id = %s", [money(paid), p['id']])
    mark_sources_paid(held)


def mark_sources_paid(payouts: list[dict]) -> None:
    for p in payouts:
        if p['source_type'] == 'expense_claim':
            execute("""UPDATE expense_claim c SET status = 'paid', paid_at = now(), updated_at = now() WHERE c.id = %s AND c.status = 'approved'
                         AND NOT EXISTS (SELECT 1 FROM expense_payout x WHERE x.source_type = 'expense_claim' AND x.source_id = c.id
                                           AND x.status = 'pending')""", [str(p['source_id'])])
        elif p['source_type'] == 'travel_advance':
            execute("UPDATE travel_request SET advance_status = 'paid' WHERE id = %s AND advance_status = 'pending'", [str(p['source_id'])])


__all__ = ['PAYOUT_COMPONENTS', 'PAYING_RUNS', 'release_run_payouts', 'claim_payouts', 'with_payout_components', 'payout_preview',
           'preview_message', 'settle_run_payouts', 'mark_sources_paid']
