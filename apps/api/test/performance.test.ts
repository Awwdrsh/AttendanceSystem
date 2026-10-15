import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { performanceReminders } from '../src/modules/performance/jobs.js';
import { goalProgress, keyResultProgress, overallScore } from '../src/modules/performance/service.js';
import { peerAggregate } from '../src/modules/performance/reviews.js';
import { addMonths } from '../src/modules/learning/service.js';
import { auth, DEMO, freshDb, loginAs, TEST_PEPPER, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let hari: string; let gita: string; let otherHr: string;
let HARI = ''; let GITA = ''; let OTHER_EMP = '';
const P = '/api/v1/performance';
const L = '/api/v1/learning';
const today = () => new Date(Date.now() + 345 * 60_000).toISOString().slice(0, 10);
const plusDays = (n: number) => new Date(Date.now() + 345 * 60_000 + n * 86_400_000).toISOString().slice(0, 10);
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;

async function addEmployee(code: string, name: string, managerId: string | null, companyId = DEMO.companyId): Promise<string> {
  const e = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, designation) VALUES ($1, $2, $3, 'Engineer') RETURNING id`, [companyId, code, name]);
  const id = e.rows[0]!.id;
  if (companyId === DEMO.companyId) {
    await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, manager_id, employment_type) VALUES ($1, daterange('2020-01-01', NULL), $2, $3, 'regular')`, [id, DEMO.branchId, managerId]);
  }
  return id;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`UPDATE employee SET designation = 'Engineer' WHERE id IN ($1, $2)`, [DEMO.sita, DEMO.ram]);
  HARI = await addEmployee('E101', 'Hari Karki', DEMO.ram);
  GITA = await addEmployee('E102', 'Gita Rai', DEMO.ram);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  hari = (await loginAs(db, app, ['employee'], HARI)).token;
  gita = (await loginAs(db, app, ['employee'], GITA)).token;
  const other = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERP', 'Other Co') RETURNING id`);
  OTHER_EMP = await addEmployee('X1', 'Foreign', null, other.rows[0]!.id);
  await createUser(db, { companyId: other.rows[0]!.id, email: 'hr@otherp.local', name: 'Other HR', password: 'a-long-enough-password', roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@otherp.local', password: 'a-long-enough-password' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('scoring helpers', () => {
  it('computes key result progress for rising, falling and yes/no targets', () => {
    expect(keyResultProgress({ measure: 'number', start_value: 0, target_value: 10, current_value: 5, weight: 1 })).toBe(50);
    expect(keyResultProgress({ measure: 'number', start_value: 20, target_value: 5, current_value: 8, weight: 1 })).toBe(80);
    expect(keyResultProgress({ measure: 'percent', start_value: 0, target_value: 100, current_value: 140, weight: 1 })).toBe(100);
    expect(keyResultProgress({ measure: 'boolean', start_value: 0, target_value: 1, current_value: 1, weight: 1 })).toBe(100);
    expect(goalProgress([{ measure: 'number', start_value: 0, target_value: 10, current_value: 10, weight: 3 }, { measure: 'number', start_value: 0, target_value: 10, current_value: 0, weight: 1 }])).toBe(75);
    expect(goalProgress([])).toBeNull();
  });
  it('blends goal and competency ratings with the cycle weights', () => {
    const items = [{ item_type: 'goal' as const, weight: 2, rating: 5 }, { item_type: 'goal' as const, weight: 1, rating: 2 }, { item_type: 'competency' as const, weight: 1, rating: 3 }];
    expect(overallScore(items, 60, 40)).toBe(3.6);
    expect(overallScore(items.filter((i) => i.item_type === 'competency'), 60, 40)).toBe(3);
    expect(overallScore([], 60, 40)).toBeNull();
  });
  it('hides peer feedback below the minimum and never names reviewers', () => {
    const peers = [{ status: 'submitted', rating: 4, strengths: 'b', improvements: null }, { status: 'submitted', rating: 2, strengths: 'a', improvements: 'x' }, { status: 'approved', rating: null, strengths: null, improvements: null }];
    expect(peerAggregate(peers, 3)).toEqual({ count: 2, shown: false });
    expect(peerAggregate(peers, 2)).toEqual({ count: 2, shown: true, averageRating: 3, strengths: ['a', 'b'], improvements: ['x'] });
  });
  it('adds months clamped to month end', () => {
    expect(addMonths('2026-01-31', 1)).toBe('2026-02-28');
    expect(addMonths('2026-10-15', 12)).toBe('2027-10-15');
  });
});

let sitaGoal = '';
describe('goals and key results', () => {
  it('an employee sets a goal with key results; check-ins move progress and tell the manager', async () => {
    const r = await request(app).post(`${P}/goals`).set(auth(sita)).send({
      title: 'Ship the attendance app', weight: 60, keyResults: [
        { title: 'Screens done', measure: 'number', startValue: 0, targetValue: 10, currentValue: 5 },
        { title: 'Released', measure: 'boolean' },
      ],
    });
    expect(r.status).toBe(201);
    sitaGoal = r.body.id;
    const g = await request(app).get(`${P}/goals/${sitaGoal}`).set(auth(sita));
    expect(Number(g.body.goal.progress)).toBe(25);
    const kr = g.body.goal.key_results.find((k: { title: string }) => k.title === 'Released');
    const before = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'goal.checkin'`);
    const c = await request(app).post(`${P}/goals/${sitaGoal}/checkins`).set(auth(sita)).send({ keyResultId: kr.id, value: 1, confidence: 3, comment: 'Live' });
    expect(c.status).toBe(201);
    const g2 = await request(app).get(`${P}/goals/${sitaGoal}`).set(auth(sita));
    expect(Number(g2.body.goal.progress)).toBe(75);
    expect(g2.body.checkins).toHaveLength(1);
    const after = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'goal.checkin'`);
    expect(after.rows[0]!.n).toBe(before.rows[0]!.n + 1);
    // Manual progress is refused when key results drive it.
    expect((await request(app).patch(`${P}/goals/${sitaGoal}`).set(auth(sita)).send({ progress: 90 })).status).toBe(400);
  });

  it('validates input', async () => {
    expect((await request(app).post(`${P}/goals`).set(auth(sita)).send({ title: '' })).status).toBe(400);
    expect((await request(app).post(`${P}/goals`).set(auth(sita)).send({ title: 'x', startDate: '2026-10-10', dueDate: '2026-10-01' })).status).toBe(400);
    expect((await request(app).post(`${P}/goals/${sitaGoal}/checkins`).set(auth(sita)).send({})).status).toBe(400);
  });

  it('managers set goals for their team; colleagues and other companies cannot see them', async () => {
    const r = await request(app).post(`${P}/goals`).set(auth(ram)).send({ employeeId: DEMO.sita, title: 'Mentor Hari', status: 'active' });
    expect(r.status).toBe(201);
    expect((await request(app).post(`${P}/goals`).set(auth(hari)).send({ employeeId: DEMO.sita, title: 'Nope' })).status).toBe(403);
    expect((await request(app).get(`${P}/goals/${sitaGoal}`).set(auth(hari))).status).toBe(404);
    expect((await request(app).get(`${P}/goals`).set(auth(hari)).query({ scope: 'employee', employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get(`${P}/goals/${sitaGoal}`).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).post(`${P}/goals`).set(auth(hr)).send({ employeeId: OTHER_EMP, title: 'x' })).status).toBe(404);
    const team = await request(app).get(`${P}/goals`).set(auth(ram)).query({ scope: 'team' });
    expect(team.body.goals.map((g: { id: string }) => g.id)).toContain(sitaGoal);
    expect((await request(app).get(`${P}/goals`).set(auth(hari)).query({ scope: 'team' })).body.goals).toHaveLength(0);
    // Manager can edit the report's goal; a colleague cannot.
    expect((await request(app).patch(`${P}/goals/${sitaGoal}`).set(auth(ram)).send({ weight: 50 })).status).toBe(200);
    expect((await request(app).patch(`${P}/goals/${sitaGoal}`).set(auth(hari)).send({ weight: 50 })).status).toBe(404);
  });

  it('company goals are set by HR and visible to everyone; personal goals align to them', async () => {
    expect((await request(app).post(`${P}/goals`).set(auth(sita)).send({ scope: 'company', title: 'Grow 30%' })).status).toBe(403);
    const c = await request(app).post(`${P}/goals`).set(auth(hr)).send({ scope: 'company', title: 'Grow 30%' });
    expect(c.status).toBe(201);
    const list = await request(app).get(`${P}/goals`).set(auth(hari)).query({ scope: 'company' });
    expect(list.body.goals.map((g: { id: string }) => g.id)).toContain(c.body.id);
    const al = await request(app).post(`${P}/goals`).set(auth(hari)).send({ title: 'Close 10 deals', parentId: c.body.id });
    expect(al.status).toBe(201);
    const d = await request(app).get(`${P}/goals/${c.body.id}`).set(auth(hr));
    expect(d.body.children.map((x: { id: string }) => x.id)).toContain(al.body.id);
    // Cannot align to someone else's private goal.
    expect((await request(app).post(`${P}/goals`).set(auth(hari)).send({ title: 'x', parentId: sitaGoal })).status).toBe(404);
  });

  it('the KPI library is managed by HR and turns into goals', async () => {
    expect((await request(app).post(`${P}/kpis`).set(auth(sita)).send({ title: 'x' })).status).toBe(403);
    const k = await request(app).post(`${P}/kpis`).set(auth(hr)).send({ designation: 'Engineer', title: 'Bugs fixed per month', defaultTarget: 20 });
    expect(k.status).toBe(201);
    const lib = await request(app).get(`${P}/kpis`).set(auth(sita)).query({ designation: 'Engineer' });
    expect(lib.body.kpis).toHaveLength(1);
    const g = await request(app).post(`${P}/kpis/${k.body.id}/apply`).set(auth(sita)).send({ weight: 40 });
    expect(g.status).toBe(201);
    const goal = await request(app).get(`${P}/goals/${g.body.id}`).set(auth(sita));
    expect(Number(goal.body.goal.key_results[0].target_value)).toBe(20);
    expect((await request(app).post(`${P}/kpis/${k.body.id}/apply`).set(auth(hari)).send({ employeeId: DEMO.sita })).status).toBe(403);
  });
});

let cycleId = ''; let sitaReview = ''; let courseForReview = '';
describe('review cycles', () => {
  it('HR defines competencies and a cycle; weights must add up to 100', async () => {
    expect((await request(app).post(`${P}/competencies`).set(auth(sita)).send({ kind: 'skill', name: 'x' })).status).toBe(403);
    const c = await request(app).post(`${P}/competencies`).set(auth(hr)).send({ kind: 'skill', name: 'Problem solving', indicators: [{ level: 3, text: 'Solves routine problems alone' }] });
    expect(c.status).toBe(201);
    await request(app).post(`${P}/competencies`).set(auth(hr)).send({ kind: 'knowledge', name: 'Finance', designations: ['Accountant'] });
    const bad = await request(app).post(`${P}/cycles`).set(auth(hr)).send({ name: 'FY 2083', periodStart: '2026-07-17', periodEnd: '2027-07-16', goalsWeight: 70, competencyWeight: 40 });
    expect(bad.status).toBe(400);
    const course = await request(app).post(`${L}/courses`).set(auth(hr)).send({ code: 'LEAD', title: 'Leadership basics', cost: '5000.00' });
    courseForReview = course.body.id;
    const ok = await request(app).post(`${P}/cycles`).set(auth(hr)).send({ name: 'FY 2083', periodStart: '2026-01-01', periodEnd: plusDays(200), peerEnabled: true, peerMin: 2, selfDue: plusDays(3) });
    expect(ok.status).toBe(201);
    cycleId = ok.body.id;
    expect((await request(app).get(`${P}/cycles/${cycleId}`).set(auth(sita))).status).toBe(404);
  });

  it('launch creates a review per employee with their goals and the competencies of their role', async () => {
    const l = await request(app).post(`${P}/cycles/${cycleId}/launch`).set(auth(hr));
    expect(l.status).toBe(200);
    expect(l.body.participants).toBe(4);
    expect((await request(app).post(`${P}/cycles/${cycleId}/launch`).set(auth(hr))).status).toBe(409);
    expect((await request(app).patch(`${P}/cycles/${cycleId}`).set(auth(hr)).send({ goalsWeight: 50, competencyWeight: 50 })).status).toBe(409);
    const mine = await request(app).get(`${P}/reviews`).set(auth(sita));
    sitaReview = mine.body.reviews[0].id;
    const rv = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(sita));
    expect(rv.body.role).toBe('self');
    expect(rv.body.can.editSelf).toBe(true);
    const types = rv.body.items.map((i: { item_type: string; title: string }) => `${i.item_type}:${i.title}`);
    expect(types).toContain('goal:Ship the attendance app');
    expect(types).toContain('competency:Problem solving');
    expect(types).not.toContain('competency:Finance');
    expect((await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(ram))).body.role).toBe('manager');
    expect((await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(hari))).status).toBe(404);
    expect((await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(otherHr))).status).toBe(404);
  });

  it('self review first: the manager waits, incomplete ratings are refused', async () => {
    const rv = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(sita));
    expect((await request(app).put(`${P}/reviews/${sitaReview}/manager`).set(auth(ram)).send({ summary: 'early' })).status).toBe(409);
    expect((await request(app).put(`${P}/reviews/${sitaReview}/self`).set(auth(ram)).send({ summary: 'x' })).status).toBe(404);
    const partial = await request(app).put(`${P}/reviews/${sitaReview}/self`).set(auth(sita)).send({ items: [{ id: rv.body.items[0].id, rating: 4 }], submit: true });
    expect(partial.status).toBe(400);
    const all = rv.body.items.map((i: { id: string }) => ({ id: i.id, rating: 4, comment: 'Did well' }));
    const ok = await request(app).put(`${P}/reviews/${sitaReview}/self`).set(auth(sita)).send({ items: all, summary: 'Good year', submit: true });
    expect(ok.status).toBe(200);
    expect((await request(app).put(`${P}/reviews/${sitaReview}/self`).set(auth(sita)).send({ summary: 'again' })).status).toBe(409);
    const row = await db.query<{ self_score: string }>('SELECT self_score FROM perf_review WHERE id = $1', [sitaReview]);
    expect(Number(row.rows[0]!.self_score)).toBe(4);
  });

  it('peers are nominated by the employee and approved by the manager', async () => {
    expect((await request(app).post(`${P}/reviews/${sitaReview}/peers`).set(auth(sita)).send({ employeeIds: [DEMO.sita] })).status).toBe(400);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/peers`).set(auth(sita)).send({ employeeIds: [OTHER_EMP] })).status).toBe(404);
    const n = await request(app).post(`${P}/reviews/${sitaReview}/peers`).set(auth(sita)).send({ employeeIds: [HARI, GITA] });
    expect(n.body.added).toBe(2);
    const view = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(ram));
    for (const p of view.body.peers.nominations) {
      expect((await request(app).post(`${P}/peers/${p.id}/decision`).set(auth(sita)).send({ approve: true })).status).toBe(403);
      expect((await request(app).post(`${P}/peers/${p.id}/decision`).set(auth(ram)).send({ approve: true })).status).toBe(200);
    }
    const reqs = await request(app).get(`${P}/peer-requests`).set(auth(hari));
    expect(reqs.body.requests).toHaveLength(1);
    expect(reqs.body.requests[0].employee_name).toBe('Sita Sharma');
  });

  it('manager review with a recommendation, then peers respond', async () => {
    expect((await request(app).post(`${P}/cycles/${cycleId}/advance`).set(auth(hr))).body.status).toBe('manager_review');
    const rv = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(ram));
    expect(rv.body.can.editManager).toBe(true);
    const items = rv.body.items.map((i: { id: string; item_type: string }) => ({ id: i.id, rating: i.item_type === 'goal' ? 5 : 3, comment: 'Solid' }));
    expect((await request(app).put(`${P}/reviews/${sitaReview}/manager`).set(auth(hari)).send({ items })).status).toBe(404);
    const bad = await request(app).put(`${P}/reviews/${sitaReview}/manager`).set(auth(ram)).send({ items, recommendation: { trainingCourseIds: ['00000000-0000-7000-8000-00000000ffff'] } });
    expect(bad.status).toBe(400);
    const ok = await request(app).put(`${P}/reviews/${sitaReview}/manager`).set(auth(ram)).send({
      items, summary: 'Strong delivery', submit: true, recommendation: { incrementPct: 10, promotion: true, trainingCourseIds: [courseForReview] },
    });
    expect(ok.status).toBe(200);
    const row = await db.query<{ manager_score: string }>('SELECT manager_score FROM perf_review WHERE id = $1', [sitaReview]);
    expect(Number(row.rows[0]!.manager_score)).toBe(4.2);   // goals 5 × 60% + competency 3 × 40%
    expect((await request(app).post(`${P}/cycles/${cycleId}/advance`).set(auth(hr))).body.status).toBe('peer_review');
    for (const [tok, rating] of [[hari, 4], [gita, 5]] as const) {
      const pr = (await request(app).get(`${P}/peer-requests`).set(auth(tok))).body.requests[0];
      expect((await request(app).put(`${P}/peers/${pr.id}`).set(auth(tok)).send({ rating, strengths: `strength ${rating}` })).status).toBe(200);
      expect((await request(app).put(`${P}/peers/${pr.id}`).set(auth(tok)).send({ rating })).status).toBe(409);
    }
    // Before release the employee sees neither manager ratings nor peer feedback.
    const self = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(sita));
    expect(self.body.review.final_rating).toBeNull();
    expect(self.body.items.every((i: { manager_rating: unknown }) => i.manager_rating === null)).toBe(true);
    expect(self.body.peers.aggregate).toBeNull();
  });

  it('calibration needs a reason and is audited; release shows results and creates training', async () => {
    expect((await request(app).post(`${P}/reviews/${sitaReview}/calibrate`).set(auth(hr)).send({ finalRating: 4, reason: 'adjust' })).status).toBe(409);
    expect((await request(app).post(`${P}/cycles/${cycleId}/advance`).set(auth(hr))).body.status).toBe('calibration');
    const cal = await request(app).get(`${P}/cycles/${cycleId}/calibration`).set(auth(hr));
    expect(cal.body.distribution.overall[3]).toBe(1);
    expect((await request(app).get(`${P}/cycles/${cycleId}/calibration`).set(auth(ram))).status).toBe(403);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/calibrate`).set(auth(hr)).send({ finalRating: 4 })).status).toBe(400);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/calibrate`).set(auth(ram)).send({ finalRating: 4, reason: 'Bring in line' })).status).toBe(403);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/calibrate`).set(auth(hr)).send({ finalRating: 4, reason: 'Bring in line with peers' })).status).toBe(200);
    const a = await db.query(`SELECT 1 FROM audit_log WHERE table_name = 'perf_review' AND action = 'calibrate' AND row_id = $1`, [sitaReview]);
    expect(a.rows).toHaveLength(1);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/acknowledge`).set(auth(sita)).send({})).status).toBe(409);
    expect((await request(app).post(`${P}/cycles/${cycleId}/advance`).set(auth(hr))).body.status).toBe('released');
    const self = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(sita));
    expect(Number(self.body.review.final_rating)).toBe(4);
    expect(self.body.review.recommendation).toBeUndefined();
    expect(self.body.peers.aggregate).toMatchObject({ shown: true, count: 2, averageRating: 4.5 });
    expect(JSON.stringify(self.body.peers.aggregate)).not.toMatch(/Hari|Gita|reviewer/);
    expect(self.body.peers.nominations.every((p: { status: string }) => p.status !== 'submitted')).toBe(true);
    const enr = await db.query<{ status: string; source: string }>('SELECT status, source FROM lrn_enrollment WHERE employee_id = $1 AND course_id = $2', [DEMO.sita, courseForReview]);
    expect(enr.rows[0]).toEqual({ status: 'enrolled', source: 'review' });
    expect((await request(app).post(`${P}/reviews/${sitaReview}/acknowledge`).set(auth(sita)).send({ comment: 'Thanks' })).status).toBe(200);
    expect((await request(app).post(`${P}/reviews/${sitaReview}/acknowledge`).set(auth(sita)).send({})).status).toBe(409);
  });

  it('prints a summary; the employee copy leaves out the recommendation', async () => {
    const mine = await request(app).get(`${P}/reviews/${sitaReview}/print`).set(auth(sita));
    expect(mine.status).toBe(200);
    expect(mine.text).toContain('Sita Sharma');
    expect(mine.text).not.toContain('Recommendation');
    const mgr = await request(app).get(`${P}/reviews/${sitaReview}/print`).set(auth(ram));
    expect(mgr.text).toContain('Increment 10%');
    expect((await request(app).get(`${P}/reviews/${sitaReview}/print`).set(auth(hari))).status).toBe(404);
  });
});

describe("manager's journal, feedback and 1-on-1s", () => {
  it('the journal is private to its author and HR', async () => {
    expect((await request(app).post(`${P}/journal`).set(auth(hari)).send({ employeeId: DEMO.sita, noteDate: today(), note: 'x' })).status).toBe(403);
    const j = await request(app).post(`${P}/journal`).set(auth(ram)).send({ employeeId: DEMO.sita, noteDate: today(), tag: 'positive', note: 'Handled the outage calmly' });
    expect(j.status).toBe(201);
    expect((await request(app).get(`${P}/journal`).set(auth(sita)).query({ employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get(`${P}/journal`).set(auth(ram)).query({ employeeId: DEMO.sita })).body.entries).toHaveLength(1);
    expect((await request(app).get(`${P}/journal`).set(auth(hr)).query({ employeeId: DEMO.sita })).body.entries).toHaveLength(1);
    expect((await request(app).get(`${P}/journal`).set(auth(otherHr)).query({ employeeId: DEMO.sita })).status).toBe(404);
    const rv = await request(app).get(`${P}/reviews/${sitaReview}`).set(auth(ram));
    expect(rv.body.journal.map((x: { note: string }) => x.note)).toContain('Handled the outage calmly');
    expect((await request(app).delete(`${P}/journal/${j.body.id}`).set(auth(hr))).status).toBe(404);
  });

  it('feedback: private-to-manager notes are hidden from the subject', async () => {
    expect((await request(app).post(`${P}/feedback`).set(auth(sita)).send({ toEmployeeId: DEMO.sita, body: 'me' })).status).toBe(400);
    await request(app).post(`${P}/feedback`).set(auth(hari)).send({ toEmployeeId: DEMO.sita, body: 'Great code reviews' }).expect(201);
    await request(app).post(`${P}/feedback`).set(auth(gita)).send({ toEmployeeId: DEMO.sita, body: 'Misses standups', privateToManager: true }).expect(201);
    const recv = await request(app).get(`${P}/feedback`).set(auth(sita)).query({ box: 'received' });
    expect(recv.body.feedback.map((f: { body: string }) => f.body)).toEqual(['Great code reviews']);
    const about = await request(app).get(`${P}/feedback`).set(auth(ram)).query({ box: 'about', employeeId: DEMO.sita });
    expect(about.body.feedback).toHaveLength(2);
    expect((await request(app).get(`${P}/feedback`).set(auth(hari)).query({ box: 'about', employeeId: DEMO.sita })).status).toBe(403);
  });

  it('feedback requests are answered or declined by the colleague asked', async () => {
    await request(app).post(`${P}/feedback/requests`).set(auth(sita)).send({ fromEmployeeIds: [GITA], prompt: 'How was the launch?' }).expect(201);
    expect((await request(app).post(`${P}/feedback/requests`).set(auth(hari)).send({ fromEmployeeIds: [GITA], aboutEmployeeId: DEMO.sita })).status).toBe(403);
    const inbox = await request(app).get(`${P}/feedback`).set(auth(gita)).query({ box: 'requests' });
    const reqId = inbox.body.feedback[0].id;
    expect((await request(app).put(`${P}/feedback/${reqId}`).set(auth(hari)).send({ body: 'x' })).status).toBe(404);
    await request(app).put(`${P}/feedback/${reqId}`).set(auth(gita)).send({ body: 'Smooth launch' }).expect(200);
    expect((await request(app).post(`${P}/feedback/${reqId}/decline`).set(auth(gita))).status).toBe(409);
    const mine = await request(app).get(`${P}/feedback`).set(auth(sita)).query({ box: 'requests' });
    expect(mine.body.feedback[0]).toMatchObject({ status: 'given', body: 'Smooth launch' });
  });

  it('1-on-1s belong to the two people; private notes stay on each side; recurring meetings roll forward', async () => {
    expect((await request(app).post(`${P}/one-on-ones`).set(auth(hari)).send({ withEmployeeId: DEMO.sita, scheduledAt: new Date(Date.now() + 86_400_000).toISOString() })).status).toBe(403);
    const m = await request(app).post(`${P}/one-on-ones`).set(auth(ram)).send({ withEmployeeId: DEMO.sita, scheduledAt: new Date(Date.now() + 86_400_000).toISOString(), cadence: 'weekly', agenda: ['Career plan'] });
    expect(m.status).toBe(201);
    expect((await request(app).get(`${P}/one-on-ones/${m.body.id}`).set(auth(hari))).status).toBe(404);
    expect((await request(app).get(`${P}/one-on-ones/${m.body.id}`).set(auth(hr))).status).toBe(403);
    await request(app).patch(`${P}/one-on-ones/${m.body.id}`).set(auth(ram)).send({ privateNotes: 'Considering promotion' }).expect(200);
    await request(app).patch(`${P}/one-on-ones/${m.body.id}`).set(auth(sita)).send({ privateNotes: 'Ask about training budget', sharedNotes: 'Agreed on Q2 focus' }).expect(200);
    const asSita = await request(app).get(`${P}/one-on-ones/${m.body.id}`).set(auth(sita));
    expect(asSita.body.meeting.private_notes).toBe('Ask about training budget');
    expect(JSON.stringify(asSita.body)).not.toContain('Considering promotion');
    expect((await request(app).post(`${P}/one-on-ones/${m.body.id}/items`).set(auth(sita)).send({ kind: 'action', text: 'Draft plan', ownerEmployeeId: HARI })).status).toBe(400);
    await request(app).post(`${P}/one-on-ones/${m.body.id}/items`).set(auth(sita)).send({ kind: 'action', text: 'Draft plan', ownerEmployeeId: DEMO.sita }).expect(201);
    const done = await request(app).patch(`${P}/one-on-ones/${m.body.id}`).set(auth(ram)).send({ status: 'done' });
    expect(done.body.nextId).toBeTruthy();
    const next = await request(app).get(`${P}/one-on-ones/${done.body.nextId}`).set(auth(ram));
    expect(next.body.openActions.map((a: { text: string }) => a.text)).toContain('Draft plan');
  });
});

describe('learning', () => {
  let online = ''; let classroom = ''; let sitaEnr = '';
  it('HR builds the catalogue; employees cannot', async () => {
    expect((await request(app).post(`${L}/courses`).set(auth(sita)).send({ code: 'X', title: 'x' })).status).toBe(403);
    const c = await request(app).post(`${L}/courses`).set(auth(hr)).send({ code: 'SAFE', title: 'Fire safety', delivery: 'online', mandatory: true, validityMonths: 12, passMark: 60, cost: '1000' });
    online = c.body.id;
    await request(app).put(`${L}/courses/${online}/questions`).set(auth(hr)).send({ questions: [
      { prompt: 'Extinguisher for electrical fire?', options: ['Water', 'CO2'], correctIndex: 1 },
      { prompt: 'Assembly point?', options: ['Car park', 'Roof'], correctIndex: 0 },
    ] }).expect(200);
    expect((await request(app).put(`${L}/courses/${online}/questions`).set(auth(hr)).send({ questions: [{ prompt: 'x', options: ['a', 'b'], correctIndex: 5 }] })).status).toBe(400);
    const cl = await request(app).post(`${L}/courses`).set(auth(hr)).send({ code: 'EXCEL', title: 'Excel for HR', delivery: 'classroom', cost: '2500' });
    classroom = cl.body.id;
    const learner = await request(app).get(`${L}/courses/${online}`).set(auth(sita));
    expect(learner.body.questions).toEqual([]);
    expect(learner.body.questionCount).toBe(2);
  });

  it('self-nomination goes to the manager; the assessment completes the course and issues a certificate', async () => {
    const n = await request(app).post(`${L}/enrollments/self`).set(auth(sita)).send({ courseId: online, reason: 'Required' });
    expect(n.status).toBe(201);
    sitaEnr = n.body.id;
    expect((await request(app).post(`${L}/enrollments/self`).set(auth(sita)).send({ courseId: online })).status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === sitaEnr);
    expect(item.summary.course_title).toBe('Fire safety');
    await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(ram)).send({}).expect(200);
    const a = await request(app).get(`${L}/enrollments/${sitaEnr}/assessment`).set(auth(sita));
    expect(a.body.questions).toHaveLength(2);
    expect(JSON.stringify(a.body)).not.toContain('correct');
    expect((await request(app).get(`${L}/enrollments/${sitaEnr}/assessment`).set(auth(hari))).status).toBe(404);
    expect((await request(app).post(`${L}/enrollments/${sitaEnr}/complete`).set(auth(sita))).status).toBe(409);
    expect((await request(app).post(`${L}/enrollments/${sitaEnr}/attempts`).set(auth(sita)).send({ answers: [1] })).status).toBe(400);
    const fail = await request(app).post(`${L}/enrollments/${sitaEnr}/attempts`).set(auth(sita)).send({ answers: [0, 1] });
    expect(fail.body).toMatchObject({ score: 0, passed: false });
    const pass = await request(app).post(`${L}/enrollments/${sitaEnr}/attempts`).set(auth(sita)).send({ answers: [1, 0] });
    expect(pass.body).toMatchObject({ score: 100, passed: true });
    const row = await db.query<{ status: string; certificate_issued_on: string; certificate_expires_on: string }>('SELECT status, certificate_issued_on, certificate_expires_on FROM lrn_enrollment WHERE id = $1', [sitaEnr]);
    expect(row.rows[0]!.status).toBe('completed');
    expect(row.rows[0]!.certificate_expires_on).toBe(addMonths(row.rows[0]!.certificate_issued_on, 12));
    const cert = await request(app).get(`${L}/enrollments/${sitaEnr}/certificate`).set(auth(sita));
    expect(cert.text).toContain('Certificate of completion');
    await request(app).post(`${L}/enrollments/${sitaEnr}/feedback`).set(auth(sita)).send({ rating: 5 }).expect(200);
  });

  it('managers assign training to their team only; classroom attendance completes it', async () => {
    expect((await request(app).post(`${L}/enrollments/assign`).set(auth(hari)).send({ courseId: classroom, employeeIds: [GITA] })).status).toBe(403);
    expect((await request(app).post(`${L}/enrollments/assign`).set(auth(ram)).send({ courseId: classroom, employeeIds: [OTHER_EMP] })).status).toBe(404);
    const a = await request(app).post(`${L}/enrollments/assign`).set(auth(ram)).send({ courseId: classroom, employeeIds: [HARI, HARI] });
    expect(a.body).toEqual({ added: 1, skipped: 0 });
    const enr = (await request(app).get(`${L}/enrollments`).set(auth(hari))).body.enrollments.find((e: { course_id: string }) => e.course_id === classroom);
    expect((await request(app).post(`${L}/enrollments/${enr.id}/complete`).set(auth(hari))).status).toBe(403);
    expect((await request(app).post(`${L}/enrollments/${enr.id}/attendance`).set(auth(gita)).send({ attended: true })).status).toBe(404);
    await request(app).post(`${L}/enrollments/${enr.id}/attendance`).set(auth(ram)).send({ attended: true }).expect(200);
    const team = await request(app).get(`${L}/enrollments`).set(auth(ram)).query({ scope: 'team' });
    expect(team.body.enrollments.find((e: { id: string }) => e.id === enr.id).status).toBe('completed');
    expect((await request(app).get(`${L}/enrollments`).set(auth(ram)).query({ scope: 'all' })).status).toBe(403);
  });

  it('sessions respect capacity', async () => {
    const s = await request(app).post(`${L}/courses/${classroom}/sessions`).set(auth(hr)).send({ startsAt: new Date(Date.now() + 2 * 86_400_000).toISOString(), endsAt: new Date(Date.now() + 2 * 86_400_000 + 3_600_000).toISOString(), capacity: 1, venue: 'Room 2' });
    expect(s.status).toBe(201);
    await request(app).post(`${L}/enrollments/self`).set(auth(gita)).send({ courseId: classroom, sessionId: s.body.id }).expect(201);
    expect((await request(app).post(`${L}/enrollments/self`).set(auth(sita)).send({ courseId: classroom, sessionId: s.body.id })).status).toBe(409);
  });

  it('certificates are uploaded by the learner and seen by managers and HR only', async () => {
    const up = await request(app).post('/api/v1/files').set(auth(sita)).send({ purpose: 'training_certificate', filename: 'cert.pdf', dataUrl: PDF });
    expect((await request(app).post(`${L}/enrollments/${sitaEnr}/certificate`).set(auth(hari)).send({ fileId: up.body.file.id })).status).toBe(404);
    await request(app).post(`${L}/enrollments/${sitaEnr}/certificate`).set(auth(sita)).send({ fileId: up.body.file.id }).expect(200);
    expect((await request(app).get(`${L}/enrollments/${sitaEnr}/certificate-file`).set(auth(ram))).status).toBe(200);
    expect((await request(app).get(`${L}/enrollments/${sitaEnr}/certificate-file`).set(auth(hari))).status).toBe(404);
    expect((await request(app).get(`${L}/enrollments/${sitaEnr}/certificate-file`).set(auth(otherHr))).status).toBe(404);
  });

  it('mandatory assignment, cost report and expiry reminders', async () => {
    const m = await request(app).post(`${L}/courses/${online}/assign-mandatory`).set(auth(hr));
    expect(m.body.added).toBe(3);   // everyone except Sita, whose certificate is still valid
    const cost = await request(app).get(`${L}/reports/cost`).set(auth(hr)).query({ from: plusDays(-5), to: plusDays(5) });
    expect(Number(cost.body.total)).toBeGreaterThan(0);
    expect((await request(app).get(`${L}/reports/cost`).set(auth(ram)).query({ from: plusDays(-5), to: plusDays(5) })).status).toBe(403);
    await db.query('UPDATE lrn_enrollment SET certificate_expires_on = $2 WHERE id = $1', [sitaEnr, plusDays(10)]);
    const exp = await request(app).get(`${L}/expiring`).set(auth(hr));
    expect(exp.body.enrollments.map((e: { id: string }) => e.id)).toContain(sitaEnr);
    const first = await performanceReminders(db);
    expect(first.learning.expiring).toBe(1);
    const second = await performanceReminders(db);
    expect(second.learning.expiring).toBe(0);
  });
});

describe('phase reminders', () => {
  it('reminds people who still owe a self review once per due-date step', async () => {
    const c = await request(app).post(`${P}/cycles`).set(auth(hr)).send({ name: 'Mid-year', periodStart: '2026-01-01', periodEnd: plusDays(100), selfDue: plusDays(1) });
    await request(app).post(`${P}/cycles/${c.body.id}/launch`).set(auth(hr)).expect(200);
    const r1 = await performanceReminders(db);
    expect(r1.reviews).toBe(4);
    const r2 = await performanceReminders(db);
    expect(r2.reviews).toBe(0);
  });
});
