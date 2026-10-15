import type { Router } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { notify, notifyRole, userIdForEmployee } from '../notifications/service.js';
import { createReviewNominations } from '../learning/service.js';
import { esc, isAboveInChain, overallScore, round2, tx } from './service.js';

export const PHASES = ['draft', 'self_review', 'manager_review', 'peer_review', 'calibration', 'released', 'closed'] as const;
export type Phase = typeof PHASES[number];
export const PHASE_LABEL: Record<Phase, string> = {
  draft: 'Draft', self_review: 'Self review', manager_review: 'Manager review', peer_review: 'Peer review',
  calibration: 'Calibration', released: 'Released', closed: 'Closed',
};

interface CycleRow {
  id: string; company_id: string; name: string; period_start: string; period_end: string; status: Phase; goals_weight: string; competency_weight: string;
  rating_labels: string[]; peer_enabled: boolean; peer_min: number; self_due: string | null; manager_due: string | null; peer_due: string | null;
  calibration_due: string | null; filter: { orgUnitIds?: string[]; branchIds?: string[]; joinedBefore?: string };
}
interface ReviewRow {
  id: string; cycle_id: string; company_id: string; employee_id: string; manager_employee_id: string | null; self_submitted_at: Date | null;
  manager_submitted_at: Date | null; released_at: Date | null; acknowledged_at: Date | null; final_rating: string | null; manager_score: string | null;
}

/** Peer responses are shown to the reviewed employee only as an anonymous aggregate of at least this many responses. */
export function peerAggregate(peers: Array<{ status: string; rating: number | null; strengths: string | null; improvements: string | null }>, min: number) {
  const done = peers.filter((p) => p.status === 'submitted');
  if (done.length < min) return { count: done.length, shown: false as const };
  const rated = done.filter((p) => p.rating !== null);
  const sortText = (xs: Array<string | null>) => xs.filter((x): x is string => !!x && x.trim().length > 0).sort((a, b) => a.localeCompare(b));
  return {
    count: done.length, shown: true as const,
    averageRating: rated.length ? round2(rated.reduce((s, p) => s + p.rating!, 0) / rated.length) : null,
    strengths: sortText(done.map((p) => p.strengths)), improvements: sortText(done.map((p) => p.improvements)),
  };
}

async function loadCycle(db: Queryable, companyId: string, id: string, lock = false): Promise<CycleRow> {
  const r = await db.query<CycleRow>(`SELECT * FROM perf_cycle WHERE id = $1 AND company_id = $2${lock ? ' FOR UPDATE' : ''}`, [id, companyId]);
  if (!r.rows[0]) throw new NotFoundError('review cycle not found');
  return r.rows[0];
}

async function loadReview(db: Queryable, companyId: string, id: string): Promise<ReviewRow & { cycle: CycleRow }> {
  const r = await db.query<ReviewRow>('SELECT * FROM perf_review WHERE id = $1 AND company_id = $2', [id, companyId]);
  const rv = r.rows[0];
  if (!rv) throw new NotFoundError('review not found');
  return { ...rv, cycle: await loadCycle(db, companyId, rv.cycle_id) };
}

/** 'self' for the employee, 'manager' for the reviewing manager or anyone above, 'hr' for HR; otherwise no access. */
async function viewerRole(db: Queryable, user: AuthUser, rv: ReviewRow): Promise<'self' | 'manager' | 'hr' | null> {
  if (user.employeeId && user.employeeId === rv.employee_id) return 'self';
  if (hasRole(user, 'hr')) return 'hr';
  if (user.employeeId && rv.manager_employee_id === user.employeeId) return 'manager';
  if (await isAboveInChain(db, user, rv.employee_id)) return 'manager';
  return null;
}

const ratingLabels = z.array(z.string().trim().min(1).max(40)).length(5);
const cycleBody = z.object({
  name: z.string().trim().min(1).max(120),
  periodStart: isoDate,
  periodEnd: isoDate,
  goalsWeight: z.coerce.number().min(0).max(100).default(60),
  competencyWeight: z.coerce.number().min(0).max(100).default(40),
  ratingLabels: ratingLabels.optional(),
  peerEnabled: z.boolean().default(false),
  peerMin: z.coerce.number().int().min(1).max(10).default(3),
  selfDue: isoDate.nullish(),
  managerDue: isoDate.nullish(),
  peerDue: isoDate.nullish(),
  calibrationDue: isoDate.nullish(),
  filter: z.object({
    orgUnitIds: z.array(uuid).max(200).optional(),
    branchIds: z.array(uuid).max(200).optional(),
    joinedBefore: isoDate.optional(),
  }).default({}),
});

const competencyBody = z.object({
  kind: z.enum(['knowledge', 'skill', 'attitude']),
  name: z.string().trim().min(1).max(120),
  description: z.string().trim().max(2000).nullish(),
  indicators: z.array(z.object({ level: z.number().int().min(1).max(5), text: z.string().trim().min(1).max(500) })).max(5).default([]),
  designations: z.array(z.string().trim().min(1).max(120)).max(100).default([]),
  grades: z.array(z.string().trim().min(1).max(40)).max(50).default([]),
  active: z.boolean().default(true),
});

const recommendation = z.object({
  incrementPct: z.coerce.number().min(0).max(100).nullish(),
  promotion: z.boolean().default(false),
  pip: z.boolean().default(false),
  trainingCourseIds: z.array(uuid).max(10).default([]),
  notes: z.string().trim().max(2000).nullish(),
});

const itemRatings = z.array(z.object({ id: uuid, rating: z.number().int().min(1).max(5).nullable(), comment: z.string().trim().max(2000).nullish() })).max(100);

export function reviewRoutes(r: Router, { db }: AppDeps): void {
  // ---------------------------------------------------------------- competency framework
  r.get('/competencies', async (req, res) => {
    const q = z.object({ designation: z.string().trim().max(120).optional(), grade: z.string().trim().max(40).optional(), all: z.enum(['1']).optional() }).parse(req.query);
    const showInactive = q.all === '1' && hasRole(req.user!, 'hr');
    const rows = await db.query(
      `SELECT id, kind, name, description, indicators, designations, grades, active FROM perf_competency
        WHERE company_id = $1 AND (active OR $4)
          AND ($2::text IS NULL OR cardinality(designations) = 0 OR $2 = ANY(designations))
          AND ($3::text IS NULL OR cardinality(grades) = 0 OR $3 = ANY(grades))
        ORDER BY array_position(ARRAY['knowledge','skill','attitude'], kind), name`, [companyIdOf(req), q.designation || null, q.grade || null, showInactive]);
    res.json({ competencies: rows.rows });
  });

  r.post('/competencies', requireRole('hr'), async (req, res) => {
    const b = competencyBody.parse(req.body);
    const r0 = await db.query<{ id: string }>(
      `INSERT INTO perf_competency (company_id, kind, name, description, indicators, designations, grades, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
      [companyIdOf(req), b.kind, b.name, b.description ?? null, JSON.stringify(b.indicators), b.designations, b.grades, b.active]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.patch('/competencies/:id', requireRole('hr'), async (req, res) => {
    const b = competencyBody.partial().parse(req.body);
    const id = uuid.parse(req.params.id);
    const sets: string[] = []; const vals: unknown[] = [id, companyIdOf(req)];
    for (const [k, col] of Object.entries({ kind: 'kind', name: 'name', description: 'description', indicators: 'indicators', designations: 'designations', grades: 'grades', active: 'active' })) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(k === 'indicators' ? JSON.stringify(v) : v); sets.push(`${col} = $${vals.length}`); }
    }
    if (!sets.length) { res.json({ ok: true }); return; }
    const r0 = await db.query(`UPDATE perf_competency SET ${sets.join(', ')} WHERE id = $1 AND company_id = $2`, vals);
    if (!r0.rowCount) throw new NotFoundError('competency not found');
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- cycles
  r.get('/cycles', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const rows = hasRole(u, 'hr')
      ? await db.query(
        `SELECT c.*, (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id) AS participants,
                (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.self_submitted_at IS NOT NULL) AS self_done,
                (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_submitted_at IS NOT NULL) AS manager_done
           FROM perf_cycle c WHERE c.company_id = $1 ORDER BY c.created_at DESC`, [companyId])
      : await db.query(
        `SELECT c.*, (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = $2) AS participants,
                (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = $2 AND r.self_submitted_at IS NOT NULL) AS self_done,
                (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = $2 AND r.manager_submitted_at IS NOT NULL) AS manager_done
           FROM perf_cycle c WHERE c.company_id = $1 AND c.status <> 'draft'
            AND EXISTS (SELECT 1 FROM perf_review r WHERE r.cycle_id = c.id AND (r.employee_id = $2 OR r.manager_employee_id = $2))
          ORDER BY c.created_at DESC`, [companyId, u.employeeId]);
    res.json({ cycles: rows.rows });
  });

  r.post('/cycles', requireRole('hr'), async (req, res) => {
    const b = cycleBody.parse(req.body);
    if (b.periodEnd < b.periodStart) throw new BadRequestError('the period ends before it starts');
    if (b.goalsWeight + b.competencyWeight !== 100) throw new BadRequestError('goal and competency weights must add up to 100');
    const r0 = await db.query<{ id: string }>(
      `INSERT INTO perf_cycle (company_id, name, period_start, period_end, goals_weight, competency_weight, rating_labels, peer_enabled, peer_min,
                               self_due, manager_due, peer_due, calibration_due, filter, created_by)
       VALUES ($1, $2, $3, $4, $5, $6, coalesce($7::jsonb, '["Needs improvement", "Partly meets", "Meets expectations", "Exceeds expectations", "Outstanding"]'::jsonb),
               $8, $9, $10, $11, $12, $13, $14, $15) RETURNING id`,
      [companyIdOf(req), b.name, b.periodStart, b.periodEnd, b.goalsWeight, b.competencyWeight, b.ratingLabels ? JSON.stringify(b.ratingLabels) : null,
        b.peerEnabled, b.peerMin, b.selfDue ?? null, b.managerDue ?? null, b.peerDue ?? null, b.calibrationDue ?? null, JSON.stringify(b.filter), req.user!.id]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.patch('/cycles/:id', requireRole('hr'), async (req, res) => {
    const b = cycleBody.partial().parse(req.body);
    const c = await loadCycle(db, companyIdOf(req), uuid.parse(req.params.id));
    const always = ['name', 'selfDue', 'managerDue', 'peerDue', 'calibrationDue', 'peerMin'];
    if (c.status !== 'draft' && Object.keys(b).some((k) => !always.includes(k))) throw new ConflictError('after launch only the name, deadlines and the peer minimum can change');
    const gw = b.goalsWeight ?? Number(c.goals_weight); const cw = b.competencyWeight ?? Number(c.competency_weight);
    if (gw + cw !== 100) throw new BadRequestError('goal and competency weights must add up to 100');
    const map: Record<string, string> = { name: 'name', periodStart: 'period_start', periodEnd: 'period_end', goalsWeight: 'goals_weight', competencyWeight: 'competency_weight',
      ratingLabels: 'rating_labels', peerEnabled: 'peer_enabled', peerMin: 'peer_min', selfDue: 'self_due', managerDue: 'manager_due', peerDue: 'peer_due', calibrationDue: 'calibration_due', filter: 'filter' };
    const sets: string[] = []; const vals: unknown[] = [c.id];
    for (const [k, col] of Object.entries(map)) {
      const v = (b as Record<string, unknown>)[k];
      if (v !== undefined) { vals.push(k === 'ratingLabels' || k === 'filter' ? JSON.stringify(v) : v); sets.push(`${col} = $${vals.length}`); }
    }
    if (sets.length) await db.query(`UPDATE perf_cycle SET ${sets.join(', ')} WHERE id = $1`, vals);
    res.json({ ok: true });
  });

  r.delete('/cycles/:id', requireRole('hr'), async (req, res) => {
    const c = await loadCycle(db, companyIdOf(req), uuid.parse(req.params.id));
    if (c.status !== 'draft') throw new ConflictError('only a draft cycle can be deleted');
    await db.query('DELETE FROM perf_cycle WHERE id = $1', [c.id]);
    res.json({ ok: true });
  });

  r.get('/cycles/:id', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const c = await loadCycle(db, companyId, uuid.parse(req.params.id));
    const hr = hasRole(u, 'hr');
    if (!hr && c.status === 'draft') throw new NotFoundError('review cycle not found');
    const reviews = await db.query(
      `SELECT r.id, r.employee_id, e.emp_code, e.name_en, e.designation, ou.name_en AS department, m.name_en AS manager_name, r.manager_employee_id,
              r.self_submitted_at, r.manager_submitted_at, r.self_score, r.manager_score, r.final_rating, r.released_at, r.acknowledged_at,
              (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status = 'submitted') AS peers_done,
              (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status IN ('approved', 'submitted')) AS peers_total,
              (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status = 'nominated') AS peers_pending
         FROM perf_review r JOIN employee e ON e.id = r.employee_id
         LEFT JOIN employee m ON m.id = r.manager_employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = r.employee_id AND a.valid @> current_date
         LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
        WHERE r.cycle_id = $1 AND ($2 OR r.manager_employee_id = $3)
        ORDER BY e.name_en`, [c.id, hr, u.employeeId]);
    if (!hr && reviews.rows.length === 0) throw new NotFoundError('review cycle not found');
    res.json({ cycle: c, reviews: reviews.rows });
  });

  /** Create one review per participant with their goals for the period and the competencies of their role. */
  r.post('/cycles/:id/launch', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req); const u = req.user!;
    const out = await tx(db, async (cx) => {
      const c = await loadCycle(cx, companyId, uuid.parse(req.params.id), true);
      if (c.status !== 'draft') throw new ConflictError('this cycle was already launched');
      const f = c.filter ?? {};
      const emps = await cx.query<{ id: string; designation: string | null; grade: string | null; manager_id: string | null }>(
        `SELECT e.id, e.designation, e.grade, a.manager_id FROM employee e
           LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
          WHERE e.company_id = $1 AND e.status = 'active'
            AND (cardinality($2::uuid[]) = 0 OR a.org_unit_id = ANY($2::uuid[]))
            AND (cardinality($3::uuid[]) = 0 OR a.branch_id = ANY($3::uuid[]))
            AND ($4::date IS NULL OR e.join_date IS NULL OR e.join_date <= $4::date)`,
        [companyId, f.orgUnitIds ?? [], f.branchIds ?? [], f.joinedBefore ?? null]);
      if (!emps.rows.length) throw new BadRequestError('no active employees match this cycle\'s filter');
      const comps = await cx.query<{ id: string; name: string; kind: string; description: string | null; designations: string[]; grades: string[] }>(
        'SELECT id, name, kind, description, designations, grades FROM perf_competency WHERE company_id = $1 AND active', [companyId]);
      for (const e of emps.rows) {
        const rv = await cx.query<{ id: string }>(
          'INSERT INTO perf_review (cycle_id, company_id, employee_id, manager_employee_id) VALUES ($1, $2, $3, $4) RETURNING id', [c.id, companyId, e.id, e.manager_id]);
        const rid = rv.rows[0]!.id;
        await cx.query(
          `INSERT INTO perf_review_item (review_id, item_type, ref_id, title, detail, weight)
           SELECT $1, 'goal', g.id, g.title, g.description, CASE WHEN g.weight > 0 THEN g.weight ELSE 1 END FROM perf_goal g
            WHERE g.employee_id = $2 AND g.scope = 'individual' AND g.status IN ('active', 'completed')
              AND (g.due_date IS NULL OR g.due_date >= $3::date) AND (g.start_date IS NULL OR g.start_date <= $4::date)`, [rid, e.id, c.period_start, c.period_end]);
        for (const cp of comps.rows) {
          if (cp.designations.length && !(e.designation && cp.designations.includes(e.designation))) continue;
          if (cp.grades.length && !(e.grade && cp.grades.includes(e.grade))) continue;
          await cx.query(`INSERT INTO perf_review_item (review_id, item_type, ref_id, title, detail, weight) VALUES ($1, 'competency', $2, $3, $4, 1)`,
            [rid, cp.id, cp.name, `${cp.kind.charAt(0).toUpperCase()}${cp.kind.slice(1)}${cp.description ? `: ${cp.description}` : ''}`]);
        }
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, e.id), employeeId: e.id, template: 'review.self_open', title: `${c.name}: your self review is open`,
          body: c.self_due ? `Please complete it by ${bsLabel(c.self_due)}.` : 'Rate your goals and competencies and add a summary.', payload: { reviewId: rid } });
      }
      await cx.query(`UPDATE perf_cycle SET status = 'self_review', launched_at = now() WHERE id = $1`, [c.id]);
      await audit(cx, { actorId: u.id, action: 'launch', table: 'perf_cycle', rowId: c.id, after: { participants: emps.rows.length } });
      return emps.rows.length;
    });
    res.json({ ok: true, participants: out });
  });

  /** Move the cycle to its next phase. Releasing fixes final ratings, shows results and turns training needs into nominations. */
  r.post('/cycles/:id/advance', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req); const u = req.user!;
    const next = await tx(db, async (cx) => {
      const c = await loadCycle(cx, companyId, uuid.parse(req.params.id), true);
      if (c.status === 'draft') throw new ConflictError('launch the cycle first');
      if (c.status === 'closed') throw new ConflictError('this cycle is closed');
      let idx = PHASES.indexOf(c.status) + 1;
      if (PHASES[idx] === 'peer_review' && !c.peer_enabled) idx++;
      const to = PHASES[idx]!;
      await cx.query('UPDATE perf_cycle SET status = $2 WHERE id = $1', [c.id, to]);
      if (to === 'manager_review') {
        const mgrs = await cx.query<{ manager_employee_id: string }>(
          'SELECT DISTINCT manager_employee_id FROM perf_review WHERE cycle_id = $1 AND manager_employee_id IS NOT NULL AND manager_submitted_at IS NULL', [c.id]);
        for (const m of mgrs.rows) {
          await notify(cx, { companyId, userId: await userIdForEmployee(cx, m.manager_employee_id), employeeId: m.manager_employee_id, template: 'review.manager_open',
            title: `${c.name}: manager reviews are open`, body: c.manager_due ? `Review your team by ${bsLabel(c.manager_due)}.` : 'Review your team.', payload: { cycleId: c.id } });
        }
      }
      if (to === 'peer_review') {
        const peers = await cx.query<{ reviewer_employee_id: string }>(
          `SELECT DISTINCT p.reviewer_employee_id FROM perf_peer p JOIN perf_review r ON r.id = p.review_id WHERE r.cycle_id = $1 AND p.status = 'approved'`, [c.id]);
        for (const p of peers.rows) {
          await notify(cx, { companyId, userId: await userIdForEmployee(cx, p.reviewer_employee_id), employeeId: p.reviewer_employee_id, template: 'review.peer_open',
            title: `${c.name}: peer feedback requested`, body: 'Colleagues asked for your feedback. Open Growth, Reviews to respond.', payload: { cycleId: c.id } });
        }
      }
      if (to === 'calibration') {
        await notifyRole(cx, companyId, 'hr', { template: 'review.calibration', title: `${c.name}: ready for calibration`, body: 'Compare ratings across teams and set final ratings.', payload: { cycleId: c.id } });
      }
      if (to === 'released') {
        await cx.query(`UPDATE perf_review SET final_rating = coalesce(final_rating, manager_score), released_at = now() WHERE cycle_id = $1`, [c.id]);
        await cx.query('UPDATE perf_cycle SET released_at = now() WHERE id = $1', [c.id]);
        const rvs = await cx.query<{ id: string; employee_id: string; recommendation: { trainingCourseIds?: string[] } }>(
          'SELECT id, employee_id, recommendation FROM perf_review WHERE cycle_id = $1', [c.id]);
        for (const rv of rvs.rows) {
          if (rv.recommendation?.trainingCourseIds?.length) await createReviewNominations(cx, companyId, rv.employee_id, rv.id, rv.recommendation.trainingCourseIds, u.id);
          await notify(cx, { companyId, userId: await userIdForEmployee(cx, rv.employee_id), employeeId: rv.employee_id, template: 'review.released',
            title: `${c.name}: your review is ready`, body: 'Read your manager\'s review and acknowledge it.', payload: { reviewId: rv.id } });
        }
      }
      await audit(cx, { actorId: u.id, action: 'advance', table: 'perf_cycle', rowId: c.id, before: { status: c.status }, after: { status: to } });
      return to;
    });
    res.json({ ok: true, status: next });
  });

  r.get('/cycles/:id/calibration', requireRole('hr'), async (req, res) => {
    const c = await loadCycle(db, companyIdOf(req), uuid.parse(req.params.id));
    const rows = await db.query<{ id: string; employee_id: string; name_en: string; emp_code: string; department: string | null; manager_score: string | null; self_score: string | null; final_rating: string | null; calibration_reason: string | null; recommendation: unknown }>(
      `SELECT r.id, r.employee_id, e.name_en, e.emp_code, coalesce(ou.name_en, 'No department') AS department, r.self_score, r.manager_score, r.final_rating, r.calibration_reason, r.recommendation
         FROM perf_review r JOIN employee e ON e.id = r.employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = r.employee_id AND a.valid @> current_date
         LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
        WHERE r.cycle_id = $1 ORDER BY department, e.name_en`, [c.id]);
    const bucket = (v: string | null) => (v === null ? null : Math.min(5, Math.max(1, Math.round(Number(v)))));
    const distribution: Record<string, number[]> = {};
    const overall = [0, 0, 0, 0, 0];
    for (const row of rows.rows) {
      const b = bucket(row.final_rating ?? row.manager_score);
      if (b === null) continue;
      (distribution[row.department ?? 'No department'] ??= [0, 0, 0, 0, 0])[b - 1]!++;
      overall[b - 1]!++;
    }
    res.json({ cycle: c, reviews: rows.rows, distribution: { overall, byDepartment: distribution } });
  });

  // ---------------------------------------------------------------- reviews
  r.get('/reviews', async (req, res) => {
    const q = z.object({ scope: z.enum(['mine', 'team']).default('mine') }).parse(req.query);
    const u = req.user!;
    if (!u.employeeId) { res.json({ reviews: [] }); return; }
    const rows = await db.query(
      `SELECT r.id, r.cycle_id, c.name AS cycle_name, c.status AS cycle_status, c.period_start, c.period_end, c.self_due, c.manager_due, c.rating_labels,
              r.employee_id, e.name_en, e.emp_code, e.designation, r.self_submitted_at, r.manager_submitted_at, r.released_at, r.acknowledged_at,
              CASE WHEN $3 = 'team' OR r.released_at IS NOT NULL THEN r.final_rating END AS final_rating,
              CASE WHEN $3 = 'team' THEN r.manager_score END AS manager_score, r.self_score
         FROM perf_review r JOIN perf_cycle c ON c.id = r.cycle_id JOIN employee e ON e.id = r.employee_id
        WHERE r.company_id = $1 AND c.status <> 'draft' AND (CASE WHEN $3 = 'mine' THEN r.employee_id = $2 ELSE r.manager_employee_id = $2 END)
        ORDER BY c.created_at DESC, e.name_en`, [companyIdOf(req), u.employeeId, q.scope]);
    res.json({ reviews: rows.rows });
  });

  r.get('/reviews/:id', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    const role = await viewerRole(db, u, rv);
    if (!role || rv.cycle.status === 'draft') throw new NotFoundError('review not found');
    const full = await db.query(
      `SELECT r.*, e.name_en, e.emp_code, e.designation, m.name_en AS manager_name FROM perf_review r JOIN employee e ON e.id = r.employee_id
         LEFT JOIN employee m ON m.id = r.manager_employee_id WHERE r.id = $1`, [rv.id]);
    const row = full.rows[0] as Record<string, unknown>;
    const items = await db.query('SELECT id, item_type, ref_id, title, detail, weight, self_rating, self_comment, manager_rating, manager_comment FROM perf_review_item WHERE review_id = $1 ORDER BY item_type DESC, title', [rv.id]);
    const peers = await db.query<{ id: string; reviewer_employee_id: string; reviewer_name: string; status: string; rating: number | null; strengths: string | null; improvements: string | null; submitted_at: Date | null }>(
      `SELECT p.id, p.reviewer_employee_id, e.name_en AS reviewer_name, p.status, p.rating, p.strengths, p.improvements, p.submitted_at
         FROM perf_peer p JOIN employee e ON e.id = p.reviewer_employee_id WHERE p.review_id = $1 ORDER BY e.name_en`, [rv.id]);
    const released = !!rv.released_at;
    const phase = rv.cycle.status;
    if (role === 'self') {
      const showManager = released;
      res.json({
        role, cycle: rv.cycle,
        review: {
          id: row.id, employee_id: row.employee_id, name_en: row.name_en, emp_code: row.emp_code, designation: row.designation, manager_name: row.manager_name,
          self_summary: row.self_summary, self_submitted_at: row.self_submitted_at, self_score: row.self_score,
          manager_summary: showManager ? row.manager_summary : null, manager_submitted_at: row.manager_submitted_at,
          final_rating: showManager ? row.final_rating : null, released_at: row.released_at, acknowledged_at: row.acknowledged_at, employee_comment: row.employee_comment,
        },
        items: items.rows.map((i: Record<string, unknown>) => (showManager ? i : { ...i, manager_rating: null, manager_comment: null })),
        peers: { nominations: peers.rows.map((p) => ({ id: p.id, reviewer_name: p.reviewer_name, status: p.status === 'submitted' ? 'approved' : p.status })),
          aggregate: released ? peerAggregate(peers.rows, rv.cycle.peer_min) : null },
        journal: [],
        can: {
          editSelf: !rv.self_submitted_at && (phase === 'self_review' || phase === 'manager_review'),
          nominatePeers: rv.cycle.peer_enabled && ['self_review', 'manager_review', 'peer_review'].includes(phase),
          acknowledge: released && !rv.acknowledged_at, editManager: false, calibrate: false,
        },
      });
      return;
    }
    // Manager's journal: a manager sees their own notes for the period; HR sees everyone's.
    const journal = await db.query(
      `SELECT j.id, j.note_date, j.tag, j.note, u.name AS author_name FROM perf_journal j JOIN app_user u ON u.id = j.author_id
        WHERE j.employee_id = $1 AND j.note_date BETWEEN $2 AND $3 AND ($4 OR j.author_id = $5) ORDER BY j.note_date`,
      [rv.employee_id, rv.cycle.period_start, rv.cycle.period_end, role === 'hr', u.id]);
    const managerOpen = ['self_review', 'manager_review', 'peer_review'].includes(phase) && !rv.manager_submitted_at && (!!rv.self_submitted_at || phase !== 'self_review');
    res.json({
      role, cycle: rv.cycle, review: row, items: items.rows,
      peers: { nominations: peers.rows, aggregate: peerAggregate(peers.rows, 1) },
      journal: journal.rows,
      can: {
        editSelf: false,
        editManager: managerOpen && (role === 'hr' || rv.manager_employee_id === u.employeeId || !rv.manager_employee_id),
        nominatePeers: rv.cycle.peer_enabled && ['self_review', 'manager_review', 'peer_review'].includes(phase),
        calibrate: role === 'hr' && phase === 'calibration',
        acknowledge: false,
      },
    });
  });

  const saveRatings = async (cx: Queryable, reviewId: string, side: 'self' | 'manager', ratings: z.infer<typeof itemRatings>) => {
    for (const it of ratings) {
      const up = await cx.query(`UPDATE perf_review_item SET ${side}_rating = $3, ${side}_comment = $4 WHERE id = $1 AND review_id = $2`, [it.id, reviewId, it.rating, it.comment ?? null]);
      if (!up.rowCount) throw new BadRequestError('an item does not belong to this review');
    }
  };
  const scoreOf = async (cx: Queryable, reviewId: string, side: 'self' | 'manager', c: CycleRow, requireAll: boolean) => {
    const items = await cx.query<{ item_type: 'goal' | 'competency'; weight: string; rating: number | null }>(
      `SELECT item_type, weight, ${side}_rating AS rating FROM perf_review_item WHERE review_id = $1`, [reviewId]);
    if (requireAll && items.rows.some((i) => i.rating === null)) throw new BadRequestError('rate every goal and competency before submitting');
    return overallScore(items.rows, Number(c.goals_weight), Number(c.competency_weight));
  };

  r.put('/reviews/:id/self', async (req, res) => {
    const b = z.object({ items: itemRatings.default([]), summary: z.string().trim().max(4000).nullish(), submit: z.boolean().default(false) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    if (!u.employeeId || rv.employee_id !== u.employeeId) throw new NotFoundError('review not found');
    if (!['self_review', 'manager_review'].includes(rv.cycle.status)) throw new ConflictError('the self review is closed');
    if (rv.self_submitted_at) throw new ConflictError('you already submitted your self review');
    await tx(db, async (cx) => {
      await saveRatings(cx, rv.id, 'self', b.items);
      if (b.summary !== undefined) await cx.query('UPDATE perf_review SET self_summary = $2 WHERE id = $1', [rv.id, b.summary]);
      if (b.submit) {
        const score = await scoreOf(cx, rv.id, 'self', rv.cycle, true);
        await cx.query('UPDATE perf_review SET self_submitted_at = now(), self_score = $2 WHERE id = $1', [rv.id, score]);
        if (rv.manager_employee_id) {
          await notify(cx, { companyId, userId: await userIdForEmployee(cx, rv.manager_employee_id), employeeId: rv.manager_employee_id, template: 'review.self_submitted',
            title: 'Self review submitted', body: `${u.name} submitted their self review for ${rv.cycle.name}.`, payload: { reviewId: rv.id } });
        }
      }
    });
    res.json({ ok: true });
  });

  r.put('/reviews/:id/manager', async (req, res) => {
    const b = z.object({ items: itemRatings.default([]), summary: z.string().trim().max(4000).nullish(), recommendation: recommendation.optional(), submit: z.boolean().default(false) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    const role = await viewerRole(db, u, rv);
    if (!role || role === 'self') throw new NotFoundError('review not found');
    if (role === 'manager' && rv.manager_employee_id && rv.manager_employee_id !== u.employeeId) throw new ForbiddenError('the direct manager writes this review');
    const phase = rv.cycle.status;
    if (!['self_review', 'manager_review', 'peer_review'].includes(phase)) throw new ConflictError('manager reviews are closed');
    if (phase === 'self_review' && !rv.self_submitted_at) throw new ConflictError('wait for the self review, or until the manager review phase starts');
    if (rv.manager_submitted_at) throw new ConflictError('the manager review was already submitted');
    if (b.recommendation?.trainingCourseIds.length) {
      const ok = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM lrn_course WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, b.recommendation.trainingCourseIds]);
      if (ok.rows[0]!.n !== new Set(b.recommendation.trainingCourseIds).size) throw new BadRequestError('a recommended course was not found');
    }
    await tx(db, async (cx) => {
      await saveRatings(cx, rv.id, 'manager', b.items);
      if (b.summary !== undefined) await cx.query('UPDATE perf_review SET manager_summary = $2 WHERE id = $1', [rv.id, b.summary]);
      if (b.recommendation) await cx.query('UPDATE perf_review SET recommendation = $2 WHERE id = $1', [rv.id, JSON.stringify(b.recommendation)]);
      if (b.submit) {
        const score = await scoreOf(cx, rv.id, 'manager', rv.cycle, true);
        await cx.query('UPDATE perf_review SET manager_submitted_at = now(), manager_score = $2 WHERE id = $1', [rv.id, score]);
        await audit(cx, { actorId: u.id, action: 'submit', table: 'perf_review', rowId: rv.id, after: { managerScore: score } });
      }
    });
    res.json({ ok: true });
  });

  r.post('/reviews/:id/calibrate', requireRole('hr'), async (req, res) => {
    const b = z.object({ finalRating: z.coerce.number().min(1).max(5), reason: z.string().trim().min(5, 'explain the change in a few words').max(1000) }).parse(req.body);
    const rv = await loadReview(db, companyIdOf(req), uuid.parse(req.params.id));
    if (rv.cycle.status !== 'calibration') throw new ConflictError('final ratings are set during calibration');
    const fr = round2(b.finalRating);
    await tx(db, async (cx) => {
      await cx.query('UPDATE perf_review SET final_rating = $2, calibrated_by = $3, calibration_reason = $4 WHERE id = $1', [rv.id, fr, req.user!.id, b.reason]);
      await audit(cx, { actorId: req.user!.id, action: 'calibrate', table: 'perf_review', rowId: rv.id, before: { finalRating: rv.final_rating, managerScore: rv.manager_score }, after: { finalRating: fr, reason: b.reason } });
    });
    res.json({ ok: true });
  });

  r.post('/reviews/:id/acknowledge', async (req, res) => {
    const b = z.object({ comment: z.string().trim().max(2000).nullish() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    if (!u.employeeId || rv.employee_id !== u.employeeId) throw new NotFoundError('review not found');
    if (!rv.released_at) throw new ConflictError('the review has not been released yet');
    if (rv.acknowledged_at) throw new ConflictError('already acknowledged');
    await tx(db, async (cx) => {
      await cx.query('UPDATE perf_review SET acknowledged_at = now(), employee_comment = $2 WHERE id = $1', [rv.id, b.comment ?? null]);
      if (rv.manager_employee_id) {
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, rv.manager_employee_id), employeeId: rv.manager_employee_id, template: 'review.acknowledged',
          title: 'Review acknowledged', body: `${u.name} acknowledged their ${rv.cycle.name} review${b.comment ? ` with a comment` : ''}.`, payload: { reviewId: rv.id } });
      }
    });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- peers (360)
  r.post('/reviews/:id/peers', async (req, res) => {
    const b = z.object({ employeeIds: z.array(uuid).min(1).max(10) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    const role = await viewerRole(db, u, rv);
    if (!role) throw new NotFoundError('review not found');
    if (!rv.cycle.peer_enabled) throw new ConflictError('this cycle has no peer reviews');
    if (!['self_review', 'manager_review', 'peer_review'].includes(rv.cycle.status)) throw new ConflictError('peer nominations are closed');
    const status = role === 'self' ? 'nominated' : 'approved';
    let added = 0;
    await tx(db, async (cx) => {
      for (const id of new Set(b.employeeIds)) {
        if (id === rv.employee_id) throw new BadRequestError('you cannot review yourself');
        const e = await cx.query(`SELECT 1 FROM employee WHERE id = $1 AND company_id = $2 AND status = 'active'`, [id, companyId]);
        if (!e.rows[0]) throw new NotFoundError('colleague not found');
        const ins = await cx.query('INSERT INTO perf_peer (review_id, reviewer_employee_id, status, nominated_by) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING', [rv.id, id, status, u.id]);
        added += ins.rowCount ?? 0;
      }
      if (status === 'nominated' && added && rv.manager_employee_id) {
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, rv.manager_employee_id), employeeId: rv.manager_employee_id, template: 'review.peers_nominated',
          title: 'Peer reviewers to approve', body: `${u.name} nominated ${added} peer reviewer${added === 1 ? '' : 's'} for ${rv.cycle.name}.`, payload: { reviewId: rv.id } });
      }
    });
    res.status(201).json({ ok: true, added });
  });

  const loadPeer = async (companyId: string, id: string) => {
    const r0 = await db.query<{ id: string; review_id: string; reviewer_employee_id: string; status: string }>(
      'SELECT p.id, p.review_id, p.reviewer_employee_id, p.status FROM perf_peer p JOIN perf_review r ON r.id = p.review_id WHERE p.id = $1 AND r.company_id = $2', [id, companyId]);
    if (!r0.rows[0]) throw new NotFoundError('peer review not found');
    return r0.rows[0];
  };

  r.post('/peers/:id/decision', async (req, res) => {
    const b = z.object({ approve: z.boolean() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const p = await loadPeer(companyId, uuid.parse(req.params.id));
    const rv = await loadReview(db, companyId, p.review_id);
    const role = await viewerRole(db, u, rv);
    if (role !== 'manager' && role !== 'hr') throw new ForbiddenError('the manager approves peer reviewers');
    if (p.status !== 'nominated') throw new ConflictError(`already ${p.status}`);
    await tx(db, async (cx) => {
      await cx.query('UPDATE perf_peer SET status = $2 WHERE id = $1', [p.id, b.approve ? 'approved' : 'declined']);
      if (b.approve && ['manager_review', 'peer_review', 'self_review'].includes(rv.cycle.status)) {
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, p.reviewer_employee_id), employeeId: p.reviewer_employee_id, template: 'review.peer_requested',
          title: 'Peer feedback requested', body: `You were asked to give feedback in ${rv.cycle.name}. Open Growth, Reviews.`, payload: { peerId: p.id } });
      }
    });
    res.json({ ok: true });
  });

  r.delete('/peers/:id', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const p = await loadPeer(companyId, uuid.parse(req.params.id));
    const rv = await loadReview(db, companyId, p.review_id);
    const role = await viewerRole(db, u, rv);
    if (!role) throw new NotFoundError('peer review not found');
    if (p.status === 'submitted') throw new ConflictError('this colleague already responded');
    if (role === 'self' && p.status !== 'nominated') throw new ForbiddenError('ask your manager to remove an approved reviewer');
    await db.query('DELETE FROM perf_peer WHERE id = $1', [p.id]);
    res.json({ ok: true });
  });

  /** Peer feedback I have been asked to give. */
  r.get('/peer-requests', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) { res.json({ requests: [] }); return; }
    const rows = await db.query(
      `SELECT p.id, p.status, p.rating, p.strengths, p.improvements, p.submitted_at, e.name_en AS employee_name, e.designation, c.name AS cycle_name, c.status AS cycle_status, c.peer_due, c.rating_labels
         FROM perf_peer p JOIN perf_review r ON r.id = p.review_id JOIN perf_cycle c ON c.id = r.cycle_id JOIN employee e ON e.id = r.employee_id
        WHERE p.reviewer_employee_id = $1 AND r.company_id = $2 AND p.status IN ('approved', 'submitted') AND c.status NOT IN ('draft', 'closed')
        ORDER BY p.status, c.created_at DESC`, [u.employeeId, companyIdOf(req)]);
    res.json({ requests: rows.rows });
  });

  r.put('/peers/:id', async (req, res) => {
    const b = z.object({ rating: z.number().int().min(1).max(5), strengths: z.string().trim().max(2000).nullish(), improvements: z.string().trim().max(2000).nullish() }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    const p = await loadPeer(companyId, uuid.parse(req.params.id));
    if (!u.employeeId || p.reviewer_employee_id !== u.employeeId) throw new NotFoundError('peer review not found');
    if (p.status !== 'approved') throw new ConflictError(p.status === 'submitted' ? 'you already responded' : 'this request is not open');
    const rv = await loadReview(db, companyId, p.review_id);
    if (!['self_review', 'manager_review', 'peer_review'].includes(rv.cycle.status)) throw new ConflictError('peer feedback is closed');
    await db.query(`UPDATE perf_peer SET rating = $2, strengths = $3, improvements = $4, status = 'submitted', submitted_at = now() WHERE id = $1`,
      [p.id, b.rating, b.strengths ?? null, b.improvements ?? null]);
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- printable summary
  r.get('/reviews/:id/print', async (req, res) => {
    const u = req.user!; const companyId = companyIdOf(req);
    const rv = await loadReview(db, companyId, uuid.parse(req.params.id));
    const role = await viewerRole(db, u, rv);
    if (!role || rv.cycle.status === 'draft' || (role === 'self' && !rv.released_at)) throw new NotFoundError('review not found');
    const head = await db.query<{ name_en: string; emp_code: string; designation: string | null; manager_name: string | null; company: string; self_summary: string | null; manager_summary: string | null; final_rating: string | null; manager_score: string | null; self_score: string | null; recommendation: Record<string, unknown>; employee_comment: string | null; acknowledged_at: Date | null; calibration_reason: string | null }>(
      `SELECT e.name_en, e.emp_code, e.designation, m.name_en AS manager_name, co.name_en AS company, r.self_summary, r.manager_summary, r.final_rating, r.manager_score, r.self_score,
              r.recommendation, r.employee_comment, r.acknowledged_at, r.calibration_reason
         FROM perf_review r JOIN employee e ON e.id = r.employee_id JOIN company co ON co.id = r.company_id LEFT JOIN employee m ON m.id = r.manager_employee_id WHERE r.id = $1`, [rv.id]);
    const h = head.rows[0]!;
    const items = await db.query<{ item_type: string; title: string; weight: string; self_rating: number | null; manager_rating: number | null; manager_comment: string | null; self_comment: string | null }>(
      'SELECT item_type, title, weight, self_rating, self_comment, manager_rating, manager_comment FROM perf_review_item WHERE review_id = $1 ORDER BY item_type DESC, title', [rv.id]);
    const labels = rv.cycle.rating_labels;
    const label = (n: number | string | null) => (n === null ? '—' : `${Number(n).toFixed(n === null || Number.isInteger(Number(n)) ? 0 : 2)} ${esc(labels[Math.min(5, Math.max(1, Math.round(Number(n)))) - 1] ?? '')}`);
    const rows = (type: string) => items.rows.filter((i) => i.item_type === type).map((i) => `<tr><td>${esc(i.title)}${i.manager_comment ? `<div class="c">${esc(i.manager_comment)}</div>` : ''}</td>${type === 'goal' ? `<td class="n">${esc(Number(i.weight))}</td>` : ''}<td>${label(i.self_rating)}</td><td>${label(i.manager_rating)}</td></tr>`).join('');
    const rec = role === 'self' ? null : h.recommendation;
    const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Review ${esc(h.emp_code)} ${esc(rv.cycle.name)}</title><style>
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px;color:#17191e;margin:32px auto;max-width:860px;line-height:1.55;padding:0 24px}
h1{font-size:20px;margin:0;font-weight:600}h2{font-size:14px;margin:24px 0 8px}.sub{color:#6b707b}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 24px;margin:18px 0}
.meta span{display:block;color:#6b707b;font-size:11.5px}table{width:100%;border-collapse:collapse}th{text-align:left;font-weight:500;color:#6b707b;font-size:12px;border-bottom:1px solid #e6e7ea;padding:6px 4px}
td{padding:6px 4px;border-bottom:1px solid #f0f0f2;vertical-align:top}.n{text-align:right}.c{color:#6b707b;font-size:12px}.box{border:1px solid #e6e7ea;border-radius:10px;padding:12px 14px;white-space:pre-wrap}
.final{font-size:22px;font-weight:600;color:#12773a}.sign{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;color:#6b707b}
@media print{body{margin:0 auto}}</style></head><body>
<div class="sub">${esc(h.company)}</div><h1>${esc(rv.cycle.name)}: performance review</h1>
<div class="meta"><div><span>Employee</span>${esc(h.name_en)} (${esc(h.emp_code)})</div><div><span>Designation</span>${esc(h.designation ?? '—')}</div><div><span>Manager</span>${esc(h.manager_name ?? '—')}</div>
<div><span>Period</span>${esc(bsLabel(rv.cycle.period_start))} to ${esc(bsLabel(rv.cycle.period_end))}</div><div><span>Self score</span>${label(h.self_score)}</div><div><span>Final rating</span><span class="final">${label(h.final_rating ?? h.manager_score)}</span></div></div>
<h2>Goals (${esc(Number(rv.cycle.goals_weight))}%)</h2><table><thead><tr><th>Goal</th><th class="n">Weight</th><th>Self</th><th>Manager</th></tr></thead><tbody>${rows('goal') || '<tr><td colspan="4" class="c">No goals in this review</td></tr>'}</tbody></table>
<h2>Competencies (${esc(Number(rv.cycle.competency_weight))}%)</h2><table><thead><tr><th>Competency</th><th>Self</th><th>Manager</th></tr></thead><tbody>${rows('competency') || '<tr><td colspan="3" class="c">No competencies in this review</td></tr>'}</tbody></table>
<h2>Employee's summary</h2><div class="box">${esc(h.self_summary ?? '—')}</div>
<h2>Manager's summary</h2><div class="box">${esc(h.manager_summary ?? '—')}</div>
${rec ? `<h2>Recommendation</h2><div class="box">${[rec.incrementPct != null ? `Increment ${esc(rec.incrementPct)}%` : null, rec.promotion ? 'Promotion' : null, rec.pip ? 'Performance improvement plan' : null, rec.notes ? esc(rec.notes) : null].filter(Boolean).join('\n') || '—'}</div>` : ''}
${role === 'hr' && h.calibration_reason ? `<h2>Calibration note</h2><div class="box">${esc(h.calibration_reason)}</div>` : ''}
${h.acknowledged_at ? `<h2>Employee's acknowledgement</h2><div class="box">Acknowledged on ${esc(bsLabel(h.acknowledged_at))}${h.employee_comment ? `\n${esc(h.employee_comment)}` : ''}</div>` : ''}
<div class="sign"><div>Employee</div><div>Manager</div><div>HR</div></div></body></html>`;
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.setHeader('Cache-Control', 'private, no-store');
    res.send(html);
  });
}
