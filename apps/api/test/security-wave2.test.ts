import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Express } from 'express';
import type { Db } from '../src/db/pool.js';
import { buildAdhocQuery } from '../src/modules/reports/adhoc.js';
import { DEMO, auth, freshDb, loginAs, testApp } from './setup-db.js';

describe('fixes from the wave-2 security review', () => {
  let db: Db; let app: Express;
  beforeAll(async () => { db = await freshDb(); app = testApp(db); });
  afterAll(async () => { await db.end(); });

  it('an attendance admin cannot add punches to their own attendance', async () => {
    const me = await loginAs(db, app, ['attendance_admin' as never], DEMO.ram);
    const body = { localDate: '2026-10-01', time: '09:00', direction: 'in', reason: 'forgot to punch' };
    expect((await request(app).post('/api/v1/punch/manual').set(auth(me.token)).send({ ...body, employeeId: DEMO.ram })).status).toBe(403);
    expect((await request(app).post('/api/v1/punch/manual').set(auth(me.token)).send({ ...body, employeeId: DEMO.sita })).status).toBeLessThan(300);
  });

  it('report builder names that are built-in object keys are refused, not crashed on', () => {
    expect(() => buildAdhocQuery({ dataset: 'constructor', columns: ['x'] } as never, DEMO.companyId, '2026-10-16', 10)).toThrow(/unknown dataset/);
    expect(() => buildAdhocQuery({ dataset: 'employees', columns: ['__proto__'] } as never, DEMO.companyId, '2026-10-16', 10)).toThrow(/unknown column/);
  });
});
