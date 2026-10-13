import { describe, expect, it } from 'vitest';
import { D } from './decimal.js';
import { evaluateFormula, formulaReferences, parseFormula, validateFormula, type EvalEnv } from './formula.js';

const env: EvalEnv = {
  vars: { BASIC: D('40000'), DA: D('7380'), paid_days: D(22), days_in_bs_month: D(31), is_female: true, regime: 'ssf' },
  rule: (key, path) => (key === 'ssf.rates' && path === 'employee' ? D('0.11') : key === 'min_wage' && path === 'total' ? D('19550') : D(0)),
};
const ev = (s: string) => evaluateFormula(s, env);

describe('formula DSL (plan §11.2)', () => {
  it('arithmetic with precedence and decimals', () => {
    expect(String(ev('BASIC * 0.11'))).toBe('4400');
    expect(String(ev('1 + 2 * 3 - 4 / 8'))).toBe('6.5');
    expect(String(ev('-(BASIC + DA)'))).toBe('-47380');
    expect(String(ev('BASIC * paid_days / days_in_bs_month'))).toMatch(/^28387\.09677/);
  });
  it('functions', () => {
    expect(String(ev('min(BASIC, 12170, 99999)'))).toBe('12170');
    expect(String(ev('max(BASIC + DA, rule("min_wage", "total"))'))).toBe('47380');
    expect(String(ev('round(BASIC * paid_days / days_in_bs_month, 2)'))).toBe('28387.1');
    expect(String(ev('round(2.5)'))).toBe('3');
    expect(String(ev('floor(7.9) + ceil(7.1)'))).toBe('15');
    expect(String(ev('if(is_female and regime == "ssf", 1, 0)'))).toBe('1');
    expect(String(ev('if(not is_female or BASIC < 1000, 1, 0)'))).toBe('0');
    expect(String(ev('BASIC * rule("ssf.rates", "employee")'))).toBe('4400');
  });
  it('rejects unknown identifiers, functions, bad arity, division by zero and type errors', () => {
    expect(() => ev('BASIC + HRA')).toThrow(/unknown identifier HRA/);
    expect(() => ev('eval("x")')).toThrow(/unknown function/);
    expect(() => ev('round()')).toThrow(/expects/);
    expect(() => ev('BASIC / 0')).toThrow(/division by zero/);
    expect(() => ev('BASIC + is_female')).toThrow(/expected a number/);
    expect(() => ev('if(BASIC, 1, 2)')).toThrow(/expected a condition/);
    expect(() => parseFormula('BASIC +')).toThrow();
    expect(() => parseFormula('BASIC.toString()')).toThrow();
    expect(() => parseFormula('BASIC; DROP TABLE')).toThrow();
  });
  it('validateFormula reports problems for the rule builder', () => {
    expect(validateFormula('BASIC * 0.1', ['BASIC'])).toBeNull();
    expect(validateFormula('BASIC * paid_days / days_in_bs_month', ['BASIC'])).toBeNull();
    expect(validateFormula('BASIC * HRA', ['BASIC'])).toMatch(/unknown identifier HRA/);
    expect(validateFormula('foo(1)', [])).toMatch(/unknown function/);
    expect(validateFormula('(1 + ', [])).toBeTruthy();
  });
  it('formulaReferences lists component codes and variables', () => {
    expect([...formulaReferences('if(BASIC > 0, BASIC * 0.5 + DA, 0)')].sort()).toEqual(['BASIC', 'DA']);
  });
});
