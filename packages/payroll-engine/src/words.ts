import { Decimal, D } from './decimal.js';

/** Amount in words, English, Indian/Nepali grouping (lakh, crore). */
export function amountInWordsEn(v: Decimal.Value): string {
  const signed = D(v).toDecimalPlaces(2);
  const d = signed.abs();
  const rupees = d.floor();
  const paisa = d.minus(rupees).times(100).toDecimalPlaces(0).toNumber();
  const words = rupees.isZero() ? 'Zero' : groupEn(rupees);
  return `${signed.isNegative() && !signed.isZero() ? 'Minus ' : ''}${words} Rupees${paisa ? ` and ${twoDigitsEn(paisa)} Paisa` : ''} Only`;
}

const ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen'];
const TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety'];

function twoDigitsEn(n: number): string {
  if (n < 20) return ONES[n]!;
  return `${TENS[Math.floor(n / 10)]}${n % 10 ? '-' + ONES[n % 10] : ''}`;
}
function threeDigitsEn(n: number): string {
  const h = Math.floor(n / 100); const r = n % 100;
  return [h ? `${ONES[h]} Hundred` : '', r ? twoDigitsEn(r) : ''].filter(Boolean).join(' ');
}
function groupEn(n: Decimal): string {
  // crore (1e7), lakh (1e5), thousand (1e3), hundreds
  const parts: string[] = [];
  let rest = n;
  const crore = rest.div(10_000_000).floor(); rest = rest.minus(crore.times(10_000_000));
  if (crore.gt(0)) parts.push(`${groupEn(crore)} Crore`);
  const lakh = rest.div(100_000).floor().toNumber(); rest = rest.minus(lakh * 100_000);
  if (lakh) parts.push(`${twoDigitsEn(lakh)} Lakh`);
  const thousand = rest.div(1000).floor().toNumber(); rest = rest.minus(thousand * 1000);
  if (thousand) parts.push(`${twoDigitsEn(thousand)} Thousand`);
  const h = rest.toNumber();
  if (h) parts.push(threeDigitsEn(h));
  return parts.join(' ');
}

const NE = [
  'शून्य', 'एक', 'दुई', 'तीन', 'चार', 'पाँच', 'छ', 'सात', 'आठ', 'नौ', 'दश', 'एघार', 'बाह्र', 'तेह्र', 'चौध', 'पन्ध्र', 'सोह्र', 'सत्र', 'अठार', 'उन्नाइस',
  'बीस', 'एक्काइस', 'बाइस', 'तेइस', 'चौबीस', 'पच्चीस', 'छब्बीस', 'सत्ताइस', 'अट्ठाइस', 'उनन्तीस', 'तीस', 'एकतीस', 'बत्तीस', 'तेत्तीस', 'चौंतीस', 'पैंतीस', 'छत्तीस', 'सैंतीस', 'अठतीस', 'उनन्चालीस',
  'चालीस', 'एकचालीस', 'बयालीस', 'त्रिचालीस', 'चवालीस', 'पैंतालीस', 'छयालीस', 'सच्चालीस', 'अठचालीस', 'उनन्चास', 'पचास', 'एकाउन्न', 'बाउन्न', 'त्रिपन्न', 'चवन्न', 'पचपन्न', 'छपन्न', 'सन्ताउन्न', 'अन्ठाउन्न', 'उनन्साठी',
  'साठी', 'एकसट्ठी', 'बयसट्ठी', 'त्रिसट्ठी', 'चौंसट्ठी', 'पैंसट्ठी', 'छयसट्ठी', 'सतसट्ठी', 'अठसट्ठी', 'उनन्सत्तरी', 'सत्तरी', 'एकहत्तर', 'बहत्तर', 'त्रिहत्तर', 'चौहत्तर', 'पचहत्तर', 'छयहत्तर', 'सतहत्तर', 'अठहत्तर', 'उनासी',
  'असी', 'एकासी', 'बयासी', 'त्रियासी', 'चौरासी', 'पचासी', 'छयासी', 'सतासी', 'अठासी', 'उनान्नब्बे', 'नब्बे', 'एकान्नब्बे', 'बयान्नब्बे', 'त्रियान्नब्बे', 'चौरान्नब्बे', 'पन्चान्नब्बे', 'छयान्नब्बे', 'सन्तान्नब्बे', 'अन्ठान्नब्बे', 'उनान्सय',
];

function groupNe(n: Decimal): string {
  const parts: string[] = [];
  let rest = n;
  const crore = rest.div(10_000_000).floor(); rest = rest.minus(crore.times(10_000_000));
  if (crore.gt(0)) parts.push(`${groupNe(crore)} करोड`);
  const lakh = rest.div(100_000).floor().toNumber(); rest = rest.minus(lakh * 100_000);
  if (lakh) parts.push(`${NE[lakh]} लाख`);
  const thousand = rest.div(1000).floor().toNumber(); rest = rest.minus(thousand * 1000);
  if (thousand) parts.push(`${NE[thousand]} हजार`);
  const h = rest.toNumber();
  const hundred = Math.floor(h / 100); const r = h % 100;
  if (hundred) parts.push(`${NE[hundred]} सय`);
  if (r) parts.push(NE[r]!);
  return parts.join(' ');
}

/** Amount in words, Nepali (Devanagari), lakh/crore grouping. */
export function amountInWordsNe(v: Decimal.Value): string {
  const signed = D(v).toDecimalPlaces(2);
  const d = signed.abs();
  const rupees = d.floor();
  const paisa = d.minus(rupees).times(100).toDecimalPlaces(0).toNumber();
  const words = rupees.isZero() ? NE[0]! : groupNe(rupees);
  return `${signed.isNegative() && !signed.isZero() ? 'ऋण ' : ''}${words} रुपैयाँ${paisa ? ` ${NE[paisa]} पैसा` : ''} मात्र`;
}

const DEVANAGARI = ['०', '१', '२', '३', '४', '५', '६', '७', '८', '९'];
export const toDevanagariDigits = (s: string): string => s.replace(/[0-9]/g, (c) => DEVANAGARI[Number(c)]!);

/** 'NPR 1,23,456.00' style grouping (Indian/Nepali). */
export function formatNpr(v: Decimal.Value, numerals: 'latin' | 'devanagari' = 'latin'): string {
  const d = D(v).toDecimalPlaces(2);
  const neg = d.isNegative() && !d.isZero();
  const [int, frac] = d.abs().toFixed(2).split('.') as [string, string];
  const last3 = int.slice(-3); let rest = int.slice(0, -3);
  const groups: string[] = [];
  while (rest.length > 2) { groups.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
  if (rest) groups.unshift(rest);
  const s = `${neg ? '-' : ''}${[...groups, last3].join(',')}.${frac}`;
  return numerals === 'devanagari' ? toDevanagariDigits(s) : s;
}
