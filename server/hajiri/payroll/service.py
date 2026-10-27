"""
Pay runs (port of payroll/service.ts): periods from the BS calendar, pre-flight checks, computing every payslip with
engines.payroll, HR review, approval and lock, publishing payslips, and the Bonus Act allocation.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import localcontext
from typing import Any

from django.db import transaction

from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds, fiscal_year, fiscal_year_of
from engines.payroll import PayrollError, compute_payslip
from engines.payroll.formula import FormulaError
from engines.payroll.numeric import ENGINE_CONTEXT, D, dmin, to_dp, to_fixed

from hajiri.core.audit import audit
from hajiri.core.bsdate import bs_month_name
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from hajiri.notifications.service import notify, user_id_for_employee

from ._db import fx, js_round, q, q1, x
from .compliance_service import (claim_releases, effective_cit, hold_preflight, opening_balance, release_run_claims,
                                 settle_run_releases, sync_run_holds, with_release_component)
from .rules import resolve_rules


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _period(r: dict) -> dict:
    out = dict(r)
    out['id'] = str(out['id'])
    out['company_id'] = str(out['company_id'])
    out['start_ad'] = _iso(out['start_ad'])
    out['end_ad'] = _iso(out['end_ad'])
    return out


def _run(r: dict) -> dict:
    out = dict(r)
    for k in ('id', 'company_id', 'period_id', 'references_run_id', 'maker_id', 'checker_id'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['params'] = out.get('params') or {}
    return out


def get_period(company_id: str, period_id: str) -> dict:
    r = q1('SELECT * FROM payroll_period WHERE id = $1 AND company_id = $2', [str(period_id), str(company_id)])
    if not r:
        raise NotFoundError('payroll period not found')
    return _period(r)


def ensure_period(company_id: str, bs_year: int, bs_month: int) -> dict:
    """Raises BsRangeError outside the calendar table (the route turns it into a 400)."""
    b = bs_month_bounds(bs_year, bs_month)
    r = q1("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES ($1, $2, $3, $4, $5, $6)
              ON CONFLICT (company_id, bs_year, bs_month) DO UPDATE SET days = EXCLUDED.days RETURNING *""",
           [str(company_id), bs_year, bs_month, b['startAd'], b['endAd'], b['days']])
    return _period(r)


def get_run(company_id: str, run_id: str) -> dict:
    r = q1('SELECT * FROM payroll_run WHERE id = $1 AND company_id = $2', [str(run_id), str(company_id)])
    if not r:
        raise NotFoundError('payroll run not found')
    return _run(r)


def payroll_policy(company_id: str) -> dict:
    r = q1("SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payroll.policy'", [str(company_id)])
    v = (r or {}).get('value') or {}
    num = lambda k, d: v[k] if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool) else d  # noqa: E731
    enc = v.get('encashmentDivisor')
    return {
        'prorationBasis': v.get('prorationBasis') or 'bs_calendar_days',
        'hourlyDivisor': num('hourlyDivisor', 208),
        'rounding': 'paisa' if v.get('rounding') == 'paisa' else 'half_up_rupee',
        'includeEmployerContributionInIncome': v.get('includeEmployerContributionInIncome') is not False,
        'fullTime': True,
        'payDay': num('payDay', 25),
        'separateApprover': v.get('separateApprover') is True,
        'encashmentDivisor': 'bs_days' if enc is None else (str(enc) if not isinstance(enc, bool) else str(enc).lower()),
        'publishOnLock': v.get('publishOnLock') is True,
    }


def _engine_policy(p: dict) -> dict:
    """The PayrollPolicy the engine reads (the TS object carries the extra keys too)."""
    return dict(p)


# ---------------------------------------------------------------- pre-flight (plan §13.6 step 1)

def active_employees(company_id: str, p: dict, run: dict) -> list[dict]:
    only = run.get('params', {}).get('employeeIds')
    only = [str(i) for i in only] if isinstance(only, list) else None
    r = q("""
        SELECT e.id, e.emp_code, e.name_en, e.name_ne, e.gender, e.marital_status, e.is_resident, e.regime, e.remote_area_class, e.disability, e.ot_eligible, e.festival_month,
               e.join_date::text, e.exit_date::text,
               (SELECT jsonb_build_object('structure_version_id', a.structure_version_id, 'base_amounts', a.base_amounts, 'valid', a.valid::text)
                  FROM salary_assignment a WHERE a.employee_id = e.id AND a.valid && daterange($2::date, $3::date, '[]') ORDER BY upper_inf(a.valid) DESC, lower(a.valid) DESC LIMIT 1) AS assignment,
               (SELECT jsonb_agg(jsonb_build_object('base_amounts', a.base_amounts, 'from', lower(a.valid * daterange($2::date, $3::date, '[]'))::text,
                                                    'days', upper(a.valid * daterange($2::date, $3::date, '[]')) - lower(a.valid * daterange($2::date, $3::date, '[]'))) ORDER BY lower(a.valid))
                  FROM salary_assignment a WHERE a.employee_id = e.id AND a.valid && daterange($2::date, $3::date, '[]')) AS segments
          FROM employee e
         WHERE e.company_id = $1 AND (e.join_date IS NULL OR e.join_date <= $3::date) AND (e.exit_date IS NULL OR e.exit_date >= $2::date)
           AND ($4::uuid[] IS NULL OR e.id = ANY($4::uuid[]))
         ORDER BY e.emp_code""", [str(company_id), p['start_ad'], p['end_ad'], only])
    for e in r:
        e['id'] = str(e['id'])
    return r


def components_for(company_id: str, structure_version_id: str) -> list[dict]:
    v = q1('SELECT components FROM salary_structure_version WHERE id = $1', [str(structure_version_id)])
    if not v:
        raise BadRequestError('salary structure version not found')
    codes = [c['code'] for c in v['components']]
    r = q('SELECT * FROM salary_component WHERE company_id = $1 AND code = ANY($2::text[]) AND active', [str(company_id), codes])
    return [{'code': c['code'], 'type': c['type'], 'nameEn': c['name_en'], 'nameNe': c['name_ne'], 'formula': c['formula'],
             'condition': c['condition'], 'dependsOnPaymentDays': c['depends_on_payment_days'], 'taxable': c['taxable'],
             'statutory': c['statutory'], 'rounding': c['rounding'], 'sortOrder': c['sort_order']} for c in r]


def _n(r: dict | None, k: str = 'n') -> int:
    return int((r or {}).get(k) or 0)


def preflight(company_id: str, run: dict) -> dict:
    p = get_period(company_id, run['period_id'])
    blockers: list[dict] = []
    warnings: list[dict] = []
    emps = active_employees(company_id, p, run)
    ids = [e['id'] for e in emps]

    unlocked = q1("""SELECT count(*)::text AS n FROM v_attendance_latest v WHERE v.employee_id = ANY($1::uuid[]) AND v.local_date BETWEEN $2 AND $3
                      AND v.locked_at IS NULL""", [ids, p['start_ad'], p['end_ad']])
    if _n(unlocked) > 0 and run['run_type'] == 'regular':
        blockers.append({'code': 'attendance_not_locked', 'message': 'Attendance for the period is not locked yet', 'count': _n(unlocked)})

    missing_rows = q("""
        SELECT e.emp_code, concat_ws(', ', CASE WHEN e.bank_account_enc IS NULL THEN 'bank account' END, CASE WHEN e.pan_enc IS NULL THEN 'PAN' END,
               CASE WHEN e.regime = 'ssf' AND e.ssf_id IS NULL THEN 'SSF id' END, CASE WHEN e.regime = 'pf' AND e.pf_id IS NULL THEN 'PF id' END) AS missing
          FROM employee e WHERE e.id = ANY($1::uuid[])""", [ids])
    missing = [m for m in missing_rows if m['missing']]
    if missing:
        blockers.append({'code': 'missing_master_data', 'message': 'Employees missing bank/PAN/SSF details', 'count': len(missing),
                         'employees': [f"{m['emp_code']}: {m['missing']}" for m in missing]})

    no_assign = [e for e in emps if not e['assignment']]
    if no_assign:
        blockers.append({'code': 'no_salary_assignment', 'message': 'Employees without a salary assignment for the period',
                         'count': len(no_assign), 'employees': [e['emp_code'] for e in no_assign]})

    incomplete = q("""SELECT e.emp_code, count(*)::text AS n FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
                       WHERE v.employee_id = ANY($1::uuid[]) AND v.local_date BETWEEN $2 AND $3 AND v.status = 'Incomplete'
                       GROUP BY e.emp_code ORDER BY e.emp_code""", [ids, p['start_ad'], p['end_ad']])
    if incomplete:
        days = sum(int(r['n']) for r in incomplete)
        warnings.append({'code': 'incomplete_days', 'message': 'Days with a missing punch are paid as worked; check them and regularise if needed',
                         'count': days, 'employees': [f"{r['emp_code']}: {r['n']} {'day' if r['n'] == '1' else 'days'}" for r in incomplete]})
    pending_reg = q1("""SELECT count(*)::text AS n FROM regularisation_request WHERE employee_id = ANY($1::uuid[]) AND local_date BETWEEN $2 AND $3
                        AND status = 'pending'""", [ids, p['start_ad'], p['end_ad']])
    if _n(pending_reg) > 0:
        warnings.append({'code': 'pending_regularisations', 'message': 'Regularisation requests still pending', 'count': _n(pending_reg)})
    pending_leave = q1("""SELECT count(*)::text AS n FROM leave_request WHERE employee_id = ANY($1::uuid[]) AND status = 'pending'
                          AND from_date <= $3 AND to_date >= $2""", [ids, p['start_ad'], p['end_ad']])
    if _n(pending_leave) > 0:
        warnings.append({'code': 'pending_leave', 'message': 'Leave requests pending that affect this period', 'count': _n(pending_leave)})
    pending_tax = q1("SELECT count(*)::text AS n FROM tax_declaration_request WHERE employee_id = ANY($1::uuid[]) AND status = 'pending'", [ids])
    if _n(pending_tax) > 0:
        warnings.append({'code': 'pending_tax_savings',
                         'message': 'Tax savings declarations awaiting HR verification (they do not reduce tax until approved)',
                         'count': _n(pending_tax)})
    ot_detected = q1("""SELECT count(*)::text AS n FROM ot_slip WHERE employee_id = ANY($1::uuid[]) AND local_date BETWEEN $2 AND $3
                        AND status = 'detected'""", [ids, p['start_ad'], p['end_ad']])
    if _n(ot_detected) > 0:
        warnings.append({'code': 'ot_unapproved', 'message': 'Detected overtime not yet approved (only approved hours are paid)',
                         'count': _n(ot_detected)})
    from hajiri.expenses.payroll import payout_preview, preview_message
    assigned = [e['id'] for e in emps if e['assignment']]
    payouts = payout_preview(company_id, run['id'], run['run_type'], assigned)
    if payouts['count'] > 0:
        warnings.append({'code': 'expense_payouts', 'message': preview_message(payouts), 'count': payouts['count'],
                         'employees': payouts['employees']})

    if run['run_type'] == 'regular':
        # Late-arrival penalties of this month that HR has not applied yet (Attendance, Late penalties).
        from hajiri.attendance_tools.services import extras_policy, penalty_preview
        rule = extras_policy(company_id)['latePenalty']
        if rule.get('enabled'):
            due = [ln for ln in penalty_preview(company_id, p['bs_year'], p['bs_month'], rule)
                   if ln['penaltyDays'] > 0 and not ln['applied']]
            if due:
                warnings.append({'code': 'late_penalties_pending', 'message': 'Late-arrival penalties for this month have not been applied yet',
                                 'count': len(due), 'employees': [f"{ln['empCode']}: {ln['lateDays']} late days" for ln in due]})
    warnings.extend(hold_preflight(company_id, run, assigned))

    joiners = [e['emp_code'] for e in emps if e['join_date'] and p['start_ad'] <= e['join_date'] <= p['end_ad']]
    leavers = [e['emp_code'] for e in emps if e['exit_date'] and p['start_ad'] <= e['exit_date'] <= p['end_ad']]
    return {'ok': not blockers, 'blockers': blockers, 'warnings': warnings,
            'info': {'employees': len(emps), 'joiners': joiners, 'leavers': leavers,
                     'period': {'bsYear': p['bs_year'], 'bsMonth': p['bs_month'], 'startAd': p['start_ad'], 'endAd': p['end_ad'], 'days': p['days']}}}


# ---------------------------------------------------------------- compute (plan §11.1, §13.6 step 3)

def blend_segments(segments: list[dict]) -> dict[str, str]:
    """
    A salary revision inside the period: blend every overlapping assignment's base amounts by the days each one covers,
    so 69,500 for 15 days and 60,000 for 16 days pays the day-weighted figure rather than the newest amount.
    """
    total = sum(sg['days'] for sg in segments)
    if not total:
        return segments[-1]['base_amounts'] if segments else {}
    codes: list[str] = []
    for sg in segments:
        for c in sg['base_amounts']:
            if c not in codes:
                codes.append(c)
    out: dict[str, str] = {}
    with localcontext(ENGINE_CONTEXT):
        for code in codes:
            acc = D(0)
            for sg in segments:
                acc = acc + D(sg['base_amounts'].get(code, 0)) * D(sg['days'])
            out[code] = to_fixed(to_dp(acc / D(total), 2), 2)
    return out


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _add_days(a: str, n: int) -> str:
    return (date.fromisoformat(a) + timedelta(days=n)).isoformat()


def months_between(from_ad: str, to_ad: str) -> int:
    """Math.max(0, Math.round((b - a) / (30.44 days)))."""
    return max(0, js_round(_days_between(from_ad, to_ad) / 30.44))


def _js_number(v: Any) -> float:
    """Number(v) for a setting value (NaN for text that is not a number)."""
    try:
        return float(str(v).strip()) if str(v).strip() else 0.0
    except ValueError:
        return math.nan


def _iso_ms(d: datetime) -> str:
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def compute_run(company_id: str, run_id: str, actor_id: str, override: dict | None = None) -> dict:
    run = get_run(company_id, run_id)
    if run['status'] not in ('draft', 'preflight_failed', 'computed', 'reviewed'):
        raise ConflictError(f"run is {run['status']}; cannot compute")
    pf = preflight(company_id, run)
    if not pf['ok']:
        # Blockers stop the run unless HR explicitly overrides with a reason (plan §13.6); the override is audited.
        roles = (override or {}).get('roles') or ()
        can_override = bool(override and len(override['reason'].strip()) >= 5 and ('hr' in roles or 'admin' in roles))
        if not can_override:
            x("UPDATE payroll_run SET status = 'preflight_failed', preflight = $2 WHERE id = $1", [run_id, json.dumps(pf)])
            raise ConflictError('pre-flight blockers must be resolved before computing (HR may override with a reason)', pf)
        pf['info'] = {**pf['info'], 'override': {'reason': override['reason'], 'by': actor_id, 'at': _iso_ms(datetime.now(timezone.utc))}}
        audit('preflight_override', 'payroll_run', actor_id=actor_id, row_id=run_id,
              after={'reason': override['reason'], 'blockers': [b['code'] for b in pf['blockers']]})
    try:
        with transaction.atomic():
            return _compute(company_id, run, actor_id, pf)
    except (PayrollError, FormulaError) as e:
        raise UnprocessableError(str(e))


def _compute(company_id: str, run: dict, actor_id: str, pf: dict) -> dict:
    from hajiri.attendance_tools.services import penalty_unpaid_days
    from hajiri.expenses.payroll import claim_payouts, release_run_payouts, with_payout_components

    run_id = run['id']
    p = get_period(company_id, run['period_id'])
    policy = payroll_policy(company_id)
    rules, rule_ids = resolve_rules(p['end_ad'])
    fy = fiscal_year_of(ad_to_bs(p['start_ad']))
    month_index = ((p['bs_month'] - 4 + 12) % 12) + 1
    emps = active_employees(company_id, p, run)
    prev = previous_nets(company_id, run)

    results: list[dict] = []
    cache: dict[str, list[dict]] = {}
    is_final = run['run_type'] == 'final_settlement'
    # Only regular and final-settlement runs carry the salary itself; every other run type pays one-time lines only.
    carries_salary = run['run_type'] == 'regular' or is_final

    x('DELETE FROM payslip_line WHERE payslip_id IN (SELECT id FROM payslip WHERE run_id = $1)', [run_id])
    x('DELETE FROM payslip WHERE run_id = $1', [run_id])
    release_run_payouts(run_id)
    release_run_claims(run_id)
    with localcontext(ENGINE_CONTEXT):
        for emp in emps:
            if not emp['assignment']:
                continue
            sv = str(emp['assignment']['structure_version_id'])
            components = cache.get(sv)
            if components is None:
                components = components_for(company_id, sv)
                cache[sv] = components
            # Employment window inside the period.
            from_ = emp['join_date'] if emp['join_date'] and emp['join_date'] > p['start_ad'] else p['start_ad']
            to = emp['exit_date'] if emp['exit_date'] and emp['exit_date'] < p['end_ad'] else p['end_ad']
            employed_days = _days_between(from_, to) + 1
            att = q1("""
                SELECT coalesce(sum(CASE
                          WHEN status = 'Absent' AND flags @> '[{"kind":"HalfDayLeave"}]' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 1
                          WHEN status = 'Absent' AND flags @> '[{"kind":"HalfDayLeave"}]' THEN 0.5
                          WHEN status = 'Absent' THEN 1
                          WHEN status = 'HalfDay' THEN 0.5
                          WHEN status = 'OnLeave' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 1
                          WHEN status = 'HalfDayLeave' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 0.5
                          ELSE 0 END), 0)::text AS lop,
                       count(*)::text AS rows,
                       (SELECT coalesce(sum(approved_min), 0) FROM ot_slip s WHERE s.employee_id = $1 AND s.local_date BETWEEN $2 AND $3
                           AND s.status IN ('approved', 'posted'))::text AS ot_min
                  FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3""", [emp['id'], from_, to]) or {}
            # Unpaid days from an applied late-arrival penalty for this BS month count as loss of pay on the salary run.
            penalty = D(str(penalty_unpaid_days(emp['id'], p['bs_year'], p['bs_month']))) if carries_salary else D(0)
            lop = D(att.get('lop') or 0) + penalty
            paid_days = D(employed_days) - lop
            ot_hours = to_dp(D(att.get('ot_min') or 0) / D(60), 2)

            # Year-to-date from every LOCKED payslip of this fiscal year, including other runs of the same period.
            y = q1("""
                SELECT coalesce(sum(ps.taxable), 0)::text AS taxable, coalesce(sum(ps.tds), 0)::text AS tds,
                       coalesce((SELECT sum(l.amount) FROM payslip_line l WHERE l.payslip_id = ANY(array_agg(ps.id)) AND l.component_code IN ('SSF_EE', 'PF_EE', 'CIT')), 0)::text AS ee,
                       coalesce(sum(ps.employer_statutory), 0)::text AS er, coalesce(sum(ps.gross), 0)::text AS gross,
                       coalesce(bool_or(EXISTS (SELECT 1 FROM payslip_line l WHERE l.payslip_id = ps.id AND l.component_code = 'FESTIVAL')), false) AS festival
                  FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period pp ON pp.id = r.period_id
                 WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.id <> $4 AND pp.start_ad >= $2::date AND pp.start_ad <= $3::date""",
                   [emp['id'], fy.start_ad, p['start_ad'], run_id])
            y = dict(y)
            # Income and tax from before Hajiri ran this employee's payroll this fiscal year.
            ob = opening_balance(emp['id'], fy.start_year)
            if ob:
                y['taxable'] = fx(D(y['taxable']) + D(ob['taxable']))
                y['tds'] = fx(D(y['tds']) + D(ob['tds_paid']))
                y['gross'] = fx(D(y['gross']) + D(ob['gross']))
                y['ee'] = fx(D(y['ee']) + D(ob['retirement_employee']))
                y['er'] = fx(D(y['er']) + D(ob['retirement_employer']))
                y['festival'] = bool(y['festival'] or ob['festival_paid'])
            # One-off runs have no salary lines, so the projection borrows the latest locked regular payslip's recurring figures.
            recurring_override = None
            if not carries_salary:
                ref = q1("""
                    SELECT ps.inputs_snapshot->'result'->'tax' AS tax FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period pp ON pp.id = r.period_id
                     WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND pp.start_ad >= $2::date AND pp.start_ad <= $3::date
                     ORDER BY pp.start_ad DESC, r.locked_at DESC LIMIT 1""", [emp['id'], fy.start_ad, p['start_ad']])
                t = (ref or {}).get('tax')
                if t and t.get('recurringTaxable'):
                    recurring_override = {'taxable': t['recurringTaxable'], 'retirement': t.get('recurringRetirement') or '0'}
            dcl = q1("""SELECT life_premium::text, health_premium::text, cit_monthly::text, medical_cost::text, other_retirement_annual::text,
                               donation::text, cit_from_month, cit_prev_monthly::text
                          FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'verified'""", [emp['id'], fy.start_year])
            loans = q("""SELECT l.id, l.emi::text, l.principal::text, l.kind, coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)::text AS paid
                           FROM loan l WHERE l.employee_id = $1 AND l.status = 'active' AND (l.start_bs_year, l.start_bs_month) <= ($2, $3)""",
                      [emp['id'], p['bs_year'], p['bs_month']])
            loan_inputs = [{'id': str(ln['id']), 'emi': ln['emi'], 'remaining': fx(D(ln['principal']) - D(ln['paid'])), 'kind': ln['kind']}
                           for ln in loans]
            loan_inputs = [ln for ln in loan_inputs if D(ln['remaining']) > 0]
            inputs: list[dict] = []
            for i in q('SELECT component_code, amount::text, note FROM payroll_input WHERE run_id = $1 AND employee_id = $2', [run_id, emp['id']]):
                item = {'code': i['component_code'], 'amount': i['amount']}
                if i['note']:
                    item['note'] = i['note']
                inputs.append(item)
            # Approved expense claims and travel settlements: paid (or recovered) once, as non-taxable one-time lines.
            payouts = claim_payouts(company_id, run_id, run['run_type'], emp['id'])
            if payouts:
                inputs.extend(payouts)
                components = with_payout_components(company_id, components, payouts)
            # Pay held in an earlier run and released through this one: paid once, not taxed again.
            releases = claim_releases(company_id, run_id, run['run_type'], emp['id'])
            if releases:
                inputs.extend(releases)
                components = with_release_component(company_id, components)

            # Festival allowance proration (Sec 37(3)): months of service inside the fiscal year.
            service_from = emp['join_date'] if emp['join_date'] and emp['join_date'] > fy.start_ad else fy.start_ad
            service_to = emp['exit_date'] if emp['exit_date'] and emp['exit_date'] < fy.end_ad else fy.end_ad
            extras: dict[str, Any] = {
                'monthsOfServiceInFy': 12 if service_from == fy.start_ad and service_to == fy.end_ad
                else min(12, max(1, months_between(service_from, service_to))),
            }
            if recurring_override:
                extras['recurringOverride'] = recurring_override
            if is_final:
                enc = q1("""SELECT coalesce(sum(ll.delta_days), 0)::text AS days FROM leave_ledger ll JOIN leave_type lt ON lt.id = ll.leave_type_id
                             WHERE ll.employee_id = $1 AND lt.encashable""", [emp['id']])
                extras['isFinalSettlement'] = True
                extras['recoverLoansInFull'] = True
                extras['deductFullTaxNow'] = True
                extras['leaveEncashDays'] = fx((enc or {}).get('days') or 0)
                if policy['encashmentDivisor'] == 'bs_days':
                    extras['leaveEncashDivisor'] = p['days']
                else:
                    n = _js_number(policy['encashmentDivisor'])
                    extras['leaveEncashDivisor'] = (int(n) if float(n).is_integer() else n) if n and not math.isnan(n) else p['days']
                extras['forceFestivalAllowance'] = not y['festival']
                notice = run['params'].get('noticePay')
                if isinstance(notice, str) and not D(notice).is_zero():
                    inputs.append({'code': 'NOTICE_PAY', 'amount': fx(notice)} if D(notice) > 0
                                  else {'code': 'NOTICE_DEDUCTION', 'amount': fx(abs(D(notice)))})
            # Segments are clipped to the employment window so a joiner's single assignment is not diluted.
            segs = []
            for sg in emp['segments'] or []:
                start = sg['from'] if sg['from'] > from_ else from_
                end_excl = _add_days(sg['from'], sg['days'])
                stop = end_excl if end_excl <= to else _add_days(to, 1)
                days = max(0, _days_between(start, stop))
                if days > 0:
                    segs.append({**sg, 'from': start, 'days': days})
            revised = carries_salary and len(segs) > 1
            base = emp['assignment']['base_amounts']
            base_amounts = {} if not carries_salary else blend_segments(segs) if revised else base
            if revised and base.get('BASIC') is not None:
                extras['lastBasic'] = fx(base['BASIC'])
            employee = {'id': emp['id'], 'code': emp['emp_code'], 'nameEn': emp['name_en'], 'nameNe': emp['name_ne'], 'gender': emp['gender'],
                        'maritalStatus': emp['marital_status'], 'isResident': emp['is_resident'], 'regime': emp['regime'],
                        'remoteAreaClass': emp['remote_area_class'], 'disability': emp['disability'], 'otEligible': emp['ot_eligible'],
                        'festivalMonth': emp['festival_month']}
            input_: dict[str, Any] = {
                'employee': employee,
                'period': {'bsYear': p['bs_year'], 'bsMonth': p['bs_month'], 'daysInMonth': p['days'], 'fiscalYearStart': fy.start_year,
                           'monthIndexInFy': month_index},
                'components': components, 'baseAmounts': base_amounts, 'inputs': inputs,
                'attendance': ({'paidDays': to_fixed(paid_days, 2), 'lopDays': to_fixed(lop, 2),
                                'otHours': to_fixed(ot_hours, 2) if run['run_type'] == 'regular' else '0'} if carries_salary
                               else {'paidDays': str(p['days']), 'lopDays': '0', 'otHours': '0'}),
                'ytd': {'taxable': y['taxable'], 'tdsPaid': y['tds'], 'retirementEmployee': y['ee'], 'retirementEmployer': y['er'],
                        'festivalPaid': bool(y['festival'])},
                'declaration': {'lifePremium': (dcl or {}).get('life_premium') or '0', 'healthPremium': (dcl or {}).get('health_premium') or '0',
                                'citMonthly': effective_cit(dcl, month_index), 'medicalCost': (dcl or {}).get('medical_cost') or '0',
                                'otherRetirementAnnual': (dcl or {}).get('other_retirement_annual') or '0',
                                'donation': (dcl or {}).get('donation') or '0'},
                'loans': loan_inputs if run['run_type'] == 'regular' or is_final else [], 'rules': rules, 'policy': _engine_policy(policy),
                'extras': extras,
            }
            if not carries_salary and run['run_type'] not in ('arrears', 'bonus') and not inputs:
                continue  # nothing to pay in a one-off run
            if run['run_type'] == 'arrears':
                ref = referenced_gross(run['references_run_id'], emp['id'])
                if not ref:
                    continue
                # Recompute the referenced period as if the current assignment had applied then.
                as_if_extras = {k: v for k, v in extras.items() if k != 'recurringOverride'}
                as_if = compute_payslip({**input_, 'extras': as_if_extras, 'baseAmounts': base, 'inputs': ref['inputs'], 'loans': [],
                                         'attendance': ref['attendance'], 'ytd': ref['ytd']})
                as_if_gross = D(0)
                for ln in as_if['lines']:
                    if ln['type'] == 'earning' and ln['code'] != 'ROUNDING':
                        as_if_gross = as_if_gross + D(ln['amount'])
                diff = as_if_gross - D(ref['gross'])
                if not diff > 0:
                    continue
                input_['inputs'] = [{'code': 'ARREARS', 'amount': to_fixed(diff, 2), 'note': f"difference vs run {run['references_run_id']}"}]
            if run['run_type'] == 'bonus':
                bonus = (run['params'].get('bonusByEmployee') or {}).get(emp['id'])
                if not bonus or not D(bonus) > 0:
                    continue
                input_['inputs'] = [{'code': 'BONUS', 'amount': fx(bonus), 'note': 'Bonus Act 2030'}]
            result = compute_payslip(input_)
            if revised:
                parts = '; '.join(f"{', '.join(f'{c} {v}' for c, v in sg['base_amounts'].items())} for {sg['days']} days from {sg['from']}" for sg in segs)
                result['explain'].insert(0, {'code': 'SALARY_REVISION',
                                             'text': f'Salary changed during the period; base amounts are day-weighted: {parts}'})
            flags = list(result['flags'])
            if revised:
                flags.append('SalaryRevised')
            prev_net = prev.get(emp['id'])
            if run['run_type'] == 'regular':
                if prev_net is None:
                    flags.append('FirstPayslip')
                elif D(prev_net) > 0 and abs(D(result['net']) - D(prev_net)) / D(prev_net) > D('0.10'):
                    flags.append('NetChangedOver10Pct')
                if paid_days <= 0:
                    flags.append('ZeroAttendance')
                if ot_hours > 20:
                    flags.append('HighOvertime')
            if not carries_salary and not recurring_override:
                flags.append('NoRecurringReference')
            if emp['exit_date'] and emp['exit_date'] <= p['end_ad']:
                flags.append('LastMonth')
            snapshot = {'input': input_, 'result': {'tax': result['tax'], 'ssfAllocation': result['ssfAllocation'],
                                                    'loanInstallments': result['loanInstallments'], 'payableFraction': result['payableFraction']}}
            ytd = {'gross': fx(D(y['gross']) + D(result['gross'])), 'taxable': fx(D(y['taxable']) + D(result['taxable'])),
                   'tds': fx(D(y['tds']) + D(result['tds'])), 'statutory': fx(D(y['ee']) + D(result['employeeStatutory']))}
            ps = q1("""INSERT INTO payslip (run_id, employee_id, inputs_snapshot, rule_version_ids, bs_days_in_month, paid_days, lop_days, ot_hours, gross, taxable, tds,
                                           employee_statutory, employer_statutory, total_deductions, net, ytd, explain, flags)
                       VALUES ($1, $2, $3, $4::uuid[], $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) RETURNING id""",
                    [run_id, emp['id'], json.dumps(snapshot), rule_ids, p['days'], result['paidDays'], result['lopDays'], result['otHours'],
                     result['gross'], result['taxable'], result['tds'], result['employeeStatutory'], result['employerStatutory'],
                     result['totalDeductions'], result['net'], json.dumps(ytd), json.dumps(result['explain']), json.dumps(flags)])
            for ln in result['lines']:
                x("""INSERT INTO payslip_line (payslip_id, component_code, type, name_en, name_ne, amount, basis, sort_order)
                     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                  [ps['id'], ln['code'], ln['type'], ln['nameEn'], ln['nameNe'], ln['amount'], json.dumps(ln['basis']), ln['sortOrder']])
            results.append({'emp': emp, 'result': result, 'payslip_id': str(ps['id'])})

        def total(k: str) -> str:
            t = D(0)
            for r in results:
                t = t + D(r['result'][k])
            return to_fixed(t, 2)
        totals = {'headcount': str(len(results)), 'gross': total('gross'), 'net': total('net'), 'tds': total('tds'),
                  'employeeStatutory': total('employeeStatutory'), 'employerStatutory': total('employerStatutory')}
        prev_total = D(0)
        for n in prev.values():
            prev_total = prev_total + D(n)
        variance = {
            'flagged': len([r for r in results if r['result']['flags'] or prev.get(r['emp']['id']) is None]),
            'previousNet': to_fixed(prev_total, 2),
            'byEmployee': [{'employeeId': r['emp']['id'], 'empCode': r['emp']['emp_code'], 'net': r['result']['net'],
                            'previousNet': prev.get(r['emp']['id']), 'flags': r['result']['flags']} for r in results],
        }
    x("""UPDATE payroll_run SET status = 'computed', computed_at = now(), preflight = $2, totals = $3, variance = $4, maker_id = coalesce(maker_id, $5)
          WHERE id = $1""", [run_id, json.dumps(pf), json.dumps(totals), json.dumps(variance), actor_id])
    sync_run_holds(run_id)
    audit('compute', 'payroll_run', actor_id=actor_id, row_id=run_id, after=totals)
    return {'payslips': len(results), 'totals': totals, 'variance': variance}


def previous_nets(company_id: str, run: dict) -> dict[str, str]:
    r = q("""
        SELECT ps.employee_id, ps.net::text FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
         WHERE r.company_id = $1 AND r.run_type = 'regular' AND r.locked_at IS NOT NULL AND r.id <> $2
           AND (p.bs_year, p.bs_month) = (SELECT p2.bs_year, p2.bs_month FROM payroll_run r2 JOIN payroll_period p2 ON p2.id = r2.period_id
                                           WHERE r2.company_id = $1 AND r2.run_type = 'regular' AND r2.locked_at IS NOT NULL AND r2.id <> $2
                                             AND (p2.bs_year, p2.bs_month) < (SELECT bs_year, bs_month FROM payroll_period WHERE id = $3)
                                           ORDER BY p2.bs_year DESC, p2.bs_month DESC LIMIT 1)""",
          [str(company_id), run['id'], run['period_id']])
    return {str(x_['employee_id']): x_['net'] for x_ in r}


def referenced_gross(ref_run_id: str, employee_id: str) -> dict | None:
    row = q1("""SELECT coalesce((SELECT sum(l.amount) FROM payslip_line l WHERE l.payslip_id = ps.id AND l.type = 'earning' AND l.component_code <> 'ROUNDING'), 0)::text AS gross,
                       ps.inputs_snapshot
                  FROM payslip ps WHERE ps.run_id = $1 AND ps.employee_id = $2""", [str(ref_run_id), str(employee_id)])
    if not row:
        return None
    i = row['inputs_snapshot']['input']
    return {'gross': row['gross'], 'attendance': i['attendance'], 'inputs': i['inputs'], 'ytd': i['ytd']}


# ---------------------------------------------------------------- lifecycle (plan §9.5, §11.8)

@transaction.atomic
def review_run(company_id: str, run_id: str, actor_id: str, notes: str | None = None) -> None:
    run = get_run(company_id, run_id)
    if run['status'] != 'computed':
        raise ConflictError(f"run is {run['status']}; compute it first")
    x("UPDATE payroll_run SET status = 'reviewed', reviewed_at = now(), maker_id = $2, notes = coalesce($3, notes) WHERE id = $1",
      [run_id, actor_id, notes])
    audit('review', 'payroll_run', actor_id=actor_id, row_id=run_id)


@transaction.atomic
def approve_run(company_id: str, run_id: str, approver_id: str, roles) -> dict:
    """
    HR sign-off: payroll is owned by HR, with no finance step. The approver may be the person who prepared the run
    unless payroll.policy.separateApprover is on, in which case a different HR user must approve.
    """
    run = get_run(company_id, run_id)
    if run['status'] != 'reviewed':
        raise ConflictError(f"run is {run['status']}; review it first")
    if 'hr' not in roles and 'admin' not in roles:
        raise ForbiddenError('approval requires the HR role')
    policy = payroll_policy(company_id)
    if policy['separateApprover'] and run['maker_id'] == str(approver_id):
        raise ForbiddenError('company policy requires a different HR user to approve this run')
    payload = json.dumps({'runId': run_id, 'totals': run['totals'], 'approverId': approver_id, 'at': _iso_ms(datetime.now(timezone.utc))},
                         separators=(',', ':'), ensure_ascii=False)
    signature = 'sha256:' + hashlib.sha256(payload.encode()).hexdigest()
    x("UPDATE payroll_run SET status = 'approved', approved_at = now(), checker_id = $2, approval_signature = $3 WHERE id = $1",
      [run_id, approver_id, signature])
    audit('approve', 'payroll_run', actor_id=approver_id, row_id=run_id, after={'signature': signature, 'payload': payload})
    return {'signature': signature, 'approved': True}


def lock_run(company_id: str, run_id: str, actor_id: str) -> dict:
    from hajiri.attendance.service import lock_attendance
    from hajiri.expenses.payroll import settle_run_payouts

    run = get_run(company_id, run_id)
    if run['status'] != 'approved':
        raise ConflictError(f"run is {run['status']}; approve it first")
    p = get_period(company_id, run['period_id'])
    with transaction.atomic():
        # A run scoped to specific employees locks only their attendance; a regular run locks the period.
        only = run['params'].get('employeeIds')
        if isinstance(only, list):
            n = x("""UPDATE attendance_day a SET locked_at = now() FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
                      WHERE a.id = v.id AND v.employee_id = ANY($1::uuid[]) AND e.company_id = $4 AND v.local_date BETWEEN $2 AND $3 AND a.locked_at IS NULL""",
                  [[str(i) for i in only], p['start_ad'], p['end_ad'], str(company_id)])
        else:
            n = lock_attendance(company_id, p['start_ad'], p['end_ad'])
            x('UPDATE payroll_period SET attendance_locked_at = coalesce(attendance_locked_at, now()) WHERE id = $1', [p['id']])
        x("UPDATE payroll_run SET status = 'locked', locked_at = now() WHERE id = $1", [run_id])
        if run['run_type'] == 'regular':
            x("""UPDATE ot_slip s SET status = 'posted', posted_payslip_id = ps.id FROM payslip ps
                  WHERE ps.run_id = $1 AND ps.employee_id = s.employee_id AND s.local_date BETWEEN $2 AND $3 AND s.status = 'approved'""",
              [run_id, p['start_ad'], p['end_ad']])
        for sl in q('SELECT id, employee_id, inputs_snapshot FROM payslip WHERE run_id = $1', [run_id]):
            for li in ((sl['inputs_snapshot'] or {}).get('result') or {}).get('loanInstallments') or []:
                x('INSERT INTO loan_installment (loan_id, payslip_id, amount) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
                  [li['loanId'], sl['id'], li['amount']])
                x("""UPDATE loan SET status = 'closed' WHERE id = $1
                      AND principal <= (SELECT coalesce(sum(amount), 0) FROM loan_installment WHERE loan_id = $1)""", [li['loanId']])
        settle_run_payouts(run_id)
        settle_run_releases(run_id, actor_id)
        audit('lock', 'payroll_run', actor_id=actor_id, row_id=run_id, after={'attendanceLocked': n})
    # Company setting: payslips go to employees as soon as the run is locked (otherwise HR publishes them).
    published = publish_run(company_id, run_id, actor_id) if payroll_policy(company_id)['publishOnLock'] else 0
    return {'attendanceLocked': n, 'published': published}


@transaction.atomic
def publish_run(company_id: str, run_id: str, actor_id: str) -> int:
    run = get_run(company_id, run_id)
    if not run['locked_at']:
        raise ConflictError('only locked runs can be published')
    p = get_period(company_id, run['period_id'])
    published = q('UPDATE payslip SET published_at = now() WHERE run_id = $1 AND published_at IS NULL RETURNING id, employee_id', [run_id])
    for r in published:
        emp = str(r['employee_id'])
        notify(company_id=str(company_id), user_id=user_id_for_employee(emp), employee_id=emp, template='payslip.published',
               title='Your payslip is ready', body=f"Your payslip for {bs_month_name(p['bs_year'], p['bs_month'])} is ready to view.",
               payload={'payslipId': str(r['id'])}, channels=('push',))
    audit('publish', 'payroll_run', actor_id=actor_id, row_id=run_id, after={'payslips': len(published)})
    return len(published)


# ---------------------------------------------------------------- bonus pool (plan §11.6)

def bonus_allocation(company_id: str, fiscal_year_start: int, net_profit: str) -> dict:
    fy = fiscal_year(fiscal_year_start)
    rules, _ = resolve_rules(fy.end_ad)
    caps = rules.get('bonus.caps') or {'poolRate': '0.10', 'monthsCapLow': 8, 'monthsCapHigh': 6, 'lowPayThreshold': '39100',
                                       'minWorkedFraction': '0.5'}
    r = q("""
        SELECT e.id AS employee_id, e.emp_code,
               coalesce((SELECT sum(ps.gross) FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                          WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND p.start_ad BETWEEN $2 AND $3), 0)::text AS wages,
               coalesce((SELECT count(*) FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                          WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND p.start_ad BETWEEN $2 AND $3), 0)::text AS months,
               (SELECT count(*) FROM v_attendance_latest v WHERE v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3
                   AND v.status IN ('Present', 'HalfDay', 'HalfDayLeave', 'WFH', 'OnLeave'))::text AS worked,
               (SELECT count(*) FROM v_attendance_latest v WHERE v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3
                   AND v.status NOT IN ('WeeklyOff', 'Holiday', 'OffShift'))::text AS scheduled
          FROM employee e WHERE e.company_id = $1""", [str(company_id), fy.start_ad, fy.end_ad])
    with localcontext(ENGINE_CONTEXT):
        pool = D(net_profit) * D(caps['poolRate'])
        eligible = [e for e in r if D(e['wages']) > 0
                    and (D(e['scheduled']).is_zero() or D(e['worked']) / D(e['scheduled']) >= D(caps['minWorkedFraction']))]
        total_wages = D(0)
        for e in eligible:
            total_wages = total_wages + D(e['wages'])
        by_employee: dict[str, str] = {}
        detail: list[dict] = []
        for e in eligible:
            monthly = D(e['wages']) / D(max(1, int(e['months'])))
            cap_months = caps['monthsCapLow'] if monthly <= D(caps['lowPayThreshold']) else caps['monthsCapHigh']
            share = D(0) if total_wages.is_zero() else pool * D(e['wages']) / total_wages
            amount = to_dp(dmin(share, monthly * D(cap_months)), 2)
            by_employee[str(e['employee_id'])] = to_fixed(amount, 2)
            detail.append({'employeeId': str(e['employee_id']), 'empCode': e['emp_code'], 'wages': e['wages'], 'share': to_fixed(share, 2),
                           'capMonths': cap_months, 'amount': to_fixed(amount, 2)})
        return {'pool': to_fixed(pool, 2), 'byEmployee': by_employee, 'detail': detail}


__all__ = ['get_period', 'ensure_period', 'get_run', 'payroll_policy', 'preflight', 'active_employees', 'components_for',
           'blend_segments', 'compute_run', 'review_run', 'approve_run', 'lock_run', 'publish_run', 'bonus_allocation',
           'months_between', 'BsRangeError']
