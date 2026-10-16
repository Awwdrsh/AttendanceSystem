import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { assertManages, companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import {
  chooseFlow, conditionsSchema, fellBackToHr, normaliseStep, REQUEST_TYPE_SET, REQUEST_TYPES, resolveStep, stepLabel, stepsForAmount, stepsSchema,
  visibleNotes, type FlowStep,
} from './flows.js';
import { nptToday } from './service.js';

const hr = requireRole('hr');

async function checkStepRefs(db: Queryable, companyId: string, steps: FlowStep[]): Promise<void> {
  for (const s of steps) {
    if (s.userId) {
      const r = await db.query('SELECT 1 FROM app_user WHERE id = $1 AND company_id = $2', [s.userId, companyId]);
      if (!r.rows[0]) throw new BadRequestError('the approver chosen is not a user of your company');
    }
    if (s.groupId) {
      const r = await db.query('SELECT 1 FROM user_group WHERE id = $1 AND company_id = $2', [s.groupId, companyId]);
      if (!r.rows[0]) throw new BadRequestError('the approver group does not exist');
    }
  }
}

async function checkConditionRefs(db: Queryable, companyId: string, c: z.infer<typeof conditionsSchema>): Promise<void> {
  const check = async (table: string, ids: string[], label: string) => {
    if (!ids.length) return;
    const r = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM ${table} WHERE id = ANY($1::uuid[]) AND company_id = $2`, [ids, companyId]);
    if (r.rows[0]!.n !== new Set(ids).size) throw new BadRequestError(`one of the ${label} is not in your company`);
  };
  await check('employee', c.employeeIds, 'employees');
  await check('user_group', c.groupIds, 'groups');
  await check('designation', c.designationIds, 'designations');
  await check('org_unit', c.orgUnitIds, 'departments');
  await check('branch', c.branchIds, 'branches');
}

const typeParam = z.string().refine((t) => REQUEST_TYPE_SET.has(t), 'unknown request type');

/** Names of the people a resolution points to, for previews and the verify report. */
async function names(db: Queryable, ids: string[]): Promise<Array<{ id: string; name: string }>> {
  if (!ids.length) return [];
  const r = await db.query<{ id: string; name: string }>('SELECT id, name FROM app_user WHERE id = ANY($1::uuid[]) ORDER BY name', [ids]);
  return r.rows;
}

/**
 * Approval flow set-up (HR): default chain per request type, conditional flows, user groups, approver delegation,
 * a preview of who approves a request for someone, and a check that every employee's chains reach a real person.
 */
export function flowRoutes({ db }: AppDeps): Router {
  const r = Router();

  // ------------------------------------------------------------ configuration
  r.get('/flows/config', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const [defs, flows, groups, members] = await Promise.all([
      db.query<{ object_type: string; steps: FlowStep[]; sla_hours: number; escalation_hours: number }>(
        'SELECT object_type, steps, sla_hours, escalation_hours FROM workflow_definition WHERE company_id = $1', [companyId]),
      db.query('SELECT id, object_type, name, conditions, steps, priority, active, updated_at FROM approval_flow WHERE company_id = $1 ORDER BY object_type, priority DESC, created_at', [companyId]),
      db.query('SELECT id, name, description FROM user_group WHERE company_id = $1 ORDER BY name', [companyId]),
      db.query<{ group_id: string; user_id: string; name: string }>(
        `SELECT m.group_id, m.user_id, u.name FROM user_group_member m JOIN user_group g ON g.id = m.group_id JOIN app_user u ON u.id = m.user_id
          WHERE g.company_id = $1 ORDER BY u.name`, [companyId]),
    ]);
    const def = new Map(defs.rows.map((d) => [d.object_type, d]));
    res.json({
      types: REQUEST_TYPES.map((t) => ({
        objectType: t.type, label: t.label, money: !!t.money,
        defaultSteps: def.get(t.type)?.steps ?? [{ kind: 'manager', role: 'manager' }],
        flows: flows.rows.filter((f) => f.object_type === t.type),
      })),
      groups: groups.rows.map((g) => ({ ...g, members: members.rows.filter((m) => m.group_id === g.id).map((m) => ({ id: m.user_id, name: m.name })) })),
    });
  });

  r.put('/flows/defaults/:type', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const type = typeParam.parse(req.params.type);
    const steps = stepsSchema.parse(req.body?.steps).map(normaliseStep);
    await checkStepRefs(db, companyId, steps);
    const before = await db.query('SELECT steps FROM workflow_definition WHERE company_id = $1 AND object_type = $2', [companyId, type]);
    await db.query(
      `INSERT INTO workflow_definition (company_id, object_type, steps) VALUES ($1, $2, $3)
       ON CONFLICT (company_id, object_type) DO UPDATE SET steps = EXCLUDED.steps`, [companyId, type, JSON.stringify(steps)]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'workflow_definition', rowId: null, before: before.rows[0] ?? null, after: { objectType: type, steps } });
    res.json({ objectType: type, steps });
  });

  const flowBody = z.object({
    objectType: typeParam, name: z.string().trim().min(1).max(100), conditions: conditionsSchema.default({ employeeIds: [], groupIds: [], designationIds: [], orgUnitIds: [], branchIds: [] }),
    steps: stepsSchema, priority: z.number().int().min(-100).max(100).default(0), active: z.boolean().default(true),
  });

  r.post('/flows', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = flowBody.parse(req.body);
    const steps = b.steps.map(normaliseStep);
    await checkStepRefs(db, companyId, steps);
    await checkConditionRefs(db, companyId, b.conditions);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO approval_flow (company_id, object_type, name, conditions, steps, priority, active, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
      [companyId, b.objectType, b.name, JSON.stringify(b.conditions), JSON.stringify(steps), b.priority, b.active, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'approval_flow', rowId: ins.rows[0]!.id, after: { ...b, steps } });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.patch('/flows/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const cur = await db.query('SELECT * FROM approval_flow WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!cur.rows[0]) throw new NotFoundError('flow not found');
    const b = flowBody.omit({ objectType: true }).partial().parse(req.body);
    const steps = b.steps ? b.steps.map(normaliseStep) : undefined;
    if (steps) await checkStepRefs(db, companyId, steps);
    if (b.conditions) await checkConditionRefs(db, companyId, b.conditions);
    await db.query(
      `UPDATE approval_flow SET name = coalesce($3, name), conditions = coalesce($4, conditions), steps = coalesce($5, steps), priority = coalesce($6, priority),
              active = coalesce($7, active), updated_at = now() WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.name ?? null, b.conditions ? JSON.stringify(b.conditions) : null, steps ? JSON.stringify(steps) : null, b.priority ?? null, b.active ?? null]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'approval_flow', rowId: id, before: cur.rows[0], after: { ...b, ...(steps ? { steps } : {}) } });
    res.json({ ok: true });
  });

  r.delete('/flows/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const cur = await db.query('DELETE FROM approval_flow WHERE id = $1 AND company_id = $2 RETURNING *', [id, companyId]);
    if (!cur.rows[0]) throw new NotFoundError('flow not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'approval_flow', rowId: id, before: cur.rows[0] });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ user groups
  const groupBody = z.object({ name: z.string().trim().min(1).max(80), description: z.string().trim().max(300).nullable().optional() });

  r.post('/groups', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = groupBody.extend({ userIds: z.array(z.uuid()).max(500).default([]) }).parse(req.body);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const g = await client.query<{ id: string }>('INSERT INTO user_group (company_id, name, description) VALUES ($1, $2, $3) RETURNING id', [companyId, b.name, b.description ?? null]);
      await setMembers(client, companyId, g.rows[0]!.id, b.userIds);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'user_group', rowId: g.rows[0]!.id, after: b });
      await client.query('COMMIT');
      res.status(201).json({ id: g.rows[0]!.id });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.patch('/groups/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const b = groupBody.partial().extend({ userIds: z.array(z.uuid()).max(500).optional() }).parse(req.body);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const cur = await client.query('SELECT id FROM user_group WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      if (!cur.rows[0]) throw new NotFoundError('group not found');
      await client.query('UPDATE user_group SET name = coalesce($2, name), description = CASE WHEN $4 THEN $3 ELSE description END WHERE id = $1',
        [id, b.name ?? null, b.description ?? null, b.description !== undefined]);
      if (b.userIds) await setMembers(client, companyId, id, b.userIds);
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'user_group', rowId: id, after: b });
      await client.query('COMMIT');
      res.json({ ok: true });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.delete('/groups/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const used = await db.query(
      `SELECT name FROM approval_flow WHERE company_id = $1 AND (steps @> $2::jsonb OR conditions->'groupIds' ? $3)
       UNION ALL SELECT object_type FROM workflow_definition WHERE company_id = $1 AND steps @> $2::jsonb LIMIT 1`,
      [companyId, JSON.stringify([{ groupId: id }]), id]);
    if (used.rows[0]) throw new BadRequestError(`this group is used in an approval flow (${String(used.rows[0].name).replace(/_/g, ' ')}); change the flow first`);
    const cur = await db.query('DELETE FROM user_group WHERE id = $1 AND company_id = $2 RETURNING *', [id, companyId]);
    if (!cur.rows[0]) throw new NotFoundError('group not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'user_group', rowId: id, before: cur.rows[0] });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ delegation
  /** My delegations (and, for HR, everyone's): while active, the delegate also receives and decides my approvals. */
  r.get('/delegates', async (req, res) => {
    const companyId = companyIdOf(req);
    const all = hasRole(req.user!, 'hr') && req.query.all === '1';
    const rows = await db.query(
      `SELECT d.id, d.user_id, u.name AS user_name, d.delegate_id, du.name AS delegate_name, d.from_date, d.to_date, d.note, d.cancelled_at, d.created_at
         FROM approver_delegate d JOIN app_user u ON u.id = d.user_id JOIN app_user du ON du.id = d.delegate_id
        WHERE d.company_id = $1 AND ($2 OR d.user_id = $3 OR d.delegate_id = $3) AND (d.cancelled_at IS NULL) AND d.to_date >= current_date - 30
        ORDER BY d.from_date DESC LIMIT 200`, [companyId, all, req.user!.id]);
    res.json({ delegates: rows.rows });
  });

  /** Colleagues who can be chosen as a delegate (active users of the company; names only). */
  r.get('/delegates/candidates', async (req, res) => {
    const rows = await db.query(`SELECT id, name FROM app_user WHERE company_id = $1 AND status = 'active' AND id <> $2 ORDER BY name LIMIT 1000`, [companyIdOf(req), req.user!.id]);
    res.json({ users: rows.rows });
  });

  r.post('/delegates', async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ userId: z.uuid().optional(), delegateId: z.uuid(), from: isoDate, to: isoDate, note: z.string().trim().max(300).optional() }).parse(req.body);
    const userId = b.userId ?? req.user!.id;
    if (userId !== req.user!.id && !hasRole(req.user!, 'hr')) throw new ForbiddenError('only HR can set a delegate for someone else');
    if (b.to < b.from) throw new BadRequestError('the end date is before the start date');
    if (b.to < nptToday()) throw new BadRequestError('the delegation has already ended');
    if (userId === b.delegateId) throw new BadRequestError('choose someone else as the delegate');
    const ok = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM app_user WHERE id = ANY($1::uuid[]) AND company_id = $2 AND status = 'active'`, [[userId, b.delegateId], companyId]);
    if (ok.rows[0]!.n !== 2) throw new BadRequestError('the delegate must be an active user of your company');
    const ins = await db.query<{ id: string }>(
      'INSERT INTO approver_delegate (company_id, user_id, delegate_id, from_date, to_date, note, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id',
      [companyId, userId, b.delegateId, b.from, b.to, b.note ?? null, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'approver_delegate', rowId: ins.rows[0]!.id, after: { ...b, userId } });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.delete('/delegates/:id', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const cur = await db.query<{ user_id: string }>('SELECT user_id FROM approver_delegate WHERE id = $1 AND company_id = $2 AND cancelled_at IS NULL', [id, companyId]);
    if (!cur.rows[0]) throw new NotFoundError('delegation not found');
    if (cur.rows[0].user_id !== req.user!.id && !hasRole(req.user!, 'hr')) throw new NotFoundError('delegation not found');
    await db.query('UPDATE approver_delegate SET cancelled_at = now() WHERE id = $1', [id]);
    await audit(db, { actorId: req.user!.id, action: 'cancel', table: 'approver_delegate', rowId: id });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ preview and verify
  /** Who will approve request type X for employee Y (default: me), step by step. Shown on request forms. */
  r.get('/preview', async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ objectType: typeParam, employeeId: z.uuid().optional(), amount: z.coerce.number().min(0).max(1e10).optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    if (employeeId !== req.user!.employeeId) await assertManages(db, req.user!, employeeId);
    else {
      const own = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
      if (!own.rows[0]) throw new NotFoundError('employee not found');
    }
    const flow = await chooseFlow(db, companyId, q.objectType, employeeId);
    const all = flow.steps;
    const steps = q.amount !== undefined ? stepsForAmount(all, q.amount) : all;
    const today = nptToday();
    const out = [];
    for (const s of steps) {
      const res2 = await resolveStep(db, companyId, employeeId, s, today);
      out.push({ label: stepLabel(s), minAmount: s.minAmount ?? null, approvers: await names(db, res2.users), notes: visibleNotes(res2), fallback: fellBackToHr(res2) });
    }
    res.json({ flow: flow.flowId ? { id: flow.flowId, name: flow.flowName } : null, steps: out });
  });

  /**
   * Every active employee × request type whose chain does not reach a real approver: a step with nobody set (so HR
   * gets it by default) or a step that would land on the employee themselves. With a fix suggestion per problem.
   */
  r.get('/verify', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ objectType: typeParam.optional() }).parse(req.query);
    const emps = await db.query<{ id: string; emp_code: string; name_en: string }>(
      `SELECT id, emp_code, name_en FROM employee WHERE company_id = $1 AND status = 'active' ORDER BY emp_code`, [companyId]);
    const types = REQUEST_TYPES.filter((t) => t.subject === 'employee' && (!q.objectType || t.type === q.objectType));
    const today = nptToday();
    const cache = new Map<string, Awaited<ReturnType<typeof resolveStep>>>();
    const problems: Array<Record<string, unknown>> = [];
    for (const e of emps.rows) {
      for (const t of types) {
        const flow = await chooseFlow(db, companyId, t.type, e.id);
        for (const [i, s] of flow.steps.entries()) {
          const key = `${e.id}|${JSON.stringify(s)}`;
          let res2 = cache.get(key);
          if (!res2) { res2 = await resolveStep(db, companyId, e.id, s, today); cache.set(key, res2); }
          const notes = visibleNotes(res2);
          if (!fellBackToHr(res2) && !notes.some((n) => n.startsWith('No '))) continue;
          const label = stepLabel(s);
          const kind = s.kind ?? (s.role === 'manager' ? 'manager' : 'role');
          const suggestion = kind === 'manager' ? 'Set a line manager on the employee record'
            : kind === 'manager2' ? "Set a manager for the employee's manager"
              : kind === 'department_head' ? 'Set a head for the department (Employees → Org structure)'
                : kind === 'branch_head' ? 'Set a head for the branch (Employees → Org structure)'
                  : kind === 'group' ? 'Add active members to the group'
                    : kind === 'user' ? 'Choose an active user for this step'
                      : 'Give the role to at least one active user';
          problems.push({ employeeId: e.id, empCode: e.emp_code, name: e.name_en, objectType: t.type, typeLabel: t.label, flow: flow.flowName, step: i + 1, stepLabel: label, problem: notes.join('; ') || 'Nobody can approve', suggestion });
        }
      }
    }
    res.json({ checked: emps.rows.length * types.length, problems });
  });

  return r;
}

async function setMembers(db: Queryable, companyId: string, groupId: string, userIds: string[]): Promise<void> {
  const ids = [...new Set(userIds)];
  if (ids.length) {
    const ok = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM app_user WHERE id = ANY($1::uuid[]) AND company_id = $2', [ids, companyId]);
    if (ok.rows[0]!.n !== ids.length) throw new BadRequestError('one of the members is not a user of your company');
  }
  await db.query('DELETE FROM user_group_member WHERE group_id = $1', [groupId]);
  if (ids.length) await db.query('INSERT INTO user_group_member (group_id, user_id) SELECT $1, unnest($2::uuid[])', [groupId, ids]);
}
