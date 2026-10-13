import { describe, expect, it } from 'vitest';
import { adToBs, fiscalYear, fiscalYearOf, parseBs, supportedBsYears, daysInBsMonth } from './index.js';

describe('edge cases', () => {
  it('a Date object is read as a UTC calendar date regardless of host time zone', () => {
    // 2026-08-28T23:30:00Z is still 28 Aug in UTC even though it is 29 Aug in Nepal; callers must pass the NPT date string.
    expect(adToBs(new Date('2026-08-28T23:30:00Z'))).toEqual({ year: 2083, month: 5, day: 12 });
    expect(adToBs('2026-08-29')).toEqual({ year: 2083, month: 5, day: 13 });
  });
  it('fiscal year at the top of the table does not throw and marks the end as unknown', () => {
    const { max } = supportedBsYears();
    const fy = fiscalYearOf(parseBs(`${max}-05-01`));
    expect(fy.startYear).toBe(max);
    expect(fy.label).toBe(`${max}/${String(max + 1).slice(-2)}`);
    expect(fy.approximate).toBe(true);
    expect(fy.endAd).toBe('2028-07-15');
    expect(fiscalYear(max - 1)).toMatchObject({ approximate: false, endAd: '2027-07-16' });
  });
  it('fiscal year labels wrap correctly across a century', () => {
    expect(fiscalYear(2082).label).toBe('2082/83');
    expect(daysInBsMonth(2082, 12)).toBe(30);
  });
});
