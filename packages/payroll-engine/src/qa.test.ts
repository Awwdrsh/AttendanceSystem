import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { computePayslip, PayrollError } from './compute.js';
import { D, Decimal } from './decimal.js';
import { DEFAULT_COMPONENTS } from './defaults.js';
import { evaluateFormula, FormulaError, parseFormula, validateFormula, type EvalEnv } from './formula.js';
import { baseInput } from './fixtures.js';

const line = (r: ReturnType<typeof computePayslip>, code: string) => r.lines.find((l) => l.code === code)?.amount;
const env: EvalEnv = { vars: { BASIC: D(40000), paid_days: D(31), regime: 'ssf', is_female: false }, rule: (k) => (k === 'x' ? D(1) : 'text') };

describe('formula DSL safety', () => {
  it('only ever throws FormulaError, for random input strings', () => {
    fc.assert(fc.property(fc.string({ maxLength: 120 }), (src) => {
      try { evaluateFormula(src, env); } catch (e) { expect(e).toBeInstanceOf(FormulaError); }
    }), { numRuns: 2000 });
  });
  it('only ever throws FormulaError, for random token soup', () => {
    const tok = fc.constantFrom('BASIC', 'paid_days', 'regime', 'is_female', '(', ')', ',', '+', '-', '*', '/', '<', '>=', '==', 'and', 'or', 'not', 'if', 'min', 'max', 'round', 'floor', 'rule', '"x"', '"ssf"', '0', '1', '2.5', '1e400', '-1e400', '999999999999999999999', 'constructor', '__proto__', 'toString', 'true');
    fc.assert(fc.property(fc.array(tok, { maxLength: 40 }), (parts) => {
      const src = parts.join(' ');
      try {
        const v = evaluateFormula(src, env);
        if (v instanceof Decimal) expect(v.isFinite()).toBe(true);
      } catch (e) { expect(e).toBeInstanceOf(FormulaError); }
    }), { numRuns: 3000 });
  });
  it('prototype names are not identifiers', () => {
    for (const name of ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf']) {
      expect(() => evaluateFormula(name, env)).toThrow(/unknown identifier/);
      expect(validateFormula(name, ['BASIC'])).toMatch(/unknown identifier/);
    }
  });
  it('deep nesting is rejected instead of overflowing the stack', () => {
    expect(() => parseFormula('('.repeat(500) + '1' + ')'.repeat(500))).toThrow(/nested too deeply/);
    expect(() => parseFormula('-'.repeat(500) + '1')).toThrow(/nested too deeply/);
    expect(() => parseFormula('not '.repeat(300) + 'true')).toThrow(/nested too deeply/);
    expect(() => parseFormula('min('.repeat(200) + '1' + ')'.repeat(200))).toThrow(/nested too deeply/);
    expect(() => parseFormula('1 + '.repeat(3000) + '1')).toThrow(/longer than/);
  });
  it('Decimal range errors and non-finite results become FormulaError', () => {
    expect(() => evaluateFormula('round(1.5, 2.5)', env)).toThrow(FormulaError);
    expect(() => evaluateFormula('round(1.5, 99)', env)).toThrow(FormulaError);
    expect(() => evaluateFormula('round(1.5, -1)', env)).toThrow(FormulaError);
    expect(() => evaluateFormula('1e9000000000000001 - 1e9000000000000001', env)).toThrow(FormulaError);
    expect(() => evaluateFormula('1e9000000000000001 * 2', env)).toThrow(/finite/);
    expect(() => evaluateFormula('rule("y") + 1', env)).toThrow(FormulaError);   // rule returns a non-numeric string
  });
  it('a component formula producing a non-finite amount fails the payslip loudly', () => {
    const comps = [...DEFAULT_COMPONENTS, { code: 'BAD', type: 'earning' as const, nameEn: 'Bad', formula: '1e9999999999999999', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 1 }];
    expect(() => computePayslip(baseInput({ components: comps }))).toThrow(/BAD|finite/);
    const nan = [...DEFAULT_COMPONENTS, { code: 'BAD', type: 'earning' as const, nameEn: 'Bad', formula: 'BASIC / 0', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 1 }];
    expect(() => computePayslip(baseInput({ components: nan }))).toThrow(FormulaError);
  });
  it('rule() with a non-numeric string value does not crash the lookup', () => {
    const r = computePayslip(baseInput({ rules: { ...baseInput().rules, 'label': 'bs_calendar_days' } }));
    expect(r.net).toBeDefined();
    const comps = [...DEFAULT_COMPONENTS, { code: 'X', type: 'earning' as const, nameEn: 'X', formula: 'if(rule("label") == "bs_calendar_days", 10, 0)', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa' as const, sortOrder: 1 }];
    expect(line(computePayslip(baseInput({ components: comps, rules: { ...baseInput().rules, 'label': 'bs_calendar_days' } })), 'X')).toBe('10.00');
  });
});

describe('money and totals', () => {
  it('gross and total deductions exclude the rounding adjustment; lines still sum to the whole-rupee net', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '33333.33', ALLOWANCE: '0.01' } }));
    expect(r.gross).toBe('33333.34');
    expect(D(r.net).mod(1).isZero()).toBe(true);
    const earn = r.lines.filter((l) => l.type === 'earning').reduce((a, l) => a.plus(l.amount), D(0));
    const ded = r.lines.filter((l) => l.type === 'deduction').reduce((a, l) => a.plus(l.amount), D(0));
    expect(earn.minus(ded).toFixed(2)).toBe(r.net);
    expect(D(r.gross).minus(r.totalDeductions).minus(r.net).abs().lt('0.5')).toBe(true);
  });
  it('non-statutory deductions are capped so net never goes negative (Sec 38)', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '20000' }, inputs: [{ code: 'ABSENCE', amount: '50000' }, { code: 'ADVANCE', amount: '5000' }] }));
    expect(D(r.net).gte(0)).toBe(true);
    expect(r.flags).toContain('DeductionCapped:ABSENCE');
    expect(r.flags).toContain('DeductionCapped:ADVANCE');
    expect(r.flags).not.toContain('NegativeNet');
    expect(line(r, 'SSF_EE')).toBe('2200.00');       // statutory share untouched
    expect(line(r, 'ADVANCE')).toBeUndefined();   // nothing left after ABSENCE took the rest
  });
  it('statutory shares are never capped; TDS is capped at the pay available and flagged for true-up', () => {
    const r = computePayslip(baseInput({ base: { BASIC: '20000' }, inputs: [{ code: 'ABSENCE', amount: '50000' }] }));
    expect(line(r, 'SSF_EE')).toBe('2200.00');
    expect(line(r, 'ABSENCE')).toBe('17800.00');
    const none = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'none' }, attendance: { paidDays: '0', lopDays: '31', otHours: '0' } }));
    expect(none.net).toBe('0.00');
    expect(none.tds).toBe('0.00');
    expect(none.flags).toContain('TdsCapped');
    expect(none.flags).not.toContain('NegativeNet');
  });
  it('recurring income for the projection uses full-month amounts even when this month is prorated', () => {
    const full = computePayslip(baseInput({ attendance: { paidDays: '31', lopDays: '0', otHours: '0' } }));
    const part = computePayslip(baseInput({ attendance: { paidDays: '10', lopDays: '21', otHours: '0' } }));
    expect(part.tax.recurringTaxable).toBe(full.tax.recurringTaxable);
    expect(part.tax.recurringTaxable).toBe('68000.00');
    const zero = computePayslip(baseInput({ attendance: { paidDays: '0', lopDays: '31', otHours: '0' } }));
    expect(zero.tax.recurringTaxable).toBe('68000.00');
  });
  it('one-time inputs on a recurring component are not projected', () => {
    const r = computePayslip(baseInput({ inputs: [{ code: 'BASIC', amount: '10000' }] }));
    expect(line(r, 'BASIC')).toBe('50000.00');
    expect(r.tax.recurringTaxable).toBe('68000.00');
  });
  it('recurringOverride drives the projection for one-off runs without salary lines', () => {
    const r = computePayslip(baseInput({ fy: 2082, base: {}, inputs: [{ code: 'ARREARS', amount: '50000' }],
      ytd: { taxable: '68000', tdsPaid: '1477', retirementEmployee: '4400', retirementEmployer: '8000', festivalPaid: false },
      period: { bsYear: 2082, bsMonth: 4, daysInMonth: 31, fiscalYearStart: 2082, monthIndexInFy: 1 },
      extras: { recurringOverride: { taxable: '68000', retirement: '12400' } } }));
    expect(r.tax.projectedAnnualTaxable).toBe(D(68000).plus(50000).plus(D(68000).times(11)).toFixed(2)); // festival projection needs a basic, which a one-off run has none of
    expect(D(r.tds).gt(0)).toBe(true);
  });
});

describe('tax edge cases', () => {
  const at = (taxable: number, fy: number, over: Partial<ReturnType<typeof baseInput>['employee']> = {}) => {
    // Construct income so projected taxable after deductions lands exactly on `taxable`: regime none, no deductions.
    const monthly = D(taxable).div(12);
    return computePayslip(baseInput({ fy, base: { ALLOWANCE: monthly.toFixed(6) }, employee: { ...baseInput().employee, regime: 'none', ...over } }));
  };
  it('FY 2083/84 band boundary at 1,000,000: all SST; one rupee more is taxed at 10%', () => {
    const edge = at(1_000_000, 2083);
    expect(edge.tax.taxableAfterDeductions).toBe('1000000.00');
    expect(edge.tax.annualTax).toBe('10000.00');
    const over = at(1_000_012, 2083);
    expect(D(over.tax.annualTax).minus(10000).toFixed(2)).toBe('1.20');
    expect(over.tax.bands[1]).toMatchObject({ rate: '0.10' });
  });
  it('FY 2082/83 single vs couple thresholds (500,000 / 600,000)', () => {
    expect(at(600_000, 2082).tax.annualTax).toBe('15000.00');                              // 5,000 SST + 100,000 × 10%
    expect(at(600_000, 2082, { maritalStatus: 'couple' }).tax.annualTax).toBe('6000.00');   // all SST in the couple band
    expect(at(600_000, 2083, { maritalStatus: 'couple' }).tax.status).toBe('all');
  });
  it('SST waiver: pf regime and CIT contributors are waived; regime none without contributions pays', () => {
    const pf = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'pf' } }));
    expect(pf.tax.bands[0]).toMatchObject({ waived: true });
    const cit = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'none' }, declaration: { ...baseInput().declaration, citMonthly: '1000' } }));
    expect(cit.tax.bands[0]).toMatchObject({ waived: true });
    const none = computePayslip(baseInput({ employee: { ...baseInput().employee, regime: 'none' } }));
    expect(none.tax.bands[0]).toMatchObject({ waived: false });
  });
  it('female rebate requires remuneration-only income; non-residents get no credits', () => {
    const base = { BASIC: '100000', ALLOWANCE: '50000' };
    const f = computePayslip(baseInput({ base, employee: { ...baseInput().employee, gender: 'female' } }));
    const other = computePayslip(baseInput({ base, employee: { ...baseInput().employee, gender: 'female', remunerationOnlyIncome: false } }));
    expect(D(f.tax.femaleRebate).gt(0)).toBe(true);
    expect(other.tax.femaleRebate).toBe('0.00');
    const nr = computePayslip(baseInput({ base, employee: { ...baseInput().employee, isResident: false, gender: 'female' }, declaration: { ...baseInput().declaration, medicalCost: '20000', lifePremium: '30000' } }));
    expect(nr.tax.medicalCredit).toBe('0.00');
    expect(nr.tax.femaleRebate).toBe('0.00');
    expect(nr.tax.annualTax).toBe(D(nr.tax.projectedAnnualTaxable).times('0.25').toFixed(2));
  });
  it('final month: TDS equals annual tax minus YTD, never negative', () => {
    const r = computePayslip(baseInput({ fy: 2082, period: { bsYear: 2083, bsMonth: 3, daysInMonth: 32, fiscalYearStart: 2082, monthIndexInFy: 12 },
      ytd: { taxable: '748000', tdsPaid: '10000', retirementEmployee: '48400', retirementEmployer: '88000', festivalPaid: true }, declaration: { ...baseInput().declaration, lifePremium: '30000' } }));
    expect(r.tax.remainingMonths).toBe(1);
    expect(r.tax.annualTax).toBe('13720.00');
    expect(r.tds).toBe('3720.00');
    const over = computePayslip(baseInput({ fy: 2082, period: { bsYear: 2083, bsMonth: 3, daysInMonth: 32, fiscalYearStart: 2082, monthIndexInFy: 12 },
      ytd: { taxable: '748000', tdsPaid: '99999', retirementEmployee: '48400', retirementEmployer: '88000', festivalPaid: true } }));
    expect(over.tds).toBe('0.00');
  });
  it('proration edge cases: day 1 join, last-day exit, 29/32-day months, zero days', () => {
    const b = (paid: number, days: number) => computePayslip(baseInput({ base: { BASIC: '31000' }, period: { bsYear: 2083, bsMonth: 8, daysInMonth: days, fiscalYearStart: 2083, monthIndexInFy: 5 }, attendance: { paidDays: String(paid), lopDays: String(days - paid), otHours: '0' } }));
    expect(line(b(29, 29), 'BASIC')).toBe('31000.00');
    expect(line(b(32, 32), 'BASIC')).toBe('31000.00');
    expect(line(b(1, 29), 'BASIC')).toBe('1068.97');
    expect(line(b(31, 32), 'BASIC')).toBe('30031.25');
    expect(line(b(0, 32), 'BASIC')).toBeUndefined();
    expect(b(0, 32).net).toBe('0.00');
    expect(b(0, 32).flags).not.toContain('NegativeNet');
  });
});
