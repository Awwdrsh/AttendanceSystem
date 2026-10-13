import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let ram: string; let otherMgr: string;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const m = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E903', 'Unrelated Manager') RETURNING id`, [DEMO.companyId]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES ($1, daterange('2026-01-01', NULL), 'regular')`, [m.rows[0]!.id]);
  otherMgr = (await loginAs(db, app, ['manager'], m.rows[0]!.id)).token;
});
afterAll(async () => { await db.end(); });

describe('shift and roster guards', () => {
  it('roster rows must use shifts of the same company', async () => {
    const c2 = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('C3', 'C3') RETURNING id`);
    const sh = await db.query<{ id: string }>(`INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES ($1, 'Z', 'Z', '09:00', '17:00') RETURNING id`, [c2.rows[0]!.id]);
    const r = await request(app).put('/api/v1/shifts/roster').set(auth(hr)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-11-02', shiftId: sh.rows[0]!.id }] });
    expect(r.status).toBe(404);
    expect((await db.query('SELECT 1 FROM roster WHERE employee_id = $1', [DEMO.sita])).rows).toHaveLength(0);
  });

  it('a manager rosters only direct reports and sees only them', async () => {
    expect((await request(app).put('/api/v1/shifts/roster').set(auth(otherMgr)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-11-02', shiftId: DEMO.shiftId }] })).status).toBe(403);
    expect((await request(app).put('/api/v1/shifts/roster').set(auth(ram)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-11-02', shiftId: DEMO.shiftId }] })).status).toBe(200);
    const view = await request(app).get('/api/v1/shifts/roster').set(auth(otherMgr)).query({ from: '2026-11-01', to: '2026-11-07' });
    expect(view.body.employees.map((e: { id: string }) => e.id)).not.toContain(DEMO.sita);
    expect(view.body.roster).toHaveLength(0);
    const own = await request(app).get('/api/v1/shifts/roster').set(auth(ram)).query({ from: '2026-11-01', to: '2026-11-07' });
    expect(own.body.employees.map((e: { id: string }) => e.id)).toContain(DEMO.sita);
    expect(own.body.employees.map((e: { id: string }) => e.id)).not.toContain(DEMO.ram === DEMO.sita ? '' : undefined);
  });

  it('copy-week skips payroll-locked target days', async () => {
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at) VALUES ($1, '2026-11-09', 1, 'Present', 'h', now())`, [DEMO.sita]);
    const copy = await request(app).post('/api/v1/shifts/roster/copy-week').set(auth(hr)).send({ weekStart: '2026-11-08' });
    expect(copy.body).toEqual({ copied: 0, skippedLocked: 1 });
    expect((await db.query(`SELECT 1 FROM roster WHERE employee_id = $1 AND local_date = '2026-11-09'`, [DEMO.sita])).rows).toHaveLength(0);
  });

  it('duplicate company-wide holidays are refused and the default import stays idempotent', async () => {
    expect((await request(app).post('/api/v1/shifts/holidays').set(auth(hr)).send({ localDate: '2026-12-30', nameEn: 'Tamu Lhosar' })).status).toBe(201);
    expect((await request(app).post('/api/v1/shifts/holidays').set(auth(hr)).send({ localDate: '2026-12-30', nameEn: 'Tamu Lhosar' })).status).toBe(409);
    expect((await request(app).post('/api/v1/shifts/holidays/import-default').set(auth(hr)).send({ bsYear: 2083 })).body.inserted).toBe(20);
    expect((await request(app).post('/api/v1/shifts/holidays/import-default').set(auth(hr)).send({ bsYear: 2083 })).body.inserted).toBe(0);
    const n = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM holiday WHERE company_id = $1 AND local_date = '2026-10-17'`, [DEMO.companyId]);
    expect(n.rows[0]!.n).toBe('1');
    const bs = await db.query<{ bs_date: string | null }>(`SELECT bs_date FROM holiday WHERE company_id = $1 AND local_date = '2026-10-17'`, [DEMO.companyId]);
    expect(bs.rows[0]!.bs_date).toBe('2083-06-31');
  });

  it('a holiday added by a manager is refused (HR only)', async () => {
    expect((await request(app).post('/api/v1/shifts/holidays').set(auth(ram)).send({ localDate: '2026-12-31', nameEn: 'x' })).status).toBe(403);
  });
});
