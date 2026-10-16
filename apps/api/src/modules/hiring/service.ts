import { z } from 'zod';
import type { Db, Queryable } from '../../db/pool.js';
import { BadRequestError, ForbiddenError } from '../../errors.js';
import { deleteFile } from '../files/store.js';
import { notify, userIdForEmployee, userIdsWithRole } from '../notifications/service.js';
import { registerHandler, registerSummariser, type DecisionContext } from '../workflow/service.js';

// ---------------------------------------------------------------- types and validation

export const DEFAULT_STAGES = ['Applied', 'Screening', 'Interview', 'Offer', 'Hired'];
export const SOURCES = ['careers', 'referral', 'manual', 'job_board', 'other'] as const;

const rule = z.object({ equals: z.enum(['yes', 'no']).optional(), min: z.number().min(0).max(1_000_000).optional() }).strict();

export const questionSchema = z.object({
  id: z.string().regex(/^q[0-9a-z]{1,12}$/, 'question id like q1'),
  kind: z.enum(['yes_no', 'number', 'text']),
  label: z.string().trim().min(3).max(200),
  required: z.boolean().default(true),
  /** An answer that fails the knock-out rule marks the application as not meeting a must-have. */
  knockout: rule.optional(),
  /** An answer that meets the preferred rule earns points in the score. */
  preferred: rule.optional(),
});
export type JobQuestion = z.infer<typeof questionSchema>;

export const stagesSchema = z.array(z.string().trim().min(2).max(40)).min(2).max(10)
  .refine((s) => new Set(s.map((x) => x.toLowerCase())).size === s.length, 'stage names must be different')
  .refine((s) => s[0] === 'Applied', 'the first stage must be "Applied"')
  .refine((s) => s[s.length - 1] === 'Hired', 'the last stage must be "Hired"');

export type Answer = string | number | boolean;

export interface ScoreItem { label: string; points: number; max: number; note: string }
export interface ScoreResult { score: number | null; knockedOut: boolean; detail: ScoreItem[] }

function asYesNo(a: Answer | undefined): 'yes' | 'no' | null {
  if (a === true || a === 'yes') return 'yes';
  if (a === false || a === 'no') return 'no';
  return null;
}

function meets(q: JobQuestion, r: z.infer<typeof rule>, a: Answer | undefined): boolean {
  if (r.equals) return asYesNo(a) === r.equals;
  if (r.min !== undefined) { const n = typeof a === 'number' ? a : Number(a); return Number.isFinite(n) && n >= r.min; }
  return a !== undefined && String(a).trim() !== '';
}

/**
 * Transparent, rule-based screening score (0–100): experience against the job's range is worth 40 points and each
 * question with a preferred answer 20 points. Knock-out questions don't score; failing one flags the application.
 * Every point is explained so recruiters can see exactly why a candidate ranks where they do.
 */
export function scoreApplication(job: { experience_min: string | number | null; experience_max: string | number | null; questions: JobQuestion[] },
  input: { experienceYears: number | null; answers: Record<string, Answer> }): ScoreResult {
  const detail: ScoreItem[] = [];
  let knockedOut = false;
  const min = job.experience_min === null ? null : Number(job.experience_min);
  const max = job.experience_max === null ? null : Number(job.experience_max);
  if (min !== null || max !== null) {
    const y = input.experienceYears;
    let points = 0; let note: string;
    if (y === null) note = 'Experience not given';
    else if (min !== null && y < min) { points = min > 0 ? Math.round(40 * y / min) : 40; note = `${y} years, below the ${min} years asked for`; }
    else if (max !== null && y > max) { points = 30; note = `${y} years, more than the ${max} years in the range`; }
    else { points = 40; note = `${y} years, within the range`; }
    detail.push({ label: 'Experience', points, max: 40, note });
  }
  for (const q of job.questions) {
    const a = input.answers[q.id];
    if (q.knockout) {
      const ok = meets(q, q.knockout, a);
      if (!ok) knockedOut = true;
      detail.push({ label: q.label, points: 0, max: 0, note: ok ? 'Meets the must-have' : 'Does not meet the must-have' });
    }
    if (q.preferred) {
      const ok = meets(q, q.preferred, a);
      detail.push({ label: q.label, points: ok ? 20 : 0, max: 20, note: ok ? 'Preferred answer' : 'Not the preferred answer' });
    }
  }
  const total = detail.reduce((s, d) => s + d.max, 0);
  const earned = detail.reduce((s, d) => s + d.points, 0);
  return { score: total > 0 ? Math.round(100 * earned / total) : null, knockedOut, detail };
}

/** Check answers against the job's questions: required ones present, types right, unknown ids dropped. */
export function cleanAnswers(questions: JobQuestion[], raw: Record<string, unknown> | undefined): Record<string, Answer> {
  const out: Record<string, Answer> = {};
  const src = raw ?? {};
  for (const q of questions) {
    const v = src[q.id];
    const empty = v === undefined || v === null || (typeof v === 'string' && v.trim() === '');
    if (empty) { if (q.required) throw new BadRequestError('Please answer every required question.'); continue; }
    if (q.kind === 'yes_no') {
      const yn = asYesNo(v as Answer);
      if (!yn) throw new BadRequestError('Answer the yes or no questions with yes or no.');
      out[q.id] = yn;
    } else if (q.kind === 'number') {
      const n = typeof v === 'number' ? v : Number(v);
      if (!Number.isFinite(n) || n < 0 || n > 1_000_000) throw new BadRequestError('Number answers must be a number.');
      out[q.id] = n;
    } else {
      out[q.id] = String(v).trim().slice(0, 1000);
    }
  }
  return out;
}

// ---------------------------------------------------------------- timeline, e-mail, notifications

export async function addEvent(db: Queryable, e: { companyId: string; applicationId: string; actorId: string | null; kind: string; body?: string | null; data?: Record<string, unknown> }): Promise<void> {
  await db.query('INSERT INTO hiring_event (company_id, application_id, actor_id, kind, body, data) VALUES ($1, $2, $3, $4, $5, $6)',
    [e.companyId, e.applicationId, e.actorId, e.kind, e.body ?? null, JSON.stringify(e.data ?? {})]);
}

/** Queue an e-mail to a candidate through the outbox (they are not users, so the address rides on the row) and log it. */
export async function emailCandidate(db: Queryable, e: { companyId: string; applicationId: string; to: string | null; template: string; title: string; body: string; actorId: string | null }): Promise<boolean> {
  if (!e.to) return false;
  await db.query(
    `INSERT INTO notification_outbox (company_id, user_id, employee_id, channel, template, title, body, payload, status, to_email)
     VALUES ($1, NULL, NULL, 'email', $2, $3, $4, $5, 'queued', $6)`,
    [e.companyId, e.template, e.title, e.body, JSON.stringify({ applicationId: e.applicationId }), e.to]);
  await addEvent(db, { companyId: e.companyId, applicationId: e.applicationId, actorId: e.actorId, kind: 'email', body: e.title, data: { template: e.template } });
  return true;
}

export async function companyName(db: Queryable, companyId: string): Promise<string> {
  return (await db.query<{ name_en: string }>('SELECT name_en FROM company WHERE id = $1', [companyId])).rows[0]?.name_en ?? 'our company';
}

/** HR plus the job's hiring manager: who hears about new applications. */
export async function jobWatchers(db: Queryable, companyId: string, hiringManagerId: string | null): Promise<string[]> {
  const ids = new Set(await userIdsWithRole(db, companyId, 'hr'));
  if (hiringManagerId) { const u = await userIdForEmployee(db, hiringManagerId); if (u) ids.add(u); }
  return [...ids];
}

// ---------------------------------------------------------------- policy

export interface HiringPolicy { retentionDays: number; careersEnabled: boolean }

export async function hiringPolicy(db: Queryable, companyId: string): Promise<HiringPolicy> {
  const r = await db.query<{ value: Partial<HiringPolicy> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'hiring.policy'`, [companyId]);
  const v = r.rows[0]?.value ?? {};
  return { retentionDays: typeof v.retentionDays === 'number' ? v.retentionDays : 180, careersEnabled: v.careersEnabled !== false };
}

// ---------------------------------------------------------------- hire hook

export interface CandidateHired { companyId: string; employeeId: string; applicationId: string; jobId: string; joinDate: string; actorId: string }
export type HireHook = (db: Queryable, e: CandidateHired) => Promise<void>;
const hireHooks: HireHook[] = [];

/**
 * Other modules (onboarding, documents) subscribe here to act when a candidate becomes an employee. Hooks run inside
 * the hire transaction, so a failing hook rolls the hire back.
 */
export function onCandidateHired(fn: HireHook): void { hireHooks.push(fn); }
export async function runHireHooks(db: Queryable, e: CandidateHired): Promise<void> { for (const h of hireHooks) await h(db, e); }

// ---------------------------------------------------------------- workflow handlers

async function decideRequisition(db: Queryable, id: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ company_id: string; raised_by: string; title: string; status: string }>(
    'SELECT company_id, raised_by, title, status FROM hiring_requisition WHERE id = $1 FOR UPDATE', [id]);
  const q = r.rows[0];
  if (!q || q.status !== 'pending') return;
  if (state === 'approved' && ctx.actorId === q.raised_by) throw new ForbiddenError('you raised this hiring request; someone else must approve it');
  await db.query('UPDATE hiring_requisition SET status = $2, decided_at = now(), decision_reason = $3 WHERE id = $1', [id, state, ctx.reason]);
  await notify(db, {
    companyId: q.company_id, userId: q.raised_by, template: `job_requisition.${state}`,
    title: state === 'approved' ? 'Hiring request approved' : 'Hiring request rejected',
    body: `Your request to hire "${q.title}" was ${state}${ctx.reason ? `: ${ctx.reason}` : ''}.`, payload: { requisitionId: id },
  });
}

async function decideOffer(db: Queryable, id: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ company_id: string; created_by: string | null; status: string; application_id: string; name: string }>(
    `SELECT o.company_id, o.created_by, o.status, o.application_id, c.name FROM hiring_offer o
       JOIN hiring_application a ON a.id = o.application_id JOIN hiring_candidate c ON c.id = a.candidate_id WHERE o.id = $1 FOR UPDATE OF o`, [id]);
  const o = r.rows[0];
  if (!o || o.status !== 'pending_approval') return;
  if (state === 'approved' && ctx.actorId === o.created_by) throw new ForbiddenError('you drafted this offer; someone else must approve it');
  await db.query('UPDATE hiring_offer SET status = $2, decided_at = now() WHERE id = $1', [id, state]);
  await addEvent(db, { companyId: o.company_id, applicationId: o.application_id, actorId: ctx.actorId, kind: 'offer', body: `Offer ${state}${ctx.reason ? `: ${ctx.reason}` : ''}` });
  if (o.created_by) {
    await notify(db, {
      companyId: o.company_id, userId: o.created_by, template: `job_offer.${state}`,
      title: state === 'approved' ? 'Offer approved' : 'Offer rejected',
      body: `The offer for ${o.name} was ${state}${ctx.reason ? `: ${ctx.reason}` : ''}.`, payload: { offerId: id, applicationId: o.application_id },
    });
  }
}

registerHandler('job_requisition', decideRequisition);
registerHandler('job_offer', decideOffer);

registerSummariser('job_requisition', async (db, { object_id: id }) => {
  const r = await db.query(
    `SELECT q.title, q.designation, q.headcount, q.kind, q.salary_min, q.salary_max, q.justification, ou.name_en AS department
       FROM hiring_requisition q LEFT JOIN org_unit ou ON ou.id = q.org_unit_id WHERE q.id = $1`, [id]);
  return r.rows[0] ?? {};
});
registerSummariser('job_offer', async (db, { object_id: id }) => {
  const r = await db.query(
    `SELECT o.designation, o.salary, o.join_date, c.name AS candidate, j.title AS job
       FROM hiring_offer o JOIN hiring_application a ON a.id = o.application_id JOIN hiring_candidate c ON c.id = a.candidate_id
       JOIN hiring_job j ON j.id = a.job_id WHERE o.id = $1`, [id]);
  return r.rows[0] ?? {};
});

// ---------------------------------------------------------------- retention

/**
 * Daily: once a candidate has had no open application for the retention period (rejected or withdrawn), their name,
 * contact details, CVs, cover notes, answers and notes are removed. Counts and stage history stay for reports.
 */
export async function purgeRejectedCandidates(db: Db, now = new Date()): Promise<number> {
  const due = await db.query<{ id: string; company_id: string }>(
    `SELECT c.id, c.company_id FROM hiring_candidate c
       LEFT JOIN company_setting s ON s.company_id = c.company_id AND s.key = 'hiring.policy'
      WHERE c.purged_at IS NULL
        AND EXISTS (SELECT 1 FROM hiring_application a WHERE a.candidate_id = c.id)
        AND NOT EXISTS (SELECT 1 FROM hiring_application a WHERE a.candidate_id = c.id
                         AND (a.status NOT IN ('rejected', 'withdrawn')
                              OR coalesce(a.closed_at, a.created_at) > $1::timestamptz - make_interval(days => coalesce((s.value->>'retentionDays')::int, 180))))`, [now]);
  let n = 0;
  for (const c of due.rows) {
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const files = await client.query<{ cv_file_id: string | null; id: string }>('SELECT id, cv_file_id FROM hiring_application WHERE candidate_id = $1', [c.id]);
      for (const f of files.rows) if (f.cv_file_id) await deleteFile(client, c.company_id, f.cv_file_id);
      await client.query(`UPDATE hiring_candidate SET name = 'Removed candidate', email = NULL, phone = NULL, purged_at = $2 WHERE id = $1`, [c.id, now]);
      await client.query(`UPDATE hiring_application SET cv_file_id = NULL, cover_note = NULL, answers = '{}', current_employer = NULL, tags = '{}', score_detail = '[]' WHERE candidate_id = $1`, [c.id]);
      const appIds = files.rows.map((f) => f.id);
      await client.query(`UPDATE hiring_event SET body = NULL WHERE application_id = ANY($1::uuid[]) AND kind IN ('note', 'email', 'rejected', 'applied')`, [appIds]);
      await client.query(`UPDATE hiring_scorecard SET notes = NULL WHERE interview_id IN (SELECT id FROM hiring_interview WHERE application_id = ANY($1::uuid[]))`, [appIds]);
      await client.query(`UPDATE notification_outbox SET to_email = NULL, body = '' WHERE channel = 'email' AND payload->>'applicationId' = ANY($1::text[])`, [appIds]);
      await client.query('COMMIT');
      n++;
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  }
  return n;
}

/** Find the candidate by e-mail (case-insensitive) or create them; existing details are kept and gaps filled. */
export async function upsertCandidate(c: Queryable, companyId: string, name: string, email: string, phone: string | null): Promise<string> {
  const ins = await c.query<{ id: string }>(
    `INSERT INTO hiring_candidate (company_id, name, email, phone) VALUES ($1, $2, lower($3), $4)
     ON CONFLICT (company_id, lower(email)) WHERE email IS NOT NULL DO UPDATE SET phone = coalesce(hiring_candidate.phone, EXCLUDED.phone) RETURNING id`,
    [companyId, name, email, phone]);
  return ins.rows[0]!.id;
}
