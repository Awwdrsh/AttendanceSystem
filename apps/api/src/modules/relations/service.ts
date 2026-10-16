import { localDateOf, nptToday } from '@hajiri/attendance-engine';
import { z } from 'zod';
import { bsLabel } from '../../bsdate.js';
import { sha256Hex, type FieldCipher } from '../../crypto.js';
import type { Db, Queryable } from '../../db/pool.js';
import { HttpError } from '../../errors.js';
import { hasRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { notify, userIdForEmployee, userIdsWithRole } from '../notifications/service.js';
import { logCareerEvent } from '../people/career.js';

// ---------------------------------------------------------------- policy

export const relationsPolicySchema = z.object({
  retirementAge: z.number().int().min(40).max(80).default(58),
  serviceYears: z.array(z.number().int().min(1).max(60)).max(20).default([1, 5, 10, 15, 20, 25, 30]),
  registeredDevicesOnly: z.boolean().default(false),
  appealDays: z.number().int().min(1).max(90).default(15),
});
export type RelationsPolicy = z.infer<typeof relationsPolicySchema>;

export async function relationsPolicy(db: Queryable, companyId: string): Promise<RelationsPolicy> {
  const r = await db.query<{ value: unknown }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'relations.policy'`, [companyId]);
  const p = relationsPolicySchema.safeParse(r.rows[0]?.value ?? {});
  return p.success ? p.data : relationsPolicySchema.parse({});
}

// ---------------------------------------------------------------- helpers

export const isHr = (u: AuthUser) => hasRole(u, 'hr');

export async function newId(db: Queryable): Promise<string> {
  return (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
}

/** Next per-company case number; the advisory lock keeps two filings from taking the same number. */
export async function nextNumber(db: Queryable, table: 'grievance' | 'disciplinary_case', companyId: string): Promise<number> {
  await db.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`${table}:${companyId}`]);
  const r = await db.query<{ n: number }>(`SELECT coalesce(max(number), 0) + 1 AS n FROM ${table} WHERE company_id = $1`, [companyId]);
  return r.rows[0]!.n;
}

export const encText = (cipher: FieldCipher, text: string, aad: string) => cipher.encrypt(text, aad);
export const decText = (cipher: FieldCipher, blob: Buffer | null, aad: string): string | null => (blob ? cipher.decrypt(blob, aad) : null);

export const grievanceAad = (companyId: string, id: string, field: string) => `grievance:${companyId}:${id}:${field}`;
export const grievanceNoteAad = (grievanceId: string, noteId: string) => `grievance_note:${grievanceId}:${noteId}`;
export const caseAad = (companyId: string, id: string, field: string) => `disciplinary:${companyId}:${id}:${field}`;

async function remindOnce(db: Queryable, companyId: string, kind: string, ref: string): Promise<boolean> {
  const r = await db.query('INSERT INTO relations_reminder_sent (company_id, kind, ref) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', [companyId, kind, ref]);
  return (r.rowCount ?? 0) > 0;
}

// ---------------------------------------------------------------- grievances

export const GRIEVANCE_CATEGORIES = ['harassment', 'discrimination', 'safety', 'pay_benefits', 'management', 'policy', 'other'] as const;

export interface GrievanceRow {
  id: string; company_id: string; number: number; employee_id: string; user_id: string; category: string; subject_enc: Buffer; description_enc: Buffer;
  concerns_enc: Buffer | null; status: string; excluded_user_ids: string[]; hearing_at: Date | null; hearing_place: string | null; outcome: string | null;
  findings_enc: Buffer | null; decided_by: string | null; decided_at: Date | null; appeal_reason_enc: Buffer | null; appealed_at: Date | null;
  appeal_outcome: string | null; appeal_note_enc: Buffer | null; appeal_decided_by: string | null; appeal_decided_at: Date | null; created_at: Date; updated_at: Date;
}

export type GrievanceRole = 'complainant' | 'committee' | 'hr';

export async function isCommitteeMember(db: Queryable, companyId: string, userId: string): Promise<boolean> {
  const r = await db.query('SELECT 1 FROM grievance_committee_member WHERE company_id = $1 AND user_id = $2', [companyId, userId]);
  return r.rows.length > 0;
}

/**
 * Who may see a grievance: the person who filed it, HR and the grievance committee. Line managers never see a grievance
 * through their role. Anyone HR excluded from a case (a conflict of interest) loses access to it.
 */
export async function grievanceRole(db: Queryable, user: AuthUser, g: GrievanceRow): Promise<GrievanceRole | null> {
  if (user.companyId !== g.company_id) return null;
  if (g.user_id === user.id) return 'complainant';
  if (g.excluded_user_ids.includes(user.id)) return null;
  if (isHr(user)) return 'hr';
  if (await isCommitteeMember(db, g.company_id, user.id)) return 'committee';
  return null;
}

/** HR and the committee, minus anyone excluded from the case and the complainant. */
export async function grievanceHandlers(db: Queryable, g: Pick<GrievanceRow, 'company_id' | 'user_id' | 'excluded_user_ids'>): Promise<string[]> {
  const hr = await userIdsWithRole(db, g.company_id, 'hr');
  const committee = (await db.query<{ user_id: string }>(
    `SELECT m.user_id FROM grievance_committee_member m JOIN app_user u ON u.id = m.user_id AND u.status = 'active' WHERE m.company_id = $1`, [g.company_id])).rows.map((r) => r.user_id);
  return [...new Set([...hr, ...committee])].filter((id) => id !== g.user_id && !g.excluded_user_ids.includes(id));
}

// ---------------------------------------------------------------- disciplinary

export const DEFAULT_STAGES: Array<{ code: string; name: string; severity: number; effect: 'none' | 'suspension' | 'termination'; letter: string | null }> = [
  { code: 'verbal_warning', name: 'Verbal warning', severity: 1, effect: 'none', letter: null },
  { code: 'written_warning', name: 'Written warning', severity: 2, effect: 'none', letter: 'warning' },
  { code: 'final_warning', name: 'Final written warning', severity: 3, effect: 'none', letter: 'warning' },
  { code: 'suspension', name: 'Suspension', severity: 4, effect: 'suspension', letter: 'warning' },
  { code: 'termination', name: 'Termination', severity: 5, effect: 'termination', letter: null },
];

export async function ensureStages(db: Queryable, companyId: string): Promise<void> {
  const any = await db.query('SELECT 1 FROM disciplinary_stage WHERE company_id = $1 LIMIT 1', [companyId]);
  if (any.rows.length) return;
  for (const [i, s] of DEFAULT_STAGES.entries()) {
    await db.query(
      `INSERT INTO disciplinary_stage (company_id, code, name, severity, effect, letter_template_code, sort) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING`,
      [companyId, s.code, s.name, s.severity, s.effect, s.letter, i]);
  }
}

// ---------------------------------------------------------------- registered devices

export const deviceTokenHash = (token: string) => sha256Hex(`device:${token}`);

/**
 * When the company allows browser clock-in only from registered devices, the request must carry the token of a device
 * HR approved for this employee. Throws a 403 with code 'device_not_registered' otherwise.
 */
export async function assertRegisteredDevice(db: Queryable, companyId: string, employeeId: string, token: string | undefined): Promise<void> {
  if (!(await relationsPolicy(db, companyId)).registeredDevicesOnly) return;
  if (token && token.length >= 16 && token.length <= 200) {
    const r = await db.query<{ id: string }>(
      `UPDATE registered_device SET last_used_at = now() WHERE token_hash = $1 AND employee_id = $2 AND company_id = $3 AND status = 'approved' RETURNING id`,
      [deviceTokenHash(token), employeeId, companyId]);
    if (r.rows.length) return;
  }
  throw new HttpError(403, 'Clock in from a registered device. Register this phone under My attendance and ask HR to approve it.', { code: 'device_not_registered' });
}

// ---------------------------------------------------------------- completeness (KYE)

export const KYE_SECTIONS = ['personal', 'contact', 'emergency', 'family', 'education', 'experience', 'ids', 'bank', 'photo'] as const;
export type KyeSection = typeof KYE_SECTIONS[number];
export const SECTION_LABEL: Record<KyeSection, string> = {
  personal: 'Personal details', contact: 'Contact and address', emergency: 'Emergency contacts', family: 'Family and nominees', education: 'Education',
  experience: 'Previous employment', ids: 'ID documents', bank: 'Bank account', photo: 'Profile photo',
};
/** Sections whose absence counts against the record; previous employment is optional (first jobs have none). */
export const REQUIRED_SECTIONS: KyeSection[] = ['personal', 'contact', 'emergency', 'family', 'education', 'ids', 'bank', 'photo'];

export interface Completeness { employeeId: string; empCode: string; name: string; missing: KyeSection[]; percent: number }

export async function completeness(db: Queryable, companyId: string, employeeId?: string): Promise<Completeness[]> {
  const r = await db.query<{ id: string; emp_code: string; name_en: string } & Record<KyeSection, boolean>>(
    `SELECT e.id, e.emp_code, e.name_en,
            (e.dob IS NOT NULL AND e.gender IS NOT NULL AND e.marital_status IS NOT NULL AND e.blood_group IS NOT NULL) AS personal,
            (coalesce(e.personal_phone, e.phone) IS NOT NULL AND e.permanent_address IS NOT NULL) AS contact,
            EXISTS (SELECT 1 FROM employee_emergency_contact x WHERE x.employee_id = e.id) AS emergency,
            EXISTS (SELECT 1 FROM employee_family x WHERE x.employee_id = e.id) AS family,
            EXISTS (SELECT 1 FROM employee_education x WHERE x.employee_id = e.id) AS education,
            EXISTS (SELECT 1 FROM employee_experience x WHERE x.employee_id = e.id) AS experience,
            (e.citizenship_no_enc IS NOT NULL AND e.pan_enc IS NOT NULL) AS ids,
            (e.bank_account_enc IS NOT NULL) AS bank,
            (e.avatar_file_id IS NOT NULL) AS photo
       FROM employee e WHERE e.company_id = $1 AND e.status = 'active' AND ($2::uuid IS NULL OR e.id = $2) ORDER BY e.emp_code`, [companyId, employeeId ?? null]);
  return r.rows.map((x) => {
    const missing = REQUIRED_SECTIONS.filter((s) => !x[s]);
    return { employeeId: x.id, empCode: x.emp_code, name: x.name_en, missing, percent: Math.round(((REQUIRED_SECTIONS.length - missing.length) / REQUIRED_SECTIONS.length) * 100) };
  });
}

// ---------------------------------------------------------------- milestones

export interface Milestone { kind: 'service' | 'contract_end' | 'retirement' | 'probation_end' | 'assignment_end' | 'exit'; employeeId: string; empCode: string; name: string; date: string; detail: string }

/** Upcoming dates HR plans around, from `from` for `days` days (retirements already due are included). */
export async function milestones(db: Queryable, companyId: string, from: string, days: number): Promise<Milestone[]> {
  const p = await relationsPolicy(db, companyId);
  const out: Milestone[] = [];
  const to = (await db.query<{ d: string }>(`SELECT ($1::date + $2::int)::text AS d`, [from, days])).rows[0]!.d;
  const service = await db.query<{ id: string; emp_code: string; name_en: string; d: string; years: number }>(
    `SELECT e.id, e.emp_code, e.name_en, (e.join_date + make_interval(years => y))::date::text AS d, y AS years
       FROM employee e CROSS JOIN unnest($4::int[]) AS y
      WHERE e.company_id = $1 AND e.status = 'active' AND e.join_date IS NOT NULL
        AND (e.join_date + make_interval(years => y))::date BETWEEN $2::date AND $3::date`, [companyId, from, to, p.serviceYears]);
  for (const s of service.rows) out.push({ kind: 'service', employeeId: s.id, empCode: s.emp_code, name: s.name_en, date: s.d, detail: `${s.years} ${s.years === 1 ? 'year' : 'years'} of service` });
  const other = await db.query<{ id: string; emp_code: string; name_en: string; contract_end: string | null; retire: string | null; probation: string | null; exit_date: string | null }>(
    `SELECT e.id, e.emp_code, e.name_en, e.contract_end::text, (e.dob + make_interval(years => $4))::date::text AS retire,
            CASE WHEN e.confirmed_on IS NULL THEN coalesce(a.probation_end, e.probation_end)::text END AS probation, e.exit_date::text
       FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date
      WHERE e.company_id = $1 AND e.status = 'active'
        AND (e.contract_end BETWEEN $2::date AND $3::date OR (e.dob + make_interval(years => $4))::date <= $3::date
             OR (e.confirmed_on IS NULL AND coalesce(a.probation_end, e.probation_end) BETWEEN $2::date AND $3::date)
             OR e.exit_date BETWEEN $2::date AND $3::date)`, [companyId, from, to, p.retirementAge]);
  for (const e of other.rows) {
    const base = { employeeId: e.id, empCode: e.emp_code, name: e.name_en };
    if (e.contract_end && e.contract_end >= from && e.contract_end <= to) out.push({ ...base, kind: 'contract_end', date: e.contract_end, detail: 'Contract ends' });
    if (e.retire && e.retire <= to && !e.exit_date) out.push({ ...base, kind: 'retirement', date: e.retire, detail: e.retire < from ? `Past retirement age (${p.retirementAge})` : `Reaches retirement age (${p.retirementAge})` });
    if (e.probation && e.probation >= from && e.probation <= to) out.push({ ...base, kind: 'probation_end', date: e.probation, detail: 'Probation ends' });
    if (e.exit_date && e.exit_date >= from && e.exit_date <= to) out.push({ ...base, kind: 'exit', date: e.exit_date, detail: 'Last working day' });
  }
  const temp = await db.query<{ employee_id: string; emp_code: string; name_en: string; end_date: string; kind: string }>(
    `SELECT t.employee_id, e.emp_code, e.name_en, t.end_date::text, t.kind FROM employee_temp_assignment t JOIN employee e ON e.id = t.employee_id
      WHERE t.company_id = $1 AND t.status = 'active' AND t.end_date BETWEEN $2::date AND $3::date`, [companyId, from, to]);
  for (const t of temp.rows) out.push({ kind: 'assignment_end', employeeId: t.employee_id, empCode: t.emp_code, name: t.name_en, date: t.end_date, detail: t.kind === 'acting' ? 'Acting assignment ends' : 'Deputation ends' });
  return out.sort((a, b) => a.date.localeCompare(b.date) || a.name.localeCompare(b.name));
}

// ---------------------------------------------------------------- daily job

async function managerUserOf(db: Queryable, employeeId: string, onDate: string): Promise<string | null> {
  const r = await db.query<{ id: string }>(
    `SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id AND u.status = 'active' WHERE a.employee_id = $1 AND a.valid @> $2::date`, [employeeId, onDate]);
  return r.rows[0]?.id ?? null;
}

/**
 * Once a day: end acting and deputation assignments that ran out, remind HR (and the manager) about milestones,
 * flag disciplinary responses that are overdue, remind KYE stragglers three days before the due date, and close
 * decided grievances whose appeal window has passed.
 */
export async function relationsDaily(db: Db, now = new Date()): Promise<Record<string, number>> {
  const today = localDateOf(now.getTime());
  const out = { assignmentsEnded: 0, milestoneReminders: 0, overdueResponses: 0, kyeReminders: 0, grievancesClosed: 0 };

  const ended = await db.query<{ id: string; company_id: string; employee_id: string; kind: string }>(
    `UPDATE employee_temp_assignment SET status = 'ended', ended_at = now() WHERE status = 'active' AND end_date < $1::date RETURNING id, company_id, employee_id, kind`, [today]);
  for (const t of ended.rows) {
    out.assignmentsEnded++;
    const u = await userIdForEmployee(db, t.employee_id);
    if (u) await notify(db, { companyId: t.company_id, userId: u, employeeId: t.employee_id, template: 'relations.assignment.ended', title: t.kind === 'acting' ? 'Acting assignment ended' : 'Deputation ended', body: 'Your temporary assignment has ended; your regular role continues.', payload: { assignmentId: t.id } });
  }

  const companies = await db.query<{ id: string }>('SELECT id FROM company');
  for (const c of companies.rows) {
    const list = await milestones(db, c.id, today, 30);
    const hr = await userIdsWithRole(db, c.id, 'hr');
    for (const m of list) {
      // Service anniversaries on the day; contract ends and retirements 30 and 7 days ahead and on the day.
      const daysLeft = Math.round((Date.parse(m.date) - Date.parse(today)) / 86_400_000);
      let key: string | null = null;
      if (m.kind === 'service' && daysLeft === 0) key = 'day';
      if ((m.kind === 'contract_end' || m.kind === 'retirement') && [30, 7, 0].includes(daysLeft)) key = `d${daysLeft}`;
      if (m.kind === 'retirement' && daysLeft < 0) key = 'past';
      if (!key) continue;
      if (!(await remindOnce(db, c.id, `milestone.${m.kind}`, `${m.employeeId}:${m.date}:${key}`))) continue;
      const title = m.kind === 'service' ? `${m.name}: ${m.detail}` : `${m.detail}: ${m.name}`;
      const body = m.kind === 'service' ? `${m.name} completes ${m.detail} today (${bsLabel(m.date)}).` : `${m.name} (${m.empCode}): ${m.detail.toLowerCase()} on ${bsLabel(m.date)}. Plan the next step under Employee relations, Milestones.`;
      const targets = new Set(hr);
      const mgr = await managerUserOf(db, m.employeeId, today);
      if (mgr) targets.add(mgr);
      for (const userId of targets) await notify(db, { companyId: c.id, userId, template: `relations.milestone.${m.kind}`, title, body, payload: { employeeId: m.employeeId, date: m.date } });
      out.milestoneReminders++;
    }
  }

  const overdue = await db.query<{ id: string; company_id: string; number: number }>(
    `UPDATE disciplinary_case SET overdue_notified = true WHERE status = 'show_cause' AND response_due < $1::date AND NOT overdue_notified RETURNING id, company_id, number`, [today]);
  for (const k of overdue.rows) {
    for (const userId of await userIdsWithRole(db, k.company_id, 'hr')) {
      await notify(db, { companyId: k.company_id, userId, template: 'relations.disciplinary.overdue', title: `Case D-${k.number}: no response yet`, body: 'The response deadline has passed. You can now decide the case.', payload: { caseId: k.id } });
    }
    out.overdueResponses++;
  }

  const kye = await db.query<{ id: string; company_id: string; title: string; due_date: string }>(
    `UPDATE kye_campaign SET reminded = true WHERE status = 'open' AND NOT reminded AND due_date - 3 <= $1::date RETURNING id, company_id, title, due_date::text`, [today]);
  for (const k of kye.rows) {
    const pending = await db.query<{ employee_id: string }>(`SELECT employee_id FROM kye_response WHERE campaign_id = $1 AND status = 'pending'`, [k.id]);
    for (const p of pending.rows) {
      const u = await userIdForEmployee(db, p.employee_id);
      if (u) { await notify(db, { companyId: k.company_id, userId: u, employeeId: p.employee_id, template: 'relations.kye.reminder', title: `Please review your profile: ${k.title}`, body: `Confirm your details by ${bsLabel(k.due_date)}.`, payload: { campaignId: k.id } }); out.kyeReminders++; }
    }
  }

  const closable = await db.query<{ id: string; company_id: string; decided_at: Date }>(
    `SELECT g.id, g.company_id, g.decided_at FROM grievance g WHERE g.status = 'decided' AND g.decided_at IS NOT NULL`);
  for (const g of closable.rows) {
    const p = await relationsPolicy(db, g.company_id);
    if (now.getTime() - g.decided_at.getTime() > p.appealDays * 86_400_000) {
      await db.query(`UPDATE grievance SET status = 'closed', updated_at = now() WHERE id = $1 AND status = 'decided'`, [g.id]);
      out.grievancesClosed++;
    }
  }
  return out;
}

/** A suspension decided in a disciplinary case goes on the career timeline (it changes nothing permanent). */
export async function logSuspension(db: Queryable, input: { companyId: string; employeeId: string; from: string; to: string; stageName: string; actorId: string }): Promise<void> {
  await logCareerEvent(db, { companyId: input.companyId, employeeId: input.employeeId, kind: 'suspension', effectiveDate: input.from,
    details: { endDate: input.to, reason: input.stageName }, note: 'Recorded from a disciplinary case', actorId: input.actorId });
}

export const today = () => nptToday();
