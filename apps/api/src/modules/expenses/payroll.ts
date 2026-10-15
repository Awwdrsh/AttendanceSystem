import { D, type ComponentDef } from '@hajiri/payroll-engine';
import type { Queryable } from '../../db/pool.js';
import { money, rs } from './service.js';

/**
 * Payroll side of expenses and travel. Approved claims, travel advances and settlements become expense_payout rows;
 * computing a run claims the employee's pending payouts for that run and adds them as one-time inputs, recomputing
 * releases and re-claims them, and locking the run marks them paid. A payout is held by at most one run at a time,
 * so it is paid exactly once.
 */
export const PAYOUT_COMPONENTS: Record<string, ComponentDef> = {
  EXP_REIMB: { code: 'EXP_REIMB', type: 'earning', nameEn: 'Expense reimbursement', nameNe: null, dependsOnPaymentDays: false, taxable: false, rounding: 'paisa', sortOrder: 96 },
  TRAVEL_ADV: { code: 'TRAVEL_ADV', type: 'earning', nameEn: 'Travel advance', nameNe: null, dependsOnPaymentDays: false, taxable: false, rounding: 'paisa', sortOrder: 97 },
  EXP_RECOVERY: { code: 'EXP_RECOVERY', type: 'deduction', nameEn: 'Travel advance recovery', nameNe: null, dependsOnPaymentDays: false, taxable: false, rounding: 'paisa', sortOrder: 272 },
};

/** Run types that pay reimbursements; arrears and bonus runs replace their inputs and never carry them. */
const PAYING_RUNS = new Set(['regular', 'supplementary', 'off_cycle', 'final_settlement']);

/** Start of a (re)compute: everything this run held goes back to the pool before it is claimed again. */
export async function releaseRunPayouts(db: Queryable, runId: string): Promise<void> {
  await db.query(`UPDATE expense_payout SET payroll_run_id = NULL WHERE payroll_run_id = $1 AND status = 'pending'`, [runId]);
}

/** Claim the employee's pending payouts for this run and return them as payroll inputs. */
export async function claimPayouts(db: Queryable, companyId: string, runId: string, runType: string, employeeId: string): Promise<Array<{ code: string; amount: string; note: string }>> {
  if (!PAYING_RUNS.has(runType)) return [];
  const r = await db.query<{ component_code: string; amount: string; note: string | null }>(
    `UPDATE expense_payout SET payroll_run_id = $3
      WHERE id IN (SELECT id FROM expense_payout WHERE company_id = $1 AND employee_id = $2 AND status = 'pending' AND (payroll_run_id IS NULL OR payroll_run_id = $3) FOR UPDATE)
      RETURNING component_code, amount::text, note`, [companyId, employeeId, runId]);
  return r.rows.map((p) => ({ code: p.component_code, amount: p.amount, note: p.note ?? '' }));
}

/** The employee's structure plus any payout component it does not already have (the company may rename them). */
export async function withPayoutComponents(db: Queryable, companyId: string, components: ComponentDef[], inputs: Array<{ code: string }>): Promise<ComponentDef[]> {
  const needed = [...new Set(inputs.map((i) => i.code))].filter((c) => PAYOUT_COMPONENTS[c] && !components.some((x) => x.code === c));
  if (!needed.length) return components;
  const custom = await db.query<{ code: string; name_en: string }>('SELECT code, name_en FROM salary_component WHERE company_id = $1 AND code = ANY($2) AND active', [companyId, needed]);
  return [...components, ...needed.map((c) => ({ ...PAYOUT_COMPONENTS[c]!, nameEn: custom.rows.find((x) => x.code === c)?.name_en ?? PAYOUT_COMPONENTS[c]!.nameEn }))];
}

/** Pre-flight: what this run will pay out (or recover) for claims and travel. */
export async function payoutPreview(db: Queryable, companyId: string, runId: string, runType: string, employeeIds: string[]): Promise<{ count: number; reimburse: string; recover: string; employees: string[] }> {
  if (!PAYING_RUNS.has(runType) || employeeIds.length === 0) return { count: 0, reimburse: '0.00', recover: '0.00', employees: [] };
  const r = await db.query<{ emp_code: string; component_code: string; amount: string }>(
    `SELECT e.emp_code, p.component_code, p.amount::text FROM expense_payout p JOIN employee e ON e.id = p.employee_id
      WHERE p.company_id = $1 AND p.employee_id = ANY($2) AND p.status = 'pending' AND (p.payroll_run_id IS NULL OR p.payroll_run_id = $3) ORDER BY e.emp_code`,
    [companyId, employeeIds, runId]);
  const reimburse = r.rows.filter((x) => x.component_code !== 'EXP_RECOVERY').reduce((a, x) => a.plus(x.amount), D(0));
  const recover = r.rows.filter((x) => x.component_code === 'EXP_RECOVERY').reduce((a, x) => a.plus(x.amount), D(0));
  const byEmp = new Map<string, number>();
  for (const x of r.rows) byEmp.set(x.emp_code, (byEmp.get(x.emp_code) ?? 0) + 1);
  return { count: r.rows.length, reimburse: money(reimburse), recover: money(recover), employees: [...byEmp].map(([c, n]) => `${c}: ${n} ${n === 1 ? 'item' : 'items'}`) };
}

export function previewMessage(p: { count: number; reimburse: string; recover: string }): string {
  const parts = [D(p.reimburse).gt(0) ? `${rs(p.reimburse)} of approved expenses and travel to pay` : '', D(p.recover).gt(0) ? `${rs(p.recover)} of unspent travel advances to recover` : ''].filter(Boolean);
  return `This run includes ${parts.join(' and ')} (not taxed).`;
}

/**
 * Lock: the run's payouts are paid. A recovery the pay could not fully cover (deductions are capped at the pay left)
 * keeps the rest outstanding as a new payout for the next run, and the source records are marked paid.
 */
export async function settleRunPayouts(db: Queryable, runId: string): Promise<void> {
  const held = await db.query<{ id: string; company_id: string; employee_id: string; source_type: string; source_id: string; component_code: string; amount: string; note: string | null }>(
    `SELECT id, company_id, employee_id, source_type, source_id, component_code, amount::text, note FROM expense_payout WHERE payroll_run_id = $1 AND status = 'pending' ORDER BY created_at FOR UPDATE`, [runId]);
  if (!held.rows.length) return;
  const recovered = await db.query<{ employee_id: string; amount: string }>(
    `SELECT ps.employee_id, coalesce(sum(l.amount), 0)::text AS amount FROM payslip ps JOIN payslip_line l ON l.payslip_id = ps.id
      WHERE ps.run_id = $1 AND l.component_code = 'EXP_RECOVERY' GROUP BY ps.employee_id`, [runId]);
  const available = new Map(recovered.rows.map((x) => [x.employee_id, D(x.amount)]));
  for (const p of held.rows) {
    let paid = D(p.amount);
    if (p.component_code === 'EXP_RECOVERY') {
      const left = available.get(p.employee_id) ?? D(0);
      paid = left.lt(paid) ? left : paid;
      available.set(p.employee_id, left.minus(paid));
      const rest = D(p.amount).minus(paid);
      if (rest.gt(0)) {
        await db.query(
          `INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note, parent_payout_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
          [p.company_id, p.employee_id, p.source_type, p.source_id, p.component_code, money(rest), `${p.note ?? ''} (balance)`.slice(0, 200), p.id]);
      }
    }
    await db.query(`UPDATE expense_payout SET status = 'paid', paid_amount = $2, paid_at = now() WHERE id = $1`, [p.id, money(paid)]);
  }
  await markSourcesPaid(db, held.rows);
}

export async function markSourcesPaid(db: Queryable, payouts: Array<{ source_type: string; source_id: string }>): Promise<void> {
  for (const p of payouts) {
    if (p.source_type === 'expense_claim') {
      await db.query(`UPDATE expense_claim c SET status = 'paid', paid_at = now(), updated_at = now() WHERE c.id = $1 AND c.status = 'approved'
                        AND NOT EXISTS (SELECT 1 FROM expense_payout x WHERE x.source_type = 'expense_claim' AND x.source_id = c.id AND x.status = 'pending')`, [p.source_id]);
    } else if (p.source_type === 'travel_advance') {
      await db.query(`UPDATE travel_request SET advance_status = 'paid' WHERE id = $1 AND advance_status = 'pending'`, [p.source_id]);
    }
  }
}
