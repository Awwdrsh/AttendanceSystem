import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string;
beforeAll(async () => { db = await freshDb(); app = testApp(db); hr = (await loginAs(db, app, ['hr'])).token; });
afterAll(async () => { await db.end(); });

describe('shifts', () => {
  let nightId = '';
  it('creates a night shift and reports crosses_midnight', async () => {
    const r = await request(app).post('/api/v1/shifts').set(auth(hr)).send({ code: 'NIGHT', nameEn: 'Night', start: '22:00', end: '06:00', breakMinutes: 30 });
    expect(r.status).toBe(201);
    nightId = r.body.id;
    const list = await request(app).get('/api/v1/shifts').set(auth(hr));
    expect(list.body.shifts.find((s: { code: string }) => s.code === 'NIGHT')).toMatchObject({ crosses_midnight: true, assigned_employees: '0' });
    expect((await request(app).post('/api/v1/shifts').set(auth(hr)).send({ code: 'BAD', nameEn: 'Bad', start: '25:00', end: '06:00' })).status).toBe(400);
  });
  it('assigns effective-dated and refuses to delete a shift in use', async () => {
    const a = await request(app).post('/api/v1/shifts/assign').set(auth(hr)).send({ employeeId: DEMO.ram, shiftId: nightId, from: '2026-10-01' });
    expect(a.status).toBe(201);
    const rows = await db.query<{ shift_id: string; valid: string }>('SELECT shift_id, valid::text FROM shift_assignment WHERE employee_id = $1 ORDER BY lower(valid)', [DEMO.ram]);
    expect(rows.rows).toHaveLength(2);
    expect(rows.rows[0]).toMatchObject({ shift_id: DEMO.shiftId, valid: '[2026-01-01,2026-10-01)' });
    const d = await request(app).delete(`/api/v1/shifts/${nightId}`).set(auth(hr));
    expect(d.status).toBe(409);
    expect(d.body.error).toMatch(/assigned to 1 employee/);
  });
  it('roster upsert queues recompute and refuses locked days', async () => {
    const r = await request(app).put('/api/v1/shifts/roster').set(auth(hr)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-10-20', shiftId: nightId }, { employeeId: DEMO.sita, localDate: '2026-10-21', shiftId: null }] });
    expect(r.body.upserted).toBe(2);
    const q = await db.query(`SELECT local_date FROM recompute_queue WHERE employee_id = $1 ORDER BY local_date`, [DEMO.sita]);
    expect(q.rows.map((x) => x.local_date)).toEqual(['2026-10-20', '2026-10-21']);
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at) VALUES ($1, '2026-10-22', 1, 'Present', 'h', now())`, [DEMO.sita]);
    expect((await request(app).put('/api/v1/shifts/roster').set(auth(hr)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-10-22', shiftId: nightId }] })).status).toBe(409);
    const copy = await request(app).post('/api/v1/shifts/roster/copy-week').set(auth(hr)).send({ weekStart: '2026-10-25' });
    expect(copy.body.copied).toBe(2);
  });
  it('holiday import is idempotent and the calendar marks Saturdays', async () => {
    const first = await request(app).post('/api/v1/shifts/holidays/import-default').set(auth(hr)).send({ bsYear: 2083 });
    expect(first.body).toEqual({ inserted: 20, total: 20 });
    const second = await request(app).post('/api/v1/shifts/holidays/import-default').set(auth(hr)).send({ bsYear: 2083 });
    expect(second.body.inserted).toBe(0);
    const cal = await request(app).get('/api/v1/shifts/calendar').set(auth(hr)).query({ from: '2026-10-17', to: '2026-10-24' });
    const sat = cal.body.days.find((d: { date: string }) => d.date === '2026-10-17');
    expect(sat).toMatchObject({ weeklyOff: true, bsDate: '2083-06-31' });
    expect(sat.holidays[0].name_en).toMatch(/Dashain/);
    const h = await request(app).post('/api/v1/shifts/holidays').set(auth(hr)).send({ localDate: '2026-12-30', nameEn: 'Tamu Lhosar' });
    expect(h.status).toBe(201);
    expect(h.body.recomputeQueued).toBe(2);
  });
  it('locations CRUD', async () => {
    const c = await request(app).post('/api/v1/shifts/locations').set(auth(hr)).send({ name: 'HQ', lat: 27.7172, lng: 85.324, radiusM: 100 });
    expect(c.status).toBe(201);
    expect((await request(app).get('/api/v1/shifts/locations').set(auth(hr))).body.locations).toHaveLength(1);
    expect((await request(app).delete(`/api/v1/shifts/locations/${c.body.id}`).set(auth(hr))).status).toBe(200);
  });
});
