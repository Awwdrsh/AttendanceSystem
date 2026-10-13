import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { leaveTypeByCode } from '../src/modules/leave/service.js';
import { pendingOlderThan } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let other: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  other = (await loginAs(db, app, ['manager'])).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
});
afterAll(async () => { await db.end(); });

describe('workflow engine', () => {
  it('two-step chain: manager then HR; inbox is scoped to the current approver; bulk approve; timeline', async () => {
    await db.query(`UPDATE workflow_definition SET steps = '[{"role":"manager"},{"role":"hr"}]' WHERE company_id = $1 AND object_type = 'leave_request'`, [DEMO.companyId]);
    const lwp = (await leaveTypeByCode(db, DEMO.companyId, 'LWP'))!.id;
    const a = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: lwp, from: '2026-11-02', to: '2026-11-02' });
    const b = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: lwp, from: '2026-11-03', to: '2026-11-03' });
    expect(a.status).toBe(201); expect(b.status).toBe(201);
    expect((await request(app).get('/api/v1/workflow/inbox').set(auth(other))).body.items).toHaveLength(0);
    const mgr = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(mgr.body.items).toHaveLength(2);
    expect(mgr.body.items[0]).toMatchObject({ step: 0, totalSteps: 2 });
    const bulk = await request(app).post('/api/v1/workflow/bulk-approve').set(auth(ram)).send({ ids: mgr.body.items.map((i: { instanceId: string }) => i.instanceId) });
    expect(bulk.body.results.every((r: { state: string }) => r.state === 'pending')).toBe(true);
    expect((await request(app).get('/api/v1/workflow/inbox').set(auth(ram))).body.items).toHaveLength(0);
    const hrInbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    expect(hrInbox.body.items).toHaveLength(2);
    expect(hrInbox.body.items[0].step).toBe(1);
    const final = await request(app).post(`/api/v1/workflow/${hrInbox.body.items[0].instanceId}/approve`).set(auth(hr)).send({});
    expect(final.body.state).toBe('approved');
    const tl = await request(app).get(`/api/v1/workflow/${hrInbox.body.items[0].instanceId}`).set(auth(hr));
    expect(tl.body.actions).toHaveLength(2);
    expect(tl.body.instance.state).toBe('approved');
    expect((await db.query(`SELECT status FROM leave_request WHERE id = $1`, [hrInbox.body.items[0].objectId])).rows[0]).toEqual({ status: 'approved' });
  });
  it('pendingOlderThan finds stale approvals with their approvers', async () => {
    await db.query(`UPDATE workflow_instance SET created_at = now() - interval '30 hours' WHERE state = 'pending'`);
    const stale = await pendingOlderThan(db, 24);
    expect(stale).toHaveLength(1);
    expect(stale[0]!.approvers.length).toBeGreaterThan(0);
  });
});
