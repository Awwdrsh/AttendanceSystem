import { adToBs, BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';

/** "28 Ashwin 2083" for an ISO (AD) date; falls back to the ISO date outside the calendar table. */
export function bsLabel(iso: string | Date | null | undefined): string {
  if (!iso) return '';
  const s = typeof iso === 'string' ? iso.slice(0, 10) : iso.toISOString().slice(0, 10);
  try { const b = adToBs(s); return `${b.day} ${BS_MONTH_NAMES_EN[b.month - 1]} ${b.year}`; } catch { return s; }
}

/** "28 Ashwin 2083" or "28 Ashwin to 2 Kartik 2083" for a range. */
export function bsRange(from: string, to: string): string {
  if (from === to) return bsLabel(from);
  return `${bsLabel(from)} to ${bsLabel(to)}`;
}

/** "Ashwin 2083" for a BS year and month. */
export function bsMonthName(year: number, month: number): string {
  return `${BS_MONTH_NAMES_EN[month - 1] ?? month} ${year}`;
}
