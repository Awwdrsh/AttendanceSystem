import { adToBs, formatBs, toDevanagariNumerals, BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE, type BsDate } from '@hajiri/bs-calendar';
import type { Lang, Numerals } from '../i18n';

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
  if (opts.numerals === 'devanagari') s = toDevanagariNumerals(s);
  const cur = opts.currency === false ? '' : opts.numerals === 'devanagari' ? 'रु ' : 'NPR ';
  return `${neg ? '−' : ''}${cur}${s}`;
}

export function fmtNum(value: number | string, numerals: Numerals = 'latin'): string {
  const s = String(value);
  return numerals === 'devanagari' ? toDevanagariNumerals(s) : s;
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

/** "28 Aug 2026" */
export function fmtAd(ad: string | null | undefined): string {
  if (!ad) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ad);
  if (!m) return ad;
  return `${Number(m[3])} ${AD_MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

export function safeAdToBs(ad: string): BsDate | null {
  try { return adToBs(ad); } catch { return null; }
}

/** Primary BS, muted AD: { bs: '2083-05-12', bsLong: '12 Bhadra 2083', ad: '28 Aug 2026' } */
export function dualDate(ad: string | null | undefined, lang: Lang = 'en', numerals: Numerals = 'latin'): { bs: string; bsLong: string; ad: string } {
  if (!ad) return { bs: '—', bsLong: '—', ad: '—' };
  const iso = ad.slice(0, 10);
  const bs = safeAdToBs(iso);
  if (!bs) return { bs: iso, bsLong: iso, ad: fmtAd(iso) };
  const month = lang === 'ne' ? BS_MONTH_NAMES_NE[bs.month - 1] : BS_MONTH_NAMES_EN[bs.month - 1];
  return { bs: formatBs(bs, { numerals }), bsLong: `${fmtNum(bs.day, numerals)} ${month} ${fmtNum(bs.year, numerals)}`, ad: fmtAd(iso) };
}

export function bsMonthLabel(bsYear: number, bsMonth: number, lang: Lang = 'en', numerals: Numerals = 'latin'): string {
  const month = lang === 'ne' ? BS_MONTH_NAMES_NE[bsMonth - 1] : BS_MONTH_NAMES_EN[bsMonth - 1];
  return `${month} ${fmtNum(bsYear, numerals)}`;
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
