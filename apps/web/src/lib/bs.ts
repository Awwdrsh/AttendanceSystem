import { adToBs, bsToAd, daysInBsMonth, supportedBsYears, weekdayOf, type BsDate } from '@hajiri/bs-calendar';

export interface BsCell { ad: string; bs: BsDate; weekday: number; saturday: boolean; inMonth: boolean }

/** Cells for a BS month grid, padded to full weeks starting Sunday (Nepal's week starts Sunday; Saturday is off). */
export function bsMonthGrid(year: number, month: number): BsCell[] {
  const days = daysInBsMonth(year, month);
  const cells: BsCell[] = [];
  const firstAd = bsToAd({ year, month, day: 1 });
  const lead = weekdayOf(firstAd);
  for (let i = lead; i > 0; i--) {
    const ad = shiftAd(firstAd, -i);
    const bs = safe(ad);
    if (bs) cells.push({ ad, bs, weekday: weekdayOf(ad), saturday: weekdayOf(ad) === 6, inMonth: false });
  }
  for (let day = 1; day <= days; day++) {
    const ad = bsToAd({ year, month, day });
    cells.push({ ad, bs: { year, month, day }, weekday: weekdayOf(ad), saturday: weekdayOf(ad) === 6, inMonth: true });
  }
  while (cells.length % 7 !== 0) {
    const last = cells[cells.length - 1]!;
    const ad = shiftAd(last.ad, 1);
    const bs = safe(ad);
    if (!bs) break;
    cells.push({ ad, bs, weekday: weekdayOf(ad), saturday: weekdayOf(ad) === 6, inMonth: false });
  }
  return cells;
}

export function shiftAd(ad: string, days: number): string {
  return new Date(new Date(`${ad}T00:00:00Z`).getTime() + days * 86_400_000).toISOString().slice(0, 10);
}

export function safe(ad: string): BsDate | null {
  try { return adToBs(ad); } catch { return null; }
}

export function prevMonth(y: number, m: number): { year: number; month: number } {
  return m === 1 ? { year: y - 1, month: 12 } : { year: y, month: m - 1 };
}
export function nextMonth(y: number, m: number): { year: number; month: number } {
  return m === 12 ? { year: y + 1, month: 1 } : { year: y, month: m + 1 };
}

export function inRange(y: number): boolean {
  const r = supportedBsYears();
  return y >= r.min && y <= r.max;
}

/** Parse typed input in either calendar; returns AD ISO or null. */
export function parseTyped(text: string, calendar: 'bs' | 'ad'): string | null {
  const m = /^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$/.exec(text);
  if (!m) return null;
  const y = Number(m[1]); const mo = Number(m[2]); const d = Number(m[3]);
  try {
    if (calendar === 'bs') return bsToAd({ year: y, month: mo, day: d });
    const iso = `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    if (new Date(`${iso}T00:00:00Z`).toISOString().slice(0, 10) !== iso) return null;
    return iso;
  } catch { return null; }
}
