import { describe, expect, it } from 'vitest';
import { bsMonthGrid, nextMonth, parseTyped, prevMonth } from './bs';

describe('BS month grid', () => {
  it('starts on Sunday, contains every day of Bhadra 2083 and marks Saturdays', () => {
    const grid = bsMonthGrid(2083, 5);
    expect(grid.length % 7).toBe(0);
    expect(grid[0]!.weekday).toBe(0);
    const inMonth = grid.filter((c) => c.inMonth);
    expect(inMonth).toHaveLength(31);
    expect(inMonth[11]!.ad).toBe('2026-08-28');
    expect(inMonth[12]!.saturday).toBe(true);
  });
  it('month navigation wraps years', () => {
    expect(prevMonth(2083, 1)).toEqual({ year: 2082, month: 12 });
    expect(nextMonth(2083, 12)).toEqual({ year: 2084, month: 1 });
  });
  it('typed fallback accepts both calendars and rejects impossible dates', () => {
    expect(parseTyped('2083-05-12', 'bs')).toBe('2026-08-28');
    expect(parseTyped('2026/08/28', 'ad')).toBe('2026-08-28');
    expect(parseTyped('2083-13-01', 'bs')).toBeNull();
    expect(parseTyped('2026-02-30', 'ad')).toBeNull();
    expect(parseTyped('nope', 'ad')).toBeNull();
  });
});
