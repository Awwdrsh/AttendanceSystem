import { DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083 } from '@hajiri/payroll-engine';
import type { Queryable } from '../../db/pool.js';

/** Statutory rule versions (plan §6, §6.13). Jurisdiction-wide, not per company. Idempotent on (rule_key, valid_from_ad). */
const RULES: Array<{ key: string; fromAd: string; toAd: string | null; fromBs: string; toBs: string | null; value: unknown; source: string; note?: string }> = [
  { key: 'tax.schedule', fromAd: '2025-07-17', toAd: '2026-07-16', fromBs: '2082-04-01', toBs: '2083-03-32', value: TAX_SCHEDULE_2082, source: 'https://lawalpine.com/blog/salary-tax-calculator-nepal-2082-83', note: 'Finance Act 2082: six bands, single and couple' },
  { key: 'tax.schedule', fromAd: '2026-07-17', toAd: null, fromBs: '2083-04-01', toBs: null, value: TAX_SCHEDULE_2083, source: 'https://frontline.com.np/blog/nepal-income-tax-slabs-fy-2083-84/', note: 'Finance Act 2083: single table; verify against the IRD circular (Gap G-1)' },
  { key: 'tax.deductions', fromAd: '2025-07-17', toAd: null, fromBs: '2082-04-01', toBs: null, value: DEFAULT_RULES['tax.deductions'], source: 'https://notarynepal.com/blog/tax-rate-in-nepal' },
  { key: 'ssf.rates', fromAd: '2019-07-17', toAd: null, fromBs: '2076-04-01', toBs: null, value: DEFAULT_RULES['ssf.rates'], source: 'https://pioneerlaw.com/?p=198' },
  { key: 'pf.rates', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: DEFAULT_RULES['pf.rates'], source: 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', note: 'Labour Act 2074 Sec 52-53' },
  { key: 'ot.multiplier', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: DEFAULT_RULES['ot.multiplier'], source: 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', note: 'Sec 31(1); weekly-off/holiday work may instead earn substitute leave (Sec 42)' },
  { key: 'festival.allowance', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: DEFAULT_RULES['festival.allowance'], source: 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', note: 'Sec 37' },
  { key: 'min_wage', fromAd: '2023-08-17', toAd: '2025-07-16', fromBs: '2080-05-01', toBs: '2082-03-32', value: { basic: '10820', dearness: '6480', total: '17300', daily: '667', hourly: '89', partTimeHourly: '95' }, source: 'https://pradhanlaw.com/publications/minimum-remuneration-notice-2082-2025-ad', note: 'superseded Notice 2080' },
  { key: 'min_wage', fromAd: '2025-07-17', toAd: null, fromBs: '2082-04-01', toBs: null, value: DEFAULT_RULES['min_wage'], source: 'https://thehimalayantimes.com/nepal/govt-hikes-workers-minimum-wage-to-rs-19550-per-month', note: 'Notice 2082' },
  { key: 'bonus.caps', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: { poolRate: '0.10', monthsCapLow: 8, monthsCapHigh: 6, lowPayThreshold: '39100', minWorkedFraction: '0.5', payWithinMonths: 8 }, source: 'https://nepallaws.com/Laws/bonus-act-2030/section-7-amount-to-be-obtained-for-bonus-and-its-assessment/' },
  { key: 'ssf.deposit_days', fromAd: '2019-07-17', toAd: '2025-07-29', fromBs: '2076-04-01', toBs: '2082-04-13', value: { days: 15 }, source: 'https://pioneerlaw.com/?p=1337' },
  { key: 'ssf.deposit_days', fromAd: '2025-07-30', toAd: null, fromBs: '2082-04-14', toBs: null, value: { days: 25, lateInterest: '0.10' }, source: 'https://nepallaws.com/Laws/the-contribution-based-social-security-act-2074/chapter-2-provision-related-to-contribution-based-social-security-scheme/section-4-contribution-to-be-deposited/' },
  { key: 'tds.deposit_days', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: { days: 25, lateInterest: '0.15', annualReturnBy: 'Ashwin end' }, source: 'https://lawalpine.com/blog/tds-in-nepal' },
  { key: 'payroll.proration', fromAd: '2017-09-04', toAd: null, fromBs: '2074-05-19', toBs: null, value: { default: 'bs_calendar_days', alternatives: ['fixed_30', '26_day'], hourlyDivisor: 208 }, source: 'plan.md §6.10/§11.3 (practice, not law)' },
];

export async function seedPayrollDefaults(db: Queryable, companyId: string): Promise<{ rules: number; components: number; structures: number }> {
  let rules = 0;
  for (const r of RULES) {
    const res = await db.query(
      `INSERT INTO rule_version (rule_key, jurisdiction, valid_from_ad, valid_to_ad, valid_from_bs, valid_to_bs, value, source_url, note)
       SELECT $1, 'NP', $2, $3, $4, $5, $6, $7, $8 WHERE NOT EXISTS (SELECT 1 FROM rule_version WHERE rule_key = $1 AND jurisdiction = 'NP' AND valid_from_ad = $2)`,
      [r.key, r.fromAd, r.toAd, r.fromBs, r.toBs, JSON.stringify(r.value), r.source, r.note ?? null]);
    rules += res.rowCount ?? 0;
  }
  let components = 0;
  for (const c of DEFAULT_COMPONENTS) {
    const res = await db.query(
      `INSERT INTO salary_component (company_id, code, name_en, name_ne, type, formula, condition, depends_on_payment_days, taxable, statutory, rounding, sort_order)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) ON CONFLICT (company_id, code) DO NOTHING`,
      [companyId, c.code, c.nameEn, c.nameNe ?? null, c.type, c.formula ?? null, c.condition ?? null, c.dependsOnPaymentDays, c.taxable, c.statutory ?? false, c.rounding, c.sortOrder]);
    components += res.rowCount ?? 0;
  }
  let structures = 0;
  const s = await db.query<{ id: string }>(
    `INSERT INTO salary_structure (company_id, code, name_en) VALUES ($1, 'STD', 'Standard structure') ON CONFLICT (company_id, code) DO NOTHING RETURNING id`, [companyId]);
  if (s.rows[0]) {
    await db.query(`INSERT INTO salary_structure_version (structure_id, version, components) VALUES ($1, 1, $2)`,
      [s.rows[0].id, JSON.stringify(DEFAULT_COMPONENTS.map((c) => ({ code: c.code })))]);
    structures = 1;
  }
  return { rules, components, structures };
}
