import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let sita: string; let ram: string;
const nowIso = () => new Date().toISOString();
const plusDays = (d: string, n: number) => new Date(new Date(`${d}T00:00:00Z`).getTime() + n * 86_400_000).toISOString().slice(0, 10);
const home = { lat: 27.80, lng: 85.40, accuracyM: 30, source: 'web' as const };
// A clock-in from home carries a photo (a tiny JPEG is enough for the API).
const selfie = `data:image/jpeg;base64,${Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.from('test-photo')]).toString('base64')}`;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES ($1, 'Head office', 27.7172, 85.3240, 150)`, [DEMO.companyId]);
});
afterAll(async () => { await db.end(); });

describe('work from home', () => {
  it('HR can mark an employee as allowed to work from home', async () => {
    const r = await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ workFromHome: true });
    expect(r.status).toBe(200);
    const e = await request(app).get(`/api/v1/employees/${DEMO.ram}`).set(auth(hr));
    expect(e.body.employee?.work_from_home ?? e.body.work_from_home).toBe(true);
  });

  it('an employee allowed to work from home clocks in from home without a reason, tagged remote', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(ram)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'wfh-ram-in1', ...home, selfie });
    expect(r.status).toBe(201);
    expect(r.body.remote).toBe(true);
    expect(r.body.flags).toContain('Remote');
    expect(r.body.flags).not.toContain('OutsideLocation');
    expect(r.body.message).toMatch(/from home$/);
    const t = await request(app).get('/api/v1/punch/today').set(auth(ram));
    expect(t.body.punches.at(-1).remote).toBe(true);
    const team = await request(app).get('/api/v1/attendance/today').set(auth(hr)).query({ date: nptToday() });
    expect(team.body.rows.find((x: { employee_id: string }) => x.employee_id === DEMO.ram).remote).toBe(true);
    expect(team.body.rows.find((x: { employee_id: string }) => x.employee_id === DEMO.sita).remote).toBe(false);
  });

  it('an office employee away from the office still needs a reason', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'wfh-sita-in1', ...home });
    expect(r.status).toBe(400);
  });

  it('work from home can be planned ahead, does not use the correction allowance, and once approved allows a home clock-in', async () => {
    const today = nptToday();
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: plusDays(today, 61), kind: 'wfh', reason: 'too far ahead' })).status).toBe(400);
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: plusDays(today, 3), kind: 'missed_punch', punchTime: nowIso(), punchDirection: 'in', reason: 'future correction' })).status).toBe(400);
    const ahead = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: plusDays(today, 7), kind: 'wfh', reason: 'Plumber visiting' });
    expect(ahead.status).toBe(201);
    // The monthly cap (5) applies to corrections only; six work-from-home days in a row are fine.
    for (let i = 8; i <= 13; i++) {
      expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: plusDays(today, i), kind: 'wfh', reason: 'Home week' })).status).toBe(201);
    }
    const req = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: today, kind: 'wfh', reason: 'Sick child at home' });
    expect(req.status).toBe(201);
    const inst = (await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM regularisation_request WHERE id = $1', [req.body.id])).rows[0]!.workflow_instance_id;
    expect((await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(ram)).send({})).status).toBe(200);
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'wfh-sita-in2', ...home, selfie });
    expect(r.status).toBe(201);
    expect(r.body.remote).toBe(true);
  });
});
