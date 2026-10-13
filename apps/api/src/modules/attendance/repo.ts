import type { LeaveCover, Punch, Shift } from '@hajiri/attendance-engine';
import { localMidnightMs, DAY_MIN, MIN_MS } from '@hajiri/attendance-engine';
import type { Queryable } from '../../db/pool.js';

export interface EmployeeRow {
  id: string; company_id: string; gender: string | null; ot_eligible: boolean; weekly_off_days: number[];
  join_date: string | null; exit_date: string | null;
}

export async function getEmployee(db: Queryable, employeeId: string): Promise<EmployeeRow | null> {
  const r = await db.query<EmployeeRow>(
    'SELECT id, company_id, gender, ot_eligible, weekly_off_days, join_date, exit_date FROM employee WHERE id = $1', [employeeId]);
  return r.rows[0] ?? null;
}

export async function getBranchId(db: Queryable, employeeId: string, localDate: string): Promise<string | null> {
  const r = await db.query<{ branch_id: string | null }>(
    'SELECT branch_id FROM employee_assignment WHERE employee_id = $1 AND valid @> $2::date', [employeeId, localDate]);
  return r.rows[0]?.branch_id ?? null;
}

interface ShiftRow {
  id: string; code: string; start_time: string; end_time: string; grace_in_min: number; grace_out_min: number;
  early_in_window_min: number; late_out_window_min: number; break_minutes: number; half_day_threshold_min: number;
  absent_threshold_min: number; pairing_mode: Shift['pairingMode']; rounding_min: number; rounding_direction: Shift['roundingDirection'];
}

const SHIFT_COLS = `id, code, start_time::text, end_time::text, grace_in_min, grace_out_min, early_in_window_min, late_out_window_min,
  break_minutes, half_day_threshold_min, absent_threshold_min, pairing_mode, rounding_min, rounding_direction`;

function toShift(r: ShiftRow): Shift & { id: string } {
  return {
    id: r.id, code: r.code, start: r.start_time.slice(0, 5), end: r.end_time.slice(0, 5),
    graceInMin: r.grace_in_min, graceOutMin: r.grace_out_min, earlyInWindowMin: r.early_in_window_min,
    lateOutWindowMin: r.late_out_window_min, breakMinutes: r.break_minutes, halfDayThresholdMin: r.half_day_threshold_min,
    absentThresholdMin: r.absent_threshold_min, pairingMode: r.pairing_mode, roundingMin: r.rounding_min,
    roundingDirection: r.rounding_direction,
  };
}

/** approved shift_change regularisation > roster row (even if its shift is NULL) > shift_assignment > none (plan §10.1). */
export async function resolveShift(db: Queryable, employeeId: string, localDate: string): Promise<(Shift & { id: string }) | null> {
  const reg = await db.query<{ shift_id: string | null }>(
    `SELECT shift_id FROM regularisation_request WHERE employee_id = $1 AND local_date = $2 AND kind = 'shift_change' AND status = 'approved'
      ORDER BY decided_at DESC LIMIT 1`, [employeeId, localDate]);
  let shiftId: string | null | undefined = reg.rows[0]?.shift_id;
  if (reg.rows.length === 0) {
    const roster = await db.query<{ shift_id: string | null }>(
      'SELECT shift_id FROM roster WHERE employee_id = $1 AND local_date = $2', [employeeId, localDate]);
    shiftId = roster.rows[0]?.shift_id;
    if (roster.rows.length === 0) {
      const sa = await db.query<{ shift_id: string }>(
        'SELECT shift_id FROM shift_assignment WHERE employee_id = $1 AND valid @> $2::date', [employeeId, localDate]);
      shiftId = sa.rows[0]?.shift_id ?? null;
    }
  }
  if (!shiftId) return null;
  const s = await db.query<ShiftRow>(`SELECT ${SHIFT_COLS} FROM shift WHERE id = $1`, [shiftId]);
  return s.rows[0] ? toShift(s.rows[0]) : null;
}

/** Punches that could fall in any window of this date's shift: from the previous local day to two days after. */
export async function getPunchesAround(db: Queryable, employeeId: string, localDate: string): Promise<Punch[]> {
  const midnight = localMidnightMs(localDate);
  const from = new Date(midnight - DAY_MIN * MIN_MS);
  const to = new Date(midnight + 3 * DAY_MIN * MIN_MS);
  const r = await db.query<{ id: string; punched_at: Date; direction: Punch['direction']; verify_mode: string; source: string }>(
    `SELECT p.id, p.punched_at, p.direction, p.verify_mode, p.source
       FROM punch_event p
      WHERE p.employee_id = $1 AND p.punched_at >= $2 AND p.punched_at < $3
        AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
      ORDER BY p.punched_at, p.id`,
    [employeeId, from, to]);
  return r.rows.map((p) => ({ id: p.id, at: p.punched_at.getTime(), direction: p.direction, verifyMode: p.verify_mode, source: p.source }));
}

export async function isHoliday(db: Queryable, companyId: string, branchId: string | null, localDate: string, gender: string | null): Promise<boolean> {
  const r = await db.query(
    `SELECT 1 FROM holiday
      WHERE company_id = $1 AND (branch_id IS NULL OR branch_id = $2) AND local_date = $3 AND NOT optional
        AND (gender_scope = 'all' OR gender_scope = $4) LIMIT 1`,
    [companyId, branchId, localDate, gender]);
  return r.rows.length > 0;
}

/** Approved leave covering the date: full day, or a half (plan §10.1 ctx.leave). */
export async function approvedLeave(db: Queryable, employeeId: string, localDate: string): Promise<{ cover: LeaveCover; paid: boolean; leaveTypeId: string | null }> {
  const r = await db.query<{ half_day: 'first_half' | 'second_half' | null; paid: boolean; leave_type_id: string }>(
    `SELECT lr.half_day, lt.paid, lr.leave_type_id FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id
      WHERE lr.employee_id = $1 AND lr.status = 'approved' AND $2::date BETWEEN lr.from_date AND lr.to_date
      ORDER BY lr.decided_at DESC LIMIT 1`, [employeeId, localDate]);
  const row = r.rows[0];
  if (!row) return { cover: 'none', paid: true, leaveTypeId: null };
  return { cover: row.half_day ?? 'full', paid: row.paid, leaveTypeId: row.leave_type_id };
}

export async function approvedWfh(db: Queryable, employeeId: string, localDate: string): Promise<boolean> {
  const r = await db.query(
    `SELECT 1 FROM regularisation_request WHERE employee_id = $1 AND local_date = $2 AND kind IN ('wfh', 'on_duty') AND status = 'approved' LIMIT 1`,
    [employeeId, localDate]);
  return r.rows.length > 0;
}

export async function approvedOtMinutes(db: Queryable, employeeId: string, localDate: string): Promise<number | null> {
  const r = await db.query<{ approved_min: number | null }>(
    `SELECT approved_min FROM ot_request WHERE employee_id = $1 AND local_date = $2 AND status = 'approved' ORDER BY decided_at DESC LIMIT 1`,
    [employeeId, localDate]);
  return r.rows[0]?.approved_min ?? null;
}

/** OT minutes already recorded this week (Sunday..Saturday) on other days, from the latest version of each day. */
export async function otMinutesThisWeek(db: Queryable, employeeId: string, localDate: string): Promise<number> {
  const r = await db.query<{ total: string }>(
    `WITH wk AS (SELECT ($2::date - extract(dow from $2::date)::int) AS start)
     SELECT coalesce(sum(ot_min), 0)::text AS total FROM (
       SELECT DISTINCT ON (local_date) ot_min FROM attendance_day, wk
        WHERE employee_id = $1 AND local_date <> $2::date AND local_date >= wk.start AND local_date < wk.start + 7
        ORDER BY local_date, version DESC) d`,
    [employeeId, localDate]);
  return Number(r.rows[0]?.total ?? 0);
}

export interface LatestVersion { id: string; version: number; inputs_hash: string; locked_at: Date | null }

export async function latestVersion(db: Queryable, employeeId: string, localDate: string): Promise<LatestVersion | null> {
  const r = await db.query<LatestVersion>(
    'SELECT id, version, inputs_hash, locked_at FROM attendance_day WHERE employee_id = $1 AND local_date = $2 ORDER BY version DESC LIMIT 1',
    [employeeId, localDate]);
  return r.rows[0] ?? null;
}

export async function bsDateFor(db: Queryable, localDate: string): Promise<string | null> {
  const r = await db.query<{ bs_date: string }>('SELECT bs_date FROM bs_calendar WHERE ad_date = $1', [localDate]);
  return r.rows[0]?.bs_date ?? null;
}

export async function attendancePolicy(db: Queryable, companyId: string): Promise<Record<string, unknown>> {
  const r = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'attendance.policy'`, [companyId]);
  return r.rows[0]?.value ?? {};
}
