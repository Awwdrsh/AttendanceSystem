import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { leaveTypeByCode } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let otherMgr: string; let admin: string; let adminUserId: string;
let foreign: string; let foreignEmp: string;
const lwp = async () => (await leaveTypeByCode(db, DEMO.companyId, 'LWP'))!.id;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  const a = await loginAs(db, app, ['admin'], null); admin = a.token; adminUserId = a.userId;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  // A manager with their own report who is NOT Sita's manager.
  const mgrEmp = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E900', 'Other Manager') RETURNING id`, [DEMO.companyId]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES ($1, daterange('2026-01-01', NULL), 'regular')`, [mgrEmp.rows[0]!.id]);
  otherMgr = (await loginAs(db, app, ['manager'], mgrEmp.rows[0]!.id)).token;
  // A second company with its own HR user and employee.
  const c2 = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`);
  const e2 = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'X1', 'Foreign') RETURNING id`, [c2.rows[0]!.id]);
  foreignEmp = e2.rows[0]!.id;
  await createUser(db, { companyId: c2.rows[0]!.id, email: 'hr@other.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreign = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.local', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

async function sitaRequest(from: string): Promise<{ requestId: string; instanceId: string }> {
  const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: await lwp(), from, to: from, reason: 'qa' });
  expect(r.status).toBe(201);
  const i = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [r.body.id]);
  return { requestId: r.body.id, instanceId: i.rows[0]!.workflow_instance_id };
}

describe('workflow authorisation', () => {
  it('a manager who is not the line manager cannot approve, see, or be shown the request', async () => {
    const { instanceId } = await sitaRequest('2026-11-10');
    expect((await request(app).get('/api/v1/workflow/inbox').set(auth(otherMgr))).body.items.map((i: { instanceId: string }) => i.instanceId)).not.toContain(instanceId);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(otherMgr)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/reject`).set(auth(otherMgr)).send({ reason: 'no' })).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(ram)).send({})).body.state).toBe('approved');
  });

  it('another company cannot even see the instance (404, not 403)', async () => {
    const { instanceId } = await sitaRequest('2026-11-11');
    expect((await request(app).get(`/api/v1/workflow/${instanceId}`).set(auth(foreign))).status).toBe(404);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(foreign)).send({})).status).toBe(404);
    const bulk = await request(app).post('/api/v1/workflow/bulk-approve').set(auth(foreign)).send({ ids: [instanceId] });
    expect(bulk.body.results[0].error).toMatch(/not found/);
    expect((await db.query('SELECT state FROM workflow_instance WHERE id = $1', [instanceId])).rows[0]).toEqual({ state: 'pending' });
  });

  it('an admin cannot approve their own request; a manager cannot file leave for a non-report', async () => {
    const adminEmp = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E904', 'Admin Person') RETURNING id`, [DEMO.companyId]);
    await db.query(`INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES ($1, daterange('2026-01-01', NULL), 'regular')`, [adminEmp.rows[0]!.id]);
    await db.query('UPDATE app_user SET employee_id = $2 WHERE id = $1', [adminUserId, adminEmp.rows[0]!.id]);
    const r = await request(app).post('/api/v1/leave/requests').set(auth(admin)).send({ leaveTypeId: await lwp(), from: '2026-11-12', to: '2026-11-12' });
    expect(r.status).toBe(201);
    const i = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [r.body.id]);
    expect((await request(app).post(`/api/v1/workflow/${i.rows[0]!.workflow_instance_id}/approve`).set(auth(admin)).send({})).status).toBe(403);
    await db.query('UPDATE app_user SET employee_id = NULL WHERE id = $1', [adminUserId]);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(otherMgr)).send({ employeeId: DEMO.sita, leaveTypeId: await lwp(), from: '2026-11-13', to: '2026-11-13' })).status).toBe(403);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(ram)).send({ employeeId: DEMO.sita, leaveTypeId: await lwp(), from: '2026-11-13', to: '2026-11-13' })).status).toBe(201);
  });

  it('acting on behalf is HR-only and must name a real approver of the step', async () => {
    const { instanceId } = await sitaRequest('2026-11-16');
    const ramUser = (await db.query<{ id: string }>('SELECT id FROM app_user WHERE employee_id = $1', [DEMO.ram])).rows[0]!.id;
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(ram)).send({ onBehalfOf: ramUser })).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(hr)).send({ onBehalfOf: adminUserId })).status).toBe(400);
    const ok = await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(hr)).send({ onBehalfOf: ramUser });
    expect(ok.body.state).toBe('approved');
    const act = await db.query<{ on_behalf_of: string }>('SELECT on_behalf_of FROM approval_action WHERE instance_id = $1', [instanceId]);
    expect(act.rows[0]!.on_behalf_of).toBe(ramUser);
  });

  it('employees never see other employees through leave balances, ledger or requests; managers only their reports', async () => {
    expect((await request(app).get('/api/v1/leave/balances').set(auth(sita)).query({ employeeId: DEMO.ram })).status).toBe(403);
    expect((await request(app).get('/api/v1/leave/ledger').set(auth(sita)).query({ employeeId: DEMO.ram })).status).toBe(403);
    expect((await request(app).get('/api/v1/leave/balances').set(auth(otherMgr)).query({ employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get('/api/v1/leave/balances').set(auth(ram)).query({ employeeId: DEMO.sita })).status).toBe(200);
    expect((await request(app).get('/api/v1/leave/balances').set(auth(foreign)).query({ employeeId: DEMO.sita })).status).toBe(404);
    expect((await request(app).get('/api/v1/leave/balances').set(auth(hr)).query({ employeeId: foreignEmp })).status).toBe(404);
  });

  it('a definition change does not alter in-flight instances', async () => {
    const { instanceId } = await sitaRequest('2026-11-17');
    await db.query(`UPDATE workflow_definition SET steps = '[{"role":"manager"},{"role":"hr"},{"role":"admin"}]' WHERE company_id = $1 AND object_type = 'leave_request'`, [DEMO.companyId]);
    expect((await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(ram)).send({})).body.state).toBe('approved');
    await db.query(`UPDATE workflow_definition SET steps = '[{"role":"manager"}]' WHERE company_id = $1 AND object_type = 'leave_request'`, [DEMO.companyId]);
  });
});
