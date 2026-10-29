import { adToBs, bsToAd, daysInBsMonth, supportedBsYears, weekdayOf } from '@/lib/bs-calendar';

/** Cells for a BS month grid, padded to full weeks starting Sunday (Nepal's week starts Sunday; Saturday is off). */
export function bsMonthGrid(year, month) {
  const days = daysInBsMonth(year, month);
  const cells = [];
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
    const last = cells[cells.length - 1];
    const ad = shiftAd(last.ad, 1);
    const bs = safe(ad);
    if (!bs) break;
    cells.push({ ad, bs, weekday: weekdayOf(ad), saturday: weekdayOf(ad) === 6, inMonth: false });
  }
  return cells;
}

export function shiftAd(ad, days) {
  return new Date(new Date(`${ad}T00:00:00Z`).getTime() + days * 86_400_000).toISOString().slice(0, 10);
}

export function safe(ad) {
  try {
    return adToBs(ad);
  } catch {
    return null;
  }
}

/** Previous month, clamped to the supported calendar (the first supported month stays put). */
export function prevMonth(y, m) {
  const next = m === 1 ? { year: y - 1, month: 12 } : { year: y, month: m - 1 };
  return inRange(next.year) ? next : { year: y, month: m };
}
/** Next month, clamped to the supported calendar. */
export function nextMonth(y, m) {
  const next = m === 12 ? { year: y + 1, month: 1 } : { year: y, month: m + 1 };
  return inRange(next.year) ? next : { year: y, month: m };
}

export function inRange(y) {
  const r = supportedBsYears();
  return y >= r.min && y <= r.max;
}

/** Parse typed input in either calendar; returns AD ISO or null. */
export function parseTyped(text, calendar) {
  const m = /^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$/.exec(text);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  try {
    if (calendar === 'bs') return bsToAd({ year: y, month: mo, day: d });
    const iso = `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    if (new Date(`${iso}T00:00:00Z`).toISOString().slice(0, 10) !== iso) return null;
    return iso;
  } catch {
    return null;
  }
}

/** Holidays by AD date from GET /shifts/calendar days: `label` names them ("(optional)" after an optional one) and
 *  `off` is true only when one of them is a day off for everyone it applies to. An optional holiday is a working day
 *  unless the person takes it, so it must not look like a day off. */
export function holidayMap(days) {
  const out = new Map();
  for (const d of days ?? []) {
    if (!d.holidays?.length) continue;
    out.set(d.date, {
      label: d.holidays.map((h) => (h.optional ? `${h.name_en} (optional)` : h.name_en)).join(', '),
      off: d.holidays.some((h) => !h.optional),
    });
  }
  return out;
}
