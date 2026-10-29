import { describe, expect, it } from 'vitest';
import { supportedBsYears } from '@/lib/bs-calendar';
import { bsMonthGrid, holidayMap, nextMonth, parseTyped, prevMonth } from './bs';

describe('BS month grid', () => {
  it('starts on Sunday, contains every day of Bhadra 2083 and marks Saturdays', () => {
    const grid = bsMonthGrid(2083, 5);
    expect(grid.length % 7).toBe(0);
    expect(grid[0].weekday).toBe(0);
    const inMonth = grid.filter((c) => c.inMonth);
    expect(inMonth).toHaveLength(31);
    expect(inMonth[11].ad).toBe('2026-08-28');
    expect(inMonth[12].saturday).toBe(true);
  });
  it('month navigation wraps years', () => {
    expect(prevMonth(2083, 1)).toEqual({ year: 2082, month: 12 });
    expect(nextMonth(2083, 12)).toEqual({ year: 2084, month: 1 });
  });
  it('navigation never leaves the supported calendar', () => {
    const { min, max } = supportedBsYears();
    expect(prevMonth(min, 1)).toEqual({ year: min, month: 1 });
    expect(nextMonth(max, 12)).toEqual({ year: max, month: 12 });
    expect(prevMonth(2083, 1)).toEqual({ year: 2082, month: 12 });
  });
  it('typed fallback accepts both calendars and rejects impossible dates', () => {
    expect(parseTyped('2083-05-12', 'bs')).toBe('2026-08-28');
    expect(parseTyped('2026/08/28', 'ad')).toBe('2026-08-28');
    expect(parseTyped('2083-13-01', 'bs')).toBeNull();
    expect(parseTyped('2026-02-30', 'ad')).toBeNull();
    expect(parseTyped('nope', 'ad')).toBeNull();
  });
});

describe('holidayMap', () => {
  it('labels optional holidays and does not treat them as days off', () => {
    const m = holidayMap([
      { date: '2026-09-25', holidays: [{ name_en: 'Indra Jatra', optional: true }] },
      {
        date: '2026-10-21',
        holidays: [
          { name_en: 'Dashain', optional: false },
          { name_en: 'Extra', optional: true },
        ],
      },
      { date: '2026-10-22', holidays: [] },
    ]);
    expect(m.get('2026-09-25')).toEqual({ label: 'Indra Jatra (optional)', off: false });
    expect(m.get('2026-10-21')).toEqual({ label: 'Dashain, Extra (optional)', off: true });
    expect(m.has('2026-10-22')).toBe(false);
  });
});
