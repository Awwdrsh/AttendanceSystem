import type { Router } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { canSeeEmployee, directManager, employeeInCompany, isAboveInChain, recomputeGoalProgress, tx } from './service.js';

const measure = z.enum(['number', 'percent', 'currency', 'boolean']);
const num = z.coerce.number().finite().min(-1e13).max(1e13);
const keyResultBody = z.object({
  title: z.string().trim().min(1).max(200),
  measure: measure.default('number'),
  startValue: num.default(0),
  targetValue: num.default(100),
  currentValue: num.optional(),
  unit: z.string().trim().max(20).nullish(),
  weight: z.coerce.number().positive().max(1000).default(1),
});

const goalBody = z.object({
  scope: z.enum(['company', 'department', 'individual']).default('individual'),
  employeeId: uuid.optional(),
  orgUnitId: uuid.optional(),
  parentId: uuid.nullish(),
  title: z.string().trim().min(1).max(200),
  description: z.string().trim().max(4000).nullish(),
  weight: z.coerce.number().min(0).max(100).default(0),
  startDate: isoDate.nullish(),
  dueDate: isoDate.nullish(),
  status: z.enum(['draft', 'active']).default('active'),
  keyResults: z.array(keyResultBody).max(20).default([]),
});

interface GoalRow { id: string; company_id: string; scope: 'company' | 'department' | 'individual'; org_unit_id: string | null; employee_id: string | null; status: string; title: string }

export async function loadGoal(db: Queryable, companyId: string, id: string): Promise<GoalRow> {
  const r = await db.query<GoalRow>('SELECT id, company_id, scope, org_unit_id, employee_id, status, title FROM perf_goal WHERE id = $1 AND company_id = $2', [id, companyId]);
  if (!r.rows[0]) throw new NotFoundError('goal not found');
  return r.rows[0];
}

async function currentOrgUnit(db: Queryable, employeeId: string | null): Promise<string | null> {
  if (!employeeId) return null;
  const r = await db.query<{ org_unit_id: string | null }>('SELECT org_unit_id FROM employee_assignment WHERE employee_id = $1 AND valid @> current_date', [employeeId]);
  return r.rows[0]?.org_unit_id ?? null;
}

/** Company and department goals are visible company-wide; individual goals to the owner, their manager chain and HR. */
export async function canViewGoal(db: Queryable, user: AuthUser, g: GoalRow): Promise<boolean> {
  if (g.scope !== 'individual') return true;
  return canSeeEmployee(db, user, g.employee_id!);
}

/** Company goals: HR. Department goals: HR or a manager in that department. Individual: owner, manager chain, HR. */
/** Not visible: 404 (no probing); visible but read-only: 403. */
async function assertCanEdit(db: Queryable, user: AuthUser, g: GoalRow, what = 'change this goal'): Promise<void> {
  if (!(await canViewGoal(db, user, g))) throw new NotFoundError('goal not found');
  if (!(await canEditGoal(db, user, g))) throw new ForbiddenError(`not allowed to ${what}`);
}

export async function canEditGoal(db: Queryable, user: AuthUser, g: Pick<GoalRow, 'scope' | 'org_unit_id' | 'employee_id'>): Promise<boolean> {
  if (hasRole(user, 'hr')) return true;
  if (g.scope === 'company') return false;
  if (g.scope === 'department') return hasRole(user, 'manager') && (await currentOrgUnit(db, user.employeeId)) === g.org_unit_id;
  return user.employeeId === g.employee_id || (await isAboveInChain(db, user, g.employee_id!));
}

const GOAL_SELECT = `
  SELECT g.id, g.scope, g.org_unit_id, ou.name_en AS org_unit_name, g.employee_id, e.name_en AS employee_name, e.emp_code,
         g.parent_id, p.title AS parent_title, g.title, g.description, g.weight, g.start_date, g.due_date, g.status, g.progress,
         g.created_at, g.updated_at,
         coalesce((SELECT json_agg(json_build_object('id', k.id, 'title', k.title, 'measure', k.measure, 'start_value', k.start_value,
                   'target_value', k.target_value, 'current_value', k.current_value, 'unit', k.unit, 'weight', k.weight) ORDER BY k.sort, k.created_at)
                   FROM perf_key_result k WHERE k.goal_id = g.id), '[]') AS key_results,
         (SELECT json_build_object('created_at', c.created_at, 'confidence', c.confidence, 'comment', c.comment)
            FROM perf_checkin c WHERE c.goal_id = g.id ORDER BY c.created_at DESC LIMIT 1) AS last_checkin
    FROM perf_goal g
    LEFT JOIN org_unit ou ON ou.id = g.org_unit_id
    LEFT JOIN employee e ON e.id = g.employee_id
    LEFT JOIN perf_goal p ON p.id = g.parent_id`;

/** Employees whose manager chain (up to five levels) contains the given manager. */
export const SUBORDINATES_CTE = `
  WITH RECURSIVE subs(emp, depth) AS (
    SELECT a.employee_id, 1 FROM employee_assignment a WHERE a.manager_id = $MGR AND a.valid @> current_date
    UNION
    SELECT a.employee_id, s.depth + 1 FROM subs s JOIN employee_assignment a ON a.manager_id = s.emp AND a.valid @> current_date WHERE s.depth < 5
  )`;

async function insertKeyResults(db: Queryable, goalId: string, krs: z.infer<typeof keyResultBody>[]): Promise<void> {
  let sort = 0;
  for (const k of krs) {
    await db.query(
      `INSERT INTO perf_key_result (goal_id, title, measure, start_value, target_value, current_value, unit, weight, sort) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
      [goalId, k.title, k.measure, k.measure === 'boolean' ? 0 : k.startValue, k.measure === 'boolean' ? 1 : k.targetValue,
        k.currentValue ?? (k.measure === 'boolean' ? 0 : k.startValue), k.unit ?? null, k.weight, sort++]);
  }
}

export function goalRoutes(r: Router, { db }: AppDeps): void {
  r.get('/departments', async (req, res) => {
    const rows = await db.query(`SELECT id, code, name_en FROM org_unit WHERE company_id = $1 AND kind = 'department' ORDER BY name_en`, [companyIdOf(req)]);
    res.json({ departments: rows.rows });
  });

  /** Colleagues for pickers (feedback, peer reviewers): name, designation and department only. */
  r.get('/colleagues', async (req, res) => {
    const q = z.object({ q: z.string().trim().max(60).optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT e.id, e.emp_code, e.name_en, e.designation, ou.name_en AS department, a.manager_id
         FROM employee e
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
         LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
        WHERE e.company_id = $1 AND e.status = 'active' AND ($2::text IS NULL OR e.name_en ILIKE '%' || $2 || '%' OR e.emp_code ILIKE $2 || '%')
        ORDER BY e.name_en LIMIT 500`, [companyIdOf(req), q.q || null]);
    res.json({ colleagues: rows.rows });
  });

  r.get('/goals', async (req, res) => {
    const q = z.object({
      scope: z.enum(['mine', 'team', 'company', 'employee', 'all']).default('mine'),
      employeeId: uuid.optional(),
      status: z.enum(['draft', 'active', 'completed', 'cancelled']).optional(),
    }).parse(req.query);
    const u = req.user!; const companyId = companyIdOf(req);
    const status = q.status ?? null;
    let rows;
    if (q.scope === 'company') {
      rows = await db.query(`${GOAL_SELECT} WHERE g.company_id = $1 AND g.scope <> 'individual' AND ($2::text IS NULL OR g.status = $2) AND g.status <> 'cancelled' ORDER BY g.scope, g.due_date NULLS LAST, g.title`, [companyId, status]);
    } else if (q.scope === 'mine' || q.scope === 'employee') {
      const employeeId = q.scope === 'mine' ? u.employeeId : q.employeeId;
      if (!employeeId) { res.json({ goals: [] }); return; }
      await employeeInCompany(db, companyId, employeeId);
      if (!(await canSeeEmployee(db, u, employeeId))) throw new ForbiddenError('not allowed to see these goals');
      rows = await db.query(`${GOAL_SELECT} WHERE g.company_id = $1 AND g.scope = 'individual' AND g.employee_id = $2 AND ($3::text IS NULL OR g.status = $3) ORDER BY g.status = 'cancelled', g.due_date NULLS LAST, g.created_at`, [companyId, employeeId, status]);
    } else if (q.scope === 'all') {
      if (!hasRole(u, 'hr')) throw new ForbiddenError('requires HR');
      rows = await db.query(`${GOAL_SELECT} WHERE g.company_id = $1 AND g.scope = 'individual' AND ($2::text IS NULL OR g.status = $2) ORDER BY e.name_en, g.due_date NULLS LAST`, [companyId, status]);
    } else {
      if (!u.employeeId) {
        if (!hasRole(u, 'hr')) { res.json({ goals: [] }); return; }
        rows = await db.query(`${GOAL_SELECT} WHERE g.company_id = $1 AND g.scope = 'individual' AND ($2::text IS NULL OR g.status = $2) ORDER BY e.name_en`, [companyId, status]);
      } else {
        rows = await db.query(`${SUBORDINATES_CTE.replace('$MGR', '$3')} ${GOAL_SELECT}
          WHERE g.company_id = $1 AND g.scope = 'individual' AND g.employee_id IN (SELECT emp FROM subs) AND ($2::text IS NULL OR g.status = $2)
          ORDER BY e.name_en, g.due_date NULLS LAST`, [companyId, status, u.employeeId]);
      }
    }
    res.json({ goals: rows.rows });
  });

  r.get('/goals/:id', async (req, res) => {
    const g = await loadGoal(db, companyIdOf(req), uuid.parse(req.params.id));
    if (!(await canViewGoal(db, req.user!, g))) throw new NotFoundError('goal not found');
    const row = await db.query(`${GOAL_SELECT} WHERE g.id = $1`, [g.id]);
    const checkins = await db.query(
      `SELECT c.id, c.key_result_id, k.title AS key_result_title, c.value, c.progress, c.confidence, c.comment, c.created_at, u.name AS author_name
         FROM perf_checkin c LEFT JOIN perf_key_result k ON k.id = c.key_result_id LEFT JOIN app_user u ON u.id = c.author_id
        WHERE c.goal_id = $1 ORDER BY c.created_at DESC LIMIT 100`, [g.id]);
    const children = await db.query(`SELECT id, title, scope, progress, status FROM perf_goal WHERE parent_id = $1 AND status <> 'cancelled' ORDER BY title`, [g.id]);
    res.json({ goal: row.rows[0], checkins: checkins.rows, children: children.rows, canEdit: await canEditGoal(db, req.user!, g) });
  });

  r.post('/goals', async (req, res) => {
    const b = goalBody.parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    let employeeId: string | null = null; let orgUnitId: string | null = null;
    if (b.scope === 'individual') {
      employeeId = b.employeeId ?? u.employeeId;
      if (!employeeId) throw new BadRequestError('choose whose goal this is');
      await employeeInCompany(db, companyId, employeeId);
      if (employeeId !== u.employeeId && !(await isAboveInChain(db, u, employeeId))) throw new ForbiddenError('you can set goals only for yourself and your team');
    } else if (b.scope === 'department') {
      if (!b.orgUnitId) throw new BadRequestError('choose the department');
      const ou = await db.query(`SELECT 1 FROM org_unit WHERE id = $1 AND company_id = $2`, [b.orgUnitId, companyId]);
      if (!ou.rows[0]) throw new NotFoundError('department not found');
      orgUnitId = b.orgUnitId;
    }
    if (!(await canEditGoal(db, u, { scope: b.scope, org_unit_id: orgUnitId, employee_id: employeeId }))) throw new ForbiddenError(b.scope === 'company' ? 'only HR sets company goals' : 'not allowed to set this goal');
    if (b.parentId) {
      const p = await loadGoal(db, companyId, b.parentId);
      if (!(await canViewGoal(db, u, p))) throw new NotFoundError('goal not found');
    }
    if (b.startDate && b.dueDate && b.dueDate < b.startDate) throw new BadRequestError('the due date is before the start date');
    const id = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO perf_goal (company_id, scope, org_unit_id, employee_id, parent_id, title, description, weight, start_date, due_date, status, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id`,
        [companyId, b.scope, orgUnitId, employeeId, b.parentId ?? null, b.title, b.description ?? null, b.weight, b.startDate ?? null, b.dueDate ?? null, b.status, u.id]);
      const gid = ins.rows[0]!.id;
      await insertKeyResults(c, gid, b.keyResults);
      await recomputeGoalProgress(c, gid);
      if (employeeId && employeeId !== u.employeeId) {
        await notify(c, { companyId, userId: await userIdForEmployee(c, employeeId), employeeId, template: 'goal.assigned', title: 'New goal', body: `${u.name} set a goal for you: ${b.title}`, payload: { goalId: gid } });
      }
      return gid;
    });
    res.status(201).json({ id });
  });

  r.patch('/goals/:id', async (req, res) => {
    const b = goalBody.omit({ scope: true, employeeId: true, orgUnitId: true, keyResults: true, status: true }).partial().extend({
      status: z.enum(['draft', 'active', 'completed', 'cancelled']).optional(),
      progress: z.coerce.number().min(0).max(100).optional(),
    }).parse(req.body);
    const companyId = companyIdOf(req);
    const g = await loadGoal(db, companyId, uuid.parse(req.params.id));
    await assertCanEdit(db, req.user!, g);
    if (b.parentId) {
      if (b.parentId === g.id) throw new BadRequestError('a goal cannot be aligned to itself');
      await loadGoal(db, companyId, b.parentId);
    }
    if (b.progress !== undefined) {
      const k = await db.query('SELECT 1 FROM perf_key_result WHERE goal_id = $1 LIMIT 1', [g.id]);
      if (k.rows[0]) throw new BadRequestError('progress comes from the key results; update those instead');
    }
    const sets: string[] = []; const vals: unknown[] = [g.id];
    const map: Record<string, string> = { title: 'title', description: 'description', weight: 'weight', startDate: 'start_date', dueDate: 'due_date', status: 'status', progress: 'progress', parentId: 'parent_id' };
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(v); sets.push(`${col} = $${vals.length}`); }
    }
    if (sets.length) await db.query(`UPDATE perf_goal SET ${sets.join(', ')}, updated_at = now() WHERE id = $1`, vals);
    res.json({ ok: true });
  });

  r.delete('/goals/:id', async (req, res) => {
    const g = await loadGoal(db, companyIdOf(req), uuid.parse(req.params.id));
    await assertCanEdit(db, req.user!, g);
    await db.query(`UPDATE perf_goal SET status = 'cancelled', updated_at = now() WHERE id = $1`, [g.id]);
    await audit(db, { actorId: req.user!.id, action: 'cancel', table: 'perf_goal', rowId: g.id, before: { title: g.title, status: g.status } });
    res.json({ ok: true });
  });

  r.post('/goals/:id/key-results', async (req, res) => {
    const b = keyResultBody.parse(req.body);
    const g = await loadGoal(db, companyIdOf(req), uuid.parse(req.params.id));
    await assertCanEdit(db, req.user!, g);
    await tx(db, async (c) => { await insertKeyResults(c, g.id, [b]); await recomputeGoalProgress(c, g.id); });
    res.status(201).json({ ok: true });
  });

  const krGoal = async (companyId: string, krId: string) => {
    const r0 = await db.query<{ goal_id: string; measure: string }>(
      'SELECT k.goal_id, k.measure FROM perf_key_result k JOIN perf_goal g ON g.id = k.goal_id WHERE k.id = $1 AND g.company_id = $2', [krId, companyId]);
    if (!r0.rows[0]) throw new NotFoundError('key result not found');
    return { goal: await loadGoal(db, companyId, r0.rows[0].goal_id), measure: r0.rows[0].measure };
  };

  r.patch('/key-results/:id', async (req, res) => {
    const b = keyResultBody.partial().parse(req.body);
    const id = uuid.parse(req.params.id);
    const { goal } = await krGoal(companyIdOf(req), id);
    await assertCanEdit(db, req.user!, goal);
    const map: Record<string, string> = { title: 'title', measure: 'measure', startValue: 'start_value', targetValue: 'target_value', currentValue: 'current_value', unit: 'unit', weight: 'weight' };
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(v); sets.push(`${col} = $${vals.length}`); }
    }
    await tx(db, async (c) => {
      if (sets.length) await c.query(`UPDATE perf_key_result SET ${sets.join(', ')} WHERE id = $1`, vals);
      await recomputeGoalProgress(c, goal.id);
    });
    res.json({ ok: true });
  });

  r.delete('/key-results/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { goal } = await krGoal(companyIdOf(req), id);
    await assertCanEdit(db, req.user!, goal);
    await tx(db, async (c) => { await c.query('DELETE FROM perf_key_result WHERE id = $1', [id]); await recomputeGoalProgress(c, goal.id); });
    res.json({ ok: true });
  });

  r.post('/goals/:id/checkins', async (req, res) => {
    const b = z.object({
      keyResultId: uuid.optional(),
      value: num.optional(),
      progress: z.coerce.number().min(0).max(100).optional(),
      confidence: z.coerce.number().int().min(1).max(3).optional(),
      comment: z.string().trim().max(2000).optional(),
    }).refine((x) => x.value !== undefined || x.progress !== undefined || x.comment, 'add a value, progress or a comment').parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const g = await loadGoal(db, companyId, uuid.parse(req.params.id));
    await assertCanEdit(db, u, g, 'check in on this goal');
    if (g.status === 'cancelled') throw new BadRequestError('this goal was cancelled');
    if (b.value !== undefined && !b.keyResultId) throw new BadRequestError('choose the key result the value belongs to');
    await tx(db, async (c) => {
      if (b.keyResultId) {
        const k = await c.query('SELECT 1 FROM perf_key_result WHERE id = $1 AND goal_id = $2', [b.keyResultId, g.id]);
        if (!k.rows[0]) throw new NotFoundError('key result not found');
        if (b.value !== undefined) await c.query('UPDATE perf_key_result SET current_value = $2 WHERE id = $1', [b.keyResultId, b.value]);
        await recomputeGoalProgress(c, g.id);
      } else if (b.progress !== undefined) {
        const k = await c.query('SELECT 1 FROM perf_key_result WHERE goal_id = $1 LIMIT 1', [g.id]);
        if (k.rows[0]) throw new BadRequestError('progress comes from the key results; update those instead');
        await c.query('UPDATE perf_goal SET progress = $2, updated_at = now() WHERE id = $1', [g.id, b.progress]);
      }
      const after = await c.query<{ progress: string }>('SELECT progress FROM perf_goal WHERE id = $1', [g.id]);
      await c.query(`INSERT INTO perf_checkin (goal_id, key_result_id, author_id, value, progress, confidence, comment) VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [g.id, b.keyResultId ?? null, u.id, b.value ?? null, after.rows[0]!.progress, b.confidence ?? null, b.comment ?? null]);
      // The other side of the conversation hears about it: the owner's manager, or the owner when someone else checked in.
      if (g.scope === 'individual' && g.employee_id) {
        const target = g.employee_id === u.employeeId ? await directManager(c, g.employee_id) : g.employee_id;
        if (target) {
          await notify(c, { companyId, userId: await userIdForEmployee(c, target), employeeId: target, template: 'goal.checkin', title: 'Goal update',
            body: `${u.name} updated "${g.title}" (${Math.round(Number(after.rows[0]!.progress))}%)${b.comment ? `: ${b.comment.slice(0, 140)}` : ''}`, payload: { goalId: g.id } });
        }
      }
    });
    res.status(201).json({ ok: true });
  });

  // ---------------------------------------------------------------- KPI library
  r.get('/kpis', async (req, res) => {
    const q = z.object({ designation: z.string().trim().max(120).optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT id, designation, title, description, measure, default_target, unit, active FROM perf_kpi
        WHERE company_id = $1 AND (active OR $3) AND ($2::text IS NULL OR designation IS NULL OR lower(designation) = lower($2))
        ORDER BY designation NULLS FIRST, title`, [companyIdOf(req), q.designation || null, hasRole(req.user!, 'hr')]);
    res.json({ kpis: rows.rows });
  });

  const kpiBody = z.object({
    designation: z.string().trim().max(120).nullish(),
    title: z.string().trim().min(1).max(200),
    description: z.string().trim().max(2000).nullish(),
    measure: measure.default('number'),
    defaultTarget: num.default(100),
    unit: z.string().trim().max(20).nullish(),
    active: z.boolean().default(true),
  });

  r.post('/kpis', requireRole('hr'), async (req, res) => {
    const b = kpiBody.parse(req.body);
    const r0 = await db.query<{ id: string }>(
      `INSERT INTO perf_kpi (company_id, designation, title, description, measure, default_target, unit, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
      [companyIdOf(req), b.designation || null, b.title, b.description ?? null, b.measure, b.defaultTarget, b.unit ?? null, b.active]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.patch('/kpis/:id', requireRole('hr'), async (req, res) => {
    const b = kpiBody.partial().parse(req.body);
    const id = uuid.parse(req.params.id);
    const map: Record<string, string> = { designation: 'designation', title: 'title', description: 'description', measure: 'measure', defaultTarget: 'default_target', unit: 'unit', active: 'active' };
    const sets: string[] = []; const vals: unknown[] = [id, companyIdOf(req)];
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(v === '' ? null : v); sets.push(`${col} = $${vals.length}`); }
    }
    if (!sets.length) { res.json({ ok: true }); return; }
    const r0 = await db.query(`UPDATE perf_kpi SET ${sets.join(', ')} WHERE id = $1 AND company_id = $2`, vals);
    if (!r0.rowCount) throw new NotFoundError('KPI not found');
    res.json({ ok: true });
  });

  r.delete('/kpis/:id', requireRole('hr'), async (req, res) => {
    const r0 = await db.query('UPDATE perf_kpi SET active = false WHERE id = $1 AND company_id = $2', [uuid.parse(req.params.id), companyIdOf(req)]);
    if (!r0.rowCount) throw new NotFoundError('KPI not found');
    res.json({ ok: true });
  });

  /** Turn a library KPI into an individual goal with one key result. */
  r.post('/kpis/:id/apply', async (req, res) => {
    const b = z.object({ employeeId: uuid.optional(), dueDate: isoDate.nullish(), weight: z.coerce.number().min(0).max(100).default(0), target: num.optional() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const k = await db.query<{ title: string; description: string | null; measure: string; default_target: string; unit: string | null }>(
      'SELECT title, description, measure, default_target, unit FROM perf_kpi WHERE id = $1 AND company_id = $2 AND active', [uuid.parse(req.params.id), companyId]);
    const kpi = k.rows[0];
    if (!kpi) throw new NotFoundError('KPI not found');
    const employeeId = b.employeeId ?? u.employeeId;
    if (!employeeId) throw new BadRequestError('choose whose goal this is');
    await employeeInCompany(db, companyId, employeeId);
    if (employeeId !== u.employeeId && !(await isAboveInChain(db, u, employeeId))) throw new ForbiddenError('you can set goals only for yourself and your team');
    const id = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO perf_goal (company_id, scope, employee_id, title, description, weight, due_date, created_by) VALUES ($1, 'individual', $2, $3, $4, $5, $6, $7) RETURNING id`,
        [companyId, employeeId, kpi.title, kpi.description, b.weight, b.dueDate ?? null, u.id]);
      const gid = ins.rows[0]!.id;
      await insertKeyResults(c, gid, [{ title: kpi.title, measure: kpi.measure as 'number', startValue: 0, targetValue: b.target ?? Number(kpi.default_target), unit: kpi.unit, weight: 1 }]);
      return gid;
    });
    res.status(201).json({ id });
  });
}
