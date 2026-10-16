import { localDateOf, localTimeMs, MIN_MS } from '@hajiri/attendance-engine';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { HttpError } from '../../errors.js';
import { resolveShift } from '../attendance/repo.js';
import { nptLocalTime } from '../punch/devices.js';
import { extrasPolicy } from './policy.js';

/**
 * Optional rule (off by default): the first clock-in of a working day later than shift start + grace (+ the extra
 * minutes HR allows) is refused, and the employee is asked to raise a time request with the time they arrived. Clock-outs,
 * second clock-ins, holidays, weekly offs and days with approved first-half or hourly leave at the start are never blocked.
 */
export async function assertClockInAllowed(db: Queryable, input: { companyId: string; employeeId: string; atMs: number; direction: string }): Promise<void> {
  if (input.direction !== 'in') return;
  const policy = await extrasPolicy(db, input.companyId);
  if (!policy.blockLateClockIn) return;
  const localDate = localDateOf(input.atMs);
  const already = await db.query(`SELECT 1 FROM punch_event WHERE employee_id = $1 AND local_date = $2 AND direction = 'in' LIMIT 1`, [input.employeeId, localDate]);
  if (already.rows.length) return;
  const emp = await db.query<{ weekly_off_days: number[]; gender: string | null; branch_id: string | null }>(
    `SELECT e.weekly_off_days, e.gender, a.branch_id FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date WHERE e.id = $1`,
    [input.employeeId, localDate]);
  const e = emp.rows[0];
  if (!e) return;
  if (e.weekly_off_days.includes(weekdayOf(localDate))) return;
  const holiday = await db.query(
    `SELECT 1 FROM holiday WHERE company_id = $1 AND local_date = $2 AND (branch_id IS NULL OR branch_id = $3) AND NOT optional AND (gender_scope = 'all' OR gender_scope = $4) LIMIT 1`,
    [input.companyId, localDate, e.branch_id, e.gender]);
  if (holiday.rows.length) return;
  const shift = await resolveShift(db, input.employeeId, localDate);
  if (!shift) return;
  const leave = await db.query(
    `SELECT 1 FROM leave_request WHERE employee_id = $1 AND status = 'approved' AND $2::date BETWEEN from_date AND to_date
        AND ((half_day IS NULL AND from_time IS NULL) OR half_day = 'first_half'
             OR (from_time IS NOT NULL AND from_time <= $3::time + make_interval(mins => $4::int))) LIMIT 1`,
    [input.employeeId, localDate, shift.start, shift.graceInMin]);
  if (leave.rows.length) return;
  const limit = localTimeMs(localDate, shift.start) + (shift.graceInMin + policy.blockAfterMin) * MIN_MS;
  if (input.atMs <= limit) return;
  const closedAt = nptLocalTime(limit).slice(11, 16);
  throw new HttpError(409, `Clock-in closed at ${closedAt} (your shift starts at ${shift.start}). Raise a time request with the time you arrived and your manager will approve it.`, {
    code: 'late_blocked', shiftStart: shift.start, closedAt, localDate,
  });
}
