import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Express } from 'express';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { applyLimiter } from '../src/modules/careers/routes.js';
import { onCandidateHired, purgeRejectedCandidates, scoreApplication } from '../src/modules/hiring/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;
const PUB = '/api/v1/public/careers';
const H = '/api/v1/hiring';

const QUESTIONS = [
  { id: 'q1', kind: 'yes_no', label: 'Do you hold a valid work permit for Nepal?', required: true, knockout: { equals: 'yes' } },
  { id: 'q2', kind: 'number', label: 'Years of TypeScript experience', required: true, preferred: { min: 2 } },
  { id: 'q3', kind: 'text', label: 'Link to your portfolio', required: false },
];

const future = (days: number) => new Date(Date.now() + days * 86_400_000 + 345 * 60_000).toISOString().slice(0, 10);

describe('hiring and applicant tracking', () => {
  let db: Db; let app: Express;
  let hr: string; let hr2: string; let ram: string; let ramUserId: string; let mgr2: string; let mgr2UserId: string; let emp: string; let otherHr: string;
  let unitId: string; let reqId: string; let jobId: string; let appId: string; let offerId: string;
  const hired: string[] = [];

  beforeAll(async () => {
    db = await freshDb(); app = testApp(db);
    unitId = (await db.query<{ id: string }>(`INSERT INTO org_unit (company_id, kind, code, name_en) VALUES ($1, 'department', 'ENG', 'Engineering') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    await db.query('UPDATE employee_assignment SET org_unit_id = $2 WHERE employee_id = $1', [DEMO.ram, unitId]);
    hr = (await loginAs(db, app, ['hr'])).token;
    hr2 = (await loginAs(db, app, ['hr'])).token;
    const r = await loginAs(db, app, ['manager'], DEMO.ram); ram = r.token; ramUserId = r.userId;
    const m2 = await loginAs(db, app, ['manager']); mgr2 = m2.token; mgr2UserId = m2.userId;
    emp = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
    const other = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERCO', 'Other Co') RETURNING id`)).rows[0]!.id;
    await createUser(db, { companyId: other, email: 'hr@otherco.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
    otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@otherco.local', password: 'correct-horse-battery-staple' })).body.token;
    onCandidateHired(async (_db, e) => { hired.push(e.employeeId); });
  });
  afterAll(async () => { await db.end(); });
  beforeEach(() => applyLimiter.reset());

  it('scores transparently: experience and preferred answers earn points, a failed must-have is flagged', () => {
    const job = { experience_min: '2', experience_max: '5', questions: QUESTIONS as never };
    const good = scoreApplication(job, { experienceYears: 3, answers: { q1: 'yes', q2: 4 } });
    expect(good).toMatchObject({ score: 100, knockedOut: false });
    const weak = scoreApplication(job, { experienceYears: 1, answers: { q1: 'no', q2: 1 } });
    expect(weak.knockedOut).toBe(true);
    expect(weak.score).toBe(33);  // 20 of 40 for experience, 0 of 20 for the preferred answer
    expect(weak.detail.map((d) => d.note)).toContain('Does not meet the must-have');
  });

  it('a manager raises a hiring request and HR approves it', async () => {
    const bad = await request(app).post(`${H}/requisitions`).set(auth(emp)).send({ title: 'Engineer', justification: 'We need more people.' });
    expect(bad.status).toBe(403);
    const short = await request(app).post(`${H}/requisitions`).set(auth(ram)).send({ title: 'Engineer', justification: 'short' });
    expect(short.status).toBe(400);
    const r = await request(app).post(`${H}/requisitions`).set(auth(ram)).send({ title: 'Backend engineer', designation: 'Software Engineer', orgUnitId: unitId, headcount: 1, salaryMin: '80000', salaryMax: '120000', justification: 'New payments project starting next quarter.' });
    expect(r.status).toBe(201);
    reqId = r.body.id;
    expect((await request(app).get(`${H}/requisitions`).set(auth(ram))).body.requisitions).toHaveLength(1);
    expect((await request(app).get(`${H}/requisitions`).set(auth(mgr2))).body.requisitions).toHaveLength(0);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === reqId);
    expect(item.summary).toMatchObject({ title: 'Backend engineer', department: 'Engineering', headcount: 1 });
    expect((await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(hr)).send({})).status).toBe(200);
    const row = await db.query('SELECT status FROM hiring_requisition WHERE id = $1', [reqId]);
    expect(row.rows[0].status).toBe('approved');
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE user_id = $1 AND template = 'job_requisition.approved'`, [ramUserId]);
    expect(n.rows).toHaveLength(1);
  });

  it('HR creates a job from the request; it stays private until published', async () => {
    expect((await request(app).post(`${H}/jobs`).set(auth(ram)).send({ title: 'X' })).status).toBe(403);
    const badQ = await request(app).post(`${H}/jobs`).set(auth(hr)).send({ title: 'Backend engineer', questions: [{ id: 'q1', kind: 'text', label: 'Anything?', knockout: { equals: 'yes' } }] });
    expect(badQ.status).toBe(400);
    const badStages = await request(app).post(`${H}/jobs`).set(auth(hr)).send({ title: 'Backend engineer', stages: ['Screening', 'Hired'] });
    expect(badStages.status).toBe(400);
    const j = await request(app).post(`${H}/jobs`).set(auth(hr)).send({
      requisitionId: reqId, title: 'Backend engineer', description: 'Build and run the payments services that power our clients.', requirements: 'TypeScript, PostgreSQL',
      category: 'Engineering', location: 'Kathmandu', workMode: 'hybrid', experienceMin: 2, experienceMax: 5, salaryMin: '80000', salaryMax: '120000', showSalary: false,
      deadline: future(30), orgUnitId: unitId, hiringManagerId: DEMO.ram, questions: QUESTIONS,
    });
    expect(j.status).toBe(201);
    jobId = j.body.id;
    expect((await request(app).get(`${PUB}/jobs/${jobId}`)).status).toBe(404);
    expect((await request(app).post(`${H}/jobs/${jobId}/publish`).set(auth(hr))).status).toBe(200);
  });

  it('the public careers page lists open jobs without private fields', async () => {
    const none = await request(app).get(`${PUB}/jobs`);   // two companies: a code is needed
    expect(none.status).toBe(404);
    const list = await request(app).get(`${PUB}/jobs?company=demo`);
    expect(list.status).toBe(200);
    expect(list.body.company.name).toBe('Demo Company');
    expect(list.body.jobs).toHaveLength(1);
    expect(list.body.jobs[0].salary_min).toBeNull();
    expect(list.body.jobs[0]).not.toHaveProperty('company_id');
    expect((await request(app).get(`${PUB}/jobs?company=demo&workMode=remote`)).body.jobs).toHaveLength(0);
    expect((await request(app).get(`${PUB}/jobs?company=OTHERCO`)).body.jobs).toHaveLength(0);
    const one = await request(app).get(`${PUB}/jobs/${jobId}`);
    expect(one.body.job.questions[0]).toEqual({ id: 'q1', kind: 'yes_no', label: QUESTIONS[0]!.label, required: true });
    expect(JSON.stringify(one.body)).not.toContain('knockout');
    expect((await request(app).get(`${PUB}/jobs/not-a-uuid`)).status).toBe(404);
  });

  const apply = (body: Record<string, unknown>) => request(app).post(`${PUB}/jobs/${jobId}/apply`).send({
    name: 'Anita Gurung', email: 'Anita@example.com', phone: '+977 9800000000', experienceYears: 3, coverNote: 'I love payments.',
    answers: { q1: 'yes', q2: 4, q3: 'https://example.com' }, cvFilename: 'anita-cv.pdf', cvDataUrl: PDF, consent: true, ...body,
  });

  it('accepts a public application with an encrypted CV, e-mails the candidate and tells HR', async () => {
    const r = await apply({});
    expect(r.status).toBe(201);
    const a = await db.query<{ id: string; score: number; knocked_out: boolean; cv_file_id: string }>('SELECT id, score, knocked_out, cv_file_id FROM hiring_application WHERE job_id = $1', [jobId]);
    expect(a.rows).toHaveLength(1);
    appId = a.rows[0]!.id;
    expect(a.rows[0]).toMatchObject({ score: 100, knocked_out: false });
    const f = await db.query('SELECT data_enc, attached_to FROM stored_file WHERE id = $1', [a.rows[0]!.cv_file_id]);
    expect(f.rows[0].attached_to).toBe(`hiring_application:${appId}`);
    expect(f.rows[0].data_enc.toString('latin1')).not.toContain('%PDF');
    const mail = await db.query(`SELECT to_email, title FROM notification_outbox WHERE template = 'hiring.received'`);
    expect(mail.rows[0].to_email).toBe('anita@example.com');
    const hrN = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'hiring.applied'`);
    expect(hrN.rows[0].n).toBeGreaterThanOrEqual(3);   // both HR users and the hiring manager
  });

  it('a repeat application, a bot and bad input store nothing and reveal nothing', async () => {
    const again = await apply({ name: 'Someone Else' });
    expect(again.status).toBe(201);
    const bot = await apply({ email: 'bot@example.com', website: 'http://spam' });
    expect(bot.status).toBe(201);
    expect((await db.query('SELECT count(*)::int AS n FROM hiring_application WHERE job_id = $1', [jobId])).rows[0].n).toBe(1);
    expect((await apply({ email: 'x@example.com', consent: false })).status).toBe(400);
    expect((await apply({ email: 'y@example.com', answers: { q2: 3 } })).status).toBe(400);
    const html = await apply({ email: 'z@example.com', cvDataUrl: `data:application/pdf;base64,${Buffer.from('<script>x</script>').toString('base64')}` });
    expect(html.status).toBe(400);
    applyLimiter.reset();
    const png = await apply({ email: 'w@example.com', cvDataUrl: 'data:image/png;base64,iVBORw0KGgo=' });
    expect(png.status).toBe(400);
    expect((await apply({ email: 'v@example.com', extra: 'field' })).status).toBe(400);
  });

  it('rate-limits applications from one connection', async () => {
    for (let i = 0; i < 5; i++) await apply({ email: `bad${i}@example.com`, consent: false });
    const sixth = await apply({ email: 'late@example.com' });
    expect(sixth.status).toBe(429);
  });

  it('flags a candidate who fails a must-have', async () => {
    const r = await apply({ name: 'Bikash Rai', email: 'bikash@example.com', experienceYears: 1, answers: { q1: 'no', q2: 0 } });
    expect(r.status).toBe(201);
    const row = await db.query(`SELECT a.knocked_out, a.score FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE c.email = 'bikash@example.com'`);
    expect(row.rows[0]).toMatchObject({ knocked_out: true, score: 33 });
  });

  it('scopes the pipeline: HR and the department manager yes; others no', async () => {
    const board = await request(app).get(`${H}/jobs/${jobId}/applications`).set(auth(hr));
    expect(board.status).toBe(200);
    expect(board.body.applications[0].name).toBe('Anita Gurung');   // best score first, knocked-out last
    expect(board.body.applications[1].knocked_out).toBe(true);
    expect((await request(app).get(`${H}/jobs/${jobId}/applications`).set(auth(ram))).status).toBe(200);
    expect((await request(app).get(`${H}/jobs`).set(auth(ram))).body.jobs).toHaveLength(1);
    expect((await request(app).get(`${H}/jobs`).set(auth(mgr2))).body.jobs).toHaveLength(0);
    expect((await request(app).get(`${H}/jobs/${jobId}/applications`).set(auth(mgr2))).status).toBe(404);
    expect((await request(app).get(`${H}/applications/${appId}`).set(auth(mgr2))).status).toBe(404);
    expect((await request(app).get(`${H}/jobs`).set(auth(emp))).status).toBe(403);
    const look = await request(app).get(`${H}/lookups`).set(auth(ram));
    expect(look.body.orgUnits.map((u: { name_en: string }) => u.name_en)).toEqual(['Engineering']);
    expect(look.body.users.every((u: Record<string, unknown>) => !('email' in u))).toBe(true);
    expect((await request(app).get(`${H}/lookups`).set(auth(emp))).status).toBe(403);
    expect((await request(app).get(`${H}/applications/${appId}`).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get(`${H}/jobs/${jobId}`).set(auth(otherHr))).status).toBe(404);
    const cv = await request(app).get(`${H}/applications/${appId}/cv`).set(auth(ram));
    expect(cv.status).toBe(200);
    expect(cv.headers['content-security-policy']).toContain('sandbox');
  });

  it('moves, rates, tags and notes an application with a timeline', async () => {
    expect((await request(app).post(`${H}/applications/${appId}/move`).set(auth(ram)).send({ stage: 'Nowhere' })).status).toBe(400);
    expect((await request(app).post(`${H}/applications/${appId}/move`).set(auth(ram)).send({ stage: 'Hired' })).status).toBe(400);
    expect((await request(app).post(`${H}/applications/${appId}/move`).set(auth(ram)).send({ stage: 'Screening' })).status).toBe(200);
    expect((await request(app).patch(`${H}/applications/${appId}`).set(auth(ram)).send({ rating: 4, tags: ['strong', 'payments', 'strong'] })).status).toBe(200);
    expect((await request(app).post(`${H}/applications/${appId}/notes`).set(auth(hr)).send({ body: 'Great phone screen.' })).status).toBe(201);
    const d = await request(app).get(`${H}/applications/${appId}`).set(auth(hr));
    expect(d.body.application).toMatchObject({ stage: 'Screening', rating: 4, tags: ['strong', 'payments'] });
    expect(d.body.application).not.toHaveProperty('cv_file_id');
    expect(d.body.events.map((e: { kind: string }) => e.kind)).toEqual(expect.arrayContaining(['applied', 'stage', 'rating', 'note', 'email']));
    expect(d.body.application.score_detail.length).toBeGreaterThan(0);
  });

  let interviewId: string;
  it('schedules an interview: panel notified, candidate invited, stage moves on', async () => {
    const past = await request(app).post(`${H}/applications/${appId}/interviews`).set(auth(ram)).send({ date: '2020-01-01', time: '10:00', mode: 'video', panel: [ramUserId] });
    expect(past.status).toBe(400);
    const foreign = await request(app).post(`${H}/applications/${appId}/interviews`).set(auth(ram)).send({ date: future(3), time: '11:00', mode: 'video', panel: ['00000000-0000-7000-8000-00000000ffff'] });
    expect(foreign.status).toBe(400);
    const r = await request(app).post(`${H}/applications/${appId}/interviews`).set(auth(ram)).send({ date: future(3), time: '11:00', mode: 'video', location: 'https://meet.example.com/x', panel: [ramUserId, mgr2UserId] });
    expect(r.status).toBe(201);
    interviewId = r.body.id;
    expect((await db.query(`SELECT stage FROM hiring_application WHERE id = $1`, [appId])).rows[0].stage).toBe('Interview');
    expect((await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'hiring.interview' AND channel = 'in_app'`)).rows[0].n).toBe(2);
    const invite = await db.query(`SELECT body FROM notification_outbox WHERE template = 'hiring.interview_invite'`);
    expect(invite.rows[0].body).toContain('11:00');
    const mine = await request(app).get(`${H}/interviews?scope=mine`).set(auth(mgr2));
    expect(mine.body.interviews).toHaveLength(1);
    expect((await request(app).get(`${H}/interviews?scope=all`).set(auth(emp))).status).toBe(403);
  });

  it('panel members get a read-only view and submit their own scorecard', async () => {
    const view = await request(app).get(`${H}/applications/${appId}`).set(auth(mgr2));
    expect(view.status).toBe(200);
    expect(view.body.access).toBe('panel');
    expect(view.body.events).toEqual([]);
    expect((await request(app).get(`${H}/applications/${appId}/cv`).set(auth(mgr2))).status).toBe(200);
    expect((await request(app).post(`${H}/applications/${appId}/move`).set(auth(mgr2)).send({ stage: 'Offer' })).status).toBe(404);
    expect((await request(app).post(`${H}/interviews/${interviewId}/scorecard`).set(auth(hr)).send({ ratings: { Skills: 4 }, recommendation: 'yes' })).status).toBe(404);
    expect((await request(app).post(`${H}/interviews/${interviewId}/scorecard`).set(auth(mgr2)).send({ ratings: { Skills: 9 }, recommendation: 'yes' })).status).toBe(400);
    expect((await request(app).post(`${H}/interviews/${interviewId}/scorecard`).set(auth(mgr2)).send({ ratings: { Skills: 4, Communication: 5 }, recommendation: 'strong_yes', notes: 'Clear thinker.' })).status).toBe(201);
    expect((await request(app).post(`${H}/interviews/${interviewId}/scorecard`).set(auth(ram)).send({ ratings: { Skills: 3 }, recommendation: 'yes' })).status).toBe(201);
    const panelView = await request(app).get(`${H}/applications/${appId}`).set(auth(mgr2));
    expect(panelView.body.scorecards).toHaveLength(1);
    const hrView = await request(app).get(`${H}/applications/${appId}`).set(auth(hr));
    expect(hrView.body.scorecards).toHaveLength(2);
  });

  it('an offer needs approval by another HR user, then the candidate accepts', async () => {
    expect((await request(app).post(`${H}/applications/${appId}/offers`).set(auth(ram)).send({ designation: 'Software Engineer', salary: '100000', joinDate: future(20) })).status).toBe(403);
    expect((await request(app).post(`${H}/applications/${appId}/offers`).set(auth(hr)).send({ designation: 'Software Engineer', salary: '100000', joinDate: '2020-01-01' })).status).toBe(400);
    const o = await request(app).post(`${H}/applications/${appId}/offers`).set(auth(hr)).send({ designation: 'Software Engineer', salary: '100000', joinDate: future(20), expiresOn: future(7), notes: 'Includes a laptop.' });
    expect(o.status).toBe(201);
    offerId = o.body.id;
    expect((await request(app).post(`${H}/applications/${appId}/offers`).set(auth(hr)).send({ designation: 'Engineer', salary: '1', joinDate: future(20) })).status).toBe(409);
    const draft = await request(app).get(`${H}/offers/${offerId}/letter`).set(auth(hr));
    expect(draft.text).toContain('DRAFT');
    expect((await request(app).post(`${H}/offers/${offerId}/respond`).set(auth(hr)).send({ response: 'accepted' })).status).toBe(409);
    expect((await request(app).post(`${H}/applications/${appId}/hire`).set(auth(hr)).send({ empCode: 'E900' })).status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr2));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === offerId);
    expect(item.summary).toMatchObject({ candidate: 'Anita Gurung', designation: 'Software Engineer' });
    expect((await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(hr2)).send({})).status).toBe(200);
    const letter = await request(app).get(`${H}/offers/${offerId}/letter`).set(auth(hr));
    expect(letter.text).toContain('Anita Gurung');
    expect(letter.text).not.toContain('class="draft"');
    expect(letter.text).toContain('1,00,000.00');
    expect((await request(app).post(`${H}/offers/${offerId}/respond`).set(auth(hr)).send({ response: 'accepted' })).status).toBe(200);
  });

  it('hires the candidate as an employee, fills the request and runs the onboarding hook', async () => {
    expect((await request(app).post(`${H}/applications/${appId}/hire`).set(auth(hr)).send({ empCode: 'E001' })).status).toBe(409);
    const r = await request(app).post(`${H}/applications/${appId}/hire`).set(auth(hr)).send({ empCode: 'E900', shiftId: DEMO.shiftId });
    expect(r.status).toBe(201);
    const e = await db.query('SELECT name_en, email, designation, join_date FROM employee WHERE id = $1', [r.body.employeeId]);
    expect(e.rows[0]).toMatchObject({ name_en: 'Anita Gurung', email: 'anita@example.com', designation: 'Software Engineer', join_date: future(20) });
    const a = await db.query('SELECT org_unit_id, manager_id FROM employee_assignment WHERE employee_id = $1', [r.body.employeeId]);
    expect(a.rows[0]).toMatchObject({ org_unit_id: unitId, manager_id: DEMO.ram });
    expect((await db.query('SELECT status, filled FROM hiring_requisition WHERE id = $1', [reqId])).rows[0]).toMatchObject({ status: 'filled', filled: 1 });
    expect((await db.query('SELECT status, stage, cv_file_id IS NOT NULL AS cv FROM hiring_application WHERE id = $1', [appId])).rows[0]).toMatchObject({ status: 'hired', stage: 'Hired', cv: true });
    expect(hired).toEqual([r.body.employeeId]);
    expect((await request(app).post(`${H}/applications/${appId}/hire`).set(auth(hr)).send({ empCode: 'E901' })).status).toBe(409);
  });

  it('rejects with a courteous e-mail and reports the funnel, time to hire and sources', async () => {
    const other = (await db.query<{ id: string }>(`SELECT a.id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE c.email = 'bikash@example.com'`)).rows[0]!.id;
    const rj = await request(app).post(`${H}/applications/${other}/reject`).set(auth(ram)).send({ reason: 'No work permit' });
    expect(rj.body).toMatchObject({ ok: true, emailed: true });
    expect((await request(app).post(`${H}/applications/${other}/reject`).set(auth(ram)).send({ reason: 'Again' })).status).toBe(409);
    const rep = await request(app).get(`${H}/reports`).set(auth(hr));
    expect(rep.status).toBe(200);
    expect(rep.body.totals).toMatchObject({ applications: 2, hired: 1, rejected: 1 });
    const f = rep.body.funnel[0];
    expect(f.stages.map((s: { reached: number }) => s.reached)).toEqual([2, 1, 1, 1, 1]);
    expect(rep.body.timeToHire.hires).toBe(1);
    expect(rep.body.sources).toEqual([{ source: 'careers', applications: 2, hired: 1 }]);
    expect((await request(app).get(`${H}/reports`).set(auth(ram))).status).toBe(403);
  });

  it('exports a candidate for HR and purges rejected candidates after the retention period', async () => {
    const other = (await db.query<{ id: string; cv_file_id: string }>(`SELECT a.id, a.cv_file_id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE c.email = 'bikash@example.com'`)).rows[0]!;
    const ex = await request(app).get(`${H}/applications/${other.id}/export`).set(auth(hr));
    expect(ex.status).toBe(200);
    expect(ex.body.candidate.email).toBe('bikash@example.com');
    expect((await request(app).get(`${H}/applications/${other.id}/export`).set(auth(ram))).status).toBe(403);
    expect(await purgeRejectedCandidates(db, new Date())).toBe(0);
    const n = await purgeRejectedCandidates(db, new Date(Date.now() + 200 * 86_400_000));
    expect(n).toBe(1);   // the hired candidate is kept
    const c = await db.query(`SELECT c.name, c.email, a.cv_file_id, a.cover_note FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE a.id = $1`, [other.id]);
    expect(c.rows[0]).toMatchObject({ name: 'Removed candidate', email: null, cv_file_id: null, cover_note: null });
    expect((await db.query('SELECT data_enc FROM stored_file WHERE id = $1', [other.cv_file_id])).rows[0].data_enc).toBeNull();
    expect((await db.query(`SELECT to_email FROM notification_outbox WHERE template = 'hiring.rejected'`)).rows[0].to_email).toBeNull();
    expect((await request(app).post(`${H}/applications/${other.id}/reopen`).set(auth(hr))).status).toBe(409);
  });

  it('HR can switch the careers page off and set retention', async () => {
    expect((await request(app).put(`${H}/policy`).set(auth(hr)).send({ retentionDays: 5, careersEnabled: true })).status).toBe(400);
    expect((await request(app).put(`${H}/policy`).set(auth(ram)).send({ retentionDays: 90, careersEnabled: false })).status).toBe(403);
    expect((await request(app).put(`${H}/policy`).set(auth(hr)).send({ retentionDays: 90, careersEnabled: false })).status).toBe(200);
    expect((await request(app).get(`${PUB}/jobs?company=DEMO`)).status).toBe(404);
    expect((await request(app).get(`${PUB}/jobs/${jobId}`)).status).toBe(404);
    await request(app).put(`${H}/policy`).set(auth(hr)).send({ retentionDays: 90, careersEnabled: true });
    expect((await request(app).post(`${H}/jobs/${jobId}/close`).set(auth(hr))).status).toBe(200);
    expect((await apply({ email: 'after@example.com' })).status).toBe(404);
  });
});
