import { computeDay, DEFAULT_POLICY, type AttendanceDay, type AttendancePolicy, type DayContext } from '@hajiri/attendance-engine';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { Db, Queryable } from '../../db/pool.js';
import { NotFoundError } from '../../errors.js';
import * as repo from './repo.js';

export interface RecomputeResult {
  day: AttendanceDay;
  version: number;
  changed: boolean;
  locked: boolean;
}

function policyFromSettings(v: Record<string, unknown>): AttendancePolicy {
  const n = (k: string, d: number) => (typeof v[k] === 'number' ? (v[k] as number) : d);
  const b = (k: string, d: boolean) => (typeof v[k] === 'boolean' ? (v[k] as boolean) : d);
  return {
    version: typeof v.version === 'string' ? v.version : DEFAULT_POLICY.version,
    holidayWork: (v.holidayWork as AttendancePolicy['holidayWork']) ?? DEFAULT_POLICY.holidayWork,
    ot: {
      ...DEFAULT_POLICY.ot,
      minThresholdMin: n('otMinThresholdMin', DEFAULT_POLICY.ot.minThresholdMin),
      roundingMin: n('otRoundingMin', DEFAULT_POLICY.ot.roundingMin),
      requirePreapproval: b('requirePreapproval', DEFAULT_POLICY.ot.requirePreapproval),
      nightWindowEnabled: b('nightWindowEnabled', false),
      nightPremiumRate: n('nightPremiumRate', 0),
      // Statutory caps (Sec 30(1)) are never raised from settings.
      capDayMin: Math.min(240, n('otCapDayMin', 240)),
      capWeekMin: Math.min(1440, n('otCapWeekMin', 1440)),
    },
  };
}

/**
 * Recompute one (employee, date) under an advisory lock: load inputs, run the pure engine, write a new version only
 * if inputs_hash changed. Locked days (payroll period closed) are never rewritten (plan §10.3).
 */
export async function recomputeDay(db: Db, employeeId: string, localDate: string): Promise<RecomputeResult> {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`recompute:${employeeId}:${localDate}`]);
    const result = await recomputeInTx(client, employeeId, localDate);
    await client.query('COMMIT');
    return result;
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();
  }
}

async function recomputeInTx(db: Queryable, employeeId: string, localDate: string): Promise<RecomputeResult> {
  const employee = await repo.getEmployee(db, employeeId);
  if (!employee) throw new NotFoundError(`employee ${employeeId} not found`);

  const [branchId, shiftResolved, punches, leave, wfh, otPre, otWeek, policyRaw] = await Promise.all([
    repo.getBranchId(db, employeeId, localDate),
    repo.resolveShift(db, employeeId, localDate),
    repo.getPunchesAround(db, employeeId, localDate),
    repo.approvedLeave(db, employeeId, localDate),
    repo.approvedWfh(db, employeeId, localDate),
    repo.approvedOtMinutes(db, employeeId, localDate),
    repo.otMinutesThisWeek(db, employeeId, localDate),
    repo.attendancePolicy(db, employee.company_id),
  ]);
  const holiday = await repo.isHoliday(db, employee.company_id, branchId, localDate, employee.gender);

  // Before joining or after exit there is no shift to work.
  const employed = (!employee.join_date || localDate >= employee.join_date) && (!employee.exit_date || localDate <= employee.exit_date);
  const shift = employed ? shiftResolved : null;

  const ctx: DayContext = {
    holiday,
    weeklyOff: employee.weekly_off_days.includes(weekdayOf(localDate)),
    leave: leave.cover,
    wfh,
    otEligible: employee.ot_eligible,
    otPreapprovedMin: otPre,
    otMinutesThisWeek: otWeek,
  };

  const policy = policyFromSettings(policyRaw);
  const day = computeDay({ employeeId, localDate, shift, punches, ctx, policy });
  const flags = leave.cover !== 'none' && !leave.paid ? [...day.flags, { kind: 'UnpaidLeave' as const }] : day.flags;

  const latest = await repo.latestVersion(db, employeeId, localDate);
  if (latest?.locked_at) return { day, version: latest.version, changed: false, locked: true };
  if (latest && latest.inputs_hash === day.inputsHash) return { day, version: latest.version, changed: false, locked: false };

  const version = (latest?.version ?? 0) + 1;
  const bsDate = await repo.bsDateFor(db, localDate);
  const ins = await db.query<{ id: string }>(
    `INSERT INTO attendance_day (employee_id, local_date, bs_date, version, shift_id, status, first_in, last_out,
       worked_min, break_min, late_min, early_min, ot_min, ot, flags, punch_ids, inputs_hash)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17) RETURNING id`,
    [employeeId, localDate, bsDate, version, shift?.id ?? null, day.status,
      day.firstInMs === null ? null : new Date(day.firstInMs), day.lastOutMs === null ? null : new Date(day.lastOutMs),
      day.workedMin, day.breakMin, day.lateMin, day.earlyMin, day.ot.minutes, JSON.stringify(day.ot), JSON.stringify(flags),
      day.punchIds, day.inputsHash]);

  // OT detected -> slip (plan §10.4 flow). An already approved/posted slip is never overwritten by a recompute.
  if (day.ot.minutes > 0) {
    await db.query(
      `INSERT INTO ot_slip (employee_id, local_date, attendance_day_id, detected_min, multiplier, day_type)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (employee_id, local_date) DO UPDATE SET attendance_day_id = EXCLUDED.attendance_day_id, detected_min = EXCLUDED.detected_min,
         multiplier = EXCLUDED.multiplier, day_type = EXCLUDED.day_type
       WHERE ot_slip.status = 'detected'`,
      [employeeId, localDate, ins.rows[0]!.id, day.ot.minutes, day.ot.multiplier, day.ot.dayType]);
  } else {
    await db.query(`DELETE FROM ot_slip WHERE employee_id = $1 AND local_date = $2 AND status = 'detected'`, [employeeId, localDate]);
  }
  return { day: { ...day, flags }, version, changed: true, locked: false };
}

/** Queue (employee, date) pairs for the worker (plan §10.3 triggers). Idempotent. */
export async function enqueueRecompute(db: Queryable, items: Array<{ employeeId: string; localDate: string }>, reason: string): Promise<void> {
  for (const it of items) {
    await db.query(
      `INSERT INTO recompute_queue (employee_id, local_date, reason) VALUES ($1, $2, $3) ON CONFLICT (employee_id, local_date) DO UPDATE SET enqueued_at = now(), reason = EXCLUDED.reason`,
      [it.employeeId, it.localDate, reason]);
  }
}

/** Every date in [from, to] inclusive, as 'YYYY-MM-DD'. */
export function dateRange(from: string, to: string): string[] {
  const out: string[] = [];
  const start = new Date(`${from}T00:00:00Z`).getTime();
  const end = new Date(`${to}T00:00:00Z`).getTime();
  for (let t = start; t <= end; t += 86_400_000) out.push(new Date(t).toISOString().slice(0, 10));
  return out;
}

export interface AttendanceDayRow {
  id: string; employee_id: string; local_date: string; bs_date: string | null; version: number; status: string; shift_id: string | null;
  first_in: Date | null; last_out: Date | null; worked_min: number; break_min: number; late_min: number; early_min: number;
  ot_min: number; ot: unknown; flags: unknown; computed_at: Date; locked_at: Date | null;
}

/** Latest version of each day in [from, to]. */
export async function listDays(db: Queryable, employeeId: string, from: string, to: string): Promise<AttendanceDayRow[]> {
  const r = await db.query<AttendanceDayRow>(
    `SELECT id, employee_id, local_date, bs_date, version, status, shift_id, first_in, last_out,
            worked_min, break_min, late_min, early_min, ot_min, ot, flags, computed_at, locked_at
       FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3 ORDER BY local_date`,
    [employeeId, from, to]);
  return r.rows;
}

/** Lock every latest attendance_day in a period for a company (payroll pre-flight). Returns rows locked. */
export async function lockAttendance(db: Queryable, companyId: string, from: string, to: string): Promise<number> {
  const r = await db.query(
    `UPDATE attendance_day a SET locked_at = now()
       FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
      WHERE a.id = v.id AND e.company_id = $1 AND v.local_date BETWEEN $2 AND $3 AND a.locked_at IS NULL`,
    [companyId, from, to]);
  return r.rowCount ?? 0;
}
