import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import pino from 'pino';
import { localDateOf, localTimeMs } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { enqueueRecompute, recomputeDay } from '../src/modules/attendance/service.js';
import { drainRecomputeQueue } from '../src/worker.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher } from './setup-db.js';

/** Regression tests for the attendance fixes found in the fake-data end-to-end run. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string;
let seq = 0;

async function punch(employeeId: string | null, date: string, hhmm: string, pin = '1', dayOffset = 0) {
  const at = new Date(localTimeMs(date, hhmm, dayOffset));
  const r = await db.query<{ id: string }>(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
     VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', 'fingerprint', 'zkteco', $7) RETURNING id`,
    [DEMO.deviceId, pin, employeeId, at, `${date} ${hhmm}:00`, localDateOf(at.getTime()), `fix-${++seq}`]);
  return r.rows[0]!.id;
}
const days = (employeeId: string, from: string, to = from) => request(app).get('/api/v1/attendance/days').set(auth(hr)).query({ employeeId, from, to });

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
});
afterAll(async () => { await db.end(); });

describe('overtime days', () => {
  it('a punch-out 2.5 h after the shift end pairs: Present with overtime, not Incomplete', async () => {
    await punch(DEMO.sita, '2026-09-01', '10:00');
    await punch(DEMO.sita, '2026-09-01', '20:30');
    const r = await recomputeDay(db, DEMO.sita, '2026-09-01');
    expect(r.day.status).toBe('Present');
    expect(r.day.workedMin).toBe(630);
    expect(r.day.ot.rawMinutes).toBe(150);
  });
  it('new shifts default to a 6 h late-out window', async () => {
    const s = await request(app).post('/api/v1/shifts').set(auth(hr)).send({ code: 'LATE6', nameEn: 'Ten to six', start: '10:00', end: '18:00' });
    expect(s.status).toBe(201);
    expect((await db.query('SELECT late_out_window_min FROM shift WHERE id = $1', [s.body.id])).rows[0].late_out_window_min).toBe(360);
  });
});

describe('device PIN re-mapping', () => {
  it('refuses to take over a mapped PIN without confirmation; replacing moves punches since `from` both ways and recomputes both people', async () => {
    // PIN 1 is Sita's (demo seed). Her punches on 3 Sep and 10 Sep.
    for (const d of ['2026-09-03', '2026-09-10']) { await punch(DEMO.sita, d, '10:00'); await punch(DEMO.sita, d, '18:00'); await recomputeDay(db, DEMO.sita, d); }
    const map = (body: Record<string, unknown>) => request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '1', ...body });
    const refused = await map({ employeeId: DEMO.ram, from: '2026-09-05' });
    expect(refused.status).toBe(409);
    expect(refused.body.error).toMatch(/already mapped to Sita/);
    expect((await days(DEMO.sita, '2026-09-10')).body.days[0].status).toBe('Present');

    const moved = await map({ employeeId: DEMO.ram, from: '2026-09-05', replace: true });
    expect(moved.status).toBe(201);
    expect(moved.body).toMatchObject({ dates: ['2026-09-10'], movedFrom: [DEMO.sita] });
    expect((await days(DEMO.sita, '2026-09-03')).body.days[0].status).toBe('Present');   // before `from`: untouched
    expect((await days(DEMO.sita, '2026-09-10')).body.days[0].status).toBe('Absent');
    expect((await days(DEMO.ram, '2026-09-10')).body.days[0].status).toBe('Present');

    const back = await map({ employeeId: DEMO.sita, from: '2026-09-05', replace: true });
    expect(back.status).toBe(201);
    expect(back.body.reattributed).toBe(1);
    expect((await days(DEMO.sita, '2026-09-10')).body.days[0].status).toBe('Present');
    expect((await days(DEMO.ram, '2026-09-10')).body.days[0].status).toBe('Absent');
    const current = await db.query(`SELECT employee_id FROM punch_event p WHERE device_user_pin = '1' AND local_date = '2026-09-10' AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)`);
    expect(current.rows.map((r) => r.employee_id)).toEqual([DEMO.sita, DEMO.sita]);
  });
  it('mapping the same employee again is not a conflict', async () => {
    expect((await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '1', employeeId: DEMO.sita, from: '2026-09-05' })).status).toBe(201);
  });
});

describe('locked payroll periods', () => {
  it('a period locked before any attendance row existed refuses manual punches and correction approvals', async () => {
    // Bhadra 2083 is 17 Aug .. 16 Sep 2026; lock it with no row for Ram on 20 Aug.
    const p = await request(app).post('/api/v1/payroll/periods').set(auth(hr)).send({ bsYear: 2083, bsMonth: 5 });
    await db.query('UPDATE payroll_period SET attendance_locked_at = now() WHERE id = $1', [p.body.period.id]);
    const manual = await request(app).post('/api/v1/punch/manual').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: '2026-08-20', time: '10:00', direction: 'in', reason: 'device was down' });
    expect(manual.status).toBe(409);
    expect((await db.query(`SELECT count(*)::int AS n FROM punch_event WHERE employee_id = $1 AND local_date = '2026-08-20'`, [DEMO.ram])).rows[0].n).toBe(0);
    // A recompute does not create a new version inside a locked period either.
    const rc = await recomputeDay(db, DEMO.ram, '2026-08-21');
    expect(rc.locked).toBe(true);
    expect((await db.query(`SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = $1 AND local_date = '2026-08-21'`, [DEMO.ram])).rows[0].n).toBe(0);
    // A correction filed before the lock cannot be approved after it.
    const rr = await db.query<{ id: string }>(`INSERT INTO regularisation_request (employee_id, local_date, kind, reason, created_by) VALUES ($1, '2026-08-24', 'wfh', 'home', NULL) RETURNING id`, [DEMO.sita]);
    const { applyRegularisationDecision } = await import('../src/modules/regularisation/routes.js');
    await expect(applyRegularisationDecision(db, rr.rows[0]!.id, 'approved', { actorId: null as unknown as string, reason: null, payload: {} })).rejects.toThrow(/locked/);
  });
});

describe('approvals show up immediately', () => {
  it('a manager approving a missed punch recomputes the day before responding (no worker needed)', async () => {
    // A recent weekday (Saturday is the demo weekly off), inside the correction backdate window.
    let d = localDateOf(Date.now() - 3 * 86_400_000);
    if (new Date(`${d}T00:00:00Z`).getUTCDay() === 6) d = localDateOf(Date.now() - 4 * 86_400_000);
    await punch(DEMO.sita, d, '10:00');
    await recomputeDay(db, DEMO.sita, d);
    expect((await days(DEMO.sita, d)).body.days[0].status).toBe('Incomplete');
    const at = new Date(localTimeMs(d, '18:00')).toISOString();
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: d, kind: 'missed_punch', punchTime: at, punchDirection: 'out', reason: 'forgot to punch out' });
    expect(r.status).toBe(201);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === r.body.id);
    expect((await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(ram)).send({})).body.state).toBe('approved');
    expect((await days(DEMO.sita, d)).body.days[0]).toMatchObject({ status: 'Present', worked_min: 480 });
    expect((await db.query(`SELECT count(*)::int AS n FROM recompute_queue WHERE employee_id = $1`, [DEMO.sita])).rows[0].n).toBe(0);
  });
  it('recompute-range runs inline for small ranges and queues large ones', async () => {
    const small = await request(app).post('/api/v1/attendance/recompute-range').set(auth(hr)).send({ employeeIds: [DEMO.sita], from: '2026-09-21', to: '2026-09-22' });
    expect(small.body).toEqual({ queued: 0, recomputed: 2 });
    const big = await request(app).post('/api/v1/attendance/recompute-range').set(auth(hr)).send({ employeeIds: [DEMO.sita, DEMO.ram], from: '2026-09-01', to: '2026-10-02' });
    expect(big.body).toEqual({ queued: 64, recomputed: 0 });
  });
});

describe('worker queue', () => {
  it('drains a large backlog in one tick instead of 50 rows', async () => {
    await db.query('DELETE FROM recompute_queue');
    const items = [];
    for (let i = 0; i < 300; i++) items.push({ employeeId: i % 2 ? DEMO.sita : DEMO.ram, localDate: new Date(Date.UTC(2025, 0, 1) + i * 86_400_000).toISOString().slice(0, 10) });
    await enqueueRecompute(db, items, 'test');
    // The point is batching past the old 50-row cap, not speed: give a loaded test machine room to finish.
    const r = await drainRecomputeQueue({ db, logger: pino({ level: 'silent' }), cipher: testCipher }, { budgetMs: 60_000 });
    expect(r.processed).toBe(300);
    expect((await db.query('SELECT count(*)::int AS n FROM recompute_queue')).rows[0].n).toBe(0);
  });
});

describe('policy preview', () => {
  it('matches a real recompute under the current policy (nothing affected) and is fast for 40 people × 31 days', async () => {
    const ids: string[] = [];
    for (let i = 0; i < 38; i++) {
      const e = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, join_date) VALUES ($1, $2, $3, '2026-01-01') RETURNING id`, [DEMO.companyId, `P${i}`, `Preview ${i}`]);
      await db.query(`INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange('2026-01-01', NULL))`, [e.rows[0]!.id, DEMO.shiftId]);
      ids.push(e.rows[0]!.id);
    }
    for (const id of ids.slice(0, 10)) {
      for (const d of ['2026-10-05', '2026-10-06', '2026-10-07']) { await punch(id, d, i(id) ? '10:20' : '09:55'); await punch(id, d, '19:10'); }
    }
    function i(id: string) { return ids.indexOf(id) % 2; }
    const policy = (await db.query(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'attendance.policy'`, [DEMO.companyId])).rows[0].value;
    for (const id of ids) for (let d = 1; d <= 31; d++) await recomputeDay(db, id, `2026-10-${String(d).padStart(2, '0')}`).catch(() => undefined);
    const t = Date.now();
    const r = await request(app).post('/api/v1/settings/preview/attendance').set(auth(hr)).send({ policy, from: '2026-10-01', to: '2026-10-31', employeeIds: ids });
    const ms = Date.now() - t;
    expect(r.body.error ?? r.body.issues ?? null).toBeNull();
    expect(r.status).toBe(200);
    expect(r.body.affectedEmployees).toBe(0);
    expect(r.body.proposed).toEqual(r.body.current);
    expect(ms).toBeLessThan(2000);
  }, 60_000);
});
