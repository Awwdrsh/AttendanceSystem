import { Router, type Request } from 'express';
import { z } from 'zod';
import { bsLabel } from '../../bsdate.js';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, money } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { assertManages, cancelWorkflow, isManagerOf, nptToday, registerHandler, registerSummary, startWorkflow } from '../workflow/service.js';
import {
  applyConfirmation, applyProbationDecision, applyResignationDecision, canCompleteTask, canViewCase, clearanceBlockers, completeCaseIfDone,
  completeExit, ensureLifecycleDefaults, EXIT_KINDS, lifecyclePolicy, lifecyclePolicySchema, recordExit, responsibleUsers, settlementStatus, startCase,
  type TaskRow,
} from './service.js';

registerHandler('probation_review', applyProbationDecision);
registerHandler('resignation', applyResignationDecision);
registerSummary('probation_review', async (db, inst) => {
  const r = await db.query<{ rating: number; recommendation: string; extend_to: string | null; last_working_day: string | null; probation_end: string | null; comments: string }>(
    'SELECT rating, recommendation, extend_to, last_working_day, probation_end, comments FROM probation_review WHERE id = $1', [inst.object_id]);
  const x = r.rows[0];
  if (!x) return {};
  const rec = x.recommendation === 'confirm' ? 'confirm' : x.recommendation === 'extend' ? `extend probation to ${bsLabel(x.extend_to)}` : `end employment on ${bsLabel(x.last_working_day)}`;
  return { headline: `Probation review: ${rec}`, detail: `Rating ${x.rating} of 5${x.probation_end ? `, probation ends ${bsLabel(x.probation_end)}` : ''}`, note: x.comments, ...x };
});
registerSummary('resignation', async (db, inst) => {
  const r = await db.query<{ proposed_lwd: string; accepted_lwd: string | null; reason: string | null; reason_category: string | null; notice_days: number; submitted_on: string }>(
    'SELECT proposed_lwd, accepted_lwd, reason, reason_category, notice_days, submitted_on FROM employee_exit WHERE id = $1', [inst.object_id]);
  const x = r.rows[0];
  if (!x) return {};
  const lwd = x.accepted_lwd ?? x.proposed_lwd;
  const shortfall = Math.max(0, x.notice_days - diffDays(lwd, x.submitted_on));
  return {
    headline: `Resignation, last working day ${bsLabel(lwd)}`, detail: shortfall ? `Notice short by ${shortfall} ${shortfall === 1 ? 'day' : 'days'}` : 'Full notice given',
    note: [x.reason_category, x.reason].filter(Boolean).join(': ') || null, ...x,
  };
});

const uuid = z.uuid();
const ASSIGNEE = z.enum(['hr', 'manager', 'employee', 'user']);
const pageLink = z.string().max(200).regex(/^\/[A-Za-z0-9/_?=&-]*$/, 'a page inside Hajiri, starting with /');
const templateTask = z.object({
  title: z.string().trim().min(1).max(160), description: z.string().max(1000).nullish(), department: z.string().max(60).nullish(),
  assigneeType: ASSIGNEE, assigneeUserId: z.uuid().nullish(), dueOffsetDays: z.number().int().min(-90).max(365).default(0), link: pageLink.nullish(),
}).refine((t) => t.assigneeType !== 'user' || !!t.assigneeUserId, { message: 'choose the person for this task', path: ['assigneeUserId'] });
const templateBody = z.object({
  kind: z.enum(['onboarding', 'offboarding']), name: z.string().trim().min(1).max(120), description: z.string().max(1000).nullish(),
  branchId: z.uuid().nullish(), orgUnitId: z.uuid().nullish(), isDefault: z.boolean().default(false), active: z.boolean().default(true),
  tasks: z.array(templateTask).max(60),
});

interface EmpRow { id: string; name_en: string; join_date: string | null; probation_end: string | null; confirmed_on: string | null; status: string; exit_date: string | null }
async function employeeIn(db: Queryable, companyId: string, id: string): Promise<EmpRow> {
  const r = await db.query<EmpRow>('SELECT id, name_en, join_date, probation_end, confirmed_on, status, exit_date FROM employee WHERE id = $1 AND company_id = $2', [id, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

async function usersInCompany(db: Queryable, companyId: string, ids: Array<string | null | undefined>): Promise<void> {
  const list = [...new Set(ids.filter((x): x is string => !!x))];
  if (!list.length) return;
  const r = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM app_user WHERE company_id = $1 AND id = ANY($2) AND status = 'active'`, [companyId, list]);
  if (r.rows[0]!.n !== list.length) throw new BadRequestError('the chosen person is not an active user in your company');
}

function diffDays(a: string, b: string): number { return Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / 86_400_000); }
const uniqueViolation = (msg: string) => (err: { code?: string }) => { if (err.code === '23505') throw new ConflictError(msg); throw err; };

/** Employee lifecycle: onboarding and exit checklists, probation, resignations and exits, and the asset register. */
export function lifecycleRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const tx = async <T>(fn: (c: Queryable) => Promise<T>): Promise<T> => {
    const client = await db.connect();
    try { await client.query('BEGIN'); const out = await fn(client); await client.query('COMMIT'); return out; }
    catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  };
  const isHr = (req: Request) => hasRole(req.user!, 'hr');
  // Defaults are seeded on first use, so companies that existed before this module get them too.
  r.use(async (req, _res, next) => { if (req.user?.companyId) await ensureLifecycleDefaults(db, req.user.companyId); next(); });

  // ------------------------------------------------------------ settings
  r.get('/settings', async (req, res) => { res.json({ policy: await lifecyclePolicy(db, companyIdOf(req)) }); });
  r.put('/settings', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const policy = lifecyclePolicySchema.parse(req.body);
    const before = await lifecyclePolicy(db, companyId);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, 'lifecycle.policy', $2, $3)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`, [companyId, JSON.stringify(policy), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: 'lifecycle.policy', before, after: policy });
    res.json({ policy });
  });

  // ------------------------------------------------------------ templates
  r.get('/templates', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ kind: z.enum(['onboarding', 'offboarding']).optional() }).parse(req.query);
    const t = await db.query(
      `SELECT t.*, b.name_en AS branch_name, o.name_en AS org_unit_name,
              coalesce((SELECT json_agg(x ORDER BY x.sort) FROM lifecycle_template_task x WHERE x.template_id = t.id), '[]') AS tasks
         FROM lifecycle_template t LEFT JOIN branch b ON b.id = t.branch_id LEFT JOIN org_unit o ON o.id = t.org_unit_id
        WHERE t.company_id = $1 AND ($2::text IS NULL OR t.kind = $2) ORDER BY t.kind, t.is_default DESC, t.name`, [companyIdOf(req), q.kind ?? null]);
    res.json({ templates: t.rows });
  });

  const saveTemplate = async (req: Request, id: string | null) => {
    const companyId = companyIdOf(req);
    const b = templateBody.parse(req.body);
    await usersInCompany(db, companyId, b.tasks.map((t) => t.assigneeUserId));
    for (const [table, ref] of [['branch', b.branchId], ['org_unit', b.orgUnitId]] as const) {
      if (ref && !(await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [ref, companyId])).rows.length) throw new BadRequestError(`${table.replace('_', ' ')} not found`);
    }
    return tx(async (c) => {
      let tid = id;
      if (b.isDefault) await c.query('UPDATE lifecycle_template SET is_default = false WHERE company_id = $1 AND kind = $2', [companyId, b.kind]);
      if (tid) {
        const u = await c.query(
          `UPDATE lifecycle_template SET name = $3, description = $4, branch_id = $5, org_unit_id = $6, is_default = $7, active = $8, updated_at = now()
            WHERE id = $1 AND company_id = $2 AND kind = $9 RETURNING id`,
          [tid, companyId, b.name, b.description ?? null, b.branchId ?? null, b.orgUnitId ?? null, b.isDefault, b.active, b.kind]);
        if (!u.rows.length) throw new NotFoundError('template not found');
        await c.query('DELETE FROM lifecycle_template_task WHERE template_id = $1', [tid]);
      } else {
        const ins = await c.query<{ id: string }>(
          `INSERT INTO lifecycle_template (company_id, kind, name, description, branch_id, org_unit_id, is_default, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
          [companyId, b.kind, b.name, b.description ?? null, b.branchId ?? null, b.orgUnitId ?? null, b.isDefault, b.active]).catch(uniqueViolation('a checklist with this name already exists'));
        tid = ins.rows[0]!.id;
      }
      for (const [i, t] of b.tasks.entries()) {
        await c.query(
          `INSERT INTO lifecycle_template_task (template_id, sort, title, description, department, assignee_type, assignee_user_id, due_offset_days, link) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
          [tid, i, t.title, t.description ?? null, t.department ?? null, t.assigneeType, t.assigneeType === 'user' ? t.assigneeUserId : null, t.dueOffsetDays, t.link ?? null]);
      }
      await audit(c, { actorId: req.user!.id, action: id ? 'update' : 'create', table: 'lifecycle_template', rowId: tid, after: { name: b.name, kind: b.kind, tasks: b.tasks.length } });
      return tid!;
    });
  };
  r.post('/templates', requireRole('hr'), async (req, res) => { res.status(201).json({ id: await saveTemplate(req, null) }); });
  r.put('/templates/:id', requireRole('hr'), async (req, res) => { res.json({ id: await saveTemplate(req, uuid.parse(req.params.id)) }); });

  // ------------------------------------------------------------ cases and tasks
  r.get('/cases', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ kind: z.enum(['onboarding', 'offboarding']).optional(), status: z.enum(['active', 'completed', 'cancelled']).optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT c.id, c.kind, c.status, c.anchor_date, c.created_at, c.completed_at, c.exit_id, c.employee_id, e.emp_code, e.name_en, e.designation, e.join_date,
              t.name AS template_name,
              (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id) AS total,
              (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status <> 'open') AS done,
              (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status = 'open' AND x.due_date < current_date) AS overdue,
              (SELECT min(x.due_date) FROM lifecycle_task x WHERE x.case_id = c.id AND x.status = 'open') AS next_due
         FROM lifecycle_case c JOIN employee e ON e.id = c.employee_id LEFT JOIN lifecycle_template t ON t.id = c.template_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> greatest(current_date, c.anchor_date)
        WHERE c.company_id = $1 AND ($2::text IS NULL OR c.kind = $2) AND ($3::text IS NULL OR c.status = $3)
          AND ($4 OR a.manager_id = $5::uuid)
        ORDER BY c.status = 'active' DESC, c.anchor_date DESC LIMIT 500`,
      [companyIdOf(req), q.kind ?? null, q.status ?? null, isHr(req), req.user!.employeeId]);
    res.json({ cases: rows.rows });
  });

  r.post('/cases', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ employeeId: z.uuid(), templateId: z.uuid().nullish(), anchorDate: isoDate.optional() }).parse(req.body);
    const e = await employeeIn(db, companyId, b.employeeId);
    if (e.status === 'exited') throw new BadRequestError('this employee has left');
    const id = await tx((c) => startCase(c, { companyId, employeeId: b.employeeId, kind: 'onboarding', templateId: b.templateId ?? null, anchorDate: b.anchorDate ?? e.join_date ?? nptToday(), startedBy: req.user!.id }));
    res.status(201).json({ id });
  });

  interface CaseRow { id: string; company_id: string; employee_id: string; kind: string; status: string; anchor_date: string; exit_id: string | null; template_id: string | null; created_at: Date; completed_at: Date | null }
  const loadCase = async (req: Request, id: string): Promise<CaseRow> => {
    const c = await db.query<CaseRow>('SELECT * FROM lifecycle_case WHERE id = $1', [id]);
    const row = c.rows[0];
    if (!row || !(await canViewCase(db, req.user!, row))) throw new NotFoundError('checklist not found');
    return row;
  };

  r.get('/cases/:id', async (req, res) => {
    const c = await loadCase(req, uuid.parse(req.params.id));
    const emp = await db.query('SELECT id, emp_code, name_en, designation, join_date, exit_date FROM employee WHERE id = $1', [c.employee_id]);
    const tasks = await db.query<TaskRow & Record<string, unknown>>(
      `SELECT t.*, u.name AS assignee_name, d.name AS done_by_name, (SELECT count(*)::int FROM lifecycle_task_comment m WHERE m.task_id = t.id) AS comments
         FROM lifecycle_task t LEFT JOIN app_user u ON u.id = t.assignee_user_id LEFT JOIN app_user d ON d.id = t.done_by
        WHERE t.case_id = $1 ORDER BY t.sort, t.due_date`, [c.id]);
    const withRights = await Promise.all(tasks.rows.map(async (t) => ({ ...t, can_complete: await canCompleteTask(db, req.user!, t) })));
    res.json({ case: c, employee: emp.rows[0], tasks: withRights });
  });

  r.post('/cases/:id/cancel', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const c = await loadCase(req, id);
    if (c.status !== 'active') throw new ConflictError(`checklist is ${c.status}`);
    if (c.exit_id) throw new BadRequestError('cancel the exit instead; its clearance checklist closes with it');
    await db.query(`UPDATE lifecycle_case SET status = 'cancelled', completed_at = now() WHERE id = $1`, [id]);
    await audit(db, { actorId: req.user!.id, action: 'cancel', table: 'lifecycle_case', rowId: id });
    res.json({ ok: true });
  });

  r.post('/cases/:id/tasks', requireRole('hr'), async (req, res) => {
    const c = await loadCase(req, uuid.parse(req.params.id));
    if (c.status !== 'active') throw new ConflictError(`checklist is ${c.status}`);
    const b = z.object({
      title: z.string().trim().min(1).max(160), description: z.string().max(1000).nullish(), department: z.string().max(60).nullish(), assigneeType: ASSIGNEE,
      assigneeUserId: z.uuid().nullish(), dueDate: isoDate.nullish(), link: pageLink.nullish(),
    }).parse(req.body);
    if (b.assigneeType === 'user' && !b.assigneeUserId) throw new BadRequestError('choose the person for this task');
    await usersInCompany(db, c.company_id, [b.assigneeUserId]);
    const ins = await db.query<TaskRow>(
      `INSERT INTO lifecycle_task (case_id, company_id, employee_id, sort, title, description, department, assignee_type, assignee_user_id, due_date, link)
       VALUES ($1, $2, $3, (SELECT coalesce(max(sort), 0) + 1 FROM lifecycle_task WHERE case_id = $1), $4, $5, $6, $7, $8, $9, $10) RETURNING *`,
      [c.id, c.company_id, c.employee_id, b.title, b.description ?? null, b.department ?? null, b.assigneeType, b.assigneeType === 'user' ? b.assigneeUserId : null, b.dueDate ?? null, b.link ?? null]);
    for (const userId of await responsibleUsers(db, ins.rows[0]!)) {
      if (userId !== req.user!.id) await notify(db, { companyId: c.company_id, userId, template: 'lifecycle.task.assigned', title: 'New checklist task', body: b.title, payload: { caseId: c.id } });
    }
    res.status(201).json({ task: ins.rows[0] });
  });

  const loadTask = async (req: Request, id: string): Promise<TaskRow> => {
    const t = await db.query<TaskRow>('SELECT * FROM lifecycle_task WHERE id = $1', [id]);
    const row = t.rows[0];
    if (!row) throw new NotFoundError('task not found');
    await loadCase(req, row.case_id);
    return row;
  };

  r.patch('/tasks/:id', async (req, res) => {
    const t = await loadTask(req, uuid.parse(req.params.id));
    const b = z.object({
      status: z.enum(['open', 'done', 'skipped']).optional(), note: z.string().max(1000).nullish(), dueDate: isoDate.nullish(),
      assigneeType: ASSIGNEE.optional(), assigneeUserId: z.uuid().nullish(),
    }).parse(req.body);
    const caseRow = (await db.query<{ status: string }>('SELECT status FROM lifecycle_case WHERE id = $1', [t.case_id])).rows[0]!;
    if (caseRow.status === 'cancelled') throw new ConflictError('this checklist was cancelled');
    const editsPlan = b.dueDate !== undefined || b.assigneeType !== undefined || b.assigneeUserId !== undefined;
    if (editsPlan && !isHr(req)) throw new ForbiddenError('only HR can change due dates and owners');
    if ((b.status !== undefined || b.note !== undefined) && !(await canCompleteTask(db, req.user!, t))) throw new ForbiddenError('this task is for someone else');
    if (b.status === 'skipped' && !isHr(req)) throw new ForbiddenError('only HR can skip a task');
    if (b.assigneeType === 'user' && !b.assigneeUserId) throw new BadRequestError('choose the person for this task');
    await usersInCompany(db, t.company_id, [b.assigneeUserId]);
    const out = await tx(async (c) => {
      const u = await c.query<TaskRow>(
        `UPDATE lifecycle_task SET
           status = coalesce($2, status), note = CASE WHEN $3::boolean THEN $4 ELSE note END,
           done_by = CASE WHEN $2 IN ('done', 'skipped') THEN $5::uuid WHEN $2 = 'open' THEN NULL ELSE done_by END,
           done_at = CASE WHEN $2 IN ('done', 'skipped') THEN now() WHEN $2 = 'open' THEN NULL ELSE done_at END,
           due_date = CASE WHEN $6::boolean THEN $7::date ELSE due_date END,
           assignee_type = coalesce($8, assignee_type),
           assignee_user_id = CASE WHEN $8::text IS NULL THEN assignee_user_id WHEN $8 = 'user' THEN $9::uuid ELSE NULL END
         WHERE id = $1 RETURNING *`,
        [t.id, b.status ?? null, b.note !== undefined, b.note ?? null, req.user!.id, b.dueDate !== undefined, b.dueDate ?? null, b.assigneeType ?? null, b.assigneeUserId ?? null]);
      // Clearance checklists stay open until the exit is completed; onboarding closes itself when everything is ticked.
      let completed = false;
      if (b.status === 'done' || b.status === 'skipped') {
        const k = await c.query<{ exit_id: string | null }>('SELECT exit_id FROM lifecycle_case WHERE id = $1', [t.case_id]);
        if (!k.rows[0]?.exit_id) completed = await completeCaseIfDone(c, t.case_id);
      }
      if (b.status === 'open') await c.query(`UPDATE lifecycle_case SET status = 'active', completed_at = NULL WHERE id = $1 AND status = 'completed' AND exit_id IS NULL`, [t.case_id]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'lifecycle_task', rowId: t.id, before: { status: t.status }, after: b });
      return { task: u.rows[0], caseCompleted: completed };
    });
    res.json(out);
  });

  r.get('/tasks/:id/comments', async (req, res) => {
    const t = await loadTask(req, uuid.parse(req.params.id));
    const rows = await db.query('SELECT m.id, m.body, m.created_at, m.author_id, u.name AS author_name FROM lifecycle_task_comment m JOIN app_user u ON u.id = m.author_id WHERE m.task_id = $1 ORDER BY m.created_at', [t.id]);
    res.json({ comments: rows.rows });
  });
  r.post('/tasks/:id/comments', async (req, res) => {
    const t = await loadTask(req, uuid.parse(req.params.id));
    const b = z.object({ body: z.string().trim().min(1).max(2000) }).parse(req.body);
    const ins = await db.query('INSERT INTO lifecycle_task_comment (task_id, author_id, body) VALUES ($1, $2, $3) RETURNING id, body, created_at', [t.id, req.user!.id, b.body]);
    for (const userId of await responsibleUsers(db, t)) {
      if (userId !== req.user!.id) await notify(db, { companyId: t.company_id, userId, template: 'lifecycle.task.comment', title: `Comment on "${t.title}"`, body: b.body.slice(0, 200), payload: { caseId: t.case_id, taskId: t.id } });
    }
    res.status(201).json({ comment: ins.rows[0] });
  });

  /** Tasks the signed-in person should act on: their own checklist items, tasks for their team or assigned to them, and HR tasks for HR. */
  r.get('/my-tasks', async (req, res) => {
    const u = req.user!;
    const rows = await db.query(
      `SELECT t.*, c.kind, e.name_en AS employee_name, e.emp_code, (t.employee_id = $2::uuid) AS own
         FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id JOIN employee e ON e.id = t.employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = t.employee_id AND a.valid @> greatest(current_date, c.anchor_date)
        WHERE t.company_id = $1 AND c.status = 'active' AND t.status = 'open'
          AND ((t.assignee_type = 'employee' AND t.employee_id = $2::uuid)
            OR (t.assignee_type = 'user' AND t.assignee_user_id = $3)
            OR (t.assignee_type = 'manager' AND a.manager_id = $2::uuid)
            OR ($4 AND (t.assignee_type = 'hr' OR (t.assignee_type = 'manager' AND a.manager_id IS NULL))))
        ORDER BY t.due_date NULLS LAST, t.sort LIMIT 200`,
      [companyIdOf(req), u.employeeId, u.id, isHr(req)]);
    res.json({ tasks: rows.rows });
  });

  // ------------------------------------------------------------ one employee at a glance (employee page)
  r.get('/employees/:id/summary', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const e = await employeeIn(db, companyId, id);
    await assertManages(db, req.user!, id);
    const cases = await db.query(
      `SELECT c.id, c.kind, c.status, c.anchor_date, c.created_at, (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id) AS total,
              (SELECT count(*)::int FROM lifecycle_task x WHERE x.case_id = c.id AND x.status <> 'open') AS done
         FROM lifecycle_case c WHERE c.employee_id = $1 ORDER BY c.created_at DESC`, [id]);
    const reviews = await db.query(
      `SELECT r.id, r.rating, r.recommendation, r.extend_to, r.last_working_day, r.status, r.created_at, r.probation_end, u.name AS submitted_by_name
         FROM probation_review r JOIN app_user u ON u.id = r.submitted_by WHERE r.employee_id = $1 ORDER BY r.created_at DESC`, [id]);
    const confirmations = await db.query('SELECT id, confirmed_on, review_id, created_at FROM employee_confirmation WHERE employee_id = $1 ORDER BY created_at DESC', [id]);
    const exits = await db.query('SELECT id, kind, status, voluntary, reason_category, proposed_lwd, accepted_lwd, submitted_on FROM employee_exit WHERE employee_id = $1 ORDER BY created_at DESC', [id]);
    const assets = await db.query(
      `SELECT g.id AS assignment_id, g.assigned_on, g.returned_on, g.condition_out, g.condition_in, a.id, a.tag, a.name, a.serial, t.name AS type_name
         FROM asset_assignment g JOIN asset a ON a.id = g.asset_id LEFT JOIN asset_type t ON t.id = a.type_id
        WHERE g.employee_id = $1 ORDER BY g.returned_on IS NULL DESC, g.assigned_on DESC`, [id]);
    res.json({
      employee: e, cases: cases.rows, probation: { end: e.probation_end, confirmedOn: e.confirmed_on, reviews: reviews.rows, confirmations: confirmations.rows },
      exits: exits.rows, assets: assets.rows,
    });
  });

  // ------------------------------------------------------------ probation
  r.get('/probation', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ within: z.coerce.number().int().min(1).max(365).default(60) }).parse(req.query);
    const rows = await db.query(
      `SELECT e.id, e.emp_code, e.name_en, e.designation, e.join_date, e.probation_end, (e.probation_end - current_date) AS days_left,
              m.name_en AS manager_name,
              (SELECT row_to_json(x) FROM (SELECT r.id, r.status, r.recommendation, r.created_at FROM probation_review r WHERE r.employee_id = e.id ORDER BY r.created_at DESC LIMIT 1) x) AS last_review
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date LEFT JOIN employee m ON m.id = a.manager_id
        WHERE e.company_id = $1 AND e.status = 'active' AND e.probation_end IS NOT NULL AND e.confirmed_on IS NULL
          AND (e.exit_date IS NULL OR e.exit_date >= current_date)
          AND e.probation_end <= current_date + $2::int AND ($3 OR a.manager_id = $4::uuid)
        ORDER BY e.probation_end`, [companyIdOf(req), q.within, isHr(req), req.user!.employeeId]);
    res.json({ employees: rows.rows });
  });

  r.post('/probation/reviews', requireRole('hr', 'manager'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      employeeId: z.uuid(), rating: z.number().int().min(1).max(5), recommendation: z.enum(['confirm', 'extend', 'terminate']),
      extendTo: isoDate.nullish(), lastWorkingDay: isoDate.nullish(), comments: z.string().trim().min(3).max(4000),
    }).parse(req.body);
    const e = await employeeIn(db, companyId, b.employeeId);
    if (b.employeeId === req.user!.employeeId) throw new ForbiddenError('you cannot review your own probation');
    await assertManages(db, req.user!, b.employeeId);
    if (e.status !== 'active') throw new BadRequestError('this employee has left');
    if (e.confirmed_on) throw new ConflictError(`already confirmed on ${bsLabel(e.confirmed_on)}`);
    if (b.recommendation === 'extend') {
      if (!b.extendTo) throw new BadRequestError('choose the new probation end date');
      if (b.extendTo <= (e.probation_end ?? nptToday())) throw new BadRequestError('the new end date must be after the current probation end');
    }
    if (b.recommendation === 'terminate') {
      if (!b.lastWorkingDay) throw new BadRequestError('choose the last working day');
      if (e.join_date && b.lastWorkingDay < e.join_date) throw new BadRequestError('the last working day cannot be before the join date');
    }
    const id = await tx(async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO probation_review (company_id, employee_id, probation_end, rating, recommendation, extend_to, last_working_day, comments, submitted_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
        [companyId, b.employeeId, e.probation_end, b.rating, b.recommendation, b.recommendation === 'extend' ? b.extendTo : null,
          b.recommendation === 'terminate' ? b.lastWorkingDay : null, b.comments, req.user!.id]);
      const rid = ins.rows[0]!.id;
      await startWorkflow(c, {
        companyId, objectType: 'probation_review', objectId: rid, employeeId: b.employeeId, title: 'Probation review to approve',
        body: `${e.name_en}: ${b.recommendation === 'confirm' ? 'confirm' : b.recommendation === 'extend' ? 'extend probation' : 'end employment'}`,
      });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'probation_review', rowId: rid, after: { rating: b.rating, recommendation: b.recommendation } });
      return rid;
    }).catch(uniqueViolation('a probation review is already waiting for approval'));
    res.status(201).json({ id });
  });

  r.post('/probation/confirm', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ employeeId: z.uuid(), confirmedOn: isoDate }).parse(req.body);
    const e = await employeeIn(db, companyId, b.employeeId);
    if (e.confirmed_on) throw new ConflictError(`already confirmed on ${bsLabel(e.confirmed_on)}`);
    if (e.join_date && b.confirmedOn < e.join_date) throw new BadRequestError('confirmation cannot be before the join date');
    const id = await tx((c) => applyConfirmation(c, { companyId, employeeId: b.employeeId, confirmedOn: b.confirmedOn, reviewId: null, actorId: req.user!.id }));
    res.status(201).json({ id });
  });

  // ------------------------------------------------------------ exits
  const exitCols = `x.id, x.employee_id, x.kind, x.voluntary, x.reason_category, x.reason, x.submitted_on, x.notice_days, x.proposed_lwd, x.accepted_lwd, x.status,
    x.decided_at, x.completed_at, x.created_at, e.emp_code, e.name_en, e.designation, e.join_date,
    (SELECT c.id FROM lifecycle_case c WHERE c.exit_id = x.id ORDER BY c.created_at DESC LIMIT 1) AS case_id,
    (SELECT count(*)::int FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id WHERE c.exit_id = x.id AND c.status <> 'cancelled') AS tasks_total,
    (SELECT count(*)::int FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id WHERE c.exit_id = x.id AND c.status <> 'cancelled' AND t.status <> 'open') AS tasks_done,
    (SELECT count(*)::int FROM asset_assignment g WHERE g.employee_id = x.employee_id AND g.returned_on IS NULL) AS assets_held,
    EXISTS (SELECT 1 FROM exit_interview i WHERE i.exit_id = x.id) AS interview_done`;

  r.get('/exits', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ status: z.enum(['pending', 'accepted', 'rejected', 'withdrawn', 'cancelled', 'completed', 'open']).optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT ${exitCols} FROM employee_exit x JOIN employee e ON e.id = x.employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> least(current_date, coalesce(x.accepted_lwd, x.proposed_lwd))
        WHERE x.company_id = $1 AND ($2::text IS NULL OR x.status = $2 OR ($2 = 'open' AND x.status IN ('pending', 'accepted')))
          AND ($3 OR a.manager_id = $4::uuid)
        ORDER BY x.status IN ('pending', 'accepted') DESC, coalesce(x.accepted_lwd, x.proposed_lwd) DESC LIMIT 500`,
      [companyIdOf(req), q.status ?? null, isHr(req), req.user!.employeeId]);
    res.json({ exits: rows.rows });
  });

  r.get('/exits/mine', async (req, res) => {
    const policy = await lifecyclePolicy(db, companyIdOf(req));
    if (!req.user!.employeeId) return res.json({ exits: [], policy });
    const rows = await db.query(`SELECT ${exitCols} FROM employee_exit x JOIN employee e ON e.id = x.employee_id WHERE x.employee_id = $1 ORDER BY x.created_at DESC`, [req.user!.employeeId]);
    res.json({ exits: rows.rows, policy });
  });

  r.post('/exits/resign', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('your account is not linked to an employee record');
    const b = z.object({ proposedLastWorkingDay: isoDate, reasonCategory: z.string().trim().max(60).nullish(), reason: z.string().trim().max(2000).nullish() }).parse(req.body);
    const e = await employeeIn(db, companyId, employeeId);
    if (e.status !== 'active') throw new BadRequestError('your employment has already ended');
    const today = nptToday();
    if (b.proposedLastWorkingDay < today) throw new BadRequestError('the last working day cannot be in the past');
    if (diffDays(b.proposedLastWorkingDay, today) > 365) throw new BadRequestError('the last working day must be within a year');
    const policy = await lifecyclePolicy(db, companyId);
    const id = await tx(async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO employee_exit (company_id, employee_id, kind, voluntary, reason_category, reason, submitted_on, notice_days, proposed_lwd, initiated_by)
         VALUES ($1, $2, 'resignation', true, $3, $4, $5, $6, $7, $8) RETURNING id`,
        [companyId, employeeId, b.reasonCategory ?? null, b.reason ?? null, today, policy.noticeDays, b.proposedLastWorkingDay, req.user!.id]);
      const xid = ins.rows[0]!.id;
      await startWorkflow(c, { companyId, objectType: 'resignation', objectId: xid, employeeId, title: 'Resignation submitted', body: `${e.name_en} has resigned. Proposed last working day ${bsLabel(b.proposedLastWorkingDay)}.` });
      await audit(c, { actorId: req.user!.id, action: 'resign', table: 'employee_exit', rowId: xid, after: { proposedLwd: b.proposedLastWorkingDay } });
      return xid;
    }).catch(uniqueViolation('you already have a resignation in progress'));
    const shortfall = Math.max(0, policy.noticeDays - diffDays(b.proposedLastWorkingDay, today));
    res.status(201).json({ id, noticeDays: policy.noticeDays, shortfallDays: shortfall });
  });

  r.post('/exits', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      employeeId: z.uuid(), kind: z.enum(EXIT_KINDS), lastWorkingDay: isoDate, voluntary: z.boolean().optional(),
      reasonCategory: z.string().trim().max(60).nullish(), reason: z.string().trim().max(2000).nullish(), templateId: z.uuid().nullish(),
    }).parse(req.body);
    const e = await employeeIn(db, companyId, b.employeeId);
    if (e.status !== 'active') throw new BadRequestError('this employee has already left');
    if (e.join_date && b.lastWorkingDay < e.join_date) throw new BadRequestError('the last working day cannot be before the join date');
    if (b.employeeId === req.user!.employeeId) throw new ForbiddenError('another HR user must record your own exit');
    const id = await tx((c) => recordExit(c, {
      companyId, employeeId: b.employeeId, kind: b.kind, voluntary: b.voluntary ?? (b.kind === 'resignation' || b.kind === 'retirement'),
      reasonCategory: b.reasonCategory ?? null, reason: b.reason ?? null, lastWorkingDay: b.lastWorkingDay, actorId: req.user!.id, templateId: b.templateId ?? null,
    }));
    res.status(201).json({ id });
  });

  interface ExitRow { id: string; company_id: string; employee_id: string; status: string; kind: string; proposed_lwd: string; accepted_lwd: string | null }
  const loadExit = async (req: Request, id: string): Promise<ExitRow> => {
    const r0 = await db.query<ExitRow>('SELECT id, company_id, employee_id, status, kind, proposed_lwd, accepted_lwd FROM employee_exit WHERE id = $1', [id]);
    const x = r0.rows[0];
    if (!x || x.company_id !== req.user!.companyId) throw new NotFoundError('exit not found');
    const u = req.user!;
    const lwd = x.accepted_lwd ?? x.proposed_lwd;
    const asOf = lwd < nptToday() ? lwd : nptToday();
    const allowed = hasRole(u, 'hr') || u.employeeId === x.employee_id || (!!u.employeeId && hasRole(u, 'manager') && (await isManagerOf(db, u.employeeId, x.employee_id, asOf)));
    if (!allowed) throw new NotFoundError('exit not found');
    return x;
  };

  r.get('/exits/:id', async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const row = await db.query(`SELECT ${exitCols} FROM employee_exit x JOIN employee e ON e.id = x.employee_id WHERE x.id = $1`, [x.id]);
    res.json({
      exit: row.rows[0], blockers: await clearanceBlockers(db, x.id, x.employee_id),
      settlement: isHr(req) ? await settlementStatus(db, x.company_id, x.employee_id, x.accepted_lwd ?? x.proposed_lwd) : null,
    });
  });

  r.patch('/exits/:id', requireRole('hr'), async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const b = z.object({ lastWorkingDay: isoDate.optional(), voluntary: z.boolean().optional(), reasonCategory: z.string().trim().max(60).nullish(), reason: z.string().trim().max(2000).nullish() }).parse(req.body);
    if (!['pending', 'accepted'].includes(x.status)) throw new ConflictError(`exit is ${x.status}`);
    await tx(async (c) => {
      await c.query(
        `UPDATE employee_exit SET voluntary = coalesce($2, voluntary), reason_category = CASE WHEN $3::boolean THEN $4 ELSE reason_category END,
           reason = CASE WHEN $5::boolean THEN $6 ELSE reason END, accepted_lwd = coalesce($7::date, accepted_lwd) WHERE id = $1`,
        [x.id, b.voluntary ?? null, b.reasonCategory !== undefined, b.reasonCategory ?? null, b.reason !== undefined, b.reason ?? null, b.lastWorkingDay ?? null]);
      if (b.lastWorkingDay && x.status === 'accepted') {
        await c.query('UPDATE employee SET exit_date = $2, updated_at = now() WHERE id = $1', [x.employee_id, b.lastWorkingDay]);
        // Clearance due dates follow the new last working day.
        await c.query(`UPDATE lifecycle_task t SET due_date = t.due_date + ($2::date - c.anchor_date) FROM lifecycle_case c
                        WHERE c.id = t.case_id AND c.exit_id = $1 AND c.status = 'active' AND t.due_date IS NOT NULL`, [x.id, b.lastWorkingDay]);
        await c.query(`UPDATE lifecycle_case SET anchor_date = $2 WHERE exit_id = $1 AND status = 'active'`, [x.id, b.lastWorkingDay]);
        const own = await userIdForEmployee(c, x.employee_id);
        if (own) await notify(c, { companyId: x.company_id, userId: own, employeeId: x.employee_id, template: 'lifecycle.exit.lwd_changed', title: 'Last working day changed', body: `Your last working day is now ${bsLabel(b.lastWorkingDay)}.`, payload: { exitId: x.id } });
      }
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'employee_exit', rowId: x.id, after: b });
    });
    res.json({ ok: true });
  });

  r.post('/exits/:id/withdraw', async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    if (x.employee_id !== req.user!.employeeId) throw new ForbiddenError('only the employee can withdraw their resignation');
    if (x.status !== 'pending') throw new ConflictError(x.status === 'accepted' ? 'your resignation was already accepted; ask HR to cancel it' : `resignation is ${x.status}`);
    await tx(async (c) => {
      await c.query(`UPDATE employee_exit SET status = 'withdrawn', decided_at = now() WHERE id = $1`, [x.id]);
      await cancelWorkflow(c, 'resignation', x.id);
      await audit(c, { actorId: req.user!.id, action: 'withdraw', table: 'employee_exit', rowId: x.id });
    });
    res.json({ ok: true });
  });

  r.post('/exits/:id/cancel', requireRole('hr'), async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const b = z.object({ reason: z.string().trim().min(3).max(500) }).parse(req.body);
    if (!['pending', 'accepted'].includes(x.status)) throw new ConflictError(`exit is ${x.status}`);
    await tx(async (c) => {
      await c.query(`UPDATE employee_exit SET status = 'cancelled', decided_at = now(), reason = concat_ws(E'\\n', reason, $2::text) WHERE id = $1`, [x.id, `Cancelled: ${b.reason}`]);
      await cancelWorkflow(c, 'resignation', x.id);
      if (x.status === 'accepted') {
        await c.query('UPDATE employee SET exit_date = NULL, updated_at = now() WHERE id = $1 AND exit_date = $2', [x.employee_id, x.accepted_lwd]);
        await c.query(`UPDATE lifecycle_case SET status = 'cancelled', completed_at = now() WHERE exit_id = $1 AND status = 'active'`, [x.id]);
      }
      await audit(c, { actorId: req.user!.id, action: 'cancel', table: 'employee_exit', rowId: x.id, after: b });
    });
    res.json({ ok: true });
  });

  r.post('/exits/:id/complete', requireRole('hr'), async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const b = z.object({ skipOpenTasks: z.boolean().default(false) }).parse(req.body ?? {});
    await tx((c) => completeExit(c, x.id, req.user!.id, b));
    res.json({ ok: true });
  });

  // Exit interview: questions are company-wide; answers are encrypted and only HR can read them.
  r.get('/exit-questions', async (req, res) => {
    const rows = await db.query('SELECT id, sort, question, kind, options, active FROM exit_interview_question WHERE company_id = $1 AND (active OR $2) ORDER BY sort', [companyIdOf(req), isHr(req)]);
    res.json({ questions: rows.rows });
  });
  r.put('/exit-questions', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      questions: z.array(z.object({
        id: z.uuid().optional(), question: z.string().trim().min(1).max(300), kind: z.enum(['text', 'rating', 'choice']),
        options: z.array(z.string().trim().min(1).max(80)).max(12).default([]), active: z.boolean().default(true),
      }).refine((q) => q.kind !== 'choice' || q.options.length >= 2, { message: 'a choice question needs at least two options' })).max(30),
    }).parse(req.body);
    await tx(async (c) => {
      const keep: string[] = [];
      for (const [i, q] of b.questions.entries()) {
        if (q.id) {
          const u = await c.query('UPDATE exit_interview_question SET sort = $3, question = $4, kind = $5, options = $6, active = $7 WHERE id = $1 AND company_id = $2 RETURNING id',
            [q.id, companyId, i, q.question, q.kind, JSON.stringify(q.options), q.active]);
          if (!u.rows.length) throw new BadRequestError('question not found');
          keep.push(q.id);
        } else {
          const ins = await c.query<{ id: string }>('INSERT INTO exit_interview_question (company_id, sort, question, kind, options, active) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
            [companyId, i, q.question, q.kind, JSON.stringify(q.options), q.active]);
          keep.push(ins.rows[0]!.id);
        }
      }
      // Questions are never deleted (old answers refer to them); dropped ones are switched off.
      await c.query('UPDATE exit_interview_question SET active = false WHERE company_id = $1 AND NOT (id = ANY($2::uuid[]))', [companyId, keep]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'exit_interview_question', after: { count: b.questions.length } });
    });
    res.json({ ok: true });
  });

  const interviewAad = (exitId: string) => `exit_interview:${exitId}`;
  r.get('/exits/:id/interview', requireRole('hr'), async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const row = await db.query<{ answers_enc: Buffer; by_employee: boolean; created_at: Date; recorded_by_name: string }>(
      'SELECT i.answers_enc, i.by_employee, i.created_at, u.name AS recorded_by_name FROM exit_interview i JOIN app_user u ON u.id = i.recorded_by WHERE i.exit_id = $1', [x.id]);
    const i = row.rows[0];
    if (!i) return res.json({ interview: null });
    await audit(db, { actorId: req.user!.id, action: 'read_exit_interview', table: 'exit_interview', rowId: x.id });
    res.json({ interview: { answers: JSON.parse(cipher.decrypt(i.answers_enc, interviewAad(x.id))), byEmployee: i.by_employee, createdAt: i.created_at, recordedBy: i.recorded_by_name } });
  });
  r.post('/exits/:id/interview', async (req, res) => {
    const x = await loadExit(req, uuid.parse(req.params.id));
    const own = req.user!.employeeId === x.employee_id;
    if (!own && !isHr(req)) throw new ForbiddenError('only the leaver or HR can record the exit interview');
    if (own && !(await lifecyclePolicy(db, x.company_id)).selfExitInterview) throw new ForbiddenError('HR will hold your exit interview in person');
    if (!['pending', 'accepted', 'completed'].includes(x.status)) throw new ConflictError(`exit is ${x.status}`);
    const b = z.object({ answers: z.array(z.object({ questionId: z.uuid(), answer: z.union([z.string().max(3000), z.number().int().min(1).max(5)]) })).min(1).max(30) }).parse(req.body);
    const qs = await db.query<{ id: string; question: string; kind: string; options: string[] }>('SELECT id, question, kind, options FROM exit_interview_question WHERE company_id = $1', [x.company_id]);
    const byId = new Map(qs.rows.map((q) => [q.id, q]));
    const answers = b.answers.map((a) => {
      const q = byId.get(a.questionId);
      if (!q) throw new BadRequestError('unknown question');
      if (q.kind === 'rating' && typeof a.answer !== 'number') throw new BadRequestError(`"${q.question}" needs a rating from 1 to 5`);
      if (q.kind === 'choice' && (typeof a.answer !== 'string' || !q.options.includes(a.answer))) throw new BadRequestError(`"${q.question}" needs one of the listed options`);
      if (q.kind === 'text' && typeof a.answer !== 'string') throw new BadRequestError(`"${q.question}" needs a written answer`);
      return { questionId: q.id, question: q.question, kind: q.kind, answer: a.answer };
    });
    await tx(async (c) => {
      const ins = await c.query(
        `INSERT INTO exit_interview (exit_id, company_id, answers_enc, by_employee, recorded_by) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (exit_id) DO NOTHING RETURNING exit_id`,
        [x.id, x.company_id, cipher.encrypt(JSON.stringify(answers), interviewAad(x.id)), own, req.user!.id]);
      if (!ins.rows.length) throw new ConflictError('the exit interview has already been recorded');
      await c.query(`UPDATE lifecycle_task t SET status = 'done', done_by = $2, done_at = now() FROM lifecycle_case c
                      WHERE c.id = t.case_id AND c.exit_id = $1 AND t.status = 'open' AND t.title ILIKE 'exit interview%'`, [x.id, req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'record_exit_interview', table: 'exit_interview', rowId: x.id, after: { byEmployee: own } });
    });
    res.status(201).json({ ok: true });
  });

  // ------------------------------------------------------------ assets
  r.get('/asset-types', async (req, res) => {
    const rows = await db.query('SELECT id, name FROM asset_type WHERE company_id = $1 ORDER BY name', [companyIdOf(req)]);
    res.json({ types: rows.rows });
  });
  r.post('/asset-types', requireRole('hr'), async (req, res) => {
    const b = z.object({ name: z.string().trim().min(1).max(80) }).parse(req.body);
    const ins = await db.query('INSERT INTO asset_type (company_id, name) VALUES ($1, $2) RETURNING id, name', [companyIdOf(req), b.name]).catch(uniqueViolation('this asset type already exists'));
    res.status(201).json({ type: ins.rows[0] });
  });

  const assetCols = `a.id, a.tag, a.name, a.serial, a.condition, a.status, a.purchase_date, a.cost, a.notes, a.type_id, t.name AS type_name, a.created_at,
    g.id AS assignment_id, g.employee_id, g.assigned_on, e.name_en AS employee_name, e.emp_code`;
  r.get('/assets', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ status: z.enum(['available', 'assigned', 'retired']).optional(), employeeId: z.uuid().optional(), search: z.string().max(80).optional() }).parse(req.query);
    if (q.employeeId) await assertManages(db, req.user!, q.employeeId);
    else if (!isHr(req)) throw new ForbiddenError('choose one of your team members');
    const rows = await db.query(
      `SELECT ${assetCols} FROM asset a LEFT JOIN asset_type t ON t.id = a.type_id
         LEFT JOIN asset_assignment g ON g.asset_id = a.id AND g.returned_on IS NULL LEFT JOIN employee e ON e.id = g.employee_id
        WHERE a.company_id = $1 AND ($2::text IS NULL OR a.status = $2) AND ($3::uuid IS NULL OR g.employee_id = $3)
          AND ($4::text IS NULL OR a.tag ILIKE '%' || $4 || '%' OR a.name ILIKE '%' || $4 || '%' OR a.serial ILIKE '%' || $4 || '%')
        ORDER BY a.tag LIMIT 1000`, [companyIdOf(req), q.status ?? null, q.employeeId ?? null, q.search ? q.search.replace(/[\\%_]/g, (ch) => `\\${ch}`) : null]);
    res.json({ assets: rows.rows });
  });
  r.get('/assets/mine', async (req, res) => {
    if (!req.user!.employeeId) return res.json({ assets: [] });
    const rows = await db.query(
      `SELECT ${assetCols} FROM asset_assignment g JOIN asset a ON a.id = g.asset_id LEFT JOIN asset_type t ON t.id = a.type_id JOIN employee e ON e.id = g.employee_id
        WHERE g.employee_id = $1 AND g.returned_on IS NULL ORDER BY g.assigned_on`, [req.user!.employeeId]);
    res.json({ assets: rows.rows });
  });
  r.get('/assets/:id/history', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const rows = await db.query(
      `SELECT g.id, g.employee_id, e.name_en, e.emp_code, g.assigned_on, g.returned_on, g.condition_out, g.condition_in, g.notes
         FROM asset_assignment g JOIN employee e ON e.id = g.employee_id WHERE g.asset_id = $1 AND g.company_id = $2 ORDER BY g.assigned_on DESC`, [id, companyIdOf(req)]);
    res.json({ history: rows.rows });
  });

  const assetBody = z.object({
    tag: z.string().trim().min(1).max(40), name: z.string().trim().min(1).max(120), typeId: z.uuid().nullish(), serial: z.string().trim().max(80).nullish(),
    condition: z.enum(['new', 'good', 'fair', 'poor', 'damaged', 'lost']).default('good'), purchaseDate: isoDate.nullish(), cost: money.nullish(), notes: z.string().max(1000).nullish(),
  });
  const checkType = async (companyId: string, typeId: string | null | undefined) => {
    if (typeId && !(await db.query('SELECT 1 FROM asset_type WHERE id = $1 AND company_id = $2', [typeId, companyId])).rows.length) throw new BadRequestError('asset type not found');
  };
  r.post('/assets', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = assetBody.parse(req.body);
    await checkType(companyId, b.typeId);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO asset (company_id, type_id, tag, name, serial, condition, purchase_date, cost, notes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
      [companyId, b.typeId ?? null, b.tag, b.name, b.serial ?? null, b.condition, b.purchaseDate ?? null, b.cost ?? null, b.notes ?? null]).catch(uniqueViolation('an asset with this tag already exists'));
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'asset', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });
  r.patch('/assets/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = assetBody.partial().extend({ status: z.enum(['available', 'retired']).optional() }).parse(req.body);
    await checkType(companyId, b.typeId);
    const cur = await db.query<{ status: string }>('SELECT status FROM asset WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!cur.rows[0]) throw new NotFoundError('asset not found');
    if (b.status && cur.rows[0].status === 'assigned') throw new ConflictError('record the return before changing the status of this asset');
    const cols: Array<[keyof typeof b, string]> = [['tag', 'tag'], ['name', 'name'], ['typeId', 'type_id'], ['serial', 'serial'], ['condition', 'condition'], ['purchaseDate', 'purchase_date'], ['cost', 'cost'], ['notes', 'notes'], ['status', 'status']];
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, col] of cols) if (b[k] !== undefined) { vals.push(b[k]); sets.push(`${col} = $${vals.length}`); }
    if (sets.length) await db.query(`UPDATE asset SET ${sets.join(', ')} WHERE id = $1`, vals).catch(uniqueViolation('an asset with this tag already exists'));
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'asset', rowId: id, after: b });
    res.json({ ok: true });
  });
  r.post('/assets/:id/assign', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ employeeId: z.uuid(), assignedOn: isoDate.optional(), notes: z.string().max(500).nullish() }).parse(req.body);
    const e = await employeeIn(db, companyId, b.employeeId);
    if (e.status !== 'active') throw new BadRequestError('this employee has left');
    await tx(async (c) => {
      const a = await c.query<{ status: string; condition: string; name: string; tag: string }>('SELECT status, condition, name, tag FROM asset WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const asset = a.rows[0];
      if (!asset) throw new NotFoundError('asset not found');
      if (asset.status !== 'available') throw new ConflictError(asset.status === 'assigned' ? 'this asset is already with someone; record the return first' : 'this asset is retired');
      await c.query(
        `INSERT INTO asset_assignment (asset_id, company_id, employee_id, assigned_on, condition_out, notes, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [id, companyId, b.employeeId, b.assignedOn ?? nptToday(), asset.condition, b.notes ?? null, req.user!.id]);
      await c.query(`UPDATE asset SET status = 'assigned' WHERE id = $1`, [id]);
      await audit(c, { actorId: req.user!.id, action: 'assign', table: 'asset', rowId: id, after: { employeeId: b.employeeId } });
      const own = await userIdForEmployee(c, b.employeeId);
      if (own) await notify(c, { companyId, userId: own, employeeId: b.employeeId, template: 'lifecycle.asset.assigned', title: 'Company asset handed to you', body: `${asset.name} (${asset.tag}). Please return it when you leave.`, payload: { assetId: id } });
    });
    res.json({ ok: true });
  });
  r.post('/assets/:id/return', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ returnedOn: isoDate.optional(), condition: z.enum(['new', 'good', 'fair', 'poor', 'damaged', 'lost']), notes: z.string().max(500).nullish() }).parse(req.body);
    await tx(async (c) => {
      const g = await c.query<{ id: string; assigned_on: string; employee_id: string }>(
        'SELECT g.id, g.assigned_on, g.employee_id FROM asset_assignment g JOIN asset a ON a.id = g.asset_id WHERE g.asset_id = $1 AND a.company_id = $2 AND g.returned_on IS NULL FOR UPDATE OF g', [id, companyId]);
      const open = g.rows[0];
      if (!open) throw new ConflictError('this asset is not with anyone');
      const on = b.returnedOn ?? nptToday();
      if (on < open.assigned_on) throw new BadRequestError('the return date cannot be before it was handed over');
      await c.query('UPDATE asset_assignment SET returned_on = $2, condition_in = $3, notes = coalesce($4, notes), returned_by = $5 WHERE id = $1', [open.id, on, b.condition, b.notes ?? null, req.user!.id]);
      // A lost asset leaves service; anything else is available again.
      await c.query(`UPDATE asset SET status = CASE WHEN $2 = 'lost' THEN 'retired' ELSE 'available' END, condition = $2 WHERE id = $1`, [id, b.condition]);
      await audit(c, { actorId: req.user!.id, action: 'return', table: 'asset', rowId: id, after: { employeeId: open.employee_id, condition: b.condition, returnedOn: on } });
    });
    res.json({ ok: true });
  });

  return r;
}
