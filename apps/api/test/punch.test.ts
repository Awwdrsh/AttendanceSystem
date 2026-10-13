import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let sita: string; let hr: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  hr = (await loginAs(db, app, ['hr'])).token;
  await db.query(`INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES ($1, 'Head office', 27.7172, 85.3240, 150)`, [DEMO.companyId]);
});
afterAll(async () => { await db.end(); });

const nowIso = () => new Date().toISOString();

describe('POST /api/v1/punch', () => {
  it('rejects mocked locations with a plain message', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'mock-0001', lat: 27.7172, lng: 85.324, mocked: true, source: 'mobile' });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatch(/fake location/);
  });
  it('inside the geofence: stores the punch, recomputes and returns the day', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'in-000001', lat: 27.7173, lng: 85.3241, accuracyM: 20, source: 'mobile' });
    expect(r.status).toBe(201);
    expect(r.body.duplicate).toBe(false);
    expect(r.body.location.inside).toBe(true);
    expect(r.body.day.localDate).toBe(localDateOf(Date.now()));
    expect(r.body.message).toMatch(/^Clocked in at \d\d:\d\d$/);
    const dev = await db.query(`SELECT serial_no, vendor FROM device WHERE serial_no = 'MOBILE-DEMO'`);
    expect(dev.rows[0]).toMatchObject({ vendor: 'mobile' });
  });
  it('replaying the same idempotency key is a no-op', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: 'in-000001', lat: 27.7173, lng: 85.3241, source: 'mobile' });
    expect(r.status).toBe(200);
    expect(r.body.duplicate).toBe(true);
    const n = await db.query(`SELECT count(*)::int AS n FROM punch_event WHERE employee_id = $1`, [DEMO.sita]);
    expect(n.rows[0].n).toBe(1);
  });
  it('outside the geofence requires a reason, then is accepted and flagged', async () => {
    const body = { direction: 'out', clientTime: nowIso(), idempotencyKey: 'out-00001', lat: 27.80, lng: 85.40, source: 'mobile' };
    const noReason = await request(app).post('/api/v1/punch').set(auth(sita)).send(body);
    expect(noReason.status).toBe(400);
    expect(noReason.body.error).toMatch(/m from Head office/);
    const ok = await request(app).post('/api/v1/punch').set(auth(sita)).send({ ...body, reason: 'Client site visit' });
    expect(ok.status).toBe(201);
    expect(ok.body.flags).toContain('OutsideLocation');
    expect(ok.body.location.inside).toBe(false);
  });
  it('a wildly wrong client clock is replaced by server time and flagged', async () => {
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ direction: 'in', clientTime: '2020-01-01T00:00:00Z', idempotencyKey: 'skew-0001', lat: 27.7173, lng: 85.3241, source: 'web' });
    expect(r.status).toBe(201);
    expect(r.body.flags).toContain('ClockSkew');
    expect(r.body.punch.local_date).toBe(localDateOf(Date.now()));
  });
  it('GET /punch/today returns the day and raw punches; users without an employee record get a clear error', async () => {
    const t = await request(app).get('/api/v1/punch/today').set(auth(sita));
    expect(t.status).toBe(200);
    expect(t.body.punches.length).toBeGreaterThanOrEqual(3);
    expect(t.body.day).not.toBeNull();
    expect((await request(app).get('/api/v1/punch/today').set(auth(hr))).status).toBe(400);
  });
  it('HR manual punch is audited and recomputed; employees cannot use it', async () => {
    const body = { employeeId: DEMO.ram, localDate: '2026-09-21', time: '10:00', direction: 'in', reason: 'Device was down' };
    expect((await request(app).post('/api/v1/punch/manual').set(auth(sita)).send(body)).status).toBe(403);
    const r = await request(app).post('/api/v1/punch/manual').set(auth(hr)).send(body);
    expect(r.status).toBe(201);
    expect(r.body.day.status).toBe('Incomplete');
    const a = await db.query(`SELECT 1 FROM audit_log WHERE action = 'manual_punch' AND row_id = $1`, [r.body.punchId]);
    expect(a.rows).toHaveLength(1);
    expect((await request(app).post('/api/v1/punch/manual').set(auth(hr)).send(body)).body.duplicate).toBe(true);
  });
});
