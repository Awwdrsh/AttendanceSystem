"""Optimum Citizen Investment Trust contribution (port of src/optimum.ts)."""
from __future__ import annotations

from typing import Literal, TypedDict

from .compute import compute_payslip
from .numeric import D, Decimal, dmax, dmin, engine_context, floor, ceil, to_dp, to_fixed
from .types import PayrollInput, PayrollResult


class OptimumCit(TypedDict):
    currentMonthly: str
    optimumMonthly: str
    currentAnnualTax: str
    optimumAnnualTax: str
    annualSaving: str
    # Lower of the annual cap and one third of the projected income: the most retirement saving that reduces tax.
    deductionLimit: str
    # Retirement contributions counted this year with the current CIT (SSF/PF shares, CIT, other approved funds).
    retirementWithCurrent: str
    remainingMonths: int
    # Why the suggestion stops where it does.
    reason: Literal['limit', 'no_tax_left', 'net_pay', 'already_optimal']


def _annual_retirement(r: PayrollResult, input: PayrollInput) -> Decimal:  # noqa: A002
    """Annual retirement contributions the tax projection counts (year to date + this month + future months + other funds)."""
    month = Decimal(0)
    for k in ('SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT'):
        found = next((l for l in r['lines'] if l['code'] == k), None)
        month = month + D(found['amount'] if found is not None else 0)
    future = max(0, r['tax']['remainingMonths'] - 1)
    return (D(input['ytd']['retirementEmployee']) + D(input['ytd']['retirementEmployer']) + month
            + D(r['tax']['recurringRetirement']) * D(future) + D(input['declaration']['otherRetirementAnnual'] or 0))


@engine_context
def optimum_cit(input: PayrollInput) -> OptimumCit:  # noqa: A002
    """The monthly CIT contribution, from this month to the end of the fiscal year, that brings the year's tax lowest.

    It is the smallest whole-rupee amount at which more CIT no longer reduces tax (the retirement deduction limit is
    reached, or the remaining income is already untaxed), and never more than the month's net pay can cover. Pure:
    runs the same engine as payroll with only the CIT amount changed.
    """
    def at(monthly: Decimal) -> PayrollResult:
        return compute_payslip({**input, 'declaration': {**input['declaration'], 'citMonthly': to_fixed(monthly, 2)}})

    current = D(input['declaration']['citMonthly'] or 0)
    base = at(current)
    remaining = base['tax']['remainingMonths']
    d = input['rules']['tax.deductions']
    limit = dmin(D(d['retirementCap']), D(base['tax']['projectedAnnualTaxable']) / D(d['retirementDivisor']))
    counted = _annual_retirement(base, input)
    # Upper bound: the whole deduction room spread over the remaining months, but never more than the month can pay.
    room = dmax(0, limit - counted)
    by_room = ceil(current + room / D(remaining))
    by_pay = floor(current + dmax(0, D(base['net'])))
    hi = dmin(by_room, by_pay)
    floor_tax = D(at(hi)['tax']['annualTax'])
    # Tax never rises with more CIT, so the smallest amount that reaches the floor is found by bisection on whole rupees.
    lo = Decimal(0)
    top = hi
    while lo < top:
        mid = floor((lo + top) / 2)
        if D(at(mid)['tax']['annualTax']) <= floor_tax:
            top = mid
        else:
            lo = mid + 1
    best = at(lo)
    best_tax = D(best['tax']['annualTax'])
    current_tax = D(base['tax']['annualTax'])
    # Keep the current amount when it already reaches the lowest tax (contributing more is the employee's choice).
    keep = current_tax <= best_tax
    reason = ('already_optimal' if keep
              else 'no_tax_left' if best_tax.is_zero()
              else 'net_pay' if by_pay < by_room and lo == hi
              else 'no_tax_left' if lo < hi else 'limit')
    return {
        'currentMonthly': to_fixed(current, 2), 'optimumMonthly': to_fixed(current if keep else lo, 2),
        'currentAnnualTax': to_fixed(current_tax, 2), 'optimumAnnualTax': to_fixed(current_tax if keep else best_tax, 2),
        'annualSaving': to_fixed(Decimal(0) if keep else current_tax - best_tax, 2),
        'deductionLimit': to_fixed(to_dp(limit, 2), 2), 'retirementWithCurrent': to_fixed(to_dp(counted, 2), 2),
        'remainingMonths': remaining, 'reason': reason,  # type: ignore[typeddict-item]
    }
