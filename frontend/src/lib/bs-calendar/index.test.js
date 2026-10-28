import { describe, expect, it } from 'vitest';
import {
  adToBs,
  bsToAd,
  bsMonthBounds,
  daysInBsMonth,
  fiscalYearOf,
  fiscalYear,
  formatBs,
  parseBs,
  supportedBsYears,
  weekdayOf,
  allSupportedDays,
  addBsDays,
  toDevanagariNumerals,
} from './index.js';
import { daysInBsYear } from './index.js';

// All expected values below come from plan.md §6.10 and §10.5.
describe('anchors from plan §6.10', () => {
  it('Baishakh 1, 2082 = 14 Apr 2025', () => expect(bsToAd({ year: 2082, month: 1, day: 1 })).toBe('2025-04-14'));
  it('Baishakh 1, 2083 = 14 Apr 2026', () => expect(bsToAd({ year: 2083, month: 1, day: 1 })).toBe('2026-04-14'));
  it('Chaitra 30, 2082 = 13 Apr 2026', () => expect(bsToAd({ year: 2082, month: 12, day: 30 })).toBe('2026-04-13'));
  it('FY 2082/83 = 17 Jul 2025 - 16 Jul 2026', () => {
    expect(fiscalYear(2082)).toMatchObject({ label: '2082/83', startAd: '2025-07-17', endAd: '2026-07-16' });
  });
  it('FY 2083/84 = 17 Jul 2026 - 16 Jul 2027, Ashadh has 32 days', () => {
    expect(fiscalYear(2083)).toMatchObject({ label: '2083/84', startAd: '2026-07-17', endAd: '2027-07-16' });
    expect(daysInBsMonth(2083, 3)).toBe(32);
    expect(daysInBsMonth(2084, 3)).toBe(32);
  });
  it('Shrawan 2083 = 17 Jul - 16 Aug 2026, 31 days (worked example E)', () => {
    expect(bsMonthBounds(2083, 4)).toEqual({ startAd: '2026-07-17', endAd: '2026-08-16', days: 31 });
  });
});

describe('worked example F dates (plan §10.6)', () => {
  it('2083-05-12 is Friday 28 Aug 2026; 05-13 Saturday; 05-14 Sunday', () => {
    expect(bsToAd(parseBs('2083-05-12'))).toBe('2026-08-28');
    expect(weekdayOf('2026-08-28')).toBe(5);
    expect(weekdayOf(bsToAd(parseBs('2083-05-13')))).toBe(6);
    expect(bsToAd(parseBs('2083-05-14'))).toBe('2026-08-30');
  });
});

describe('fiscal year resolution', () => {
  it('Bhadra 2083 belongs to FY 2083/84', () => expect(fiscalYearOf(parseBs('2083-05-12')).label).toBe('2083/84'));
  it('Ashadh 32, 2083 belongs to FY 2082/83', () => expect(fiscalYearOf(parseBs('2083-03-32')).label).toBe('2082/83'));
});

describe('round trip and range', () => {
  it('converts every supported day back and forth', () => {
    let count = 0;
    for (const { bs, ad } of allSupportedDays()) {
      expect(adToBs(ad)).toEqual(bs);
      count++;
    }
    const { min, max } = supportedBsYears();
    let expected = 0;
    for (let y = min; y <= max; y++) expected += daysInBsYear(y);
    expect(count).toBe(expected);
    expect(count).toBeGreaterThan(33_000);
  });
  it('rejects dates outside the table', () => {
    expect(() => adToBs('1943-04-13')).toThrow(RangeError);
    expect(() => bsToAd({ year: 1999, month: 12, day: 1 })).toThrow(RangeError);
    expect(() => bsToAd({ year: 2091, month: 1, day: 1 })).toThrow(RangeError);
    expect(() => parseBs('2083-02-32')).toThrow(RangeError);
    expect(supportedBsYears()).toEqual({ min: 2000, max: 2090 });
    expect(adToBs('1943-04-14')).toEqual({ year: 2000, month: 1, day: 1 });
  });
  it('addBsDays crosses month and year boundaries', () => {
    expect(addBsDays(parseBs('2082-12-30'), 1)).toEqual({ year: 2083, month: 1, day: 1 });
    expect(addBsDays(parseBs('2083-03-32'), 1)).toEqual({ year: 2083, month: 4, day: 1 });
  });
});

describe('formatting', () => {
  it('latin and devanagari numerals', () => {
    expect(formatBs({ year: 2083, month: 5, day: 1 })).toBe('2083-05-01');
    expect(formatBs({ year: 2083, month: 5, day: 1 }, { numerals: 'devanagari', separator: '/' })).toBe('२०८३/०५/०१');
    expect(toDevanagariNumerals('NPR 2,163')).toBe('NPR २,१६३');
  });
});
