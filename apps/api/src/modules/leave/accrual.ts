import type { Queryable } from '../../db/pool.js';

export interface AccrualSummary { employees: number; postings: number; bsYear: number; bsMonth: number }

/**
 * Monthly leave accrual for a BS month (plan §10.6): home leave 1 day per 20 worked days, sick 1 per month (pro-rated
 * in the first year), caps, carry-forward. Idempotent per (employee, type, month) via ref_type/ref_id on the ledger.
 * TODO: implemented by the leave module.
 */
export async function runMonthlyAccrual(_db: Queryable, _companyId: string, bsYear: number, bsMonth: number): Promise<AccrualSummary> {
  return { employees: 0, postings: 0, bsYear, bsMonth };
}
