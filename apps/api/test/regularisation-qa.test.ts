import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import request from 'supertest';
import { localTimeMs } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let otherMgr: string;
const npt = (date: string, hhmm: string) => new Date(localTimeMs(date, hhmm)).toISOString();

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const m = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E901', 'Unrelated Manager') RETURNING id`, [DEMO.companyId]);
  otherMgr = (await loginAs(db, app, ['manager'], m.rows[0]!.id)).token;
});
afterAll(async () => { vi.useRealTimers(); await db.end(); });

describe('regularisation guards', () => {
  it('"today" is the Nepal calendar date, not the UTC date', async () => {
    // 19:00 UTC on 13 Oct is 00:45 NPT on 14 Oct: the Nepal date must already read 14 Oct.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-13T19:00:00Z'));
    expect(nptToday()).toBe('2026-10-14');
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-10-13');
    vi.useRealTimers();
    // The route must accept the Nepal "today" as not-in-the-future at any hour of the day.
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: nptToday(), kind: 'on_duty', reason: 'today in Nepal' });
    expect(r.status).toBe(201);
  });

  it('a missed punch cannot be placed before the attendance date, but may fall on the next day (night shift)', async () => {
    const d = '2026-10-05';
    expect((await request(app).post('/api/v1/regularisation').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d, kind: 'missed_punch', punchTime: npt('2026-10-04', '23:00'), punchDirection: 'in', reason: 'x' })).status).toBe(400);
    expect((await request(app).post('/api/v1/regularisation').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d, kind: 'missed_punch', punchTime: npt('2026-10-06', '06:00'), punchDirection: 'out', reason: 'night shift out' })).status).toBe(201);
    expect((await request(app).post('/api/v1/regularisation').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d, kind: 'missed_punch', punchTime: npt('2026-10-07', '06:00'), punchDirection: 'out', reason: 'too far' })).status).toBe(400);
  });

  it('shift change must reference a shift of the same company', async () => {
    const c2 = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('C2', 'C2') RETURNING id`);
    const sh = await db.query<{ id: string }>(`INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES ($1, 'X', 'X', '09:00', '17:00') RETURNING id`, [c2.rows[0]!.id]);
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: '2026-10-06', kind: 'shift_change', shiftId: sh.rows[0]!.id, reason: 'swap' })).status).toBe(404);
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: '2026-10-06', kind: 'shift_change', shiftId: DEMO.shiftId, reason: 'swap' })).status).toBe(201);
  });

  it('a manager can only file for, list and cancel requests of direct reports', async () => {
    expect((await request(app).post('/api/v1/regularisation').set(auth(otherMgr)).send({ employeeId: DEMO.sita, localDate: '2026-10-07', kind: 'wfh', reason: 'not my report' })).status).toBe(403);
    const ok = await request(app).post('/api/v1/regularisation').set(auth(ram)).send({ employeeId: DEMO.sita, localDate: '2026-10-07', kind: 'wfh', reason: 'yes' });
    expect(ok.status).toBe(201);
    expect((await request(app).post(`/api/v1/regularisation/${ok.body.id}/cancel`).set(auth(otherMgr))).status).toBe(403);
    expect((await request(app).get('/api/v1/regularisation').set(auth(otherMgr)).query({ employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).post(`/api/v1/regularisation/${ok.body.id}/cancel`).set(auth(ram))).status).toBe(200);
  });

  it("the team's today list shows a manager only their direct reports and themselves; HR sees everyone", async () => {
    const day = '2026-10-07';
    const ids = (r: request.Response) => (r.body.rows as Array<{ employee_id: string }>).map((x) => x.employee_id).sort();
    const mine = await request(app).get('/api/v1/attendance/today').set(auth(ram)).query({ date: day });
    expect(mine.status).toBe(200);
    expect(ids(mine)).toEqual([DEMO.sita, DEMO.ram].sort());
    const other = await request(app).get('/api/v1/attendance/today').set(auth(otherMgr)).query({ date: day });
    expect(other.body.rows).toHaveLength(1);
    const all = await request(app).get('/api/v1/attendance/today').set(auth(hr)).query({ date: day });
    expect(all.body.rows.length).toBeGreaterThanOrEqual(3);
  });

  it('approving the same missed-punch request twice never creates two punches', async () => {
    const d = '2026-10-08';
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: d, kind: 'missed_punch', punchTime: npt(d, '18:00'), punchDirection: 'out', reason: 'forgot' });
    const inst = (await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM regularisation_request WHERE id = $1', [r.body.id])).rows[0]!.workflow_instance_id;
    const results = await Promise.all([0, 1, 2].map(() => request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(ram)).send({})));
    expect(results.map((x) => x.status).sort()).toEqual([200, 409, 409]);
    const punches = await db.query(`SELECT 1 FROM punch_event WHERE employee_id = $1 AND local_date = $2 AND source = 'regularisation'`, [DEMO.sita, d]);
    expect(punches.rows).toHaveLength(1);
  });
});
