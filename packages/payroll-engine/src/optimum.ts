import { computePayslip } from './compute.js';
import { D, Decimal } from './decimal.js';
import type { PayrollInput, PayrollResult } from './types.js';

export interface OptimumCit {
  currentMonthly: string;
  optimumMonthly: string;
  currentAnnualTax: string;
  optimumAnnualTax: string;
  annualSaving: string;
  /** Lower of the annual cap and one third of the projected income: the most retirement saving that reduces tax. */
  deductionLimit: string;
  /** Retirement contributions counted this year with the current CIT (SSF/PF shares, CIT, other approved funds). */
  retirementWithCurrent: string;
  remainingMonths: number;
  /** Why the suggestion stops where it does. */
  reason: 'limit' | 'no_tax_left' | 'net_pay' | 'already_optimal';
}

/** Annual retirement contributions the tax projection counts (year to date + this month + future months + other funds). */
function annualRetirement(r: PayrollResult, input: PayrollInput): Decimal {
  const month = ['SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT'].reduce((a, k) => a.plus(r.lines.find((l) => l.code === k)?.amount ?? 0), new Decimal(0));
  const future = Math.max(0, r.tax.remainingMonths - 1);
  return D(input.ytd.retirementEmployee).plus(input.ytd.retirementEmployer).plus(month).plus(D(r.tax.recurringRetirement).times(future))
    .plus(D(input.declaration.otherRetirementAnnual || 0));
}

/**
 * The monthly Citizen Investment Trust contribution, from this month to the end of the fiscal year, that brings the
 * year's tax lowest: the smallest whole-rupee amount at which more CIT no longer reduces tax (the retirement deduction
 * limit is reached, or the remaining income is already untaxed). It never exceeds what the month's net pay can cover.
 * Pure: runs the same engine as payroll with only the CIT amount changed.
 */
export function optimumCit(input: PayrollInput): OptimumCit {
  const at = (monthly: Decimal) => computePayslip({ ...input, declaration: { ...input.declaration, citMonthly: monthly.toFixed(2) } });
  const current = D(input.declaration.citMonthly || 0);
  const base = at(current);
  const remaining = base.tax.remainingMonths;
  const d = input.rules['tax.deductions'];
  const limit = Decimal.min(D(d.retirementCap), D(base.tax.projectedAnnualTaxable).div(D(d.retirementDivisor)));
  const counted = annualRetirement(base, input);
  // Upper bound: the whole deduction room spread over the remaining months, but never more than the month can pay.
  const room = Decimal.max(0, limit.minus(counted));
  const byRoom = current.plus(room.div(remaining)).ceil();
  const byPay = current.plus(Decimal.max(0, D(base.net))).floor();
  const hi = Decimal.min(byRoom, byPay);
  const floorTax = D(at(hi).tax.annualTax);
  // Tax never rises with more CIT, so the smallest amount that reaches the floor is found by bisection on whole rupees.
  let lo = new Decimal(0);
  let top = hi;
  while (lo.lt(top)) {
    const mid = lo.plus(top).div(2).floor();
    if (D(at(mid).tax.annualTax).lte(floorTax)) top = mid; else lo = mid.plus(1);
  }
  const best = at(lo);
  const bestTax = D(best.tax.annualTax);
  const currentTax = D(base.tax.annualTax);
  // Keep the current amount when it already reaches the lowest tax (contributing more is the employee's choice, not an error).
  const keep = currentTax.lte(bestTax);
  const reason: OptimumCit['reason'] = keep ? 'already_optimal'
    : bestTax.isZero() ? 'no_tax_left'
    : byPay.lt(byRoom) && lo.eq(hi) ? 'net_pay'
    : lo.lt(hi) ? 'no_tax_left' : 'limit';
  return {
    currentMonthly: current.toFixed(2), optimumMonthly: (keep ? current : lo).toFixed(2),
    currentAnnualTax: currentTax.toFixed(2), optimumAnnualTax: (keep ? currentTax : bestTax).toFixed(2),
    annualSaving: (keep ? new Decimal(0) : currentTax.minus(bestTax)).toFixed(2),
    deductionLimit: limit.toDecimalPlaces(2).toFixed(2), retirementWithCurrent: counted.toDecimalPlaces(2).toFixed(2), remainingMonths: remaining, reason,
  };
}
