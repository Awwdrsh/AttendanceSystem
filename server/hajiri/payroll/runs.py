"""
Pay runs (port of payroll/service.ts): periods from the BS calendar, pre-flight checks, computing every payslip with
engines.payroll, HR review, approval and lock, publishing payslips, and the Bonus Act allocation. Every function takes
the ACTIVE company id and reads only that company's rows.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import (BooleanField, Case, Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Prefetch, Q,
                              Subquery, Sum, UUIDField, Value, When)
from django.db.models.functions import Coalesce, Now

from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds, fiscal_year, fiscal_year_of
from engines.payroll import PayrollError, compute_payslip
from engines.payroll.formula import FormulaError
from engines.payroll.numeric import ENGINE_CONTEXT, D, dmin, to_dp, to_fixed

from hajiri.core.audit import audit
from hajiri.core.bsdate import bs_month_name
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from hajiri.core.scoping import as_uuid
from hajiri.core.settings_store import get_setting
from hajiri.db.models import (AttendanceDay, Employee, LeaveLedger, LeaveRequest, Loan, LoanInstallment, OtSlip, PayrollInput,
                              PayrollPeriod, PayrollRun, Payslip, PayslipLine, RegularisationRequest, SalaryAssignment,
                              SalaryComponent, SalaryStructureVersion, TaxDeclaration, TaxDeclarationRequest)
from hajiri.notifications.service import notify, user_id_for_employee

from .compliance_service import (claim_releases, effective_cit, hold_preflight, opening_balance, release_run_claims,
                                 settle_run_releases, sync_run_holds, with_release_component)
from .numbers import fx, iso, iso_ms, range_text, round_half_up
from .queries import MONEY, count_of, in_fiscal_window, latest_days, locked_payslips, sum_of, tuple_at_most, tuple_before
from .rules import resolve_rules

Run = PayrollRun
RunType, RunStatus = PayrollRun.RunType, PayrollRun.Status
Day = AttendanceDay.Status
COMPUTABLE = (RunStatus.DRAFT, RunStatus.PREFLIGHT_FAILED, RunStatus.COMPUTED, RunStatus.REVIEWED)
RETIREMENT_EE = ('SSF_EE', 'PF_EE', 'CIT')
DECL_AMOUNTS = ('life_premium', 'health_premium', 'cit_monthly', 'medical_cost', 'other_retirement_annual', 'donation', 'cit_prev_monthly')


def _period(r: dict) -> dict:
    out = dict(r)
    out['id'] = str(out['id'])
    out['company_id'] = str(out['company_id'])
    out['start_ad'] = iso(out['start_ad'])
    out['end_ad'] = iso(out['end_ad'])
    return out


def _run(r: dict) -> dict:
    out = dict(r)
    for k in ('id', 'company_id', 'period_id', 'references_run_id', 'maker_id', 'checker_id'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['params'] = out.get('params') or {}
    return out


def periods(company_id: str):
    return PayrollPeriod.objects.for_company(company_id)


def runs(company_id: str):
    return PayrollRun.objects.for_company(company_id)


def get_period(company_id: str, period_id: str) -> dict:
    r = periods(company_id).filter(pk=as_uuid(period_id)).values().first()
    if not r:
        raise NotFoundError('payroll period not found')
    return _period(r)


def ensure_period(company_id: str, bs_year: int, bs_month: int) -> dict:
    """Raises BsRangeError outside the calendar table (the route turns it into a 400)."""
    b = bs_month_bounds(bs_year, bs_month)
    p, _ = PayrollPeriod.objects.update_or_create(
        company_id=company_id, bs_year=bs_year, bs_month=bs_month, defaults={'days': b['days']},
        create_defaults={'days': b['days'], 'start_ad': b['startAd'], 'end_ad': b['endAd']})
    return _period(periods(company_id).filter(pk=p.pk).values().get())


def get_run(company_id: str, run_id: str) -> dict:
    r = runs(company_id).filter(pk=as_uuid(run_id)).values().first()
    if not r:
        raise NotFoundError('payroll run not found')
    return _run(r)


def payroll_policy(company_id: str) -> dict:
    v = get_setting(company_id, 'payroll.policy', {}) or {}
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


# ---------------------------------------------------------------- pre-flight (plan §13.6 step 1)

EMPLOYEE_FIELDS = ('id', 'emp_code', 'name_en', 'name_ne', 'gender', 'marital_status', 'is_resident', 'regime', 'remote_area_class',
                   'disability', 'ot_eligible', 'festival_month', 'join_date', 'exit_date')


def _window_segments(assignments, start: date, end: date) -> tuple[dict | None, list[dict] | None]:
    """The assignment that applies to the window (open-ended first, then the latest start) and every overlapping one
    clipped to the window, oldest first."""
    if not assignments:
        return None, None
    stop = end + timedelta(days=1)
    pick = max(assignments, key=lambda a: (a.valid.upper is None, a.valid.lower or date.min))
    segs = []
    for a in sorted(assignments, key=lambda a: a.valid.lower or date.min):
        lo = max(a.valid.lower or start, start)
        hi = min(a.valid.upper or stop, stop)
        segs.append({'base_amounts': a.base_amounts, 'from': lo.isoformat(), 'days': (hi - lo).days})
    return {'structure_version_id': str(pick.structure_version_id), 'base_amounts': pick.base_amounts,
            'valid': range_text(pick.valid)}, segs


def active_employees(company_id: str, p: dict, run: dict) -> list[dict]:
    """Employees employed during the period (or the run's named ones), with their salary assignment for the period
    and every assignment overlapping it (a revision inside the month)."""
    only = run.get('params', {}).get('employeeIds')
    start, end = date.fromisoformat(p['start_ad']), date.fromisoformat(p['end_ad'])
    window = SalaryAssignment.objects.filter(valid__overlap=DateRange(start, end, '[]'))
    qs = (Employee.objects.for_company(company_id)
          .filter(Q(join_date__isnull=True) | Q(join_date__lte=end), Q(exit_date__isnull=True) | Q(exit_date__gte=start))
          .prefetch_related(Prefetch('salary_assignments', queryset=window, to_attr='in_window'))
          .order_by('emp_code').only(*EMPLOYEE_FIELDS))
    if isinstance(only, list):
        qs = qs.filter(pk__in=[as_uuid(i) for i in only])
    out = []
    for e in qs:
        row = {k: getattr(e, k) for k in EMPLOYEE_FIELDS}
        row['id'] = str(e.id)
        row['join_date'], row['exit_date'] = iso(e.join_date), iso(e.exit_date)
        row['assignment'], row['segments'] = _window_segments(e.in_window, start, end)
        out.append(row)
    return out


def components_for(company_id: str, structure_version_id: str) -> list[dict]:
    v = (SalaryStructureVersion.objects.for_company(company_id).filter(pk=as_uuid(structure_version_id))
         .values_list('components', flat=True).first())
    if v is None:
        raise BadRequestError('salary structure version not found')
    codes = [c['code'] for c in v]
    r = SalaryComponent.objects.for_company(company_id).filter(code__in=codes, active=True).values()
    return [{'code': c['code'], 'type': c['type'], 'nameEn': c['name_en'], 'nameNe': c['name_ne'], 'formula': c['formula'],
             'condition': c['condition'], 'dependsOnPaymentDays': c['depends_on_payment_days'], 'taxable': c['taxable'],
             'statutory': c['statutory'], 'rounding': c['rounding'], 'sortOrder': c['sort_order']} for c in r]


def preflight(company_id: str, run: dict) -> dict:
    p = get_period(company_id, run['period_id'])
    blockers: list[dict] = []
    warnings: list[dict] = []
    emps = active_employees(company_id, p, run)
    ids = [e['id'] for e in emps]
    in_period = {'local_date__range': (p['start_ad'], p['end_ad'])}
    days = latest_days().filter(employee_id__in=ids, **in_period)

    unlocked = days.filter(locked_at__isnull=True).count()
    if unlocked > 0 and run['run_type'] == RunType.REGULAR:
        blockers.append({'code': 'attendance_not_locked', 'message': 'Attendance for the period is not locked yet', 'count': unlocked})

    missing_rows = (Employee.objects.filter(pk__in=ids).order_by('emp_code')
                    .values_list('emp_code', ExpressionWrapper(Q(bank_account_enc__isnull=True), output_field=BooleanField()),
                                 ExpressionWrapper(Q(pan_enc__isnull=True), output_field=BooleanField()),
                                 ExpressionWrapper(Q(regime=Employee.Regime.SSF, ssf_id__isnull=True), output_field=BooleanField()),
                                 ExpressionWrapper(Q(regime=Employee.Regime.PF, pf_id__isnull=True), output_field=BooleanField())))
    labels = ('bank account', 'PAN', 'SSF id', 'PF id')
    missing = [(code, ', '.join(label for label, gone in zip(labels, flags) if gone)) for code, *flags in missing_rows]
    missing = [m for m in missing if m[1]]
    if missing:
        blockers.append({'code': 'missing_master_data', 'message': 'Employees missing bank/PAN/SSF details', 'count': len(missing),
                         'employees': [f'{code}: {what}' for code, what in missing]})

    no_assign = [e for e in emps if not e['assignment']]
    if no_assign:
        blockers.append({'code': 'no_salary_assignment', 'message': 'Employees without a salary assignment for the period',
                         'count': len(no_assign), 'employees': [e['emp_code'] for e in no_assign]})

    incomplete = list(days.filter(status=Day.INCOMPLETE).values('employee__emp_code').annotate(n=Count('id'))
                      .order_by('employee__emp_code').values_list('employee__emp_code', 'n'))
    if incomplete:
        warnings.append({'code': 'incomplete_days', 'message': 'Days with a missing punch are paid as worked; check them and regularise if needed',
                         'count': sum(n for _, n in incomplete),
                         'employees': [f"{code}: {n} {'day' if n == 1 else 'days'}" for code, n in incomplete]})
    pending_reg = RegularisationRequest.objects.filter(employee_id__in=ids, status=RegularisationRequest.Status.PENDING,
                                                       **in_period).count()
    if pending_reg > 0:
        warnings.append({'code': 'pending_regularisations', 'message': 'Regularisation requests still pending', 'count': pending_reg})
    pending_leave = LeaveRequest.objects.filter(employee_id__in=ids, status=LeaveRequest.Status.PENDING, from_date__lte=p['end_ad'],
                                                to_date__gte=p['start_ad']).count()
    if pending_leave > 0:
        warnings.append({'code': 'pending_leave', 'message': 'Leave requests pending that affect this period', 'count': pending_leave})
    pending_tax = TaxDeclarationRequest.objects.filter(employee_id__in=ids, status=TaxDeclarationRequest.Status.PENDING).count()
    if pending_tax > 0:
        warnings.append({'code': 'pending_tax_savings',
                         'message': 'Tax savings declarations awaiting HR verification (they do not reduce tax until approved)',
                         'count': pending_tax})
    ot_detected = OtSlip.objects.filter(employee_id__in=ids, status=OtSlip.Status.DETECTED, **in_period).count()
    if ot_detected > 0:
        warnings.append({'code': 'ot_unapproved', 'message': 'Detected overtime not yet approved (only approved hours are paid)',
                         'count': ot_detected})
    from hajiri.expenses.payroll import payout_preview, preview_message
    assigned = [e['id'] for e in emps if e['assignment']]
    payouts = payout_preview(company_id, run['id'], run['run_type'], assigned)
    if payouts['count'] > 0:
        warnings.append({'code': 'expense_payouts', 'message': preview_message(payouts), 'count': payouts['count'],
                         'employees': payouts['employees']})

    if run['run_type'] == RunType.REGULAR:
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
    """Whole months between two dates (30.44 days each, halves up), at least 0."""
    return max(0, round_half_up(_days_between(from_ad, to_ad) / 30.44))


def _setting_number(v: Any) -> float:
    """A number typed into a setting (NaN for text that is not a number, 0 for blank)."""
    try:
        return float(str(v).strip()) if str(v).strip() else 0.0
    except ValueError:
        return math.nan


def compute_run(company_id: str, run_id: str, actor_id: str, override: dict | None = None) -> dict:
    run = get_run(company_id, run_id)
    if run['status'] not in COMPUTABLE:
        raise ConflictError(f"run is {run['status']}; cannot compute")
    pf = preflight(company_id, run)
    if not pf['ok']:
        # Blockers stop the run unless HR explicitly overrides with a reason (plan §13.6); the override is audited.
        roles = (override or {}).get('roles') or ()
        can_override = bool(override and len(override['reason'].strip()) >= 5 and ('hr' in roles or 'admin' in roles))
        if not can_override:
            runs(company_id).filter(pk=run['id']).update(status=RunStatus.PREFLIGHT_FAILED, preflight=pf)
            raise ConflictError('pre-flight blockers must be resolved before computing (HR may override with a reason)', pf)
        pf['info'] = {**pf['info'], 'override': {'reason': override['reason'], 'by': actor_id, 'at': iso_ms(datetime.now(timezone.utc))}}
        audit('preflight_override', 'payroll_run', actor_id=actor_id, row_id=run['id'],
              after={'reason': override['reason'], 'blockers': [b['code'] for b in pf['blockers']]})
    try:
        with transaction.atomic():
            return _compute(company_id, run, actor_id, pf)
    except (PayrollError, FormulaError) as e:
        raise UnprocessableError(str(e))


_HALF_LEAVE = [{'kind': 'HalfDayLeave'}]
_UNPAID = [{'kind': 'UnpaidLeave'}]
_ONE, _HALF = Value(Decimal('1')), Value(Decimal('0.5'))
# Days of loss of pay in the attendance of the period: absences, half days and unpaid leave.
LOSS_OF_PAY = Coalesce(Sum(Case(
    When(status=Day.ABSENT, flags__contains=_HALF_LEAVE, then=Case(When(flags__contains=_UNPAID, then=_ONE), default=_HALF)),
    When(status=Day.ABSENT, then=_ONE),
    When(status=Day.HALF_DAY, then=_HALF),
    When(status=Day.ON_LEAVE, flags__contains=_UNPAID, then=_ONE),
    When(status=Day.HALF_DAY_LEAVE, flags__contains=_UNPAID, then=_HALF),
    default=Value(Decimal('0')), output_field=DecimalField())), Value(Decimal('0')), output_field=DecimalField())


def _zero_money():
    return Value(Decimal('0'), output_field=MONEY)


def year_to_date(employee_id: str, fy_start_ad: str, last_start_ad: str, *, exclude_run: str | None = None,
                 inclusive: bool = True) -> dict:
    """Totals of the employee's locked payslips this fiscal year up to a period start: taxable, TDS, employee
    retirement contributions, employer statutory, gross, and whether a festival allowance was paid (strings)."""
    slips = in_fiscal_window(locked_payslips().filter(employee_id=employee_id), fy_start_ad, last_start_ad, inclusive=inclusive)
    if exclude_run:
        slips = slips.exclude(run_id=exclude_run)
    totals = slips.aggregate(taxable=Coalesce(Sum('taxable'), _zero_money()), tds=Coalesce(Sum('tds'), _zero_money()),
                             er=Coalesce(Sum('employer_statutory'), _zero_money()), gross=Coalesce(Sum('gross'), _zero_money()))
    lines = PayslipLine.objects.filter(payslip__in=slips).aggregate(
        ee=Coalesce(Sum('amount', filter=Q(component_code__in=RETIREMENT_EE)), _zero_money()),
        festival=Count('id', filter=Q(component_code='FESTIVAL')))
    return {**{k: str(v) for k, v in totals.items()}, 'ee': str(lines['ee']), 'festival': lines['festival'] > 0}


def verified_declaration(employee_id: str, fiscal_year_start: int) -> dict | None:
    """The verified tax declaration of the year, amounts as strings."""
    d = (TaxDeclaration.objects.filter(employee_id=employee_id, fiscal_year_start=fiscal_year_start, status=TaxDeclaration.Status.VERIFIED)
         .values(*DECL_AMOUNTS, 'cit_from_month').first())
    if d:
        for k in DECL_AMOUNTS:
            d[k] = str(d[k]) if d[k] is not None else None
    return d


def _attendance(employee_id: str, from_: str, to: str) -> dict:
    lop = latest_days().filter(employee_id=employee_id, local_date__range=(from_, to)).aggregate(lop=LOSS_OF_PAY)['lop']
    ot = (OtSlip.objects.filter(employee_id=employee_id, local_date__range=(from_, to),
                                status__in=(OtSlip.Status.APPROVED, OtSlip.Status.POSTED))
          .aggregate(m=Coalesce(Sum('approved_min'), 0))['m'])
    return {'lop': lop, 'ot_min': ot}


def _loans(employee_id: str, bs_year: int, bs_month: int) -> list[dict]:
    paid = sum_of(LoanInstallment.objects.filter(loan_id=OuterRef('pk')), 'amount', group='loan_id')
    loans = (Loan.objects.filter(tuple_at_most('start_', bs_year, bs_month), employee_id=employee_id, status=Loan.Status.ACTIVE)
             .annotate(paid=paid).order_by('created_at', 'id').values('id', 'emi', 'principal', 'kind', 'paid'))
    out = [{'id': str(ln['id']), 'emi': str(ln['emi']), 'remaining': fx(D(ln['principal']) - D(ln['paid'])), 'kind': ln['kind']}
           for ln in loans]
    return [ln for ln in out if D(ln['remaining']) > 0]


def _inputs(run_id: str, employee_id: str) -> list[dict]:
    out = []
    for i in (PayrollInput.objects.filter(run_id=run_id, employee_id=employee_id).order_by('created_at', 'id')
              .values('component_code', 'amount', 'note')):
        item = {'code': i['component_code'], 'amount': str(i['amount'])}
        if i['note']:
            item['note'] = i['note']
        out.append(item)
    return out


def _recurring_reference(employee_id: str, fy_start_ad: str, last_start_ad: str) -> dict | None:
    """The tax figures of the latest locked regular payslip this year (one-off runs project from them)."""
    t = (in_fiscal_window(locked_payslips().filter(employee_id=employee_id, run__run_type=RunType.REGULAR), fy_start_ad, last_start_ad)
         .order_by('-run__period__start_ad', '-run__locked_at').values_list('inputs_snapshot__result__tax', flat=True).first())
    if t and t.get('recurringTaxable'):
        return {'taxable': t['recurringTaxable'], 'retirement': t.get('recurringRetirement') or '0'}
    return None


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
    is_final = run['run_type'] == RunType.FINAL_SETTLEMENT
    # Only regular and final-settlement runs carry the salary itself; every other run type pays one-time lines only.
    carries_salary = run['run_type'] == RunType.REGULAR or is_final

    PayslipLine.objects.filter(payslip__run_id=run_id).delete()
    Payslip.objects.filter(run_id=run_id).delete()
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
            att = _attendance(emp['id'], from_, to)
            # Unpaid days from an applied late-arrival penalty for this BS month count as loss of pay on the salary run.
            penalty = D(str(penalty_unpaid_days(emp['id'], p['bs_year'], p['bs_month']))) if carries_salary else D(0)
            lop = D(att['lop'] or 0) + penalty
            paid_days = D(employed_days) - lop
            ot_hours = to_dp(D(att['ot_min'] or 0) / D(60), 2)

            # Year-to-date from every LOCKED payslip of this fiscal year, including other runs of the same period.
            y = year_to_date(emp['id'], fy.start_ad, p['start_ad'], exclude_run=run_id)
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
            recurring_override = None if carries_salary else _recurring_reference(emp['id'], fy.start_ad, p['start_ad'])
            dcl = verified_declaration(emp['id'], fy.start_year)
            loan_inputs = _loans(emp['id'], p['bs_year'], p['bs_month'])
            inputs = _inputs(run_id, emp['id'])
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
                enc = (LeaveLedger.objects.filter(employee_id=emp['id'], leave_type__encashable=True)
                       .aggregate(days=Coalesce(Sum('delta_days'), Value(Decimal('0')), output_field=DecimalField()))['days'])
                extras['isFinalSettlement'] = True
                extras['recoverLoansInFull'] = True
                extras['deductFullTaxNow'] = True
                extras['leaveEncashDays'] = fx(enc)
                if policy['encashmentDivisor'] == 'bs_days':
                    extras['leaveEncashDivisor'] = p['days']
                else:
                    n = _setting_number(policy['encashmentDivisor'])
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
                                'otHours': to_fixed(ot_hours, 2) if run['run_type'] == RunType.REGULAR else '0'} if carries_salary
                               else {'paidDays': str(p['days']), 'lopDays': '0', 'otHours': '0'}),
                'ytd': {'taxable': y['taxable'], 'tdsPaid': y['tds'], 'retirementEmployee': y['ee'], 'retirementEmployer': y['er'],
                        'festivalPaid': bool(y['festival'])},
                'declaration': {'lifePremium': (dcl or {}).get('life_premium') or '0', 'healthPremium': (dcl or {}).get('health_premium') or '0',
                                'citMonthly': effective_cit(dcl, month_index), 'medicalCost': (dcl or {}).get('medical_cost') or '0',
                                'otherRetirementAnnual': (dcl or {}).get('other_retirement_annual') or '0',
                                'donation': (dcl or {}).get('donation') or '0'},
                'loans': loan_inputs if run['run_type'] == RunType.REGULAR or is_final else [], 'rules': rules, 'policy': dict(policy),
                'extras': extras,
            }
            if not carries_salary and run['run_type'] not in (RunType.ARREARS, RunType.BONUS) and not inputs:
                continue  # nothing to pay in a one-off run
            if run['run_type'] == RunType.ARREARS:
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
            if run['run_type'] == RunType.BONUS:
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
            if run['run_type'] == RunType.REGULAR:
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
            ps = Payslip.objects.create(
                run_id=run_id, employee_id=emp['id'], inputs_snapshot=snapshot, rule_version_ids=rule_ids,
                bs_days_in_month=p['days'], paid_days=result['paidDays'], lop_days=result['lopDays'], ot_hours=result['otHours'],
                gross=result['gross'], taxable=result['taxable'], tds=result['tds'], employee_statutory=result['employeeStatutory'],
                employer_statutory=result['employerStatutory'], total_deductions=result['totalDeductions'], net=result['net'],
                ytd=ytd, explain=result['explain'], flags=flags)
            PayslipLine.objects.bulk_create([
                PayslipLine(payslip=ps, component_code=ln['code'], type=ln['type'], name_en=ln['nameEn'], name_ne=ln['nameNe'],
                            amount=ln['amount'], basis=ln['basis'], sort_order=ln['sortOrder']) for ln in result['lines']])
            results.append({'emp': emp, 'result': result, 'payslip_id': str(ps.pk)})

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
    runs(company_id).filter(pk=run_id).update(
        status=RunStatus.COMPUTED, computed_at=Now(), preflight=pf, totals=totals, variance=variance,
        maker_id=Coalesce(F('maker_id'), Value(actor_id, output_field=UUIDField()), output_field=UUIDField()))
    sync_run_holds(run_id)
    audit('compute', 'payroll_run', actor_id=actor_id, row_id=run_id, after=totals)
    return {'payslips': len(results), 'totals': totals, 'variance': variance}


def previous_regular_run(company_id: str, run: dict):
    """The locked regular runs of the latest month before this run's period (other than this run)."""
    cur = periods(company_id).filter(pk=run['period_id']).values('bs_year', 'bs_month').first()
    locked = runs(company_id).filter(run_type=RunType.REGULAR, locked_at__isnull=False).exclude(pk=run['id'])
    if not cur:
        return locked.none()
    prev = (locked.filter(tuple_before('period__', cur['bs_year'], cur['bs_month']))
            .order_by('-period__bs_year', '-period__bs_month').values('period__bs_year', 'period__bs_month').first())
    if not prev:
        return locked.none()
    return locked.filter(period__bs_year=prev['period__bs_year'], period__bs_month=prev['period__bs_month'])


def previous_nets(company_id: str, run: dict) -> dict[str, str]:
    slips = Payslip.objects.filter(run__in=previous_regular_run(company_id, run)).values_list('employee_id', 'net')
    return {str(e): str(n) for e, n in slips}


def referenced_gross(ref_run_id: str, employee_id: str) -> dict | None:
    earnings = PayslipLine.objects.filter(payslip_id=OuterRef('pk'), type=PayslipLine.Type.EARNING).exclude(component_code='ROUNDING')
    row = (Payslip.objects.filter(run_id=ref_run_id, employee_id=employee_id)
           .annotate(earned=sum_of(earnings, 'amount', group='payslip_id')).values('earned', 'inputs_snapshot').first())
    if not row:
        return None
    i = row['inputs_snapshot']['input']
    return {'gross': str(row['earned']), 'attendance': i['attendance'], 'inputs': i['inputs'], 'ytd': i['ytd']}


# ---------------------------------------------------------------- lifecycle (plan §9.5, §11.8)

@transaction.atomic
def review_run(company_id: str, run_id: str, actor_id: str, notes: str | None = None) -> None:
    run = get_run(company_id, run_id)
    if run['status'] != RunStatus.COMPUTED:
        raise ConflictError(f"run is {run['status']}; compute it first")
    runs(company_id).filter(pk=run['id']).update(status=RunStatus.REVIEWED, reviewed_at=Now(), maker_id=actor_id,
                                                 **({'notes': notes} if notes is not None else {}))
    audit('review', 'payroll_run', actor_id=actor_id, row_id=run['id'])


@transaction.atomic
def approve_run(company_id: str, run_id: str, approver_id: str, roles) -> dict:
    """
    HR sign-off: payroll is owned by HR, with no finance step. The approver may be the person who prepared the run
    unless payroll.policy.separateApprover is on, in which case a different HR user must approve.
    """
    run = get_run(company_id, run_id)
    if run['status'] != RunStatus.REVIEWED:
        raise ConflictError(f"run is {run['status']}; review it first")
    if 'hr' not in roles and 'admin' not in roles:
        raise ForbiddenError('approval requires the HR role')
    policy = payroll_policy(company_id)
    if policy['separateApprover'] and run['maker_id'] == str(approver_id):
        raise ForbiddenError('company policy requires a different HR user to approve this run')
    payload = json.dumps({'runId': run['id'], 'totals': run['totals'], 'approverId': approver_id, 'at': iso_ms(datetime.now(timezone.utc))},
                         separators=(',', ':'), ensure_ascii=False)
    signature = 'sha256:' + hashlib.sha256(payload.encode()).hexdigest()
    runs(company_id).filter(pk=run['id']).update(status=RunStatus.APPROVED, approved_at=Now(), checker_id=approver_id,
                                                 approval_signature=signature)
    audit('approve', 'payroll_run', actor_id=approver_id, row_id=run['id'], after={'signature': signature, 'payload': payload})
    return {'signature': signature, 'approved': True}


def lock_run(company_id: str, run_id: str, actor_id: str) -> dict:
    from hajiri.attendance.service import lock_attendance
    from hajiri.expenses.payroll import settle_run_payouts

    run = get_run(company_id, run_id)
    run_id = run['id']
    if run['status'] != RunStatus.APPROVED:
        raise ConflictError(f"run is {run['status']}; approve it first")
    p = get_period(company_id, run['period_id'])
    with transaction.atomic():
        # A run scoped to specific employees locks only their attendance; a regular run locks the period.
        only = run['params'].get('employeeIds')
        if isinstance(only, list):
            n = (latest_days().for_company(company_id)
                 .filter(employee_id__in=[as_uuid(i) for i in only], local_date__range=(p['start_ad'], p['end_ad']), locked_at__isnull=True)
                 .update(locked_at=Now()))
        else:
            n = lock_attendance(company_id, p['start_ad'], p['end_ad'])
            periods(company_id).filter(pk=p['id']).update(attendance_locked_at=Coalesce(F('attendance_locked_at'), Now()))
        runs(company_id).filter(pk=run_id).update(status=RunStatus.LOCKED, locked_at=Now())
        if run['run_type'] == RunType.REGULAR:
            slip = Payslip.objects.filter(run_id=run_id, employee_id=OuterRef('employee_id'))
            (OtSlip.objects.filter(Exists(slip), local_date__range=(p['start_ad'], p['end_ad']), status=OtSlip.Status.APPROVED)
             .update(status=OtSlip.Status.POSTED, posted_payslip_id=Subquery(slip.values('id')[:1])))
        installments = []
        for sl_id, snapshot in Payslip.objects.filter(run_id=run_id).values_list('id', 'inputs_snapshot'):
            for li in ((snapshot or {}).get('result') or {}).get('loanInstallments') or []:
                installments.append(LoanInstallment(loan_id=li['loanId'], payslip_id=sl_id, amount=li['amount']))
        LoanInstallment.objects.bulk_create(installments, ignore_conflicts=True)
        loan_ids = {li.loan_id for li in installments}
        if loan_ids:
            repaid = sum_of(LoanInstallment.objects.filter(loan_id=OuterRef('pk')), 'amount', group='loan_id')
            Loan.objects.filter(pk__in=loan_ids, principal__lte=repaid).update(status=Loan.Status.CLOSED)
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
    unpublished = Payslip.objects.filter(run_id=run['id'], published_at__isnull=True)
    published = list(unpublished.select_for_update().values_list('id', 'employee_id'))
    Payslip.objects.filter(pk__in=[pid for pid, _ in published]).update(published_at=Now())
    for pid, employee_id in published:
        emp = str(employee_id)
        notify(company_id=str(company_id), user_id=user_id_for_employee(emp), employee_id=emp, template='payslip.published',
               title='Your payslip is ready', body=f"Your payslip for {bs_month_name(p['bs_year'], p['bs_month'])} is ready to view.",
               payload={'payslipId': str(pid)}, channels=('push',))
    audit('publish', 'payroll_run', actor_id=actor_id, row_id=run['id'], after={'payslips': len(published)})
    return len(published)


# ---------------------------------------------------------------- bonus pool (plan §11.6)

WORKED = (Day.PRESENT, Day.HALF_DAY, Day.HALF_DAY_LEAVE, Day.WFH, Day.ON_LEAVE)
NOT_SCHEDULED = (Day.WEEKLY_OFF, Day.HOLIDAY, Day.OFF_SHIFT)


def bonus_allocation(company_id: str, fiscal_year_start: int, net_profit: str) -> dict:
    fy = fiscal_year(fiscal_year_start)
    rules, _ = resolve_rules(fy.end_ad)
    caps = rules.get('bonus.caps') or {'poolRate': '0.10', 'monthsCapLow': 8, 'monthsCapHigh': 6, 'lowPayThreshold': '39100',
                                       'minWorkedFraction': '0.5'}
    wage_slips = locked_payslips().filter(employee_id=OuterRef('pk'), run__run_type=RunType.REGULAR,
                                          run__period__start_ad__range=(fy.start_ad, fy.end_ad))
    year_days = latest_days().filter(employee_id=OuterRef('pk'), local_date__range=(fy.start_ad, fy.end_ad))
    emps = (Employee.objects.for_company(company_id).order_by('emp_code')
            .annotate(wages=sum_of(wage_slips, 'gross', group='employee_id'), months=count_of(wage_slips, group='employee_id'),
                      worked=count_of(year_days.filter(status__in=WORKED), group='employee_id'),
                      scheduled=count_of(year_days.exclude(status__in=NOT_SCHEDULED), group='employee_id'))
            .values('id', 'emp_code', 'wages', 'months', 'worked', 'scheduled'))
    with localcontext(ENGINE_CONTEXT):
        pool = D(net_profit) * D(caps['poolRate'])
        eligible = [e for e in emps if D(e['wages']) > 0
                    and (e['scheduled'] == 0 or D(e['worked']) / D(e['scheduled']) >= D(caps['minWorkedFraction']))]
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
            by_employee[str(e['id'])] = to_fixed(amount, 2)
            detail.append({'employeeId': str(e['id']), 'empCode': e['emp_code'], 'wages': str(e['wages']), 'share': to_fixed(share, 2),
                           'capMonths': cap_months, 'amount': to_fixed(amount, 2)})
        return {'pool': to_fixed(pool, 2), 'byEmployee': by_employee, 'detail': detail}


__all__ = ['get_period', 'ensure_period', 'get_run', 'payroll_policy', 'preflight', 'active_employees', 'components_for',
           'blend_segments', 'compute_run', 'review_run', 'approve_run', 'lock_run', 'publish_run', 'bonus_allocation',
           'months_between', 'year_to_date', 'verified_declaration', 'BsRangeError']
