/** Nepal Standard Time is UTC+05:45 with no DST (plan §6.10), so a fixed offset is correct and index-friendly. */
export const NPT_OFFSET_MIN = 345;
export const MIN_MS = 60_000;
export const DAY_MIN = 1440;

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const HHMM_RE = /^(\d{2}):(\d{2})$/;

/** 'HH:MM' -> minutes since midnight. */
export function parseHHMM(s: string): number {
  const m = HHMM_RE.exec(s);
  if (!m) throw new RangeError(`Time must be HH:MM, got ${s}`);
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) throw new RangeError(`Time out of range: ${s}`);
  return h * 60 + mi;
}

/** Epoch ms of local (NPT) midnight for 'YYYY-MM-DD'. */
export function localMidnightMs(localDate: string): number {
  const m = DATE_RE.exec(localDate);
  if (!m) throw new RangeError(`Date must be YYYY-MM-DD, got ${localDate}`);
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (new Date(ms).toISOString().slice(0, 10) !== localDate) throw new RangeError(`Invalid date ${localDate}`);
  return ms - NPT_OFFSET_MIN * MIN_MS;
}

/** Epoch ms for 'HH:MM' on a local date, optionally shifted by whole days. */
export function localTimeMs(localDate: string, hhmm: string, dayOffset = 0): number {
  return localMidnightMs(localDate) + (parseHHMM(hhmm) + dayOffset * DAY_MIN) * MIN_MS;
}

/** Local (NPT) calendar date of an instant. */
export function localDateOf(epochMs: number): string {
  return new Date(epochMs + NPT_OFFSET_MIN * MIN_MS).toISOString().slice(0, 10);
}

/** Today's calendar date in Nepal (never the UTC date: between 00:00 and 05:45 NPT those differ). */
export function nptToday(now: number = Date.now()): string {
  return localDateOf(now);
}

/** Whole minutes from a to b (floor). Negative if b < a. */
export function minutesBetween(aMs: number, bMs: number): number {
  return Math.floor((bMs - aMs) / MIN_MS);
}

export function roundMinutes(min: number, step: number, direction: 'nearest' | 'down' | 'up'): number {
  if (step <= 0) return min;
  const q = min / step;
  const r = direction === 'down' ? Math.floor(q) : direction === 'up' ? Math.ceil(q) : Math.round(q);
  return r * step;
}
