import { localDateOf, nptToday } from '@hajiri/attendance-engine';
import { z } from 'zod';
import type { Db, Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { recordFieldChanges, type FieldChange } from './history.js';

/**
 * Effective-dated career events (hire, promotion, transfer, confirmation, separation...). Recording an event that is
 * due applies it at once; a future event waits as 'scheduled' and the daily job applies it on its date. Applying
 * updates the employee row and splits the effective-dated assignment at the event date, and writes field history.
 */
export const CAREER_KINDS = ['hire', 'confirmation', 'promotion', 'transfer', 'designation_change', 'grade_change', 'manager_change',
  'employment_type_change', 'salary_revision', 'separation', 'rehire', 'note', 'suspension', 'leave_of_absence', 'acting', 'deputation'] as const;
export type CareerKind = typeof CAREER_KINDS[number];

export const CAREER_LABEL: Record<CareerKind, string> = {
  hire: 'Joined', confirmation: 'Confirmed', promotion: 'Promoted', transfer: 'Transferred', designation_change: 'Designation changed',
  grade_change: 'Grade changed', manager_change: 'Manager changed', employment_type_change: 'Employment type changed', salary_revision: 'Salary revised',
  separation: 'Left the company', rehire: 'Rejoined', note: 'Note',
  suspension: 'Suspended', leave_of_absence: 'Leave of absence', acting: 'Acting assignment', deputation: 'Deputation',
};

/** Temporary or record-only events: logged on the timeline, never applied to the permanent record. */
export const TEMPORARY_KINDS: ReadonlySet<CareerKind> = new Set(['suspension', 'leave_of_absence', 'acting', 'deputation']);

export const careerDetails = z.object({
  designationId: z.uuid().optional(),
  designation: z.string().trim().min(1).max(120).optional(),
  grade: z.string().trim().min(1).max(40).optional(),
  branchId: z.uuid().optional(),
  orgUnitId: z.uuid().nullable().optional(),
  managerId: z.uuid().nullable().optional(),
  employmentType: z.enum(['regular', 'work_based', 'time_based', 'casual', 'part_time']).optional(),
  separationType: z.enum(['resignation', 'termination', 'retirement', 'contract_end', 'death', 'other']).optional(),
  revisionPercent: z.number().min(-100).max(1000).optional(),
  reason: z.string().trim().max(500).optional(),
  endDate: isoDate.optional(),
}).strict();
export type CareerDetails = z.infer<typeof careerDetails>;

export const careerBody = z.object({
  kind: z.enum(CAREER_KINDS),
  effectiveDate: isoDate,
  details: careerDetails.default({}),
  note: z.string().trim().max(1000).optional(),
});

export interface CareerEventInput {
  companyId: string; employeeId: string; kind: CareerKind; effectiveDate: string; details?: CareerDetails; note?: string | null; actorId?: string | null;
}

export interface CareerEventRow {
  id: string; company_id: string; employee_id: string; kind: CareerKind; effective_date: string; details: CareerDetails; before: Record<string, unknown> | null;
  note: string | null; status: 'scheduled' | 'applied' | 'cancelled'; applied_at: Date | null; created_by: string | null; created_at: Date;
}

/** A separation takes effect the day after the last working day; everything else on its date. */
const isDue = (kind: CareerKind, effective: string, today: string) => kind === 'separation' ? effective < today : effective <= today;

async function checkRefs(db: Queryable, companyId: string, employeeId: string, kind: CareerKind, d: CareerDetails): Promise<void> {
  const owned = async (table: string, id: string | null | undefined, label: string) => {
    if (!id) return;
    const r = await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (!r.rows[0]) throw new BadRequestError(`${label} does not exist in your company`);
  };
  await owned('designation', d.designationId, 'designation');
  await owned('branch', d.branchId, 'branch');
  await owned('org_unit', d.orgUnitId, 'department');
  await owned('employee', d.managerId, 'manager');
  if (d.managerId && d.managerId === employeeId) throw new BadRequestError('an employee cannot be their own manager');
  const need = (ok: boolean, msg: string) => { if (!ok) throw new BadRequestError(msg); };
  switch (kind) {
    case 'promotion': need(!!(d.designationId || d.designation || d.grade), 'a promotion needs a new designation or grade'); break;
    case 'designation_change': need(!!(d.designationId || d.designation), 'choose the new designation'); break;
    case 'grade_change': need(!!d.grade, 'enter the new grade'); break;
    case 'transfer': need(d.branchId !== undefined || d.orgUnitId !== undefined, 'a transfer needs a new branch or department'); break;
    case 'manager_change': need(d.managerId !== undefined, 'choose the new manager'); break;
    case 'employment_type_change': need(!!d.employmentType, 'choose the new employment type'); break;
    case 'separation': need(!!d.separationType, 'choose the type of separation'); break;
    default: break;
  }
}

/** Record a career event; applies it now when due. Call inside a transaction (it locks the employee row). */
export async function recordCareerEvent(db: Queryable, input: CareerEventInput, today = nptToday()): Promise<CareerEventRow> {
  const d = careerDetails.parse(input.details ?? {});
  const emp = await db.query<{ id: string; join_date: string | null; status: string }>(
    'SELECT id, join_date, status FROM employee WHERE id = $1 AND company_id = $2 FOR UPDATE', [input.employeeId, input.companyId]);
  if (!emp.rows[0]) throw new NotFoundError('employee not found');
  await checkRefs(db, input.companyId, input.employeeId, input.kind, d);
  const joined = emp.rows[0].join_date;
  if (joined && input.effectiveDate < joined && input.kind !== 'hire' && input.kind !== 'note') throw new BadRequestError('the effective date is before the employee joined');
  if (input.kind === 'separation') {
    const open = await db.query(`SELECT 1 FROM career_event WHERE employee_id = $1 AND kind = 'separation' AND status = 'scheduled'`, [input.employeeId]);
    if (open.rows[0]) throw new ConflictError('a separation is already scheduled; cancel it first');
    // The exit date is known as soon as the separation is recorded (payroll settles up to it); the status changes on the day after.
    await db.query('UPDATE employee SET exit_date = $2, updated_at = now() WHERE id = $1', [input.employeeId, input.effectiveDate]);
  }
  const r = await db.query<CareerEventRow>(
    `INSERT INTO career_event (company_id, employee_id, kind, effective_date, details, note, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *`,
    [input.companyId, input.employeeId, input.kind, input.effectiveDate, JSON.stringify(d), input.note ?? null, input.actorId ?? null]);
  let ev = r.rows[0]!;
  if (input.kind === 'hire' || input.kind === 'note' || input.kind === 'salary_revision') {
    ev = (await db.query<CareerEventRow>(`UPDATE career_event SET status = 'applied', applied_at = now() WHERE id = $1 RETURNING *`, [ev.id])).rows[0]!;
  } else if (isDue(ev.kind, ev.effective_date, today)) {
    ev = await applyCareerEvent(db, ev, input.actorId ?? null);
  }
  return ev;
}

/**
 * Record an event that another module has already put into effect (a probation confirmation, a completed exit), so it
 * appears on the career timeline without being applied a second time.
 */
export async function logCareerEvent(db: Queryable, input: CareerEventInput): Promise<void> {
  const d = careerDetails.parse(input.details ?? {});
  await db.query(
    `INSERT INTO career_event (company_id, employee_id, kind, effective_date, details, note, status, applied_at, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, 'applied', now(), $7)`,
    [input.companyId, input.employeeId, input.kind, input.effectiveDate, JSON.stringify(d), input.note ?? null, input.actorId ?? null]);
}

interface Snapshot {
  designation: string | null; designation_id: string | null; grade: string | null; status: string; exit_date: string | null; confirmed_on: string | null;
  branch_id: string | null; org_unit_id: string | null; manager_id: string | null; employment_type: string | null;
}

async function snapshot(db: Queryable, employeeId: string, onDate: string): Promise<Snapshot> {
  const r = await db.query<Snapshot>(
    `SELECT e.designation, e.designation_id, e.grade, e.status, e.exit_date, e.confirmed_on, a.branch_id, a.org_unit_id, a.manager_id, a.employment_type
       FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date WHERE e.id = $1`, [employeeId, onDate]);
  return r.rows[0]!;
}

/**
 * Split the employee's effective-dated assignment at `from` and apply `changes` to the period that starts there.
 * Later periods (already planned) keep their own values.
 */
export async function splitAssignment(db: Queryable, employeeId: string, from: string, changes: Partial<Record<'branch_id' | 'org_unit_id' | 'manager_id' | 'employment_type', string | null>>): Promise<void> {
  const cur = await db.query<{ lower: string | null; upper: string | null; branch_id: string | null; org_unit_id: string | null; manager_id: string | null; employment_type: string; probation_end: string | null }>(
    `SELECT lower(valid)::text AS lower, upper(valid)::text AS upper, branch_id, org_unit_id, manager_id, employment_type, probation_end
       FROM employee_assignment WHERE employee_id = $1 AND valid @> $2::date`, [employeeId, from]);
  const c = cur.rows[0];
  if (!c) {
    // Nothing covers the date (e.g. before the first assignment): open a period up to the next one, if any.
    const next = await db.query<{ lower: string }>(`SELECT lower(valid)::text AS lower FROM employee_assignment WHERE employee_id = $1 AND lower(valid) > $2::date ORDER BY lower(valid) LIMIT 1`, [employeeId, from]);
    await db.query(
      `INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type) VALUES ($1, daterange($2::date, $3::date), $4, $5, $6, $7)`,
      [employeeId, from, next.rows[0]?.lower ?? null, changes.branch_id ?? null, changes.org_unit_id ?? null, changes.manager_id ?? null, changes.employment_type ?? 'regular']);
    return;
  }
  const merged = {
    branch_id: changes.branch_id !== undefined ? changes.branch_id : c.branch_id,
    org_unit_id: changes.org_unit_id !== undefined ? changes.org_unit_id : c.org_unit_id,
    manager_id: changes.manager_id !== undefined ? changes.manager_id : c.manager_id,
    employment_type: changes.employment_type ?? c.employment_type,
  };
  if (c.lower === from) {
    await db.query(`UPDATE employee_assignment SET branch_id = $3, org_unit_id = $4, manager_id = $5, employment_type = $6 WHERE employee_id = $1 AND lower(valid) = $2::date`,
      [employeeId, from, merged.branch_id, merged.org_unit_id, merged.manager_id, merged.employment_type]);
    return;
  }
  await db.query(`UPDATE employee_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND valid @> $2::date`, [employeeId, from]);
  await db.query(
    `INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type, probation_end)
     VALUES ($1, daterange($2::date, $3::date), $4, $5, $6, $7, $8)`,
    [employeeId, from, c.upper, merged.branch_id, merged.org_unit_id, merged.manager_id, merged.employment_type, c.probation_end]);
}

const EMPLOYMENT: Record<string, string> = { regular: 'Regular', work_based: 'Work-based', time_based: 'Time-based', casual: 'Casual', part_time: 'Part-time' };

async function nameOf(db: Queryable, table: 'branch' | 'org_unit' | 'employee', id: string | null | undefined): Promise<string | null> {
  if (!id) return null;
  return (await db.query<{ n: string }>(`SELECT name_en AS n FROM ${table} WHERE id = $1`, [id])).rows[0]?.n ?? null;
}

/** Apply a scheduled event (locks it). Returns the updated row. */
export async function applyCareerEvent(db: Queryable, ev: CareerEventRow, actorId: string | null): Promise<CareerEventRow> {
  const locked = await db.query<CareerEventRow>(`SELECT * FROM career_event WHERE id = $1 FOR UPDATE`, [ev.id]);
  const e = locked.rows[0];
  if (!e || e.status !== 'scheduled') return e ?? ev;
  const d = e.details;
  const before = await snapshot(db, e.employee_id, e.effective_date);
  const empSets: Record<string, unknown> = {};
  const assignment: Partial<Record<'branch_id' | 'org_unit_id' | 'manager_id' | 'employment_type', string | null>> = {};
  if (TEMPORARY_KINDS.has(e.kind)) { /* acting, deputation, suspension and leave of absence change nothing permanent */ }
  else if (d.designationId) {
    const des = await db.query<{ name_en: string; grade: string | null }>('SELECT name_en, grade FROM designation WHERE id = $1', [d.designationId]);
    empSets.designation_id = d.designationId;
    empSets.designation = des.rows[0]?.name_en ?? d.designation ?? null;
    if (!d.grade && des.rows[0]?.grade) empSets.grade = des.rows[0].grade;
  } else if (d.designation) { empSets.designation = d.designation; empSets.designation_id = null; }
  if (!TEMPORARY_KINDS.has(e.kind)) {
    if (d.grade) empSets.grade = d.grade;
    if (d.branchId !== undefined) assignment.branch_id = d.branchId;
    if (d.orgUnitId !== undefined) assignment.org_unit_id = d.orgUnitId;
    if (d.managerId !== undefined) assignment.manager_id = d.managerId;
    if (d.employmentType) assignment.employment_type = d.employmentType;
  }
  if (e.kind === 'confirmation') empSets.confirmed_on = e.effective_date;
  if (e.kind === 'separation') { empSets.status = 'exited'; empSets.exit_date = e.effective_date; }
  if (e.kind === 'rehire') { empSets.status = 'active'; empSets.exit_date = null; }

  const entries = Object.entries(empSets);
  if (entries.length) {
    await db.query(`UPDATE employee SET ${entries.map(([c], i) => `${c} = $${i + 2}`).join(', ')}, updated_at = now() WHERE id = $1`, [e.employee_id, ...entries.map(([, v]) => v)]);
  }
  // Sign-in ends by itself once the exit date has passed (auth checks employee.exit_date).
  if (Object.keys(assignment).length) await splitAssignment(db, e.employee_id, e.effective_date, assignment);

  const after = await snapshot(db, e.employee_id, e.effective_date);
  const changes: FieldChange[] = [];
  const push = (field: string, label: string, from: string | null, to: string | null) => { if (from !== to) changes.push({ field, label, from, to }); };
  push('designation', 'Designation', before.designation, after.designation);
  push('grade', 'Grade', before.grade, after.grade);
  push('branch_id', 'Branch', await nameOf(db, 'branch', before.branch_id), await nameOf(db, 'branch', after.branch_id));
  push('org_unit_id', 'Department', await nameOf(db, 'org_unit', before.org_unit_id), await nameOf(db, 'org_unit', after.org_unit_id));
  push('manager_id', 'Manager', await nameOf(db, 'employee', before.manager_id), await nameOf(db, 'employee', after.manager_id));
  push('employment_type', 'Employment type', before.employment_type ? EMPLOYMENT[before.employment_type] ?? before.employment_type : null, after.employment_type ? EMPLOYMENT[after.employment_type] ?? after.employment_type : null);
  push('confirmed_on', 'Confirmed on', before.confirmed_on, after.confirmed_on);
  push('status', 'Status', before.status, after.status);
  const companyId = e.company_id;
  await recordFieldChanges(db, { companyId, employeeId: e.employee_id, changes, source: 'career_event', refId: e.id, actorId });
  const r = await db.query<CareerEventRow>(`UPDATE career_event SET status = 'applied', applied_at = now(), before = $2 WHERE id = $1 RETURNING *`,
    [e.id, JSON.stringify(before)]);
  const userId = await userIdForEmployee(db, e.employee_id);
  if (userId && e.kind !== 'separation') {
    await notify(db, { companyId, userId, template: 'people.career_event', title: CAREER_LABEL[e.kind], body: careerSentence(e.kind, after), payload: { eventId: e.id } });
  }
  return r.rows[0]!;
}

function careerSentence(kind: CareerKind, s: Snapshot): string {
  switch (kind) {
    case 'promotion': case 'designation_change': return `Your designation is now ${s.designation ?? '—'}${s.grade ? `, grade ${s.grade}` : ''}.`;
    case 'grade_change': return `Your grade is now ${s.grade ?? '—'}.`;
    case 'confirmation': return 'Your employment has been confirmed. Congratulations!';
    case 'transfer': return 'Your branch or department has changed. See your profile for details.';
    case 'manager_change': return 'Your manager has changed. See your profile for details.';
    default: return 'Your employment record was updated.';
  }
}

/** Daily job: apply every scheduled event that has become due, each in its own transaction. */
export async function applyDueCareerEvents(db: Db, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  const due = await db.query<CareerEventRow>(
    `SELECT * FROM career_event WHERE status = 'scheduled' AND (effective_date < $1::date OR (kind <> 'separation' AND effective_date = $1::date)) ORDER BY effective_date, created_at`, [today]);
  let n = 0;
  for (const ev of due.rows) {
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await applyCareerEvent(client, ev, null);
      await client.query('COMMIT');
      n++;
    } catch (err) { await client.query('ROLLBACK'); throw err; } finally { client.release(); }
  }
  return n;
}

/** Cancel a scheduled event. A cancelled separation clears the exit date it set. */
export async function cancelCareerEvent(db: Queryable, companyId: string, employeeId: string, eventId: string): Promise<void> {
  const r = await db.query<CareerEventRow>('SELECT * FROM career_event WHERE id = $1 AND employee_id = $2 AND company_id = $3 FOR UPDATE', [eventId, employeeId, companyId]);
  const ev = r.rows[0];
  if (!ev) throw new NotFoundError('event not found');
  if (ev.status !== 'scheduled') throw new ConflictError('only a scheduled event can be cancelled; record a new event to change an applied one');
  await db.query(`UPDATE career_event SET status = 'cancelled' WHERE id = $1`, [eventId]);
  if (ev.kind === 'separation') await db.query(`UPDATE employee SET exit_date = NULL, updated_at = now() WHERE id = $1 AND status = 'active'`, [employeeId]);
}
