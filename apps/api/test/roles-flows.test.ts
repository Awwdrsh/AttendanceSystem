import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { leaveTypeByCode } from '../src/modules/leave/service.js';
import { nptToday, registerHandler, startWorkflow } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

/**
 * Roles, scopes and approval flows: department and branch heads manage their people, specialist HR roles reach only
 * their area, flows pick approvers by kind with conditions and amount thresholds, away approvers hand over to a
 * delegate or HR, and nobody gains access they should not have.
 */
let db: Db; let app: ReturnType<typeof testApp>;
let hrTok: string; let adminTok: string;
let n = 0;

async function emp(opts: { orgUnitId?: string | null; branchId?: string | null; managerId?: string | null; designationId?: string | null } = {}): Promise<string> {
  n++;
  const r = await db.query<{ id: string }>(
    `INSERT INTO employee (company_id, emp_code, name_en, join_date, designation_id) VALUES ($1, $2, $3, '2025-01-01', $4) RETURNING id`,
    [DEMO.companyId, `RF${String(n).padStart(3, '0')}`, `Person ${n}`, opts.designationId ?? null]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, org_unit_id, branch_id, manager_id, employment_type) VALUES ($1, daterange('2025-01-01', NULL), $2, $3, $4, 'regular')`,
    [r.rows[0]!.id, opts.orgUnitId ?? null, opts.branchId ?? DEMO.branchId, opts.managerId ?? null]);
  return r.rows[0]!.id;
}
async function unit(code: string, parentId: string | null = null, head: string | null = null): Promise<string> {
  const r = await db.query<{ id: string }>(`INSERT INTO org_unit (company_id, kind, code, name_en, parent_id, head_employee_id) VALUES ($1, 'department', $2, $2, $3, $4) RETURNING id`,
    [DEMO.companyId, code, parentId, head]);
  return r.rows[0]!.id;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  // These tests start approvals for made-up letter requests; the decision itself has nothing to apply.
  registerHandler('letter_request', async () => undefined);
  hrTok = (await loginAs(db, app, ['hr'])).token;
  adminTok = (await loginAs(db, app, ['admin'])).token;
});
afterAll(async () => { await db.end(); });

describe('department and branch heads', () => {
  let head: string; let headTok: string; let inSub: string; let outside: string; let plainTok: string;
  beforeAll(async () => {
    head = await emp();
    const top = await unit('ENG', null, head);
    const sub = await unit('ENG-WEB', top);
    inSub = await emp({ orgUnitId: sub });
    outside = await emp({ orgUnitId: await unit('OPS') });
    headTok = (await loginAs(db, app, ['employee'], head)).token;
    plainTok = (await loginAs(db, app, ['employee'], await emp({ orgUnitId: sub }))).token;
  });

  it('a department head gets manager pages without being given the role', async () => {
    const me = await request(app).get('/api/v1/auth/me').set(auth(headTok));
    expect(me.body.user.roles).toContain('manager');
    const plain = await request(app).get('/api/v1/auth/me').set(auth(plainTok));
    expect(plain.body.user.roles).not.toContain('manager');
  });

  it('sees people anywhere under the department, not outside it', async () => {
    expect((await request(app).get(`/api/v1/employees/${inSub}`).set(auth(headTok))).status).toBe(200);
    expect((await request(app).get(`/api/v1/employees/${outside}`).set(auth(headTok))).status).toBe(403);
    const list = await request(app).get('/api/v1/employees').set(auth(headTok));
    const ids = list.body.employees.map((e: { id: string }) => e.id);
    expect(ids).toContain(inSub);
    expect(ids).not.toContain(outside);
  });

  it('an ordinary colleague in the same department sees nobody else', async () => {
    expect((await request(app).get(`/api/v1/employees/${inSub}`).set(auth(plainTok))).status).toBe(403);
    expect((await request(app).get('/api/v1/employees').set(auth(plainTok))).status).toBe(403);
  });

  it('a branch head manages everyone in the branch', async () => {
    const bh = await emp();
    const br = await db.query<{ id: string }>(`INSERT INTO branch (company_id, code, name_en, head_employee_id) VALUES ($1, 'PKR', 'Pokhara', $2) RETURNING id`, [DEMO.companyId, bh]);
    const there = await emp({ branchId: br.rows[0]!.id });
    const tok = (await loginAs(db, app, ['employee'], bh)).token;
    expect((await request(app).get(`/api/v1/employees/${there}`).set(auth(tok))).status).toBe(200);
    expect((await request(app).get(`/api/v1/employees/${inSub}`).set(auth(tok))).status).toBe(403);
  });

  it('losing the head post ends the access', async () => {
    const h2 = await emp();
    const u = await unit('TMP', null, h2);
    const member = await emp({ orgUnitId: u });
    const tok = (await loginAs(db, app, ['employee'], h2)).token;
    expect((await request(app).get(`/api/v1/employees/${member}`).set(auth(tok))).status).toBe(200);
    await db.query('UPDATE org_unit SET head_employee_id = NULL WHERE id = $1', [u]);
    const again = await request(app).get(`/api/v1/employees/${member}`).set(auth(tok));
    expect([401, 403]).toContain(again.status);
  });
});

describe('specialist HR roles', () => {
  let payrollTok: string; let attendanceTok: string; let recruiterTok: string; let execTok: string; let someone: string; let home: string;
  beforeAll(async () => {
    payrollTok = (await loginAs(db, app, ['payroll_admin'])).token;
    attendanceTok = (await loginAs(db, app, ['attendance_admin'])).token;
    recruiterTok = (await loginAs(db, app, ['recruiter'])).token;
    execTok = (await loginAs(db, app, ['executive'])).token;
    someone = await emp();
    home = (await leaveTypeByCode(db, DEMO.companyId, 'HOME'))!.id;
  });

  it('payroll admin works in payroll only', async () => {
    expect((await request(app).get('/api/v1/payroll/runs').set(auth(payrollTok))).status).toBe(200);
    expect((await request(app).post('/api/v1/leave/adjust').set(auth(payrollTok)).send({ employeeId: someone, leaveTypeId: home, deltaDays: 1, reason: 'test' })).status).toBe(403);
    expect((await request(app).post('/api/v1/hiring/jobs').set(auth(payrollTok)).send({})).status).toBe(403);
    expect((await request(app).get(`/api/v1/employees/${someone}/sensitive`).set(auth(payrollTok))).status).toBe(403);
    expect((await request(app).get('/api/v1/insights').set(auth(payrollTok))).status).toBe(403);
    // it can find employees to work with, but not change them
    expect((await request(app).get('/api/v1/employees').set(auth(payrollTok))).status).toBe(200);
    expect((await request(app).patch(`/api/v1/employees/${someone}`).set(auth(payrollTok)).send({ nameEn: 'X' })).status).toBe(403);
  });

  it('attendance admin handles leave and attendance, not payroll or hiring', async () => {
    const adj = await request(app).post('/api/v1/leave/adjust').set(auth(attendanceTok)).send({ employeeId: someone, leaveTypeId: home, deltaDays: 1, reason: 'opening balance' });
    expect(adj.status).toBeLessThan(300);
    expect((await request(app).get('/api/v1/leave/requests').query({ scope: 'all' }).set(auth(attendanceTok))).status).toBe(200);
    expect((await request(app).get('/api/v1/payroll/runs').set(auth(attendanceTok))).status).toBe(403);
    expect((await request(app).get('/api/v1/hiring/reports').set(auth(attendanceTok))).status).toBe(403);
  });

  it('recruiter runs hiring but cannot hire into the employee master', async () => {
    expect((await request(app).get('/api/v1/hiring/jobs').set(auth(recruiterTok))).status).toBe(200);
    expect((await request(app).get('/api/v1/hiring/reports').set(auth(recruiterTok))).status).toBe(200);
    const hire = await request(app).post(`/api/v1/hiring/applications/${someone}/hire`).set(auth(recruiterTok)).send({ empCode: 'X1' });
    expect(hire.status).toBe(403);
    expect((await request(app).get('/api/v1/payroll/runs').set(auth(recruiterTok))).status).toBe(403);
    expect((await request(app).get('/api/v1/employees').set(auth(recruiterTok))).status).toBe(403);
  });

  it('executive reads dashboards and insights, changes nothing', async () => {
    expect((await request(app).get('/api/v1/insights').set(auth(execTok))).status).toBe(200);
    expect((await request(app).get('/api/v1/dashboard/executive').set(auth(execTok))).status).toBe(200);
    expect((await request(app).post('/api/v1/leave/adjust').set(auth(execTok)).send({ employeeId: someone, leaveTypeId: home, deltaDays: 1, reason: 'x' })).status).toBe(403);
    expect((await request(app).get('/api/v1/payroll/runs').set(auth(execTok))).status).toBe(403);
    expect((await request(app).get('/api/v1/workflow/flows/config').set(auth(execTok))).status).toBe(403);
  });

  it('only an admin hands out roles', async () => {
    const u = await loginAs(db, app, ['employee']);
    expect((await request(app).patch(`/api/v1/auth/users/${u.userId}`).set(auth(hrTok)).send({ roles: ['payroll_admin'] })).status).toBe(403);
    const ok = await request(app).patch(`/api/v1/auth/users/${u.userId}`).set(auth(adminTok)).send({ roles: ['employee', 'payroll_admin'] });
    expect(ok.status).toBe(200);
    expect(ok.body.user.roles).toEqual(['employee', 'payroll_admin']);
    expect((await request(app).patch(`/api/v1/auth/users/${u.userId}`).set(auth(adminTok)).send({ roles: ['superuser'] })).status).toBe(400);
  });
});

describe('approval flows', () => {
  let mgr: string; let mgrTok: string; let deptHead: string; let headTok: string; let worker: string; let workerTok: string; let unitId: string;
  let otherUser: { token: string; userId: string };

  beforeAll(async () => {
    deptHead = await emp();
    unitId = await unit('FIN', null, deptHead);
    mgr = await emp({ orgUnitId: unitId, managerId: deptHead });
    worker = await emp({ orgUnitId: unitId, managerId: mgr });
    mgrTok = (await loginAs(db, app, ['manager'], mgr)).token;
    headTok = (await loginAs(db, app, ['employee'], deptHead)).token;
    workerTok = (await loginAs(db, app, ['employee'], worker)).token;
    otherUser = await loginAs(db, app, ['employee'], await emp());
  });

  const inbox = async (tok: string) => (await request(app).get('/api/v1/workflow/inbox').set(auth(tok))).body.items as Array<{ instanceId: string; objectId: string }>;

  it('a default chain of line manager then department head', async () => {
    const put = await request(app).put('/api/v1/workflow/flows/defaults/letter_request').set(auth(hrTok))
      .send({ steps: [{ kind: 'manager' }, { kind: 'department_head' }] });
    expect(put.status).toBe(200);
    const objectId = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
    const inst = await startWorkflow(db, { companyId: DEMO.companyId, objectType: 'letter_request', objectId, employeeId: worker, title: 't', body: 'b' });
    expect(inst.steps.map((s) => s.kind)).toEqual(['manager', 'department_head']);
    expect((await inbox(mgrTok)).map((i) => i.instanceId)).toContain(inst.id);
    expect((await inbox(headTok)).map((i) => i.instanceId)).not.toContain(inst.id);
    expect((await request(app).post(`/api/v1/workflow/${inst.id}/approve`).set(auth(mgrTok)).send({})).body.state).toBe('pending');
    expect((await inbox(headTok)).map((i) => i.instanceId)).toContain(inst.id);
    // the requester can never decide their own request
    expect((await request(app).post(`/api/v1/workflow/${inst.id}/approve`).set(auth(workerTok)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${inst.id}/approve`).set(auth(headTok)).send({})).body.state).toBe('approved');
  });

  it('the most specific conditional flow wins, and groups approve', async () => {
    const g = await request(app).post('/api/v1/workflow/groups').set(auth(hrTok)).send({ name: 'Finance approvers', userIds: [otherUser.userId] });
    expect(g.status).toBe(201);
    const dept = await request(app).post('/api/v1/workflow/flows').set(auth(hrTok))
      .send({ objectType: 'letter_request', name: 'Finance department', conditions: { orgUnitIds: [unitId] }, steps: [{ kind: 'group', groupId: g.body.id }] });
    expect(dept.status).toBe(201);
    const person = await request(app).post('/api/v1/workflow/flows').set(auth(hrTok))
      .send({ objectType: 'letter_request', name: 'Just this person', conditions: { employeeIds: [mgr] }, steps: [{ kind: 'role', role: 'hr' }] });
    expect(person.status).toBe(201);

    const p1 = await request(app).get('/api/v1/workflow/preview').set(auth(workerTok)).query({ objectType: 'letter_request' });
    expect(p1.status).toBe(200);
    expect(p1.body.flow.name).toBe('Finance department');
    expect(p1.body.steps[0].approvers.map((a: { id: string }) => a.id)).toEqual([otherUser.userId]);
    const p2 = await request(app).get('/api/v1/workflow/preview').set(auth(mgrTok)).query({ objectType: 'letter_request' });
    expect(p2.body.flow.name).toBe('Just this person');

    const objectId = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
    const inst = await startWorkflow(db, { companyId: DEMO.companyId, objectType: 'letter_request', objectId, employeeId: worker, title: 't', body: 'b' });
    expect((await inbox(otherUser.token)).map((i) => i.instanceId)).toContain(inst.id);
    expect((await inbox(mgrTok)).map((i) => i.instanceId)).not.toContain(inst.id);
    expect((await request(app).post(`/api/v1/workflow/${inst.id}/approve`).set(auth(otherUser.token)).send({})).body.state).toBe('approved');
  });

  it('amount thresholds add a step only for larger requests', async () => {
    await request(app).put('/api/v1/workflow/flows/defaults/expense_claim').set(auth(hrTok))
      .send({ steps: [{ kind: 'manager' }, { kind: 'role', role: 'hr', minAmount: 10000 }] });
    const small = await request(app).get('/api/v1/workflow/preview').set(auth(workerTok)).query({ objectType: 'expense_claim', amount: 5000 });
    const big = await request(app).get('/api/v1/workflow/preview').set(auth(workerTok)).query({ objectType: 'expense_claim', amount: 25000 });
    expect(small.body.steps).toHaveLength(1);
    expect(big.body.steps).toHaveLength(2);
    const objectId = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
    const inst = await startWorkflow(db, { companyId: DEMO.companyId, objectType: 'expense_claim', objectId, employeeId: worker, title: 't', body: 'b', amount: 4000 });
    expect(inst.steps).toHaveLength(1);
  });

  it('an approver on leave hands over to their delegate, or to HR', async () => {
    await request(app).put('/api/v1/workflow/flows/defaults/training_nomination').set(auth(hrTok)).send({ steps: [{ kind: 'manager' }] });
    const home = (await leaveTypeByCode(db, DEMO.companyId, 'HOME'))!.id;
    const today = nptToday();
    await db.query(`INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status) VALUES ($1, $2, $3, $3, 1, 'approved')`, [mgr, home, today]);
    const objectId = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
    const inst = await startWorkflow(db, { companyId: DEMO.companyId, objectType: 'training_nomination', objectId, employeeId: worker, title: 't', body: 'b' });
    const routed = await db.query<{ routing: Array<{ notes: string[] }> }>('SELECT routing FROM workflow_instance WHERE id = $1', [inst.id]);
    expect(routed.rows[0]!.routing[0]!.notes.join(' ')).toMatch(/on leave/);
    expect((await inbox(mgrTok)).map((i) => i.instanceId)).not.toContain(inst.id);
    expect((await inbox(hrTok)).map((i) => i.instanceId)).toContain(inst.id);

    // the manager names a delegate: the delegate now decides
    const mgrUser = (await db.query<{ id: string }>('SELECT id FROM app_user WHERE employee_id = $1', [mgr])).rows[0]!.id;
    const del = await request(app).post('/api/v1/workflow/delegates').set(auth(mgrTok)).send({ delegateId: otherUser.userId, from: today, to: today });
    expect(del.status).toBe(201);
    expect((await inbox(otherUser.token)).map((i) => i.instanceId)).toContain(inst.id);
    // someone else cannot set or cancel the manager's delegation
    expect((await request(app).post('/api/v1/workflow/delegates').set(auth(workerTok)).send({ userId: mgrUser, delegateId: otherUser.userId, from: today, to: today })).status).toBe(403);
    expect((await request(app).delete(`/api/v1/workflow/delegates/${del.body.id}`).set(auth(workerTok))).status).toBe(404);
    await db.query('DELETE FROM leave_request WHERE employee_id = $1', [mgr]);
  });

  it('verify lists employees whose chain reaches nobody, with a fix', async () => {
    const orphan = await emp();
    const v = await request(app).get('/api/v1/workflow/verify').set(auth(hrTok)).query({ objectType: 'leave_request' });
    expect(v.status).toBe(200);
    const mine = v.body.problems.filter((p: { employeeId: string }) => p.employeeId === orphan);
    expect(mine.length).toBe(1);
    expect(mine[0].suggestion).toMatch(/line manager/);
    await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [orphan, mgr]);
    const v2 = await request(app).get('/api/v1/workflow/verify').set(auth(hrTok)).query({ objectType: 'leave_request' });
    expect(v2.body.problems.filter((p: { employeeId: string }) => p.employeeId === orphan)).toHaveLength(0);
  });

  it('a leave request follows the configured chain end to end', async () => {
    await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(hrTok)).send({ steps: [{ kind: 'department_head' }] });
    const home = (await leaveTypeByCode(db, DEMO.companyId, 'HOME'))!.id;
    await request(app).post('/api/v1/leave/adjust').set(auth(hrTok)).send({ employeeId: worker, leaveTypeId: home, deltaDays: 5, reason: 'opening', kind: 'allocation' });
    const lr = await request(app).post('/api/v1/leave/requests').set(auth(workerTok)).send({ leaveTypeId: home, from: '2026-12-07', to: '2026-12-07', reason: 'errand' });
    expect(lr.status).toBe(201);
    const items = await inbox(headTok);
    const it2 = items.find((i) => i.objectId === lr.body.id);
    expect(it2).toBeTruthy();
    expect((await inbox(mgrTok)).find((i) => i.objectId === lr.body.id)).toBeUndefined();
    expect((await request(app).post(`/api/v1/workflow/${it2!.instanceId}/approve`).set(auth(headTok)).send({})).body.state).toBe('approved');
    await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(hrTok)).send({ steps: [{ kind: 'manager' }] });
  });

  it('nobody outside HR edits flows, and references must stay inside the company', async () => {
    expect((await request(app).get('/api/v1/workflow/flows/config').set(auth(mgrTok))).status).toBe(403);
    expect((await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(mgrTok)).send({ steps: [{ kind: 'manager' }] })).status).toBe(403);
    expect((await request(app).post('/api/v1/workflow/groups').set(auth(mgrTok)).send({ name: 'x' })).status).toBe(403);
    const foreign = '00000000-0000-7000-8000-00000000dead';
    expect((await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(hrTok)).send({ steps: [{ kind: 'user', userId: foreign }] })).status).toBe(400);
    expect((await request(app).post('/api/v1/workflow/flows').set(auth(hrTok)).send({ objectType: 'leave_request', name: 'x', conditions: { branchIds: [foreign] }, steps: [{ kind: 'manager' }] })).status).toBe(400);
    expect((await request(app).put('/api/v1/workflow/flows/defaults/not_a_type').set(auth(hrTok)).send({ steps: [{ kind: 'manager' }] })).status).toBe(400);
    expect((await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(hrTok)).send({ steps: [{ kind: 'role' }] })).status).toBe(400);
    expect((await request(app).put('/api/v1/workflow/flows/defaults/leave_request').set(auth(hrTok)).send({ steps: [{ kind: 'role', role: 'employee' }] })).status).toBe(400);
  });

  it('preview for someone else needs to manage them', async () => {
    expect((await request(app).get('/api/v1/workflow/preview').set(auth(workerTok)).query({ objectType: 'leave_request', employeeId: mgr })).status).toBe(403);
    expect((await request(app).get('/api/v1/workflow/preview').set(auth(mgrTok)).query({ objectType: 'leave_request', employeeId: worker })).status).toBe(200);
  });

  it('a group in use cannot be deleted', async () => {
    const cfg = await request(app).get('/api/v1/workflow/flows/config').set(auth(hrTok));
    const g = cfg.body.groups.find((x: { name: string }) => x.name === 'Finance approvers');
    expect((await request(app).delete(`/api/v1/workflow/groups/${g.id}`).set(auth(hrTok))).status).toBe(400);
  });
});
