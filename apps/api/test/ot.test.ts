import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { ledgerBalance, leaveTypeByCode } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string;
const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10);

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
});
afterAll(async () => { await db.end(); });

describe('overtime pre-approval', () => {
  it('rejects more than 4 hours a day and more than 24 hours a week (Sec 30(1))', async () => {
    expect((await request(app).post('/api/v1/ot/requests').set(auth(sita)).send({ localDate: daysAgo(1), requestedMin: 300 })).status).toBe(400);
    // Monday..Saturday of a past week each already carry 4 h of OT.
    const base = new Date('2026-09-13T00:00:00Z'); // Sunday
    for (let i = 0; i < 6; i++) {
      const d = new Date(base.getTime() + i * 86_400_000).toISOString().slice(0, 10);
      await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, ot_min, inputs_hash) VALUES ($1, $2, 1, 'Present', 240, 'h')`, [DEMO.sita, d]);
    }
    const r = await request(app).post('/api/v1/ot/requests').set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-09-19', requestedMin: 60 });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatch(/weekly overtime cap/);
  });

  it('manager approves a smaller amount than requested; past-dated window applies to employees', async () => {
    const d = daysAgo(1);
    const r = await request(app).post('/api/v1/ot/requests').set(auth(sita)).send({ localDate: d, requestedMin: 120, reason: 'month-end close' });
    expect(r.status).toBe(201);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === r.body.id);
    expect(item.summary).toMatchObject({ requested_min: 120 });
    const ok = await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(ram)).send({ approvedMin: 60 });
    expect(ok.body.state).toBe('approved');
    const row = await db.query<{ approved_min: number; status: string }>('SELECT approved_min, status FROM ot_request WHERE id = $1', [r.body.id]);
    expect(row.rows[0]).toEqual({ approved_min: 60, status: 'approved' });
    expect((await request(app).post('/api/v1/ot/requests').set(auth(sita)).send({ localDate: daysAgo(20), requestedMin: 30 })).status).toBe(400);
    expect((await request(app).post('/api/v1/ot/requests').set(auth(sita)).send({ localDate: d, requestedMin: 30 })).status).toBe(409);
  });

  it('ineligible grades cannot request OT', async () => {
    await db.query('UPDATE employee SET ot_eligible = false WHERE id = $1', [DEMO.ram]);
    expect((await request(app).post('/api/v1/ot/requests').set(auth(ram)).send({ localDate: daysAgo(1), requestedMin: 30 })).status).toBe(400);
    await db.query('UPDATE employee SET ot_eligible = true WHERE id = $1', [DEMO.ram]);
  });
});

describe('overtime slips', () => {
  let slipId = '';
  it('HR approves a detected slip capped at the detected minutes; a posted slip is immutable', async () => {
    const ins = await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES ($1, '2026-09-21', 120, 'working') RETURNING id`, [DEMO.ram]);
    slipId = ins.rows[0]!.id;
    const own = await request(app).get('/api/v1/ot/slips').set(auth(sita));
    expect(own.body.slips).toHaveLength(0);
    const a = await request(app).post(`/api/v1/ot/slips/${slipId}/approve`).set(auth(hr)).send({ approvedMin: 200 });
    expect(a.body.approvedMin).toBe(120);
    await db.query(`UPDATE ot_slip SET status = 'posted' WHERE id = $1`, [slipId]);
    expect((await request(app).post(`/api/v1/ot/slips/${slipId}/reject`).set(auth(hr)).send({ reason: 'already posted' })).status).toBe(409);
  });
  it('comp-off credits substitute leave expiring 21 days later (Sec 42)', async () => {
    const ins = await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES ($1, '2026-09-26', 240, 'weekly_off') RETURNING id`, [DEMO.ram]);
    const r = await request(app).post(`/api/v1/ot/slips/${ins.rows[0]!.id}/comp-off`).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true, days: 0.5, expiresOn: '2026-10-17' });
    const sub = (await leaveTypeByCode(db, DEMO.companyId, 'SUBSTITUTE'))!.id;
    expect(await ledgerBalance(db, DEMO.ram, sub)).toBe(0.5);
    const csv = await request(app).get('/api/v1/ot/register').set(auth(hr)).query({ from: '2026-09-01', to: '2026-09-30', format: 'csv' });
    expect(csv.text.split('\n')[0]).toBe('emp_code,name_en,local_date,bs_date,day_type,detected_min,approved_min,multiplier,status');
    expect(csv.text).toContain('E002,Ram Thapa,2026-09-26');
  });
});
