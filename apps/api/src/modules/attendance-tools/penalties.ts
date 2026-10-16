import { bsMonthBounds } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { bsMonthName } from '../../bsdate.js';
import { ConflictError, NotFoundError } from '../../errors.js';
import { ledgerBalance, pendingDays } from '../leave/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import type { LatePenaltyRule } from './policy.js';

/**
 * Late-arrival penalty (study: "Late attendance penalty"). For a BS month, every `lateCount` days with a late arrival of
 * at least `minLateMinutes` after the grace time cost `penaltyDays`. HR previews the month, then applies it before
 * payroll: the days come off the chosen leave types in order (a leave-ledger adjustment), and whatever they cannot cover
 * becomes unpaid days that the pay run deducts as loss of pay. Applied penalties can be reversed until the month's
 * regular pay run is locked.
 */
export interface PenaltyLine {
  employeeId: string; empCode: string; name: string; lateDays: number; lateMinutes: number; lateDates: string[];
  penaltyDays: number; allocations: Array<{ leaveTypeId: string; code: string; days: number }>; unpaidDays: number;
  applied: { id: string; penaltyDays: number; leaveDays: number; unpaidDays: number; appliedAt: string } | null;
}

/** Penalty for a number of late days under a rule (pure; unit-tested). */
export function penaltyFor(lateDays: number, rule: Pick<LatePenaltyRule, 'lateCount' | 'penaltyDays'>): number {
  if (lateDays < rule.lateCount) return 0;
  return Math.floor(lateDays / rule.lateCount) * rule.penaltyDays;
}

/** Split penalty days over the available balances of the leave types in order; the rest is unpaid (pure; unit-tested). */
export function allocatePenalty(days: number, available: Array<{ leaveTypeId: string; code: string; available: number }>): { allocations: Array<{ leaveTypeId: string; code: string; days: number }>; unpaid: number } {
  let left = days;
  const allocations: Array<{ leaveTypeId: string; code: string; days: number }> = [];
  for (const a of available) {
    if (left <= 0) break;
    // Leave is taken in quarter days so balances never end up with odd fractions.
    const can = Math.floor(Math.max(0, a.available) * 4) / 4;
    const take = Math.min(left, can);
    if (take > 0) { allocations.push({ leaveTypeId: a.leaveTypeId, code: a.code, days: take }); left = Math.round((left - take) * 100) / 100; }
  }
  return { allocations, unpaid: Math.max(0, Math.round(left * 100) / 100) };
}

export async function monthLocked(db: Queryable, companyId: string, bsYear: number, bsMonth: number): Promise<boolean> {
  const r = await db.query(
    `SELECT 1 FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
      WHERE p.company_id = $1 AND p.bs_year = $2 AND p.bs_month = $3 AND r.run_type = 'regular' AND r.locked_at IS NOT NULL LIMIT 1`, [companyId, bsYear, bsMonth]);
  return r.rows.length > 0;
}

/** Late days per employee in the month (one employee, a manager's team or the whole company). */
export async function penaltyPreview(db: Queryable, companyId: string, bsYear: number, bsMonth: number, rule: LatePenaltyRule,
  scope: { employeeId?: string; managerEmployeeId?: string } = {}): Promise<PenaltyLine[]> {
  const { startAd, endAd } = bsMonthBounds(bsYear, bsMonth);
  const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; late_days: number; late_minutes: number; late_dates: string[] }>(
    `SELECT e.id AS employee_id, e.emp_code, e.name_en,
            count(v.local_date) FILTER (WHERE v.late_min >= $4)::int AS late_days,
            coalesce(sum(v.late_min) FILTER (WHERE v.late_min >= $4), 0)::int AS late_minutes,
            coalesce(array_agg(v.local_date::text ORDER BY v.local_date) FILTER (WHERE v.late_min >= $4), '{}') AS late_dates
       FROM employee e
       JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3
       LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
      WHERE e.company_id = $1 AND ($5::uuid IS NULL OR e.id = $5) AND ($6::uuid IS NULL OR hj_manages($6::uuid, e.id, current_date))
      GROUP BY e.id, e.emp_code, e.name_en
     HAVING count(v.local_date) FILTER (WHERE v.late_min >= $4) > 0
      ORDER BY late_days DESC, e.emp_code`,
    [companyId, startAd, endAd, rule.minLateMinutes, scope.employeeId ?? null, scope.managerEmployeeId ?? null]);
  const applied = await db.query<{ id: string; employee_id: string; penalty_days: string; leave_days: string; unpaid_days: string; applied_at: Date }>(
    `SELECT id, employee_id, penalty_days, leave_days, unpaid_days, applied_at FROM late_penalty
      WHERE company_id = $1 AND bs_year = $2 AND bs_month = $3 AND status = 'applied'`, [companyId, bsYear, bsMonth]);
  const types = rule.leaveTypeIds.length
    ? (await db.query<{ id: string; code: string }>('SELECT id, code FROM leave_type WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, rule.leaveTypeIds])).rows
    : [];
  const ordered = rule.leaveTypeIds.map((id) => types.find((t) => t.id === id)).filter((t): t is { id: string; code: string } => !!t);
  const out: PenaltyLine[] = [];
  for (const r of rows.rows) {
    const penaltyDays = penaltyFor(r.late_days, rule);
    const avail = [];
    for (const t of ordered) avail.push({ leaveTypeId: t.id, code: t.code, available: (await ledgerBalance(db, r.employee_id, t.id)) - (await pendingDays(db, r.employee_id, t.id)) });
    const a = allocatePenalty(penaltyDays, avail);
    const ap = applied.rows.find((x) => x.employee_id === r.employee_id);
    out.push({
      employeeId: r.employee_id, empCode: r.emp_code, name: r.name_en, lateDays: r.late_days, lateMinutes: r.late_minutes, lateDates: r.late_dates,
      penaltyDays, allocations: a.allocations, unpaidDays: a.unpaid,
      applied: ap ? { id: ap.id, penaltyDays: Number(ap.penalty_days), leaveDays: Number(ap.leave_days), unpaidDays: Number(ap.unpaid_days), appliedAt: ap.applied_at.toISOString() } : null,
    });
  }
  return out;
}

/** Apply the month's penalties for the listed employees (or everyone with one); already-applied employees are skipped. */
export async function applyPenalties(db: Queryable, companyId: string, bsYear: number, bsMonth: number, rule: LatePenaltyRule, employeeIds: string[] | null, actorId: string): Promise<{ applied: number; skipped: number }> {
  if (await monthLocked(db, companyId, bsYear, bsMonth)) throw new ConflictError(`${bsMonthName(bsYear, bsMonth)} ${bsYear} payroll is locked; penalties can no longer change`);
  const { endAd } = bsMonthBounds(bsYear, bsMonth);
  const lines = await penaltyPreview(db, companyId, bsYear, bsMonth, rule);
  let applied = 0; let skipped = 0;
  for (const l of lines) {
    if (employeeIds && !employeeIds.includes(l.employeeId)) continue;
    if (l.applied || l.penaltyDays <= 0) { skipped++; continue; }
    const leaveDays = l.allocations.reduce((s, a) => s + a.days, 0);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO late_penalty (company_id, employee_id, bs_year, bs_month, late_days, late_minutes, penalty_days, leave_days, leave_type_id, unpaid_days, rule, applied_by)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) ON CONFLICT DO NOTHING RETURNING id`,
      [companyId, l.employeeId, bsYear, bsMonth, l.lateDays, l.lateMinutes, l.penaltyDays, leaveDays, l.allocations[0]?.leaveTypeId ?? null, l.unpaidDays,
        JSON.stringify({ ...rule, allocations: l.allocations, lateDates: l.lateDates }), actorId]);
    const id = ins.rows[0]?.id;
    if (!id) { skipped++; continue; }
    for (const a of l.allocations) {
      await db.query(
        `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note, posted_by)
         VALUES ($1, $2, $3, $4, 'adjustment', 'late_penalty', $5, $6, $7)`,
        [l.employeeId, a.leaveTypeId, endAd, -a.days, id, `Late-arrival penalty, ${bsMonthName(bsYear, bsMonth)} ${bsYear} (${l.lateDays} late days)`, actorId]);
    }
    await audit(db, { actorId, action: 'apply_late_penalty', table: 'late_penalty', rowId: id, after: { employeeId: l.employeeId, bsYear, bsMonth, lateDays: l.lateDays, penaltyDays: l.penaltyDays, leaveDays, unpaidDays: l.unpaidDays } });
    const userId = await userIdForEmployee(db, l.employeeId);
    const parts = [leaveDays ? `${leaveDays} day(s) from leave` : '', l.unpaidDays ? `${l.unpaidDays} unpaid day(s)` : ''].filter(Boolean).join(' and ');
    await notify(db, { companyId, userId, employeeId: l.employeeId, template: 'attendance.late_penalty', title: 'Late-arrival penalty',
      body: `You arrived late on ${l.lateDays} day(s) in ${bsMonthName(bsYear, bsMonth)} ${bsYear}. Under the company rule this counts as ${l.penaltyDays} day(s): ${parts}.`, payload: { penaltyId: id } });
    applied++;
  }
  return { applied, skipped };
}

export async function reversePenalty(db: Queryable, companyId: string, id: string, reason: string, actorId: string): Promise<void> {
  const r = await db.query<{ employee_id: string; bs_year: number; bs_month: number; status: string; rule: { allocations?: Array<{ leaveTypeId: string; days: number }> } }>(
    'SELECT employee_id, bs_year, bs_month, status, rule FROM late_penalty WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
  const p = r.rows[0];
  if (!p) throw new NotFoundError('penalty not found');
  if (p.status !== 'applied') throw new ConflictError('this penalty is already reversed');
  if (await monthLocked(db, companyId, p.bs_year, p.bs_month)) throw new ConflictError('the payroll for this month is locked; the penalty can no longer be reversed');
  const { endAd } = bsMonthBounds(p.bs_year, p.bs_month);
  for (const a of p.rule.allocations ?? []) {
    await db.query(
      `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note, posted_by)
       VALUES ($1, $2, $3, $4, 'adjustment', 'late_penalty_reversed', $5, $6, $7)`,
      [p.employee_id, a.leaveTypeId, endAd, a.days, id, `Late-arrival penalty reversed: ${reason}`, actorId]);
  }
  await db.query(`UPDATE late_penalty SET status = 'reversed', reversed_by = $2, reversed_at = now(), reverse_reason = $3 WHERE id = $1`, [id, actorId, reason]);
  await audit(db, { actorId, action: 'reverse_late_penalty', table: 'late_penalty', rowId: id, after: { reason } });
}

/** Unpaid penalty days for payroll's loss-of-pay in a BS month (applied penalties only). */
export async function penaltyUnpaidDays(db: Queryable, employeeId: string, bsYear: number, bsMonth: number): Promise<number> {
  const r = await db.query<{ d: string }>(
    `SELECT coalesce(sum(unpaid_days), 0)::text AS d FROM late_penalty WHERE employee_id = $1 AND bs_year = $2 AND bs_month = $3 AND status = 'applied'`, [employeeId, bsYear, bsMonth]);
  return Number(r.rows[0]!.d);
}
