import { localDateOf, localTimeMs, parseHHMM } from '@hajiri/attendance-engine';
import { Router, type Request } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import { bsRange } from '../../bsdate.js';
import type { Db, Queryable } from '../../db/pool.js';
import type { AppDeps } from '../../deps.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { hhmm, isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasModule, hasRole, MODULE_ROLES, requireModule, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { sendReport } from '../reports/csv.js';
import { assertManages, cancelWorkflow, nptToday, startWorkflow } from '../workflow/service.js';
import {
  addDaysIso, assertWeekOpen, autoStopStaleTimers, isSunday, loadTimePolicy, minutesFromMs, roundEntry, TIME_POLICY_KEY,
  timePolicySchema, weekStartOf, type TimePolicy,
} from './service.js';

/** Days where tracked and attended time differ by more than this are flagged for the reviewer. */
export const DISCREPANCY_MIN = 60;
const DAY_LIMIT_MIN = 1440;

const name = z.string().trim().min(1).max(120);
const color = z.string().regex(/^#[0-9a-fA-F]{6}$/);
const rate = z.number().min(0).max(1_000_000).nullable();

interface ProjectRow { id: string; status: string; all_members: boolean; billable: boolean; name: string }

async function tx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const c = await db.connect();
  try {
    await c.query('BEGIN');
    const out = await fn(c);
    await c.query('COMMIT');
    return out;
  } catch (e) { await c.query('ROLLBACK'); throw e; } finally { c.release(); }
}

/** The signed-in person's own employee record; time is always logged by the person who did the work. */
function ownEmployee(req: Request): string {
  const id = req.user!.employeeId;
  if (!id) throw new ForbiddenError('Only employees can track time. Link this account to an employee first.');
  return id;
}

/** Whose time a request may read: own by default; a manager their team; HR anyone in the company. */
async function readableEmployee(db: Queryable, req: Request, employeeId: string | undefined): Promise<string> {
  const u = req.user!;
  if (!employeeId || employeeId === u.employeeId) return ownEmployee(req);
  const r = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyIdOf(req)]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  await assertManages(db, u, employeeId, 'attendance');
  return employeeId;
}

async function enabledPolicy(db: Queryable, companyId: string): Promise<TimePolicy> {
  const p = await loadTimePolicy(db, companyId);
  if (!p.enabled) throw new ForbiddenError('Time tracking is turned off for your company.');
  return p;
}

/** Project and activity checks shared by the timer and manual entries. Returns the project's default billable flag. */
async function checkWork(db: Queryable, companyId: string, employeeId: string, policy: TimePolicy, projectId: string | null, activityId: string | null): Promise<{ billable: boolean }> {
  if (!projectId) {
    if (policy.requireProject) throw new BadRequestError('Pick a project for this time.');
    if (activityId) {
      const a = await db.query('SELECT 1 FROM time_activity WHERE id = $1 AND company_id = $2 AND project_id IS NULL AND NOT archived', [activityId, companyId]);
      if (!a.rows[0]) throw new BadRequestError('That activity is not available without a project.');
    }
    return { billable: false };
  }
  const p = await db.query<ProjectRow>('SELECT id, status, all_members, billable, name FROM time_project WHERE id = $1 AND company_id = $2', [projectId, companyId]);
  const proj = p.rows[0];
  if (!proj) throw new BadRequestError('Project not found.');
  if (proj.status !== 'active') throw new BadRequestError(`${proj.name} is archived.`);
  if (!proj.all_members) {
    const m = await db.query('SELECT 1 FROM time_project_member WHERE project_id = $1 AND employee_id = $2', [projectId, employeeId]);
    if (!m.rows[0]) throw new ForbiddenError(`You are not a member of ${proj.name}.`);
  }
  if (activityId) {
    const a = await db.query('SELECT 1 FROM time_activity WHERE id = $1 AND company_id = $2 AND (project_id IS NULL OR project_id = $3) AND NOT archived', [activityId, companyId, projectId]);
    if (!a.rows[0]) throw new BadRequestError('That activity does not belong to this project.');
  }
  return { billable: proj.billable };
}

async function dayTotal(db: Queryable, employeeId: string, localDate: string, excludeId: string | null): Promise<number> {
  const r = await db.query<{ min: number }>(
    `SELECT coalesce(sum(duration_min), 0)::int AS min FROM time_entry WHERE employee_id = $1 AND local_date = $2 AND ($3::uuid IS NULL OR id <> $3)`,
    [employeeId, localDate, excludeId]);
  return r.rows[0]?.min ?? 0;
}

async function assertNoOverlap(db: Queryable, employeeId: string, start: Date, end: Date, excludeId: string | null): Promise<void> {
  const r = await db.query(
    `SELECT 1 FROM time_entry WHERE employee_id = $1 AND started_at IS NOT NULL AND ($4::uuid IS NULL OR id <> $4)
        AND tstzrange(started_at, coalesce(ended_at, 'infinity'::timestamptz), '[)') && tstzrange($2, $3, '[)')`, [employeeId, start, end, excludeId]);
  if (r.rows[0]) throw new ConflictError('This time overlaps another entry or your running timer.');
}

const ENTRY_COLS = `e.id, e.employee_id, to_char(e.local_date, 'YYYY-MM-DD') AS local_date, e.started_at, e.ended_at, e.duration_min, e.project_id,
  p.name AS project_name, p.code AS project_code, p.color AS project_color, c.name AS client_name, e.activity_id, a.name AS activity_name,
  e.note, e.billable, e.source, e.auto_stopped, e.created_at, e.updated_at`;
const ENTRY_FROM = `time_entry e LEFT JOIN time_project p ON p.id = e.project_id LEFT JOIN time_client c ON c.id = p.client_id LEFT JOIN time_activity a ON a.id = e.activity_id`;

async function entryById(db: Queryable, id: string) {
  const r = await db.query(`SELECT ${ENTRY_COLS} FROM ${ENTRY_FROM} WHERE e.id = $1`, [id]);
  return r.rows[0] ?? null;
}

const manualEntry = z.object({
  date: isoDate,
  start: hhmm.optional(),
  end: hhmm.optional(),
  durationMin: z.number().int().min(1).max(DAY_LIMIT_MIN).optional(),
  projectId: uuid.nullable().optional(),
  activityId: uuid.nullable().optional(),
  note: z.string().trim().max(500).nullable().optional(),
  billable: z.boolean().optional(),
});

/** Resolve start/end or a bare duration into the stored shape, checking the clock and the day. */
function resolveSpan(b: { date: string; start?: string | undefined; end?: string | undefined; durationMin?: number | undefined }, now = Date.now()): { startedAt: Date | null; endedAt: Date | null; durationMin: number } {
  if (b.date > localDateOf(now)) throw new BadRequestError('You cannot log time for a future day.');
  if (b.start || b.end) {
    if (!b.start || !b.end) throw new BadRequestError('Give both a start and an end time, or just a duration.');
    if (parseHHMM(b.end) <= parseHHMM(b.start)) throw new BadRequestError('The end time must be after the start time.');
    const s = localTimeMs(b.date, b.start); const e = localTimeMs(b.date, b.end);
    if (e > now) throw new BadRequestError('You cannot log time that has not happened yet.');
    return { startedAt: new Date(s), endedAt: new Date(e), durationMin: Math.round((e - s) / 60_000) };
  }
  if (!b.durationMin) throw new BadRequestError('Give a start and end time or a duration.');
  return { startedAt: null, endedAt: null, durationMin: b.durationMin };
}

const TS_COLS = `t.id, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, t.status, t.total_min, t.note, t.submitted_at, t.decided_at, t.decision_comment,
  t.workflow_instance_id, u.name AS decided_by_name`;

/** One employee's week: entries per day, rounded totals and the comparison with attended time. */
async function weekView(db: Queryable, companyId: string, employeeId: string, weekStart: string) {
  const policy = await loadTimePolicy(db, companyId);
  const weekEnd = addDaysIso(weekStart, 6);
  const entries = await db.query<{ local_date: string; duration_min: number | null; billable: boolean } & Record<string, unknown>>(
    `SELECT ${ENTRY_COLS} FROM ${ENTRY_FROM} WHERE e.employee_id = $1 AND e.local_date BETWEEN $2 AND $3 ORDER BY e.local_date, e.started_at NULLS LAST, e.created_at`,
    [employeeId, weekStart, weekEnd]);
  const att = await db.query<{ local_date: string; worked_min: number; status: string | null }>(
    `SELECT to_char(local_date, 'YYYY-MM-DD') AS local_date, worked_min, status FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3`,
    [employeeId, weekStart, weekEnd]);
  const attBy = new Map(att.rows.map((a) => [a.local_date, a]));
  // The latest submission decides the week's state; earlier returned or withdrawn ones are history.
  const ts = await db.query(
    `SELECT ${TS_COLS} FROM timesheet t LEFT JOIN app_user u ON u.id = t.decided_by WHERE t.employee_id = $1 AND t.week_start = $2 ORDER BY t.submitted_at DESC`, [employeeId, weekStart]);
  const today = nptToday();
  const days = Array.from({ length: 7 }, (_, i) => {
    const date = addDaysIso(weekStart, i);
    const list = entries.rows.filter((e) => e.local_date === date);
    const done = list.filter((e) => e.duration_min !== null);
    const trackedMin = done.reduce((s, e) => s + (e.duration_min ?? 0), 0);
    const roundedMin = done.reduce((s, e) => s + roundEntry(e.duration_min ?? 0, policy), 0);
    const billableMin = done.filter((e) => e.billable).reduce((s, e) => s + roundEntry(e.duration_min ?? 0, policy), 0);
    const a = attBy.get(date);
    const attendanceMin = a ? a.worked_min : null;
    const diffMin = attendanceMin === null ? null : trackedMin - attendanceMin;
    const flag = trackedMin > 0 && !attendanceMin && date < today ? 'no_attendance'
      : diffMin !== null && trackedMin > 0 && Math.abs(diffMin) > DISCREPANCY_MIN ? (diffMin > 0 ? 'over' : 'under') : null;
    return { date, entries: list, trackedMin, roundedMin, billableMin, attendanceMin, attendanceStatus: a?.status ?? null, diffMin, flag };
  });
  const totals = {
    trackedMin: days.reduce((s, d) => s + d.trackedMin, 0),
    roundedMin: days.reduce((s, d) => s + d.roundedMin, 0),
    billableMin: days.reduce((s, d) => s + d.billableMin, 0),
    attendanceMin: days.reduce((s, d) => s + (d.attendanceMin ?? 0), 0),
    flaggedDays: days.filter((d) => d.flag).length,
  };
  return {
    weekStart, weekEnd, days, totals, running: entries.rows.some((e) => e.duration_min === null),
    timesheet: ts.rows[0] ?? null, history: ts.rows.slice(1), rounding: { step: policy.roundingStep, mode: policy.roundingMode },
  };
}

type Scope = 'own' | 'team' | 'all';
function scopeOf(u: AuthUser, asked: Scope | undefined): Scope {
  const s = asked ?? (hasModule(u, 'attendance') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own');
  if (s === 'all' && !hasModule(u, 'attendance')) throw new ForbiddenError('Only HR can see everyone\'s time.');
  if (s === 'team' && !hasRole(u, ...MODULE_ROLES.attendance, 'manager')) throw new ForbiddenError('Only managers can see their team\'s time.');
  return s;
}
/** SQL filter on employee alias `emp` for a scope; parameters $n = scope, $n+1 = viewer's employee id. */
const scopeSql = (n: number) => `(CASE $${n}::text WHEN 'own' THEN emp.id = $${n + 1}::uuid
  WHEN 'team' THEN (emp.id = $${n + 1}::uuid OR hj_manages($${n + 1}::uuid, emp.id, current_date))
  ELSE true END)`;

export function timesheetsRoutes({ db }: AppDeps): Router {
  const r = Router();

  // ---------------------------------------------------------------- settings
  r.get('/settings', async (req, res) => {
    res.json({ policy: await loadTimePolicy(db, companyIdOf(req)) });
  });

  r.put('/settings', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const value = timePolicySchema.parse(req.body);
    const before = await loadTimePolicy(db, companyId);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [companyId, TIME_POLICY_KEY, JSON.stringify(value), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: TIME_POLICY_KEY, before, after: value });
    res.json({ policy: value });
  });

  // ---------------------------------------------------------------- clients
  r.get('/clients', async (req, res) => {
    const rows = await db.query(
      `SELECT c.id, c.name, c.notes, c.archived, (SELECT count(*)::int FROM time_project p WHERE p.client_id = c.id AND p.status = 'active') AS active_projects
         FROM time_client c WHERE c.company_id = $1 AND ($2::boolean OR NOT c.archived) ORDER BY c.archived, lower(c.name)`,
      [companyIdOf(req), req.query.all === '1' && hasModule(req.user!, 'attendance')]);
    res.json({ clients: rows.rows });
  });

  r.post('/clients', requireModule('attendance'), async (req, res) => {
    const b = z.object({ name, notes: z.string().trim().max(500).nullable().optional() }).parse(req.body);
    const ins = await db.query<{ id: string }>('INSERT INTO time_client (company_id, name, notes) VALUES ($1, $2, $3) RETURNING id, name, notes, archived', [companyIdOf(req), b.name, b.notes || null]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'time_client', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ client: ins.rows[0] });
  });

  r.patch('/clients/:id', requireModule('attendance'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ name: name.optional(), notes: z.string().trim().max(500).nullable().optional(), archived: z.boolean().optional() }).parse(req.body);
    const u = await db.query(
      `UPDATE time_client SET name = coalesce($3, name), notes = CASE WHEN $4::boolean THEN $5 ELSE notes END, archived = coalesce($6, archived)
        WHERE id = $1 AND company_id = $2 RETURNING id, name, notes, archived`,
      [id, companyIdOf(req), b.name ?? null, b.notes !== undefined, b.notes || null, b.archived ?? null]);
    if (!u.rows[0]) throw new NotFoundError('client not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'time_client', rowId: id, after: b });
    res.json({ client: u.rows[0] });
  });

  // ---------------------------------------------------------------- projects
  /** Employees see the active projects they may log to; HR with all=1 sees every project with its members. */
  r.get('/projects', async (req, res) => {
    const hr = hasModule(req.user!, 'attendance');
    const all = hr && req.query.all === '1';
    const rows = await db.query(
      `SELECT p.id, p.code, p.name, p.color, p.billable, p.bill_rate, p.budget_hours, p.all_members, p.status, p.client_id, c.name AS client_name,
              (SELECT coalesce(sum(t.duration_min), 0)::int FROM time_entry t WHERE t.project_id = p.id) AS tracked_min,
              ${hr ? `(SELECT coalesce(json_agg(json_build_object('id', e.id, 'name', e.name_en, 'empCode', e.emp_code) ORDER BY e.name_en), '[]') FROM time_project_member m JOIN employee e ON e.id = m.employee_id WHERE m.project_id = p.id)` : `'[]'::json`} AS members
         FROM time_project p LEFT JOIN time_client c ON c.id = p.client_id
        WHERE p.company_id = $1 AND ($2::boolean OR p.status = 'active')
          AND ($2::boolean OR p.all_members OR EXISTS (SELECT 1 FROM time_project_member m WHERE m.project_id = p.id AND m.employee_id = $3::uuid))
        ORDER BY p.status, lower(p.name)`, [companyIdOf(req), all, req.user!.employeeId]);
    res.json({ projects: rows.rows });
  });

  const projectBody = z.object({
    code: z.string().trim().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$/, 'letters, digits, - or _ (max 20)'),
    name, clientId: uuid.nullable().optional(), color: color.optional(), billable: z.boolean().optional(), billRate: rate.optional(),
    budgetHours: z.number().positive().max(1_000_000).nullable().optional(), allMembers: z.boolean().optional(), memberIds: z.array(uuid).max(1000).optional(),
    status: z.enum(['active', 'archived']).optional(),
  });

  async function setMembers(c: Queryable, companyId: string, projectId: string, ids: string[]): Promise<void> {
    const unique = [...new Set(ids)];
    if (unique.length) {
      const ok = await c.query<{ n: number }>('SELECT count(*)::int AS n FROM employee WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, unique]);
      if (ok.rows[0]!.n !== unique.length) throw new BadRequestError('Some members are not employees of this company.');
    }
    await c.query('DELETE FROM time_project_member WHERE project_id = $1', [projectId]);
    if (unique.length) await c.query('INSERT INTO time_project_member (project_id, employee_id) SELECT $1, unnest($2::uuid[])', [projectId, unique]);
  }

  async function checkClient(c: Queryable, companyId: string, clientId: string | null | undefined): Promise<void> {
    if (!clientId) return;
    const r0 = await c.query('SELECT 1 FROM time_client WHERE id = $1 AND company_id = $2', [clientId, companyId]);
    if (!r0.rows[0]) throw new BadRequestError('Client not found.');
  }

  r.post('/projects', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = projectBody.parse(req.body);
    const id = await tx(db, async (c) => {
      await checkClient(c, companyId, b.clientId);
      const ins = await c.query<{ id: string }>(
        `INSERT INTO time_project (company_id, client_id, code, name, color, billable, bill_rate, budget_hours, all_members, status)
         VALUES ($1, $2, upper($3), $4, coalesce($5, '#12773a'), coalesce($6, true), $7, $8, coalesce($9, true), coalesce($10, 'active')) RETURNING id`,
        [companyId, b.clientId ?? null, b.code, b.name, b.color ?? null, b.billable ?? null, b.billRate ?? null, b.budgetHours ?? null, b.allMembers ?? null, b.status ?? null]);
      const pid = ins.rows[0]!.id;
      if (b.memberIds) await setMembers(c, companyId, pid, b.memberIds);
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'time_project', rowId: pid, after: b });
      return pid;
    });
    res.status(201).json({ id });
  });

  r.patch('/projects/:id', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = projectBody.partial().parse(req.body);
    await tx(db, async (c) => {
      const cur = await c.query('SELECT * FROM time_project WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      if (!cur.rows[0]) throw new NotFoundError('project not found');
      await checkClient(c, companyId, b.clientId);
      await c.query(
        `UPDATE time_project SET code = coalesce(upper($2), code), name = coalesce($3, name),
                client_id = CASE WHEN $4::boolean THEN $5::uuid ELSE client_id END, color = coalesce($6, color), billable = coalesce($7, billable),
                bill_rate = CASE WHEN $8::boolean THEN $9::numeric ELSE bill_rate END, budget_hours = CASE WHEN $10::boolean THEN $11::numeric ELSE budget_hours END,
                all_members = coalesce($12, all_members), status = coalesce($13, status), updated_at = now()
          WHERE id = $1`,
        [id, b.code ?? null, b.name ?? null, b.clientId !== undefined, b.clientId ?? null, b.color ?? null, b.billable ?? null,
          b.billRate !== undefined, b.billRate ?? null, b.budgetHours !== undefined, b.budgetHours ?? null, b.allMembers ?? null, b.status ?? null]);
      if (b.memberIds) await setMembers(c, companyId, id, b.memberIds);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'time_project', rowId: id, before: cur.rows[0], after: b });
    });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- activities
  r.get('/activities', async (req, res) => {
    const q = z.object({ projectId: uuid.optional(), all: z.enum(['0', '1']).optional() }).parse(req.query);
    const all = q.all === '1' && hasModule(req.user!, 'attendance');
    const rows = await db.query(
      `SELECT a.id, a.name, a.project_id, p.name AS project_name, a.archived FROM time_activity a LEFT JOIN time_project p ON p.id = a.project_id
        WHERE a.company_id = $1 AND ($2::boolean OR NOT a.archived) AND ($3::uuid IS NULL OR a.project_id IS NULL OR a.project_id = $3)
        ORDER BY a.project_id NULLS FIRST, lower(a.name)`, [companyIdOf(req), all, q.projectId ?? null]);
    res.json({ activities: rows.rows });
  });

  r.post('/activities', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ name: z.string().trim().min(1).max(80), projectId: uuid.nullable().optional() }).parse(req.body);
    if (b.projectId) {
      const p = await db.query('SELECT 1 FROM time_project WHERE id = $1 AND company_id = $2', [b.projectId, companyId]);
      if (!p.rows[0]) throw new BadRequestError('Project not found.');
    }
    const ins = await db.query<{ id: string }>('INSERT INTO time_activity (company_id, project_id, name) VALUES ($1, $2, $3) RETURNING id, name, project_id, archived', [companyId, b.projectId ?? null, b.name]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'time_activity', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ activity: ins.rows[0] });
  });

  r.patch('/activities/:id', requireModule('attendance'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ name: z.string().trim().min(1).max(80).optional(), archived: z.boolean().optional() }).parse(req.body);
    const u = await db.query('UPDATE time_activity SET name = coalesce($3, name), archived = coalesce($4, archived) WHERE id = $1 AND company_id = $2 RETURNING id, name, project_id, archived',
      [id, companyIdOf(req), b.name ?? null, b.archived ?? null]);
    if (!u.rows[0]) throw new NotFoundError('activity not found');
    res.json({ activity: u.rows[0] });
  });

  // ---------------------------------------------------------------- timer (server clock only)
  async function runningFor(c: Queryable, employeeId: string) {
    const r0 = await c.query(`SELECT ${ENTRY_COLS} FROM ${ENTRY_FROM} WHERE e.employee_id = $1 AND e.ended_at IS NULL AND e.duration_min IS NULL`, [employeeId]);
    return r0.rows[0] ?? null;
  }

  r.get('/timer', async (req, res) => {
    const employeeId = ownEmployee(req);
    await autoStopStaleTimers(db, new Date(), employeeId);
    const last = await db.query(`SELECT ${ENTRY_COLS} FROM ${ENTRY_FROM} WHERE e.employee_id = $1 AND e.duration_min IS NOT NULL ORDER BY coalesce(e.ended_at, e.created_at) DESC LIMIT 1`, [employeeId]);
    res.json({ running: await runningFor(db, employeeId), last: last.rows[0] ?? null, todayMin: await dayTotal(db, employeeId, nptToday(), null), serverTime: new Date().toISOString() });
  });

  const timerBody = z.object({ projectId: uuid.nullable().optional(), activityId: uuid.nullable().optional(), note: z.string().trim().max(500).nullable().optional(), billable: z.boolean().optional() });

  async function startTimer(req: Request, b: z.infer<typeof timerBody>) {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const policy = await enabledPolicy(db, companyId);
    await autoStopStaleTimers(db, new Date(), employeeId);
    return tx(db, async (c) => {
      await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`time_entry:${employeeId}`]);
      if (await runningFor(c, employeeId)) throw new ConflictError('A timer is already running. Stop it first.');
      const now = new Date();
      const today = localDateOf(now.getTime());
      await assertWeekOpen(c, employeeId, today);
      const work = await checkWork(c, companyId, employeeId, policy, b.projectId ?? null, b.activityId ?? null);
      await assertNoOverlap(c, employeeId, now, new Date(now.getTime() + 1000), null);
      const ins = await c.query<{ id: string }>(
        `INSERT INTO time_entry (company_id, employee_id, local_date, started_at, project_id, activity_id, note, billable, source, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'timer', $9) RETURNING id`,
        [companyId, employeeId, today, now, b.projectId ?? null, b.activityId ?? null, b.note || null, b.billable ?? work.billable, req.user!.id]);
      return entryById(c, ins.rows[0]!.id);
    });
  }

  r.post('/timer/start', async (req, res) => {
    res.status(201).json({ running: await startTimer(req, timerBody.parse(req.body)) });
  });

  /** Start again on whatever was tracked last (Jibble's "resume"). */
  r.post('/timer/resume', async (req, res) => {
    const employeeId = ownEmployee(req);
    const last = await db.query<{ project_id: string | null; activity_id: string | null; note: string | null; billable: boolean }>(
      'SELECT project_id, activity_id, note, billable FROM time_entry WHERE employee_id = $1 AND duration_min IS NOT NULL ORDER BY coalesce(ended_at, created_at) DESC LIMIT 1', [employeeId]);
    const l = last.rows[0];
    if (!l) throw new BadRequestError('There is nothing to resume yet. Start a new timer.');
    res.status(201).json({ running: await startTimer(req, { projectId: l.project_id, activityId: l.activity_id, note: l.note, billable: l.billable }) });
  });

  r.post('/timer/stop', async (req, res) => {
    const employeeId = ownEmployee(req);
    const b = z.object({ note: z.string().trim().max(500).nullable().optional() }).parse(req.body ?? {});
    await autoStopStaleTimers(db, new Date(), employeeId);
    const out = await tx(db, async (c) => {
      const run = await c.query<{ id: string; started_at: Date }>(
        'SELECT id, started_at FROM time_entry WHERE employee_id = $1 AND ended_at IS NULL AND duration_min IS NULL FOR UPDATE', [employeeId]);
      const t = run.rows[0];
      if (!t) throw new ConflictError('No timer is running.');
      // The entry ends now (so the next timer can start straight away); anything under a minute counts as one minute.
      const end = new Date(Math.max(Date.now(), t.started_at.getTime() + 1));
      await c.query(`UPDATE time_entry SET ended_at = $2, duration_min = least($3, 1440), note = CASE WHEN $4::boolean THEN $5 ELSE note END, updated_at = now() WHERE id = $1`,
        [t.id, end, minutesFromMs(end.getTime() - t.started_at.getTime()), b.note !== undefined, b.note || null]);
      return entryById(c, t.id);
    });
    res.json({ entry: out });
  });

  // ---------------------------------------------------------------- entries
  r.get('/entries', async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, employeeId: uuid.optional() }).parse(req.query);
    if (q.to < q.from) throw new BadRequestError('from must be before to');
    const employeeId = await readableEmployee(db, req, q.employeeId);
    const rows = await db.query(`SELECT ${ENTRY_COLS} FROM ${ENTRY_FROM} WHERE e.employee_id = $1 AND e.local_date BETWEEN $2 AND $3 ORDER BY e.local_date DESC, e.started_at DESC NULLS LAST, e.created_at DESC LIMIT 2000`,
      [employeeId, q.from, q.to]);
    res.json({ entries: rows.rows });
  });

  r.post('/entries', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const b = manualEntry.parse(req.body);
    const policy = await enabledPolicy(db, companyId);
    const span = resolveSpan(b);
    const id = await tx(db, async (c) => {
      await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`time_entry:${employeeId}`]);
      await assertWeekOpen(c, employeeId, b.date);
      const work = await checkWork(c, companyId, employeeId, policy, b.projectId ?? null, b.activityId ?? null);
      if (span.startedAt && span.endedAt) await assertNoOverlap(c, employeeId, span.startedAt, span.endedAt, null);
      if ((await dayTotal(c, employeeId, b.date, null)) + span.durationMin > DAY_LIMIT_MIN) throw new BadRequestError('A day cannot have more than 24 hours of time.');
      const ins = await c.query<{ id: string }>(
        `INSERT INTO time_entry (company_id, employee_id, local_date, started_at, ended_at, duration_min, project_id, activity_id, note, billable, source, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'manual', $11) RETURNING id`,
        [companyId, employeeId, b.date, span.startedAt, span.endedAt, span.durationMin, b.projectId ?? null, b.activityId ?? null, b.note || null, b.billable ?? work.billable, req.user!.id]);
      return ins.rows[0]!.id;
    });
    res.status(201).json({ entry: await entryById(db, id) });
  });

  r.patch('/entries/:id', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const id = uuid.parse(req.params.id);
    const b = manualEntry.partial().parse(req.body);
    const policy = await enabledPolicy(db, companyId);
    await tx(db, async (c) => {
      await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`time_entry:${employeeId}`]);
      const cur0 = await c.query<{ employee_id: string; local_date: string; started_at: Date | null; ended_at: Date | null; duration_min: number | null; project_id: string | null; activity_id: string | null; note: string | null; billable: boolean }>(
        `SELECT employee_id, to_char(local_date, 'YYYY-MM-DD') AS local_date, started_at, ended_at, duration_min, project_id, activity_id, note, billable
           FROM time_entry WHERE id = $1 AND company_id = $2 FOR UPDATE`, [id, companyId]);
      const cur = cur0.rows[0];
      if (!cur || cur.employee_id !== employeeId) throw new NotFoundError('entry not found');
      await assertWeekOpen(c, employeeId, cur.local_date);
      const projectChanged = b.projectId !== undefined && b.projectId !== cur.project_id;
      const projectId = b.projectId !== undefined ? b.projectId : cur.project_id;
      const activityId = b.activityId !== undefined ? b.activityId : projectChanged ? null : cur.activity_id;
      const work = await checkWork(c, companyId, employeeId, policy, projectId, activityId);
      const timeChanged = b.date !== undefined || b.start !== undefined || b.end !== undefined || b.durationMin !== undefined;
      let span: { startedAt: Date | null; endedAt: Date | null; durationMin: number | null } = { startedAt: cur.started_at, endedAt: cur.ended_at, durationMin: cur.duration_min };
      let date = cur.local_date;
      if (timeChanged) {
        if (cur.duration_min === null) throw new BadRequestError('Stop the timer before changing its times.');
        date = b.date ?? cur.local_date;
        await assertWeekOpen(c, employeeId, date);
        span = resolveSpan({ date, start: b.start, end: b.end, durationMin: b.durationMin });
        if (span.startedAt && span.endedAt) await assertNoOverlap(c, employeeId, span.startedAt, span.endedAt, id);
        if ((await dayTotal(c, employeeId, date, id)) + (span.durationMin ?? 0) > DAY_LIMIT_MIN) throw new BadRequestError('A day cannot have more than 24 hours of time.');
      }
      const billable = b.billable ?? (projectChanged ? work.billable : cur.billable);
      await c.query(
        `UPDATE time_entry SET local_date = $2, started_at = $3, ended_at = $4, duration_min = $5, project_id = $6, activity_id = $7,
                note = $8, billable = $9, auto_stopped = CASE WHEN $10::boolean THEN false ELSE auto_stopped END, updated_at = now() WHERE id = $1`,
        [id, date, span.startedAt, span.endedAt, span.durationMin, projectId, activityId, b.note !== undefined ? (b.note || null) : cur.note, billable, timeChanged]);
    });
    res.json({ entry: await entryById(db, id) });
  });

  r.delete('/entries/:id', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const id = uuid.parse(req.params.id);
    await tx(db, async (c) => {
      const cur = await c.query<{ employee_id: string; local_date: string }>(`SELECT employee_id, to_char(local_date, 'YYYY-MM-DD') AS local_date FROM time_entry WHERE id = $1 AND company_id = $2 FOR UPDATE`, [id, companyId]);
      if (!cur.rows[0] || cur.rows[0].employee_id !== employeeId) throw new NotFoundError('entry not found');
      await assertWeekOpen(c, employeeId, cur.rows[0].local_date);
      await c.query('DELETE FROM time_entry WHERE id = $1', [id]);
    });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- weeks and timesheets
  r.get('/week', async (req, res) => {
    const q = z.object({ weekStart: isoDate.optional(), date: isoDate.optional(), employeeId: uuid.optional() }).parse(req.query);
    const employeeId = await readableEmployee(db, req, q.employeeId);
    if (employeeId === req.user!.employeeId) await autoStopStaleTimers(db, new Date(), employeeId);
    res.json(await weekView(db, companyIdOf(req), employeeId, weekStartOf(q.weekStart ?? q.date ?? nptToday())));
  });

  /** Submit a week for approval. Each submission is its own row and approval; returned weeks can be fixed and sent again. */
  r.post('/timesheets', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const b = z.object({ weekStart: isoDate, note: z.string().trim().max(500).nullable().optional() }).parse(req.body);
    if (!isSunday(b.weekStart)) throw new BadRequestError('weekStart must be a Sunday');
    if (b.weekStart > nptToday()) throw new BadRequestError('You cannot submit a week that has not started.');
    await enabledPolicy(db, companyId);
    const id = await tx(db, async (c) => {
      await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`time_entry:${employeeId}`]);
      const totals = await c.query<{ min: number; n: number; running: number }>(
        `SELECT coalesce(sum(duration_min), 0)::int AS min, count(*)::int AS n, count(*) FILTER (WHERE duration_min IS NULL)::int AS running
           FROM time_entry WHERE employee_id = $1 AND local_date BETWEEN $2::date AND $2::date + 6`, [employeeId, b.weekStart]);
      const t = totals.rows[0]!;
      if (t.running) throw new ConflictError('Stop your running timer before submitting this week.');
      if (!t.n) throw new BadRequestError('There is no time logged in this week.');
      await assertWeekOpen(c, employeeId, b.weekStart);
      const ins = await c.query<{ id: string }>(
        `INSERT INTO timesheet (company_id, employee_id, week_start, status, total_min, note, submitted_by) VALUES ($1, $2, $3, 'submitted', $4, $5, $6) RETURNING id`,
        [companyId, employeeId, b.weekStart, t.min, b.note || null, req.user!.id]);
      const tsId = ins.rows[0]!.id;
      const inst = await startWorkflow(c, { companyId, objectType: 'timesheet', objectId: tsId, employeeId, title: 'Timesheet to approve',
        body: `${req.user!.name} submitted ${(t.min / 60).toFixed(1)} h for ${bsRange(b.weekStart, addDaysIso(b.weekStart, 6))}.` });
      await c.query('UPDATE timesheet SET workflow_instance_id = $2 WHERE id = $1', [tsId, inst.id]);
      await audit(c, { actorId: req.user!.id, action: 'submit', table: 'timesheet', rowId: tsId, after: { weekStart: b.weekStart, totalMin: t.min } });
      return tsId;
    });
    res.status(201).json({ id });
  });

  r.post('/timesheets/:id/withdraw', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = ownEmployee(req);
    const id = uuid.parse(req.params.id);
    await tx(db, async (c) => {
      const cur = await c.query<{ employee_id: string; status: string }>('SELECT employee_id, status FROM timesheet WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      if (!cur.rows[0] || cur.rows[0].employee_id !== employeeId) throw new NotFoundError('timesheet not found');
      if (cur.rows[0].status !== 'submitted') throw new ConflictError(`Only a timesheet waiting for approval can be withdrawn (it is ${cur.rows[0].status}).`);
      await c.query(`UPDATE timesheet SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
      await cancelWorkflow(c, 'timesheet', id);
      await audit(c, { actorId: req.user!.id, action: 'withdraw', table: 'timesheet', rowId: id });
    });
    res.json({ ok: true });
  });

  /** Timesheets for review: own, a manager's team or (HR) everyone, with tracked vs attended hours for each week. */
  r.get('/timesheets', async (req, res) => {
    const q = z.object({ status: z.enum(['submitted', 'approved', 'rejected', 'cancelled']).optional(), from: isoDate.optional(), to: isoDate.optional(),
      scope: z.enum(['own', 'team', 'all']).optional(), employeeId: uuid.optional() }).parse(req.query);
    const u = req.user!;
    const scope = scopeOf(u, q.scope);
    if (q.employeeId) await readableEmployee(db, req, q.employeeId);
    const rows = await db.query<{ total_min: number; attendance_min: number } & Record<string, unknown>>(
      `SELECT t.id, t.employee_id, emp.emp_code, emp.name_en, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, t.status, t.total_min, t.note,
              t.submitted_at, t.decided_at, t.decision_comment, t.workflow_instance_id, w.state AS workflow_state, du.name AS decided_by_name,
              (SELECT coalesce(sum(a.worked_min), 0)::int FROM v_attendance_latest a WHERE a.employee_id = t.employee_id AND a.local_date BETWEEN t.week_start AND t.week_start + 6) AS attendance_min,
              (SELECT coalesce(sum(te.duration_min) FILTER (WHERE te.billable), 0)::int FROM time_entry te WHERE te.employee_id = t.employee_id AND te.local_date BETWEEN t.week_start AND t.week_start + 6) AS billable_min
         FROM timesheet t JOIN employee emp ON emp.id = t.employee_id
         LEFT JOIN workflow_instance w ON w.id = t.workflow_instance_id LEFT JOIN app_user du ON du.id = t.decided_by
        WHERE t.company_id = $1 AND ($2::text IS NULL OR t.status = $2) AND ($3::date IS NULL OR t.week_start >= $3::date - 6) AND ($4::date IS NULL OR t.week_start <= $4)
          AND ($5::uuid IS NULL OR t.employee_id = $5) AND ${scopeSql(6)}
        ORDER BY t.week_start DESC, t.submitted_at DESC, emp.name_en LIMIT 500`,
      [companyIdOf(req), q.status ?? null, q.from ?? null, q.to ?? null, q.employeeId ?? null, scope, u.employeeId]);
    res.json({ timesheets: rows.rows.map((t) => ({ ...t, diff_min: t.total_min - t.attendance_min })) });
  });

  r.get('/timesheets/:id', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const t = await db.query<{ employee_id: string; week_start: string; emp_code: string; name_en: string }>(
      `SELECT t.employee_id, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, e.emp_code, e.name_en FROM timesheet t JOIN employee e ON e.id = t.employee_id WHERE t.id = $1 AND t.company_id = $2`, [id, companyId]);
    const row = t.rows[0];
    if (!row) throw new NotFoundError('timesheet not found');
    await assertManages(db, req.user!, row.employee_id, 'attendance');
    const ts = await db.query(`SELECT ${TS_COLS} FROM timesheet t LEFT JOIN app_user u ON u.id = t.decided_by WHERE t.id = $1`, [id]);
    res.json({ employee: { id: row.employee_id, empCode: row.emp_code, name: row.name_en }, ...(await weekView(db, companyId, row.employee_id, row.week_start)), submission: ts.rows[0] });
  });

  /** Who on the team has (not) submitted a week: the manager's at-a-glance view. */
  r.get('/team-week', async (req, res) => {
    const q = z.object({ weekStart: isoDate.optional(), scope: z.enum(['team', 'all']).optional() }).parse(req.query);
    const u = req.user!;
    const scope = scopeOf(u, q.scope ?? (hasModule(u, 'attendance') ? 'all' : 'team'));
    const weekStart = weekStartOf(q.weekStart ?? nptToday());
    const rows = await db.query(
      `SELECT emp.id AS employee_id, emp.emp_code, emp.name_en,
              (SELECT coalesce(sum(te.duration_min), 0)::int FROM time_entry te WHERE te.employee_id = emp.id AND te.local_date BETWEEN $2::date AND $2::date + 6) AS tracked_min,
              (SELECT coalesce(sum(a.worked_min), 0)::int FROM v_attendance_latest a WHERE a.employee_id = emp.id AND a.local_date BETWEEN $2::date AND $2::date + 6) AS attendance_min,
              t.id AS timesheet_id, t.status
         FROM employee emp
         LEFT JOIN LATERAL (SELECT id, status FROM timesheet ts WHERE ts.employee_id = emp.id AND ts.week_start = $2 ORDER BY ts.submitted_at DESC LIMIT 1) t ON true
        WHERE emp.company_id = $1 AND emp.status = 'active' AND ${scopeSql(3)} AND ($3::text <> 'team' OR emp.id <> $4::uuid)
        ORDER BY emp.name_en`, [companyIdOf(req), weekStart, scope, u.employeeId]);
    res.json({ weekStart, weekEnd: addDaysIso(weekStart, 6), people: rows.rows });
  });

  // ---------------------------------------------------------------- reports
  const reportQuery = z.object({
    from: isoDate, to: isoDate, groupBy: z.enum(['project', 'client', 'employee', 'activity', 'day']).default('project'),
    employeeId: uuid.optional(), projectId: uuid.optional(), clientId: uuid.optional(), scope: z.enum(['own', 'team', 'all']).optional(), format: z.enum(['json', 'csv']).optional(),
  });

  async function reportEntries(req: Request, q: z.infer<typeof reportQuery>) {
    if (q.to < q.from) throw new BadRequestError('from must be before to');
    if ((new Date(`${q.to}T00:00:00Z`).getTime() - new Date(`${q.from}T00:00:00Z`).getTime()) / 86_400_000 > 400) throw new BadRequestError('Pick a range of at most 400 days.');
    const u = req.user!;
    const scope = scopeOf(u, q.scope);
    if (q.employeeId) await readableEmployee(db, req, q.employeeId);
    const rows = await db.query<{
      id: string; employee_id: string; emp_code: string; name_en: string; local_date: string; duration_min: number; billable: boolean; note: string | null;
      project_id: string | null; project_code: string | null; project_name: string | null; bill_rate: string | null; budget_hours: string | null;
      client_id: string | null; client_name: string | null; activity_id: string | null; activity_name: string | null; started_at: Date | null; ended_at: Date | null;
    }>(
      `SELECT e.id, e.employee_id, emp.emp_code, emp.name_en, to_char(e.local_date, 'YYYY-MM-DD') AS local_date, e.duration_min, e.billable, e.note,
              e.project_id, p.code AS project_code, p.name AS project_name, p.bill_rate, p.budget_hours, p.client_id, c.name AS client_name,
              e.activity_id, a.name AS activity_name, e.started_at, e.ended_at
         FROM time_entry e JOIN employee emp ON emp.id = e.employee_id
         LEFT JOIN time_project p ON p.id = e.project_id LEFT JOIN time_client c ON c.id = p.client_id LEFT JOIN time_activity a ON a.id = e.activity_id
        WHERE e.company_id = $1 AND e.duration_min IS NOT NULL AND e.local_date BETWEEN $2 AND $3
          AND ($4::uuid IS NULL OR e.employee_id = $4) AND ($5::uuid IS NULL OR e.project_id = $5) AND ($6::uuid IS NULL OR p.client_id = $6)
          AND ${scopeSql(7)}
        ORDER BY e.local_date, emp.name_en, e.started_at NULLS LAST`,
      [companyIdOf(req), q.from, q.to, q.employeeId ?? null, q.projectId ?? null, q.clientId ?? null, scope, u.employeeId]);
    return rows.rows;
  }

  r.get('/reports/summary', async (req, res) => {
    const q = reportQuery.parse(req.query);
    const policy = await loadTimePolicy(db, companyIdOf(req));
    const entries = await reportEntries(req, q);
    const groups = new Map<string, { key: string; label: string; code: string | null; trackedMin: number; roundedMin: number; billableMin: number; amount: number; budgetHours: number | null; entries: number }>();
    for (const e of entries) {
      const [key, label, code, budget]: [string, string, string | null, number | null] =
        q.groupBy === 'project' ? [e.project_id ?? 'none', e.project_name ?? 'No project', e.project_code, e.budget_hours ? Number(e.budget_hours) : null]
          : q.groupBy === 'client' ? [e.client_id ?? 'none', e.client_name ?? 'No client', null, null]
            : q.groupBy === 'employee' ? [e.employee_id, e.name_en, e.emp_code, null]
              : q.groupBy === 'activity' ? [e.activity_id ?? 'none', e.activity_name ?? 'No activity', null, null]
                : [e.local_date, e.local_date, null, null];
      const g = groups.get(key) ?? { key, label, code, trackedMin: 0, roundedMin: 0, billableMin: 0, amount: 0, budgetHours: budget, entries: 0 };
      const rounded = roundEntry(e.duration_min, policy);
      g.trackedMin += e.duration_min; g.roundedMin += rounded; g.entries += 1;
      if (e.billable) { g.billableMin += rounded; g.amount += e.bill_rate ? (rounded / 60) * Number(e.bill_rate) : 0; }
      groups.set(key, g);
    }
    // Budget burn is all-time hours on the project against its budget, not just this range.
    const burn = new Map<string, number>();
    const ids = q.groupBy === 'project' ? [...groups.keys()].filter((k) => k !== 'none') : [];
    if (ids.length) {
      const b = await db.query<{ project_id: string; min: number }>('SELECT project_id, sum(duration_min)::int AS min FROM time_entry WHERE project_id = ANY($1::uuid[]) AND duration_min IS NOT NULL GROUP BY 1', [ids]);
      for (const x of b.rows) burn.set(x.project_id, x.min);
    }
    const rows = [...groups.values()].sort((a, b) => (q.groupBy === 'day' ? a.key.localeCompare(b.key) : b.roundedMin - a.roundedMin)).map((g) => ({
      key: g.key, label: g.label, code: g.code, entries: g.entries,
      tracked_hours: +(g.trackedMin / 60).toFixed(2), hours: +(g.roundedMin / 60).toFixed(2), billable_hours: +(g.billableMin / 60).toFixed(2),
      non_billable_hours: +((g.roundedMin - g.billableMin) / 60).toFixed(2), billable_amount: Math.round(g.amount * 100) / 100,
      budget_hours: g.budgetHours, budget_used_pct: g.budgetHours ? Math.round(((burn.get(g.key) ?? 0) / 60 / g.budgetHours) * 1000) / 10 : null,
    }));
    const columns = [
      { key: 'label', header: q.groupBy[0]!.toUpperCase() + q.groupBy.slice(1) }, ...(q.groupBy === 'project' || q.groupBy === 'employee' ? [{ key: 'code', header: 'Code' }] : []),
      { key: 'hours', header: 'Hours' }, { key: 'billable_hours', header: 'Billable hours' }, { key: 'non_billable_hours', header: 'Non-billable hours' },
      { key: 'billable_amount', header: 'Billable amount (NPR)' }, ...(q.groupBy === 'project' ? [{ key: 'budget_hours', header: 'Budget hours' }, { key: 'budget_used_pct', header: 'Budget used %' }] : []),
      { key: 'entries', header: 'Entries' },
    ];
    if (q.format === 'csv') return sendReport(res, 'csv', `time-by-${q.groupBy}-${q.from}-to-${q.to}`, columns, rows);
    const total = rows.reduce((s, x) => ({ hours: s.hours + x.hours, billable: s.billable + x.billable_hours, amount: s.amount + x.billable_amount }), { hours: 0, billable: 0, amount: 0 });
    res.json({ columns, rows, totals: { hours: +total.hours.toFixed(2), billableHours: +total.billable.toFixed(2), billableAmount: Math.round(total.amount * 100) / 100 }, rounding: { step: policy.roundingStep, mode: policy.roundingMode } });
  });

  r.get('/reports/entries', async (req, res) => {
    const q = reportQuery.parse(req.query);
    const policy = await loadTimePolicy(db, companyIdOf(req));
    const hm = (d: Date | null) => (d ? new Date(d.getTime() + 345 * 60_000).toISOString().slice(11, 16) : '');
    const rows = (await reportEntries(req, q)).map((e) => ({
      date: e.local_date, emp_code: e.emp_code, name: e.name_en, client: e.client_name ?? '', project: e.project_name ?? '', activity: e.activity_name ?? '',
      start: hm(e.started_at), end: hm(e.ended_at), minutes: e.duration_min, hours: +(roundEntry(e.duration_min, policy) / 60).toFixed(2),
      billable: e.billable ? 'Yes' : 'No', note: e.note ?? '',
    }));
    const columns = [
      { key: 'date', header: 'Date (AD)' }, { key: 'emp_code', header: 'Employee code' }, { key: 'name', header: 'Employee' }, { key: 'client', header: 'Client' },
      { key: 'project', header: 'Project' }, { key: 'activity', header: 'Activity' }, { key: 'start', header: 'Start' }, { key: 'end', header: 'End' },
      { key: 'minutes', header: 'Minutes tracked' }, { key: 'hours', header: 'Hours (rounded)' }, { key: 'billable', header: 'Billable' }, { key: 'note', header: 'Note' },
    ];
    sendReport(res, q.format, `time-entries-${q.from}-to-${q.to}`, columns, rows);
  });

  return r;
}
