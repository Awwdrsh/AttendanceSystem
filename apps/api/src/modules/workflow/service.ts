import { createHash } from 'node:crypto';
import { localDateOf } from '@hajiri/attendance-engine';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { hasRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { notify, userIdsWithRole } from '../notifications/service.js';

/**
 * Generic multi-level approval engine (plan §5.8): one workflow_instance per request object, steps copied from the
 * company's workflow_definition, signed approval_action rows, and per-object-type decision handlers that apply the
 * business effect (ledger posting, punch creation, ...) in the same transaction as the decision.
 */
export type WorkflowState = 'pending' | 'approved' | 'rejected' | 'cancelled';
export interface WorkflowStep { role: string }

export interface WorkflowInstance {
  id: string;
  company_id: string;
  object_type: string;
  object_id: string;
  employee_id: string | null;
  steps: WorkflowStep[];
  current_step: number;
  state: WorkflowState;
  created_at: Date;
  completed_at: Date | null;
}

export interface DecisionContext {
  actorId: string;
  reason: string | null;
  payload: Record<string, unknown>;
}

export type DecisionHandler = (db: Queryable, objectId: string, state: 'approved' | 'rejected', ctx: DecisionContext) => Promise<void>;

const handlers = new Map<string, DecisionHandler>();

/** Today's calendar date in Nepal (UTC+05:45). Never use new Date().toISOString() for "today": between 00:00 and 05:45 NPT it is still yesterday in UTC. */
export function nptToday(): string { return localDateOf(Date.now()); }

/** True when managerEmployeeId is the line manager of employeeId on `asOf` (default today, NPT). */
export async function isManagerOf(db: Queryable, managerEmployeeId: string, employeeId: string, asOf = nptToday()): Promise<boolean> {
  const r = await db.query('SELECT 1 FROM employee_assignment WHERE employee_id = $1 AND manager_id = $2 AND valid @> $3::date', [employeeId, managerEmployeeId, asOf]);
  return r.rows.length > 0;
}

/**
 * Team-scoped access (plan §12.4 ABAC "own record only" / direct reports): HR and admin may act for anyone in
 * their company; a manager only for direct reports (and self); everyone for themselves. Throws ForbiddenError otherwise.
 * Callers must separately verify the employee belongs to the user's company (404 for other companies).
 */
export async function assertManages(db: Queryable, user: AuthUser, employeeId: string): Promise<void> {
  if (hasRole(user, 'hr')) return;
  if (user.employeeId === employeeId) return;
  if (hasRole(user, 'manager') && user.employeeId && (await isManagerOf(db, user.employeeId, employeeId))) return;
  throw new ForbiddenError('not allowed to act for this employee');
}

/** Each request module registers how an approved/rejected decision is applied to its object. */
export function registerHandler(objectType: string, fn: DecisionHandler): void {
  handlers.set(objectType, fn);
}

export interface StartWorkflowInput {
  companyId: string;
  objectType: string;
  objectId: string;
  employeeId: string | null;
  /** Notification shown to the first approver. */
  title: string;
  body: string;
}

export async function getInstance(db: Queryable, id: string): Promise<WorkflowInstance | null> {
  const r = await db.query<WorkflowInstance>('SELECT * FROM workflow_instance WHERE id = $1', [id]);
  return r.rows[0] ?? null;
}

export async function instanceForObject(db: Queryable, objectType: string, objectId: string): Promise<WorkflowInstance | null> {
  const r = await db.query<WorkflowInstance>('SELECT * FROM workflow_instance WHERE object_type = $1 AND object_id = $2', [objectType, objectId]);
  return r.rows[0] ?? null;
}

/** Users who may act on a step: manager → the subject's line manager (falls back to HR); other roles → role holders. */
export async function approversFor(db: Queryable, companyId: string, employeeId: string | null, role: string, cache?: Map<string, string[]>): Promise<string[]> {
  const cacheKey = `${companyId}|${employeeId ?? ''}|${role}`;
  const hit = cache?.get(cacheKey);
  if (hit) return hit;
  const ids = await approversUncached(db, companyId, employeeId, role);
  cache?.set(cacheKey, ids);
  return ids;
}

async function approversUncached(db: Queryable, companyId: string, employeeId: string | null, role: string): Promise<string[]> {
  if (role === 'manager') {
    if (employeeId) {
      const r = await db.query<{ id: string }>(
        `SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id AND u.status = 'active'
          WHERE a.employee_id = $1 AND a.valid @> current_date`, [employeeId]);
      if (r.rows.length) return r.rows.map((x) => x.id);
    }
    return userIdsWithRole(db, companyId, 'hr');
  }
  return userIdsWithRole(db, companyId, role);
}

/** Admins may act anywhere, nobody may decide their own request. */
export async function canAct(db: Queryable, user: AuthUser, inst: WorkflowInstance, cache?: Map<string, string[]>): Promise<boolean> {
  if (inst.state !== 'pending') return false;
  if (inst.employee_id && user.employeeId === inst.employee_id) return false;
  if (user.companyId !== inst.company_id) return false;
  if (hasRole(user, 'admin')) return true;
  const step = inst.steps[inst.current_step];
  if (!step) return false;
  // HR supervises line managers: it may decide any manager step (plan §3 HR "fix punch on behalf", delegation).
  if (step.role === 'manager' && hasRole(user, 'hr')) return true;
  const ids = await approversFor(db, inst.company_id, inst.employee_id, step.role, cache);
  return ids.includes(user.id);
}

/** Who may read an approval: HR, the subject, the subject's line manager, a current approver, or anyone who already acted on it. */
export async function canView(db: Queryable, user: AuthUser, inst: WorkflowInstance): Promise<boolean> {
  if (user.companyId !== inst.company_id) return false;
  if (hasRole(user, 'hr')) return true;
  if (inst.employee_id && user.employeeId === inst.employee_id) return true;
  if (inst.employee_id && user.employeeId && hasRole(user, 'manager') && (await isManagerOf(db, user.employeeId, inst.employee_id))) return true;
  if (await canAct(db, user, inst)) return true;
  const acted = await db.query('SELECT 1 FROM approval_action WHERE instance_id = $1 AND (actor_id = $2 OR on_behalf_of = $2) LIMIT 1', [inst.id, user.id]);
  return acted.rows.length > 0;
}

export async function startWorkflow(db: Queryable, input: StartWorkflowInput): Promise<WorkflowInstance> {
  const def = await db.query<{ steps: WorkflowStep[] }>(
    'SELECT steps FROM workflow_definition WHERE company_id = $1 AND object_type = $2', [input.companyId, input.objectType]);
  const steps = def.rows[0]?.steps?.length ? def.rows[0].steps : [{ role: 'manager' }];
  const r = await db.query<WorkflowInstance>(
    `INSERT INTO workflow_instance (company_id, object_type, object_id, employee_id, steps) VALUES ($1, $2, $3, $4, $5) RETURNING *`,
    [input.companyId, input.objectType, input.objectId, input.employeeId, JSON.stringify(steps)]);
  const inst = r.rows[0]!;
  await notifyStepApprovers(db, inst, input.title, input.body);
  return inst;
}

async function notifyStepApprovers(db: Queryable, inst: WorkflowInstance, title: string, body: string): Promise<void> {
  const step = inst.steps[inst.current_step];
  if (!step) return;
  const ids = await approversFor(db, inst.company_id, inst.employee_id, step.role);
  for (const userId of ids) {
    await notify(db, {
      companyId: inst.company_id, userId, template: `${inst.object_type}.pending`, title, body,
      payload: { instanceId: inst.id, objectType: inst.object_type, objectId: inst.object_id, step: inst.current_step },
      channels: ['push'],
    });
  }
}

export interface ActInput {
  instanceId: string;
  user: AuthUser;
  decision: 'approve' | 'reject';
  reason?: string | null;
  onBehalfOf?: string | null;
  /** Extra data for the handler, e.g. {approvedMin} for OT pre-approvals. */
  payload?: Record<string, unknown>;
}

/**
 * Record a signed decision. Reject ends the workflow; approve advances a step and the last step applies the
 * object's handler. Must be called inside a transaction; the row is locked FOR UPDATE.
 */
export async function act(db: Queryable, input: ActInput): Promise<WorkflowState> {
  const r = await db.query<WorkflowInstance>('SELECT * FROM workflow_instance WHERE id = $1 FOR UPDATE', [input.instanceId]);
  const inst = r.rows[0];
  if (!inst) throw new NotFoundError('approval not found');
  if (inst.state !== 'pending') throw new ConflictError(`request already ${inst.state}`);
  if (!(await canAct(db, input.user, inst))) throw new ForbiddenError('you are not an approver for this step');
  const reason = input.reason?.trim() || null;
  if (input.decision === 'reject' && !reason) throw new BadRequestError('a reason is required to reject');
  if (input.onBehalfOf) {
    // Delegation is recorded, never assumed: only HR/admin may act on behalf of someone, and only for a real approver of this step.
    if (!hasRole(input.user, 'hr')) throw new ForbiddenError('only HR may act on behalf of another approver');
    const step = inst.steps[inst.current_step];
    const ids = step ? await approversFor(db, inst.company_id, inst.employee_id, step.role) : [];
    if (!ids.includes(input.onBehalfOf) || input.onBehalfOf === input.user.id) throw new BadRequestError('onBehalfOf must be an approver for this step');
  }

  const actedAt = new Date();
  const signature = createHash('sha256')
    .update(`${inst.id}|${inst.current_step}|${input.user.id}|${input.decision}|${actedAt.toISOString()}`).digest('hex');
  await db.query(
    `INSERT INTO approval_action (instance_id, step, actor_id, decision, reason, on_behalf_of, signature, acted_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [inst.id, inst.current_step, input.user.id, input.decision, reason, input.onBehalfOf ?? null, signature, actedAt]);

  let state: WorkflowState;
  if (input.decision === 'reject') {
    state = 'rejected';
    await db.query(`UPDATE workflow_instance SET state = 'rejected', completed_at = now() WHERE id = $1`, [inst.id]);
  } else if (inst.current_step + 1 >= inst.steps.length) {
    state = 'approved';
    await db.query(`UPDATE workflow_instance SET state = 'approved', current_step = $2, completed_at = now() WHERE id = $1`, [inst.id, inst.current_step + 1]);
  } else {
    state = 'pending';
    await db.query('UPDATE workflow_instance SET current_step = $2 WHERE id = $1', [inst.id, inst.current_step + 1]);
    await notifyStepApprovers(db, { ...inst, current_step: inst.current_step + 1 }, 'Approval needed', `${inst.object_type.replace('_', ' ')} awaiting your decision (step ${inst.current_step + 2})`);
  }
  await audit(db, { actorId: input.user.id, action: input.decision, table: inst.object_type, rowId: inst.object_id, after: { instanceId: inst.id, step: inst.current_step, reason, signature } });

  if (state !== 'pending') {
    const handler = handlers.get(inst.object_type);
    if (handler) await handler(db, inst.object_id, state, { actorId: input.user.id, reason, payload: input.payload ?? {} });
  }
  return state;
}

export async function cancelWorkflow(db: Queryable, objectType: string, objectId: string): Promise<void> {
  await db.query(`UPDATE workflow_instance SET state = 'cancelled', completed_at = now() WHERE object_type = $1 AND object_id = $2 AND state = 'pending'`, [objectType, objectId]);
}

/** Pending instances older than N hours with their current approvers, for SLA reminders and escalation (plan §13.5). */
export async function pendingOlderThan(db: Queryable, hours: number): Promise<Array<{ instance: WorkflowInstance; approvers: string[] }>> {
  const r = await db.query<WorkflowInstance>(
    `SELECT * FROM workflow_instance WHERE state = 'pending' AND created_at < now() - make_interval(hours => $1) ORDER BY created_at`, [hours]);
  const out: Array<{ instance: WorkflowInstance; approvers: string[] }> = [];
  for (const inst of r.rows) {
    const step = inst.steps[inst.current_step];
    out.push({ instance: inst, approvers: step ? await approversFor(db, inst.company_id, inst.employee_id, step.role) : [] });
  }
  return out;
}

export interface InboxItem {
  instanceId: string;
  objectType: string;
  objectId: string;
  step: number;
  totalSteps: number;
  createdAt: Date;
  employee: { id: string; empCode: string; nameEn: string; nameNe: string | null } | null;
  summary: Record<string, unknown>;
}

/** Everything the user can decide right now, with enough context for a one-tap decision. */
export async function inboxFor(db: Queryable, user: AuthUser): Promise<InboxItem[]> {
  const r = await db.query<WorkflowInstance & { emp_code: string | null; name_en: string | null; name_ne: string | null }>(
    `SELECT w.*, e.emp_code, e.name_en, e.name_ne FROM workflow_instance w LEFT JOIN employee e ON e.id = w.employee_id
      WHERE w.company_id = $1 AND w.state = 'pending' ORDER BY w.created_at`, [user.companyId]);
  const items: InboxItem[] = [];
  const cache = new Map<string, string[]>();
  for (const row of r.rows) {
    if (!(await canAct(db, user, row, cache))) continue;
    items.push({
      instanceId: row.id, objectType: row.object_type, objectId: row.object_id, step: row.current_step, totalSteps: row.steps.length,
      createdAt: row.created_at,
      employee: row.employee_id ? { id: row.employee_id, empCode: row.emp_code ?? '', nameEn: row.name_en ?? '', nameNe: row.name_ne } : null,
      summary: await summarise(db, row),
    });
  }
  return items;
}

/** Object-specific context for the inbox card (plan §13.5 manager inbox card). */
export async function summarise(db: Queryable, inst: WorkflowInstance): Promise<Record<string, unknown>> {
  switch (inst.object_type) {
    case 'leave_request': {
      const r = await db.query<{ from_date: string; to_date: string; half_day: string | null; days_requested: string; reason: string | null; type_code: string; type_name: string; leave_type_id: string; employee_id: string }>(
        `SELECT lr.from_date, lr.to_date, lr.half_day, lr.days_requested, lr.reason, lt.code AS type_code, lt.name_en AS type_name, lr.leave_type_id, lr.employee_id
           FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id WHERE lr.id = $1`, [inst.object_id]);
      const lr = r.rows[0];
      if (!lr) return {};
      const bal = await db.query<{ balance: string }>(
        'SELECT coalesce(sum(delta_days), 0)::text AS balance FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2', [lr.employee_id, lr.leave_type_id]);
      const overlap = await db.query<{ name_en: string }>(
        `SELECT e.name_en FROM leave_request o JOIN employee e ON e.id = o.employee_id
          WHERE o.id <> $1 AND o.status IN ('pending', 'approved') AND o.employee_id <> $2 AND o.from_date <= $4::date AND o.to_date >= $3::date
            AND e.company_id = $5 LIMIT 5`, [inst.object_id, lr.employee_id, lr.from_date, lr.to_date, inst.company_id]);
      return {
        leaveType: lr.type_code, leaveTypeName: lr.type_name, from: lr.from_date, to: lr.to_date, halfDay: lr.half_day, days: Number(lr.days_requested),
        reason: lr.reason, balanceNow: Number(bal.rows[0]?.balance ?? 0), balanceAfter: Number(bal.rows[0]?.balance ?? 0) - Number(lr.days_requested),
        teamOverlap: overlap.rows.map((x) => x.name_en),
      };
    }
    case 'regularisation_request': {
      const r = await db.query('SELECT local_date, kind, punch_time, punch_direction, shift_id, reason FROM regularisation_request WHERE id = $1', [inst.object_id]);
      return r.rows[0] ?? {};
    }
    case 'ot_request': {
      const r = await db.query('SELECT local_date, requested_min, reason FROM ot_request WHERE id = $1', [inst.object_id]);
      return r.rows[0] ?? {};
    }
    case 'payroll_run': {
      const r = await db.query(
        `SELECT r.run_type, r.status, p.bs_year, p.bs_month, (SELECT count(*) FROM payslip s WHERE s.run_id = r.id) AS payslips,
                (SELECT coalesce(sum(net), 0)::text FROM payslip s WHERE s.run_id = r.id) AS total_net
           FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.id = $1`, [inst.object_id]).catch(() => ({ rows: [] as Record<string, unknown>[] }));
      return r.rows[0] ?? {};
    }
    case 'training_nomination': {
      const r = await db.query(
        `SELECT c.title AS course_title, c.delivery, c.cost, e.reason, s.starts_at FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id
           LEFT JOIN lrn_session s ON s.id = e.session_id WHERE e.id = $1`, [inst.object_id]);
      return r.rows[0] ?? {};
    }
    default:
      return {};
  }
}

export async function timeline(db: Queryable, inst: WorkflowInstance): Promise<Array<Record<string, unknown>>> {
  const r = await db.query(
    `SELECT a.step, a.decision, a.reason, a.acted_at, a.signature, a.on_behalf_of, u.name AS actor_name, u.id AS actor_id
       FROM approval_action a JOIN app_user u ON u.id = a.actor_id WHERE a.instance_id = $1 ORDER BY a.acted_at`, [inst.id]);
  return r.rows;
}
