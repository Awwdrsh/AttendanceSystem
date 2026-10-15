import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { lifecycleDaily } from '../src/modules/lifecycle/service.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hr2: string; let sita: string; let ram: string; let ramUser: string; let otherHr: string;
const plusDays = (d: string, n: number) => new Date(new Date(`${d}T00:00:00Z`).getTime() + n * 86_400_000).toISOString().slice(0, 10);
const today = nptToday();

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  hr2 = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  const r = await loginAs(db, app, ['employee', 'manager'], DEMO.ram);
  ram = r.token; ramUser = r.userId;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query('UPDATE employee SET join_date = $2 WHERE id = ANY($1)', [[DEMO.sita, DEMO.ram], plusDays(today, -200)]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERLC', 'Other Co') RETURNING id`);
  await createUser(db, { companyId: c.rows[0]!.id, email: 'hr@other-lc.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other-lc.local', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('onboarding checklists', () => {
  let newEmp: string; let newEmpToken: string; let caseId: string;

  it('seeds a default onboarding and exit checklist for the company', async () => {
    const r = await request(app).get('/api/v1/lifecycle/templates').set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.templates.map((t: { kind: string }) => t.kind).sort()).toEqual(['offboarding', 'onboarding']);
    expect(r.body.templates[0].tasks.length).toBeGreaterThan(3);
    const e = await request(app).get('/api/v1/lifecycle/templates').set(auth(sita));
    expect(e.status).toBe(403);
  });

  it('starts onboarding automatically when HR adds an employee, with due dates from the join date', async () => {
    const join = plusDays(today, 5);
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'NEW1', nameEn: 'New Joiner', joinDate: join, managerId: DEMO.ram, branchId: DEMO.branchId });
    expect(c.status).toBe(201);
    newEmp = c.body.id;
    const cases = await request(app).get('/api/v1/lifecycle/cases?kind=onboarding').set(auth(hr));
    const mine = cases.body.cases.find((x: { employee_id: string }) => x.employee_id === newEmp);
    expect(mine).toBeTruthy();
    caseId = mine.id;
    expect(mine.anchor_date).toBe(join);
    const d = await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(hr));
    const welcome = d.body.tasks.find((t: { title: string }) => t.title.startsWith('Welcome meeting'));
    expect(welcome.due_date).toBe(plusDays(join, 1));
    expect(welcome.assignee_type).toBe('manager');
  });

  it('a second onboarding for the same employee is refused', async () => {
    const r = await request(app).post('/api/v1/lifecycle/cases').set(auth(hr)).send({ employeeId: newEmp });
    expect(r.status).toBe(409);
  });

  it('the joiner sees their own tasks and can only tick their own', async () => {
    newEmpToken = (await loginAs(db, app, ['employee'], newEmp)).token;
    const my = await request(app).get('/api/v1/lifecycle/my-tasks').set(auth(newEmpToken));
    expect(my.status).toBe(200);
    expect(my.body.tasks.length).toBeGreaterThan(0);
    expect(my.body.tasks.every((t: { assignee_type: string }) => t.assignee_type === 'employee')).toBe(true);
    const own = my.body.tasks[0];
    const ok = await request(app).patch(`/api/v1/lifecycle/tasks/${own.id}`).set(auth(newEmpToken)).send({ status: 'done', note: 'Uploaded' });
    expect(ok.status).toBe(200);
    expect(ok.body.task.status).toBe('done');
    const d = await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(newEmpToken));
    const hrTask = d.body.tasks.find((t: { assignee_type: string }) => t.assignee_type === 'hr');
    expect(hrTask.can_complete).toBe(false);
    const no = await request(app).patch(`/api/v1/lifecycle/tasks/${hrTask.id}`).set(auth(newEmpToken)).send({ status: 'done' });
    expect(no.status).toBe(403);
    const skip = await request(app).patch(`/api/v1/lifecycle/tasks/${own.id}`).set(auth(newEmpToken)).send({ status: 'skipped' });
    expect(skip.status).toBe(403);
    const due = await request(app).patch(`/api/v1/lifecycle/tasks/${own.id}`).set(auth(newEmpToken)).send({ dueDate: today });
    expect(due.status).toBe(403);
  });

  it('the line manager sees the case and ticks manager tasks; strangers and other companies cannot', async () => {
    const list = await request(app).get('/api/v1/lifecycle/cases?kind=onboarding').set(auth(ram));
    expect(list.body.cases.some((c: { id: string }) => c.id === caseId)).toBe(true);
    const my = await request(app).get('/api/v1/lifecycle/my-tasks').set(auth(ram));
    const mt = my.body.tasks.find((t: { assignee_type: string; employee_id: string }) => t.assignee_type === 'manager' && t.employee_id === newEmp);
    expect(mt).toBeTruthy();
    expect((await request(app).patch(`/api/v1/lifecycle/tasks/${mt.id}`).set(auth(ram)).send({ status: 'done' })).status).toBe(200);
    expect((await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(sita))).status).toBe(404);
    expect((await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get('/api/v1/lifecycle/cases').set(auth(otherHr))).body.cases).toEqual([]);
  });

  it('comments notify the people responsible, and the checklist closes when everything is done', async () => {
    const d = await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(hr));
    const hrTask = d.body.tasks.find((t: { assignee_type: string; status: string }) => t.assignee_type === 'hr' && t.status === 'open');
    const c = await request(app).post(`/api/v1/lifecycle/tasks/${hrTask.id}/comments`).set(auth(newEmpToken)).send({ body: 'When will my account be ready?' });
    expect(c.status).toBe(201);
    const list = await request(app).get(`/api/v1/lifecycle/tasks/${hrTask.id}/comments`).set(auth(hr));
    expect(list.body.comments).toHaveLength(1);
    let last: { caseCompleted: boolean } = { caseCompleted: false };
    for (const t of d.body.tasks) {
      if (t.status !== 'open') continue;
      const r = await request(app).patch(`/api/v1/lifecycle/tasks/${t.id}`).set(auth(hr)).send({ status: 'done' });
      expect(r.status).toBe(200);
      last = r.body;
    }
    expect(last.caseCompleted).toBe(true);
    const after = await request(app).get(`/api/v1/lifecycle/cases/${caseId}`).set(auth(hr));
    expect(after.body.case.status).toBe('completed');
  });

  it('HR edits templates; a task for a named person needs that person', async () => {
    const bad = await request(app).post('/api/v1/lifecycle/templates').set(auth(hr)).send({ kind: 'onboarding', name: 'Engineering', tasks: [{ title: 'Repo access', assigneeType: 'user', dueOffsetDays: 0 }] });
    expect(bad.status).toBe(400);
    const ok = await request(app).post('/api/v1/lifecycle/templates').set(auth(hr)).send({ kind: 'onboarding', name: 'Engineering', tasks: [{ title: 'Repo access', assigneeType: 'user', assigneeUserId: ramUser, dueOffsetDays: 0, link: '/me/profile' }] });
    expect(ok.status).toBe(201);
    const badLink = await request(app).put(`/api/v1/lifecycle/templates/${ok.body.id}`).set(auth(hr)).send({ kind: 'onboarding', name: 'Engineering', tasks: [{ title: 'x', assigneeType: 'hr', link: 'https://evil.example' }] });
    expect(badLink.status).toBe(400);
    const dup = await request(app).post('/api/v1/lifecycle/templates').set(auth(hr)).send({ kind: 'onboarding', name: 'Engineering', tasks: [] });
    expect(dup.status).toBe(409);
  });
});

describe('probation', () => {
  it('lists employees whose probation ends soon for their manager', async () => {
    await db.query('UPDATE employee SET probation_end = $2 WHERE id = $1', [DEMO.sita, plusDays(today, 10)]);
    const r = await request(app).get('/api/v1/lifecycle/probation').set(auth(ram));
    expect(r.body.employees.map((e: { id: string }) => e.id)).toContain(DEMO.sita);
    expect((await request(app).get('/api/v1/lifecycle/probation').set(auth(sita))).status).toBe(403);
  });

  it('reminds the manager once per threshold', async () => {
    await db.query('UPDATE employee SET probation_end = $2 WHERE id = $1', [DEMO.sita, plusDays(today, 7)]);
    const a = await lifecycleDaily(db);
    const b = await lifecycleDaily(db);
    expect(a.probationReminders).toBe(1);
    expect(b.probationReminders).toBe(0);
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = $1 AND template = 'lifecycle.probation.ending' AND channel = 'in_app'`, [ramUser]);
    expect(n.rows[0].n).toBe(1);
  });

  it('validates the review and stops people reviewing themselves', async () => {
    const self = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(ram)).send({ employeeId: DEMO.ram, rating: 5, recommendation: 'confirm', comments: 'I am great' });
    expect(self.status).toBe(403);
    const ext = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(ram)).send({ employeeId: DEMO.sita, rating: 3, recommendation: 'extend', extendTo: today, comments: 'Needs more time' });
    expect(ext.status).toBe(400);
    const emp = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(sita)).send({ employeeId: DEMO.sita, rating: 5, recommendation: 'confirm', comments: 'ok ok' });
    expect(emp.status).toBe(403);
  });

  it('manager recommends confirmation, HR approves, the employee is confirmed', async () => {
    const r = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(ram)).send({ employeeId: DEMO.sita, rating: 4, recommendation: 'confirm', comments: 'Reliable and quick to learn.' });
    expect(r.status).toBe(201);
    const dup = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(ram)).send({ employeeId: DEMO.sita, rating: 4, recommendation: 'confirm', comments: 'Again' });
    expect(dup.status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === r.body.id);
    expect(item.summary.headline).toMatch(/Probation review: confirm/);
    expect((await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(ram)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(hr)).send({})).status).toBe(200);
    const e = await db.query('SELECT confirmed_on FROM employee WHERE id = $1', [DEMO.sita]);
    expect(e.rows[0].confirmed_on).toBe(plusDays(today, 7));
    const s = await request(app).get(`/api/v1/lifecycle/employees/${DEMO.sita}/summary`).set(auth(hr));
    expect(s.body.probation.confirmations).toHaveLength(1);
    const again = await request(app).post('/api/v1/lifecycle/probation/reviews').set(auth(ram)).send({ employeeId: DEMO.sita, rating: 4, recommendation: 'confirm', comments: 'Again' });
    expect(again.status).toBe(409);
  });
});

describe('resignation and exit', () => {
  let exitId: string; let assetId: string;

  it('the employee resigns; short notice is reported', async () => {
    const past = await request(app).post('/api/v1/lifecycle/exits/resign').set(auth(sita)).send({ proposedLastWorkingDay: plusDays(today, -1) });
    expect(past.status).toBe(400);
    const r = await request(app).post('/api/v1/lifecycle/exits/resign').set(auth(sita)).send({ proposedLastWorkingDay: plusDays(today, 10), reasonCategory: 'Further studies', reason: 'Masters abroad' });
    expect(r.status).toBe(201);
    expect(r.body.noticeDays).toBe(30);
    expect(r.body.shortfallDays).toBe(20);
    exitId = r.body.id;
    expect((await request(app).post('/api/v1/lifecycle/exits/resign').set(auth(sita)).send({ proposedLastWorkingDay: plusDays(today, 30) })).status).toBe(409);
    const mine = await request(app).get('/api/v1/lifecycle/exits/mine').set(auth(sita));
    expect(mine.body.exits[0].status).toBe('pending');
  });

  it('manager then HR approve; the exit date is set and clearance starts', async () => {
    const mi = (await request(app).get('/api/v1/workflow/inbox').set(auth(ram))).body.items.find((i: { objectId: string }) => i.objectId === exitId);
    expect(mi.summary.detail).toMatch(/short by 20 days/);
    expect((await request(app).post(`/api/v1/workflow/${mi.instanceId}/approve`).set(auth(ram)).send({})).status).toBe(200);
    const hi = (await request(app).get('/api/v1/workflow/inbox').set(auth(hr))).body.items.find((i: { objectId: string }) => i.objectId === exitId);
    expect((await request(app).post(`/api/v1/workflow/${hi.instanceId}/approve`).set(auth(hr)).send({})).status).toBe(200);
    const x = await request(app).get(`/api/v1/lifecycle/exits/${exitId}`).set(auth(hr));
    expect(x.body.exit.status).toBe('accepted');
    expect(x.body.exit.accepted_lwd).toBe(plusDays(today, 10));
    expect(x.body.exit.tasks_total).toBeGreaterThan(3);
    expect(x.body.settlement.steps.length).toBeGreaterThan(0);
    const e = await db.query('SELECT exit_date, status FROM employee WHERE id = $1', [DEMO.sita]);
    expect(e.rows[0]).toEqual({ exit_date: plusDays(today, 10), status: 'active' });
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/withdraw`).set(auth(sita))).status).toBe(409);
    // The employee sees no settlement detail; other companies see nothing.
    expect((await request(app).get(`/api/v1/lifecycle/exits/${exitId}`).set(auth(sita))).body.settlement).toBeNull();
    expect((await request(app).get(`/api/v1/lifecycle/exits/${exitId}`).set(auth(otherHr))).status).toBe(404);
  });

  it('the exit interview is confidential to HR and recorded once', async () => {
    const qs = (await request(app).get('/api/v1/lifecycle/exit-questions').set(auth(sita))).body.questions;
    const answers = qs.map((q: { id: string; kind: string; options: string[] }) => ({ questionId: q.id, answer: q.kind === 'rating' ? 4 : q.kind === 'choice' ? q.options[0] : 'Good team' }));
    const bad = await request(app).post(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(sita)).send({ answers: [{ questionId: qs[0].id, answer: 'Not an option' }] });
    expect(bad.status).toBe(400);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(ram)).send({ answers })).status).toBe(403);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(sita)).send({ answers })).status).toBe(201);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(sita)).send({ answers })).status).toBe(409);
    expect((await request(app).get(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(sita))).status).toBe(403);
    expect((await request(app).get(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(ram))).status).toBe(403);
    const read = await request(app).get(`/api/v1/lifecycle/exits/${exitId}/interview`).set(auth(hr));
    expect(read.body.interview.answers[3].answer).toBe('Good team');
    const raw = await db.query('SELECT answers_enc FROM exit_interview WHERE exit_id = $1', [exitId]);
    expect(raw.rows[0].answers_enc.toString('latin1')).not.toContain('Good team');
  });

  it('assets are assigned and block the exit until returned', async () => {
    const types = (await request(app).get('/api/v1/lifecycle/asset-types').set(auth(hr))).body.types;
    const a = await request(app).post('/api/v1/lifecycle/assets').set(auth(hr)).send({ tag: 'LT-001', name: 'ThinkPad T14', typeId: types[0].id, serial: 'PF123', cost: '150000.00' });
    expect(a.status).toBe(201);
    assetId = a.body.id;
    expect((await request(app).post('/api/v1/lifecycle/assets').set(auth(hr)).send({ tag: 'LT-001', name: 'Dup' })).status).toBe(409);
    expect((await request(app).post(`/api/v1/lifecycle/assets/${assetId}/assign`).set(auth(hr)).send({ employeeId: DEMO.sita })).status).toBe(200);
    expect((await request(app).post(`/api/v1/lifecycle/assets/${assetId}/assign`).set(auth(hr)).send({ employeeId: DEMO.ram })).status).toBe(409);
    expect((await request(app).post(`/api/v1/lifecycle/assets/${assetId}/assign`).set(auth(otherHr)).send({ employeeId: DEMO.sita })).status).toBe(404);
    expect((await request(app).get('/api/v1/lifecycle/assets/mine').set(auth(sita))).body.assets).toHaveLength(1);
    expect((await request(app).get(`/api/v1/lifecycle/assets?employeeId=${DEMO.sita}`).set(auth(ram))).body.assets).toHaveLength(1);
    expect((await request(app).get('/api/v1/lifecycle/assets').set(auth(ram))).status).toBe(403);
    expect((await request(app).post('/api/v1/lifecycle/assets').set(auth(sita)).send({ tag: 'X', name: 'X' })).status).toBe(403);
  });

  it('completion waits for the last working day, the assets and the clearance', async () => {
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/complete`).set(auth(hr)).send({})).status).toBe(409);
    const lwd = plusDays(today, -1);
    expect((await request(app).patch(`/api/v1/lifecycle/exits/${exitId}`).set(auth(hr)).send({ lastWorkingDay: lwd })).status).toBe(200);
    const assetsBlock = await request(app).post(`/api/v1/lifecycle/exits/${exitId}/complete`).set(auth(hr)).send({ skipOpenTasks: true });
    expect(assetsBlock.status).toBe(409);
    expect(assetsBlock.body.error).toMatch(/asset/);
    expect((await request(app).post(`/api/v1/lifecycle/assets/${assetId}/return`).set(auth(hr)).send({ condition: 'good' })).status).toBe(200);
    const daily = await lifecycleDaily(db);
    expect(daily.exitsFlagged).toBe(1);
    const tasksBlock = await request(app).post(`/api/v1/lifecycle/exits/${exitId}/complete`).set(auth(hr)).send({});
    expect(tasksBlock.status).toBe(409);
    expect(tasksBlock.body.error).toMatch(/clearance/);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${exitId}/complete`).set(auth(hr)).send({ skipOpenTasks: true })).status).toBe(200);
    const e = await db.query('SELECT exit_date, status FROM employee WHERE id = $1', [DEMO.sita]);
    expect(e.rows[0]).toEqual({ exit_date: lwd, status: 'exited' });
    // The leaver can no longer sign in after the last working day.
    expect((await request(app).get('/api/v1/auth/me').set(auth(sita))).status).toBe(401);
  });

  it('HR records an exit for someone else; the daily job completes it once clearance is done', async () => {
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'TMP1', nameEn: 'Contract Worker', joinDate: plusDays(today, -100) });
    const id = c.body.id;
    expect((await request(app).post('/api/v1/lifecycle/exits').set(auth(ram)).send({ employeeId: id, kind: 'contract_end', lastWorkingDay: plusDays(today, -2) })).status).toBe(403);
    const x = await request(app).post('/api/v1/lifecycle/exits').set(auth(hr)).send({ employeeId: id, kind: 'contract_end', lastWorkingDay: plusDays(today, -2), reasonCategory: 'Contract ended' });
    expect(x.status).toBe(201);
    const detail = await request(app).get(`/api/v1/lifecycle/exits/${x.body.id}`).set(auth(hr));
    const caseRes = await request(app).get(`/api/v1/lifecycle/cases/${detail.body.exit.case_id}`).set(auth(hr));
    for (const t of caseRes.body.tasks) await request(app).patch(`/api/v1/lifecycle/tasks/${t.id}`).set(auth(hr2)).send({ status: 'done' });
    const before = await db.query('SELECT status FROM lifecycle_case WHERE id = $1', [detail.body.exit.case_id]);
    expect(before.rows[0].status).toBe('active');
    const d = await lifecycleDaily(db);
    expect(d.exitsCompleted).toBe(1);
    const e = await db.query('SELECT status FROM employee WHERE id = $1', [id]);
    expect(e.rows[0].status).toBe('exited');
  });

  it('an employee may withdraw a pending resignation; HR may cancel an accepted exit', async () => {
    const r = await request(app).post('/api/v1/lifecycle/exits/resign').set(auth(ram)).send({ proposedLastWorkingDay: plusDays(today, 40) });
    expect(r.status).toBe(201);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${r.body.id}/withdraw`).set(auth(ram))).status).toBe(200);
    const pending = await db.query(`SELECT state FROM workflow_instance WHERE object_type = 'resignation' AND object_id = $1`, [r.body.id]);
    expect(pending.rows[0].state).toBe('cancelled');
    const x = await request(app).post('/api/v1/lifecycle/exits').set(auth(hr)).send({ employeeId: DEMO.ram, kind: 'termination', lastWorkingDay: plusDays(today, 20) });
    expect(x.status).toBe(201);
    expect((await request(app).post(`/api/v1/lifecycle/exits/${x.body.id}/cancel`).set(auth(hr)).send({ reason: 'Decision reversed' })).status).toBe(200);
    const e = await db.query('SELECT exit_date FROM employee WHERE id = $1', [DEMO.ram]);
    expect(e.rows[0].exit_date).toBeNull();
    const cases = await db.query(`SELECT status FROM lifecycle_case WHERE exit_id = $1`, [x.body.id]);
    expect(cases.rows[0].status).toBe('cancelled');
  });
});
