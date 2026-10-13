import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let emp: string;
beforeAll(async () => { db = await freshDb(); app = testApp(db); hr = (await loginAs(db, app, ['hr'])).token; emp = (await loginAs(db, app, ['employee'], DEMO.sita)).token; });
afterAll(async () => { await db.end(); });

describe('settings', () => {
  it('lists all keys with descriptions and locked statutory caps', async () => {
    const r = await request(app).get('/api/v1/settings').set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.settings.map((s: { key: string }) => s.key)).toContain('attendance.policy');
    expect(r.body.locked).toMatchObject({ otCapDayMin: 240, otCapWeekMin: 1440 });
    expect((await request(app).get('/api/v1/settings').set(auth(emp))).status).toBe(403);
  });
  it('clamps OT caps to the statutory maximum, bumps the policy version and audits', async () => {
    const r = await request(app).put('/api/v1/settings/attendance.policy').set(auth(hr)).send({ otMinThresholdMin: 20, otCapDayMin: 600, otCapWeekMin: 9999 });
    expect(r.status).toBe(200);
    expect(r.body.value).toMatchObject({ otMinThresholdMin: 20, otCapDayMin: 240, otCapWeekMin: 1440 });
    expect(r.body.value.version).toMatch(/^v-[0-9a-f]{12}$/);
    const r2 = await request(app).put('/api/v1/settings/attendance.policy').set(auth(hr)).send({ otMinThresholdMin: 25 });
    expect(r2.body.value.version).not.toBe(r.body.value.version);
    const a = await db.query(`SELECT before, after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'attendance.policy' ORDER BY id DESC LIMIT 1`);
    expect(a.rows[0].before.otMinThresholdMin).toBe(20);
    expect(a.rows[0].after.otMinThresholdMin).toBe(25);
  });
  it('rejects unknown keys and invalid values', async () => {
    expect((await request(app).put('/api/v1/settings/nope').set(auth(hr)).send({})).status).toBe(404);
    expect((await request(app).put('/api/v1/settings/notifications.policy').set(auth(hr)).send({ quietHoursStart: '25:00' })).status).toBe(400);
  });
  it('company profile and header endpoint', async () => {
    await request(app).put('/api/v1/settings/company.profile').set(auth(hr)).send({ nameEn: 'Demo Pvt Ltd', nameNe: 'डेमो', pan: '123456789' });
    const r = await request(app).get('/api/v1/settings/company').set(auth(emp));
    expect(r.status).toBe(200);
    expect(r.body.company.nameEn).toBe('Demo Pvt Ltd');
    expect(r.body.today.bs).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
  it('branches and org units CRUD', async () => {
    const b = await request(app).post('/api/v1/settings/branches').set(auth(hr)).send({ code: 'PKR', nameEn: 'Pokhara' });
    expect(b.status).toBe(201);
    expect((await request(app).post('/api/v1/settings/branches').set(auth(hr)).send({ code: 'PKR', nameEn: 'Dup' })).status).toBe(409);
    const u = await request(app).post('/api/v1/settings/org-units').set(auth(hr)).send({ kind: 'department', code: 'FIN', nameEn: 'Finance', branchId: b.body.branch.id });
    expect(u.status).toBe(201);
    const p = await request(app).patch(`/api/v1/settings/org-units/${u.body.orgUnit.id}`).set(auth(hr)).send({ nameNe: 'वित्त' });
    expect(p.body.orgUnit.name_ne).toBe('वित्त');
    expect((await request(app).get('/api/v1/settings/branches').set(auth(hr))).body.branches).toHaveLength(2);
  });
  it('preview reports proposed vs current counts without writing', async () => {
    const before = await db.query('SELECT count(*)::int AS n FROM attendance_day');
    const r = await request(app).post('/api/v1/settings/preview/attendance').set(auth(hr)).send({ policy: { otMinThresholdMin: 0 }, from: '2026-09-21', to: '2026-09-22' });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ range: { days: 2 }, employees: 2 });
    expect(r.body.summary).toMatch(/late marks/);
    expect((await db.query('SELECT count(*)::int AS n FROM attendance_day')).rows[0].n).toBe(before.rows[0].n);
    expect((await request(app).post('/api/v1/settings/preview/attendance').set(auth(hr)).send({ policy: {}, from: '2026-01-01', to: '2026-12-31' })).status).toBe(400);
  });
});
