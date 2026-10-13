import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let emp: string;
beforeAll(async () => { db = await freshDb(); app = testApp(db); hr = (await loginAs(db, app, ['hr'])).token; emp = (await loginAs(db, app, ['employee'], DEMO.sita)).token; });
afterAll(async () => { await db.end(); });

describe('dashboard', () => {
  it('executive dashboard has at most 6 tiles, a trend and a needs-attention list', async () => {
    await db.query(`UPDATE device SET last_seen_at = now() - interval '10 minutes' WHERE id = $1`, [DEMO.deviceId]);
    const r = await request(app).get('/api/v1/dashboard/executive').set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.tiles.length).toBeLessThanOrEqual(6);
    expect(r.body.tiles.find((t: { key: string }) => t.key === 'headcount').value).toBe(2);
    expect(Array.isArray(r.body.trend)).toBe(true);
    expect(r.body.needsAttention.some((n: { kind: string }) => n.kind === 'device_offline')).toBe(true);
    expect(r.body.lastPayrollRun).toBeNull();
    expect((await request(app).get('/api/v1/dashboard/executive').set(auth(emp))).status).toBe(403);
  });
  it('employee home shows today, month summary, balances and pending requests', async () => {
    const r = await request(app).get('/api/v1/dashboard/me').set(auth(emp));
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ today: null, unreadNotifications: 0, pendingRequests: [] });
    expect(r.body.leaveBalances.map((b: { code: string }) => b.code)).toContain('HOME');
    expect(r.body.month.bs).toMatch(/^\d{4}-\d{2}$/);
    expect((await request(app).get('/api/v1/dashboard/me').set(auth(hr))).status).toBe(400);
  });
});
