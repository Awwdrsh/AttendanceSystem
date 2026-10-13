import { describe, expect, it } from 'vitest';
import { amountInWordsEn, amountInWordsNe, formatNpr, toDevanagariDigits } from './words.js';

describe('amount in words and NPR formatting (plan §13.7)', () => {
  it('English with lakh/crore grouping', () => {
    expect(amountInWordsEn('40000')).toBe('Forty Thousand Rupees Only');
    expect(amountInWordsEn('2163.46')).toBe('Two Thousand One Hundred Sixty-Three Rupees and Forty-Six Paisa Only');
    expect(amountInWordsEn('12345678.5')).toBe('One Crore Twenty-Three Lakh Forty-Five Thousand Six Hundred Seventy-Eight Rupees and Fifty Paisa Only');
    expect(amountInWordsEn('0')).toBe('Zero Rupees Only');
  });
  it('Nepali', () => {
    expect(amountInWordsNe('40000')).toBe('चालीस हजार रुपैयाँ मात्र');
    expect(amountInWordsNe('28387.10')).toBe('अट्ठाइस हजार तीन सय सतासी रुपैयाँ दश पैसा मात्र');
    expect(amountInWordsNe('1500000')).toBe('पन्ध्र लाख रुपैयाँ मात्र');
  });
  it('NPR grouping and numerals', () => {
    expect(formatNpr('123456.7')).toBe('1,23,456.70');
    expect(formatNpr('999')).toBe('999.00');
    expect(formatNpr('-1234.5')).toBe('-1,234.50');
    expect(formatNpr('123456.7', 'devanagari')).toBe('१,२३,४५६.७०');
    expect(toDevanagariDigits('2083-05-12')).toBe('२०८३-०५-१२');
  });
});
