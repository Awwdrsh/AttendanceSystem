import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { computePayslip } from './compute.js';
import { D } from './decimal.js';
import { baseInput } from './fixtures.js';

const money = (max: number) => fc.integer({ min: 0, max: max * 100 }).map((p) => (p / 100).toFixed(2));

const scenario = fc.record({
  basic: fc.integer({ min: 10_000, max: 500_000 }).map(String),
  allowance: money(200_000),
  paidDays: fc.integer({ min: 0, max: 31 }),
  otHours: fc.integer({ min: 0, max: 40 }).map(String),
  regime: fc.constantFrom('ssf', 'pf', 'none'),
  gender: fc.constantFrom('male', 'female'),
  marital: fc.constantFrom('single', 'couple'),
  fy: fc.constantFrom(2082, 2083),
  monthIndex: fc.integer({ min: 1, max: 12 }),
  life: money(100_000),
  loanEmi: money(20_000),
});

type Scenario = { basic: string; allowance: string; paidDays: number; otHours: string; regime: 'ssf' | 'pf' | 'none'; gender: 'male' | 'female'; marital: 'single' | 'couple'; fy: number; monthIndex: number; life: string; loanEmi: string };
const build = (x: Scenario) => {
  return baseInput({
    fy: x.fy, period: { bsYear: x.fy, bsMonth: ((x.monthIndex + 2) % 12) + 1, daysInMonth: 31, fiscalYearStart: x.fy, monthIndexInFy: x.monthIndex },
    base: { BASIC: x.basic, ALLOWANCE: x.allowance }, attendance: { paidDays: String(x.paidDays), lopDays: String(31 - x.paidDays), otHours: x.otHours },
    employee: { ...baseInput().employee, regime: x.regime, gender: x.gender, maritalStatus: x.marital },
    declaration: { ...baseInput().declaration, lifePremium: x.life }, loans: D(x.loanEmi).gt(0) ? [{ id: 'L', emi: x.loanEmi, remaining: '999999' }] : [],
  });
};

describe('payroll engine invariants (plan §11.11)', () => {
  it('Σ earnings − Σ deductions = net, gross ≥ net ≥ 0, employer contributions separate', () => {
    fc.assert(fc.property(scenario, (s) => {
      const r = computePayslip(build(s));
      const earn = r.lines.filter((l) => l.type === 'earning').reduce((a, l) => a.plus(l.amount), D(0));
      const ded = r.lines.filter((l) => l.type === 'deduction').reduce((a, l) => a.plus(l.amount), D(0));
      expect(earn.minus(ded).toFixed(2)).toBe(r.net);
      expect(D(r.gross).gte(r.net)).toBe(true);
      expect(D(r.net).gte(0)).toBe(true);
      expect(r.lines.every((l) => D(l.amount).gte(0))).toBe(true);
    }), { numRuns: 300 });
  });
  it('recompute is idempotent', () => {
    fc.assert(fc.property(scenario, (s) => { expect(computePayslip(build(s))).toEqual(computePayslip(build(s))); }), { numRuns: 100 });
  });
  it('TDS is monotonic in income', () => {
    fc.assert(fc.property(scenario, fc.integer({ min: 1, max: 100_000 }), (s, extra) => {
      const low = computePayslip(build(s));
      const high = computePayslip(build({ ...s, basic: String(Number(s.basic) + extra) }));
      expect(D(high.tds).gte(low.tds)).toBe(true);
    }), { numRuns: 200 });
  });
  it('proration across a join/exit pair sums to the full month', () => {
    fc.assert(fc.property(fc.integer({ min: 1, max: 31 }), fc.integer({ min: 10_000, max: 300_000 }), (k, basic) => {
      const a = computePayslip(baseInput({ base: { BASIC: String(basic) }, attendance: { paidDays: String(k), lopDays: '0', otHours: '0' } }));
      const b = computePayslip(baseInput({ base: { BASIC: String(basic) }, attendance: { paidDays: String(31 - k), lopDays: '0', otHours: '0' } }));
      const total = D(a.lines.find((l) => l.code === 'BASIC')?.amount ?? 0).plus(b.lines.find((l) => l.code === 'BASIC')?.amount ?? 0);
      expect(total.minus(basic).abs().lte('0.01')).toBe(true);
    }), { numRuns: 100 });
  });
});
