import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { recomputeDay } from '../src/modules/attendance/service.js';
import { runMonthlyAccrual, expireSubstituteLeave } from '../src/modules/leave/accrual.js';
import { creditCompOff, ledgerBalance, leaveTypeByCode } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let ramUserId: string;
const typeId: Record<string, string> = {};
const t = (code: string): string => typeId[code]!;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  const m = await loginAs(db, app, ['manager'], DEMO.ram); ram = m.token; ramUserId = m.userId;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  for (const code of ['HOME', 'SICK', 'CASUAL', 'LWP', 'SUBSTITUTE']) typeId[code] = (await leaveTypeByCode(db, DEMO.companyId, code))!.id;
});
afterAll(async () => { await db.end(); });

describe('leave day computation (plan §10.6 example F)', () => {
  it('2083-05-12..14 with Saturday between: sandwich off → 2 days, on → 3 days', async () => {
    const q = { employeeId: DEMO.sita, leaveTypeId: t('HOME'), from: '2026-08-28', to: '2026-08-30' };
    const off = await request(app).get('/api/v1/leave/preview').set(auth(hr)).query(q);
    expect(off.status).toBe(200);
    expect(off.body).toMatchObject({ days: 2, sandwichApplied: false, excludedDays: ['2026-08-29'] });
    await db.query(`UPDATE company_setting SET value = value || '{"sandwichRule": true}' WHERE company_id = $1 AND key = 'leave.policy'`, [DEMO.companyId]);
    const on = await request(app).get('/api/v1/leave/preview').set(auth(hr)).query(q);
    expect(on.body).toMatchObject({ days: 3, sandwichApplied: true });
    await db.query(`UPDATE company_setting SET value = value || '{"sandwichRule": false}' WHERE company_id = $1 AND key = 'leave.policy'`, [DEMO.companyId]);
  });
  it('holidays are excluded, gender-aware', async () => {
    await db.query(`INSERT INTO holiday (company_id, local_date, name_en, gender_scope) VALUES ($1, '2026-09-14', 'Teej', 'female')`, [DEMO.companyId]);
    const female = await request(app).get('/api/v1/leave/preview').set(auth(hr)).query({ employeeId: DEMO.sita, leaveTypeId: t('HOME'), from: '2026-09-13', to: '2026-09-15' });
    const male = await request(app).get('/api/v1/leave/preview').set(auth(hr)).query({ employeeId: DEMO.ram, leaveTypeId: t('HOME'), from: '2026-09-13', to: '2026-09-15' });
    expect(female.body.days).toBe(2);
    expect(male.body.days).toBe(3);
  });
});

describe('leave requests and approvals', () => {
  let requestId = ''; let instanceId = '';

  it('insufficient balance returns a split suggestion', async () => {
    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-09-21', to: '2026-09-23', reason: 'family' });
    expect(r.status).toBe(400);
    expect(r.body.suggestion).toEqual({ paidDays: 0, lwpDays: 3 });
    expect(r.body.error).toMatch(/unpaid leave/);
  });

  it('employee requests, manager sees it in the inbox with balance-after and approves; day becomes OnLeave', async () => {
    const adj = await request(app).post('/api/v1/leave/adjust').set(auth(hr)).send({ employeeId: DEMO.sita, leaveTypeId: t('HOME'), deltaDays: 10, reason: 'opening balance', kind: 'allocation' });
    expect(adj.status).toBe(201);
    expect(adj.body.balance).toBe(10);
    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-09-21', to: '2026-09-22', reason: 'family' });
    expect(r.status).toBe(201);
    expect(r.body).toMatchObject({ days: 2, sandwichApplied: false });
    requestId = r.body.id;
    const bal = await request(app).get('/api/v1/leave/balances').set(auth(sita));
    const home = bal.body.balances.find((b: { code: string }) => b.code === 'HOME');
    expect(home).toMatchObject({ balance: 10, pending: 2, available: 8 });
    expect(home.explanation).toMatch(/Opening/);

    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(inbox.body.items).toHaveLength(1);
    const item = inbox.body.items[0];
    expect(item).toMatchObject({ objectType: 'leave_request', objectId: requestId, employee: { id: DEMO.sita } });
    expect(item.summary).toMatchObject({ leaveType: 'HOME', days: 2, balanceNow: 10, balanceAfter: 8 });
    instanceId = item.instanceId;
    // Manager got a notification about the pending request.
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE user_id = $1 AND template = 'leave_request.pending'`, [ramUserId]);
    expect(n.rows.length).toBeGreaterThan(0);

    const ok = await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(ram)).send({});
    expect(ok.status).toBe(200);
    expect(ok.body.state).toBe('approved');
    expect(await ledgerBalance(db, DEMO.sita, t('HOME'))).toBe(8);
    const day = await recomputeDay(db, DEMO.sita, '2026-09-21');
    expect(day.day.status).toBe('OnLeave');
    const act = await db.query<{ signature: string }>('SELECT signature FROM approval_action WHERE instance_id = $1', [instanceId]);
    expect(act.rows[0]!.signature).toHaveLength(64);
    // The approval recomputes the covered days before responding, so nothing is left for the worker.
    const queued = await db.query(`SELECT 1 FROM recompute_queue WHERE employee_id = $1 AND local_date = '2026-09-22'`, [DEMO.sita]);
    expect(queued.rows).toHaveLength(0);
    expect((await db.query(`SELECT status FROM v_attendance_latest WHERE employee_id = $1 AND local_date = '2026-09-22'`, [DEMO.sita])).rows[0].status).toBe('OnLeave');
  });

  it('nobody approves their own request; rejecting needs a reason; double decision is a conflict', async () => {
    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-09-24', to: '2026-09-24' });
    expect(r.status).toBe(201);
    const lr = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [r.body.id]);
    const inst = lr.rows[0]!.workflow_instance_id;
    expect((await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(sita)).send({})).status).toBe(403);
    expect((await request(app).post(`/api/v1/workflow/${inst}/reject`).set(auth(ram)).send({})).status).toBe(400);
    expect((await request(app).post(`/api/v1/workflow/${inst}/reject`).set(auth(ram)).send({ reason: 'coverage' })).body.state).toBe('rejected');
    expect((await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(ram)).send({})).status).toBe(409);
    expect(await ledgerBalance(db, DEMO.sita, t('HOME'))).toBe(8);
  });

  it('overlapping request is rejected; cancelling an approved request reverses the ledger', async () => {
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-09-22', to: '2026-09-23' })).status).toBe(409);
    const c = await request(app).post(`/api/v1/leave/requests/${requestId}/cancel`).set(auth(sita)).send({ reason: 'plans changed' });
    expect(c.status).toBe(200);
    expect(await ledgerBalance(db, DEMO.sita, t('HOME'))).toBe(10);
    expect((await recomputeDay(db, DEMO.sita, '2026-09-21')).day.status).toBe('Absent');
  });

  it('sick leave beyond 3 days needs a certificate; LWP is always allowed', async () => {
    const noCert = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('SICK'), from: '2026-10-04', to: '2026-10-08' });
    expect(noCert.status).toBe(400);
    expect(noCert.body.error).toMatch(/certificate/);
    const lwp = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('LWP'), from: '2026-10-04', to: '2026-10-08', reason: 'travel' });
    expect(lwp.status).toBe(201);
    expect(lwp.body.days).toBe(5); // Sun 4 Oct .. Thu 8 Oct 2026, no Saturday
  });

  it('team calendar lists who is off', async () => {
    const cal = await request(app).get('/api/v1/leave/team-calendar').set(auth(hr)).query({ from: '2026-10-04', to: '2026-10-08' });
    expect(cal.body.days.find((d: { date: string }) => d.date === '2026-10-05').off[0]).toMatchObject({ employeeId: DEMO.sita, type: 'LWP', status: 'pending' });
  });

  it('statutory entitlement fields are locked', async () => {
    const r = await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ maxBalance: 200 });
    expect(r.status).toBe(400);
    expect((await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ nameNe: 'घर बिदा' })).status).toBe(200);
  });
});

describe('accrual (plan §10.6 example E) and substitute leave expiry', () => {
  it('22 present days in Shrawan 2083 → 1.10 home, 1.0 sick, casual allocated; idempotent', async () => {
    const dates = ['2026-07-19', '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23', '2026-07-26', '2026-07-27', '2026-07-28', '2026-07-29', '2026-07-30',
      '2026-08-02', '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-09', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-16', '2026-07-17'];
    for (const d of dates) await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash) VALUES ($1, $2, 1, 'Present', 'h')`, [DEMO.ram, d]);
    for (const d of ['2026-08-14', '2026-08-15', '2026-07-18']) await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash) VALUES ($1, $2, 1, 'Absent', 'h')`, [DEMO.ram, d]);
    const first = await runMonthlyAccrual(db, DEMO.companyId, 2083, 4);
    expect(first.postings).toBeGreaterThanOrEqual(3);
    expect(await ledgerBalance(db, DEMO.ram, t('HOME'))).toBeCloseTo(1.1, 2);
    expect(await ledgerBalance(db, DEMO.ram, t('SICK'))).toBe(1);
    expect(await ledgerBalance(db, DEMO.ram, t('CASUAL'))).toBe(6);
    const again = await runMonthlyAccrual(db, DEMO.companyId, 2083, 4);
    expect(again.postings).toBe(0);
    expect(await ledgerBalance(db, DEMO.ram, t('HOME'))).toBeCloseTo(1.1, 2);
  });
  it('sick leave cap of 45 expires the excess', async () => {
    await db.query(`INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES ($1, $2, '2026-08-01', 44.5, 'adjustment')`, [DEMO.ram, t('SICK')]);
    await runMonthlyAccrual(db, DEMO.companyId, 2083, 5);
    expect(await ledgerBalance(db, DEMO.ram, t('SICK'))).toBe(45);
  });
  it('unused substitute leave lapses after its expiry date', async () => {
    await creditCompOff(db, DEMO.ram, 1, '2026-09-01', { refType: 'test', refId: DEMO.ram });
    expect(await ledgerBalance(db, DEMO.ram, t('SUBSTITUTE'))).toBe(1);
    expect(await expireSubstituteLeave(db, DEMO.companyId, '2026-10-01')).toBe(1);
    expect(await ledgerBalance(db, DEMO.ram, t('SUBSTITUTE'))).toBe(0);
    expect(await expireSubstituteLeave(db, DEMO.companyId, '2026-10-01')).toBe(0);
  });
});
