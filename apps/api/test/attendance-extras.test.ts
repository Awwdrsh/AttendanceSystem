import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localTimeMs } from '@hajiri/attendance-engine';
import { bsMonthBounds } from '@hajiri/bs-calendar';
import type { Db } from '../src/db/pool.js';
import { recomputeDay } from '../src/modules/attendance/service.js';
import { assertClockInAllowed } from '../src/modules/attendance-tools/block.js';
import { allocatePenalty, penaltyFor, penaltyUnpaidDays } from '../src/modules/attendance-tools/penalties.js';
import { ledgerBalance, leaveTypeByCode } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let ramUserId: string; let sitaUserId: string; let outsider: string;
const typeId: Record<string, string> = {};
const t = (code: string): string => typeId[code]!;
const OTHER_EMP = '00000000-0000-7000-8000-0000000000e3';

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  const s = await loginAs(db, app, ['employee'], DEMO.sita); sita = s.token; sitaUserId = s.userId;
  const m = await loginAs(db, app, ['manager'], DEMO.ram); ram = m.token; ramUserId = m.userId;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`INSERT INTO employee (id, company_id, emp_code, name_en, gender) VALUES ($1, $2, 'E003', 'Hari Karki', 'male')`, [OTHER_EMP, DEMO.companyId]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2026-01-01', NULL), $2, 'regular')`, [OTHER_EMP, DEMO.branchId]);
  outsider = (await loginAs(db, app, ['employee'], OTHER_EMP)).token;
  for (const code of ['HOME', 'SICK', 'CASUAL', 'LWP']) typeId[code] = (await leaveTypeByCode(db, DEMO.companyId, code))!.id;
});
afterAll(async () => { await db.end(); });

const adjust = (employeeId: string, code: string, days: number) =>
  request(app).post('/api/v1/leave/adjust').set(auth(hr)).send({ employeeId, leaveTypeId: t(code), deltaDays: days, reason: 'opening balance', kind: 'allocation', effectiveDate: '2026-07-20' });
const approve = async (requestId: string, token = ram) => {
  const inst = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [requestId]);
  const r = await request(app).post(`/api/v1/workflow/${inst.rows[0]!.workflow_instance_id}/approve`).set(auth(token)).send({});
  expect(r.status).toBe(200);
};

describe('ordered and clubbed leave types (sandwich across requests)', () => {
  it('without the sandwich rule, the Saturday between two leaves is not charged', async () => {
    await adjust(DEMO.sita, 'HOME', 10); await adjust(DEMO.sita, 'SICK', 10);
    const a = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('SICK'), from: '2026-08-28', to: '2026-08-28', reason: 'fever' });
    expect(a.status).toBe(201);
    const p = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: t('HOME'), from: '2026-08-30', to: '2026-08-30' });
    expect(p.body.bridge).toBeNull();
    await request(app).post(`/api/v1/leave/requests/${a.body.id}/cancel`).set(auth(sita)).send({});
  });

  it('the type first in the order absorbs the gap when its sandwich rule is on, and it is posted on approval', async () => {
    await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ sandwichRule: true });
    const a = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('SICK'), from: '2026-08-28', to: '2026-08-28', reason: 'fever' });
    expect(a.status).toBe(201);
    await approve(a.body.id);
    // HOME (order 0) comes before SICK (order 1): the Saturday 2026-08-29 is charged to HOME.
    const b = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-08-30', to: '2026-08-30', reason: 'trip' });
    expect(b.status).toBe(201);
    expect(b.body.bridge).toMatchObject({ days: 1, typeId: t('HOME'), from: '2026-08-29', to: '2026-08-29' });
    const bal = await request(app).get('/api/v1/leave/balances').set(auth(sita));
    expect(bal.body.balances.find((x: { code: string }) => x.code === 'HOME')).toMatchObject({ pending: 2 });
    const before = await ledgerBalance(db, DEMO.sita, t('HOME'));
    await approve(b.body.id);
    expect(await ledgerBalance(db, DEMO.sita, t('HOME'))).toBe(before - 2);
    // A second request touching the same gap does not charge it again.
    const again = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: t('HOME'), from: '2026-08-27', to: '2026-08-27' });
    expect(again.body.bridge).toBeNull();
    // Cancelling gives the bridged day back.
    await request(app).post(`/api/v1/leave/requests/${b.body.id}/cancel`).set(auth(sita)).send({});
    expect(await ledgerBalance(db, DEMO.sita, t('HOME'))).toBe(before);
  });

  it('when the earlier type has the sandwich rule and comes first, the gap is charged to it', async () => {
    // SICK request on Sunday after an approved SICK on Friday? Use CASUAL (order 6) after HOME (order 0): HOME owns the gap.
    await adjust(DEMO.sita, 'CASUAL', 6);
    const h = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-09-04', to: '2026-09-04', reason: 'trip' });
    expect(h.status).toBe(201);
    const p = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: t('CASUAL'), from: '2026-09-06', to: '2026-09-06' });
    expect(p.body.bridge).toMatchObject({ days: 1, typeId: t('HOME') });
    await request(app).post(`/api/v1/leave/requests/${h.body.id}/cancel`).set(auth(sita)).send({});
  });

  it('a clubbed type follows its main type: CASUAL clubbed with HOME takes HOME\'s order and sandwich rule', async () => {
    await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ sandwichRule: false });
    await request(app).patch(`/api/v1/leave/types/${t('CASUAL')}`).set(auth(hr)).send({ sandwichRule: true });
    const s = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('SICK'), from: '2026-09-11', to: '2026-09-11', reason: 'cold' });
    expect(s.status).toBe(201);
    // Unclubbed: CASUAL (order 6) after SICK (order 1) — SICK owns the gap but has no sandwich rule.
    const p1 = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: t('CASUAL'), from: '2026-09-13', to: '2026-09-13' });
    expect(p1.body.bridge).toBeNull();
    // Clubbed with HOME (order 0) and HOME's sandwich rule on: CASUAL now comes first and absorbs the gap.
    const club = await request(app).patch(`/api/v1/leave/types/${t('CASUAL')}`).set(auth(hr)).send({ clubWithId: t('HOME') });
    expect(club.status).toBe(200);
    await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ sandwichRule: true });
    const p2 = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: t('CASUAL'), from: '2026-09-13', to: '2026-09-13' });
    expect(p2.body.bridge).toMatchObject({ days: 1, typeId: t('CASUAL') });
    await request(app).post(`/api/v1/leave/requests/${s.body.id}/cancel`).set(auth(sita)).send({});
  });

  it('rejects clubbing chains, self-clubbing and other companies\' types; order can be set in one call; employees cannot', async () => {
    expect((await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ clubWithId: t('HOME') })).status).toBe(400);
    expect((await request(app).patch(`/api/v1/leave/types/${t('SICK')}`).set(auth(hr)).send({ clubWithId: t('CASUAL') })).status).toBe(400);
    expect((await request(app).patch(`/api/v1/leave/types/${t('HOME')}`).set(auth(hr)).send({ clubWithId: t('SICK') })).status).toBe(400);
    const ord = await request(app).post('/api/v1/leave/types/order').set(auth(hr)).send({ ids: [t('SICK'), t('HOME')] });
    expect(ord.status).toBe(200);
    const types = await request(app).get('/api/v1/leave/types').set(auth(hr));
    const so = (code: string) => types.body.types.find((x: { code: string }) => x.code === code).sort_order;
    expect(so('SICK')).toBeLessThan(so('HOME'));
    expect((await request(app).post('/api/v1/leave/types/order').set(auth(sita)).send({ ids: [t('HOME')] })).status).toBe(403);
    expect((await request(app).post('/api/v1/leave/types/order').set(auth(hr)).send({ ids: ['00000000-0000-7000-8000-00000000dead'] })).status).toBe(400);
    await request(app).patch(`/api/v1/leave/types/${t('CASUAL')}`).set(auth(hr)).send({ clubWithId: null });
  });
});

describe('late-arrival penalty', () => {
  const Y = 2083; const M = 5;
  let lateDates: string[] = [];

  beforeAll(async () => {
    const { startAd } = bsMonthBounds(Y, M);
    // Four weekdays with Ram arriving at 10:40 (grace ends 10:15) and leaving at 18:00.
    const days: string[] = [];
    for (let i = 0; days.length < 4; i++) {
      const d = new Date(Date.parse(`${startAd}T00:00:00Z`) + i * 86_400_000).toISOString().slice(0, 10);
      if (new Date(`${d}T00:00:00Z`).getUTCDay() === 6) continue;
      days.push(d);
    }
    lateDates = days;
    for (const d of days) {
      for (const [time, direction] of [['10:40', 'in'], ['18:00', 'out']] as const) {
        const r = await request(app).post('/api/v1/punch/manual').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: d, time, direction, reason: 'test data' });
        expect(r.status).toBe(201);
      }
    }
    await adjust(DEMO.ram, 'HOME', 0.25);
  });

  it('pure rules: penalty per N lates and allocation over balances in quarter days', () => {
    expect(penaltyFor(2, { lateCount: 3, penaltyDays: 0.5 })).toBe(0);
    expect(penaltyFor(7, { lateCount: 3, penaltyDays: 0.5 })).toBe(1);
    expect(allocatePenalty(1, [{ leaveTypeId: 'a', code: 'A', available: 0.4 }, { leaveTypeId: 'b', code: 'B', available: 5 }]))
      .toEqual({ allocations: [{ leaveTypeId: 'a', code: 'A', days: 0.25 }, { leaveTypeId: 'b', code: 'B', days: 0.75 }], unpaid: 0 });
    expect(allocatePenalty(1, [{ leaveTypeId: 'a', code: 'A', available: 0 }])).toEqual({ allocations: [], unpaid: 1 });
  });

  it('HR sets the rule; only paid balance leave types are accepted; employees cannot change it', async () => {
    const bad = await request(app).put('/api/v1/attendance-tools/policy').set(auth(hr)).send({ latePenalty: { enabled: true, leaveTypeIds: [t('LWP')] } });
    expect(bad.status).toBe(400);
    expect((await request(app).put('/api/v1/attendance-tools/policy').set(auth(sita)).send({})).status).toBe(403);
    const ok = await request(app).put('/api/v1/attendance-tools/policy').set(auth(hr)).send({ latePenalty: { enabled: true, lateCount: 3, penaltyDays: 0.5, minLateMinutes: 5, leaveTypeIds: [t('HOME')] } });
    expect(ok.status).toBe(200);
    expect(ok.body.policy.latePenalty).toMatchObject({ enabled: true, lateCount: 3 });
  });

  it('preview shows late days and the split; the employee sees their own count; managers see their team only', async () => {
    const r = await request(app).get('/api/v1/attendance-tools/penalties').set(auth(hr)).query({ bsYear: Y, bsMonth: M });
    expect(r.status).toBe(200);
    const line = r.body.lines.find((l: { employeeId: string }) => l.employeeId === DEMO.ram);
    expect(line).toMatchObject({ lateDays: 4, penaltyDays: 0.5, unpaidDays: 0.25, applied: null });
    expect(line.lateDates).toEqual(lateDates);
    expect(line.allocations).toEqual([{ leaveTypeId: t('HOME'), code: 'HOME', days: 0.25 }]);
    const me = await request(app).get('/api/v1/attendance-tools/penalties/me').set(auth(ram)).query({ bsYear: Y, bsMonth: M });
    expect(me.body).toMatchObject({ lateDays: 4, projectedDays: 0.5, enabled: true });
    // Ram manages Sita, not himself: his own line is not in his team view.
    const mgr = await request(app).get('/api/v1/attendance-tools/penalties').set(auth(ram)).query({ bsYear: Y, bsMonth: M });
    expect(mgr.body.lines.find((l: { employeeId: string }) => l.employeeId === DEMO.ram)).toBeUndefined();
    expect((await request(app).get('/api/v1/attendance-tools/penalties').set(auth(sita)).query({ bsYear: Y, bsMonth: M })).status).toBe(403);
  });

  it('applying posts the leave deduction and unpaid days for payroll, once; reversing restores the balance', async () => {
    const before = await ledgerBalance(db, DEMO.ram, t('HOME'));
    expect((await request(app).post('/api/v1/attendance-tools/penalties/apply').set(auth(ram)).send({ bsYear: Y, bsMonth: M })).status).toBe(403);
    const a = await request(app).post('/api/v1/attendance-tools/penalties/apply').set(auth(hr)).send({ bsYear: Y, bsMonth: M });
    expect(a.status).toBe(200);
    expect(a.body.applied).toBe(1);
    expect(await ledgerBalance(db, DEMO.ram, t('HOME'))).toBe(before - 0.25);
    expect(await penaltyUnpaidDays(db, DEMO.ram, Y, M)).toBe(0.25);
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE user_id = $1 AND template = 'attendance.late_penalty'`, [ramUserId]);
    expect(n.rows.length).toBe(1);
    const again = await request(app).post('/api/v1/attendance-tools/penalties/apply').set(auth(hr)).send({ bsYear: Y, bsMonth: M });
    expect(again.body).toMatchObject({ applied: 0 });
    const list = await request(app).get('/api/v1/attendance-tools/penalties').set(auth(hr)).query({ bsYear: Y, bsMonth: M });
    const id = list.body.lines.find((l: { employeeId: string }) => l.employeeId === DEMO.ram).applied.id;
    expect((await request(app).post(`/api/v1/attendance-tools/penalties/${id}/reverse`).set(auth(hr)).send({ reason: 'x' })).status).toBe(400);
    const rev = await request(app).post(`/api/v1/attendance-tools/penalties/${id}/reverse`).set(auth(hr)).send({ reason: 'device clock was wrong' });
    expect(rev.status).toBe(200);
    expect(await ledgerBalance(db, DEMO.ram, t('HOME'))).toBe(before);
    expect(await penaltyUnpaidDays(db, DEMO.ram, Y, M)).toBe(0);
    expect((await request(app).post(`/api/v1/attendance-tools/penalties/${id}/reverse`).set(auth(hr)).send({ reason: 'twice over' })).status).toBe(409);
  });

  it('a locked pay run freezes the month', async () => {
    const { startAd, endAd, days } = bsMonthBounds(Y, M);
    const per = await db.query<{ id: string }>(`INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES ($1, $2, $3, $4, $5, $6)
      ON CONFLICT (company_id, bs_year, bs_month) DO UPDATE SET days = EXCLUDED.days RETURNING id`, [DEMO.companyId, Y, M, startAd, endAd, days]);
    await db.query(`INSERT INTO payroll_run (company_id, period_id, run_type, status, locked_at) VALUES ($1, $2, 'regular', 'locked', now())`, [DEMO.companyId, per.rows[0]!.id]);
    const a = await request(app).post('/api/v1/attendance-tools/penalties/apply').set(auth(hr)).send({ bsYear: Y, bsMonth: M });
    expect(a.status).toBe(409);

  });
});

describe('blocking clock-in after the grace time', () => {
  const day = '2026-11-02'; // a Monday
  const at = (hhmm: string) => localTimeMs(day, hhmm);

  it('is off by default', async () => {
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: at('11:30'), direction: 'in' })).resolves.toBeUndefined();
  });

  it('refuses a first clock-in after start + grace (+ extra minutes), never a clock-out', async () => {
    await request(app).put('/api/v1/attendance-tools/policy').set(auth(hr)).send({ blockLateClockIn: true, blockAfterMin: 10 });
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: at('10:20'), direction: 'in' })).resolves.toBeUndefined();
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: at('10:26'), direction: 'in' }))
      .rejects.toMatchObject({ status: 409, details: { code: 'late_blocked', closedAt: '10:25', shiftStart: '10:00' } });
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: at('10:26'), direction: 'out' })).resolves.toBeUndefined();
  });

  it('weekly offs, approved first-half leave and a second clock-in are not blocked', async () => {
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: localTimeMs('2026-11-07', '13:00'), direction: 'in' })).resolves.toBeUndefined();
    await db.query(`INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, half_day, days_requested, status) VALUES ($1, $2, '2026-11-03', '2026-11-03', 'first_half', 0.5, 'approved')`, [DEMO.sita, t('HOME')]);
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: localTimeMs('2026-11-03', '14:00'), direction: 'in' })).resolves.toBeUndefined();
    await request(app).post('/api/v1/punch/manual').set(auth(hr)).send({ employeeId: DEMO.sita, localDate: day, time: '09:55', direction: 'in', reason: 'test data' });
    await expect(assertClockInAllowed(db, { companyId: DEMO.companyId, employeeId: DEMO.sita, atMs: at('14:00'), direction: 'in' })).resolves.toBeUndefined();
    await request(app).put('/api/v1/attendance-tools/policy').set(auth(hr)).send({ blockLateClockIn: false });
  });
});

describe('manual attendance upload', () => {
  const csv = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\nE002,2083-06-01,22:00,06:00,night\nE999,2083-06-01,10:00,18:00,\nE001,2083-13-01,10:00,,\nE001,2083-06-01,10:00,18:00,dup\n';

  it('checks every row first and reports errors per line; only HR', async () => {
    expect((await request(app).post('/api/v1/attendance-tools/upload/check').set(auth(ram)).send({ csv })).status).toBe(403);
    const r = await request(app).post('/api/v1/attendance-tools/upload/check').set(auth(hr)).send({ csv });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ valid: 2, invalid: 3 });
    expect(r.body.rows[1]).toMatchObject({ empCode: 'E002', overnight: true, errors: [] });
    expect(r.body.rows[2].errors[0]).toMatch(/No employee with code E999/);
    expect(r.body.rows[3].errors[0]).toMatch(/not a valid BS date/);
    expect(r.body.rows[4].errors[0]).toMatch(/more than once/);
    const bad = await request(app).post('/api/v1/attendance-tools/upload/import').set(auth(hr)).send({ csv, reason: 'device outage' });
    expect(bad.status).toBe(400);
    expect(bad.body.details.rows).toHaveLength(3);
    const noHeader = await request(app).post('/api/v1/attendance-tools/upload/check').set(auth(hr)).send({ csv: 'a,b\n1,2\n' });
    expect(noHeader.body.errors.length).toBeGreaterThan(0);
  });

  it('imports manual punches, recomputes the days, and a second upload adds nothing', async () => {
    const good = csv.split('\n').slice(0, 3).join('\n');
    const r = await request(app).post('/api/v1/attendance-tools/upload/import').set(auth(hr)).send({ csv: good, reason: 'device outage', filename: 'aug.csv' });
    expect(r.status).toBe(201);
    expect(r.body).toMatchObject({ rows: 2, punches: 4, duplicates: 0 });
    const day = await db.query(`SELECT status FROM v_attendance_latest WHERE employee_id = $1 AND local_date = '2026-09-17'`, [DEMO.sita]);
    expect(day.rows[0]?.status).toBe('Present');
    const again = await request(app).post('/api/v1/attendance-tools/upload/import').set(auth(hr)).send({ csv: good, reason: 'device outage' });
    expect(again.body).toMatchObject({ punches: 0, duplicates: 4 });
    const hist = await request(app).get('/api/v1/attendance-tools/uploads').set(auth(hr));
    expect(hist.body.uploads).toHaveLength(2);
    const tpl = await request(app).get('/api/v1/attendance-tools/upload/template').set(auth(hr));
    expect(tpl.text).toContain('emp_code,date_bs,in_time,out_time,note');
  });
});

describe('attendance issues and nudges', () => {
  let leaveId = '';
  beforeAll(async () => {
    await recomputeDay(db, DEMO.sita, '2026-10-05');
    const l = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-10-07', to: '2026-10-07', reason: 'errand' });
    expect(l.status).toBe(201);
    leaveId = l.body.id;
  });

  it('lists absences and pending requests with who they wait on; managers see their team', async () => {
    const r = await request(app).get('/api/v1/attendance-tools/issues').set(auth(ram)).query({ from: '2026-10-01', to: '2026-10-10' });
    expect(r.status).toBe(200);
    const s = r.body.rows.find((x: { employeeId: string }) => x.employeeId === DEMO.sita);
    expect(s.absent).toContain('2026-10-05');
    expect(s.pending[0]).toMatchObject({ type: 'leave', requestId: leaveId });
    expect(s.pending[0].waitingOn.map((w: { userId: string }) => w.userId)).toContain(ramUserId);
    expect(r.body.rows.find((x: { employeeId: string }) => x.employeeId === OTHER_EMP)).toBeUndefined();
    expect((await request(app).get('/api/v1/attendance-tools/issues').set(auth(sita)).query({ from: '2026-10-01', to: '2026-10-10' })).status).toBe(403);
    expect((await request(app).get('/api/v1/attendance-tools/issues').set(auth(hr)).query({ from: '2026-01-01', to: '2026-10-10' })).status).toBe(400);
  });

  it('the pre-payroll summary counts them', async () => {
    const r = await request(app).get('/api/v1/attendance-tools/issues/summary').set(auth(hr)).query({ bsYear: 2083, bsMonth: 6 });
    expect(r.status).toBe(200);
    expect(r.body.absent).toBeGreaterThan(0);
    expect(r.body.pendingLeave).toBe(1);
  });

  it('nudges the employee and the approver, once a day each', async () => {
    const e1 = await request(app).post('/api/v1/attendance-tools/issues/nudge').set(auth(ram)).send({ target: 'employee', employeeId: DEMO.sita, from: '2026-10-01', to: '2026-10-10' });
    expect(e1.body).toMatchObject({ sent: 1, already: false });
    const e2 = await request(app).post('/api/v1/attendance-tools/issues/nudge').set(auth(ram)).send({ target: 'employee', employeeId: DEMO.sita, from: '2026-10-01', to: '2026-10-10' });
    expect(e2.body).toMatchObject({ sent: 0, already: true });
    const n = await db.query(`SELECT channel, title, created_at FROM notification_outbox WHERE user_id = $1 AND template = 'attendance.nudge' AND channel = 'in_app'`, [sitaUserId]);
    expect(n.rows).toHaveLength(1);
    const inst = await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM leave_request WHERE id = $1', [leaveId]);
    const a = await request(app).post('/api/v1/attendance-tools/issues/nudge').set(auth(hr)).send({ target: 'approver', instanceId: inst.rows[0]!.workflow_instance_id });
    expect(a.body.sent).toBe(1);
    const n2 = await db.query(`SELECT 1 FROM notification_outbox WHERE user_id = $1 AND template = 'attendance.nudge_approver' AND channel = 'in_app'`, [ramUserId]);
    expect(n2.rows.length).toBe(1);
    // An outsider manager-less employee cannot nudge anyone.
    expect((await request(app).post('/api/v1/attendance-tools/issues/nudge').set(auth(outsider)).send({ target: 'employee', employeeId: DEMO.sita, from: '2026-10-01', to: '2026-10-10' })).status).toBe(403);
  });
});

describe("who's out", () => {
  it('shows approved leave in the team scope, without the leave type, and hides it from other teams', async () => {
    const today = new Date(Date.now() + 345 * 60_000).toISOString().slice(0, 10);
    const tomorrow = new Date(Date.parse(`${today}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);
    await db.query(`INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status) VALUES ($1, $2, $3, $3, 1, 'approved')`, [DEMO.sita, t('SICK'), tomorrow]);
    const r = await request(app).get('/api/v1/attendance-tools/whos-out').set(auth(ram)).query({ days: 3 });
    expect(r.status).toBe(200);
    expect(r.body.scope).toBe('team');
    const d = r.body.days.find((x: { date: string }) => x.date === tomorrow);
    expect(d.people).toEqual([expect.objectContaining({ employeeId: DEMO.sita, kind: 'leave', detail: null })]);
    expect(JSON.stringify(r.body)).not.toContain('Sick');
    const other = await request(app).get('/api/v1/attendance-tools/whos-out').set(auth(outsider)).query({ days: 3 });
    expect(other.body.days.flatMap((x: { people: unknown[] }) => x.people)).toHaveLength(0);
    await request(app).put('/api/v1/attendance-tools/policy').set(auth(hr)).send({ whosOutScope: 'company' });
    const co = await request(app).get('/api/v1/attendance-tools/whos-out').set(auth(outsider)).query({ days: 3 });
    expect(co.body.days.flatMap((x: { people: unknown[] }) => x.people).length).toBeGreaterThan(0);
  });
});
