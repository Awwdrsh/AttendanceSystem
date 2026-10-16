import { D, type ComponentDef } from '@hajiri/payroll-engine';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';

/**
 * Payroll hooks for the compliance extras: opening balances in year-to-date, a CIT amount that starts from a chosen
 * month, and held pay (a held net is released once, through a later run as a non-taxable line, or outside payroll).
 */

export interface OpeningBalanceRow {
  id: string; company_id: string; employee_id: string; fiscal_year_start: number; gross: string; taxable: string; tds_paid: string;
  retirement_employee: string; retirement_employer: string; festival_paid: boolean; months: number; source: string; note: string | null;
}

export async function openingBalance(db: Queryable, employeeId: string, fiscalYearStart: number): Promise<OpeningBalanceRow | null> {
  const r = await db.query<OpeningBalanceRow>(
    `SELECT id, company_id, employee_id, fiscal_year_start, gross::text, taxable::text, tds_paid::text, retirement_employee::text, retirement_employer::text,
            festival_paid, months, source, note FROM payroll_opening_balance WHERE employee_id = $1 AND fiscal_year_start = $2`, [employeeId, fiscalYearStart]);
  return r.rows[0] ?? null;
}

/** The CIT for a month of the fiscal year: the amount applied from `cit_from_month` onward, the earlier amount before it. */
export function effectiveCit(d: { cit_monthly: string; cit_from_month?: number | null; cit_prev_monthly?: string | null } | undefined, monthIndexInFy: number): string {
  if (!d) return '0';
  if (d.cit_from_month && monthIndexInFy < d.cit_from_month) return D(d.cit_prev_monthly ?? 0).toFixed(2);
  return D(d.cit_monthly).toFixed(2);
}

// ---------------------------------------------------------------- held pay

export const RELEASE_COMPONENT: ComponentDef = {
  code: 'HELD_RELEASE', type: 'earning', nameEn: 'Released pay (held earlier)', nameNe: null, dependsOnPaymentDays: false, taxable: false, rounding: 'paisa', sortOrder: 98,
};
/** Runs that can carry a release; arrears and bonus runs replace their inputs and never do. */
const RELEASING_RUNS = new Set(['regular', 'supplementary', 'off_cycle', 'final_settlement']);

/** Start of a (re)compute: releases this run had claimed go back to the queue before they are claimed again. */
export async function releaseRunClaims(db: Queryable, runId: string): Promise<void> {
  await db.query(`UPDATE payroll_hold SET release_run_id = NULL WHERE release_run_id = $1 AND status = 'releasing'`, [runId]);
}

/** Claim the employee's held pay that HR released through payroll; returned as one-time inputs. */
export async function claimReleases(db: Queryable, companyId: string, runId: string, runType: string, employeeId: string): Promise<Array<{ code: string; amount: string; note: string }>> {
  if (!RELEASING_RUNS.has(runType)) return [];
  const r = await db.query<{ amount: string; bs_year: number; bs_month: number }>(
    `UPDATE payroll_hold h SET release_run_id = $3 FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
      WHERE h.run_id = r.id AND h.company_id = $1 AND h.employee_id = $2 AND h.status = 'releasing' AND h.release_mode = 'payroll'
        AND (h.release_run_id IS NULL OR h.release_run_id = $3) AND h.run_id <> $3
      RETURNING h.amount::text, p.bs_year, p.bs_month`, [companyId, employeeId, runId]);
  return r.rows.filter((x) => D(x.amount).gt(0)).map((x) => ({ code: 'HELD_RELEASE', amount: D(x.amount).toFixed(2), note: `pay held in ${x.bs_year}-${String(x.bs_month).padStart(2, '0')}` }));
}

export async function withReleaseComponent(db: Queryable, companyId: string, components: ComponentDef[]): Promise<ComponentDef[]> {
  if (components.some((c) => c.code === RELEASE_COMPONENT.code)) return components;
  const custom = await db.query<{ name_en: string }>('SELECT name_en FROM salary_component WHERE company_id = $1 AND code = $2 AND active', [companyId, RELEASE_COMPONENT.code]);
  return [...components, { ...RELEASE_COMPONENT, nameEn: custom.rows[0]?.name_en ?? RELEASE_COMPONENT.nameEn }];
}

/** After a (re)compute: held amounts follow the new net; a hold whose employee is no longer in the run is cancelled. */
export async function syncRunHolds(db: Queryable, runId: string): Promise<void> {
  await db.query(
    `UPDATE payroll_hold h SET amount = ps.net FROM payslip ps WHERE h.run_id = $1 AND h.status = 'held' AND ps.run_id = h.run_id AND ps.employee_id = h.employee_id`, [runId]);
  await db.query(
    `UPDATE payroll_hold h SET status = 'cancelled', release_note = 'employee no longer in the run'
      WHERE h.run_id = $1 AND h.status = 'held' AND NOT EXISTS (SELECT 1 FROM payslip ps WHERE ps.run_id = h.run_id AND ps.employee_id = h.employee_id)`, [runId]);
}

/** Lock: releases paid by this run are done. */
export async function settleRunReleases(db: Queryable, runId: string, actorId: string): Promise<void> {
  const r = await db.query<{ id: string }>(
    `UPDATE payroll_hold h SET status = 'released', released_at = now(), released_by = coalesce(released_by, $2)
      WHERE h.release_run_id = $1 AND h.status = 'releasing'
        AND EXISTS (SELECT 1 FROM payslip ps JOIN payslip_line l ON l.payslip_id = ps.id WHERE ps.run_id = $1 AND ps.employee_id = h.employee_id AND l.component_code = 'HELD_RELEASE')
      RETURNING h.id`, [runId, actorId]);
  for (const x of r.rows) await audit(db, { actorId, action: 'release', table: 'payroll_hold', rowId: x.id, after: { runId } });
  // A release claimed by a run that ended up without the line (employee left the run) waits for the next run.
  await db.query(`UPDATE payroll_hold SET release_run_id = NULL WHERE release_run_id = $1 AND status = 'releasing'`, [runId]);
}

export async function holdPreflight(db: Queryable, companyId: string, run: { id: string; run_type: string }, employeeIds: string[]): Promise<Array<{ code: string; message: string; count?: number; employees?: string[] }>> {
  const out: Array<{ code: string; message: string; count?: number; employees?: string[] }> = [];
  const held = await db.query<{ emp_code: string; amount: string }>(
    `SELECT e.emp_code, h.amount::text FROM payroll_hold h JOIN employee e ON e.id = h.employee_id WHERE h.run_id = $1 AND h.status = 'held' ORDER BY e.emp_code`, [run.id]);
  if (held.rows.length) out.push({ code: 'pay_held', message: 'Pay is on hold for these employees in this run: their payslip is calculated but the net is not paid', count: held.rows.length, employees: held.rows.map((h) => `${h.emp_code}: NPR ${h.amount}`) });
  if (RELEASING_RUNS.has(run.run_type) && employeeIds.length) {
    const rel = await db.query<{ emp_code: string; amount: string }>(
      `SELECT e.emp_code, h.amount::text FROM payroll_hold h JOIN employee e ON e.id = h.employee_id
        WHERE h.company_id = $1 AND h.employee_id = ANY($2) AND h.status = 'releasing' AND h.release_mode = 'payroll' AND (h.release_run_id IS NULL OR h.release_run_id = $3) AND h.run_id <> $3
        ORDER BY e.emp_code`, [companyId, employeeIds, run.id]);
    if (rel.rows.length) out.push({ code: 'held_pay_release', message: 'Pay held earlier is released through this run (not taxed again)', count: rel.rows.length, employees: rel.rows.map((h) => `${h.emp_code}: NPR ${h.amount}`) });
  }
  return out;
}
