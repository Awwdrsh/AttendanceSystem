import { describe, expect, it } from 'vitest';
import { computePayslip, payableFraction } from './compute.js';
import { D } from './decimal.js';
import { DEFAULT_COMPONENTS, rulesForFy } from './defaults.js';
import { baseInput } from './fixtures.js';

const line = (r: ReturnType<typeof computePayslip>, code: string) => r.lines.find((l) => l.code === code)?.amount;

describe('worked examples from plan.md', () => {
  it('G: FY 2083/84 single SSF contributor, basic 40,000 + allowances 20,000, life 30,000 -> TDS 0', () => {
    const r = computePayslip(baseInput({ fy: 2083, declaration: { lifePremium: '30000', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0' } }));
    expect(r.tax.projectedAnnualTaxable).toBe('856000.00');
    expect(r.tax.retirementDeduction).toBe('148800.00');
    expect(r.tax.taxableAfterDeductions).toBe('677200.00');
    expect(r.tax.annualTax).toBe('0.00');
    expect(r.tds).toBe('0.00');
    expect(line(r, 'SSF_EE')).toBe('4400.00');
    expect(line(r, 'SSF_ER')).toBe('8000.00');
    expect(r.gross).toBe('60000.00');
    expect(r.net).toBe('55600.00');
    expect(r.ssfAllocation).toEqual({ medical: '400.00', accident: '560.00', dependent: '108.00', old_age: '11332.00' });
  });
  it('G: same employee under FY 2082/83 single schedule -> annual tax 17,720, monthly TDS 1,477', () => {
    const r = computePayslip(baseInput({ fy: 2082, declaration: { lifePremium: '30000', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0' } }));
    expect(r.tax.taxableAfterDeductions).toBe('677200.00');
    expect(r.tax.bands[0]).toMatchObject({ upTo: '500000', waived: true, tax: '0.00' });
    expect(r.tax.bands[1]).toMatchObject({ slice: '177200.00', tax: '17720.00' });
    expect(r.tax.annualTax).toBe('17720.00');
    expect(r.tds).toBe('1477.00');
    expect(r.net).toBe('54123.00');
  });
  it('G couple under FY 2082/83 uses the couple table (600,000 first band)', () => {
    const r = computePayslip(baseInput({ fy: 2082, employee: { ...baseInput().employee, maritalStatus: 'couple' }, declaration: { ...baseInput().declaration, lifePremium: '30000' } }));
    expect(r.tax.status).toBe('couple');
    expect(r.tax.bands[1]).toMatchObject({ slice: '77200.00', tax: '7720.00' });
    expect(r.tds).toBe('643.00');
  });
  it('proration: basic 40,000, joined Shrawan 10 (22 of 31 days) -> 28,387.10', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '40000' }, attendance: { paidDays: '22', lopDays: '0', otHours: '0' } }));
    expect(line(r, 'BASIC')).toBe('28387.10');
    expect(r.payableFraction).toBe('0.709677');
    expect(payableFraction(D(28), 30, 'fixed_30').toFixed(4)).toBe('0.9333');
    expect(payableFraction(D(32), 32, 'fixed_30').toFixed(4)).toBe('1.0000');
    expect(payableFraction(D(24), 26, '26_day').toFixed(4)).toBe('0.9231');
  });
  it('C: OT pay 10 h at basic 30,000, divisor 208, 1.5x -> 2,163; day cap already applied upstream', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '30000' }, attendance: { paidDays: '31', lopDays: '0', otHours: '10' } }));
    expect(line(r, 'OT')).toBe('2163.00');
    expect(r.lines.find((l) => l.code === 'OT')?.basis).toMatchObject({ hourlyBasic: '144.23', multiplier: '1.50' });
    const none = computePayslip(baseInput({ base: { BASIC: '30000' }, attendance: { paidDays: '31', lopDays: '0', otHours: '10' }, employee: { ...baseInput().employee, otEligible: false } }));
    expect(line(none, 'OT')).toBeUndefined();
  });
  it('festival allowance: Ashwin, 8 months of service -> 26,666.67 and taxable in that month', () => {
    const r = computePayslip(baseInput({ period: { bsYear: 2083, bsMonth: 6, daysInMonth: 31, fiscalYearStart: 2083, monthIndexInFy: 3 }, extras: { monthsOfServiceInFy: 8 } }));
    expect(line(r, 'FESTIVAL')).toBe('26666.67');
    expect(D(r.taxable).toFixed(2)).toBe('94666.67');
    const paid = computePayslip(baseInput({ period: { bsYear: 2083, bsMonth: 6, daysInMonth: 31, fiscalYearStart: 2083, monthIndexInFy: 3 }, ytd: { ...baseInput().ytd, festivalPaid: true } }));
    expect(line(paid, 'FESTIVAL')).toBeUndefined();
    const chosen = computePayslip(baseInput({ period: { bsYear: 2083, bsMonth: 9, daysInMonth: 30, fiscalYearStart: 2083, monthIndexInFy: 6 }, employee: { ...baseInput().employee, festivalMonth: 9 } }));
    expect(line(chosen, 'FESTIVAL')).toBe('40000.00');
  });
  it('final settlement: encash 30 days at last basic 40,000 with divisor 30 -> 40,000; TDS trued up in one month', () => {
    const r = computePayslip(baseInput({ fy: 2082, extras: { isFinalSettlement: true, leaveEncashDays: '30', leaveEncashDivisor: 30, recoverLoansInFull: true },
      ytd: { taxable: '544000', tdsPaid: '10000', retirementEmployee: '35200', retirementEmployer: '64000', festivalPaid: true },
      loans: [{ id: 'L1', emi: '5000', remaining: '12000' }] }));
    expect(line(r, 'LEAVE_ENCASH')).toBe('40000.00');
    expect(line(r, 'LOAN')).toBe('12000.00');
    expect(r.tax.remainingMonths).toBe(1);
    expect(r.tax.projectedAnnualTaxable).toBe('652000.00');
  });
  it('minimum wage flag (Notice 2082): basic 12,000 flagged; 12,170 + 7,380 not flagged', () => {
    expect(computePayslip(baseInput({ base: { BASIC: '12000', DA: '7550' } })).flags).toContain('BelowMinimumWage');
    expect(computePayslip(baseInput({ base: { BASIC: '12170', DA: '7380' } })).flags).not.toContain('BelowMinimumWage');
    expect(computePayslip(baseInput({ base: { BASIC: '12000' }, policy: { ...baseInput().policy, fullTime: false } })).flags).not.toContain('BelowMinimumWage');
  });
});

describe('statutory regimes and deductions', () => {
  it('PF regime: 10% + 10% + gratuity 8.33%, no SSF lines', () => {
    const r = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'pf' } }));
    expect(line(r, 'PF_EE')).toBe('4000.00');
    expect(line(r, 'PF_ER')).toBe('4000.00');
    expect(line(r, 'GRATUITY')).toBe('3332.00');
    expect(line(r, 'SSF_EE')).toBeUndefined();
    expect(r.employerStatutory).toBe('7332.00');
  });
  it('regime none pays SST 1% (no waiver) and has no retirement lines', () => {
    const r = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'none' } }));
    expect(r.employeeStatutory).toBe('0.00');
    expect(r.tax.bands[0]).toMatchObject({ waived: false });
    expect(D(r.tax.annualTax).gt(0)).toBe(true);
  });
  it('female rebate 10% for a resident woman filing individually', () => {
    const m = computePayslip(baseInput({ base: { BASIC: '100000', ALLOWANCE: '50000' } }));
    const f = computePayslip(baseInput({ base: { BASIC: '100000', ALLOWANCE: '50000' }, employee: { ...baseInput().employee, gender: 'female' } }));
    expect(D(f.tax.femaleRebate).toFixed(2)).toBe(D(m.tax.grossTax).times('0.10').toFixed(2));
    expect(D(f.tds).lt(D(m.tds))).toBe(true);
  });
  it('non-resident: flat 25% with no deductions', () => {
    const r = computePayslip(baseInput({ employee: { ...baseInput().employee, isResident: false } }));
    expect(r.tax.status).toBe('non_resident');
    expect(r.tax.annualTax).toBe(D(r.tax.projectedAnnualTaxable).times('0.25').toFixed(2));
  });
  it('insurance caps, remote area, disability and medical credit apply', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '100000', ALLOWANCE: '60000' }, employee: { ...baseInput().employee, remoteAreaClass: 'A', disability: true },
      declaration: { lifePremium: '90000', healthPremium: '50000', citMonthly: '0', medicalCost: '20000', otherRetirementAnnual: '0' } }));
    expect(r.tax.insuranceDeduction).toBe('60000.00');
    expect(r.tax.remoteAreaDeduction).toBe('50000.00');
    expect(r.tax.disabilityDeduction).toBe('500000.00');
    expect(r.tax.medicalCredit).toBe('1500.00');
  });
  it('loan EMI is capped at available net and reported per loan', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '20000' }, loans: [{ id: 'A', emi: '5000', remaining: '30000' }, { id: 'B', emi: '20000', remaining: '20000' }] }));
    expect(r.loanInstallments[0]).toEqual({ loanId: 'A', amount: '5000.00' });
    expect(r.flags).toContain('LoanCapped');
    expect(D(r.net).gte(0)).toBe(true);
  });
  it('CIT counts toward the retirement deduction and is deducted', () => {
    const r = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'none' }, declaration: { ...baseInput().declaration, citMonthly: '5000' } }));
    expect(line(r, 'CIT')).toBe('5000.00');
    expect(r.tax.retirementDeduction).toBe('60000.00');
  });
});

describe('component graph', () => {
  it('formula components evaluate in dependency order and conditions skip lines', () => {
    const comps = [...DEFAULT_COMPONENTS.filter((c) => c.code !== 'HRA'),
      { code: 'HRA', type: 'earning' as const, nameEn: 'HRA', formula: 'BASIC * 0.2', condition: 'BASIC > 10000', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 30 },
      { code: 'TRANSPORT', type: 'earning' as const, nameEn: 'Transport', formula: 'HRA / 2', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 5 },
    ];
    const r = computePayslip(baseInput({ components: comps }));
    expect(line(r, 'HRA')).toBe('8000.00');
    expect(line(r, 'TRANSPORT')).toBe('4000.00');
    const skipped = computePayslip(baseInput({ components: comps, base: { BASIC: '9000' } }));
    expect(line(skipped, 'HRA')).toBeUndefined();
    expect(line(skipped, 'TRANSPORT')).toBeUndefined();
  });
  it('detects formula cycles', () => {
    const comps = [...DEFAULT_COMPONENTS,
      { code: 'X', type: 'earning' as const, nameEn: 'X', formula: 'Y + 1', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 1 },
      { code: 'Y', type: 'earning' as const, nameEn: 'Y', formula: 'X + 1', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 2 }];
    expect(() => computePayslip(baseInput({ components: comps }))).toThrow(/cycle/);
  });
  it('one-time inputs add to lines and are not projected as recurring income', () => {
    const r = computePayslip(baseInput({ inputs: [{ code: 'BONUS', amount: '50000' }, { code: 'ABSENCE', amount: '1290.32' }] }));
    expect(line(r, 'BONUS')).toBe('50000.00');
    expect(line(r, 'ABSENCE')).toBe('1290.32');
    expect(r.tax.recurringTaxable).toBe('68000.00');
    expect(r.tax.monthTaxable).toBe('118000.00');
  });
  it('sum of lines equals totals and the rounding line keeps net whole', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '33333.33', ALLOWANCE: '0.01' } }));
    const earn = r.lines.filter((l) => l.type === 'earning').reduce((a, l) => a.plus(l.amount), D(0));
    const ded = r.lines.filter((l) => l.type === 'deduction').reduce((a, l) => a.plus(l.amount), D(0));
    expect(earn.minus(ded).toFixed(2)).toBe(r.net);
    expect(D(r.net).mod(1).isZero()).toBe(true);
    expect(r.lines.some((l) => l.code === 'ROUNDING')).toBe(true);
  });
  it('TDS projection trues up against YTD', () => {
    const r = computePayslip(baseInput({ fy: 2082, period: { bsYear: 2082, bsMonth: 9, daysInMonth: 30, fiscalYearStart: 2082, monthIndexInFy: 6 },
      ytd: { taxable: '340000', tdsPaid: '7000', retirementEmployee: '22000', retirementEmployer: '40000', festivalPaid: true }, declaration: { ...baseInput().declaration, lifePremium: '30000' } }));
    expect(r.tax.remainingMonths).toBe(7);
    // 340,000 + 68,000 × 7 = 816,000 projected; retirement 62,000 + 12,400 × 7 = 148,800 → taxable 637,200 → tax 13,720 − 7,000 paid = 6,720 / 7 = 960
    expect(r.tax.projectedAnnualTaxable).toBe('816000.00');
    expect(r.tax.annualTax).toBe('13720.00');
    expect(r.tds).toBe('960.00');
  });
});
