import {
  ANCHOR, BS_MONTH_DAYS, BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE, FISCAL_YEAR_START_MONTH, DEFAULT_WEEKLY_OFF_DAY, PROJECTED_BS_YEARS, LIBRARY_KNOWN_ERRORS,
} from './data.js';

export { BS_MONTH_DAYS, BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE, FISCAL_YEAR_START_MONTH, DEFAULT_WEEKLY_OFF_DAY, PROJECTED_BS_YEARS, LIBRARY_KNOWN_ERRORS };

/** True when the BS year's month lengths are still a projection. */
export function isProjectedBsYear(year: number): boolean { return PROJECTED_BS_YEARS.includes(year); }

export interface BsDate { year: number; month: number; day: number }

const DAY_MS = 86_400_000;
const YEARS = Object.keys(BS_MONTH_DAYS).map(Number).sort((a, b) => a - b);
const MIN_YEAR = YEARS[0]!;
const MAX_YEAR = YEARS[YEARS.length - 1]!;

// Validate the table once at load: 12 months per year, 365/366 days, contiguous years.
for (let y = MIN_YEAR; y <= MAX_YEAR; y++) {
  const months = BS_MONTH_DAYS[y];
  if (!months) throw new Error(`BS_MONTH_DAYS missing year ${y} (table must be contiguous)`);
  if (months.length !== 12) throw new Error(`BS ${y}: expected 12 months, got ${months.length}`);
  const total = months.reduce((a, b) => a + b, 0);
  if (total !== 365 && total !== 366) throw new Error(`BS ${y}: year length ${total} is not 365/366`);
}

export function supportedBsYears(): { min: number; max: number } {
  return { min: MIN_YEAR, max: MAX_YEAR };
}

function assertYear(year: number): readonly number[] {
  const months = BS_MONTH_DAYS[year];
  if (!months) throw new RangeError(`BS year ${year} is outside the supported range ${MIN_YEAR}-${MAX_YEAR}`);
  return months;
}

export function daysInBsMonth(year: number, month: number): number {
  const months = assertYear(year);
  if (!Number.isInteger(month) || month < 1 || month > 12) throw new RangeError(`BS month ${month} out of range 1-12`);
  return months[month - 1]!;
}

export function daysInBsYear(year: number): number {
  return assertYear(year).reduce((a, b) => a + b, 0);
}

export function isValidBsDate(bs: BsDate): boolean {
  try {
    return Number.isInteger(bs.day) && bs.day >= 1 && bs.day <= daysInBsMonth(bs.year, bs.month);
  } catch {
    return false;
  }
}

/** Days from Baishakh 1 of MIN_YEAR to the given BS date. */
function ordinal(bs: BsDate): number {
  if (!isValidBsDate(bs)) throw new RangeError(`Invalid BS date ${formatBs(bs)}`);
  let n = 0;
  for (let y = MIN_YEAR; y < bs.year; y++) n += daysInBsYear(y);
  const months = BS_MONTH_DAYS[bs.year]!;
  for (let m = 1; m < bs.month; m++) n += months[m - 1]!;
  return n + (bs.day - 1);
}

const ANCHOR_ORDINAL = ordinal(ANCHOR.bs);

function toIsoDate(utcMs: number): string {
  return new Date(utcMs).toISOString().slice(0, 10);
}

export function parseIsoDate(ad: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ad);
  if (!m) throw new RangeError(`AD date must be YYYY-MM-DD, got ${ad}`);
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (toIsoDate(ms) !== ad) throw new RangeError(`Invalid AD date ${ad}`);
  return ms;
}

/** BS -> AD as 'YYYY-MM-DD'. */
export function bsToAd(bs: BsDate): string {
  return toIsoDate(ANCHOR.adUtcMs + (ordinal(bs) - ANCHOR_ORDINAL) * DAY_MS);
}

/** AD ('YYYY-MM-DD' or Date, interpreted as a UTC calendar date) -> BS. */
export function adToBs(ad: string | Date): BsDate {
  const ms = typeof ad === 'string' ? parseIsoDate(ad) : Date.UTC(ad.getUTCFullYear(), ad.getUTCMonth(), ad.getUTCDate());
  let n = Math.round((ms - ANCHOR.adUtcMs) / DAY_MS) + ANCHOR_ORDINAL;
  if (n < 0) throw new RangeError(`AD date ${toIsoDate(ms)} is before supported BS range`);
  let year = MIN_YEAR;
  for (;;) {
    if (year > MAX_YEAR) throw new RangeError(`AD date ${toIsoDate(ms)} is after supported BS range`);
    const len = daysInBsYear(year);
    if (n < len) break;
    n -= len;
    year++;
  }
  const months = BS_MONTH_DAYS[year]!;
  let month = 1;
  while (n >= months[month - 1]!) {
    n -= months[month - 1]!;
    month++;
  }
  return { year, month, day: n + 1 };
}

export function addBsDays(bs: BsDate, days: number): BsDate {
  return adToBs(toIsoDate(parseIsoDate(bsToAd(bs)) + days * DAY_MS));
}

/** First and last AD dates of a BS month, plus its length. Used for payroll periods and accrual jobs. */
export function bsMonthBounds(year: number, month: number): { startAd: string; endAd: string; days: number } {
  const days = daysInBsMonth(year, month);
  return { startAd: bsToAd({ year, month, day: 1 }), endAd: bsToAd({ year, month, day: days }), days };
}

export interface FiscalYear {
  startYear: number;
  label: string;
  startAd: string;
  endAd: string;
  /** true when the following BS year is not in the table yet: endAd is then startAd + 364 days (verify each Chaitra). */
  approximate: boolean;
}

/** Fiscal year containing a BS date, e.g. Bhadra 2083 -> FY 2083/84. */
export function fiscalYearOf(bs: BsDate): FiscalYear {
  const startYear = bs.month >= FISCAL_YEAR_START_MONTH ? bs.year : bs.year - 1;
  return fiscalYear(startYear);
}

export function fiscalYear(startYear: number): FiscalYear {
  const endYear = startYear + 1;
  const endMonth = FISCAL_YEAR_START_MONTH - 1;
  const startAd = bsToAd({ year: startYear, month: FISCAL_YEAR_START_MONTH, day: 1 });
  const known = Boolean(BS_MONTH_DAYS[endYear]);
  const endAd = known
    ? bsToAd({ year: endYear, month: endMonth, day: daysInBsMonth(endYear, endMonth) })
    : toIsoDate(parseIsoDate(startAd) + 364 * DAY_MS);
  return { startYear, label: `${startYear}/${String(endYear).slice(-2)}`, startAd, endAd, approximate: !known };
}

/** 0 = Sunday ... 6 = Saturday, for an AD 'YYYY-MM-DD'. */
export function weekdayOf(ad: string): number {
  return new Date(parseIsoDate(ad)).getUTCDay();
}

const DEVANAGARI_DIGITS = ['०', '१', '२', '३', '४', '५', '६', '७', '८', '९'] as const;

export function toDevanagariNumerals(s: string): string {
  return s.replace(/[0-9]/g, (d) => DEVANAGARI_DIGITS[Number(d)]!);
}

export function formatBs(bs: BsDate, opts: { numerals?: 'latin' | 'devanagari'; separator?: string } = {}): string {
  const sep = opts.separator ?? '-';
  const s = `${bs.year}${sep}${String(bs.month).padStart(2, '0')}${sep}${String(bs.day).padStart(2, '0')}`;
  return opts.numerals === 'devanagari' ? toDevanagariNumerals(s) : s;
}

export function parseBs(s: string): BsDate {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) throw new RangeError(`BS date must be YYYY-MM-DD, got ${s}`);
  const bs = { year: Number(m[1]), month: Number(m[2]), day: Number(m[3]) };
  if (!isValidBsDate(bs)) throw new RangeError(`Invalid BS date ${s}`);
  return bs;
}

/** Every supported day, for seeding the DB bs_calendar table. */
export function* allSupportedDays(): Generator<{ bs: BsDate; ad: string }> {
  for (let year = MIN_YEAR; year <= MAX_YEAR; year++) {
    for (let month = 1; month <= 12; month++) {
      const n = daysInBsMonth(year, month);
      for (let day = 1; day <= n; day++) {
        const bs = { year, month, day };
        yield { bs, ad: bsToAd(bs) };
      }
    }
  }
}
