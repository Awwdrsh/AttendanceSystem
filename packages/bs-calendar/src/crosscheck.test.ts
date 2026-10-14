import { describe, expect, it } from 'vitest';
import DateConverter from '@remotemerge/nepali-date-converter';
import { adToBs, allSupportedDays, bsToAd, isProjectedBsYear, LIBRARY_KNOWN_ERRORS, PROJECTED_BS_YEARS, supportedBsYears } from './index.js';

/**
 * plan §8.4 / §13.11: the calendar table is cross-checked against an independent library. Every day of a published
 * (non-projected) BS year must agree in both directions. Projected years are allowed to differ and are reported.
 */
describe('cross-check against @remotemerge/nepali-date-converter', () => {
  it('agrees on every day of every published BS year, in both directions', () => {
    const mismatches: string[] = [];
    for (const { bs, ad } of allSupportedDays()) {
      if (isProjectedBsYear(bs.year) || LIBRARY_KNOWN_ERRORS.includes(bs.year) || LIBRARY_KNOWN_ERRORS.includes(bs.year + 1) && bs.month === 12 && bs.day > 28) continue;
      const lib = new DateConverter(`${bs.year}-${String(bs.month).padStart(2, '0')}-${String(bs.day).padStart(2, '0')}`).toAd();
      const libAd = `${lib.year}-${String(lib.month).padStart(2, '0')}-${String(lib.date).padStart(2, '0')}`;
      if (libAd !== ad) mismatches.push(`BS ${bs.year}-${bs.month}-${bs.day}: table ${ad}, library ${libAd}`);
      if (LIBRARY_KNOWN_ERRORS.some((y) => ad >= bsToAd({ year: y, month: 1, day: 1 }) && ad <= bsToAd({ year: y, month: 2, day: 2 }))) continue;
      const back = new DateConverter(ad).toBs();
      const ours = adToBs(ad);
      if (back.year !== ours.year || back.month !== ours.month || back.date !== ours.day) {
        mismatches.push(`AD ${ad}: table ${ours.year}-${ours.month}-${ours.day}, library ${back.year}-${back.month}-${back.date}`);
      }
    }
    expect(mismatches.slice(0, 10)).toEqual([]);
  });

  it('projected years are declared and are the only years allowed to diverge from the library', () => {
    const { max } = supportedBsYears();
    expect(PROJECTED_BS_YEARS).toContain(max);
    let diverging = 0;
    for (const { bs, ad } of allSupportedDays()) {
      if (!isProjectedBsYear(bs.year)) continue;
      try {
        const lib = new DateConverter(`${bs.year}-${String(bs.month).padStart(2, '0')}-${String(bs.day).padStart(2, '0')}`).toAd();
        if (`${lib.year}-${String(lib.month).padStart(2, '0')}-${String(lib.date).padStart(2, '0')}` !== ad) diverging++;
      } catch { diverging++; }
    }
    // Known: the library projects Jestha 2084 with 32 days; our table (plan §6.10) uses 31. Re-verify each Chaitra.
    expect(diverging).toBeGreaterThan(0);
  });
});

describe('fixed dates confirmed against published Nepali calendars', () => {
  it('matches dates read from Nepali Patro', () => {
    expect(bsToAd({ year: 2000, month: 1, day: 1 })).toBe('1943-04-14');
    expect(bsToAd({ year: 2062, month: 1, day: 1 })).toBe('2005-04-14');
    expect(bsToAd({ year: 2062, month: 1, day: 31 })).toBe('2005-05-14');
    expect(bsToAd({ year: 2062, month: 2, day: 1 })).toBe('2005-05-15');
    expect(adToBs('2026-10-14')).toEqual({ year: 2083, month: 6, day: 28 });
    expect(adToBs('2026-10-17')).toEqual({ year: 2083, month: 6, day: 31 });
    expect(bsToAd({ year: 2084, month: 1, day: 1 })).toBe('2027-04-14');
  });
});
