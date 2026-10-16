import { randomUUID } from 'node:crypto';
import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { HttpError, NotFoundError } from '../../errors.js';
import { featureOn } from '../features/service.js';
import { parseUpload, saveFile } from '../files/store.js';
import { notify } from '../notifications/service.js';
import { nptToday } from '../workflow/service.js';
import { addEvent, cleanAnswers, emailCandidate, hiringPolicy, jobWatchers, scoreApplication, upsertCandidate, type JobQuestion } from '../hiring/service.js';

/** In-memory sliding window per client address: enough to blunt scripted floods on a single-server install. */
export class RateLimiter {
  private hits = new Map<string, number[]>();
  constructor(private readonly limit: number, private readonly windowMs: number) {}
  allow(key: string, now = Date.now()): boolean {
    const recent = (this.hits.get(key) ?? []).filter((t) => now - t < this.windowMs);
    if (recent.length >= this.limit) { this.hits.set(key, recent); return false; }
    recent.push(now);
    this.hits.set(key, recent);
    if (this.hits.size > 10_000) for (const [k, v] of this.hits) if (!v.some((t) => now - t < this.windowMs)) this.hits.delete(k);
    return true;
  }
  reset(): void { this.hits.clear(); }
}

export const applyLimiter = new RateLimiter(5, 60 * 60_000);

interface PublicCompany { id: string; code: string; name_en: string }

const PUBLIC_JOB_COLS = `j.id, j.title, j.description, j.requirements, j.category, j.location, j.work_mode, j.employment_type, j.experience_min, j.experience_max,
  CASE WHEN j.show_salary THEN j.salary_min END AS salary_min, CASE WHEN j.show_salary THEN j.salary_max END AS salary_max,
  j.openings, j.deadline, j.published_at, ou.name_en AS department`;

const OPEN = `j.status = 'open' AND (j.deadline IS NULL OR j.deadline >= $2::date)`;

const applyBody = z.object({
  name: z.string().trim().min(2).max(120),
  email: z.email().max(200),
  phone: z.string().trim().regex(/^[0-9+\- ()]{6,30}$/, 'enter a valid phone number'),
  experienceYears: z.number().min(0).max(60).nullable().optional(),
  currentEmployer: z.string().trim().max(120).optional().nullable(),
  coverNote: z.string().trim().max(4000).optional().nullable(),
  answers: z.record(z.string().max(20), z.union([z.string().max(1000), z.number(), z.boolean()])).optional(),
  cvFilename: z.string().trim().min(1).max(200),
  cvDataUrl: z.string().min(20).max(6_000_000),
  consent: z.literal(true, { message: 'please agree to the processing of your data' }),
  source: z.enum(['careers', 'job_board', 'referral', 'other']).default('careers'),
  /** Honeypot: hidden from people, filled in by bots. */
  website: z.string().max(200).optional(),
}).strict();

/** Only questions' public parts go out; knock-out and preferred answers stay private. */
const publicQuestions = (qs: JobQuestion[]) => qs.map((q) => ({ id: q.id, kind: q.kind, label: q.label, required: q.required }));

/**
 * Public careers site (no session): open jobs and job applications. A company is chosen by its code (?company=CODE);
 * an install with a single company needs no code. Nothing but open jobs' public fields ever leaves this router.
 */
export function careersRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  const resolveCompany = async (code: string | undefined): Promise<PublicCompany> => {
    const rows = code
      ? await db.query<PublicCompany>('SELECT id, code, name_en FROM company WHERE lower(code) = lower($1)', [code])
      : await db.query<PublicCompany>('SELECT id, code, name_en FROM company ORDER BY created_at LIMIT 2');
    if (rows.rows.length !== 1) throw new NotFoundError('careers page not found');
    const c = rows.rows[0]!;
    if (!(await hiringPolicy(db, c.id)).careersEnabled || !(await featureOn(db, c.id, 'hiring'))) throw new NotFoundError('careers page not found');
    return c;
  };

  const openJob = async (q: Queryable, jobId: string) => {
    const j = await q.query<{ id: string; company_id: string; title: string; questions: JobQuestion[]; stages: string[]; experience_min: string | null; experience_max: string | null; hiring_manager_id: string | null }>(
      `SELECT j.id, j.company_id, j.title, j.questions, j.stages, j.experience_min, j.experience_max, j.hiring_manager_id FROM hiring_job j WHERE j.id = $1 AND ${OPEN}`, [jobId, nptToday()]);
    const job = j.rows[0];
    if (!job || !(await hiringPolicy(q, job.company_id)).careersEnabled || !(await featureOn(q, job.company_id, 'hiring'))) throw new NotFoundError('This job is no longer accepting applications.');
    return job;
  };

  r.get('/jobs', async (req, res) => {
    const q = z.object({
      company: z.string().max(32).optional(), q: z.string().trim().max(100).optional(), workMode: z.enum(['onsite', 'remote', 'hybrid']).optional(),
      category: z.string().max(60).optional(), experience: z.coerce.number().min(0).max(60).optional(),
    }).parse(req.query);
    const company = await resolveCompany(q.company);
    const rows = await db.query(
      `SELECT ${PUBLIC_JOB_COLS} FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id
        WHERE j.company_id = $1 AND ${OPEN}
          AND ($3::text IS NULL OR j.work_mode = $3) AND ($4::text IS NULL OR j.category = $4)
          AND ($5::numeric IS NULL OR j.experience_min IS NULL OR j.experience_min <= $5)
          AND ($6::text IS NULL OR j.title ILIKE '%' || $6 || '%' OR j.description ILIKE '%' || $6 || '%')
        ORDER BY j.published_at DESC NULLS LAST LIMIT 200`,
      [company.id, nptToday(), q.workMode ?? null, q.category ?? null, q.experience ?? null, q.q ? q.q.replace(/[\\%_]/g, (c) => `\\${c}`) : null]);
    const cats = await db.query<{ category: string }>(
      `SELECT DISTINCT j.category FROM hiring_job j WHERE j.company_id = $1 AND ${OPEN} AND j.category IS NOT NULL ORDER BY 1`, [company.id, nptToday()]);
    res.setHeader('Cache-Control', 'no-store');
    res.json({ company: { name: company.name_en, code: company.code }, jobs: rows.rows, categories: cats.rows.map((x) => x.category) });
  });

  r.get('/jobs/:id', async (req, res) => {
    const id = z.uuid().safeParse(req.params.id);
    if (!id.success) throw new NotFoundError('This job is no longer accepting applications.');
    const job = await openJob(db, id.data);
    const row = await db.query(`SELECT ${PUBLIC_JOB_COLS}, co.name_en AS company_name, co.code AS company_code FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id
      JOIN company co ON co.id = j.company_id WHERE j.id = $1`, [job.id]);
    const { company_name, company_code, ...pub } = row.rows[0] as Record<string, unknown>;
    res.setHeader('Cache-Control', 'no-store');
    res.json({ company: { name: company_name, code: company_code }, job: { ...pub, questions: publicQuestions(job.questions) } });
  });

  r.post('/jobs/:id/apply', async (req: Request, res) => {
    const ok = { ok: true, message: 'Thank you. Your application has been received.' };
    if (!applyLimiter.allow(req.ip ?? 'unknown')) throw new HttpError(429, 'Too many applications from this connection. Please try again later.');
    const id = z.uuid().safeParse(req.params.id);
    if (!id.success) throw new NotFoundError('This job is no longer accepting applications.');
    const b = applyBody.parse(req.body);
    if (b.website && b.website.trim() !== '') return res.status(201).json(ok);   // a bot filled the hidden field: pretend, store nothing
    const job = await openJob(db, id.data);
    const answers = cleanAnswers(job.questions, b.answers);
    const cv = parseUpload(b.cvDataUrl, { allowed: ['application/pdf'] });
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const cand = await upsertCandidate(client, job.company_id, b.name, b.email, b.phone);
      const dup = await client.query('SELECT 1 FROM hiring_application WHERE job_id = $1 AND candidate_id = $2', [job.id, cand]);
      // Same answer either way, so the form never reveals whether an address has applied before.
      if (dup.rows.length) { await client.query('ROLLBACK'); return res.status(201).json(ok); }
      const appId = randomUUID();
      const file = await saveFile(client, cipher, { companyId: job.company_id, purpose: 'hiring_cv', filename: b.cvFilename, file: cv, uploadedBy: null, attachedTo: `hiring_application:${appId}` });
      const score = scoreApplication(job, { experienceYears: b.experienceYears ?? null, answers });
      await client.query(
        `INSERT INTO hiring_application (id, company_id, job_id, candidate_id, stage, source, cv_file_id, cover_note, answers, experience_years, current_employer, score, score_detail, knocked_out, consent_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())`,
        [appId, job.company_id, job.id, cand, job.stages[0], b.source, file.id, b.coverNote ?? null, JSON.stringify(answers), b.experienceYears ?? null,
          b.currentEmployer ?? null, score.score, JSON.stringify(score.detail), score.knockedOut]);
      await addEvent(client, { companyId: job.company_id, applicationId: appId, actorId: null, kind: 'applied', body: `Applied through the careers page (${b.source.replace('_', ' ')})` });
      const company = (await client.query<{ name_en: string }>('SELECT name_en FROM company WHERE id = $1', [job.company_id])).rows[0]!.name_en;
      await emailCandidate(client, {
        companyId: job.company_id, applicationId: appId, to: b.email.toLowerCase(), actorId: null, template: 'hiring.received',
        title: `We received your application for ${job.title}`,
        body: `Dear ${b.name},\n\nThank you for applying for the ${job.title} role at ${company}. Our team will review your application and contact you if your profile matches what we are looking for.\n\n${company}`,
      });
      for (const userId of await jobWatchers(client, job.company_id, job.hiring_manager_id)) {
        await notify(client, { companyId: job.company_id, userId, template: 'hiring.applied', title: `New application: ${job.title}`, body: `${b.name} applied for ${job.title}.`, payload: { applicationId: appId, jobId: job.id } });
      }
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK').catch(() => undefined); throw e; } finally { client.release(); }
    return res.status(201).json(ok);
  });

  return r;
}
