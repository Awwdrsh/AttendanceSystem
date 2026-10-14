import { adToBs, formatBs, BS_MONTH_NAMES_EN, type BsDate } from '@hajiri/bs-calendar';

type Lang = 'en';
type Numerals = 'latin';

export const NPT_OFFSET_MIN = 345;

/** Group with lakh/crore (12,34,567) or international (1,234,567) separators. */
export function groupDigits(intPart: string, style: 'lakh' | 'intl' = 'lakh'): string {
  if (style === 'intl') return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  if (intPart.length <= 3) return intPart;
  const last3 = intPart.slice(-3);
  const rest = intPart.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ',');
  return `${rest},${last3}`;
}

/** Money from a decimal string or number; never floats internally beyond display. */
export function fmtMoney(value: string | number | null | undefined, opts: { numerals?: Numerals; style?: 'lakh' | 'intl'; currency?: boolean; decimals?: number } = {}): string {
  if (value === null || value === undefined || value === '') return '—';
  const decimals = opts.decimals ?? 2;
  const n = typeof value === 'number' ? value.toFixed(decimals) : Number(value).toFixed(decimals);
  const neg = n.startsWith('-');
  const [i, f] = n.replace('-', '').split('.');
  let s = groupDigits(i ?? '0', opts.style ?? 'lakh') + (decimals > 0 ? `.${f ?? ''}` : '');
  const cur = opts.currency === false ? '' : 'NPR ';
  return `${neg ? '−' : ''}${cur}${s}`;
}

export function fmtNum(value: number | string, _numerals: Numerals = 'latin'): string {
  return typeof value === 'number' ? value.toLocaleString('en-US') : String(value);
}

/** 503 → "8 h 23 m"; 0 → "0 m". */
export function minutesToHm(min: number | null | undefined, numerals: Numerals = 'latin'): string {
  if (min === null || min === undefined) return '—';
  const h = Math.floor(min / 60);
  const m = min % 60;
  const s = h > 0 ? (m > 0 ? `${h} h ${m} m` : `${h} h`) : `${m} m`;
  return fmtNum(s, numerals);
}

/** HH:MM in Nepal time for an ISO instant or Date. */
export function fmtTime(iso: string | Date | null | undefined, numerals: Numerals = 'latin'): string {
  if (!iso) return '—';
  const ms = typeof iso === 'string' ? Date.parse(iso) : iso.getTime();
  if (Number.isNaN(ms)) return '—';
  const d = new Date(ms + NPT_OFFSET_MIN * 60_000);
  return fmtNum(d.toISOString().slice(11, 16), numerals);
}

export function todayNpt(): string {
  return new Date(Date.now() + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10);
}

export function addDays(ad: string, days: number): string {
  return new Date(new Date(`${ad}T00:00:00Z`).getTime() + days * 86_400_000).toISOString().slice(0, 10);
}

const AD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Gregorian "14 Oct 2026", for the small secondary date shown next to a Bikram Sambat date. */
export function fmtGregorian(ad: string | null | undefined): string {
  if (!ad) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ad);
  if (!m) return ad;
  return `${Number(m[3])} ${AD_MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

/**
 * The app's date format: Bikram Sambat, e.g. "28 Ashwin 2083". Input is the ISO (AD) date the API uses.
 * Dates outside the calendar table fall back to the Gregorian form.
 */
export function fmtAd(ad: string | null | undefined): string {
  if (!ad) return '—';
  const iso = ad.slice(0, 10);
  const bs = safeAdToBs(iso);
  if (!bs) return fmtGregorian(iso);
  return `${bs.day} ${BS_MONTH_NAMES_EN[bs.month - 1]} ${bs.year}`;
}

/** Same as fmtAd; reads better at call sites. */
export const fmtDate = fmtAd;

export function safeAdToBs(ad: string): BsDate | null {
  try { return adToBs(ad); } catch { return null; }
}

/** Primary BS, muted AD: { bs: '2083-05-12', bsLong: '12 Bhadra 2083', ad: '28 Aug 2026' } */
export function dualDate(ad: string | null | undefined, lang: Lang = 'en', numerals: Numerals = 'latin'): { bs: string; bsLong: string; ad: string } {
  if (!ad) return { bs: '—', bsLong: '—', ad: '—' };
  const iso = ad.slice(0, 10);
  const bs = safeAdToBs(iso);
  if (!bs) return { bs: iso, bsLong: iso, ad: fmtGregorian(iso) };
  void lang; void numerals;
  const month = BS_MONTH_NAMES_EN[bs.month - 1];
  return { bs: formatBs(bs), bsLong: `${bs.day} ${month} ${bs.year}`, ad: fmtGregorian(iso) };
}

export function bsMonthLabel(bsYear: number, bsMonth: number, lang: Lang = 'en', numerals: Numerals = 'latin'): string {
  void lang; void numerals;
  return `${BS_MONTH_NAMES_EN[bsMonth - 1]} ${bsYear}`;
}

/** Fiscal year label from an AD date: "2083/84". */
export function fiscalLabel(ad: string): string {
  const bs = safeAdToBs(ad);
  if (!bs) return '';
  const start = bs.month >= 4 ? bs.year : bs.year - 1;
  return `${start}/${String(start + 1).slice(-2)}`;
}

export function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `${Math.round(n)}%`;
}

export function relativeTime(iso: string | Date | null | undefined): string {
  if (!iso) return '—';
  const ms = typeof iso === 'string' ? Date.parse(iso) : iso.getTime();
  const diff = Date.now() - ms;
  const min = Math.round(diff / 60_000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

export function truncate(s: string | null | undefined, n = 40): string {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** "Tue, 27 Ashwin 2083" */
export function fmtDateLong(ad: string | null | undefined): string {
  if (!ad) return '—';
  const iso = ad.slice(0, 10);
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return ad;
  return `${WEEKDAYS[d.getUTCDay()]}, ${fmtAd(iso)}`;
}

/** "27 Ashwin" within the current BS year, otherwise "27 Ashwin 2082". */
export function fmtDateShort(ad: string | null | undefined): string {
  if (!ad) return '—';
  const iso = ad.slice(0, 10);
  const full = fmtAd(iso);
  const bs = safeAdToBs(iso); const now = safeAdToBs(todayNpt());
  return bs && now && bs.year === now.year ? full.replace(/ \d{4}$/, '') : full;
}

/** "Good morning" etc. in Nepal time. */
export function greeting(): string {
  const h = new Date(Date.now() + NPT_OFFSET_MIN * 60_000).getUTCHours();
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
}
