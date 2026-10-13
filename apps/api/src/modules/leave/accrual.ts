import { bsMonthBounds, bsToAd, daysInBsMonth } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { notify, userIdsWithRole } from '../notifications/service.js';
import type { LeaveTypeRow } from './service.js';

export interface AccrualSummary { employees: number; postings: number; expired: number; bsYear: number; bsMonth: number }

const FY_START_MONTH = 4; // Shrawan

/**
 * Monthly leave accrual for a BS month (plan §10.6, Labour Act Sec 43–44, 49):
 * - worked_days basis (home leave): worked days / N, fractions carried in the numeric ledger;
 * - monthly basis (sick leave): accrual_days per month, pro-rated by months of service in the first year (Sec 44 proviso);
 * - annual basis (casual): allocated once on Shrawan;
 * - caps: balance above max_balance is expired (and HR is told so it can be paid out under Sec 49(3) if encashable);
 * - substitute leave past expires_on lapses.
 * Idempotent: each (employee, type, month) posting is keyed by ref_type 'accrual' and a deterministic ref note.
 */
export async function runMonthlyAccrual(db: Queryable, companyId: string, bsYear: number, bsMonth: number): Promise<AccrualSummary> {
  const bounds = bsMonthBounds(bsYear, bsMonth);
  const types = (await db.query<LeaveTypeRow>(`SELECT * FROM leave_type WHERE company_id = $1 AND accrual_basis <> 'none'`, [companyId])).rows;
  const emps = await db.query<{ id: string; gender: string | null; join_date: string | null }>(
    `SELECT id, gender, join_date FROM employee WHERE company_id = $1 AND status = 'active' AND (join_date IS NULL OR join_date <= $2::date)`, [companyId, bounds.endAd]);
  let postings = 0;
  const key = (empId: string, typeId: string) => `accrual:${empId}:${typeId}:${bsYear}-${bsMonth}`;

  for (const emp of emps.rows) {
    const worked = await db.query<{ worked: string }>(
      `SELECT coalesce(sum(CASE WHEN status IN ('Present', 'WFH') THEN 1 WHEN status IN ('HalfDay', 'HalfDayLeave') THEN 0.5 ELSE 0 END), 0)::text AS worked
         FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3`, [emp.id, bounds.startAd, bounds.endAd]);
    const workedDays = Number(worked.rows[0]!.worked);
    for (const t of types) {
      if (t.gender_scope !== 'all' && t.gender_scope !== emp.gender) continue;
      const exists = await db.query('SELECT 1 FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2 AND reason IN ($3, $4) AND note = $5', [emp.id, t.id, 'accrual', 'allocation', key(emp.id, t.id)]);
      if (exists.rows[0]) continue;
      let delta = 0; let reason: 'accrual' | 'allocation' = 'accrual';
      if (t.accrual_basis === 'worked_days') {
        const per = t.accrual_per_worked_days ?? 20;
        delta = Math.round((workedDays / per) * Number(t.accrual_days) * 100) / 100;
      } else if (t.accrual_basis === 'monthly') {
        delta = Number(t.accrual_days);
        // First year of service: pro-rata by months served (Sec 44 proviso) — a month counts once the employee was present at its end.
        if (emp.join_date && emp.join_date > bounds.startAd) delta = 0;
      } else if (t.accrual_basis === 'annual') {
        if (bsMonth !== FY_START_MONTH) continue;
        reason = 'allocation';
        delta = Number(t.accrual_days);
        if (emp.join_date) {
          const fyStart = bounds.startAd;
          const joinedMonths = emp.join_date > fyStart ? Math.max(0, 12 - monthsSince(fyStart, emp.join_date)) : 12;
          delta = Math.round((delta * joinedMonths) / 12 * 100) / 100;
        }
      }
      if (delta <= 0) continue;
      await db.query(
        `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note) VALUES ($1, $2, $3, $4, $5, 'accrual', $6)`,
        [emp.id, t.id, bounds.endAd, delta, reason, key(emp.id, t.id)]);
      postings++;
      // Cap (Sec 43: home ≤ 90, Sec 44: sick ≤ 45). Excess is expired; encashable excess is flagged to HR for payout (Sec 49(3)).
      if (t.max_balance !== null) {
        const bal = await db.query<{ b: string }>('SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2', [emp.id, t.id]);
        const excess = Number(bal.rows[0]!.b) - Number(t.max_balance);
        if (excess > 0) {
          await db.query(
            `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note) VALUES ($1, $2, $3, $4, 'expiry', 'accrual_cap', $5)`,
            [emp.id, t.id, bounds.endAd, -excess, `${key(emp.id, t.id)}:cap`]);
          if (t.encashable) {
            for (const userId of await userIdsWithRole(db, companyId, 'hr')) {
              await notify(db, { companyId, userId, employeeId: emp.id, template: 'leave.cap_exceeded', title: 'Leave balance above cap',
                body: `${t.name_en} balance exceeded ${t.max_balance} days by ${excess.toFixed(2)}; excess should be paid out (Sec 49(3)).`, payload: { leaveTypeId: t.id, excess } });
            }
          }
        }
      }
    }
  }
  const expired = await expireSubstituteLeave(db, companyId, bounds.endAd);
  return { employees: emps.rows.length, postings, expired, bsYear, bsMonth };
}

function monthsSince(fromAd: string, toAd: string): number {
  const a = new Date(`${fromAd}T00:00:00Z`); const b = new Date(`${toAd}T00:00:00Z`);
  return (b.getUTCFullYear() - a.getUTCFullYear()) * 12 + (b.getUTCMonth() - a.getUTCMonth()) + (b.getUTCDate() > a.getUTCDate() ? 1 : 0);
}

/** Substitute leave not used within its window lapses (Sec 42: 21 days). Returns rows expired. */
export async function expireSubstituteLeave(db: Queryable, companyId: string, asOf: string): Promise<number> {
  const rows = await db.query<{ id: string; employee_id: string; leave_type_id: string; delta_days: string; expires_on: string }>(
    `SELECT l.id, l.employee_id, l.leave_type_id, l.delta_days, l.expires_on FROM leave_ledger l JOIN leave_type t ON t.id = l.leave_type_id
      WHERE t.company_id = $1 AND l.reason = 'comp_off' AND l.expires_on IS NOT NULL AND l.expires_on < $2::date
        AND NOT EXISTS (SELECT 1 FROM leave_ledger x WHERE x.reason = 'expiry' AND x.ref_type = 'comp_off' AND x.ref_id = l.id)`, [companyId, asOf]);
  let n = 0;
  for (const row of rows.rows) {
    // Only the unused part of the credit lapses: balance of the type cannot go below zero because of expiry.
    const bal = await db.query<{ b: string }>('SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2', [row.employee_id, row.leave_type_id]);
    const lapse = Math.min(Number(row.delta_days), Math.max(0, Number(bal.rows[0]!.b)));
    await db.query(
      `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note) VALUES ($1, $2, $3, $4, 'expiry', 'comp_off', $5, $6)`,
      [row.employee_id, row.leave_type_id, row.expires_on, -lapse, row.id, `substitute leave expired after ${row.expires_on}`]);
    n++;
  }
  return n;
}

export { bsToAd, daysInBsMonth };
