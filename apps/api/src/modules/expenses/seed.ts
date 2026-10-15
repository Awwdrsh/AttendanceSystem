import type { Queryable } from '../../db/pool.js';

export const DEFAULT_CATEGORIES: Array<[code: string, name: string, kind: 'amount' | 'mileage', receiptAbove: string | null, sort: number]> = [
  ['LOCAL_TRAVEL', 'Local travel', 'amount', '500', 10],
  ['MEALS', 'Meals', 'amount', '500', 20],
  ['FUEL', 'Fuel (own vehicle)', 'mileage', null, 30],
  ['MOBILE', 'Mobile & internet', 'amount', '0', 40],
  ['MEDICAL', 'Medical', 'amount', '0', 50],
  ['OFFICE', 'Office supplies', 'amount', '0', 60],
  ['OTHER', 'Other', 'amount', '0', 70],
];

/** Default daily allowance and lodging per night ('*' = every grade); international figures are US dollars. */
export const DEFAULT_TADA: Array<[destination: string, da: string, lodging: string]> = [
  ['valley', '1000', '2500'], ['city', '1500', '3000'], ['rural', '1200', '2000'], ['international', '50', '100'],
];

/** Idempotent per-company expense and travel defaults (categories, TADA rates, approval steps, policy). */
export async function seedExpenseDefaults(db: Queryable, companyId: string): Promise<{ expenseCategories: number }> {
  let n = 0;
  for (const [code, name, kind, receipt, sort] of DEFAULT_CATEGORIES) {
    const r = await db.query(
      `INSERT INTO expense_category (company_id, code, name, kind, receipt_required_above, sort_order) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (company_id, code) DO NOTHING`,
      [companyId, code, name, kind, receipt, sort]);
    n += r.rowCount ?? 0;
  }
  for (const [dest, da, lodging] of DEFAULT_TADA) {
    await db.query(`INSERT INTO tada_rate (company_id, grade, destination, da_rate, lodging_rate) VALUES ($1, '*', $2, $3, $4) ON CONFLICT (company_id, grade, destination) DO NOTHING`,
      [companyId, dest, da, lodging]);
  }
  for (const t of ['expense_claim', 'travel_request', 'travel_settlement']) {
    await db.query(`INSERT INTO workflow_definition (company_id, object_type, steps) VALUES ($1, $2, '[{"role":"manager"},{"role":"hr"}]') ON CONFLICT (company_id, object_type) DO NOTHING`, [companyId, t]);
  }
  await db.query(`INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'expense.policy', '{"mileageRatePerKm": "15", "limitMode": "block", "claimWindowDays": 90}') ON CONFLICT DO NOTHING`, [companyId]);
  return { expenseCategories: n };
}
