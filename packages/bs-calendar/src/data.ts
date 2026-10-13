/**
 * BS month lengths per year. Source: plan.md §6.10, citing nepali-datetime calendar_bs.csv.
 * BS 2084 is a projection until the official patro is published; re-verify each Chaitra (plan §16.7).
 *
 * To extend: add a year here, make sure its 12 months sum to 365 or 366, and re-run the
 * cross-check test against @remotemerge/nepali-date-converter.
 */
export const BS_MONTH_DAYS: Readonly<Record<number, readonly number[]>> = {
  2082: [31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30],
  2083: [31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30],
  2084: [31, 31, 32, 31, 31, 30, 30, 30, 29, 30, 30, 30],
};

/** Anchor: Baishakh 1, 2082 = 14 April 2025 (plan §6.10, Merokalam). */
export const ANCHOR = { bs: { year: 2082, month: 1, day: 1 }, adUtcMs: Date.UTC(2025, 3, 14) } as const;

export const BS_MONTH_NAMES_EN = [
  'Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin',
  'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra',
] as const;

export const BS_MONTH_NAMES_NE = [
  'बैशाख', 'जेठ', 'असार', 'साउन', 'भदौ', 'असोज',
  'कात्तिक', 'मंसिर', 'पुस', 'माघ', 'फागुन', 'चैत',
] as const;

/** Fiscal year starts on Shrawan 1 (month 4) and ends on the last day of Ashadh (month 3). */
export const FISCAL_YEAR_START_MONTH = 4;

/** Saturday is the statutory weekly off; Sunday is a working day. JS getUTCDay(): 0 = Sunday, 6 = Saturday. */
export const DEFAULT_WEEKLY_OFF_DAY = 6;
