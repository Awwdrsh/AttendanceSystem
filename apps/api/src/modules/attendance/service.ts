import { computeDay, DEFAULT_POLICY, localDateOf, NPT_OFFSET_MIN, MIN_MS, type AttendanceDay, type AttendancePolicy, type DayContext } from '@hajiri/attendance-engine';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { Db, Queryable } from '../../db/pool.js';
import { BadRequestError, NotFoundError } from '../../errors.js';
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

  const [branchId, shiftResolved, punches, leave, wfh, otPre, otWeek, policyRaw, shortLeave] = await Promise.all([
    repo.getBranchId(db, employeeId, localDate),
    repo.resolveShift(db, employeeId, localDate),
    repo.getPunchesAround(db, employeeId, localDate),
    repo.approvedLeave(db, employeeId, localDate),
    repo.approvedWfh(db, employeeId, localDate),
    repo.approvedOtMinutes(db, employeeId, localDate),
    repo.otMinutesThisWeek(db, employeeId, localDate),
    repo.attendancePolicy(db, employee.company_id),
    repo.approvedShortLeave(db, employeeId, localDate),
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
    // Only present when there is hourly leave, so days without it keep their existing inputs hash.
    ...(shortLeave.length ? { shortLeave } : {}),
  };

  const policy = policyFromSettings(policyRaw);
  const day = computeDay({ employeeId, localDate, shift, punches, ctx, policy });
  const flags = leave.cover !== 'none' && !leave.paid ? [...day.flags, { kind: 'UnpaidLeave' as const }] : day.flags;

  const latest = await repo.latestVersion(db, employeeId, localDate);
  if (latest?.locked_at) return { day, version: latest.version, changed: false, locked: true };
  // A payroll period whose attendance is locked is frozen even for days that had no row yet.
  if (!latest && (await periodLocked(db, employee.company_id, localDate))) return { day, version: 0, changed: false, locked: true };
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
  if (items.length === 0) return;
  // One statement per 5,000 pairs (unnest) instead of one round trip per pair.
  for (let i = 0; i < items.length; i += 5000) {
    const chunk = items.slice(i, i + 5000);
    await db.query(
      `INSERT INTO recompute_queue (employee_id, local_date, reason)
       SELECT e, d, $3 FROM unnest($1::uuid[], $2::date[]) AS t(e, d)
       ON CONFLICT (employee_id, local_date) DO UPDATE SET enqueued_at = now(), reason = EXCLUDED.reason`,
      [chunk.map((x) => x.employeeId), chunk.map((x) => x.localDate), reason]);
  }
}

/** Every date in [from, to] inclusive, as 'YYYY-MM-DD'. */
/** Longest range any caller may expand day by day (about ten years); stops a request like 0001-01-01..9999-12-31 from pinning the CPU. */
export const MAX_RANGE_DAYS = 3660;

export function dateRange(from: string, to: string): string[] {
  const out: string[] = [];
  const start = new Date(`${from}T00:00:00Z`).getTime();
  const end = new Date(`${to}T00:00:00Z`).getTime();
  if ((end - start) / 86_400_000 >= MAX_RANGE_DAYS) throw new BadRequestError(`date range is too long (max ${MAX_RANGE_DAYS} days)`);
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

async function periodLocked(db: Queryable, companyId: string, localDate: string): Promise<boolean> {
  const r = await db.query('SELECT 1 FROM payroll_period WHERE company_id = $1 AND attendance_locked_at IS NOT NULL AND $2::date BETWEEN start_ad AND end_ad LIMIT 1', [companyId, localDate]);
  return r.rows.length > 0;
}

/**
 * Is this employee's day frozen by payroll? True when the day's latest version is locked, or when the payroll period
 * covering it has had its attendance locked (even if no row existed for that day at lock time).
 */
export async function isAttendanceLocked(db: Queryable, employeeId: string, localDate: string): Promise<boolean> {
  const r = await db.query(
    `SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2 AND locked_at IS NOT NULL
     UNION ALL
     SELECT 1 FROM payroll_period p JOIN employee e ON e.company_id = p.company_id
      WHERE e.id = $1 AND p.attendance_locked_at IS NOT NULL AND $2::date BETWEEN p.start_ad AND p.end_ad
     LIMIT 1`, [employeeId, localDate]);
  return r.rows.length > 0;
}

/**
 * Days a punch at `atMs` can belong to: its own calendar date and, for a morning punch, the previous date (a night shift
 * or an overtime punch-out after midnight belongs to the day the shift started).
 */
export function datesForPunch(atMs: number): string[] {
  const date = localDateOf(atMs);
  const hour = new Date(atMs + NPT_OFFSET_MIN * MIN_MS).getUTCHours();
  if (hour >= 12) return [date];
  const prev = new Date(Date.parse(`${date}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
  return [prev, date];
}

/** Recompute every day a punch can affect; returns the day that now contains the punch (or the punch's own date). */
export async function recomputeForPunch(db: Db, employeeId: string, atMs: number, punchId: string): Promise<RecomputeResult> {
  const results: RecomputeResult[] = [];
  for (const d of datesForPunch(atMs)) results.push(await recomputeDay(db, employeeId, d));
  return results.find((r) => r.day.punchIds.includes(punchId)) ?? results[results.length - 1]!;
}

/** Most days a request handler recomputes inline; larger ranges are left to the worker. */
export const INLINE_RECOMPUTE_MAX = 62;

/**
 * Process this employee's queued recomputes right away (after an approval commits) so the UI shows the corrected day
 * immediately. Skipped when more than INLINE_RECOMPUTE_MAX days are queued; failures stay queued for the worker.
 */
export async function recomputeQueuedFor(db: Db, employeeId: string): Promise<number> {
  const q = await db.query<{ local_date: string }>(`SELECT local_date::text FROM recompute_queue WHERE employee_id = $1 AND enqueued_at <= now() ORDER BY local_date LIMIT $2`, [employeeId, INLINE_RECOMPUTE_MAX + 1]);
  if (q.rows.length === 0 || q.rows.length > INLINE_RECOMPUTE_MAX) return 0;
  let n = 0;
  for (const { local_date } of q.rows) {
    try {
      await recomputeDay(db, employeeId, local_date);
      await db.query('DELETE FROM recompute_queue WHERE employee_id = $1 AND local_date = $2', [employeeId, local_date]);
      n++;
    } catch { /* left queued; the worker retries with backoff */ }
  }
  return n;
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
