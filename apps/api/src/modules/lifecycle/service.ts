import { logCareerEvent } from '../people/career.js';
import { z } from 'zod';
import { bsLabel } from '../../bsdate.js';
import type { Db, Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { hasRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { featureOn } from '../features/service.js';
import { notify, notifyRole, userIdForEmployee, userIdsWithRole } from '../notifications/service.js';
import { nptToday, type DecisionContext } from '../workflow/service.js';

// ---------------------------------------------------------------- policy
export const lifecyclePolicySchema = z.object({
  /** Notice an employee gives when resigning, in calendar days. */
  noticeDays: z.number().int().min(0).max(180).default(30),
  /** Start the default onboarding checklist when HR adds an employee. */
  autoStartOnboarding: z.boolean().default(true),
  /** Days before probation ends on which the manager is reminded. */
  probationReminderDays: z.array(z.number().int().min(1).max(90)).max(4).default([14, 7]),
  /** Let leavers fill in the exit interview themselves. */
  selfExitInterview: z.boolean().default(true),
});
export type LifecyclePolicy = z.infer<typeof lifecyclePolicySchema>;

export async function lifecyclePolicy(db: Queryable, companyId: string): Promise<LifecyclePolicy> {
  const r = await db.query<{ value: unknown }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'lifecycle.policy'`, [companyId]);
  const parsed = lifecyclePolicySchema.safeParse(r.rows[0]?.value ?? {});
  return parsed.success ? parsed.data : lifecyclePolicySchema.parse({});
}

// ---------------------------------------------------------------- defaults
type TaskSeed = [title: string, assignee: 'hr' | 'manager' | 'employee', offset: number, department: string | null, link: string | null, description?: string];

const ONBOARDING: TaskSeed[] = [
  ['Prepare the appointment letter and contract', 'hr', -3, 'HR', null],
  ['Set up the workstation, laptop and email', 'manager', -1, 'IT', null, 'Ask IT for a laptop, an email address and access to the tools the role needs.'],
  ['Create the Hajiri sign-in and assign a shift', 'hr', 0, 'HR', '/settings/users'],
  ['Enrol fingerprint or face on the attendance terminal', 'hr', 0, 'HR', null],
  ['Complete your profile and bank details', 'employee', 0, null, '/me/profile', 'Check your contact details, emergency contact and bank account.'],
  ['Upload your citizenship, PAN and academic certificates', 'employee', 1, null, '/me/profile/documents'],
  ['Assign the salary structure and SSF registration', 'hr', 2, 'HR', '/payroll/assignments'],
  ['Welcome meeting: introduce the team and the role', 'manager', 1, null, null],
  ['Read the employee handbook and leave policy', 'employee', 7, null, null],
  ['Agree goals for the probation period', 'manager', 14, null, '/me/growth'],
  ['First-month check-in', 'manager', 30, null, null],
];

const OFFBOARDING: TaskSeed[] = [
  ['Hand over work, files and passwords', 'manager', -3, 'Manager', null],
  ['Exit interview', 'hr', -2, 'HR', null],
  ['Return laptop and devices; remove email and system access', 'hr', 0, 'IT', null],
  ['Clear advances, loans and other dues', 'hr', 0, 'Finance', '/payroll/loans'],
  ['Return ID card, keys and SIM', 'hr', 0, 'Admin', null],
  ['Run the final settlement in payroll', 'hr', 3, 'HR', '/payroll'],
  ['Issue the experience and relieving letter', 'hr', 5, 'HR', null],
];

const EXIT_QUESTIONS: Array<[string, 'text' | 'rating' | 'choice', string[]]> = [
  ['What is the main reason you are leaving?', 'choice', ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation', 'Further studies', 'Health or family', 'Other']],
  ['How would you rate your overall experience here?', 'rating', []],
  ['How would you rate the support from your manager?', 'rating', []],
  ['What did you like most about working here?', 'text', []],
  ['What should we improve?', 'text', []],
  ['Would you recommend us as a place to work?', 'choice', ['Yes', 'Maybe', 'No']],
];

const ASSET_TYPES = ['Laptop', 'Desktop', 'Mobile phone', 'SIM card', 'ID card', 'Keys', 'Vehicle', 'Other'];

/** Idempotent per-company defaults; the marker row makes concurrent first visits seed only once. */
export async function ensureLifecycleDefaults(db: Db | Queryable, companyId: string): Promise<boolean> {
  const marker = await db.query(
    `INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'lifecycle.seeded', '{"version":1}') ON CONFLICT DO NOTHING`, [companyId]);
  if ((marker.rowCount ?? 0) === 0) return false;
  for (const [kind, name, tasks] of [['onboarding', 'Standard onboarding', ONBOARDING], ['offboarding', 'Standard exit clearance', OFFBOARDING]] as const) {
    const t = await db.query<{ id: string }>(
      `INSERT INTO lifecycle_template (company_id, kind, name, description, is_default) VALUES ($1, $2, $3, $4, true)
       ON CONFLICT (company_id, kind, name) DO NOTHING RETURNING id`,
      [companyId, kind, name, kind === 'onboarding' ? 'Checklist for every new joiner. Due dates count from the join date.' : 'Clearance for every leaver. Due dates count from the last working day.']);
    const id = t.rows[0]?.id;
    if (!id) continue;
    for (const [i, [title, assignee, offset, dept, link, description]] of tasks.entries()) {
      await db.query(
        `INSERT INTO lifecycle_template_task (template_id, sort, title, description, department, assignee_type, due_offset_days, link) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
        [id, i, title, description ?? null, dept, assignee, offset, link]);
    }
  }
  for (const [i, [q, kind, options]] of EXIT_QUESTIONS.entries()) {
    await db.query('INSERT INTO exit_interview_question (company_id, sort, question, kind, options) VALUES ($1, $2, $3, $4, $5)', [companyId, i, q, kind, JSON.stringify(options)]);
  }
  for (const name of ASSET_TYPES) await db.query('INSERT INTO asset_type (company_id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING', [companyId, name]);
  return true;
}

// ---------------------------------------------------------------- reminders
/** Record a reminder; false when it was already sent (callers skip the notification). */
export async function remindOnce(db: Queryable, companyId: string, kind: string, refId: string, key: string): Promise<boolean> {
  const r = await db.query('INSERT INTO lifecycle_reminder (company_id, kind, ref_id, key) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING', [companyId, kind, refId, key]);
  return (r.rowCount ?? 0) > 0;
}

// ---------------------------------------------------------------- checklists
export interface TaskRow {
  id: string; case_id: string; company_id: string; employee_id: string; sort: number; title: string; description: string | null; department: string | null;
  assignee_type: 'hr' | 'manager' | 'employee' | 'user'; assignee_user_id: string | null; due_date: string | null; link: string | null;
  status: 'open' | 'done' | 'skipped'; note: string | null; done_by: string | null; done_at: Date | null;
}

export async function managerUserOf(db: Queryable, employeeId: string): Promise<string | null> {
  const r = await db.query<{ id: string }>(
    `SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id AND u.status = 'active'
      WHERE a.employee_id = $1 AND (a.valid @> current_date OR lower(a.valid) > current_date) ORDER BY lower(a.valid) LIMIT 1`, [employeeId]);
  return r.rows[0]?.id ?? null;
}

/** Users who should act on a task: HR, the line manager (HR when there is none), the employee, or the named user. */
export async function responsibleUsers(db: Queryable, t: Pick<TaskRow, 'company_id' | 'employee_id' | 'assignee_type' | 'assignee_user_id'>): Promise<string[]> {
  switch (t.assignee_type) {
    case 'hr': return userIdsWithRole(db, t.company_id, 'hr');
    case 'manager': { const m = await managerUserOf(db, t.employee_id); return m ? [m] : userIdsWithRole(db, t.company_id, 'hr'); }
    case 'employee': { const u = await userIdForEmployee(db, t.employee_id); return u ? [u] : []; }
    case 'user': return t.assignee_user_id ? [t.assignee_user_id] : userIdsWithRole(db, t.company_id, 'hr');
  }
}

/** The employee's current line manager, or the manager they will have once they join (pre-joining onboarding). */
async function managesNowOrSoon(db: Queryable, managerEmployeeId: string, employeeId: string): Promise<boolean> {
  const r = await db.query(
    'SELECT 1 FROM employee_assignment WHERE employee_id = $1 AND manager_id = $2 AND (valid @> current_date OR lower(valid) > current_date) LIMIT 1', [employeeId, managerEmployeeId]);
  return r.rows.length > 0;
}

/** HR may complete anything; otherwise only the person the task is for. */
export async function canCompleteTask(db: Queryable, user: AuthUser, t: TaskRow): Promise<boolean> {
  if (user.companyId !== t.company_id) return false;
  if (hasRole(user, 'hr')) return true;
  switch (t.assignee_type) {
    case 'hr': return false;
    case 'employee': return user.employeeId === t.employee_id;
    case 'manager': return !!user.employeeId && (await managesNowOrSoon(db, user.employeeId, t.employee_id));
    case 'user': return t.assignee_user_id === user.id;
  }
}

/** HR, the employee, their line manager, and anyone with a task in the case may see it. */
export async function canViewCase(db: Queryable, user: AuthUser, c: { id: string; company_id: string; employee_id: string }): Promise<boolean> {
  if (user.companyId !== c.company_id) return false;
  if (hasRole(user, 'hr')) return true;
  if (user.employeeId === c.employee_id) return true;
  if (user.employeeId && hasRole(user, 'manager') && (await managesNowOrSoon(db, user.employeeId, c.employee_id))) return true;
  const r = await db.query('SELECT 1 FROM lifecycle_task WHERE case_id = $1 AND assignee_type = $2 AND assignee_user_id = $3 LIMIT 1', [c.id, 'user', user.id]);
  return r.rows.length > 0;
}

/**
 * Open a checklist for an employee from a template (the best match for their branch and department when none is
 * given). Due dates count from the anchor date: the join date for onboarding, the last working day for exits.
 */
export async function startCase(db: Queryable, input: {
  companyId: string; employeeId: string; kind: 'onboarding' | 'offboarding'; templateId?: string | null; anchorDate: string; startedBy: string | null; exitId?: string | null;
}): Promise<string> {
  await ensureLifecycleDefaults(db, input.companyId);
  let templateId = input.templateId ?? null;
  if (templateId) {
    const t = await db.query('SELECT 1 FROM lifecycle_template WHERE id = $1 AND company_id = $2 AND kind = $3 AND active', [templateId, input.companyId, input.kind]);
    if (!t.rows.length) throw new BadRequestError('checklist template not found');
  } else {
    const t = await db.query<{ id: string }>(
      `SELECT t.id FROM lifecycle_template t
         LEFT JOIN employee_assignment a ON a.employee_id = $3 AND a.valid @> greatest(current_date, $4::date)
        WHERE t.company_id = $1 AND t.kind = $2 AND t.active
          AND (t.branch_id IS NULL OR t.branch_id = a.branch_id) AND (t.org_unit_id IS NULL OR t.org_unit_id = a.org_unit_id)
        ORDER BY (t.org_unit_id IS NOT NULL) DESC, (t.branch_id IS NOT NULL) DESC, t.is_default DESC, t.created_at LIMIT 1`,
      [input.companyId, input.kind, input.employeeId, input.anchorDate]);
    templateId = t.rows[0]?.id ?? null;
  }
  const existing = await db.query('SELECT 1 FROM lifecycle_case WHERE employee_id = $1 AND kind = $2 AND status = $3', [input.employeeId, input.kind, 'active']);
  if (existing.rows.length) throw new ConflictError(input.kind === 'onboarding' ? 'onboarding is already in progress for this employee' : 'an exit clearance is already in progress for this employee');
  const c = await db.query<{ id: string }>(
    `INSERT INTO lifecycle_case (company_id, employee_id, kind, template_id, anchor_date, started_by, exit_id) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
    [input.companyId, input.employeeId, input.kind, templateId, input.anchorDate, input.startedBy, input.exitId ?? null]);
  const caseId = c.rows[0]!.id;
  if (templateId) {
    await db.query(
      `INSERT INTO lifecycle_task (case_id, company_id, employee_id, sort, title, description, department, assignee_type, assignee_user_id, due_date, link)
       SELECT $1, $2, $3, sort, title, description, department, assignee_type, assignee_user_id, $5::date + due_offset_days, link
         FROM lifecycle_template_task WHERE template_id = $4 ORDER BY sort`,
      [caseId, input.companyId, input.employeeId, templateId, input.anchorDate]);
  }
  await audit(db, { actorId: input.startedBy, action: 'start', table: 'lifecycle_case', rowId: caseId, after: { kind: input.kind, employeeId: input.employeeId, templateId } });
  const emp = await db.query<{ name_en: string }>('SELECT name_en FROM employee WHERE id = $1', [input.employeeId]);
  const name = emp.rows[0]?.name_en ?? 'An employee';
  // Tell each person once that they have something to do, with their own count.
  const tasks = await db.query<TaskRow>('SELECT * FROM lifecycle_task WHERE case_id = $1', [caseId]);
  const perUser = new Map<string, number>();
  for (const t of tasks.rows) for (const u of await responsibleUsers(db, t)) perUser.set(u, (perUser.get(u) ?? 0) + 1);
  for (const [userId, n] of perUser) {
    if (userId === input.startedBy) continue;
    const own = (await userIdForEmployee(db, input.employeeId)) === userId;
    await notify(db, {
      companyId: input.companyId, userId, template: `lifecycle.${input.kind}.started`,
      title: input.kind === 'onboarding' ? (own ? 'Welcome! Your getting-started checklist' : `Onboarding for ${name}`) : (own ? 'Your exit checklist' : `Exit clearance for ${name}`),
      body: `${n} ${n === 1 ? 'task is' : 'tasks are'} waiting for you.`, payload: { caseId },
    });
  }
  return caseId;
}

/** Mark a case completed once nothing is left open. */
export async function completeCaseIfDone(db: Queryable, caseId: string): Promise<boolean> {
  const r = await db.query(
    `UPDATE lifecycle_case SET status = 'completed', completed_at = now()
      WHERE id = $1 AND status = 'active' AND NOT EXISTS (SELECT 1 FROM lifecycle_task WHERE case_id = $1 AND status = 'open') RETURNING id`, [caseId]);
  return (r.rowCount ?? 0) > 0;
}

// ---------------------------------------------------------------- probation
/**
 * The confirmation effect of an approved probation review (or a direct HR confirmation): stamps the employee as
 * confirmed and keeps a confirmation record. Exported so a career-history module can record it as an event too.
 */
export async function applyConfirmation(db: Queryable, input: { companyId: string; employeeId: string; confirmedOn: string; reviewId: string | null; actorId: string | null }): Promise<string> {
  await db.query('UPDATE employee SET confirmed_on = $2, updated_at = now() WHERE id = $1', [input.employeeId, input.confirmedOn]);
  const r = await db.query<{ id: string }>(
    'INSERT INTO employee_confirmation (company_id, employee_id, confirmed_on, review_id, created_by) VALUES ($1, $2, $3, $4, $5) RETURNING id',
    [input.companyId, input.employeeId, input.confirmedOn, input.reviewId, input.actorId]);
  await audit(db, { actorId: input.actorId, action: 'confirm', table: 'employee', rowId: input.employeeId, after: { confirmedOn: input.confirmedOn, reviewId: input.reviewId } });
  await logCareerEvent(db, { companyId: input.companyId, employeeId: input.employeeId, kind: 'confirmation', effectiveDate: input.confirmedOn, note: input.reviewId ? 'Confirmed after the probation review' : null, actorId: input.actorId });
  return r.rows[0]!.id;
}

export async function applyProbationDecision(db: Queryable, reviewId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ id: string; company_id: string; employee_id: string; probation_end: string | null; recommendation: string; extend_to: string | null; last_working_day: string | null; status: string; submitted_by: string }>(
    'SELECT * FROM probation_review WHERE id = $1 FOR UPDATE', [reviewId]);
  const rv = r.rows[0];
  if (!rv) throw new NotFoundError('probation review not found');
  if (rv.status !== 'pending') throw new ConflictError(`review already ${rv.status}`);
  await db.query('UPDATE probation_review SET status = $2, decided_at = now() WHERE id = $1', [reviewId, state]);
  const emp = await db.query<{ name_en: string }>('SELECT name_en FROM employee WHERE id = $1', [rv.employee_id]);
  const name = emp.rows[0]?.name_en ?? 'the employee';
  let outcome = 'was not approved';
  if (state === 'approved') {
    const today = nptToday();
    if (rv.recommendation === 'confirm') {
      const on = rv.probation_end && rv.probation_end > today ? rv.probation_end : today;
      await applyConfirmation(db, { companyId: rv.company_id, employeeId: rv.employee_id, confirmedOn: on, reviewId, actorId: ctx.actorId });
      outcome = `confirmed from ${bsLabel(on)}`;
      const own = await userIdForEmployee(db, rv.employee_id);
      if (own) await notify(db, { companyId: rv.company_id, userId: own, employeeId: rv.employee_id, template: 'lifecycle.confirmed', title: 'Congratulations, your employment is confirmed', body: `Your probation is complete. You are confirmed from ${bsLabel(on)}.`, payload: { reviewId }, channels: ['push'] });
    } else if (rv.recommendation === 'extend') {
      await db.query('UPDATE employee SET probation_end = $2, updated_at = now() WHERE id = $1', [rv.employee_id, rv.extend_to]);
      await audit(db, { actorId: ctx.actorId, action: 'extend_probation', table: 'employee', rowId: rv.employee_id, before: { probationEnd: rv.probation_end }, after: { probationEnd: rv.extend_to } });
      outcome = `probation extended to ${bsLabel(rv.extend_to)}`;
      const own = await userIdForEmployee(db, rv.employee_id);
      if (own) await notify(db, { companyId: rv.company_id, userId: own, employeeId: rv.employee_id, template: 'lifecycle.probation_extended', title: 'Probation extended', body: `Your probation now ends on ${bsLabel(rv.extend_to)}. Your manager will talk you through the next steps.`, payload: { reviewId } });
    } else {
      const exitId = await recordExit(db, {
        companyId: rv.company_id, employeeId: rv.employee_id, kind: 'termination', voluntary: false, reasonCategory: 'Probation not successful',
        reason: 'Probation review', lastWorkingDay: rv.last_working_day!, actorId: ctx.actorId,
      });
      outcome = `employment ends on ${bsLabel(rv.last_working_day)}`;
      void exitId;
    }
  }
  await notify(db, { companyId: rv.company_id, userId: rv.submitted_by, template: `lifecycle.probation_review.${state}`, title: `Probation review for ${name}`, body: `Your review ${state === 'approved' ? 'was approved' : 'was not approved'}: ${state === 'approved' ? outcome : ctx.reason ?? 'no reason given'}.`, payload: { reviewId } });
}

// ---------------------------------------------------------------- exits
export const EXIT_KINDS = ['resignation', 'termination', 'retirement', 'contract_end', 'death', 'other'] as const;
export type ExitKind = typeof EXIT_KINDS[number];

/**
 * An accepted exit: the last working day becomes the employee's exit date (sign-in stops the day after) and the exit
 * clearance checklist starts. Used for HR-initiated exits and terminations after a failed probation.
 */
export async function recordExit(db: Queryable, input: {
  companyId: string; employeeId: string; kind: ExitKind; voluntary: boolean; reasonCategory?: string | null; reason?: string | null;
  lastWorkingDay: string; actorId: string | null; templateId?: string | null;
}): Promise<string> {
  const open = await db.query('SELECT 1 FROM employee_exit WHERE employee_id = $1 AND status IN ($2, $3)', [input.employeeId, 'pending', 'accepted']);
  if (open.rows.length) throw new ConflictError('an exit is already in progress for this employee');
  const r = await db.query<{ id: string }>(
    `INSERT INTO employee_exit (company_id, employee_id, kind, voluntary, reason_category, reason, submitted_on, notice_days, proposed_lwd, accepted_lwd, status, initiated_by, decided_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $8, 'accepted', $9, now()) RETURNING id`,
    [input.companyId, input.employeeId, input.kind, input.voluntary, input.reasonCategory ?? null, input.reason ?? null, nptToday(), input.lastWorkingDay, input.actorId]);
  const id = r.rows[0]!.id;
  await acceptExit(db, id, input.lastWorkingDay, input.actorId, input.templateId ?? null);
  return id;
}

export async function acceptExit(db: Queryable, exitId: string, lwd: string, actorId: string | null, templateId: string | null = null): Promise<void> {
  const r = await db.query<{ company_id: string; employee_id: string; kind: string }>(
    `UPDATE employee_exit SET status = 'accepted', accepted_lwd = $2, decided_at = coalesce(decided_at, now()) WHERE id = $1 RETURNING company_id, employee_id, kind`, [exitId, lwd]);
  const ex = r.rows[0]!;
  await db.query('UPDATE employee SET exit_date = $2, updated_at = now() WHERE id = $1', [ex.employee_id, lwd]);
  await audit(db, { actorId, action: 'accept_exit', table: 'employee_exit', rowId: exitId, after: { lastWorkingDay: lwd, kind: ex.kind } });
  const existing = await db.query('SELECT 1 FROM lifecycle_case WHERE employee_id = $1 AND kind = $2 AND status = $3', [ex.employee_id, 'offboarding', 'active']);
  if (!existing.rows.length) await startCase(db, { companyId: ex.company_id, employeeId: ex.employee_id, kind: 'offboarding', templateId, anchorDate: lwd, startedBy: actorId, exitId });
  const own = await userIdForEmployee(db, ex.employee_id);
  if (own && ex.kind !== 'death') {
    await notify(db, { companyId: ex.company_id, userId: own, employeeId: ex.employee_id, template: 'lifecycle.exit.accepted', title: ex.kind === 'resignation' ? 'Resignation accepted' : 'Your last working day',
      body: `Your last working day is ${bsLabel(lwd)}. Your exit checklist is on Home.`, payload: { exitId }, channels: ['push'] });
  }
}

export async function applyResignationDecision(db: Queryable, exitId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ status: string; proposed_lwd: string; accepted_lwd: string | null; employee_id: string; company_id: string }>(
    'SELECT status, proposed_lwd, accepted_lwd, employee_id, company_id FROM employee_exit WHERE id = $1 FOR UPDATE', [exitId]);
  const ex = r.rows[0];
  if (!ex) throw new NotFoundError('resignation not found');
  if (ex.status !== 'pending') throw new ConflictError(`resignation already ${ex.status}`);
  if (state === 'approved') { await acceptExit(db, exitId, ex.accepted_lwd ?? ex.proposed_lwd, ctx.actorId); return; }
  await db.query(`UPDATE employee_exit SET status = 'rejected', decided_at = now() WHERE id = $1`, [exitId]);
  const own = await userIdForEmployee(db, ex.employee_id);
  if (own) await notify(db, { companyId: ex.company_id, userId: own, employeeId: ex.employee_id, template: 'lifecycle.exit.rejected', title: 'Resignation not accepted', body: `${ctx.reason ?? 'Please talk to your manager or HR.'}`, payload: { exitId } });
}

/** Open clearance tasks and assets still held, which stop an exit from being completed. */
export async function clearanceBlockers(db: Queryable, exitId: string, employeeId: string): Promise<{ openTasks: number; assetsHeld: number }> {
  const t = await db.query<{ n: number }>(
    `SELECT count(*)::int AS n FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id
      WHERE c.exit_id = $1 AND c.status = 'active' AND t.status = 'open'`, [exitId]);
  const a = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM asset_assignment WHERE employee_id = $1 AND returned_on IS NULL', [employeeId]);
  return { openTasks: t.rows[0]!.n, assetsHeld: a.rows[0]!.n };
}

/** Finish an exit: the employee becomes exited (sign-in already stops after the last working day). */
export async function completeExit(db: Queryable, exitId: string, actorId: string | null, opts: { skipOpenTasks?: boolean } = {}): Promise<void> {
  const r = await db.query<{ status: string; employee_id: string; company_id: string; accepted_lwd: string; kind: string; reason: string | null }>(
    'SELECT status, employee_id, company_id, accepted_lwd, kind, reason FROM employee_exit WHERE id = $1 FOR UPDATE', [exitId]);
  const ex = r.rows[0];
  if (!ex) throw new NotFoundError('exit not found');
  if (ex.status !== 'accepted') throw new ConflictError(`exit is ${ex.status}`);
  if (ex.accepted_lwd > nptToday()) throw new ConflictError(`the last working day is ${bsLabel(ex.accepted_lwd)}; complete the exit after it`);
  const b = await clearanceBlockers(db, exitId, ex.employee_id);
  if (b.assetsHeld) throw new ConflictError(`${b.assetsHeld} company ${b.assetsHeld === 1 ? 'asset is' : 'assets are'} still with the employee; record the return (or mark lost) first`);
  if (b.openTasks && !opts.skipOpenTasks) throw new ConflictError(`${b.openTasks} clearance ${b.openTasks === 1 ? 'task is' : 'tasks are'} still open`);
  if (b.openTasks) {
    await db.query(`UPDATE lifecycle_task t SET status = 'skipped', done_by = $2, done_at = now(), note = coalesce(note, 'Skipped when the exit was completed')
                      FROM lifecycle_case c WHERE c.id = t.case_id AND c.exit_id = $1 AND t.status = 'open'`, [exitId, actorId]);
  }
  await db.query(`UPDATE lifecycle_case SET status = 'completed', completed_at = now() WHERE exit_id = $1 AND status = 'active'`, [exitId]);
  await db.query(`UPDATE employee_exit SET status = 'completed', completed_at = now() WHERE id = $1`, [exitId]);
  await db.query(`UPDATE employee SET status = 'exited', exit_date = $2, updated_at = now() WHERE id = $1`, [ex.employee_id, ex.accepted_lwd]);
  await audit(db, { actorId, action: 'complete_exit', table: 'employee_exit', rowId: exitId, after: { skippedTasks: b.openTasks } });
  await logCareerEvent(db, { companyId: ex.company_id, employeeId: ex.employee_id, kind: 'separation', effectiveDate: ex.accepted_lwd,
    details: { separationType: ex.kind as 'resignation' }, note: ex.reason, actorId });
}

/** What HR still needs to do in payroll for a leaver (final settlement run, loans, leave encashment). */
export async function settlementStatus(db: Queryable, companyId: string, employeeId: string, lwd: string | null): Promise<Record<string, unknown>> {
  const period = lwd ? (await db.query<{ id: string; bs_year: number; bs_month: number }>(
    'SELECT id, bs_year, bs_month FROM payroll_period WHERE company_id = $1 AND start_ad <= $2::date AND end_ad >= $2::date', [companyId, lwd])).rows[0] ?? null : null;
  const run = (await db.query<{ id: string; status: string; locked_at: Date | null }>(
    `SELECT id, status, locked_at FROM payroll_run WHERE company_id = $1 AND run_type = 'final_settlement' AND params->'employeeIds' ? $2::text ORDER BY created_at DESC LIMIT 1`,
    [companyId, employeeId])).rows[0] ?? null;
  const loans = await db.query<{ n: number; outstanding: string }>(
    `SELECT count(*)::int AS n, coalesce(sum(l.principal - coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)), 0)::text AS outstanding
       FROM loan l WHERE l.employee_id = $1 AND l.status = 'active'`, [employeeId]);
  const leave = await db.query<{ days: string }>(
    `SELECT coalesce(sum(g.delta_days), 0)::text AS days FROM leave_ledger g JOIN leave_type t ON t.id = g.leave_type_id WHERE g.employee_id = $1 AND t.encashable`, [employeeId]);
  const steps: string[] = [];
  if (!run) steps.push(period ? `Create a final settlement run for ${['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra'][period.bs_month - 1]} ${period.bs_year} (Payroll, New run, Final settlement).` : 'Create the payroll period that contains the last working day, then a final settlement run for this employee.');
  else if (!run.locked_at) steps.push('Finish and lock the final settlement run.');
  if (loans.rows[0]!.n) steps.push(`Recover ${loans.rows[0]!.n} active ${loans.rows[0]!.n === 1 ? 'loan or advance' : 'loans or advances'} in the settlement.`);
  if (Number(leave.rows[0]!.days) > 0) steps.push(`Encashable leave of ${Number(leave.rows[0]!.days).toFixed(1)} days is paid out in the settlement.`);
  return {
    period: period ? { id: period.id, bsYear: period.bs_year, bsMonth: period.bs_month } : null,
    run: run ? { id: run.id, status: run.status, locked: !!run.locked_at } : null,
    activeLoans: loans.rows[0]!.n, loanOutstanding: loans.rows[0]!.outstanding, encashableLeaveDays: Number(leave.rows[0]!.days), steps,
  };
}

/**
 * Called when HR adds an employee: opens the default onboarding checklist when the company has switched that on.
 * Runs in the caller's transaction behind a savepoint, so a checklist problem never stops the employee being created.
 */
export async function autoStartOnboarding(db: Queryable, companyId: string, employeeId: string, joinDate: string, actorId: string | null): Promise<string | null> {
  const policy = await lifecyclePolicy(db, companyId);
  if (!policy.autoStartOnboarding || !(await featureOn(db, companyId, 'lifecycle'))) return null;
  await db.query('SAVEPOINT auto_onboarding');
  try {
    const id = await startCase(db, { companyId, employeeId, kind: 'onboarding', anchorDate: joinDate, startedBy: actorId });
    await db.query('RELEASE SAVEPOINT auto_onboarding');
    return id;
  } catch {
    await db.query('ROLLBACK TO SAVEPOINT auto_onboarding');
    return null;
  }
}

// ---------------------------------------------------------------- daily job
export async function lifecycleDaily(db: Db, now = new Date()): Promise<Record<string, number>> {
  const today = nptToday();
  void now;
  const out = { overdueReminders: 0, probationReminders: 0, exitsCompleted: 0, exitsFlagged: 0 };

  // 1. Overdue checklist tasks: one reminder per task to whoever should act.
  const overdue = await db.query<TaskRow & { name_en: string }>(
    `SELECT t.*, e.name_en FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id JOIN employee e ON e.id = t.employee_id
      WHERE t.status = 'open' AND c.status = 'active' AND t.due_date < $1::date ORDER BY t.due_date LIMIT 2000`, [today]);
  for (const t of overdue.rows) {
    if (!(await remindOnce(db, t.company_id, 'task_overdue', t.id, 'overdue'))) continue;
    for (const userId of await responsibleUsers(db, t)) {
      await notify(db, { companyId: t.company_id, userId, template: 'lifecycle.task.overdue', title: 'Checklist task overdue', body: `"${t.title}" for ${t.name_en} was due on ${bsLabel(t.due_date)}.`, payload: { caseId: t.case_id, taskId: t.id } });
    }
    out.overdueReminders++;
  }

  // 2. Probation ending: remind the manager (HR when there is none) at each configured threshold.
  const companies = await db.query<{ id: string }>('SELECT id FROM company');
  for (const c of companies.rows) {
    const policy = await lifecyclePolicy(db, c.id);
    const maxDays = Math.max(0, ...policy.probationReminderDays);
    if (!maxDays) continue;
    const due = await db.query<{ id: string; name_en: string; probation_end: string; days_left: number }>(
      `SELECT e.id, e.name_en, e.probation_end, (e.probation_end - $2::date) AS days_left FROM employee e
        WHERE e.company_id = $1 AND e.status = 'active' AND e.probation_end IS NOT NULL AND e.confirmed_on IS NULL
          AND (e.exit_date IS NULL OR e.exit_date >= $2::date)
          AND e.probation_end >= $2::date AND e.probation_end <= $2::date + $3::int
          AND NOT EXISTS (SELECT 1 FROM probation_review r WHERE r.employee_id = e.id AND r.status = 'pending')`, [c.id, today, maxDays]);
    for (const e of due.rows) {
      const threshold = [...policy.probationReminderDays].sort((a, b) => a - b).find((d) => e.days_left <= d);
      if (threshold === undefined) continue;
      if (!(await remindOnce(db, c.id, 'probation', e.id, `${e.probation_end}:${threshold}`))) continue;
      const mgr = await managerUserOf(db, e.id);
      const targets = mgr ? [mgr] : await userIdsWithRole(db, c.id, 'hr');
      for (const userId of targets) {
        await notify(db, { companyId: c.id, userId, template: 'lifecycle.probation.ending', title: `Probation ends in ${e.days_left} ${e.days_left === 1 ? 'day' : 'days'}`,
          body: `${e.name_en}'s probation ends on ${bsLabel(e.probation_end)}. Submit a probation review to confirm, extend or end it.`, payload: { employeeId: e.id }, channels: ['push'] });
      }
      out.probationReminders++;
    }
  }

  // 3. Exits past their last working day: complete when clearance is done, otherwise tell HR once.
  const exits = await db.query<{ id: string; company_id: string; employee_id: string; name_en: string }>(
    `SELECT x.id, x.company_id, x.employee_id, e.name_en FROM employee_exit x JOIN employee e ON e.id = x.employee_id
      WHERE x.status = 'accepted' AND x.accepted_lwd < $1::date`, [today]);
  for (const x of exits.rows) {
    const b = await clearanceBlockers(db, x.id, x.employee_id);
    if (!b.openTasks && !b.assetsHeld) {
      const client = await db.connect();
      try { await client.query('BEGIN'); await completeExit(client, x.id, null); await client.query('COMMIT'); out.exitsCompleted++; }
      catch { await client.query('ROLLBACK'); } finally { client.release(); }
      continue;
    }
    if (!(await remindOnce(db, x.company_id, 'exit_pending', x.id, 'clearance'))) continue;
    await notifyRole(db, x.company_id, 'hr', { template: 'lifecycle.exit.clearance_pending', title: `Exit clearance pending for ${x.name_en}`,
      body: `The last working day has passed. ${b.openTasks} clearance ${b.openTasks === 1 ? 'task is' : 'tasks are'} open and ${b.assetsHeld} ${b.assetsHeld === 1 ? 'asset is' : 'assets are'} not returned.`, payload: { exitId: x.id } });
    out.exitsFlagged++;
  }
  return out;
}
