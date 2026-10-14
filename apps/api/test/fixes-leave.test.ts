import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { allocateAnnualLeave } from '../src/modules/leave/accrual.js';
import { ledgerBalance, leaveTypeByCode } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

/** Regression tests for leave fixes: event-based statutory leave, annual allocation catch-up, leave over worked days. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let stranger: string;
const typeId: Record<string, string> = {};
const t = (code: string): string => typeId[code]!;
const L = (path: string) => `/api/v1/leave${path}`;

async function newEmployee(code: string, gender: string, joinDate: string | null): Promise<string> {
  return (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, gender, join_date) VALUES ($1, $2, $3, $4, $5) RETURNING id`,
    [DEMO.companyId, code, `Test ${code}`, gender, joinDate])).rows[0]!.id;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee'], DEMO.ram)).token;
  stranger = (await loginAs(db, app, ['employee'])).token;
  for (const code of ['HOME', 'CASUAL', 'MATERNITY', 'PATERNITY', 'MOURNING', 'SPECIAL_UNPAID']) typeId[code] = (await leaveTypeByCode(db, DEMO.companyId, code))!.id;
});
afterAll(async () => { await db.end(); });

describe('event-based statutory leave (Labour Act Sec 45-47)', () => {
  it('maternity: 60 paid days per event without a balance, beyond the 30-day policy cap; the rest goes to special unpaid leave', async () => {
    const bal = await request(app).get(L('/balances')).set(auth(sita));
    expect(bal.body.balances.find((b: { code: string }) => b.code === 'MATERNITY')).toMatchObject({ available: 60 });
    const tooLong = await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('MATERNITY'), from: '2026-11-23', to: '2027-01-22' });
    expect(tooLong.status).toBe(400);
    expect(tooLong.body.error).toMatch(/60 day\(s\) per event/);
    expect(tooLong.body.suggestion.remainderLeaveTypeId).toBe(t('SPECIAL_UNPAID'));
    const paid = await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('MATERNITY'), from: '2026-11-23', to: '2027-01-21' });
    expect(paid.status, JSON.stringify(paid.body)).toBe(201);
    expect(paid.body.days).toBe(60);
    const after = await request(app).get(L('/balances')).set(auth(sita));
    expect(after.body.balances.find((b: { code: string }) => b.code === 'MATERNITY')).toMatchObject({ available: 0 });
    // A second maternity request for the same event is refused; the remaining 38 days go in as special unpaid leave.
    const more = await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('MATERNITY'), from: '2027-01-22', to: '2027-01-25' });
    expect(more.status).toBe(400);
    expect(more.body.error).toMatch(/60 are already requested/);
    const rest = await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('SPECIAL_UNPAID'), from: '2027-01-22', to: '2027-02-28' });
    expect(rest.status, JSON.stringify(rest.body)).toBe(201);
    expect(rest.body.days).toBeGreaterThan(30);
  });
  it('maternity-care and mourning work for men without a balance and are capped per event; maternity is refused for men', async () => {
    expect((await request(app).post(L('/requests')).set(auth(ram)).send({ leaveTypeId: t('MATERNITY'), from: '2026-11-23', to: '2026-11-24' })).status).toBe(400);
    expect((await request(app).post(L('/requests')).set(auth(ram)).send({ leaveTypeId: t('PATERNITY'), from: '2026-11-23', to: '2026-12-08' })).status).toBe(400); // 16 days
    const care = await request(app).post(L('/requests')).set(auth(ram)).send({ leaveTypeId: t('PATERNITY'), from: '2026-11-23', to: '2026-12-07' });
    expect(care.status, JSON.stringify(care.body)).toBe(201);
    expect(care.body.days).toBe(15);
    const mourning = await request(app).post(L('/requests')).set(auth(ram)).send({ leaveTypeId: t('MOURNING'), from: '2027-03-01', to: '2027-03-13' });
    expect(mourning.status, JSON.stringify(mourning.body)).toBe(201);
    expect(mourning.body.days).toBe(13);
    const preview = await request(app).get(L('/preview')).set(auth(ram)).query({ leaveTypeId: t('MOURNING'), from: '2027-03-14', to: '2027-03-15' });
    expect(preview.body).toMatchObject({ available: 0, balanceAfter: -2 });
  });
  it('statutory event entitlements cannot be edited through the API', async () => {
    expect((await request(app).patch(L(`/types/${t('MATERNITY')}`)).set(auth(hr)).send({ eventDays: 90 })).status).toBe(400);
  });
});

describe('annual allocation catch-up', () => {
  it('a joiner after Shrawan gets a pro-rated casual allocation as soon as balances are viewed, once', async () => {
    const id = await newEmployee('J01', 'male', '2026-09-01');
    const b = await request(app).get(L('/balances')).set(auth(hr)).query({ employeeId: id });
    expect(b.body.balances.find((x: { code: string }) => x.code === 'CASUAL')).toMatchObject({ accrued: 5, balance: 5 });
    await request(app).get(L('/balances')).set(auth(hr)).query({ employeeId: id });
    expect(await ledgerBalance(db, id, t('CASUAL'))).toBe(5);
  });
  it('HR catch-up grants the full year to staff on the books before the fiscal year, idempotently, and respects HR opening allocations', async () => {
    const veteran = await newEmployee('V01', 'female', '2020-01-01');
    const opened = await newEmployee('V02', 'male', '2020-01-01');
    await request(app).post(L('/adjust')).set(auth(hr)).send({ employeeId: opened, leaveTypeId: t('CASUAL'), deltaDays: 3, reason: 'opening balance', kind: 'allocation', effectiveDate: '2026-07-20' });
    expect((await request(app).post(L('/accrual/run')).set(auth(sita))).status).toBe(403);
    const run = await request(app).post(L('/accrual/run')).set(auth(hr));
    expect(run.status).toBe(200);
    expect(run.body.months.map((m: { bsMonth: number }) => m.bsMonth)).toEqual(expect.arrayContaining([4, 5]));
    expect(await ledgerBalance(db, veteran, t('CASUAL'))).toBe(6);
    expect(await ledgerBalance(db, opened, t('CASUAL'))).toBe(3);
    const again = await request(app).post(L('/accrual/run')).set(auth(hr));
    expect(again.body.postings).toBe(0);
    expect(await ledgerBalance(db, veteran, t('CASUAL'))).toBe(6);
    expect(await allocateAnnualLeave(db, DEMO.companyId, '2026-10-14')).toBe(0);
  });
});

describe('leave over days already worked', () => {
  it('full-day leave on a past worked day is refused; a half day is allowed; HR may override', async () => {
    for (const d of ['2026-10-05', '2026-10-06']) {
      await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash) VALUES ($1, $2, 1, 'Present', 480, 'h')`, [DEMO.sita, d]);
    }
    await request(app).post(L('/adjust')).set(auth(hr)).send({ employeeId: DEMO.sita, leaveTypeId: t('HOME'), deltaDays: 5, reason: 'opening balance', kind: 'allocation' });
    const full = await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-10-05', to: '2026-10-05' });
    expect(full.status).toBe(409);
    expect(full.body.error).toMatch(/recorded as working/);
    expect((await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-10-05', to: '2026-10-05', halfDay: 'second_half' })).status).toBe(201);
    expect((await request(app).post(L('/requests')).set(auth(sita)).send({ leaveTypeId: t('HOME'), from: '2026-10-06', to: '2026-10-06', override: true })).status).toBe(403);
    expect((await request(app).post(L('/requests')).set(auth(hr)).send({ employeeId: DEMO.sita, leaveTypeId: t('HOME'), from: '2026-10-06', to: '2026-10-06', override: true })).status).toBe(201);
    expect((await request(app).get(L('/balances')).set(auth(stranger)).query({ employeeId: DEMO.sita })).status).toBe(403);
  });
});

describe('HR opening allocation and the automatic annual grant', () => {
  it('an HR allocation for the year replaces the automatic grant instead of stacking on it', async () => {
    const emp = await newEmployee('ALLOC1', 'female', '2025-01-01');
    expect(await allocateAnnualLeave(db, DEMO.companyId, '2026-10-14', emp)).toBeGreaterThan(0);
    const auto = await ledgerBalance(db, emp, t('CASUAL'));
    expect(auto).toBeGreaterThan(0);
    const res = await request(app).post(L('/adjust')).set(auth(hr)).send({ employeeId: emp, leaveTypeId: t('CASUAL'), deltaDays: 6, kind: 'allocation', reason: 'Opening balance', effectiveDate: '2026-07-17' });
    expect(res.status).toBe(201);
    expect(Number(res.body.replacedAutomatic)).toBe(auto);
    expect(await ledgerBalance(db, emp, t('CASUAL'))).toBe(6);
    // A second HR allocation is an intentional top-up and is not offset again; the automatic grant never comes back.
    await request(app).post(L('/adjust')).set(auth(hr)).send({ employeeId: emp, leaveTypeId: t('CASUAL'), deltaDays: 1, kind: 'allocation', reason: 'Top-up', effectiveDate: '2026-07-17' });
    expect(await allocateAnnualLeave(db, DEMO.companyId, '2026-10-14', emp)).toBe(0);
    expect(await ledgerBalance(db, emp, t('CASUAL'))).toBe(7);
  });
});
