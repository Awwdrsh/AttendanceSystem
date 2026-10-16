import { z } from 'zod';
import type { Queryable } from '../../db/pool.js';
import { userIdsWithRole } from '../notifications/service.js';

/**
 * Approval flows. A step names who approves: the line manager, the manager's manager, the department or branch head,
 * everyone holding a role, one named user, or any member of a user group. A company has a default chain per request
 * type (workflow_definition) and may add conditional flows (approval_flow) for initiators in a department, branch,
 * designation or user group; the most specific match wins, and with no default the reporting line approves.
 *
 * Resolution happens when someone looks (inbox, notification), so a manager change or a head appointment is picked up
 * without editing any flow. Approvers who are away (on full-day leave today, exited, disabled) hand over to an active
 * delegate; when nobody is left the step falls back to HR, and the instance records why.
 */
export const APPROVER_KINDS = ['manager', 'manager2', 'department_head', 'branch_head', 'role', 'user', 'group'] as const;
export type ApproverKind = typeof APPROVER_KINDS[number];
export const STEP_ROLES = ['hr', 'admin', 'payroll_admin', 'attendance_admin', 'recruiter'] as const;

export interface FlowStep {
  /** Kept for older instances: 'manager' or a role name. New steps also carry kind. */
  role: string;
  kind?: ApproverKind;
  userId?: string;
  groupId?: string;
  /** Money requests: the step only applies when the amount is at least this much (NPR). */
  minAmount?: number;
  label?: string;
}

export const stepSchema = z.object({
  kind: z.enum(APPROVER_KINDS),
  role: z.enum(STEP_ROLES).optional(),
  userId: z.uuid().optional(),
  groupId: z.uuid().optional(),
  minAmount: z.number().min(0).max(1e10).optional(),
  label: z.string().trim().max(60).optional(),
}).superRefine((s, ctx) => {
  if (s.kind === 'role' && !s.role) ctx.addIssue({ code: 'custom', message: 'choose the role that approves', path: ['role'] });
  if (s.kind === 'user' && !s.userId) ctx.addIssue({ code: 'custom', message: 'choose the person who approves', path: ['userId'] });
  if (s.kind === 'group' && !s.groupId) ctx.addIssue({ code: 'custom', message: 'choose the group that approves', path: ['groupId'] });
});
export const stepsSchema = z.array(stepSchema).min(1, 'a flow needs at least one step').max(6, 'at most 6 steps');

export const conditionsSchema = z.object({
  employeeIds: z.array(z.uuid()).max(500).default([]),
  groupIds: z.array(z.uuid()).max(50).default([]),
  designationIds: z.array(z.uuid()).max(100).default([]),
  orgUnitIds: z.array(z.uuid()).max(100).default([]),
  branchIds: z.array(z.uuid()).max(100).default([]),
});
export type FlowConditions = z.infer<typeof conditionsSchema>;

/** Stored shape: `role` mirrors the kind (or the role for role steps) so older code that reads step.role still works. */
export function normaliseStep(s: z.infer<typeof stepSchema>): FlowStep {
  const out: FlowStep = { kind: s.kind, role: s.kind === 'role' ? s.role! : s.kind };
  if (s.kind === 'user') out.userId = s.userId!;
  if (s.kind === 'group') out.groupId = s.groupId!;
  if (s.minAmount !== undefined) out.minAmount = s.minAmount;
  if (s.label) out.label = s.label;
  return out;
}

export const kindOf = (s: FlowStep): ApproverKind => s.kind ?? (s.role === 'manager' ? 'manager' : 'role');

/** Request types that go through approvals, with the label HR sees. Money types can have amount thresholds. */
export const REQUEST_TYPES: Array<{ type: string; label: string; money?: boolean; subject: 'employee' | 'requester' }> = [
  { type: 'leave_request', label: 'Leave', subject: 'employee' },
  { type: 'regularisation_request', label: 'Attendance correction', subject: 'employee' },
  { type: 'ot_request', label: 'Overtime', subject: 'employee' },
  { type: 'timesheet', label: 'Timesheet', subject: 'employee' },
  { type: 'shift_swap', label: 'Shift swap', subject: 'employee' },
  { type: 'expense_claim', label: 'Expense claim', money: true, subject: 'employee' },
  { type: 'travel_request', label: 'Travel request', money: true, subject: 'employee' },
  { type: 'travel_settlement', label: 'Travel settlement', money: true, subject: 'employee' },
  { type: 'letter_request', label: 'Letter request', subject: 'employee' },
  { type: 'training_nomination', label: 'Training request', subject: 'employee' },
  { type: 'tax_declaration', label: 'Tax savings declaration', subject: 'employee' },
  { type: 'profile_change', label: 'Profile change', subject: 'employee' },
  { type: 'resignation', label: 'Resignation', subject: 'employee' },
  { type: 'probation_review', label: 'Probation review', subject: 'employee' },
  { type: 'job_requisition', label: 'Hiring request', subject: 'requester' },
  { type: 'job_offer', label: 'Job offer', subject: 'requester' },
];
export const REQUEST_TYPE_SET = new Set(REQUEST_TYPES.map((t) => t.type));

export const DEFAULT_STEPS: FlowStep[] = [{ kind: 'manager', role: 'manager' }];

// ---------------------------------------------------------------- choosing the flow

interface Subject { userId: string | null; designationId: string | null; orgUnitIds: string[]; branchId: string | null; groupIds: string[] }

async function subjectOf(db: Queryable, employeeId: string): Promise<Subject> {
  const r = await db.query<{ user_id: string | null; designation_id: string | null; org_unit_id: string | null; branch_id: string | null }>(
    `SELECT (SELECT u.id FROM app_user u WHERE u.employee_id = e.id AND u.status = 'active' LIMIT 1) AS user_id, e.designation_id, a.org_unit_id, a.branch_id
       FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date WHERE e.id = $1`, [employeeId]);
  const row = r.rows[0];
  if (!row) return { userId: null, designationId: null, orgUnitIds: [], branchId: null, groupIds: [] };
  const units = row.org_unit_id ? (await db.query<{ id: string }>(
    `WITH RECURSIVE up(id, parent_id, depth) AS (SELECT id, parent_id, 1 FROM org_unit WHERE id = $1
       UNION ALL SELECT o.id, o.parent_id, up.depth + 1 FROM org_unit o JOIN up ON o.id = up.parent_id WHERE up.depth < 20) SELECT id FROM up`, [row.org_unit_id])).rows.map((x) => x.id) : [];
  const groups = row.user_id ? (await db.query<{ group_id: string }>('SELECT group_id FROM user_group_member WHERE user_id = $1', [row.user_id])).rows.map((x) => x.group_id) : [];
  return { userId: row.user_id, designationId: row.designation_id, orgUnitIds: units, branchId: row.branch_id, groupIds: groups };
}

/** How specifically a flow's conditions match the subject: -1 = no match, 0 = matches everyone, higher = narrower. */
export function matchScore(c: Partial<FlowConditions>, subjectEmployeeId: string | null, s: Subject): number {
  const lists: Array<[string[] | undefined, number, (ids: string[]) => boolean]> = [
    [c.employeeIds, 16, (ids) => !!subjectEmployeeId && ids.includes(subjectEmployeeId)],
    [c.groupIds, 8, (ids) => s.groupIds.some((g) => ids.includes(g))],
    [c.designationIds, 4, (ids) => !!s.designationId && ids.includes(s.designationId)],
    [c.orgUnitIds, 2, (ids) => s.orgUnitIds.some((u) => ids.includes(u))],
    [c.branchIds, 1, (ids) => !!s.branchId && ids.includes(s.branchId)],
  ];
  let score = 0;
  for (const [ids, weight, test] of lists) {
    if (!ids || ids.length === 0) continue;
    if (!test(ids)) return -1;
    score += weight;
  }
  return score;
}

export interface ChosenFlow { steps: FlowStep[]; flowId: string | null; flowName: string | null }

/** The flow for this request: most specific matching conditional flow, else the company default, else the reporting line. */
export async function chooseFlow(db: Queryable, companyId: string, objectType: string, employeeId: string | null): Promise<ChosenFlow> {
  const flows = await db.query<{ id: string; name: string; conditions: Partial<FlowConditions>; steps: FlowStep[]; priority: number }>(
    `SELECT id, name, conditions, steps, priority FROM approval_flow WHERE company_id = $1 AND object_type = $2 AND active ORDER BY priority DESC, created_at`,
    [companyId, objectType]);
  if (flows.rows.length) {
    const subject = employeeId ? await subjectOf(db, employeeId) : { userId: null, designationId: null, orgUnitIds: [], branchId: null, groupIds: [] };
    let best: { row: typeof flows.rows[number]; score: number } | null = null;
    for (const row of flows.rows) {
      const score = matchScore(row.conditions ?? {}, employeeId, subject);
      if (score < 0) continue;
      if (!best || score > best.score || (score === best.score && row.priority > best.row.priority)) best = { row, score };
    }
    if (best && best.row.steps?.length) return { steps: best.row.steps, flowId: best.row.id, flowName: best.row.name };
  }
  const def = await db.query<{ steps: FlowStep[] }>('SELECT steps FROM workflow_definition WHERE company_id = $1 AND object_type = $2', [companyId, objectType]);
  return { steps: def.rows[0]?.steps?.length ? def.rows[0].steps : DEFAULT_STEPS, flowId: null, flowName: null };
}

/** Steps with an amount threshold apply only when the request reaches it; a request without an amount skips them. */
export function stepsForAmount(steps: FlowStep[], amount: number | null | undefined): FlowStep[] {
  const kept = steps.filter((s) => s.minAmount === undefined || (amount != null && amount >= s.minAmount));
  return kept.length ? kept : steps.slice(0, 1);
}

// ---------------------------------------------------------------- resolving a step to people

export interface Resolution { users: string[]; notes: string[] }

async function usersOfEmployees(db: Queryable, employeeIds: string[]): Promise<string[]> {
  if (!employeeIds.length) return [];
  const r = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE employee_id = ANY($1::uuid[]) AND status = 'active'`, [employeeIds]);
  return r.rows.map((x) => x.id);
}

async function lineManager(db: Queryable, employeeId: string, onDate: string): Promise<string | null> {
  const r = await db.query<{ manager_id: string | null }>('SELECT manager_id FROM employee_assignment WHERE employee_id = $1 AND valid @> $2::date', [employeeId, onDate]);
  return r.rows[0]?.manager_id ?? null;
}

/** Nearest head above the employee who is not the employee: their department, then each parent department. */
async function departmentHead(db: Queryable, employeeId: string, onDate: string): Promise<string | null> {
  const r = await db.query<{ head: string | null }>(
    `WITH RECURSIVE up(id, parent_id, head, depth) AS (
       SELECT o.id, o.parent_id, o.head_employee_id, 1 FROM employee_assignment a JOIN org_unit o ON o.id = a.org_unit_id
        WHERE a.employee_id = $1 AND a.valid @> $2::date
       UNION ALL SELECT p.id, p.parent_id, p.head_employee_id, up.depth + 1 FROM org_unit p JOIN up ON p.id = up.parent_id WHERE up.depth < 20)
     SELECT head FROM up WHERE head IS NOT NULL AND head <> $1 ORDER BY depth LIMIT 1`, [employeeId, onDate]);
  return r.rows[0]?.head ?? null;
}

async function branchHead(db: Queryable, employeeId: string, onDate: string): Promise<string | null> {
  const r = await db.query<{ head: string | null }>(
    `SELECT b.head_employee_id AS head FROM employee_assignment a JOIN branch b ON b.id = a.branch_id
      WHERE a.employee_id = $1 AND a.valid @> $2::date AND b.head_employee_id IS NOT NULL AND b.head_employee_id <> $1`, [employeeId, onDate]);
  return r.rows[0]?.head ?? null;
}

const KIND_LABEL: Record<ApproverKind, string> = {
  manager: 'Line manager', manager2: "Manager's manager", department_head: 'Department head', branch_head: 'Branch head',
  role: 'Role', user: 'Named person', group: 'Group',
};
const ROLE_LABEL: Record<string, string> = { hr: 'HR', admin: 'Admin', payroll_admin: 'Payroll admin', attendance_admin: 'Attendance admin', recruiter: 'Recruiter' };

export function stepLabel(s: FlowStep): string {
  if (s.label) return s.label;
  const k = kindOf(s);
  return k === 'role' ? ROLE_LABEL[s.role] ?? s.role : KIND_LABEL[k];
}

/** The people named by the step before anyone is checked for absence. */
async function namedApprovers(db: Queryable, companyId: string, employeeId: string | null, step: FlowStep, onDate: string): Promise<string[]> {
  const kind = kindOf(step);
  switch (kind) {
    case 'manager': {
      if (!employeeId) return [];
      const m = await lineManager(db, employeeId, onDate);
      return m ? usersOfEmployees(db, [m]) : [];
    }
    case 'manager2': {
      if (!employeeId) return [];
      const m = await lineManager(db, employeeId, onDate);
      const m2 = m ? await lineManager(db, m, onDate) : null;
      return m2 && m2 !== employeeId ? usersOfEmployees(db, [m2]) : [];
    }
    case 'department_head': {
      if (!employeeId) return [];
      const h = await departmentHead(db, employeeId, onDate);
      return h ? usersOfEmployees(db, [h]) : [];
    }
    case 'branch_head': {
      if (!employeeId) return [];
      const h = await branchHead(db, employeeId, onDate);
      return h ? usersOfEmployees(db, [h]) : [];
    }
    case 'role': return userIdsWithRole(db, companyId, step.role);
    case 'user': {
      if (!step.userId) return [];
      const r = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE id = $1 AND company_id = $2 AND status = 'active'`, [step.userId, companyId]);
      return r.rows.map((x) => x.id);
    }
    case 'group': {
      if (!step.groupId) return [];
      const r = await db.query<{ id: string }>(
        `SELECT u.id FROM user_group_member m JOIN user_group g ON g.id = m.group_id JOIN app_user u ON u.id = m.user_id
          WHERE m.group_id = $1 AND g.company_id = $2 AND u.status = 'active' ORDER BY u.name`, [step.groupId, companyId]);
      return r.rows.map((x) => x.id);
    }
  }
}

interface Presence { id: string; name: string; away: string | null; delegate: string | null }

/** Who of these users is away today (full-day leave or exited) and whom they delegated to. */
async function presenceOf(db: Queryable, userIds: string[], onDate: string): Promise<Map<string, Presence>> {
  const out = new Map<string, Presence>();
  if (!userIds.length) return out;
  const r = await db.query<{ id: string; name: string; away: string | null; delegate: string | null }>(
    `SELECT u.id, u.name,
            CASE WHEN e.id IS NOT NULL AND (e.status = 'exited' OR (e.exit_date IS NOT NULL AND e.exit_date < $2::date)) THEN 'has left'
                 WHEN EXISTS (SELECT 1 FROM leave_request l WHERE l.employee_id = e.id AND l.status = 'approved' AND l.from_time IS NULL
                                AND $2::date BETWEEN l.from_date AND l.to_date AND l.half_day IS NULL) THEN 'is on leave' END AS away,
            (SELECT d.delegate_id FROM approver_delegate d JOIN app_user du ON du.id = d.delegate_id AND du.status = 'active'
              WHERE d.user_id = u.id AND d.cancelled_at IS NULL AND $2::date BETWEEN d.from_date AND d.to_date ORDER BY d.created_at DESC LIMIT 1) AS delegate
       FROM app_user u LEFT JOIN employee e ON e.id = u.employee_id WHERE u.id = ANY($1::uuid[])`, [userIds, onDate]);
  for (const row of r.rows) out.set(row.id, row);
  return out;
}

/**
 * Everyone who may decide this step now: the named approvers, minus the requester, with away approvers replaced by
 * their delegate (a delegate also covers while the approver is present). Nobody left → HR (and admins).
 */
export async function resolveStep(db: Queryable, companyId: string, employeeId: string | null, step: FlowStep, onDate: string): Promise<Resolution> {
  const notes: string[] = [];
  const subjectUser = employeeId
    ? (await db.query<{ id: string }>(`SELECT id FROM app_user WHERE employee_id = $1`, [employeeId])).rows.map((x) => x.id) : [];
  const named = (await namedApprovers(db, companyId, employeeId, step, onDate)).filter((u) => !subjectUser.includes(u));
  const label = stepLabel(step);
  if (!named.length && kindOf(step) !== 'role') notes.push(`No ${label.toLowerCase()} is set`);
  const presence = await presenceOf(db, named, onDate);
  const users = new Set<string>();
  for (const id of named) {
    const p = presence.get(id);
    if (p?.delegate && !subjectUser.includes(p.delegate)) {
      users.add(p.delegate);
      if (p.away) notes.push(`${p.name} ${p.away}; delegated`);
    }
    if (p?.away) { if (!p.delegate) notes.push(`${p.name} ${p.away}`); continue; }
    users.add(id);
  }
  if (users.size === 0) {
    const hr = (await userIdsWithRole(db, companyId, 'hr')).filter((u) => !subjectUser.includes(u));
    if (named.length || kindOf(step) === 'role') notes.push('Nobody available; HR decides');
    else notes.push('HR decides instead');
    hr.forEach((u) => users.add(u));
    return { users: [...users], notes: dedupe(notes).concat(['fallback:hr']) };
  }
  return { users: [...users], notes: dedupe(notes) };
}

const dedupe = (xs: string[]) => [...new Set(xs)];
export const fellBackToHr = (r: Resolution) => r.notes.includes('fallback:hr');
export const visibleNotes = (r: Resolution) => r.notes.filter((n) => n !== 'fallback:hr');
