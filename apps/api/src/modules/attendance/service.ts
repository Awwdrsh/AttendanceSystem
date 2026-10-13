import { computeDay, DEFAULT_POLICY, type AttendanceDay, type DayContext } from '@hajiri/attendance-engine';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { Db } from '../../db/pool.js';
import * as repo from './repo.js';

export interface RecomputeResult {
  day: AttendanceDay;
  version: number;
  changed: boolean;
  locked: boolean;
}

export class NotFoundError extends Error {}

/**
 * Recompute one (employee, date): load inputs, run the pure engine, write a new version only if inputs_hash changed.
 * Locked days (payroll period closed) are never rewritten (plan §10.3).
 */
export async function recomputeDay(db: Db, employeeId: string, localDate: string): Promise<RecomputeResult> {
  const employee = await repo.getEmployee(db, employeeId);
  if (!employee) throw new NotFoundError(`employee ${employeeId} not found`);

  const [branchId, shift, punches] = await Promise.all([
    repo.getBranchId(db, employeeId, localDate),
    repo.resolveShift(db, employeeId, localDate),
    repo.getPunchesAround(db, employeeId, localDate),
  ]);
  const [holiday, otWeek] = await Promise.all([
    repo.isHoliday(db, employee.company_id, branchId, localDate, employee.gender),
    repo.otMinutesThisWeek(db, employeeId, localDate),
  ]);

  const ctx: DayContext = {
    holiday,
    weeklyOff: employee.weekly_off_days.includes(weekdayOf(localDate)),
    leave: 'none',          // leave module (Phase 2) will supply this
    wfh: false,             // regularisation module (Phase 2)
    otEligible: employee.ot_eligible,
    otPreapprovedMin: null, // OT pre-approval (Phase 2)
    otMinutesThisWeek: otWeek,
  };

  const day = computeDay({ employeeId, localDate, shift, punches, ctx, policy: DEFAULT_POLICY });

  const latest = await repo.latestVersion(db, employeeId, localDate);
  if (latest?.locked_at) return { day, version: latest.version, changed: false, locked: true };
  if (latest && latest.inputs_hash === day.inputsHash) return { day, version: latest.version, changed: false, locked: false };

  const version = (latest?.version ?? 0) + 1;
  const bsDate = await repo.bsDateFor(db, localDate);
  await db.query(
    `INSERT INTO attendance_day (employee_id, local_date, bs_date, version, shift_id, status, first_in, last_out,
       worked_min, break_min, late_min, early_min, ot_min, ot, flags, punch_ids, inputs_hash)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)`,
    [employeeId, localDate, bsDate, version, shift?.id ?? null, day.status,
      day.firstInMs === null ? null : new Date(day.firstInMs), day.lastOutMs === null ? null : new Date(day.lastOutMs),
      day.workedMin, day.breakMin, day.lateMin, day.earlyMin, day.ot.minutes, JSON.stringify(day.ot), JSON.stringify(day.flags),
      day.punchIds, day.inputsHash]);
  return { day, version, changed: true, locked: false };
}

export interface AttendanceDayRow {
  employee_id: string; local_date: string; bs_date: string | null; version: number; status: string;
  first_in: Date | null; last_out: Date | null; worked_min: number; break_min: number; late_min: number; early_min: number;
  ot_min: number; ot: unknown; flags: unknown; computed_at: Date; locked_at: Date | null;
}

/** Latest version of each day in [from, to]. */
export async function listDays(db: Db, employeeId: string, from: string, to: string): Promise<AttendanceDayRow[]> {
  const r = await db.query<AttendanceDayRow>(
    `SELECT DISTINCT ON (local_date) employee_id, local_date, bs_date, version, status, first_in, last_out,
            worked_min, break_min, late_min, early_min, ot_min, ot, flags, computed_at, locked_at
       FROM attendance_day WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3
      ORDER BY local_date, version DESC`,
    [employeeId, from, to]);
  return r.rows;
}
