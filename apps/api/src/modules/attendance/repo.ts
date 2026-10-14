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
  if (row) return { cover: row.half_day ?? 'full', paid: row.paid, leaveTypeId: row.leave_type_id };
  // An approved half-day regularisation (plan §5.4) is a paid half day; the request does not say which half, so the
  // afternoon is assumed (the employee worked the morning).
  const reg = await db.query(
    `SELECT 1 FROM regularisation_request WHERE employee_id = $1 AND local_date = $2 AND kind = 'half_day' AND status = 'approved' LIMIT 1`, [employeeId, localDate]);
  if (reg.rows.length > 0) return { cover: 'second_half', paid: true, leaveTypeId: null };
  return { cover: 'none', paid: true, leaveTypeId: null };
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

// ---------------------------------------------------------------- bulk inputs for previews (same rules as the per-day functions above)

export interface RangeEmployee extends EmployeeRow {}
export interface DayInputs {
  shift: (Shift & { id: string }) | null; punches: Punch[]; holiday: boolean; leave: LeaveCover; wfh: boolean;
  otPreapprovedMin: number | null; otMinutesThisWeek: number; stored: { status: string; late_min: number; ot_min: number } | null;
}

const addDays = (d: string, n: number) => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const iso = (v: unknown) => (v instanceof Date ? v.toISOString().slice(0, 10) : String(v).slice(0, 10));
const inRange = (range: string, d: string) => {
  // Postgres daterange text: [2026-01-01,2026-02-01) or [2026-01-01,)
  const m = /^[[(]([^,]*),([^)\]]*)[)\]]$/.exec(range);
  if (!m) return false;
  return (!m[1] || d >= m[1]) && (!m[2] || d < m[2]);
};

/**
 * Everything computeDay needs for every (employee, date) in a range, loaded with a fixed number of queries instead of
 * about ten per day. Used by the policy preview, which may cover 200 employees × 31 days.
 */
export async function loadRangeInputs(db: Queryable, companyId: string, employees: RangeEmployee[], dates: string[]): Promise<Map<string, DayInputs>> {
  const ids = employees.map((e) => e.id);
  const from = dates[0]!; const to = dates[dates.length - 1]!;
  const weekStart = addDays(from, -new Date(`${from}T00:00:00Z`).getUTCDay());
  const [assign, regs, roster, sa, punches, leave, ot, stored, holidays] = await Promise.all([
    db.query<{ employee_id: string; branch_id: string | null; valid: string }>('SELECT employee_id, branch_id, valid::text FROM employee_assignment WHERE employee_id = ANY($1) AND valid && daterange($2::date, $3::date, \'[]\')', [ids, from, to]),
    db.query<{ employee_id: string; local_date: Date; kind: string; shift_id: string | null; decided_at: Date | null }>(
      `SELECT employee_id, local_date, kind, shift_id, decided_at FROM regularisation_request WHERE employee_id = ANY($1) AND status = 'approved' AND local_date BETWEEN $2 AND $3
        AND kind IN ('shift_change', 'half_day', 'wfh', 'on_duty') ORDER BY decided_at DESC NULLS LAST`, [ids, from, to]),
    db.query<{ employee_id: string; local_date: Date; shift_id: string | null }>('SELECT employee_id, local_date, shift_id FROM roster WHERE employee_id = ANY($1) AND local_date BETWEEN $2 AND $3', [ids, from, to]),
    db.query<{ employee_id: string; shift_id: string; valid: string }>('SELECT employee_id, shift_id, valid::text FROM shift_assignment WHERE employee_id = ANY($1) AND valid && daterange($2::date, $3::date, \'[]\')', [ids, from, to]),
    db.query<{ id: string; employee_id: string; punched_at: Date; direction: Punch['direction']; verify_mode: string; source: string }>(
      `SELECT p.id, p.employee_id, p.punched_at, p.direction, p.verify_mode, p.source FROM punch_event p
        WHERE p.employee_id = ANY($1) AND p.punched_at >= $2 AND p.punched_at < $3 AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
        ORDER BY p.punched_at, p.id`, [ids, new Date(localMidnightMs(from) - DAY_MIN * MIN_MS), new Date(localMidnightMs(to) + 3 * DAY_MIN * MIN_MS)]),
    db.query<{ employee_id: string; from_date: Date; to_date: Date; half_day: 'first_half' | 'second_half' | null }>(
      `SELECT lr.employee_id, lr.from_date, lr.to_date, lr.half_day FROM leave_request lr WHERE lr.employee_id = ANY($1) AND lr.status = 'approved'
        AND lr.to_date >= $2 AND lr.from_date <= $3 ORDER BY lr.decided_at DESC NULLS LAST`, [ids, from, to]),
    db.query<{ employee_id: string; local_date: Date; approved_min: number | null }>(
      `SELECT employee_id, local_date, approved_min FROM ot_request WHERE employee_id = ANY($1) AND status = 'approved' AND local_date BETWEEN $2 AND $3 ORDER BY decided_at DESC NULLS LAST`, [ids, from, to]),
    db.query<{ employee_id: string; local_date: Date; status: string; late_min: number; ot_min: number }>(
      `SELECT DISTINCT ON (employee_id, local_date) employee_id, local_date, status, late_min, ot_min FROM attendance_day
        WHERE employee_id = ANY($1) AND local_date BETWEEN $2 AND $3 ORDER BY employee_id, local_date, version DESC`, [ids, weekStart, addDays(to, 6)]),
    db.query<{ local_date: Date; branch_id: string | null; gender_scope: string }>(
      'SELECT local_date, branch_id, gender_scope FROM holiday WHERE company_id = $1 AND NOT optional AND local_date BETWEEN $2 AND $3', [companyId, from, to]),
  ]);
  const shiftIds = [...new Set([...regs.rows.map((r) => r.shift_id), ...roster.rows.map((r) => r.shift_id), ...sa.rows.map((r) => r.shift_id)].filter((x): x is string => !!x))];
  const shifts = new Map((await db.query<ShiftRow>(`SELECT ${SHIFT_COLS} FROM shift WHERE id = ANY($1)`, [shiftIds])).rows.map((r) => [r.id, toShift(r)]));

  const key = (e: string, d: string) => `${e}|${d}`;
  const first = <T>(rows: T[], k: (r: T) => string) => { const m = new Map<string, T>(); for (const r of rows) if (!m.has(k(r))) m.set(k(r), r); return m; };
  const shiftReg = first(regs.rows.filter((r) => r.kind === 'shift_change'), (r) => key(r.employee_id, iso(r.local_date)));
  const halfReg = new Set(regs.rows.filter((r) => r.kind === 'half_day').map((r) => key(r.employee_id, iso(r.local_date))));
  const wfhReg = new Set(regs.rows.filter((r) => r.kind === 'wfh' || r.kind === 'on_duty').map((r) => key(r.employee_id, iso(r.local_date))));
  const rosterBy = first(roster.rows, (r) => key(r.employee_id, iso(r.local_date)));
  const otBy = first(ot.rows, (r) => key(r.employee_id, iso(r.local_date)));
  const storedBy = new Map(stored.rows.map((r) => [key(r.employee_id, iso(r.local_date)), r]));
  const punchesBy = new Map<string, Punch[]>();
  for (const p of punches.rows) {
    const list = punchesBy.get(p.employee_id) ?? [];
    list.push({ id: p.id, at: p.punched_at.getTime(), direction: p.direction, verifyMode: p.verify_mode, source: p.source });
    punchesBy.set(p.employee_id, list);
  }

  const out = new Map<string, DayInputs>();
  for (const e of employees) {
    const all = punchesBy.get(e.id) ?? [];
    for (const d of dates) {
      const k = key(e.id, d);
      const reg = shiftReg.get(k);
      let shiftId: string | null;
      if (reg) shiftId = reg.shift_id;
      else if (rosterBy.has(k)) shiftId = rosterBy.get(k)!.shift_id;
      else shiftId = sa.rows.find((r) => r.employee_id === e.id && inRange(r.valid, d))?.shift_id ?? null;
      const branchId = assign.rows.find((r) => r.employee_id === e.id && inRange(r.valid, d))?.branch_id ?? null;
      const lv = leave.rows.find((r) => r.employee_id === e.id && iso(r.from_date) <= d && d <= iso(r.to_date));
      const leaveCover: LeaveCover = lv ? (lv.half_day ?? 'full') : halfReg.has(k) ? 'second_half' : 'none';
      const ws = addDays(d, -new Date(`${d}T00:00:00Z`).getUTCDay());
      let week = 0;
      for (let i = 0; i < 7; i++) { const wd = addDays(ws, i); if (wd !== d) week += storedBy.get(key(e.id, wd))?.ot_min ?? 0; }
      const lo = localMidnightMs(d) - DAY_MIN * MIN_MS; const hi = localMidnightMs(d) + 3 * DAY_MIN * MIN_MS;
      out.set(k, {
        shift: shiftId ? shifts.get(shiftId) ?? null : null,
        punches: all.filter((p) => p.at >= lo && p.at < hi),
        holiday: holidays.rows.some((h) => iso(h.local_date) === d && (h.branch_id === null || h.branch_id === branchId) && (h.gender_scope === 'all' || h.gender_scope === e.gender)),
        leave: leaveCover, wfh: wfhReg.has(k), otPreapprovedMin: otBy.get(k)?.approved_min ?? null, otMinutesThisWeek: week,
        stored: storedBy.get(k) ?? null,
      });
    }
  }
  return out;
}
