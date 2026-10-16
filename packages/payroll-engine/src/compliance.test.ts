import { describe, expect, it } from 'vitest';
import { computePayslip } from './compute.js';
import { D } from './decimal.js';
import { baseInput } from './fixtures.js';
import { optimumCit } from './optimum.js';

describe('donation deduction (Income Tax Act Sec 12)', () => {
  it('deducts approved donations up to 5% of adjusted taxable income', () => {
    const plain = computePayslip(baseInput({ base: { BASIC: '150000', ALLOWANCE: '50000' } }));
    const withGift = computePayslip(baseInput({ base: { BASIC: '150000', ALLOWANCE: '50000' }, declaration: { lifePremium: '0', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0', donation: '30000' } }));
    expect(withGift.tax.donationDeduction).toBe('30000.00');
    expect(D(withGift.tax.taxableAfterDeductions).toFixed(2)).toBe(D(plain.tax.taxableAfterDeductions).minus(30000).toFixed(2));
    expect(D(withGift.tax.annualTax).lt(plain.tax.annualTax)).toBe(true);
  });

  it('caps the deduction at the lower of Rs 1,00,000 and 5% of adjusted taxable income', () => {
    const small = computePayslip(baseInput({ declaration: { lifePremium: '0', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0', donation: '500000' } }));
    const adjusted = D(small.tax.taxableAfterDeductions).plus(small.tax.donationDeduction!);
    expect(small.tax.donationDeduction).toBe(adjusted.times('0.05').toDecimalPlaces(2).toFixed(2));
    const big = computePayslip(baseInput({ base: { BASIC: '600000', ALLOWANCE: '200000' }, declaration: { lifePremium: '0', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0', donation: '500000' } }));
    expect(big.tax.donationDeduction).toBe('100000.00');
  });

  it('is zero without a declaration and for non-residents', () => {
    expect(computePayslip(baseInput()).tax.donationDeduction).toBe('0.00');
    const nr = computePayslip(baseInput({ employee: { ...baseInput().employee, isResident: false }, declaration: { lifePremium: '0', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0', donation: '50000' } }));
    expect(nr.tax.donationDeduction).toBe('0.00');
  });
});

describe('optimum CIT', () => {
  it('fills the retirement deduction room and never lowers the saving below the brute-force best', () => {
    const input = baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' } });
    const o = optimumCit(input);
    expect(D(o.optimumMonthly).gt(0)).toBe(true);
    expect(D(o.annualSaving).gt(0)).toBe(true);
    // Brute force over a grid: no CIT amount gives a lower tax than the suggestion.
    for (const c of [0, 1000, 5000, 10000, 20000, 30000, 40000, 60000]) {
      const r = computePayslip({ ...input, declaration: { ...input.declaration, citMonthly: String(c) } });
      expect(D(r.tax.annualTax).gte(o.optimumAnnualTax)).toBe(true);
    }
    // One rupee less no longer reaches the lowest tax (it is the smallest such amount).
    const less = computePayslip({ ...input, declaration: { ...input.declaration, citMonthly: D(o.optimumMonthly).minus(1).toFixed(2) } });
    expect(D(less.tax.annualTax).gt(o.optimumAnnualTax)).toBe(true);
  });

  it('keeps the current CIT when it already reaches the lowest tax', () => {
    const input = baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' } });
    const o = optimumCit(input);
    const again = optimumCit({ ...input, declaration: { ...input.declaration, citMonthly: D(o.optimumMonthly).plus(5000).toFixed(2) } });
    expect(again.reason).toBe('already_optimal');
    expect(again.annualSaving).toBe('0.00');
  });

  it('suggests nothing when no tax is left to save', () => {
    const o = optimumCit(baseInput({ base: { BASIC: '20000', ALLOWANCE: '0' } }));
    expect(o.annualSaving).toBe('0.00');
  });

  it('spreads the room over the months left in the year', () => {
    const early = optimumCit(baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' } }));
    const late = optimumCit(baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' }, period: { bsYear: 2083, bsMonth: 12, daysInMonth: 30, fiscalYearStart: 2083, monthIndexInFy: 9 },
      ytd: { taxable: '1800000', tdsPaid: '150000', retirementEmployee: '105600', retirementEmployer: '192000', festivalPaid: true } }));
    expect(late.remainingMonths).toBe(4);
    expect(D(late.optimumMonthly).gt(early.optimumMonthly)).toBe(true);
  });
});
