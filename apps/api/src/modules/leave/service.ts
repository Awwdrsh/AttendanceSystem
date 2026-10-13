import { addBsDays, adToBs, bsToAd, fiscalYearOf } from '@hajiri/bs-calendar';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { dateRange } from '../attendance/service.js';
import { nptToday } from '../workflow/service.js';

export interface LeaveTypeRow {
  id: string; company_id: string; code: string; name_en: string; name_ne: string | null; paid: boolean; statutory: boolean;
  accrual_basis: 'none' | 'worked_days' | 'monthly' | 'annual'; accrual_days: string; accrual_per_worked_days: number | null;
  max_balance: string | null; carry_forward: boolean; encashable: boolean; certificate_after_days: number | null; allow_half_day: boolean;
  max_consecutive_days: number | null; min_service_days: number; negative_balance_limit: string; gender_scope: 'all' | 'female' | 'male';
  sandwich_rule: boolean; expiry_days: number | null; sort_order: number;
}

export interface EmployeeLite {
  id: string; company_id: string; gender: string | null; weekly_off_days: number[]; join_date: string | null; probation_end: string | null; exit_date: string | null; status: string;
}

export async function getLeaveType(db: Queryable, id: string): Promise<LeaveTypeRow | null> {
  const r = await db.query<LeaveTypeRow>('SELECT * FROM leave_type WHERE id = $1', [id]);
  return r.rows[0] ?? null;
}

export async function leaveTypeByCode(db: Queryable, companyId: string, code: string): Promise<LeaveTypeRow | null> {
  const r = await db.query<LeaveTypeRow>('SELECT * FROM leave_type WHERE company_id = $1 AND code = $2', [companyId, code]);
  return r.rows[0] ?? null;
}

export async function getEmployeeLite(db: Queryable, id: string): Promise<EmployeeLite | null> {
  const r = await db.query<EmployeeLite>('SELECT id, company_id, gender, weekly_off_days, join_date, probation_end, exit_date, status FROM employee WHERE id = $1', [id]);
  return r.rows[0] ?? null;
}

/** Dates in [from, to] that are neither weekly offs nor (gender-aware, branch-aware) holidays. */
export async function workingDaysBetween(db: Queryable, emp: EmployeeLite, from: string, to: string): Promise<{ working: string[]; nonWorking: string[] }> {
  const branch = await db.query<{ branch_id: string | null }>('SELECT branch_id FROM employee_assignment WHERE employee_id = $1 AND valid @> $2::date', [emp.id, from]);
  const hol = await db.query<{ local_date: string }>(
    `SELECT local_date FROM holiday WHERE company_id = $1 AND (branch_id IS NULL OR branch_id = $2) AND local_date BETWEEN $3 AND $4 AND NOT optional
       AND (gender_scope = 'all' OR gender_scope = $5)`, [emp.company_id, branch.rows[0]?.branch_id ?? null, from, to, emp.gender]);
  const holidays = new Set(hol.rows.map((h) => h.local_date));
  const working: string[] = []; const nonWorking: string[] = [];
  for (const d of dateRange(from, to)) {
    if (holidays.has(d) || emp.weekly_off_days.includes(weekdayOf(d))) nonWorking.push(d); else working.push(d);
  }
  return { working, nonWorking };
}

export interface DaysComputation { days: number; workingDays: string[]; excludedDays: string[]; sandwichApplied: boolean; explanation: string }

/**
 * Days deducted for a request (plan §10.6): working days only, unless the sandwich rule bridges the range with leave on
 * both ends and only holidays/weekly-offs between. Half-day requests are a single date worth 0.5.
 */
export async function computeLeaveDays(db: Queryable, emp: EmployeeLite, type: LeaveTypeRow, from: string, to: string, halfDay: string | null, sandwichPolicy: boolean): Promise<DaysComputation> {
  if (halfDay) {
    if (from !== to) throw new BadRequestError('half-day leave must be a single date');
    if (!type.allow_half_day) throw new BadRequestError(`${type.name_en} cannot be taken as a half day`);
    const { working } = await workingDaysBetween(db, emp, from, to);
    if (!working.length) throw new BadRequestError(`${from} is not a working day`);
    return { days: 0.5, workingDays: working, excludedDays: [], sandwichApplied: false, explanation: 'Half day will be deducted' };
  }
  const { working, nonWorking } = await workingDaysBetween(db, emp, from, to);
  const sandwich = (type.sandwich_rule || sandwichPolicy) && working.length >= 2 && nonWorking.length > 0 && working[0] === from && working[working.length - 1] === to;
  if (sandwich) {
    const all = dateRange(from, to);
    return { days: all.length, workingDays: all, excludedDays: [], sandwichApplied: true,
      explanation: `${all.length} calendar days will be deducted (sandwich rule: ${nonWorking.length} non-working day(s) between leave days count)` };
  }
  if (!working.length) throw new BadRequestError('the selected range contains no working days');
  return { days: working.length, workingDays: working, excludedDays: nonWorking, sandwichApplied: false,
    explanation: nonWorking.length ? `${working.length} working day(s) will be deducted; ${nonWorking.length} weekly-off/holiday day(s) excluded` : `${working.length} working day(s) will be deducted` };
}

export async function ledgerBalance(db: Queryable, employeeId: string, leaveTypeId: string, asOf?: string): Promise<number> {
  const r = await db.query<{ b: string }>(
    'SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2 AND ($3::date IS NULL OR effective_date <= $3)',
    [employeeId, leaveTypeId, asOf ?? null]);
  return Number(r.rows[0]!.b);
}

export async function pendingDays(db: Queryable, employeeId: string, leaveTypeId: string): Promise<number> {
  const r = await db.query<{ d: string }>(`SELECT coalesce(sum(days_requested), 0)::text AS d FROM leave_request WHERE employee_id = $1 AND leave_type_id = $2 AND status = 'pending'`, [employeeId, leaveTypeId]);
  return Number(r.rows[0]!.d);
}

export interface BalanceLine {
  leaveTypeId: string; code: string; nameEn: string; nameNe: string | null; paid: boolean; encashable: boolean;
  opening: number; accrued: number; taken: number; cancelled: number; lapsed: number; carried: number; adjustments: number; compOff: number; encashed: number;
  pending: number; balance: number; available: number; fiscalYear: string; explanation: string;
}

/** Balance explanation per plan §13.5: Opening (1 Shrawan) + accruals − taken − lapsed + carried forward. */
export async function balances(db: Queryable, emp: EmployeeLite, asOfAd: string): Promise<BalanceLine[]> {
  const fy = fiscalYearOf(adToBs(asOfAd));
  const types = await db.query<LeaveTypeRow>(`SELECT * FROM leave_type WHERE company_id = $1 AND (gender_scope = 'all' OR gender_scope = $2) ORDER BY sort_order, code`, [emp.company_id, emp.gender]);
  const out: BalanceLine[] = [];
  for (const t of types.rows) {
    const r = await db.query<{ reason: string; in_fy: boolean; total: string }>(
      `SELECT reason, (effective_date >= $3::date) AS in_fy, coalesce(sum(delta_days), 0)::text AS total FROM leave_ledger
        WHERE employee_id = $1 AND leave_type_id = $2 GROUP BY reason, (effective_date >= $3::date)`, [emp.id, t.id, fy.startAd]);
    const sum = (reason: string, inFy: boolean) => r.rows.filter((x) => x.reason === reason && x.in_fy === inFy).reduce((a, x) => a + Number(x.total), 0);
    const opening = r.rows.filter((x) => !x.in_fy).reduce((a, x) => a + Number(x.total), 0);
    const accrued = sum('accrual', true) + sum('allocation', true);
    const taken = -sum('usage', true);
    const cancelled = sum('cancellation', true);
    const lapsed = -sum('expiry', true);
    const carried = sum('carry_forward', true);
    const adjustments = sum('adjustment', true);
    const compOff = sum('comp_off', true);
    const encashed = -sum('encashment', true);
    const balance = r.rows.reduce((a, x) => a + Number(x.total), 0);
    const pending = await pendingDays(db, emp.id, t.id);
    out.push({
      leaveTypeId: t.id, code: t.code, nameEn: t.name_en, nameNe: t.name_ne, paid: t.paid, encashable: t.encashable,
      opening, accrued, taken, cancelled, lapsed, carried, adjustments, compOff, encashed, pending, balance, available: balance - pending, fiscalYear: fy.label,
      explanation: `Opening ${opening.toFixed(2)} on 1 Shrawan ${fy.startYear} + accrued ${accrued.toFixed(2)} + comp-off ${compOff.toFixed(2)} + adjustments ${adjustments.toFixed(2)} − taken ${taken.toFixed(2)} + cancelled ${cancelled.toFixed(2)} − lapsed ${lapsed.toFixed(2)} − encashed ${encashed.toFixed(2)} = ${balance.toFixed(2)} days${pending ? ` (${pending} pending)` : ''}`,
    });
  }
  return out;
}

/** Credit substitute leave for holiday/weekly-off work or comp-off (Sec 42: expires 21 days after). */
export async function creditCompOff(db: Queryable, employeeId: string, days: number, expiresOn: string, ref: { refType: string; refId: string; note?: string; postedBy?: string | null }): Promise<string> {
  const emp = await getEmployeeLite(db, employeeId);
  if (!emp) throw new NotFoundError('employee not found');
  const type = await leaveTypeByCode(db, emp.company_id, 'SUBSTITUTE');
  if (!type) throw new NotFoundError('SUBSTITUTE leave type is not configured');
  const r = await db.query<{ id: string }>(
    `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, expires_on, note, posted_by)
     VALUES ($1, $2, $9::date, $3, 'comp_off', $4, $5, $6, $7, $8) RETURNING id`,
    [employeeId, type.id, days, ref.refType, ref.refId, expiresOn, ref.note ?? null, ref.postedBy ?? null, nptToday()]);
  return r.rows[0]!.id;
}

/** Encashable balances at separation (Sec 49(2)): home and sick leave. */
export async function encashableBalances(db: Queryable, employeeId: string): Promise<Array<{ leaveTypeId: string; code: string; days: number }>> {
  const emp = await getEmployeeLite(db, employeeId);
  if (!emp) throw new NotFoundError('employee not found');
  const types = await db.query<LeaveTypeRow>('SELECT * FROM leave_type WHERE company_id = $1 AND encashable ORDER BY sort_order', [emp.company_id]);
  const out: Array<{ leaveTypeId: string; code: string; days: number }> = [];
  for (const t of types.rows) {
    const b = await ledgerBalance(db, employeeId, t.id);
    if (b > 0) out.push({ leaveTypeId: t.id, code: t.code, days: b });
  }
  return out;
}

/** Post the encashment rows (payroll final settlement calls this once the run is locked). */
export async function postEncashment(db: Queryable, employeeId: string, leaveTypeId: string, days: number, ref: { refType: string; refId: string; postedBy?: string | null }): Promise<void> {
  await db.query(
    `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by) VALUES ($1, $2, $7::date, $3, 'encashment', $4, $5, $6)`,
    [employeeId, leaveTypeId, -days, ref.refType, ref.refId, ref.postedBy ?? null, nptToday()]);
}

export async function assertNoOverlap(db: Queryable, employeeId: string, from: string, to: string, excludeId?: string): Promise<void> {
  const r = await db.query(
    `SELECT 1 FROM leave_request WHERE employee_id = $1 AND status IN ('pending', 'approved') AND from_date <= $3::date AND to_date >= $2::date AND ($4::uuid IS NULL OR id <> $4) LIMIT 1`,
    [employeeId, from, to, excludeId ?? null]);
  if (r.rows[0]) throw new ConflictError('an existing leave request already covers part of this range');
}

/** Any latest attendance day in [from, to] already locked by payroll? Such days can never be changed by leave decisions (plan §10.3). */
export async function hasLockedDay(db: Queryable, employeeId: string, from: string, to: string): Promise<boolean> {
  const r = await db.query('SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3 AND locked_at IS NOT NULL LIMIT 1', [employeeId, from, to]);
  return r.rows.length > 0;
}

export { bsToAd, addBsDays };
