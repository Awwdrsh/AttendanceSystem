import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { hhmm, isoDate, money } from '../../validation.js';
import { companyIdOf, hasModule, hasRole, MODULE_ROLES, requireModule, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { attachFile, readFile, sendStoredFile } from '../files/store.js';
import { notify } from '../notifications/service.js';
import { cancelWorkflow, nptToday, startWorkflow } from '../workflow/service.js';
import { renderOfferLetter } from './offer-letter.js';
import {
  addEvent, cleanAnswers, companyName, DEFAULT_STAGES, emailCandidate, hiringPolicy, questionSchema, runHireHooks, scoreApplication,
  SOURCES, stagesSchema, upsertCandidate, type JobQuestion,
} from './service.js';

type Access = 'full' | 'panel';

export interface JobRow {
  id: string; company_id: string; requisition_id: string | null; title: string; status: string; stages: string[]; questions: JobQuestion[];
  experience_min: string | null; experience_max: string | null; org_unit_id: string | null; hiring_manager_id: string | null; created_by: string | null;
  location: string | null; employment_type: string; deadline: string | null;
}

interface AppRow {
  id: string; company_id: string; job_id: string; candidate_id: string; stage: string; status: string; cv_file_id: string | null;
  furthest_stage: number; created_at: Date; name: string; email: string | null; phone: string | null;
}

const optText = (max: number) => z.string().trim().max(max).optional().nullable();

const jobBody = z.object({
  requisitionId: z.uuid().nullable().optional(),
  title: z.string().trim().min(2).max(120),
  description: z.string().max(10_000).default(''),
  requirements: z.string().max(10_000).default(''),
  category: optText(60),
  location: optText(120),
  workMode: z.enum(['onsite', 'remote', 'hybrid']).default('onsite'),
  employmentType: z.enum(['full_time', 'part_time', 'contract', 'internship', 'temporary']).default('full_time'),
  experienceMin: z.number().min(0).max(60).nullable().optional(),
  experienceMax: z.number().min(0).max(60).nullable().optional(),
  salaryMin: money.nullable().optional(),
  salaryMax: money.nullable().optional(),
  showSalary: z.boolean().default(false),
  openings: z.number().int().min(1).max(500).default(1),
  deadline: isoDate.nullable().optional(),
  orgUnitId: z.uuid().nullable().optional(),
  branchId: z.uuid().nullable().optional(),
  hiringManagerId: z.uuid().nullable().optional(),
  stages: stagesSchema.optional(),
  questions: z.array(questionSchema).max(15).optional(),
});
type JobInput = z.infer<typeof jobBody>;

const JOB_COLS = `j.id, j.company_id, j.requisition_id, j.title, j.description, j.requirements, j.category, j.location, j.work_mode, j.employment_type,
  j.experience_min, j.experience_max, j.salary_min, j.salary_max, j.show_salary, j.openings, j.deadline, j.org_unit_id, j.branch_id, j.hiring_manager_id,
  j.stages, j.questions, j.status, j.published_at, j.closed_at, j.created_by, j.created_at, j.updated_at,
  ou.name_en AS department, hm.name_en AS hiring_manager_name`;

async function tx<T>(db: AppDeps['db'], fn: (c: Queryable) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    const out = await fn(client);
    await client.query('COMMIT');
    return out;
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
}

async function managerUnit(db: Queryable, employeeId: string | null | undefined): Promise<string | null> {
  if (!employeeId) return null;
  const r = await db.query<{ org_unit_id: string | null }>('SELECT org_unit_id FROM employee_assignment WHERE employee_id = $1 AND valid @> current_date', [employeeId]);
  return r.rows[0]?.org_unit_id ?? null;
}

/** HR sees every job; a manager sees the jobs they manage, created, or that belong to their department. */
async function canSeeJob(db: Queryable, user: AuthUser, job: Pick<JobRow, 'hiring_manager_id' | 'created_by' | 'org_unit_id'>): Promise<boolean> {
  if (hasModule(user, 'hiring')) return true;
  if (!hasRole(user, 'manager')) return false;
  if (job.created_by === user.id) return true;
  if (user.employeeId && job.hiring_manager_id === user.employeeId) return true;
  const unit = await managerUnit(db, user.employeeId);
  return !!unit && job.org_unit_id === unit;
}

async function checkRefs(db: Queryable, companyId: string, refs: Array<[string, string | null | undefined, string]>): Promise<void> {
  for (const [table, id, label] of refs) {
    if (!id) continue;
    const r = await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (!r.rows.length) throw new BadRequestError(`${label} does not exist in your company`);
  }
}

/** A Nepal wall-clock date and time as an instant. */
const npt = (date: string, time: string) => new Date(`${date}T${time}:00+05:45`);
const nptIso = (d: Date) => new Date(d.getTime() + 345 * 60_000).toISOString();

function validateJob(b: Partial<Pick<JobInput, 'experienceMin' | 'experienceMax' | 'salaryMin' | 'salaryMax' | 'questions'>>): void {
  if (b.experienceMin != null && b.experienceMax != null && b.experienceMax < b.experienceMin) throw new BadRequestError('the most experience cannot be below the least');
  if (b.salaryMin && b.salaryMax && Number(b.salaryMax) < Number(b.salaryMin)) throw new BadRequestError('the highest salary cannot be below the lowest');
  if (b.questions) {
    const ids = b.questions.map((q) => q.id);
    if (new Set(ids).size !== ids.length) throw new BadRequestError('each question needs a different id');
    for (const q of b.questions) {
      for (const rr of [q.knockout, q.preferred]) {
        if (!rr) continue;
        if (q.kind === 'text') throw new BadRequestError(`"${q.label}": text answers cannot be scored`);
        if (q.kind === 'yes_no' && !rr.equals) throw new BadRequestError(`"${q.label}": a yes or no rule needs a yes or no answer`);
        if (q.kind === 'number' && rr.min === undefined) throw new BadRequestError(`"${q.label}": a number rule needs a minimum`);
      }
    }
  }
}

export function hiringRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const staff = requireRole('manager', ...MODULE_ROLES.hiring);

  const loadJob = async (req: Request, id: string): Promise<JobRow> => {
    const j = await db.query<JobRow>('SELECT * FROM hiring_job WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    const job = j.rows[0];
    if (!job || !(await canSeeJob(db, req.user!, job))) throw new NotFoundError('job not found');
    return job;
  };

  /** An application with its job; HR and managers in scope get full access, interview panel members a read-only view. */
  const loadApp = async (q: Queryable, req: Request, id: string, opts: { allowPanel?: boolean; lock?: boolean } = {}): Promise<{ app: AppRow; job: JobRow; access: Access }> => {
    const a = await q.query<AppRow>(
      `SELECT a.id, a.company_id, a.job_id, a.candidate_id, a.stage, a.status, a.cv_file_id, a.furthest_stage, a.created_at, c.name, c.email, c.phone
         FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE a.id = $1 AND a.company_id = $2${opts.lock ? ' FOR UPDATE OF a' : ''}`,
      [id, companyIdOf(req)]);
    const app = a.rows[0];
    if (!app) throw new NotFoundError('application not found');
    const job = (await q.query<JobRow>('SELECT * FROM hiring_job WHERE id = $1', [app.job_id])).rows[0]!;
    if (await canSeeJob(q, req.user!, job)) return { app, job, access: 'full' };
    if (opts.allowPanel) {
      const p = await q.query('SELECT 1 FROM hiring_interview WHERE application_id = $1 AND $2 = ANY(panel) LIMIT 1', [id, req.user!.id]);
      if (p.rows.length) return { app, job, access: 'panel' };
    }
    throw new NotFoundError('application not found');
  };

  const moveTo = async (c: Queryable, app: AppRow, job: JobRow, stage: string, actorId: string) => {
    const idx = job.stages.indexOf(stage);
    if (idx <= 0 || job.stages.indexOf(app.stage) >= idx) return;
    await c.query('UPDATE hiring_application SET stage = $2, furthest_stage = greatest(furthest_stage, $3), stage_changed_at = now() WHERE id = $1', [app.id, stage, idx]);
    await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId, kind: 'stage', body: `${app.stage} → ${stage}`, data: { from: app.stage, to: stage } });
  };

  // ------------------------------------------------------------ requisitions

  r.get('/requisitions', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const hr = hasModule(req.user!, 'hiring');
    const unit = hr ? null : await managerUnit(db, req.user!.employeeId);
    const rows = await db.query(
      `SELECT q.*, ou.name_en AS department, b.name_en AS branch, u.name AS raised_by_name, re.name_en AS replacing_name,
              (SELECT count(*)::int FROM hiring_job j WHERE j.requisition_id = q.id) AS jobs
         FROM hiring_requisition q LEFT JOIN org_unit ou ON ou.id = q.org_unit_id LEFT JOIN branch b ON b.id = q.branch_id
         LEFT JOIN app_user u ON u.id = q.raised_by LEFT JOIN employee re ON re.id = q.replacing_employee_id
        WHERE q.company_id = $1 AND ($2 OR q.raised_by = $3 OR (q.org_unit_id IS NOT NULL AND q.org_unit_id = $4::uuid))
        ORDER BY q.created_at DESC LIMIT 500`, [companyId, hr, req.user!.id, unit]);
    res.json({ requisitions: rows.rows });
  });

  r.post('/requisitions', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      title: z.string().trim().min(2).max(120), designation: optText(120), orgUnitId: z.uuid().nullable().optional(), branchId: z.uuid().nullable().optional(),
      headcount: z.number().int().min(1).max(500).default(1), salaryMin: money.nullable().optional(), salaryMax: money.nullable().optional(),
      kind: z.enum(['new', 'replacement']).default('new'), replacingEmployeeId: z.uuid().nullable().optional(), justification: z.string().trim().min(10).max(4000),
    }).parse(req.body);
    if (b.salaryMin && b.salaryMax && Number(b.salaryMax) < Number(b.salaryMin)) throw new BadRequestError('the highest salary cannot be below the lowest');
    if (b.kind === 'replacement' && !b.replacingEmployeeId) throw new BadRequestError('choose the employee being replaced');
    await checkRefs(db, companyId, [['org_unit', b.orgUnitId, 'department'], ['branch', b.branchId, 'branch'], ['employee', b.replacingEmployeeId, 'employee']]);
    const id = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO hiring_requisition (company_id, raised_by, raised_by_employee_id, title, designation, org_unit_id, branch_id, headcount, salary_min, salary_max, kind, replacing_employee_id, justification)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id`,
        [companyId, req.user!.id, req.user!.employeeId ?? null, b.title, b.designation ?? null, b.orgUnitId ?? null, b.branchId ?? null, b.headcount,
          b.salaryMin ?? null, b.salaryMax ?? null, b.kind, b.kind === 'replacement' ? b.replacingEmployeeId : null, b.justification]);
      const reqId = ins.rows[0]!.id;
      await startWorkflow(c, {
        companyId, objectType: 'job_requisition', objectId: reqId, employeeId: req.user!.employeeId ?? null,
        title: 'Hiring request', body: `${req.user!.name} asks to hire ${b.headcount} × ${b.title}.`,
      });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'hiring_requisition', rowId: reqId, after: b });
      return reqId;
    });
    res.status(201).json({ id });
  });

  r.post('/requisitions/:id/cancel', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    await tx(db, async (c) => {
      const q = (await c.query<{ raised_by: string; status: string }>('SELECT raised_by, status FROM hiring_requisition WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId])).rows[0];
      if (!q) throw new NotFoundError('request not found');
      if (q.raised_by !== req.user!.id && !hasModule(req.user!, 'hiring')) throw new NotFoundError('request not found');
      if (!['pending', 'approved'].includes(q.status)) throw new ConflictError(`this request is already ${q.status}`);
      await c.query(`UPDATE hiring_requisition SET status = 'cancelled', decided_at = coalesce(decided_at, now()) WHERE id = $1`, [id]);
      await cancelWorkflow(c, 'job_requisition', id);
      await audit(c, { actorId: req.user!.id, action: 'cancel', table: 'hiring_requisition', rowId: id });
    });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ jobs

  r.get('/jobs', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ status: z.enum(['draft', 'open', 'closed']).optional() }).parse(req.query);
    const hr = hasModule(req.user!, 'hiring');
    const unit = hr ? null : await managerUnit(db, req.user!.employeeId);
    const rows = await db.query(
      `SELECT ${JOB_COLS},
              (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id) AS applications,
              (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.status = 'active') AS active,
              (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.created_at > now() - interval '7 days') AS new_this_week,
              (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.status = 'hired') AS hired
         FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id LEFT JOIN employee hm ON hm.id = j.hiring_manager_id
        WHERE j.company_id = $1 AND ($2::text IS NULL OR j.status = $2)
          AND ($3 OR j.created_by = $4 OR j.hiring_manager_id = $5::uuid OR (j.org_unit_id IS NOT NULL AND j.org_unit_id = $6::uuid))
        ORDER BY (j.status = 'open') DESC, j.created_at DESC LIMIT 500`,
      [companyId, q.status ?? null, hr, req.user!.id, req.user!.employeeId ?? null, unit]);
    const code = (await db.query<{ code: string }>('SELECT code FROM company WHERE id = $1', [companyId])).rows[0]?.code ?? null;
    res.json({ jobs: rows.rows, companyCode: code });
  });

  r.post('/jobs', requireModule('hiring'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = jobBody.parse(req.body);
    validateJob(b);
    await checkRefs(db, companyId, [['org_unit', b.orgUnitId, 'department'], ['branch', b.branchId, 'branch'], ['employee', b.hiringManagerId, 'hiring manager']]);
    if (b.requisitionId) {
      const q = await db.query<{ status: string }>('SELECT status FROM hiring_requisition WHERE id = $1 AND company_id = $2', [b.requisitionId, companyId]);
      if (!q.rows[0]) throw new BadRequestError('hiring request does not exist in your company');
      if (q.rows[0].status !== 'approved') throw new BadRequestError('only an approved hiring request can be turned into a job');
    }
    const id = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO hiring_job (company_id, requisition_id, title, description, requirements, category, location, work_mode, employment_type, experience_min, experience_max,
           salary_min, salary_max, show_salary, openings, deadline, org_unit_id, branch_id, hiring_manager_id, stages, questions, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22) RETURNING id`,
        [companyId, b.requisitionId ?? null, b.title, b.description, b.requirements, b.category ?? null, b.location ?? null, b.workMode, b.employmentType,
          b.experienceMin ?? null, b.experienceMax ?? null, b.salaryMin ?? null, b.salaryMax ?? null, b.showSalary, b.openings, b.deadline ?? null,
          b.orgUnitId ?? null, b.branchId ?? null, b.hiringManagerId ?? null, JSON.stringify(b.stages ?? DEFAULT_STAGES), JSON.stringify(b.questions ?? []), req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'hiring_job', rowId: ins.rows[0]!.id, after: { title: b.title } });
      return ins.rows[0]!.id;
    });
    res.status(201).json({ id });
  });

  r.get('/jobs/:id', staff, async (req, res) => {
    const job = await loadJob(req, z.uuid().parse(req.params.id));
    const full = await db.query(`SELECT ${JOB_COLS} FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id LEFT JOIN employee hm ON hm.id = j.hiring_manager_id WHERE j.id = $1`, [job.id]);
    const counts = await db.query<{ stage: string; n: number }>(`SELECT stage, count(*)::int AS n FROM hiring_application WHERE job_id = $1 AND status = 'active' GROUP BY stage`, [job.id]);
    res.json({ job: full.rows[0], stageCounts: Object.fromEntries(counts.rows.map((x) => [x.stage, x.n])) });
  });

  r.patch('/jobs/:id', requireModule('hiring'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const raw = jobBody.partial().parse(req.body);
    // Only fields actually sent are changed (zod defaults must not overwrite stored values).
    const sent = new Set(Object.keys(req.body ?? {}));
    const b = Object.fromEntries(Object.entries(raw).filter(([k]) => sent.has(k))) as Partial<JobInput>;
    const job = await loadJob(req, id);
    validateJob({ experienceMin: job.experience_min === null ? null : Number(job.experience_min), experienceMax: job.experience_max === null ? null : Number(job.experience_max), ...b });
    await checkRefs(db, companyId, [['org_unit', b.orgUnitId, 'department'], ['branch', b.branchId, 'branch'], ['employee', b.hiringManagerId, 'hiring manager']]);
    if (b.requisitionId !== undefined) throw new BadRequestError('the hiring request of a job cannot be changed');
    if (b.stages) {
      const used = await db.query<{ stage: string }>(`SELECT DISTINCT stage FROM hiring_application WHERE job_id = $1`, [id]);
      const missing = used.rows.map((x) => x.stage).filter((s) => !b.stages!.includes(s));
      if (missing.length) throw new ConflictError(`candidates are in: ${missing.join(', ')}. Move them before removing the stage.`);
    }
    const cols: Record<string, string> = {
      title: 'title', description: 'description', requirements: 'requirements', category: 'category', location: 'location', workMode: 'work_mode',
      employmentType: 'employment_type', experienceMin: 'experience_min', experienceMax: 'experience_max', salaryMin: 'salary_min', salaryMax: 'salary_max',
      showSalary: 'show_salary', openings: 'openings', deadline: 'deadline', orgUnitId: 'org_unit_id', branchId: 'branch_id', hiringManagerId: 'hiring_manager_id',
      stages: 'stages', questions: 'questions',
    };
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, v] of Object.entries(b)) {
      const col = cols[k];
      if (!col) continue;
      vals.push(k === 'stages' || k === 'questions' ? JSON.stringify(v) : v ?? null);
      sets.push(`${col} = $${vals.length}`);
    }
    if (sets.length) {
      await db.query(`UPDATE hiring_job SET ${sets.join(', ')}, updated_at = now() WHERE id = $1`, vals);
      await audit(db, { actorId: req.user!.id, action: 'update', table: 'hiring_job', rowId: id, after: b });
    }
    res.json({ ok: true });
  });

  r.post('/jobs/:id/publish', requireModule('hiring'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const job = await loadJob(req, id);
    if (job.status === 'open') return res.json({ ok: true });
    if (job.deadline && job.deadline < nptToday()) throw new BadRequestError('the application deadline has passed; set a new deadline first');
    const full = (await db.query<{ description: string }>('SELECT description FROM hiring_job WHERE id = $1', [id])).rows[0]!;
    if (full.description.trim().length < 20) throw new BadRequestError('add a job description (at least 20 characters) before publishing');
    await db.query(`UPDATE hiring_job SET status = 'open', published_at = coalesce(published_at, now()), closed_at = NULL, updated_at = now() WHERE id = $1`, [id]);
    await audit(db, { actorId: req.user!.id, action: 'publish', table: 'hiring_job', rowId: id });
    return res.json({ ok: true });
  });

  r.post('/jobs/:id/close', requireModule('hiring'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    await loadJob(req, id);
    await db.query(`UPDATE hiring_job SET status = 'closed', closed_at = now(), updated_at = now() WHERE id = $1`, [id]);
    await audit(db, { actorId: req.user!.id, action: 'close', table: 'hiring_job', rowId: id });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ applications

  r.get('/jobs/:id/applications', staff, async (req, res) => {
    const job = await loadJob(req, z.uuid().parse(req.params.id));
    const q = z.object({ include: z.enum(['active', 'all']).default('active') }).parse(req.query);
    const rows = await db.query(
      `SELECT a.id, a.stage, a.status, a.source, a.score, a.knocked_out, a.rating, a.tags, a.experience_years, a.current_employer, a.created_at, a.stage_changed_at,
              a.rejected_reason, a.owner_user_id, ou.name AS owner_name, c.id AS candidate_id, c.name, c.email, c.phone, (a.cv_file_id IS NOT NULL) AS has_cv,
              (SELECT count(*)::int FROM hiring_application o WHERE o.candidate_id = a.candidate_id AND o.id <> a.id) AS other_applications,
              (SELECT min(i.starts_at) FROM hiring_interview i WHERE i.application_id = a.id AND i.status = 'scheduled' AND i.starts_at > now()) AS next_interview
         FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id LEFT JOIN app_user ou ON ou.id = a.owner_user_id
        WHERE a.job_id = $1 AND ($2 = 'all' OR a.status = 'active')
        ORDER BY a.knocked_out, a.score DESC NULLS LAST, a.created_at`, [job.id, q.include]);
    res.json({ applications: rows.rows, stages: job.stages });
  });

  r.post('/jobs/:id/applications', requireModule('hiring'), async (req, res) => {
    const companyId = companyIdOf(req);
    const job = await loadJob(req, z.uuid().parse(req.params.id));
    const b = z.object({
      name: z.string().trim().min(2).max(120), email: z.email().max(200), phone: z.string().trim().regex(/^[0-9+\- ()]{6,30}$/, 'phone number').optional().nullable(),
      source: z.enum(SOURCES).default('manual'), cvFileId: z.uuid().optional().nullable(), coverNote: optText(4000),
      experienceYears: z.number().min(0).max(60).nullable().optional(), currentEmployer: optText(120), answers: z.record(z.string(), z.unknown()).optional(),
    }).parse(req.body);
    const answers = cleanAnswers(job.questions, b.answers);
    const id = await tx(db, async (c) => {
      const cand = await upsertCandidate(c, companyId, b.name, b.email, b.phone ?? null);
      const dup = await c.query('SELECT 1 FROM hiring_application WHERE job_id = $1 AND candidate_id = $2', [job.id, cand]);
      if (dup.rows.length) throw new ConflictError('this candidate has already applied for this job');
      const score = scoreApplication(job, { experienceYears: b.experienceYears ?? null, answers });
      const ins = await c.query<{ id: string }>(
        `INSERT INTO hiring_application (company_id, job_id, candidate_id, stage, source, cover_note, answers, experience_years, current_employer, score, score_detail, knocked_out, owner_user_id)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id`,
        [companyId, job.id, cand, job.stages[0], b.source, b.coverNote ?? null, JSON.stringify(answers), b.experienceYears ?? null, b.currentEmployer ?? null,
          score.score, JSON.stringify(score.detail), score.knockedOut, req.user!.id]);
      const appId = ins.rows[0]!.id;
      if (b.cvFileId) {
        await attachFile(c, { companyId, fileId: b.cvFileId, userId: req.user!.id, purpose: 'hiring_cv', attachedTo: `hiring_application:${appId}` });
        await c.query('UPDATE hiring_application SET cv_file_id = $2 WHERE id = $1', [appId, b.cvFileId]);
      }
      await addEvent(c, { companyId, applicationId: appId, actorId: req.user!.id, kind: 'applied', body: `Added by ${req.user!.name} (${b.source.replace('_', ' ')})` });
      return appId;
    });
    res.status(201).json({ id });
  });

  r.get('/applications/:id', async (req, res) => {
    const { app, job, access } = await loadApp(db, req, z.uuid().parse(req.params.id), { allowPanel: true });
    const full = await db.query(
      `SELECT a.id, a.job_id, a.candidate_id, a.stage, a.status, a.source, a.cover_note, a.answers, a.experience_years, a.current_employer, a.score, a.score_detail,
              a.knocked_out, a.rating, a.tags, a.owner_user_id, a.rejected_reason, a.closed_at, a.hired_employee_id, a.consent_at, a.stage_changed_at, a.created_at,
              (a.cv_file_id IS NOT NULL) AS has_cv, c.name, c.email, c.phone, c.purged_at, ou.name AS owner_name
         FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id LEFT JOIN app_user ou ON ou.id = a.owner_user_id WHERE a.id = $1`, [app.id]);
    const others = access === 'full' ? (await db.query(
      `SELECT a.id, a.stage, a.status, a.created_at, j.title FROM hiring_application a JOIN hiring_job j ON j.id = a.job_id
        WHERE a.candidate_id = $1 AND a.id <> $2 ORDER BY a.created_at DESC`, [app.candidate_id, app.id])).rows : [];
    const events = access === 'full' ? (await db.query(
      `SELECT e.id, e.kind, e.body, e.data, e.created_at, u.name AS actor FROM hiring_event e LEFT JOIN app_user u ON u.id = e.actor_id
        WHERE e.application_id = $1 ORDER BY e.created_at DESC LIMIT 300`, [app.id])).rows : [];
    const interviews = await db.query<{ id: string }>(
      `SELECT i.id, i.title, i.starts_at, i.duration_min, i.mode, i.location, i.panel, i.status,
              (SELECT array_agg(u.name ORDER BY u.name) FROM app_user u WHERE u.id = ANY(i.panel)) AS panel_names
         FROM hiring_interview i WHERE i.application_id = $1 ORDER BY i.starts_at`, [app.id]);
    const cards = await db.query<{ interview_id: string; reviewer_id: string }>(
      `SELECT s.id, s.interview_id, s.reviewer_id, s.ratings, s.recommendation, s.notes, s.submitted_at, u.name AS reviewer
         FROM hiring_scorecard s JOIN app_user u ON u.id = s.reviewer_id WHERE s.interview_id = ANY($1::uuid[]) ORDER BY s.submitted_at`, [interviews.rows.map((i) => i.id)]);
    const scorecards = access === 'full' ? cards.rows : cards.rows.filter((c) => c.reviewer_id === req.user!.id);
    const offers = access === 'full' ? (await db.query(
      'SELECT id, designation, salary, join_date, expires_on, notes, status, created_at, decided_at, responded_at FROM hiring_offer WHERE application_id = $1 ORDER BY created_at DESC', [app.id])).rows : [];
    res.json({
      application: full.rows[0], access, otherApplications: others, events, interviews: interviews.rows, scorecards, offers,
      job: { id: job.id, title: job.title, stages: job.stages, questions: job.questions, status: job.status, requisitionId: job.requisition_id },
    });
  });

  r.get('/applications/:id/cv', async (req, res) => {
    const { app } = await loadApp(db, req, z.uuid().parse(req.params.id), { allowPanel: true });
    if (!app.cv_file_id) throw new NotFoundError('no CV on file');
    sendStoredFile(res, await readFile(db, cipher, app.company_id, app.cv_file_id), { download: req.query.download === '1' });
  });

  r.patch('/applications/:id', staff, async (req, res) => {
    const b = z.object({
      rating: z.number().int().min(1).max(5).nullable().optional(), tags: z.array(z.string().trim().min(1).max(30)).max(10).optional(),
      ownerUserId: z.uuid().nullable().optional(),
    }).parse(req.body);
    const { app } = await loadApp(db, req, z.uuid().parse(req.params.id));
    if (b.ownerUserId) {
      const u = await db.query(`SELECT 1 FROM app_user WHERE id = $1 AND company_id = $2 AND status = 'active'`, [b.ownerUserId, app.company_id]);
      if (!u.rows.length) throw new BadRequestError('owner must be an active user in your company');
    }
    await tx(db, async (c) => {
      if (b.rating !== undefined) {
        await c.query('UPDATE hiring_application SET rating = $2 WHERE id = $1', [app.id, b.rating]);
        await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'rating', body: b.rating ? `Rated ${b.rating} of 5` : 'Rating cleared' });
      }
      if (b.tags) await c.query('UPDATE hiring_application SET tags = $2 WHERE id = $1', [app.id, [...new Set(b.tags)]]);
      if (b.ownerUserId !== undefined) await c.query('UPDATE hiring_application SET owner_user_id = $2 WHERE id = $1', [app.id, b.ownerUserId]);
    });
    res.json({ ok: true });
  });

  r.post('/applications/:id/move', staff, async (req, res) => {
    const { stage } = z.object({ stage: z.string().trim().min(1).max(40) }).parse(req.body);
    await tx(db, async (c) => {
      const { app, job } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (app.status !== 'active') throw new ConflictError(`this application is ${app.status}`);
      const idx = job.stages.indexOf(stage);
      if (idx < 0) throw new BadRequestError('that stage is not part of this job');
      if (idx === job.stages.length - 1) throw new BadRequestError('use "Hire" to move a candidate to Hired once they accept an offer');
      if (stage === app.stage) return;
      await c.query('UPDATE hiring_application SET stage = $2, furthest_stage = greatest(furthest_stage, $3), stage_changed_at = now() WHERE id = $1', [app.id, stage, idx]);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'stage', body: `${app.stage} → ${stage}`, data: { from: app.stage, to: stage } });
    });
    res.json({ ok: true });
  });

  r.post('/applications/:id/reject', staff, async (req, res) => {
    const b = z.object({ reason: z.string().trim().min(3).max(500), notifyCandidate: z.boolean().default(true) }).parse(req.body);
    const emailed = await tx(db, async (c) => {
      const { app, job } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (app.status !== 'active') throw new ConflictError(`this application is already ${app.status}`);
      await c.query(`UPDATE hiring_application SET status = 'rejected', rejected_reason = $2, closed_at = now() WHERE id = $1`, [app.id, b.reason]);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'rejected', body: b.reason });
      const open = await c.query<{ id: string }>(`UPDATE hiring_offer SET status = 'withdrawn' WHERE application_id = $1 AND status IN ('pending_approval', 'approved', 'accepted') RETURNING id`, [app.id]);
      for (const o of open.rows) await cancelWorkflow(c, 'job_offer', o.id);
      if (!b.notifyCandidate) return false;
      const company = await companyName(c, app.company_id);
      return emailCandidate(c, {
        companyId: app.company_id, applicationId: app.id, to: app.email, actorId: req.user!.id, template: 'hiring.rejected',
        title: `Your application for ${job.title}`,
        body: `Dear ${app.name},\n\nThank you for your interest in the ${job.title} role at ${company} and for the time you put into your application. After careful consideration we will not be moving forward with your application this time.\n\nWe wish you every success.\n\n${company}`,
      });
    });
    res.json({ ok: true, emailed });
  });

  r.post('/applications/:id/reopen', requireModule('hiring'), async (req, res) => {
    await tx(db, async (c) => {
      const { app } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (!['rejected', 'withdrawn'].includes(app.status)) throw new ConflictError(`this application is ${app.status}`);
      const purged = await c.query<{ purged_at: Date | null }>('SELECT purged_at FROM hiring_candidate WHERE id = $1', [app.candidate_id]);
      if (purged.rows[0]?.purged_at) throw new ConflictError("this candidate's data has been removed");
      await c.query(`UPDATE hiring_application SET status = 'active', rejected_reason = NULL, closed_at = NULL WHERE id = $1`, [app.id]);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'reopened', body: 'Application reopened' });
    });
    res.json({ ok: true });
  });

  r.post('/applications/:id/notes', staff, async (req, res) => {
    const { body } = z.object({ body: z.string().trim().min(1).max(4000) }).parse(req.body);
    const { app } = await loadApp(db, req, z.uuid().parse(req.params.id));
    await addEvent(db, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'note', body });
    res.status(201).json({ ok: true });
  });

  /** Everything held about a candidate, for a data access request. HR only, audited. */
  r.get('/applications/:id/export', requireModule('hiring'), async (req, res) => {
    const { app } = await loadApp(db, req, z.uuid().parse(req.params.id));
    const cand = (await db.query('SELECT id, name, email, phone, created_at, purged_at FROM hiring_candidate WHERE id = $1', [app.candidate_id])).rows[0];
    const apps = (await db.query<{ id: string }>(
      `SELECT a.id, j.title AS job, a.stage, a.status, a.source, a.cover_note, a.answers, a.experience_years, a.current_employer, a.score, a.score_detail,
              a.rating, a.tags, a.rejected_reason, a.created_at, a.closed_at, a.consent_at
         FROM hiring_application a JOIN hiring_job j ON j.id = a.job_id WHERE a.candidate_id = $1 ORDER BY a.created_at`, [app.candidate_id])).rows;
    const ids = apps.map((a) => a.id);
    const events = (await db.query('SELECT application_id, kind, body, created_at FROM hiring_event WHERE application_id = ANY($1::uuid[]) ORDER BY created_at', [ids])).rows;
    const interviews = (await db.query('SELECT application_id, title, starts_at, mode, status FROM hiring_interview WHERE application_id = ANY($1::uuid[])', [ids])).rows;
    const offers = (await db.query('SELECT application_id, designation, salary, join_date, status, created_at FROM hiring_offer WHERE application_id = ANY($1::uuid[])', [ids])).rows;
    await audit(db, { actorId: req.user!.id, action: 'export', table: 'hiring_candidate', rowId: app.candidate_id });
    res.setHeader('Content-Disposition', `attachment; filename="candidate-${app.candidate_id}.json"`);
    res.json({ exportedAt: new Date().toISOString(), candidate: cand, applications: apps, events, interviews, offers });
  });

  // ------------------------------------------------------------ interviews

  const interviewBody = z.object({
    title: z.string().trim().min(2).max(80).default('Interview'), date: isoDate, time: hhmm, durationMin: z.number().int().min(10).max(480).default(45),
    mode: z.enum(['in_person', 'phone', 'video']), location: optText(300), panel: z.array(z.uuid()).min(1).max(10), notifyCandidate: z.boolean().default(true),
  });

  const checkPanel = async (c: Queryable, companyId: string, panel: string[]): Promise<string[]> => {
    const ids = [...new Set(panel)];
    const u = await c.query(`SELECT id FROM app_user WHERE id = ANY($1::uuid[]) AND company_id = $2 AND status = 'active'`, [ids, companyId]);
    if (u.rows.length !== ids.length) throw new BadRequestError('every panel member must be an active user in your company');
    return ids;
  };

  r.post('/applications/:id/interviews', staff, async (req, res) => {
    const b = interviewBody.parse(req.body);
    const startsAt = npt(b.date, b.time);
    if (startsAt.getTime() < Date.now() - 60 * 60_000) throw new BadRequestError('the interview time is in the past');
    const id = await tx(db, async (c) => {
      const { app, job } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (app.status !== 'active') throw new ConflictError(`this application is ${app.status}`);
      const panel = await checkPanel(c, app.company_id, b.panel);
      const ins = await c.query<{ id: string }>(
        `INSERT INTO hiring_interview (company_id, application_id, title, starts_at, duration_min, mode, location, panel, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
        [app.company_id, app.id, b.title, startsAt, b.durationMin, b.mode, b.location ?? null, panel, req.user!.id]);
      const interviewId = ins.rows[0]!.id;
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'interview', body: `${b.title} scheduled`, data: { interviewId, startsAt } });
      await moveTo(c, app, job, 'Interview', req.user!.id);
      for (const userId of panel) {
        await notify(c, {
          companyId: app.company_id, userId, template: 'hiring.interview', title: `Interview: ${app.name}`,
          body: `You are on the interview panel for ${app.name} (${job.title}).`, payload: { applicationId: app.id, interviewId, startsAt: startsAt.toISOString() }, channels: ['push'],
        });
      }
      if (b.notifyCandidate) {
        const company = await companyName(c, app.company_id);
        const how = b.mode === 'video' ? `by video${b.location ? ` (${b.location})` : ''}` : b.mode === 'phone' ? 'by phone' : `in person${b.location ? ` at ${b.location}` : ''}`;
        await emailCandidate(c, {
          companyId: app.company_id, applicationId: app.id, to: app.email, actorId: req.user!.id, template: 'hiring.interview_invite',
          title: `Interview for ${job.title} at ${company}`,
          body: `Dear ${app.name},\n\nWe would like to invite you to an interview for the ${job.title} role on ${bsLabel(b.date)} (${b.date}) at ${b.time} Nepal time, ${how}. It will take about ${b.durationMin} minutes.\n\nPlease reply to let us know if this time does not work for you.\n\n${company}`,
        });
      }
      return interviewId;
    });
    res.status(201).json({ id });
  });

  r.get('/interviews', async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ scope: z.enum(['mine', 'all']).default('mine'), from: isoDate.optional(), to: isoDate.optional() }).parse(req.query);
    if (q.scope === 'all' && !hasRole(req.user!, 'manager', ...MODULE_ROLES.hiring)) throw new ForbiddenError('only managers and HR can see all interviews');
    const hr = hasModule(req.user!, 'hiring');
    const unit = hr ? null : await managerUnit(db, req.user!.employeeId);
    const rows = await db.query(
      `SELECT i.id, i.application_id, i.title, i.starts_at, i.duration_min, i.mode, i.location, i.status, i.panel,
              (SELECT array_agg(u.name ORDER BY u.name) FROM app_user u WHERE u.id = ANY(i.panel)) AS panel_names,
              c.name AS candidate, j.title AS job, j.id AS job_id, ($2 = ANY(i.panel)) AS on_panel,
              EXISTS (SELECT 1 FROM hiring_scorecard s WHERE s.interview_id = i.id AND s.reviewer_id = $2) AS my_scorecard,
              (SELECT count(*)::int FROM hiring_scorecard s WHERE s.interview_id = i.id) AS scorecards
         FROM hiring_interview i JOIN hiring_application a ON a.id = i.application_id JOIN hiring_candidate c ON c.id = a.candidate_id JOIN hiring_job j ON j.id = a.job_id
        WHERE i.company_id = $1
          AND ($3::date IS NULL OR i.starts_at >= ($3::date)::timestamp AT TIME ZONE 'Asia/Kathmandu')
          AND ($4::date IS NULL OR i.starts_at < ($4::date + 1)::timestamp AT TIME ZONE 'Asia/Kathmandu')
          AND (CASE WHEN $5 = 'mine' THEN $2 = ANY(i.panel)
                    ELSE ($6 OR $2 = ANY(i.panel) OR j.created_by = $2 OR j.hiring_manager_id = $7::uuid OR (j.org_unit_id IS NOT NULL AND j.org_unit_id = $8::uuid)) END)
        ORDER BY i.starts_at LIMIT 500`, [companyId, req.user!.id, q.from ?? null, q.to ?? null, q.scope, hr, req.user!.employeeId ?? null, unit]);
    res.json({ interviews: rows.rows });
  });

  r.patch('/interviews/:id', staff, async (req, res) => {
    const b = z.object({
      status: z.enum(['scheduled', 'completed', 'cancelled']).optional(), date: isoDate.optional(), time: hhmm.optional(),
      location: optText(300), panel: z.array(z.uuid()).min(1).max(10).optional(),
    }).parse(req.body);
    const id = z.uuid().parse(req.params.id);
    await tx(db, async (c) => {
      const i = (await c.query<{ application_id: string; starts_at: Date }>('SELECT application_id, starts_at FROM hiring_interview WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyIdOf(req)])).rows[0];
      if (!i) throw new NotFoundError('interview not found');
      const { app } = await loadApp(c, req, i.application_id);
      const sets: string[] = []; const vals: unknown[] = [id];
      if (b.status) { vals.push(b.status); sets.push(`status = $${vals.length}`); }
      if (b.date || b.time) {
        const cur = nptIso(i.starts_at);
        vals.push(npt(b.date ?? cur.slice(0, 10), b.time ?? cur.slice(11, 16))); sets.push(`starts_at = $${vals.length}`);
      }
      if (b.location !== undefined) { vals.push(b.location); sets.push(`location = $${vals.length}`); }
      if (b.panel) { vals.push(await checkPanel(c, app.company_id, b.panel)); sets.push(`panel = $${vals.length}`); }
      if (!sets.length) return;
      await c.query(`UPDATE hiring_interview SET ${sets.join(', ')} WHERE id = $1`, vals);
      await addEvent(c, {
        companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'interview', data: { interviewId: id },
        body: b.status === 'cancelled' ? 'Interview cancelled' : b.status === 'completed' ? 'Interview completed' : 'Interview updated',
      });
    });
    res.json({ ok: true });
  });

  /** Panel members score the interview they sat on; one scorecard each, editable until the candidate is hired. */
  r.post('/interviews/:id/scorecard', async (req, res) => {
    const b = z.object({
      ratings: z.record(z.string().trim().min(2).max(40), z.number().int().min(1).max(5))
        .refine((x) => Object.keys(x).length >= 1 && Object.keys(x).length <= 10, 'rate between 1 and 10 criteria'),
      recommendation: z.enum(['strong_no', 'no', 'yes', 'strong_yes']), notes: optText(4000),
    }).parse(req.body);
    const id = z.uuid().parse(req.params.id);
    const companyId = companyIdOf(req);
    await tx(db, async (c) => {
      const i = (await c.query<{ application_id: string; panel: string[]; status: string }>('SELECT application_id, panel, status FROM hiring_interview WHERE id = $1 AND company_id = $2', [id, companyId])).rows[0];
      if (!i || !i.panel.includes(req.user!.id)) throw new NotFoundError('interview not found');
      if (i.status === 'cancelled') throw new ConflictError('this interview was cancelled');
      const st = (await c.query<{ status: string }>('SELECT status FROM hiring_application WHERE id = $1', [i.application_id])).rows[0];
      if (st?.status === 'hired') throw new ConflictError('this candidate has already been hired');
      await c.query(
        `INSERT INTO hiring_scorecard (interview_id, reviewer_id, ratings, recommendation, notes) VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (interview_id, reviewer_id) DO UPDATE SET ratings = EXCLUDED.ratings, recommendation = EXCLUDED.recommendation, notes = EXCLUDED.notes, submitted_at = now()`,
        [id, req.user!.id, JSON.stringify(b.ratings), b.recommendation, b.notes ?? null]);
      await addEvent(c, { companyId, applicationId: i.application_id, actorId: req.user!.id, kind: 'scorecard', body: `Scorecard: ${b.recommendation.replace('_', ' ')}`, data: { interviewId: id } });
    });
    res.status(201).json({ ok: true });
  });

  // ------------------------------------------------------------ offers

  r.post('/applications/:id/offers', requireModule('hiring'), async (req, res) => {
    const b = z.object({
      designation: z.string().trim().min(2).max(120), salary: money.refine((s) => Number(s) > 0, 'salary must be above zero'), joinDate: isoDate,
      expiresOn: isoDate.nullable().optional(), notes: optText(4000),
    }).parse(req.body);
    if (b.joinDate < nptToday()) throw new BadRequestError('the joining date is in the past');
    if (b.expiresOn && b.expiresOn > b.joinDate) throw new BadRequestError('the offer should expire on or before the joining date');
    const id = await tx(db, async (c) => {
      const { app, job } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (app.status !== 'active') throw new ConflictError(`this application is ${app.status}`);
      const open = await c.query(`SELECT 1 FROM hiring_offer WHERE application_id = $1 AND status IN ('pending_approval', 'approved', 'accepted')`, [app.id]);
      if (open.rows.length) throw new ConflictError('this candidate already has an open offer; withdraw it first');
      const ins = await c.query<{ id: string }>(
        `INSERT INTO hiring_offer (company_id, application_id, designation, salary, join_date, expires_on, notes, created_by, created_by_employee_id)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
        [app.company_id, app.id, b.designation, b.salary, b.joinDate, b.expiresOn ?? null, b.notes ?? null, req.user!.id, req.user!.employeeId ?? null]);
      const offerId = ins.rows[0]!.id;
      // Recording the drafter as the subject means a drafter who is also an employee cannot approve their own offer.
      await startWorkflow(c, {
        companyId: app.company_id, objectType: 'job_offer', objectId: offerId, employeeId: req.user!.employeeId ?? null,
        title: 'Offer needs approval', body: `Offer for ${app.name} as ${b.designation} (${job.title}).`,
      });
      await moveTo(c, app, job, 'Offer', req.user!.id);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'offer', body: `Offer drafted: ${b.designation}`, data: { offerId } });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'hiring_offer', rowId: offerId, after: b });
      return offerId;
    });
    res.status(201).json({ id });
  });

  const loadOffer = async (c: Queryable, req: Request, id: string, lock = false) => {
    const o = (await c.query<{ id: string; application_id: string; status: string; designation: string; salary: string; join_date: string; expires_on: string | null; notes: string | null; created_at: Date }>(
      `SELECT * FROM hiring_offer WHERE id = $1 AND company_id = $2${lock ? ' FOR UPDATE' : ''}`, [id, companyIdOf(req)])).rows[0];
    if (!o) throw new NotFoundError('offer not found');
    const { app, job } = await loadApp(c, req, o.application_id);
    return { o, app, job };
  };

  r.get('/offers/:id/letter', staff, async (req, res) => {
    const { o, app, job } = await loadOffer(db, req, z.uuid().parse(req.params.id));
    const extra = (await db.query<{ department: string | null; location: string | null; employment_type: string; code: string; name_en: string }>(
      `SELECT ou.name_en AS department, j.location, j.employment_type, co.code, co.name_en FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id
         JOIN company co ON co.id = j.company_id WHERE j.id = $1`, [job.id])).rows[0]!;
    res.type('text/html').send(renderOfferLetter({
      company: extra.name_en, candidate: app.name, designation: o.designation, department: extra.department, location: extra.location,
      employmentType: extra.employment_type, salary: o.salary, joinDate: o.join_date, expiresOn: o.expires_on, notes: o.notes,
      issuedOn: nptIso(o.created_at).slice(0, 10), reference: `${extra.code}/HR/OFFER/${o.id.slice(-6).toUpperCase()}`,
      draft: !['approved', 'accepted'].includes(o.status),
    }));
  });

  r.post('/offers/:id/respond', requireModule('hiring'), async (req, res) => {
    const { response } = z.object({ response: z.enum(['accepted', 'declined']) }).parse(req.body);
    await tx(db, async (c) => {
      const { o, app } = await loadOffer(c, req, z.uuid().parse(req.params.id), true);
      if (o.status !== 'approved') throw new ConflictError(o.status === 'pending_approval' ? 'the offer must be approved before the candidate can respond' : `this offer is ${o.status.replace('_', ' ')}`);
      await c.query('UPDATE hiring_offer SET status = $2, responded_at = now() WHERE id = $1', [o.id, response]);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'offer', body: `Offer ${response} by the candidate`, data: { offerId: o.id } });
      await audit(c, { actorId: req.user!.id, action: response, table: 'hiring_offer', rowId: o.id });
    });
    res.json({ ok: true });
  });

  r.post('/offers/:id/withdraw', requireModule('hiring'), async (req, res) => {
    await tx(db, async (c) => {
      const { o, app } = await loadOffer(c, req, z.uuid().parse(req.params.id), true);
      if (!['pending_approval', 'approved', 'accepted'].includes(o.status)) throw new ConflictError(`this offer is ${o.status.replace('_', ' ')}`);
      if (app.status === 'hired') throw new ConflictError('the candidate has already been hired');
      await c.query(`UPDATE hiring_offer SET status = 'withdrawn' WHERE id = $1`, [o.id]);
      await cancelWorkflow(c, 'job_offer', o.id);
      await addEvent(c, { companyId: app.company_id, applicationId: app.id, actorId: req.user!.id, kind: 'offer', body: 'Offer withdrawn', data: { offerId: o.id } });
      await audit(c, { actorId: req.user!.id, action: 'withdraw', table: 'hiring_offer', rowId: o.id });
    });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ hire

  /** Turn a candidate who accepted an offer into an employee; the CV stays linked through the application. */
  r.post('/applications/:id/hire', requireRole('hr'), async (req, res) => {
    const b = z.object({
      empCode: z.string().trim().min(1).max(32), joinDate: isoDate.optional(), designation: z.string().trim().max(120).optional(),
      branchId: z.uuid().optional(), orgUnitId: z.uuid().nullable().optional(), managerId: z.uuid().nullable().optional(), shiftId: z.uuid().optional(),
      employmentType: z.enum(['regular', 'work_based', 'time_based', 'casual', 'part_time']).default('regular'),
      gender: z.enum(['male', 'female', 'other']).optional(), probationEnd: isoDate.nullable().optional(),
    }).parse(req.body);
    const companyId = companyIdOf(req);
    await checkRefs(db, companyId, [['branch', b.branchId, 'branch'], ['org_unit', b.orgUnitId, 'department'], ['employee', b.managerId, 'manager'], ['shift', b.shiftId, 'shift']]);
    const employeeId = await tx(db, async (c) => {
      const { app, job } = await loadApp(c, req, z.uuid().parse(req.params.id), { lock: true });
      if (app.status !== 'active') throw new ConflictError(`this application is ${app.status}`);
      const offer = (await c.query<{ id: string; designation: string; join_date: string }>(
        `SELECT id, designation, join_date FROM hiring_offer WHERE application_id = $1 AND status = 'accepted' ORDER BY created_at DESC LIMIT 1`, [app.id])).rows[0];
      if (!offer) throw new ConflictError('the candidate must accept an approved offer before they can be hired');
      const dupCode = await c.query('SELECT 1 FROM employee WHERE company_id = $1 AND emp_code = $2', [companyId, b.empCode]);
      if (dupCode.rows.length) throw new ConflictError('this employee code is already in use');
      const joinDate = b.joinDate ?? offer.join_date;
      if (b.probationEnd && b.probationEnd < joinDate) throw new BadRequestError('probation end cannot be before join date');
      const ins = await c.query<{ id: string }>(
        `INSERT INTO employee (company_id, emp_code, name_en, gender, join_date, designation, phone, email, probation_end, weekly_off_days, regime)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '{6}', 'ssf') RETURNING id`,
        [companyId, b.empCode, app.name, b.gender ?? null, joinDate, b.designation || offer.designation, app.phone, app.email, b.probationEnd ?? null]);
      const empId = ins.rows[0]!.id;
      const jobRefs = (await c.query<{ branch_id: string | null }>('SELECT branch_id FROM hiring_job WHERE id = $1', [job.id])).rows[0];
      await c.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type) VALUES ($1, daterange($2, NULL), $3, $4, $5, $6)`,
        [empId, joinDate, b.branchId ?? jobRefs?.branch_id ?? null, b.orgUnitId !== undefined ? b.orgUnitId : job.org_unit_id,
          b.managerId !== undefined ? b.managerId : job.hiring_manager_id, b.employmentType]);
      if (b.shiftId) await c.query('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange($3, NULL))', [empId, b.shiftId, joinDate]);
      await c.query(`UPDATE hiring_application SET status = 'hired', stage = $2, furthest_stage = $3, hired_employee_id = $4, closed_at = now(), stage_changed_at = now() WHERE id = $1`,
        [app.id, job.stages[job.stages.length - 1], job.stages.length - 1, empId]);
      if (job.requisition_id) {
        await c.query(`UPDATE hiring_requisition SET filled = filled + 1, status = CASE WHEN filled + 1 >= headcount THEN 'filled' ELSE status END WHERE id = $1`, [job.requisition_id]);
      }
      await addEvent(c, { companyId, applicationId: app.id, actorId: req.user!.id, kind: 'hired', body: `Hired as ${b.empCode}`, data: { employeeId: empId } });
      await audit(c, { actorId: req.user!.id, action: 'hire', table: 'employee', rowId: empId, after: { applicationId: app.id, empCode: b.empCode, joinDate } });
      await runHireHooks(c, { companyId, employeeId: empId, applicationId: app.id, jobId: job.id, joinDate, actorId: req.user!.id });
      return empId;
    });
    res.status(201).json({ employeeId });
  });

  // ------------------------------------------------------------ people, reports, policy

  /** Names for the hiring forms: users who can sit on a panel or own a candidate, departments, branches and shifts. */
  r.get('/lookups', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const [users, units, branches, shifts] = await Promise.all([
      db.query(`SELECT id, name, roles FROM app_user WHERE company_id = $1 AND status = 'active' ORDER BY name`, [companyId]),
      db.query(`SELECT id, name_en, kind FROM org_unit WHERE company_id = $1 ORDER BY name_en`, [companyId]),
      db.query(`SELECT id, name_en FROM branch WHERE company_id = $1 ORDER BY name_en`, [companyId]),
      db.query(`SELECT id, code, name_en FROM shift WHERE company_id = $1 ORDER BY name_en`, [companyId]),
    ]);
    res.json({ users: users.rows, orgUnits: units.rows, branches: branches.rows, shifts: shifts.rows });
  });

  r.get('/reports', requireModule('hiring'), async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ from: isoDate.optional(), to: isoDate.optional() }).parse(req.query);
    const params = [companyId, q.from ?? null, q.to ?? null];
    const window = `a.company_id = $1 AND ($2::date IS NULL OR a.created_at >= ($2::date)::timestamp AT TIME ZONE 'Asia/Kathmandu')
                    AND ($3::date IS NULL OR a.created_at < ($3::date + 1)::timestamp AT TIME ZONE 'Asia/Kathmandu')`;
    const jobs = await db.query<{ id: string; title: string; status: string; stages: string[] }>(
      `SELECT j.id, j.title, j.status, j.stages FROM hiring_job j WHERE j.company_id = $1 AND EXISTS (SELECT 1 FROM hiring_application a WHERE a.job_id = j.id AND ${window})
        ORDER BY j.created_at DESC`, params);
    const reached = await db.query<{ job_id: string; furthest_stage: number; status: string }>(`SELECT a.job_id, a.furthest_stage, a.status FROM hiring_application a WHERE ${window}`, params);
    const funnel = jobs.rows.map((j) => {
      const apps = reached.rows.filter((x) => x.job_id === j.id);
      return {
        jobId: j.id, title: j.title, status: j.status, total: apps.length, rejected: apps.filter((a) => a.status === 'rejected').length,
        stages: j.stages.map((s, i) => ({ stage: s, reached: apps.filter((a) => a.furthest_stage >= i).length })),
      };
    });
    const tth = (await db.query<{ avg: string | null; median: string | null; hires: number }>(
      `SELECT round(avg(extract(epoch FROM a.closed_at - a.created_at) / 86400)::numeric, 1)::text AS avg,
              round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM a.closed_at - a.created_at) / 86400))::numeric, 1)::text AS median,
              count(*)::int AS hires
         FROM hiring_application a WHERE ${window} AND a.status = 'hired'`, params)).rows[0];
    const sources = await db.query(
      `SELECT a.source, count(*)::int AS applications, count(*) FILTER (WHERE a.status = 'hired')::int AS hired FROM hiring_application a WHERE ${window} GROUP BY a.source ORDER BY 2 DESC`, params);
    const totals = await db.query(
      `SELECT count(*)::int AS applications, count(*) FILTER (WHERE a.status = 'active')::int AS active, count(*) FILTER (WHERE a.status = 'hired')::int AS hired,
              count(*) FILTER (WHERE a.status = 'rejected')::int AS rejected FROM hiring_application a WHERE ${window}`, params);
    res.json({
      totals: totals.rows[0], funnel, sources: sources.rows,
      timeToHire: { averageDays: tth?.avg == null ? null : Number(tth.avg), medianDays: tth?.median == null ? null : Number(tth.median), hires: tth?.hires ?? 0 },
    });
  });

  r.get('/policy', staff, async (req, res) => { res.json({ policy: await hiringPolicy(db, companyIdOf(req)) }); });

  r.put('/policy', requireModule('hiring'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ retentionDays: z.number().int().min(30).max(1095), careersEnabled: z.boolean() }).parse(req.body);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, 'hiring.policy', $2, $3)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()`, [companyId, JSON.stringify(b), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: null, after: { key: 'hiring.policy', ...b } });
    res.json({ policy: b });
  });

  return r;
}
