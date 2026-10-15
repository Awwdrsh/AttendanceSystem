import { Router } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, money, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { attachFile, readFile, sendStoredFile } from '../files/store.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { SUBORDINATES_CTE } from '../performance/goals.js';
import { employeeInCompany, esc, isAboveInChain, tx } from '../performance/service.js';
import { cancelWorkflow, nptToday, startWorkflow } from '../workflow/service.js';
import { completeEnrollment } from './service.js';

const MAX_ATTEMPTS = 5;

const courseBody = z.object({
  code: z.string().trim().min(1).max(30),
  title: z.string().trim().min(1).max(200),
  description: z.string().trim().max(4000).nullish(),
  category: z.string().trim().max(60).nullish(),
  delivery: z.enum(['online', 'classroom', 'blended']).default('classroom'),
  source: z.enum(['internal', 'external']).default('internal'),
  provider: z.string().trim().max(120).nullish(),
  durationHours: z.coerce.number().min(0).max(10000).nullish(),
  url: z.url({ protocol: /^https?$/ }).max(500).nullish().or(z.literal('').transform(() => null)),
  cost: money.default('0'),
  mandatory: z.boolean().default(false),
  mandatoryFor: z.array(z.string().trim().min(1).max(120)).max(100).default([]),
  validityMonths: z.coerce.number().int().min(1).max(120).nullish(),
  passMark: z.coerce.number().int().min(1).max(100).nullish(),
  active: z.boolean().default(true),
});

const sessionBody = z.object({
  startsAt: z.iso.datetime({ offset: true }),
  endsAt: z.iso.datetime({ offset: true }),
  venue: z.string().trim().max(200).nullish(),
  trainer: z.string().trim().max(120).nullish(),
  capacity: z.coerce.number().int().min(1).max(10000).nullish(),
  status: z.enum(['scheduled', 'completed', 'cancelled']).default('scheduled'),
});

interface EnrollmentRow {
  id: string; company_id: string; course_id: string; session_id: string | null; employee_id: string; status: string; workflow_instance_id: string | null;
  certificate_file_id: string | null; delivery: string; pass_mark: number | null; title: string; questions: number;
}

async function loadEnrollment(db: Queryable, companyId: string, id: string): Promise<EnrollmentRow> {
  const r = await db.query<EnrollmentRow>(
    `SELECT e.id, e.company_id, e.course_id, e.session_id, e.employee_id, e.status, e.workflow_instance_id, e.certificate_file_id, c.delivery, c.pass_mark, c.title,
            (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions
       FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id WHERE e.id = $1 AND e.company_id = $2`, [id, companyId]);
  if (!r.rows[0]) throw new NotFoundError('enrolment not found');
  return r.rows[0];
}

/** The learner, their managers and HR may see an enrolment; only managers and HR may manage it. */
async function enrollmentRole(db: Queryable, user: AuthUser, e: EnrollmentRow): Promise<'self' | 'manager' | null> {
  if (user.employeeId === e.employee_id) return 'self';
  if (await isAboveInChain(db, user, e.employee_id)) return 'manager';
  return null;
}

async function sessionHasRoom(db: Queryable, sessionId: string, courseId: string, companyId: string): Promise<void> {
  const s = await db.query<{ capacity: number | null; status: string; taken: number }>(
    `SELECT s.capacity, s.status, (SELECT count(*)::int FROM lrn_enrollment e WHERE e.session_id = s.id AND e.status IN ('pending_approval', 'enrolled', 'completed')) AS taken
       FROM lrn_session s WHERE s.id = $1 AND s.course_id = $2 AND s.company_id = $3`, [sessionId, courseId, companyId]);
  const row = s.rows[0];
  if (!row) throw new NotFoundError('session not found');
  if (row.status !== 'scheduled') throw new ConflictError('this session is not open');
  if (row.capacity !== null && row.taken >= row.capacity) throw new ConflictError('this session is full');
}

const ENROLLMENT_SELECT = `
  SELECT e.id, e.course_id, c.code, c.title, c.delivery, c.mandatory, c.pass_mark, c.validity_months, e.session_id, s.starts_at, s.ends_at, s.venue,
         e.employee_id, em.name_en, em.emp_code, e.source, e.status, e.attended, e.completed_at, e.score, e.passed, e.feedback_rating, e.feedback_comment,
         e.certificate_file_id, e.certificate_issued_on, e.certificate_expires_on, e.cost, e.reason, e.created_at,
         (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions,
         (SELECT count(*)::int FROM lrn_attempt a WHERE a.enrollment_id = e.id) AS attempts
    FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id JOIN employee em ON em.id = e.employee_id
    LEFT JOIN lrn_session s ON s.id = e.session_id`;

export function learningRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  // ---------------------------------------------------------------- catalogue
  r.get('/courses', async (req, res) => {
    const companyId = companyIdOf(req); const hr = hasRole(req.user!, 'hr');
    const rows = await db.query(
      `SELECT c.*, (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions,
              (SELECT count(*)::int FROM lrn_enrollment e WHERE e.course_id = c.id AND e.status = 'completed') AS completed,
              (SELECT count(*)::int FROM lrn_enrollment e WHERE e.course_id = c.id AND e.status IN ('enrolled', 'pending_approval')) AS enrolled,
              (SELECT min(s.starts_at) FROM lrn_session s WHERE s.course_id = c.id AND s.status = 'scheduled' AND s.starts_at > now()) AS next_session
         FROM lrn_course c WHERE c.company_id = $1 AND (c.active OR $2) ORDER BY c.mandatory DESC, c.title`, [companyId, hr]);
    res.json({ courses: rows.rows });
  });

  r.get('/courses/:id', async (req, res) => {
    const companyId = companyIdOf(req); const hr = hasRole(req.user!, 'hr');
    const c = await db.query('SELECT * FROM lrn_course WHERE id = $1 AND company_id = $2 AND (active OR $3)', [uuid.parse(req.params.id), companyId, hr]);
    if (!c.rows[0]) throw new NotFoundError('course not found');
    const sessions = await db.query(
      `SELECT s.*, (SELECT count(*)::int FROM lrn_enrollment e WHERE e.session_id = s.id AND e.status IN ('pending_approval', 'enrolled', 'completed')) AS taken
         FROM lrn_session s WHERE s.course_id = $1 ORDER BY s.starts_at DESC`, [c.rows[0].id]);
    // Answers stay on the server; HR sees them to edit the assessment.
    const questions = await db.query(`SELECT id, prompt, options${hr ? ', correct_index' : ''} FROM lrn_question WHERE course_id = $1 ORDER BY sort, id`, [c.rows[0].id]);
    res.json({ course: c.rows[0], sessions: sessions.rows, questions: hr ? questions.rows : [], questionCount: questions.rows.length });
  });

  r.post('/courses', requireRole('hr'), async (req, res) => {
    const b = courseBody.parse(req.body);
    const r0 = await db.query<{ id: string }>(
      `INSERT INTO lrn_course (company_id, code, title, description, category, delivery, source, provider, duration_hours, url, cost, mandatory, mandatory_for, validity_months, pass_mark, active)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id`,
      [companyIdOf(req), b.code, b.title, b.description ?? null, b.category ?? null, b.delivery, b.source, b.provider ?? null, b.durationHours ?? null, b.url ?? null,
        b.cost, b.mandatory, b.mandatoryFor, b.validityMonths ?? null, b.passMark ?? null, b.active]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.patch('/courses/:id', requireRole('hr'), async (req, res) => {
    const b = courseBody.partial().parse(req.body);
    const map: Record<string, string> = { code: 'code', title: 'title', description: 'description', category: 'category', delivery: 'delivery', source: 'source', provider: 'provider',
      durationHours: 'duration_hours', url: 'url', cost: 'cost', mandatory: 'mandatory', mandatoryFor: 'mandatory_for', validityMonths: 'validity_months', passMark: 'pass_mark', active: 'active' };
    const sets: string[] = []; const vals: unknown[] = [uuid.parse(req.params.id), companyIdOf(req)];
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(v); sets.push(`${col} = $${vals.length}`); }
    }
    if (!sets.length) { res.json({ ok: true }); return; }
    const r0 = await db.query(`UPDATE lrn_course SET ${sets.join(', ')} WHERE id = $1 AND company_id = $2`, vals);
    if (!r0.rowCount) throw new NotFoundError('course not found');
    res.json({ ok: true });
  });

  /** Replace the course's multiple-choice assessment. */
  r.put('/courses/:id/questions', requireRole('hr'), async (req, res) => {
    const b = z.object({ questions: z.array(z.object({
      prompt: z.string().trim().min(1).max(1000),
      options: z.array(z.string().trim().min(1).max(300)).min(2).max(6),
      correctIndex: z.number().int().min(0),
    }).refine((q) => q.correctIndex < q.options.length, 'the correct answer must be one of the options')).max(50) }).parse(req.body);
    const id = uuid.parse(req.params.id); const companyId = companyIdOf(req);
    await tx(db, async (cx) => {
      const c = await cx.query('SELECT 1 FROM lrn_course WHERE id = $1 AND company_id = $2', [id, companyId]);
      if (!c.rows[0]) throw new NotFoundError('course not found');
      await cx.query('DELETE FROM lrn_question WHERE course_id = $1', [id]);
      let sort = 0;
      for (const q of b.questions) await cx.query('INSERT INTO lrn_question (course_id, prompt, options, correct_index, sort) VALUES ($1, $2, $3, $4, $5)', [id, q.prompt, JSON.stringify(q.options), q.correctIndex, sort++]);
      if (b.questions.length) await cx.query('UPDATE lrn_course SET pass_mark = coalesce(pass_mark, 60) WHERE id = $1', [id]);
    });
    res.json({ ok: true });
  });

  r.post('/courses/:id/sessions', requireRole('hr'), async (req, res) => {
    const b = sessionBody.parse(req.body);
    if (new Date(b.endsAt) <= new Date(b.startsAt)) throw new BadRequestError('the session ends before it starts');
    const id = uuid.parse(req.params.id); const companyId = companyIdOf(req);
    const c = await db.query('SELECT 1 FROM lrn_course WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!c.rows[0]) throw new NotFoundError('course not found');
    const r0 = await db.query<{ id: string }>('INSERT INTO lrn_session (company_id, course_id, starts_at, ends_at, venue, trainer, capacity, status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id',
      [companyId, id, b.startsAt, b.endsAt, b.venue ?? null, b.trainer ?? null, b.capacity ?? null, b.status]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.patch('/sessions/:id', requireRole('hr'), async (req, res) => {
    const b = sessionBody.partial().parse(req.body);
    const map: Record<string, string> = { startsAt: 'starts_at', endsAt: 'ends_at', venue: 'venue', trainer: 'trainer', capacity: 'capacity', status: 'status' };
    const sets: string[] = []; const vals: unknown[] = [uuid.parse(req.params.id), companyIdOf(req)];
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(v); sets.push(`${col} = $${vals.length}`); }
    }
    if (!sets.length) { res.json({ ok: true }); return; }
    const r0 = await db.query(`UPDATE lrn_session SET ${sets.join(', ')} WHERE id = $1 AND company_id = $2`, vals);
    if (!r0.rowCount) throw new NotFoundError('session not found');
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- enrolments
  r.get('/enrollments', async (req, res) => {
    const q = z.object({ scope: z.enum(['mine', 'team', 'all']).default('mine'), courseId: uuid.optional(), status: z.string().max(30).optional() }).parse(req.query);
    const u = req.user!; const companyId = companyIdOf(req);
    const filters = `e.company_id = $1 AND ($2::uuid IS NULL OR e.course_id = $2) AND ($3::text IS NULL OR e.status = $3)`;
    let rows;
    if (q.scope === 'mine') {
      if (!u.employeeId) { res.json({ enrollments: [] }); return; }
      rows = await db.query(`${ENROLLMENT_SELECT} WHERE ${filters} AND e.employee_id = $4 ORDER BY e.created_at DESC`, [companyId, q.courseId ?? null, q.status ?? null, u.employeeId]);
    } else if (q.scope === 'all' || (q.scope === 'team' && !u.employeeId)) {
      if (!hasRole(u, 'hr')) throw new ForbiddenError('requires HR');
      rows = await db.query(`${ENROLLMENT_SELECT} WHERE ${filters} ORDER BY e.created_at DESC LIMIT 1000`, [companyId, q.courseId ?? null, q.status ?? null]);
    } else {
      rows = await db.query(`${SUBORDINATES_CTE.replace('$MGR', '$4')} ${ENROLLMENT_SELECT} WHERE ${filters} AND e.employee_id IN (SELECT emp FROM subs) ORDER BY e.created_at DESC LIMIT 1000`,
        [companyId, q.courseId ?? null, q.status ?? null, u.employeeId]);
    }
    res.json({ enrollments: rows.rows });
  });

  /** Ask to join a course; the manager approves through the approvals inbox. */
  r.post('/enrollments/self', async (req, res) => {
    const b = z.object({ courseId: uuid, sessionId: uuid.optional(), reason: z.string().trim().max(1000).optional() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    if (!u.employeeId) throw new ForbiddenError('this account is not linked to an employee');
    const c = await db.query<{ title: string; cost: string }>('SELECT title, cost FROM lrn_course WHERE id = $1 AND company_id = $2 AND active', [b.courseId, companyId]);
    if (!c.rows[0]) throw new NotFoundError('course not found');
    if (b.sessionId) await sessionHasRoom(db, b.sessionId, b.courseId, companyId);
    const id = await tx(db, async (cx) => {
      const ins = await cx.query<{ id: string }>(
        `INSERT INTO lrn_enrollment (company_id, course_id, session_id, employee_id, source, status, reason, cost) VALUES ($1, $2, $3, $4, 'self', 'pending_approval', $5, $6) RETURNING id`,
        [companyId, b.courseId, b.sessionId ?? null, u.employeeId, b.reason ?? null, c.rows[0]!.cost]).catch((e: { code?: string }) => {
        if (e.code === '23505') throw new ConflictError('you are already enrolled or waiting for approval for this course');
        throw e;
      });
      const eid = ins.rows[0]!.id;
      const inst = await startWorkflow(cx, { companyId, objectType: 'training_nomination', objectId: eid, employeeId: u.employeeId, title: 'Training request',
        body: `${u.name} asked to join ${c.rows[0]!.title}${b.reason ? `: ${b.reason}` : ''}` });
      await cx.query('UPDATE lrn_enrollment SET workflow_instance_id = $2 WHERE id = $1', [eid, inst.id]);
      return eid;
    });
    res.status(201).json({ id });
  });

  /** HR assigns anyone; a manager assigns their team. Assigned enrolments need no approval. */
  r.post('/enrollments/assign', async (req, res) => {
    const b = z.object({ courseId: uuid, sessionId: uuid.optional(), employeeIds: z.array(uuid).min(1).max(500) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    if (!hasRole(u, 'hr', 'manager')) throw new ForbiddenError('only managers and HR assign training');
    const c = await db.query<{ title: string; cost: string }>('SELECT title, cost FROM lrn_course WHERE id = $1 AND company_id = $2 AND active', [b.courseId, companyId]);
    if (!c.rows[0]) throw new NotFoundError('course not found');
    const out = await tx(db, async (cx) => {
      let added = 0; let skipped = 0;
      for (const empId of new Set(b.employeeIds)) {
        await employeeInCompany(cx, companyId, empId);
        if (!(await isAboveInChain(cx, u, empId))) throw new ForbiddenError('you can assign training only to your team');
        if (b.sessionId) await sessionHasRoom(cx, b.sessionId, b.courseId, companyId);
        const ins = await cx.query(
          `INSERT INTO lrn_enrollment (company_id, course_id, session_id, employee_id, source, status, assigned_by, cost) VALUES ($1, $2, $3, $4, 'assigned', 'enrolled', $5, $6) ON CONFLICT DO NOTHING`,
          [companyId, b.courseId, b.sessionId ?? null, empId, u.id, c.rows[0]!.cost]);
        if (ins.rowCount) {
          added++;
          await notify(cx, { companyId, userId: await userIdForEmployee(cx, empId), employeeId: empId, template: 'learning.assigned', title: 'Training assigned', body: `${u.name} enrolled you in ${c.rows[0]!.title}.`, payload: { courseId: b.courseId } });
        } else skipped++;
      }
      return { added, skipped };
    });
    res.status(201).json(out);
  });

  /** Enrol everyone a mandatory course applies to who has no open enrolment and no certificate still valid. */
  r.post('/courses/:id/assign-mandatory', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id); const companyId = companyIdOf(req); const u = req.user!;
    const c = await db.query<{ title: string; cost: string; mandatory: boolean; mandatory_for: string[] }>('SELECT title, cost, mandatory, mandatory_for FROM lrn_course WHERE id = $1 AND company_id = $2 AND active', [id, companyId]);
    const course = c.rows[0];
    if (!course) throw new NotFoundError('course not found');
    if (!course.mandatory) throw new BadRequestError('mark the course mandatory first');
    const added = await tx(db, async (cx) => {
      const emps = await cx.query<{ id: string }>(
        `SELECT e.id FROM employee e WHERE e.company_id = $1 AND e.status = 'active' AND (cardinality($2::text[]) = 0 OR e.designation = ANY($2::text[]))
           AND NOT EXISTS (SELECT 1 FROM lrn_enrollment x WHERE x.employee_id = e.id AND x.course_id = $3
                            AND (x.status IN ('pending_approval', 'enrolled') OR (x.status = 'completed' AND (x.certificate_expires_on IS NULL OR x.certificate_expires_on > current_date + 30))))`,
        [companyId, course.mandatory_for, id]);
      for (const e of emps.rows) {
        await cx.query(`INSERT INTO lrn_enrollment (company_id, course_id, employee_id, source, status, assigned_by, cost) VALUES ($1, $2, $3, 'mandatory', 'enrolled', $4, $5) ON CONFLICT DO NOTHING`,
          [companyId, id, e.id, u.id, course.cost]);
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, e.id), employeeId: e.id, template: 'learning.assigned', title: 'Mandatory training', body: `You are enrolled in ${course.title}.`, payload: { courseId: id } });
      }
      return emps.rows.length;
    });
    res.json({ ok: true, added });
  });

  r.post('/enrollments/:id/cancel', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    const role = await enrollmentRole(db, u, e);
    if (!role) throw new NotFoundError('enrolment not found');
    if (!['pending_approval', 'enrolled'].includes(e.status)) throw new ConflictError(`this enrolment is ${e.status.replace('_', ' ')}`);
    await tx(db, async (cx) => {
      await cx.query(`UPDATE lrn_enrollment SET status = 'cancelled' WHERE id = $1`, [e.id]);
      if (e.status === 'pending_approval') await cancelWorkflow(cx, 'training_nomination', e.id);
    });
    res.json({ ok: true });
  });

  /** Trainer's register: attended completes the course (unless an assessment is still to pass); absent is a no-show. */
  r.post('/enrollments/:id/attendance', async (req, res) => {
    const b = z.object({ attended: z.boolean() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    const role = await enrollmentRole(db, u, e);
    if (!role) throw new NotFoundError('enrolment not found');
    if (role !== 'manager') throw new ForbiddenError('managers and HR record attendance');
    if (e.status !== 'enrolled') throw new ConflictError(`this enrolment is ${e.status.replace('_', ' ')}`);
    await tx(db, async (cx) => {
      if (!b.attended) await cx.query(`UPDATE lrn_enrollment SET attended = false, status = 'no_show' WHERE id = $1`, [e.id]);
      else if (e.questions > 0) await cx.query('UPDATE lrn_enrollment SET attended = true WHERE id = $1', [e.id]);
      else await completeEnrollment(cx, e.id, { attended: true });
      await audit(cx, { actorId: u.id, action: 'attendance', table: 'lrn_enrollment', rowId: e.id, after: b });
    });
    res.json({ ok: true });
  });

  /** Online courses without an assessment are completed by the learner; managers and HR may complete any. */
  r.post('/enrollments/:id/complete', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    const role = await enrollmentRole(db, u, e);
    if (!role) throw new NotFoundError('enrolment not found');
    if (role === 'self') {
      if (e.questions > 0) throw new ConflictError('pass the assessment to complete this course');
      if (e.delivery !== 'online') throw new ForbiddenError('classroom attendance is recorded by the trainer or HR');
    }
    await tx(db, (cx) => completeEnrollment(cx, e.id));
    res.json({ ok: true });
  });

  r.get('/enrollments/:id/assessment', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    if (u.employeeId !== e.employee_id) throw new NotFoundError('enrolment not found');
    const qs = await db.query('SELECT id, prompt, options FROM lrn_question WHERE course_id = $1 ORDER BY sort, id', [e.course_id]);
    const attempts = await db.query('SELECT score, passed, created_at FROM lrn_attempt WHERE enrollment_id = $1 ORDER BY created_at DESC', [e.id]);
    res.json({ title: e.title, passMark: e.pass_mark, questions: qs.rows, attempts: attempts.rows, attemptsLeft: Math.max(0, MAX_ATTEMPTS - attempts.rows.length) });
  });

  r.post('/enrollments/:id/attempts', async (req, res) => {
    const b = z.object({ answers: z.array(z.number().int().min(0).max(10)).max(50) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const out = await tx(db, async (cx) => {
      const e = await loadEnrollment(cx, companyId, uuid.parse(req.params.id));
      if (u.employeeId !== e.employee_id) throw new NotFoundError('enrolment not found');
      if (e.status !== 'enrolled') throw new ConflictError(e.status === 'completed' ? 'you already completed this course' : `this enrolment is ${e.status.replace('_', ' ')}`);
      await cx.query('SELECT 1 FROM lrn_enrollment WHERE id = $1 FOR UPDATE', [e.id]);
      const qs = await cx.query<{ correct_index: number }>('SELECT correct_index FROM lrn_question WHERE course_id = $1 ORDER BY sort, id', [e.course_id]);
      if (!qs.rows.length) throw new BadRequestError('this course has no assessment');
      if (b.answers.length !== qs.rows.length) throw new BadRequestError(`answer all ${qs.rows.length} questions`);
      const used = await cx.query<{ n: number }>('SELECT count(*)::int AS n FROM lrn_attempt WHERE enrollment_id = $1', [e.id]);
      if (used.rows[0]!.n >= MAX_ATTEMPTS) throw new ConflictError(`no attempts left (maximum ${MAX_ATTEMPTS}); ask HR to re-enrol you`);
      const correct = qs.rows.reduce((s, q, i) => s + (q.correct_index === b.answers[i] ? 1 : 0), 0);
      const score = Math.round((correct / qs.rows.length) * 10000) / 100;
      const passed = score >= (e.pass_mark ?? 60);
      await cx.query('INSERT INTO lrn_attempt (enrollment_id, answers, score, passed) VALUES ($1, $2, $3, $4)', [e.id, JSON.stringify(b.answers), score, passed]);
      await cx.query('UPDATE lrn_enrollment SET score = GREATEST(coalesce(score, 0), $2), passed = coalesce(passed, false) OR $3 WHERE id = $1', [e.id, score, passed]);
      // An online course is done when the assessment is passed; classroom courses also need the trainer's attendance.
      const att = await cx.query<{ attended: boolean | null }>('SELECT attended FROM lrn_enrollment WHERE id = $1', [e.id]);
      if (passed && (e.delivery === 'online' || att.rows[0]?.attended)) await completeEnrollment(cx, e.id);
      return { score, passed, correct, total: qs.rows.length, attemptsLeft: MAX_ATTEMPTS - used.rows[0]!.n - 1 };
    });
    res.status(201).json(out);
  });

  r.post('/enrollments/:id/feedback', async (req, res) => {
    const b = z.object({ rating: z.number().int().min(1).max(5), comment: z.string().trim().max(2000).nullish() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    if (u.employeeId !== e.employee_id) throw new NotFoundError('enrolment not found');
    if (e.status !== 'completed') throw new ConflictError('rate the course after completing it');
    await db.query('UPDATE lrn_enrollment SET feedback_rating = $2, feedback_comment = $3 WHERE id = $1', [e.id, b.rating, b.comment ?? null]);
    res.json({ ok: true });
  });

  /** Attach an uploaded certificate (e.g. from an external provider); HR may set the issue and expiry dates. */
  r.post('/enrollments/:id/certificate', async (req, res) => {
    const b = z.object({ fileId: uuid, issuedOn: isoDate.optional(), expiresOn: isoDate.nullish() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    const role = await enrollmentRole(db, u, e);
    if (!role) throw new NotFoundError('enrolment not found');
    if (e.status !== 'completed') throw new ConflictError('add a certificate after the course is completed');
    if (b.issuedOn && b.expiresOn && b.expiresOn < b.issuedOn) throw new BadRequestError('the certificate expires before it was issued');
    await tx(db, async (cx) => {
      await attachFile(cx, { companyId, fileId: b.fileId, userId: u.id, purpose: 'training_certificate', attachedTo: `lrn_enrollment:${e.id}` });
      await cx.query(
        `UPDATE lrn_enrollment SET certificate_file_id = $2, certificate_issued_on = coalesce($3, certificate_issued_on),
                certificate_expires_on = CASE WHEN $5 THEN $4::date ELSE certificate_expires_on END, expiry_reminded_at = NULL WHERE id = $1`,
        [e.id, b.fileId, b.issuedOn ?? null, b.expiresOn ?? null, b.expiresOn !== undefined]);
      await audit(cx, { actorId: u.id, action: 'certificate', table: 'lrn_enrollment', rowId: e.id, after: { fileId: b.fileId } });
    });
    res.json({ ok: true });
  });

  r.get('/enrollments/:id/certificate-file', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    if (!(await enrollmentRole(db, u, e))) throw new NotFoundError('enrolment not found');
    if (!e.certificate_file_id) throw new NotFoundError('no certificate uploaded');
    sendStoredFile(res, await readFile(db, cipher, companyId, e.certificate_file_id), { download: req.query.download === '1' });
  });

  /** Printable completion certificate generated by the system. */
  r.get('/enrollments/:id/certificate', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const e = await loadEnrollment(db, companyId, uuid.parse(req.params.id));
    if (!(await enrollmentRole(db, u, e))) throw new NotFoundError('enrolment not found');
    const r0 = await db.query<{ status: string; name_en: string; emp_code: string; title: string; duration_hours: string | null; provider: string | null; certificate_issued_on: string | null; certificate_expires_on: string | null; score: string | null; company: string }>(
      `SELECT e.status, em.name_en, em.emp_code, c.title, c.duration_hours, c.provider, e.certificate_issued_on, e.certificate_expires_on, e.score, co.name_en AS company
         FROM lrn_enrollment e JOIN employee em ON em.id = e.employee_id JOIN lrn_course c ON c.id = e.course_id JOIN company co ON co.id = e.company_id WHERE e.id = $1`, [e.id]);
    const x = r0.rows[0]!;
    if (x.status !== 'completed') throw new ConflictError('the certificate is available after completion');
    const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Certificate ${esc(x.emp_code)} ${esc(x.title)}</title><style>
@page{size:A4 landscape;margin:0}body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#17191e}
.page{box-sizing:border-box;width:297mm;height:210mm;padding:22mm;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;border:10px solid #12773a;outline:2px solid #12773a;outline-offset:-22px}
.co{font-size:14px;letter-spacing:.2em;text-transform:uppercase;color:#6b707b}h1{font-size:40px;margin:16px 0 4px;font-weight:600;letter-spacing:-.02em}.to{color:#6b707b;margin-top:24px}
.name{font-size:30px;font-weight:600;margin:6px 0;color:#12773a}.course{font-size:20px;margin:8px 0}.meta{color:#6b707b;margin-top:18px;font-size:13px}
.sign{display:flex;gap:120px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;width:200px;color:#6b707b;font-size:12px}</style></head><body><div class="page">
<div class="co">${esc(x.company)}</div><h1>Certificate of completion</h1><div class="to">This certifies that</div><div class="name">${esc(x.name_en)}</div>
<div>has completed</div><div class="course">${esc(x.title)}</div>
<div class="meta">${[x.duration_hours ? `${esc(Number(x.duration_hours))} hours` : null, x.provider ? `Provided by ${esc(x.provider)}` : null, x.score !== null ? `Assessment score ${esc(Number(x.score))}%` : null].filter(Boolean).join(' · ')}</div>
<div class="meta">Issued ${esc(bsLabel(x.certificate_issued_on))}${x.certificate_expires_on ? ` · Valid until ${esc(bsLabel(x.certificate_expires_on))}` : ''} · Reference ${esc(e.id.slice(-8).toUpperCase())}</div>
<div class="sign"><div>Trainer</div><div>HR</div></div></div></body></html>`;
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.setHeader('Cache-Control', 'private, no-store');
    res.send(html);
  });

  // ---------------------------------------------------------------- reports
  r.get('/expiring', requireRole('hr'), async (req, res) => {
    const q = z.object({ days: z.coerce.number().int().min(0).max(365).default(60) }).parse(req.query);
    const rows = await db.query(
      `${ENROLLMENT_SELECT} WHERE e.company_id = $1 AND e.status = 'completed' AND e.certificate_expires_on IS NOT NULL AND e.certificate_expires_on <= $2::date + $3::int AND em.status = 'active'
         AND NOT EXISTS (SELECT 1 FROM lrn_enrollment n WHERE n.course_id = e.course_id AND n.employee_id = e.employee_id AND n.created_at > e.created_at AND n.status IN ('enrolled', 'completed'))
       ORDER BY e.certificate_expires_on`, [companyIdOf(req), nptToday(), q.days]);
    res.json({ enrollments: rows.rows });
  });

  /** Training spend by course and by department, counting enrolled and completed seats in the period. */
  r.get('/reports/cost', requireRole('hr'), async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate }).parse(req.query);
    if (q.to < q.from) throw new BadRequestError('the period ends before it starts');
    const base = `FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id
      LEFT JOIN employee_assignment a ON a.employee_id = e.employee_id AND a.valid @> current_date LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
      WHERE e.company_id = $1 AND e.status IN ('enrolled', 'completed') AND (coalesce(e.completed_at, e.created_at) AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN $2 AND $3`;
    const byCourse = await db.query(`SELECT c.id, c.code, c.title, count(*)::int AS seats, count(*) FILTER (WHERE e.status = 'completed')::int AS completed, coalesce(sum(e.cost), 0)::text AS cost,
                                            round(avg(e.feedback_rating), 2)::text AS rating ${base} GROUP BY c.id, c.code, c.title ORDER BY sum(e.cost) DESC, c.title`, [companyIdOf(req), q.from, q.to]);
    const byDept = await db.query(`SELECT coalesce(ou.name_en, 'No department') AS department, count(*)::int AS seats, coalesce(sum(e.cost), 0)::text AS cost ${base} GROUP BY 1 ORDER BY sum(e.cost) DESC`, [companyIdOf(req), q.from, q.to]);
    const total = byCourse.rows.reduce((s: number, x: { cost: string }) => s + Number(x.cost), 0);
    res.json({ byCourse: byCourse.rows, byDepartment: byDept.rows, total: total.toFixed(2) });
  });

  return r;
}
