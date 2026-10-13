import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { recomputeDay } from '../src/modules/attendance/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string;
const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10);
/** nth most recent day before today that is not a Saturday (the demo weekly off). */
const workdayAgo = (n: number): string => {
  let count = 0;
  for (let i = 1; i < 60; i++) {
    const d = daysAgo(i);
    if (new Date(`${d}T00:00:00Z`).getUTCDay() === 6) continue;
    if (++count === n) return d;
  }
  throw new Error('unreachable');
};
const npt = (date: string, hhmm: string) => new Date(localTimeMs(date, hhmm)).toISOString();

async function insertPunch(employeeId: string, localDate: string, hhmm: string, pin = '1') {
  const at = new Date(localTimeMs(localDate, hhmm));
  await db.query(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
     VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', 'fingerprint', 'zkteco', $7)`,
    [DEMO.deviceId, pin, employeeId, at, `${localDate} ${hhmm}:00`, localDateOf(at.getTime()), `k-${pin}-${localDate}-${hhmm}`]);
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
});
afterAll(async () => { await db.end(); });

describe('regularisation', () => {
  const d = workdayAgo(3);

  it('missed OUT punch: employee files, manager approves, a punch is created and the day becomes Present', async () => {
    await insertPunch(DEMO.sita, d, '10:00');
    expect((await recomputeDay(db, DEMO.sita, d)).day.status).toBe('Incomplete');
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: d, kind: 'missed_punch', punchTime: npt(d, '18:00'), punchDirection: 'out', reason: 'forgot to punch out' });
    expect(r.status).toBe(201);
    expect(r.body.status).toBe('pending');
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(inbox.body.items[0]).toMatchObject({ objectType: 'regularisation_request', summary: { kind: 'missed_punch' } });
    const ok = await request(app).post(`/api/v1/workflow/${inbox.body.items[0].instanceId}/approve`).set(auth(ram)).send({});
    expect(ok.body.state).toBe('approved');
    const rr = await db.query<{ resulting_punch_id: string | null; status: string }>('SELECT resulting_punch_id, status FROM regularisation_request WHERE id = $1', [r.body.id]);
    expect(rr.rows[0]).toMatchObject({ status: 'approved' });
    expect(rr.rows[0]!.resulting_punch_id).not.toBeNull();
    const p = await db.query<{ source: string; direction: string; device_sn: string }>('SELECT source, direction, device_sn FROM punch_event WHERE id = $1', [rr.rows[0]!.resulting_punch_id]);
    expect(p.rows[0]).toMatchObject({ source: 'regularisation', direction: 'out', device_sn: 'MANUAL-DEMO' });
    const day = await recomputeDay(db, DEMO.sita, d);
    expect(day.day).toMatchObject({ status: 'Present', workedMin: 480 });
  });

  it('HR fixes a punch on behalf: approved immediately with a signed action', async () => {
    const d2 = workdayAgo(4);
    const r1 = await request(app).post('/api/v1/regularisation').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d2, kind: 'missed_punch', punchTime: npt(d2, '10:00'), punchDirection: 'in', reason: 'device offline' });
    expect(r1.status).toBe(201);
    expect(r1.body.status).toBe('approved');
    const r2 = await request(app).post('/api/v1/regularisation').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d2, kind: 'missed_punch', punchTime: npt(d2, '18:00'), punchDirection: 'out', reason: 'device offline' });
    expect(r2.body.status).toBe('approved');
    const acts = await db.query('SELECT a.decision FROM approval_action a JOIN workflow_instance w ON w.id = a.instance_id WHERE w.object_id = $1', [r1.body.id]);
    expect(acts.rows).toEqual([{ decision: 'approve' }]);
    expect((await recomputeDay(db, DEMO.ram, d2)).day).toMatchObject({ status: 'Present', workedMin: 480 });
  });

  it('WFH approval turns an absent day into WFH', async () => {
    const d3 = workdayAgo(5);
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: d3, kind: 'wfh', reason: 'worked from home' });
    expect(r.status).toBe(201);
    const inst = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM regularisation_request WHERE id = $1', [r.body.id]);
    await request(app).post(`/api/v1/workflow/${inst.rows[0]!.workflow_instance_id}/approve`).set(auth(hr)).send({});
    expect((await recomputeDay(db, DEMO.sita, d3)).day.status).toBe('WFH');
  });

  it('enforces the back-date window, future dates, locked days and the monthly cap', async () => {
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: daysAgo(45), kind: 'wfh', reason: 'old' })).status).toBe(400);
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: daysAgo(-2), kind: 'wfh', reason: 'future' })).status).toBe(400);
    const dl = workdayAgo(6);
    await recomputeDay(db, DEMO.sita, dl);
    await db.query('UPDATE attendance_day SET locked_at = now() WHERE employee_id = $1 AND local_date = $2', [DEMO.sita, dl]);
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: dl, kind: 'wfh', reason: 'locked' })).status).toBe(409);
    await db.query(`UPDATE company_setting SET value = value || '{"regularisationMonthlyCap": 1}' WHERE company_id = $1 AND key = 'attendance.policy'`, [DEMO.companyId]);
    const capped = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: workdayAgo(1), kind: 'on_duty', reason: 'site visit' });
    expect(capped.status).toBe(400);
    expect(capped.body.error).toMatch(/monthly limit/);
  });

  it('employee cannot file for someone else; cancel only while pending', async () => {
    expect((await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ employeeId: DEMO.ram, localDate: workdayAgo(2), kind: 'wfh', reason: 'not allowed' })).status).toBe(403);
    const r = await request(app).post('/api/v1/regularisation').set(auth(ram)).send({ localDate: workdayAgo(2), kind: 'on_duty', reason: 'client site' });
    expect(r.status).toBe(201);
    expect((await request(app).post(`/api/v1/regularisation/${r.body.id}/cancel`).set(auth(ram))).status).toBe(200);
    expect((await request(app).post(`/api/v1/regularisation/${r.body.id}/cancel`).set(auth(ram))).status).toBe(409);
  });
});
