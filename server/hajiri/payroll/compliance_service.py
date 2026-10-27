"""
Payroll hooks for the compliance extras (port of payroll/compliance-service.ts): opening balances in year-to-date, a
CIT amount that starts from a chosen month, and held pay (a held net is released once, through a later run as a
non-taxable line, or outside payroll).
"""
from __future__ import annotations

from engines.payroll.numeric import D

from hajiri.core.audit import audit

from ._db import fx, q, q1, x


def opening_balance(employee_id: str, fiscal_year_start: int) -> dict | None:
    return q1("""SELECT id, company_id, employee_id, fiscal_year_start, gross::text, taxable::text, tds_paid::text, retirement_employee::text,
                        retirement_employer::text, festival_paid, months, source, note
                   FROM payroll_opening_balance WHERE employee_id = $1 AND fiscal_year_start = $2""", [str(employee_id), fiscal_year_start])


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


def release_run_claims(run_id: str) -> None:
    """Start of a (re)compute: releases this run had claimed go back to the queue before they are claimed again."""
    x("UPDATE payroll_hold SET release_run_id = NULL WHERE release_run_id = $1 AND status = 'releasing'", [str(run_id)])


def claim_releases(company_id: str, run_id: str, run_type: str, employee_id: str) -> list[dict]:
    """Claim the employee's held pay that HR released through payroll; returned as one-time inputs."""
    if run_type not in RELEASING_RUNS:
        return []
    r = q("""UPDATE payroll_hold h SET release_run_id = $3 FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
              WHERE h.run_id = r.id AND h.company_id = $1 AND h.employee_id = $2 AND h.status = 'releasing' AND h.release_mode = 'payroll'
                AND (h.release_run_id IS NULL OR h.release_run_id = $3) AND h.run_id <> $3
              RETURNING h.amount::text, p.bs_year, p.bs_month""", [str(company_id), str(employee_id), str(run_id)])
    return [{'code': 'HELD_RELEASE', 'amount': fx(h['amount']), 'note': f"pay held in {h['bs_year']}-{h['bs_month']:02d}"}
            for h in r if D(h['amount']) > 0]


def with_release_component(company_id: str, components: list[dict]) -> list[dict]:
    if any(c['code'] == RELEASE_COMPONENT['code'] for c in components):
        return components
    custom = q1('SELECT name_en FROM salary_component WHERE company_id = $1 AND code = $2 AND active', [str(company_id), RELEASE_COMPONENT['code']])
    return [*components, {**RELEASE_COMPONENT, 'nameEn': (custom or {}).get('name_en') or RELEASE_COMPONENT['nameEn']}]


def sync_run_holds(run_id: str) -> None:
    """After a (re)compute: held amounts follow the new net; a hold whose employee is no longer in the run is cancelled."""
    x("""UPDATE payroll_hold h SET amount = ps.net FROM payslip ps
          WHERE h.run_id = $1 AND h.status = 'held' AND ps.run_id = h.run_id AND ps.employee_id = h.employee_id""", [str(run_id)])
    x("""UPDATE payroll_hold h SET status = 'cancelled', release_note = 'employee no longer in the run'
          WHERE h.run_id = $1 AND h.status = 'held'
            AND NOT EXISTS (SELECT 1 FROM payslip ps WHERE ps.run_id = h.run_id AND ps.employee_id = h.employee_id)""", [str(run_id)])


def settle_run_releases(run_id: str, actor_id: str) -> None:
    """Lock: releases paid by this run are done."""
    r = q("""UPDATE payroll_hold h SET status = 'released', released_at = now(), released_by = coalesce(released_by, $2)
              WHERE h.release_run_id = $1 AND h.status = 'releasing'
                AND EXISTS (SELECT 1 FROM payslip ps JOIN payslip_line l ON l.payslip_id = ps.id
                             WHERE ps.run_id = $1 AND ps.employee_id = h.employee_id AND l.component_code = 'HELD_RELEASE')
              RETURNING h.id""", [str(run_id), str(actor_id)])
    for h in r:
        audit('release', 'payroll_hold', actor_id=actor_id, row_id=str(h['id']), after={'runId': str(run_id)})
    # A release claimed by a run that ended up without the line (employee left the run) waits for the next run.
    x("UPDATE payroll_hold SET release_run_id = NULL WHERE release_run_id = $1 AND status = 'releasing'", [str(run_id)])


def hold_preflight(company_id: str, run: dict, employee_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    held = q("""SELECT e.emp_code, h.amount::text FROM payroll_hold h JOIN employee e ON e.id = h.employee_id
                 WHERE h.run_id = $1 AND h.status = 'held' ORDER BY e.emp_code""", [run['id']])
    if held:
        out.append({'code': 'pay_held', 'message': 'Pay is on hold for these employees in this run: their payslip is calculated but the net is not paid',
                    'count': len(held), 'employees': [f"{h['emp_code']}: NPR {h['amount']}" for h in held]})
    if run['run_type'] in RELEASING_RUNS and employee_ids:
        rel = q("""SELECT e.emp_code, h.amount::text FROM payroll_hold h JOIN employee e ON e.id = h.employee_id
                    WHERE h.company_id = $1 AND h.employee_id = ANY($2::uuid[]) AND h.status = 'releasing' AND h.release_mode = 'payroll'
                      AND (h.release_run_id IS NULL OR h.release_run_id = $3) AND h.run_id <> $3
                    ORDER BY e.emp_code""", [str(company_id), employee_ids, run['id']])
        if rel:
            out.append({'code': 'held_pay_release', 'message': 'Pay held earlier is released through this run (not taxed again)',
                        'count': len(rel), 'employees': [f"{h['emp_code']}: NPR {h['amount']}" for h in rel]})
    return out
