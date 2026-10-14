import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { leaveTypeByCode, ledgerBalance } from '../src/modules/leave/service.js';
import { runMonthlyAccrual } from '../src/modules/leave/accrual.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string;
const typeIds: Record<string, string> = {};
const t = { get HOME() { return typeIds.HOME!; }, get SICK() { return typeIds.SICK!; }, get LWP() { return typeIds.LWP!; } };

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  for (const c of ['HOME', 'SICK', 'LWP']) typeIds[c] = (await leaveTypeByCode(db, DEMO.companyId, c))!.id;
  await db.query(`INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES ($1, $2, '2026-07-17', 20, 'allocation')`, [DEMO.sita, t.HOME]);
});
afterAll(async () => { await db.end(); });

describe('leave guards', () => {
  it('half-day must be a single date; from after to is rejected', async () => {
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.HOME, from: '2026-12-01', to: '2026-12-02', halfDay: 'first_half' })).status).toBe(400);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.HOME, from: '2026-12-02', to: '2026-12-01' })).status).toBe(400);
  });

  it('leave cannot start before joining or end after exit', async () => {
    await db.query(`UPDATE employee SET join_date = '2026-06-01' WHERE id = $1`, [DEMO.sita]);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.LWP, from: '2026-05-31', to: '2026-06-01' })).status).toBe(400);
    await db.query(`UPDATE employee SET exit_date = '2026-12-15' WHERE id = $1`, [DEMO.sita]);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.LWP, from: '2026-12-14', to: '2026-12-16' })).status).toBe(400);
    await db.query(`UPDATE employee SET exit_date = NULL WHERE id = $1`, [DEMO.sita]);
  });

  it('a payroll-locked day blocks new requests, approval, and cancellation', async () => {
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at) VALUES ($1, '2026-12-07', 1, 'Present', 'h', now())`, [DEMO.sita]);
    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.HOME, from: '2026-12-06', to: '2026-12-08' });
    expect(r.status).toBe(409);
    // A request filed before the lock, then the day gets locked: approval must refuse and leave the ledger untouched.
    const r2 = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.HOME, from: '2026-12-09', to: '2026-12-09' });
    expect(r2.status).toBe(201);
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at) VALUES ($1, '2026-12-09', 1, 'Present', 'h', now())`, [DEMO.sita]);
    const inst = (await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [r2.body.id])).rows[0]!.workflow_instance_id;
    const ap = await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(ram)).send({});
    expect(ap.status).toBe(409);
    expect(await ledgerBalance(db, DEMO.sita, t.HOME)).toBe(20);
    expect((await db.query('SELECT state FROM workflow_instance WHERE id = $1', [inst])).rows[0]).toEqual({ state: 'pending' });
    const rj = await request(app).post(`/api/v1/workflow/${inst}/reject`).set(auth(ram)).send({ reason: 'period closed' });
    expect(rj.body.state).toBe('rejected');
  });

  it('concurrent identical requests: exactly one is accepted', async () => {
    const body = { leaveTypeId: t.LWP, from: '2026-12-20', to: '2026-12-21', reason: 'race' };
    const results = await Promise.all(Array.from({ length: 5 }, () => request(app).post('/api/v1/leave/requests').set(auth(sita)).send(body)));
    const codes = results.map((r) => r.status).sort();
    expect(codes.filter((c) => c === 201)).toHaveLength(1);
    expect(codes.filter((c) => c === 409)).toHaveLength(4);
  });

  it('every usage has a matching reversal on cancel and balances are pure sums', async () => {
    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t.HOME, from: '2026-12-22', to: '2026-12-23' });
    expect(r.status).toBe(201);
    const inst = (await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [r.body.id])).rows[0]!.workflow_instance_id;
    await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(ram)).send({});
    expect(await ledgerBalance(db, DEMO.sita, t.HOME)).toBe(18);
    expect((await request(app).post(`/api/v1/leave/requests/${r.body.id}/cancel`).set(auth(sita)).send({})).status).toBe(200);
    const rows = await db.query<{ reason: string; delta_days: string }>(`SELECT reason, delta_days FROM leave_ledger WHERE ref_id = $1 ORDER BY posted_at`, [r.body.id]);
    expect(rows.rows).toEqual([{ reason: 'usage', delta_days: '-2.00' }, { reason: 'cancellation', delta_days: '2.00' }]);
    expect(await ledgerBalance(db, DEMO.sita, t.HOME)).toBe(20);
    expect((await request(app).post(`/api/v1/leave/requests/${r.body.id}/cancel`).set(auth(sita)).send({})).status).toBe(409);
  });

  it('weekly-off pattern other than Saturday is honoured', async () => {
    await db.query(`UPDATE employee SET weekly_off_days = '{0}' WHERE id = $1`, [DEMO.ram]);
    // HOME has no sandwich rule (LWP does by default), so the Sunday in the middle is simply excluded.
    const p = await request(app).get('/api/v1/leave/preview').set(auth(hr)).query({ employeeId: DEMO.ram, leaveTypeId: t.HOME, from: '2026-12-05', to: '2026-12-07' });
    expect(p.body, JSON.stringify(p.body)).toMatchObject({ days: 2, excludedDays: ['2026-12-06'], sandwichApplied: false });
    await db.query(`UPDATE employee SET weekly_off_days = '{6}' WHERE id = $1`, [DEMO.ram]);
  });

  it('accrual runs twice without double posting and skips employees who joined after the month', async () => {
    await db.query(`UPDATE employee SET join_date = '2026-09-01' WHERE id = $1`, [DEMO.ram]);
    // Earlier requests may already have granted Ram this year's casual allocation; the month run must add nothing for him.
    const ramRows = async () => (await db.query('SELECT 1 FROM leave_ledger WHERE employee_id = $1 AND reason IN ($2, $3)', [DEMO.ram, 'accrual', 'allocation'])).rows.length;
    const before = await ramRows();
    const a = await runMonthlyAccrual(db, DEMO.companyId, 2083, 4); // Shrawan 2083 ends 16 Aug 2026, before Ram joined
    expect(await ramRows()).toBe(before);
    const b = await runMonthlyAccrual(db, DEMO.companyId, 2083, 4);
    expect(b.postings).toBe(0);
    expect(a.bsMonth).toBe(4);
    await db.query(`UPDATE employee SET join_date = NULL WHERE id = $1`, [DEMO.ram]);
  });
});
