import { describe, expect, it } from 'vitest';
import { computePayslip, PayrollError } from './compute.js';
import { baseInput } from './fixtures.js';
import { amountInWordsEn, amountInWordsNe, formatNpr } from './words.js';

describe('amount bounds', () => {
  it('a line too large for numeric(14,2) is a PayrollError, not a database overflow later', () => {
    expect(() => computePayslip(baseInput({ base: { BASIC: '2000000000000', ALLOWANCE: '0' } }))).toThrow(PayrollError);
    expect(() => computePayslip(baseInput({ base: { BASIC: '2000000000000', ALLOWANCE: '0' } }))).toThrow(/too large/);
    expect(() => computePayslip(baseInput({ base: { BASIC: '999999', ALLOWANCE: '0' } }))).not.toThrow();
  });
});

describe('negative zero', () => {
  it('amounts that round to zero are never shown as negative', () => {
    expect(formatNpr('-0.004')).toBe('0.00');
    expect(formatNpr('-0')).toBe('0.00');
    expect(amountInWordsEn('-0.004')).toBe('Zero Rupees Only');
    expect(amountInWordsNe('-0.001')).not.toMatch(/^ऋण/);
    expect(formatNpr('-0.01')).toBe('-0.01');
    expect(amountInWordsEn('-1500.50')).toBe('Minus One Thousand Five Hundred Rupees and Fifty Paisa Only');
  });
});
