"""Payslip computation: component graph, proration, statutory shares, TDS, loans and totals (port of src/compute.ts).

compute_payslip is pure: it reads a PayrollInput dict (camelCase keys, as in the TS engine) and returns a
PayrollResult dict whose every field matches the TS output for the same input.
"""
from __future__ import annotations

from typing import Any, Callable

from ._js import js_get, js_member, js_mod, js_truthy, nullish
from .formula import EvalEnv, FormulaError, evaluate_formula, formula_references
from .numeric import (
    UNDEFINED, ZERO, D, Decimal, dmax, dmin, dsum, engine_context, is_neg, js_num_str, js_str, max0, paisa, rupee,
    to_fixed,
)
from .tax import compute_tds
from .types import ComponentDef, Line, PayrollInput, PayrollResult, ProrationBasis

__all__ = ['PayrollError', 'MAX_LINE_AMOUNT', 'payable_fraction', 'compute_payslip']

# Codes the engine computes itself when the component has no formula (plan §11.1 graph).
BUILTIN = frozenset({'OT', 'FESTIVAL', 'LEAVE_ENCASH', 'SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT', 'GROSS',
                     'TAXABLE', 'TDS', 'LOAN'})
# Earnings that do not recur every month (not projected for TDS).
ONE_OFF = frozenset({'OT', 'FESTIVAL', 'BONUS', 'ARREARS', 'LEAVE_ENCASH', 'NOTICE_PAY', 'ABSENCE', 'ADVANCE'})
STATUTORY_PHASE = frozenset({'SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT'})
RETIREMENT_CODES = ('SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT')


class PayrollError(Exception):
    pass


# One below the numeric(14,2) ceiling of payslip lines.
MAX_LINE_AMOUNT = Decimal('1e12')


def _fx(v: Decimal) -> str:
    return to_fixed(v, 2)


@engine_context
def payable_fraction(paid_days: Decimal, days_in_month: int, basis: ProrationBasis) -> Decimal:
    """Payable fraction of the month (plan §11.3). unpaid = days in month - paid days; basis days = 29-32 | 30 | 26."""
    basis_days = 30 if basis == 'fixed_30' else 26 if basis == '26_day' else days_in_month
    unpaid = D(days_in_month) - paid_days
    return dmax(0, dmin(1, (D(basis_days) - unpaid) / D(basis_days)))


def _phase_of(c: ComponentDef) -> int:
    if c['type'] == 'earning':
        return 1
    if c['code'] in STATUTORY_PHASE or c['type'] == 'employer_contribution':
        return 2
    if c['type'] == 'statistical':
        return 3
    if c['code'] == 'TDS':
        return 4
    if c['code'] == 'LOAN':
        return 6
    return 5


# ICU root collation for ASCII (what String.prototype.localeCompare uses): punctuation, then digits, then letters with
# case ignored at the primary level; lowercase sorts before uppercase only when the strings are otherwise equal.
_PRIMARY_ORDER = [' ', '_', '-', ',', ';', ':', '!', '?', '.', "'", '"', '(', ')', '[', ']', '{', '}', '@', '*', '/',
                  '\\', '&', '#', '%', '`', '^', '+', '<', '=', '>', '|', '~', '$', *'0123456789',
                  *('aA bB cC dD eE fF gG hH iI jJ kK lL mM nN oO pP qQ rR sS tT uU vV wW xX yY zZ'.split())]
_PRIMARY = {ch: i for i, group in enumerate(_PRIMARY_ORDER) for ch in group}


def _collation_key(s: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Characters outside printable ASCII fall back to code point order after ASCII (an approximation of ICU).
    primary = tuple(_PRIMARY.get(ch, 1000 + ord(ch)) for ch in s)
    tertiary = tuple(1 if 'A' <= ch <= 'Z' else 0 for ch in s)
    return primary, tertiary


def _order(components: list[ComponentDef]) -> list[ComponentDef]:
    """Topological order inside one phase by formula references (cycle = error); stable by sortOrder otherwise."""
    by_code = {c['code']: c for c in components}
    deps: dict[str, list[str]] = {}
    for c in components:
        refs = list(formula_references(c['formula'])) if js_truthy(c.get('formula')) else []
        cond = list(formula_references(c['condition'])) if js_truthy(c.get('condition')) else []
        deps[c['code']] = [r for r in [*refs, *cond] if r in by_code and r != c['code']]
    out: list[ComponentDef] = []
    state: dict[str, int] = {}

    def visit(code: str, chain: list[str]) -> None:
        s = state.get(code)
        if s == 2:
            return
        if s == 1:
            raise PayrollError(f"formula cycle: {' -> '.join([*chain, code])}")
        state[code] = 1
        for d in deps.get(code, []):
            visit(d, [*chain, code])
        state[code] = 2
        out.append(by_code[code])

    for c in sorted(components, key=lambda c: c['sortOrder']):
        visit(c['code'], [])
    return out


def _rule_lookup(rules: dict[str, Any]) -> Callable[[str, str | None], Any]:
    def lookup(key: str, path: str | None) -> Any:
        v = js_get(rules, key)
        if v is UNDEFINED:
            raise FormulaError(f'unknown rule {key}')
        if path:
            for seg in path.split('.'):
                v = js_member(v, seg)
                if v is UNDEFINED:
                    raise FormulaError(f'rule {key} has no {path}')
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return D(v)
        if isinstance(v, str):
            try:
                d = D(v)
                return v if d.is_nan() else d
            except Exception:  # noqa: BLE001 - mirrors the TS catch-all
                return v
        raise FormulaError(f"rule {key}{'.' + path if path else ''} is not a value")

    return lookup


@engine_context
def compute_payslip(input: PayrollInput) -> PayrollResult:  # noqa: A002 - mirrors the TS parameter name
    employee = input['employee']
    period = input['period']
    policy = input['policy']
    rules: dict[str, Any] = input['rules']  # type: ignore[assignment]
    attendance = input['attendance']
    ytd = input['ytd']
    declaration = input['declaration']
    extras: dict[str, Any] = nullish(input.get('extras'), {})  # type: ignore[assignment]
    flags: list[str] = []
    explain: list[dict[str, str]] = []
    lines: list[Line] = []
    amounts: dict[str, Decimal] = {}

    paid_days = D(attendance['paidDays'])
    lop_days = D(attendance['lopDays'])
    ot_hours = D(attendance['otHours'])
    fraction = payable_fraction(paid_days, period['daysInMonth'], policy['prorationBasis'])

    def base(code: str) -> Decimal:
        return D(nullish(js_get(input['baseAmounts'], code), 0))

    basic_full = base('BASIC')

    def inputs_for(code: str) -> Decimal:
        return dsum(D(i['amount']) for i in input['inputs'] if i['code'] == code)

    months_of_service = extras.get('monthsOfServiceInFy')
    vars_: dict[str, Any] = {
        'days_in_bs_month': D(period['daysInMonth']), 'paid_days': paid_days, 'lop_days': lop_days,
        'hourly_divisor': D(policy['hourlyDivisor']), 'ot_hours': ot_hours,
        'months_in_service': D(nullish(months_of_service, 12)), 'is_female': employee['gender'] == 'female',
        'regime': employee['regime'], 'bs_month': D(period['bsMonth']), 'fiscal_year': D(period['fiscalYearStart']),
    }

    def set_var(code: str, v: Decimal) -> None:
        # JS: vars['__proto__'] = x sets the prototype instead of a key, so that code never becomes a variable.
        if code != '__proto__':
            vars_[code] = v

    env = EvalEnv(vars=vars_, rule=_rule_lookup(rules))

    def eval_num(src: str, code: str) -> Decimal:
        v = evaluate_formula(src, env)
        if not isinstance(v, Decimal):
            raise PayrollError(f'{code}: formula must produce a number')
        if not v.is_finite():
            raise PayrollError(f'{code}: formula produced a non-finite amount')
        return v

    # Pre-proration amount of every evaluated component (zero amounts included), for the recurring-income projection.
    unprorated: dict[str, Decimal] = {}

    def round_line(c: ComponentDef, v: Decimal) -> Decimal:
        statutory_or_tds = js_truthy(c.get('statutory')) or c['code'] == 'TDS'
        return rupee(v) if c['rounding'] == 'rupee' or (policy['rounding'] == 'half_up_rupee' and statutory_or_tds) else paisa(v)

    def push(c: ComponentDef, amount: Decimal, basis: dict[str, Any], text: str | None) -> None:
        amounts[c['code']] = amount
        set_var(c['code'], amount)
        if amount.is_zero() and c['type'] != 'statistical':
            return
        lines.append({'code': c['code'], 'type': c['type'], 'nameEn': c['nameEn'], 'nameNe': nullish(c.get('nameNe'), None),
                      'amount': _fx(amount), 'taxable': c['taxable'], 'basis': basis, 'sortOrder': c['sortOrder']})
        if text:
            explain.append({'code': c['code'], 'text': text})

    ssf = rules['ssf.rates']
    pf = rules['pf.rates']
    festival_month = nullish(employee['festivalMonth'], rules['festival.allowance']['defaultBsMonth'])

    def month_index_of(bs_month: int) -> int:
        return js_mod(bs_month - 4 + 12, 12) + 1  # Shrawan = 1

    loan_installments: list[dict[str, str]] = []
    tax: dict[str, Any] | None = None

    def earning_total() -> Decimal:
        return dsum(l['amount'] for l in lines if l['type'] == 'earning')

    def deduction_total() -> Decimal:
        return dsum(l['amount'] for l in lines if l['type'] == 'deduction')

    def pct(rate: str) -> str:
        return js_str(D(rate) * 100)

    def builtin(c: ComponentDef) -> tuple[Decimal, dict[str, Any], str] | None:
        nonlocal tax
        basic = amounts.get('BASIC', ZERO)
        code = c['code']
        if code == 'OT':
            if not employee['otEligible'] or ot_hours.is_zero():
                return None
            mult = D(js_get(rules['ot.multiplier'], nullish(attendance.get('otDayType'), 'working')))
            divisor = policy['hourlyDivisor']
            hourly = basic_full / D(divisor)
            amt = rupee(ot_hours * hourly * mult)
            return (amt, {'hours': _fx(ot_hours), 'hourlyBasic': _fx(paisa(hourly)), 'multiplier': _fx(mult), 'divisor': divisor},
                    f'{_fx(ot_hours)} h × (basic {_fx(basic_full)} ÷ {js_num_str(divisor)}) × {_fx(mult)} = {_fx(amt)} '
                    f'(Labour Act Sec 31(1))')
        if code == 'FESTIVAL':
            due = js_truthy(extras.get('forceFestivalAllowance')) or (period['bsMonth'] == festival_month and not ytd['festivalPaid'])
            if not due:
                return None
            months = min(nullish(months_of_service, 12), 12)
            amt = paisa(basic_full * D(rules['festival.allowance']['months']) * D(months) / 12)
            return (amt, {'basic': _fx(basic_full), 'monthsOfService': months},
                    f'basic {_fx(basic_full)} × {js_num_str(months)}/12 = {_fx(amt)} (Sec 37, pro-rated)')
        if code == 'LEAVE_ENCASH':
            days = D(nullish(extras.get('leaveEncashDays'), 0))
            if days.is_zero():
                return None
            divisor = nullish(extras.get('leaveEncashDivisor'), period['daysInMonth'])
            last = D(extras['lastBasic']) if 'lastBasic' in extras else basic_full
            amt = paisa(days * last / D(divisor))
            return (amt, {'days': _fx(days), 'lastBasic': _fx(last), 'divisor': divisor},
                    f'{_fx(days)} days × last basic {_fx(last)} ÷ {js_num_str(divisor)} = {_fx(amt)} (Sec 49(2))')
        if code == 'SSF_EE':
            return (basic * D(ssf['employee']), {'basic': _fx(basic), 'rate': ssf['employee']},
                    f"{pct(ssf['employee'])}% of basic {_fx(basic)} (CBSS Act Sec 4)") if employee['regime'] == 'ssf' else None
        if code == 'SSF_ER':
            return (basic * D(ssf['employer']), {'basic': _fx(basic), 'rate': ssf['employer']},
                    f"{pct(ssf['employer'])}% of basic {_fx(basic)} employer share (CBSS Act Sec 4)") if employee['regime'] == 'ssf' else None
        if code == 'PF_EE':
            return (basic * D(pf['employee']), {'basic': _fx(basic), 'rate': pf['employee']},
                    f"{pct(pf['employee'])}% of basic (Labour Act Sec 52(1))") if employee['regime'] == 'pf' else None
        if code == 'PF_ER':
            return (basic * D(pf['employer']), {'basic': _fx(basic), 'rate': pf['employer']},
                    f"{pct(pf['employer'])}% of basic employer share (Sec 52(1))") if employee['regime'] == 'pf' else None
        if code == 'GRATUITY':
            return (basic * D(pf['gratuity']), {'basic': _fx(basic), 'rate': pf['gratuity']},
                    f"{pct(pf['gratuity'])}% of basic gratuity accrual (Sec 53)") if employee['regime'] == 'pf' else None
        if code == 'CIT':
            v = D(declaration['citMonthly'])
            return None if v.is_zero() else (v, {'declared': _fx(v)}, 'citizen investment trust as declared')
        if code == 'GROSS':
            return earning_total(), {}, 'sum of earnings'
        if code == 'TAXABLE':
            inc = policy['includeEmployerContributionInIncome']
            t = dsum(l['amount'] for l in lines
                     if (l['type'] == 'earning' and l['taxable']) or (inc and l['type'] == 'employer_contribution' and l['taxable']))
            return (t, {'includesEmployerContribution': inc},
                    'taxable earnings' + (' + employer retirement contribution' if inc else ''))
        if code == 'TDS':
            month_taxable = amounts.get('TAXABLE', ZERO)
            # Recurring monthly income for the annual projection (plan §11.4): the full-month (pre-proration) amount of
            # every recurring taxable earning, less any one-time input part, plus the employer retirement contribution
            # on the full basic when policy counts it as income. A formula that references an already prorated line
            # stays prorated; the true-up in later months corrects any residual.
            recurring_earnings = dsum(max0(unprorated.get(k['code'], ZERO) - inputs_for(k['code']))
                                      for k in input['components']
                                      if k['type'] == 'earning' and k['taxable'] and k['code'] not in ONE_OFF)
            regime = employee['regime']
            if not policy['includeEmployerContributionInIncome']:
                employer_recurring = ZERO
            elif regime == 'ssf':
                employer_recurring = basic_full * D(ssf['employer'])
            elif regime == 'pf':
                employer_recurring = basic_full * (D(pf['employer']) + D(pf['gratuity']))
            else:
                employer_recurring = ZERO
            employee_recurring = (basic_full * D(ssf['employee']) if regime == 'ssf'
                                  else basic_full * D(pf['employee']) if regime == 'pf' else ZERO)
            override = extras.get('recurringOverride')
            recurring = D(override['taxable']) if js_truthy(override) else recurring_earnings + employer_recurring
            retirement = ZERO
            for k in RETIREMENT_CODES:
                retirement = retirement + amounts.get(k, ZERO)
            recurring_retirement = (D(override['retirement']) if js_truthy(override)
                                    else employee_recurring + employer_recurring + D(declaration['citMonthly']))
            festival_later = (basic_full * D(rules['festival.allowance']['months'])
                              if not ytd['festivalPaid'] and not amounts.get('FESTIVAL', ZERO) > 0
                              and month_index_of(festival_month) > period['monthIndexInFy']
                              and not js_truthy(extras.get('isFinalSettlement')) else ZERO)
            tax = compute_tds({
                'schedule': rules['tax.schedule'], 'deductions': rules['tax.deductions'], 'regime': regime,
                'isResident': employee['isResident'], 'gender': employee['gender'], 'maritalStatus': employee['maritalStatus'],
                'remoteAreaClass': employee['remoteAreaClass'], 'disability': employee['disability'],
                'remunerationOnlyIncome': nullish(employee.get('remunerationOnlyIncome'), True),
                'monthTaxable': month_taxable, 'recurringTaxable': recurring, 'futureOneOffs': festival_later,
                'ytdTaxable': D(ytd['taxable']), 'ytdTdsPaid': D(ytd['tdsPaid']), 'monthRetirement': retirement,
                'recurringRetirement': recurring_retirement,
                'ytdRetirement': D(ytd['retirementEmployee']) + D(ytd['retirementEmployer']),
                'otherRetirementAnnual': D(declaration['otherRetirementAnnual']), 'lifePremium': D(declaration['lifePremium']),
                'healthPremium': D(declaration['healthPremium']), 'medicalCost': D(declaration['medicalCost']),
                'donation': D(nullish(declaration.get('donation'), 0)), 'monthIndexInFy': period['monthIndexInFy'],
                'deductFullTaxNow': js_truthy(extras.get('deductFullTaxNow')),
                'isFinalSettlement': js_truthy(extras.get('isFinalSettlement')),
            })
            amt = D(tax['tdsThisMonth'])
            fy = tax['fiscalYearStart']
            return (amt, {'annualTax': tax['annualTax'], 'projectedTaxable': tax['projectedAnnualTaxable'],
                          'remainingMonths': tax['remainingMonths'], 'status': tax['status']},
                    f"annual tax {tax['annualTax']} on projected taxable {tax['taxableAfterDeductions']} "
                    f"(FY {fy}/{str(fy + 1)[-2:]}), less YTD TDS {tax['ytdTdsPaid']}, over {tax['remainingMonths']} "
                    f'month(s) (Income Tax Act Sec 87)')
        if code == 'LOAN':
            if not input['loans']:
                return None
            gross = amounts['GROSS'] if 'GROSS' in amounts else earning_total()
            available = max0(gross - deduction_total())
            total = ZERO
            for loan in input['loans']:
                due = (D(loan['remaining']) if js_truthy(extras.get('recoverLoansInFull'))
                       else dmin(D(loan['emi']), D(loan['remaining'])))
                take = dmin(due, available)
                if take < due:
                    flags.append('LoanCapped')
                if take > 0:
                    loan_installments.append({'loanId': loan['id'], 'amount': _fx(paisa(take))})
                    total = total + take
                    available = available - take
            return None if total.is_zero() else (total, {'installments': loan_installments}, 'loan/advance instalments (Sec 38(i))')
        return None

    # Evaluate the component graph phase by phase (plan §11.1 order).
    phases: dict[int, list[ComponentDef]] = {}
    for c in input['components']:
        phases.setdefault(_phase_of(c), []).append(c)
    for p in (1, 2, 3, 4, 5, 6):
        for c in _order(phases.get(p, [])):
            code = c['code']
            if js_truthy(c.get('condition')):
                ok = evaluate_formula(c['condition'], env)  # type: ignore[arg-type]
                if not isinstance(ok, bool):
                    raise PayrollError(f'{code}: condition must be true/false')
                if not ok:
                    set_var(code, ZERO)
                    continue
            text: str | None
            if js_truthy(c.get('formula')):
                amount = eval_num(c['formula'], code)  # type: ignore[arg-type]
                basis: dict[str, Any] = {'formula': c['formula']}
                text = f"{c['formula']} = {_fx(paisa(amount))}"
            elif code in BUILTIN:
                b = builtin(c)
                if b is None:
                    set_var(code, ZERO)
                    amounts[code] = ZERO
                    continue
                amount, basis, text = b
            else:
                bv = base(code)
                iv = inputs_for(code)
                amount = bv + iv
                basis = {'base': _fx(bv), 'inputs': _fx(iv)}
                text = f"{c['nameEn']} {_fx(bv)}" if iv.is_zero() else f"{c['nameEn']} {_fx(bv)} + one-time {_fx(iv)}"
            unprorated[code] = amount
            if c['dependsOnPaymentDays'] and not fraction == 1:
                before = amount
                amount = amount * fraction
                basis = {**basis, 'prorated': {'from': _fx(before), 'paidDays': _fx(paid_days),
                                               'daysInMonth': period['daysInMonth'], 'basis': policy['prorationBasis']}}
                basis_days = (period['daysInMonth'] if policy['prorationBasis'] == 'bs_calendar_days'
                              else 30 if policy['prorationBasis'] == 'fixed_30' else 26)
                text = f'{_fx(before)} × {_fx(paid_days)}/{js_num_str(basis_days)} paid days = {_fx(paisa(amount * 1))}'
            if is_neg(amount) and c['type'] != 'statistical':
                flags.append(f'NegativeAmount:{code}')
                amount = ZERO
            if not amount.is_finite():
                raise PayrollError(f'{code}: amount is not finite')
            # Payslip amounts are stored as numeric(14,2); anything larger is a formula or input mistake, not a salary.
            if amount.copy_abs() >= MAX_LINE_AMOUNT:
                raise PayrollError(f'{code}: amount {_fx(amount)} is too large (limit {to_fixed(MAX_LINE_AMOUNT, 0)})')
            amount = round_line(c, amount)
            # Withholding cannot exceed the pay it is withheld from: TDS (Sec 87 applies to amounts paid) and the Sec 38
            # (c)-(i) deductions are capped at the pay remaining after the statutory shares, so net never goes below
            # zero. A capped TDS is caught by the true-up in later months (only what was actually withheld counts).
            if c['type'] == 'deduction' and p >= 4 and code != 'LOAN':
                available = max0(earning_total() - deduction_total())
                if amount > available:
                    flags.append('TdsCapped' if code == 'TDS' else f'DeductionCapped:{code}')
                    basis = {**basis, 'capped': {'requested': _fx(amount), 'available': _fx(available)}}
                    amount = available
            push(c, amount, basis, text)

    # Totals (plan §11.7): gross and total deductions exclude the rounding adjustment; the ROUNDING line only exists so
    # that sum(earning lines) - sum(deduction lines) equals the whole-rupee net.
    gross_total = earning_total()
    deduction_sum = deduction_total()
    exact = gross_total - deduction_sum
    net = rupee(exact) if policy['rounding'] == 'half_up_rupee' else exact
    if not net == exact:
        diff = net - exact
        lines.append({'code': 'ROUNDING', 'type': 'earning' if diff > 0 else 'deduction', 'nameEn': 'Rounding',
                      'nameNe': 'पूर्णाङ्कन', 'amount': _fx(diff.copy_abs()), 'taxable': False,
                      'basis': {'exactNet': _fx(exact)}, 'sortOrder': 9999})
    if is_neg(net):
        flags.append('NegativeNet')

    mw = rules['min_wage']
    if (policy['fullTime'] and basic_full > 0
            and (basic_full < D(mw['basic']) or basic_full + base('DA') < D(mw['total']))):
        flags.append('BelowMinimumWage')

    ssf_allocation = ({k: _fx(paisa(amounts.get('BASIC', ZERO) * D(r))) for k, r in ssf['allocation'].items()}
                      if employee['regime'] == 'ssf' and 'SSF_ER' in amounts else None)

    employee_statutory = amounts.get('SSF_EE', ZERO) + amounts.get('PF_EE', ZERO)
    employer_statutory = amounts.get('SSF_ER', ZERO) + amounts.get('PF_ER', ZERO) + amounts.get('GRATUITY', ZERO)

    if tax is None:
        tax = compute_tds({
            'schedule': rules['tax.schedule'], 'deductions': rules['tax.deductions'], 'regime': employee['regime'],
            'isResident': employee['isResident'], 'gender': employee['gender'], 'maritalStatus': employee['maritalStatus'],
            'remoteAreaClass': employee['remoteAreaClass'], 'disability': employee['disability'],
            'remunerationOnlyIncome': nullish(employee.get('remunerationOnlyIncome'), True), 'monthTaxable': ZERO,
            'recurringTaxable': ZERO, 'futureOneOffs': ZERO, 'ytdTaxable': ZERO, 'ytdTdsPaid': ZERO,
            'monthRetirement': ZERO, 'recurringRetirement': ZERO, 'ytdRetirement': ZERO, 'otherRetirementAnnual': ZERO,
            'lifePremium': ZERO, 'healthPremium': ZERO, 'medicalCost': ZERO, 'monthIndexInFy': period['monthIndexInFy'],
            'deductFullTaxNow': False, 'isFinalSettlement': False,
        })

    lines.sort(key=lambda l: (l['sortOrder'], _collation_key(l['code'])))
    return {
        'lines': lines, 'gross': _fx(paisa(gross_total)), 'taxable': _fx(paisa(amounts.get('TAXABLE', ZERO))),
        'tds': _fx(paisa(amounts.get('TDS', ZERO))), 'employeeStatutory': _fx(paisa(employee_statutory)),
        'employerStatutory': _fx(paisa(employer_statutory)), 'totalDeductions': _fx(paisa(deduction_sum)),
        'net': _fx(paisa(net)), 'paidDays': _fx(paid_days), 'lopDays': _fx(lop_days), 'otHours': _fx(ot_hours),
        'payableFraction': to_fixed(fraction, 6), 'tax': tax, 'ssfAllocation': ssf_allocation,  # type: ignore[typeddict-item]
        'loanInstallments': loan_installments, 'explain': explain, 'flags': list(dict.fromkeys(flags)),
    }
