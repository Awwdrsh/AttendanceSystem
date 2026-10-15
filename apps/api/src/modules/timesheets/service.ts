import { localDateOf, MIN_MS, roundMinutes } from '@hajiri/attendance-engine';
import { z } from 'zod';
import { bsLabel, bsRange } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { ConflictError, NotFoundError } from '../../errors.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { registerHandler, type DecisionContext } from '../workflow/service.js';

// ---------------------------------------------------------------- policy

export const timePolicySchema = z.object({
  enabled: z.boolean().default(true),
  requireProject: z.boolean().default(false),
  /** Jibble-style rounding applied to each entry in approvals and reports; the raw minutes are always kept. */
  roundingStep: z.union([z.literal(0), z.literal(5), z.literal(6), z.literal(10), z.literal(15)]).default(0),
  roundingMode: z.enum(['nearest', 'up', 'down']).default('nearest'),
  fridayReminder: z.boolean().default(true),
  /** A timer left running longer than this is stopped at this length and the employee is told. */
  autoStopHours: z.number().int().min(1).max(24).default(12),
});
export type TimePolicy = z.infer<typeof timePolicySchema>;
export const TIME_POLICY_KEY = 'time.policy';

export async function loadTimePolicy(db: Queryable, companyId: string): Promise<TimePolicy> {
  const r = await db.query<{ value: unknown }>('SELECT value FROM company_setting WHERE company_id = $1 AND key = $2', [companyId, TIME_POLICY_KEY]);
  const parsed = timePolicySchema.safeParse(r.rows[0]?.value ?? {});
  return parsed.success ? parsed.data : timePolicySchema.parse({});
}

export function roundEntry(min: number, p: Pick<TimePolicy, 'roundingStep' | 'roundingMode'>): number {
  return roundMinutes(min, p.roundingStep, p.roundingMode);
}

// ---------------------------------------------------------------- weeks (Sunday to Saturday)

export function addDaysIso(iso: string, days: number): string {
  return new Date(new Date(`${iso}T00:00:00Z`).getTime() + days * 86_400_000).toISOString().slice(0, 10);
}
export function weekStartOf(iso: string): string {
  return addDaysIso(iso, -new Date(`${iso}T00:00:00Z`).getUTCDay());
}
export function isSunday(iso: string): boolean { return new Date(`${iso}T00:00:00Z`).getUTCDay() === 0; }

/** Submitted and approved weeks are locked: their entries can no longer be added, changed or removed. */
export async function assertWeekOpen(db: Queryable, employeeId: string, localDate: string): Promise<void> {
  const r = await db.query<{ status: string }>(
    `SELECT status FROM timesheet WHERE employee_id = $1 AND week_start = $2 AND status IN ('submitted', 'approved')`, [employeeId, weekStartOf(localDate)]);
  if (r.rows[0]) {
    throw new ConflictError(r.rows[0].status === 'approved'
      ? 'This week\'s timesheet is approved and locked.'
      : 'This week\'s timesheet is waiting for approval. Withdraw it to make changes.');
  }
}

// ---------------------------------------------------------------- auto-stop

/**
 * Stop timers that have run longer than the company's limit, ending them exactly at the limit (so a forgotten timer
 * never logs a whole night), and tell the employee. Optionally limited to one employee for the lazy check on read.
 */
export async function autoStopStaleTimers(db: Queryable, now = new Date(), employeeId: string | null = null): Promise<number> {
  const r = await db.query<{ id: string; employee_id: string; company_id: string; local_date: string; hours: number }>(
    `WITH lim AS (
       SELECT c.id AS company_id, coalesce((s.value->>'autoStopHours')::int, 12) AS hours
         FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'time.policy')
     UPDATE time_entry t SET ended_at = t.started_at + make_interval(hours => lim.hours), duration_min = lim.hours * 60,
            auto_stopped = true, updated_at = now()
       FROM lim
      WHERE lim.company_id = t.company_id AND t.ended_at IS NULL AND t.duration_min IS NULL
        AND t.started_at <= $1::timestamptz - make_interval(hours => lim.hours)
        AND ($2::uuid IS NULL OR t.employee_id = $2)
      RETURNING t.id, t.employee_id, t.company_id, to_char(t.local_date, 'YYYY-MM-DD') AS local_date, lim.hours`, [now, employeeId]);
  for (const t of r.rows) {
    await notify(db, {
      companyId: t.company_id, userId: await userIdForEmployee(db, t.employee_id), employeeId: t.employee_id, template: 'time.timer_auto_stopped',
      title: 'Timer stopped automatically', body: `Your timer from ${bsLabel(t.local_date)} ran for more than ${t.hours} hours and was stopped. Check the entry and correct it if needed.`,
      payload: { entryId: t.id },
    });
  }
  return r.rows.length;
}

// ---------------------------------------------------------------- Friday reminder

/** On Fridays (Nepal time), remind people who track time and have not submitted this week's timesheet. Once per week. */
export async function fridayTimesheetReminders(db: Queryable, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  if (new Date(`${today}T00:00:00Z`).getUTCDay() !== 5) return 0;
  const week = weekStartOf(today);
  const r = await db.query<{ employee_id: string; company_id: string; user_id: string }>(
    `SELECT DISTINCT t.employee_id, t.company_id, u.id AS user_id
       FROM time_entry t
       JOIN employee e ON e.id = t.employee_id AND e.status = 'active'
       JOIN app_user u ON u.employee_id = t.employee_id AND u.status = 'active'
       LEFT JOIN company_setting s ON s.company_id = t.company_id AND s.key = 'time.policy'
      WHERE t.local_date >= $1::date - 28
        AND coalesce((s.value->>'enabled')::boolean, true) AND coalesce((s.value->>'fridayReminder')::boolean, true)
        AND NOT EXISTS (SELECT 1 FROM timesheet ts WHERE ts.employee_id = t.employee_id AND ts.week_start = $1::date AND ts.status IN ('submitted', 'approved'))
        AND NOT EXISTS (SELECT 1 FROM notification_outbox n WHERE n.user_id = u.id AND n.template = 'timesheet.reminder' AND n.payload->>'weekStart' = $2)`,
    [week, week]);
  for (const x of r.rows) {
    await notify(db, {
      companyId: x.company_id, userId: x.user_id, employeeId: x.employee_id, template: 'timesheet.reminder',
      title: 'Submit your timesheet', body: `Your timesheet for ${bsRange(week, addDaysIso(week, 6))} has not been submitted yet. Check your hours and submit it before the week ends.`,
      payload: { weekStart: week }, channels: ['push'],
    });
  }
  return r.rows.length;
}

export async function dailyTimeJobs(db: Queryable, now: Date): Promise<{ autoStopped: number; reminded: number }> {
  return { autoStopped: await autoStopStaleTimers(db, now), reminded: await fridayTimesheetReminders(db, now) };
}

// ---------------------------------------------------------------- approval

export async function applyTimesheetDecision(db: Queryable, id: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ employee_id: string; company_id: string; status: string; week_start: string }>(
    `SELECT employee_id, company_id, status, to_char(week_start, 'YYYY-MM-DD') AS week_start FROM timesheet WHERE id = $1 FOR UPDATE`, [id]);
  const t = r.rows[0];
  if (!t) throw new NotFoundError('timesheet not found');
  if (t.status !== 'submitted') throw new ConflictError(`timesheet already ${t.status}`);
  await db.query('UPDATE timesheet SET status = $2, decided_at = now(), decided_by = $3, decision_comment = $4 WHERE id = $1', [id, state, ctx.actorId, ctx.reason]);
  await notify(db, {
    companyId: t.company_id, userId: await userIdForEmployee(db, t.employee_id), employeeId: t.employee_id, template: `timesheet.${state}`,
    title: state === 'approved' ? 'Timesheet approved' : 'Timesheet returned',
    body: `Your timesheet for ${bsRange(t.week_start, addDaysIso(t.week_start, 6))} was ${state === 'approved' ? 'approved' : 'returned for changes'}${ctx.reason ? `: ${ctx.reason}` : '.'}`,
    payload: { timesheetId: id, weekStart: t.week_start }, channels: ['push'],
  });
}
registerHandler('timesheet', applyTimesheetDecision);

/** Inbox card details for a timesheet approval. */
export async function summariseTimesheet(db: Queryable, id: string): Promise<Record<string, unknown>> {
  const r = await db.query<{ week_start: string; total_min: number; note: string | null; employee_id: string }>(
    `SELECT to_char(week_start, 'YYYY-MM-DD') AS week_start, total_min, note, employee_id FROM timesheet WHERE id = $1`, [id]);
  const t = r.rows[0];
  if (!t) return {};
  const byProject = await db.query<{ name: string; min: number }>(
    `SELECT coalesce(p.name, 'No project') AS name, sum(e.duration_min)::int AS min
       FROM time_entry e LEFT JOIN time_project p ON p.id = e.project_id
      WHERE e.employee_id = $1 AND e.local_date BETWEEN $2::date AND $2::date + 6 AND e.duration_min IS NOT NULL
      GROUP BY 1 ORDER BY 2 DESC LIMIT 5`, [t.employee_id, t.week_start]);
  const worked = await db.query<{ min: number }>(
    `SELECT coalesce(sum(worked_min), 0)::int AS min FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2::date AND $2::date + 6`,
    [t.employee_id, t.week_start]);
  return { weekStart: t.week_start, weekEnd: addDaysIso(t.week_start, 6), totalMin: t.total_min, attendanceMin: worked.rows[0]?.min ?? 0, note: t.note, projects: byProject.rows };
}

export const minutesFromMs = (ms: number) => Math.max(1, Math.round(ms / MIN_MS));
