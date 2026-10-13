import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { leaveTypeByCode, ledgerBalance } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let ram: string; let otherMgr: string; let sita: string;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const m = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E902', 'Unrelated Manager') RETURNING id`, [DEMO.companyId]);
  otherMgr = (await loginAs(db, app, ['manager'], m.rows[0]!.id)).token;
});
afterAll(async () => { await db.end(); });

describe('overtime slip guards', () => {
  it('only the line manager (or HR) decides a slip; the employee never does', async () => {
    const s = await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES ($1, '2026-10-01', 60, 'working') RETURNING id`, [DEMO.sita]);
    expect((await request(app).post(`/api/v1/ot/slips/${s.rows[0]!.id}/approve`).set(auth(otherMgr)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/ot/slips/${s.rows[0]!.id}/approve`).set(auth(sita)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/ot/slips/${s.rows[0]!.id}/approve`).set(auth(ram)).send({ approvedMin: 30 })).body.approvedMin).toBe(30);
  });

  it('a slip converted to substitute leave cannot be approved, rejected or converted again', async () => {
    const s = await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES ($1, '2026-10-03', 240, 'weekly_off') RETURNING id`, [DEMO.sita]);
    const id = s.rows[0]!.id;
    expect((await request(app).post(`/api/v1/ot/slips/${id}/comp-off`).set(auth(hr))).body.days).toBe(0.5);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/approve`).set(auth(hr)).send({})).status).toBe(409);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/reject`).set(auth(hr)).send({ reason: 'oops' })).status).toBe(409);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/comp-off`).set(auth(hr))).status).toBe(409);
    const sub = (await leaveTypeByCode(db, DEMO.companyId, 'SUBSTITUTE'))!.id;
    expect(await ledgerBalance(db, DEMO.sita, sub)).toBe(0.5);
  });

  it('a manager cannot pre-approve overtime for a non-report; the register CSV opens cleanly in Excel', async () => {
    expect((await request(app).post('/api/v1/ot/requests').set(auth(otherMgr)).send({ employeeId: DEMO.sita, localDate: '2026-10-05', requestedMin: 60 })).status).toBe(403);
    const csv = await request(app).get('/api/v1/ot/register').set(auth(hr)).query({ from: '2026-10-01', to: '2026-10-31', format: 'csv' });
    expect(csv.text.startsWith('﻿emp_code,')).toBe(true);
    expect(csv.text).toContain('\r\n');
  });
});
