import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** Regression tests for authorisation, tenant isolation, injection and race fixes found in the security review. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let admin: string; let sita: string; let ramMgr: string; let stranger: string;
let otherCompanyId: string; let otherBranchId: string; let otherEmployeeId: string; let otherShiftId: string;
const PASS = 'correct-horse-battery-staple';
const daysAgo = (n: number) => localDateOf(Date.now() - n * 86_400_000);

async function lockedDay(employeeId: string, date: string) {
  await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at) VALUES ($1, $2, 50, 'Present', 480, 'h', now())`, [employeeId, date]);
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  admin = (await loginAs(db, app, ['admin'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ramMgr = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  stranger = (await loginAs(db, app, ['employee'])).token;
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`);
  otherCompanyId = c.rows[0]!.id;
  otherBranchId = (await db.query<{ id: string }>(`INSERT INTO branch (company_id, code, name_en) VALUES ($1, 'OB', 'Other branch') RETURNING id`, [otherCompanyId])).rows[0]!.id;
  otherEmployeeId = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, weekly_off_days) VALUES ($1, 'X1', 'Foreign', '{0,3}') RETURNING id`, [otherCompanyId])).rows[0]!.id;
  otherShiftId = (await db.query<{ id: string }>(`INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES ($1, 'OS', 'Other shift', '09:00', '17:00') RETURNING id`, [otherCompanyId])).rows[0]!.id;
  await createUser(db, { companyId: otherCompanyId, email: 'hr@other.local', name: 'Other HR', password: PASS, roles: ['hr'], pepper: TEST_PEPPER });
});
afterAll(async () => { await db.end(); });

describe('manager scoping', () => {
  it('a manager lists only their direct reports and themselves; HR lists everyone', async () => {
    await db.query(`UPDATE employee_assignment SET manager_id = NULL WHERE employee_id = $1`, [DEMO.sita]);
    const before = await request(app).get('/api/v1/employees').set(auth(ramMgr));
    expect(before.body.employees.map((e: { id: string }) => e.id)).toEqual([DEMO.ram]);
    await db.query(`UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1 AND upper_inf(valid)`, [DEMO.sita, DEMO.ram]);
    const after = await request(app).get('/api/v1/employees').set(auth(ramMgr));
    expect(after.body.employees.map((e: { id: string }) => e.id).sort()).toEqual([DEMO.sita, DEMO.ram].sort());
    const all = await request(app).get('/api/v1/employees').set(auth(hr));
    expect(all.body.employees.length).toBeGreaterThanOrEqual(2);
    await db.query(`UPDATE employee_assignment SET manager_id = NULL WHERE employee_id = $1`, [DEMO.sita]);
  });

  it('exceptions and employee-level reports are limited to the manager\'s team', async () => {
    const d = daysAgo(3);
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash) VALUES ($1, $2, 40, 'Absent', 'h')`, [DEMO.sita, d]);
    const ex = await request(app).get('/api/v1/attendance/exceptions').set(auth(ramMgr)).query({ from: d, to: d });
    expect(ex.status).toBe(200);
    expect(ex.body.exceptions.some((x: { employee_id: string }) => x.employee_id === DEMO.sita)).toBe(false);
    expect((await request(app).get('/api/v1/attendance/exceptions').set(auth(hr)).query({ from: d, to: d })).body.exceptions.some((x: { employee_id: string }) => x.employee_id === DEMO.sita)).toBe(true);
    const daily = await request(app).get('/api/v1/reports/daily').set(auth(ramMgr)).query({ date: d });
    expect(daily.body.rows.map((r: { emp_code: string }) => r.emp_code)).toEqual(['E002']);
    expect((await request(app).get('/api/v1/reports/register').set(auth(ramMgr)).query({ bsYear: 2083, bsMonth: 6, employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get('/api/v1/reports/leave-balances').set(auth(ramMgr))).status).toBe(403);
    expect((await request(app).get('/api/v1/reports/devices').set(auth(ramMgr))).status).toBe(403);
    expect((await request(app).get('/api/v1/reports/leave-balances').set(auth(hr))).status).toBe(200);
  });

  it('the manager dashboard does not show payroll totals', async () => {
    await db.query(`INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES ($1, 2083, 1, '2026-04-14', '2026-05-14', 31)`, [DEMO.companyId]);
    await db.query(`INSERT INTO payroll_run (company_id, period_id, run_type) SELECT $1, id, 'regular' FROM payroll_period WHERE company_id = $1 AND bs_year = 2083 AND bs_month = 1`, [DEMO.companyId]);
    expect((await request(app).get('/api/v1/dashboard/executive').set(auth(ramMgr))).body.lastPayrollRun).toBeNull();
    expect((await request(app).get('/api/v1/dashboard/executive').set(auth(hr))).body.lastPayrollRun).not.toBeNull();
  });
});

describe('approvals', () => {
  let instanceId = ''; const day = daysAgo(2);
  it('only people involved can read an approval; others get 404', async () => {
    const r = await request(app).post('/api/v1/regularisation').set(auth(sita)).send({ localDate: day, kind: 'missed_punch', punchTime: `${day}T09:05:00+05:45`, punchDirection: 'in', reason: 'card reader was down' });
    expect(r.status).toBe(201);
    instanceId = (await db.query<{ workflow_instance_id: string }>('SELECT workflow_instance_id FROM regularisation_request WHERE id = $1', [r.body.id])).rows[0]!.workflow_instance_id;
    expect((await request(app).get(`/api/v1/workflow/${instanceId}`).set(auth(sita))).status).toBe(200);
    expect((await request(app).get(`/api/v1/workflow/${instanceId}`).set(auth(hr))).status).toBe(200);
    expect((await request(app).get(`/api/v1/workflow/${instanceId}`).set(auth(stranger))).status).toBe(404);
  });
  it('a correction cannot be approved once payroll has locked that day', async () => {
    await lockedDay(DEMO.sita, day);
    const ap = await request(app).post(`/api/v1/workflow/${instanceId}/approve`).set(auth(hr)).send({});
    expect(ap.status).toBe(409);
    expect((await db.query('SELECT count(*)::int AS n FROM punch_event WHERE employee_id = $1 AND source = $2', [DEMO.sita, 'regularisation'])).rows[0].n).toBe(0);
  });
});

describe('punches', () => {
  it('the punch time is the server clock, not a phone clock set a few minutes back', async () => {
    const before = Date.now();
    const r = await request(app).post('/api/v1/punch').set(auth(sita)).send({ source: 'web', direction: 'in', clientTime: new Date(before - 9 * 60_000).toISOString(), idempotencyKey: 'sec-review-1', reason: 'test' });
    expect(r.status).toBe(201);
    expect(Math.abs(new Date(r.body.punch.punched_at).getTime() - before)).toBeLessThan(60_000);
    const raw = await db.query<{ raw: { clientTime: string } }>('SELECT raw FROM punch_event WHERE id = $1', [r.body.punch.id]);
    expect(raw.rows[0]!.raw.clientTime).toBeTruthy();
  });
  it('HR cannot add a manual punch to a payroll-locked day', async () => {
    await lockedDay(DEMO.ram, '2026-01-05');
    expect((await request(app).post('/api/v1/punch/manual').set(auth(hr)).send({ employeeId: DEMO.ram, localDate: '2026-01-05', time: '09:00', direction: 'in', reason: 'forgot card' })).status).toBe(409);
  });
});

describe('devices', () => {
  it('a PIN with control characters cannot inject device commands; a foreign branch cannot be attached', async () => {
    const dev = await db.query<{ id: string }>(`INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES ($1, 'SEC-ZK-1', 'zkteco', 'adms', 'active') RETURNING id`, [DEMO.companyId]);
    const id = dev.rows[0]!.id;
    const bad = await request(app).post(`/api/v1/devices/${id}/map`).set(auth(hr)).send({ pin: '7\tPri=14', employeeId: DEMO.sita, from: '2026-01-01' });
    expect(bad.status).toBe(400);
    expect((await request(app).post(`/api/v1/devices/${id}/map`).set(auth(hr)).send({ pin: '7\nC:1:CLEAR DATA', employeeId: DEMO.sita, from: '2026-01-01' })).status).toBe(400);
    expect((await request(app).post(`/api/v1/devices/${id}/map`).set(auth(hr)).send({ pin: '7', employeeId: DEMO.sita, from: '2026-01-01' })).status).toBe(201);
    expect((await db.query(`SELECT count(*)::int AS n FROM device_command WHERE device_id = $1 AND command LIKE '%Pri=14%'`, [id])).rows[0].n).toBe(0);
    expect((await request(app).post(`/api/v1/devices/${id}/activate`).set(auth(hr)).send({ branchId: otherBranchId })).status).toBe(404);
  });
});

describe('cross-company references', () => {
  it('holidays, locations and org units refuse another company\'s branch', async () => {
    expect((await request(app).post('/api/v1/shifts/holidays').set(auth(hr)).send({ localDate: '2026-12-31', nameEn: 'Anniversary', branchId: otherBranchId })).status).toBe(404);
    expect((await request(app).post('/api/v1/shifts/locations').set(auth(hr)).send({ name: 'HQ', lat: 27.7, lng: 85.3, branchId: otherBranchId })).status).toBe(404);
    expect((await request(app).post('/api/v1/settings/org-units').set(auth(hr)).send({ kind: 'team', code: 'T1', nameEn: 'Team', branchId: otherBranchId })).status).toBe(404);
    const unit = await request(app).post('/api/v1/settings/org-units').set(auth(hr)).send({ kind: 'team', code: 'T2', nameEn: 'Team two' });
    expect((await request(app).patch(`/api/v1/settings/org-units/${unit.body.orgUnit.id}`).set(auth(hr)).send({ parentId: unit.body.orgUnit.id })).status).toBe(400);
  });
  it('the calendar and shift delete do not reveal another company\'s data', async () => {
    expect((await request(app).get('/api/v1/shifts/calendar').set(auth(sita)).query({ from: '2026-10-01', to: '2026-10-07', employeeId: otherEmployeeId })).status).toBe(404);
    expect((await request(app).delete(`/api/v1/shifts/${otherShiftId}`).set(auth(hr))).status).toBe(404);
  });
  it('a pay run cannot name another company\'s employees (it would lock their attendance)', async () => {
    const p = await request(app).post('/api/v1/payroll/periods').set(auth(hr)).send({ bsYear: 2083, bsMonth: 4 });
    const r = await request(app).post('/api/v1/payroll/runs').set(auth(hr)).send({ periodId: p.body.period.id, runType: 'supplementary', employeeIds: [otherEmployeeId] });
    expect(r.status).toBe(404);
  });
});

describe('input validation', () => {
  it('very long date ranges are refused instead of expanded day by day', async () => {
    const types = await request(app).get('/api/v1/leave/types').set(auth(sita));
    const sick = types.body.types.find((t: { code: string }) => t.code === 'SICK');
    const r = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: sick.id, from: '0001-01-01', to: '9999-12-31' });
    expect(r.status).toBe(400);
    expect((await request(app).get('/api/v1/shifts/calendar').set(auth(sita)).query({ from: '0001-01-01', to: '9999-12-31' })).status).toBe(400);
  });
  it('declarations and salary amounts cannot be negative', async () => {
    const neg = await request(app).put('/api/v1/payroll/declarations').set(auth(sita)).send({ fiscalYearStart: 2083, citMonthly: '-50000.00' });
    expect(neg.status).toBe(400);
    expect((await request(app).put('/api/v1/payroll/declarations').set(auth(sita)).send({ fiscalYearStart: 2083, citMonthly: '2000.00' })).status).toBe(200);
    const s = await request(app).get('/api/v1/payroll/structures').set(auth(hr));
    const ver = s.body.structures[0].versions[0].id;
    expect((await request(app).post('/api/v1/payroll/assignments').set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: ver, from: '2026-07-17', baseAmounts: { BASIC: '-40000.00' } })).status).toBe(400);
  });
  it('rule sources must be http(s) links', async () => {
    const body = { ruleKey: 'test.rule', validFromAd: '2026-01-01', validFromBs: '2082-09-17', value: { x: 1 } };
    expect((await request(app).post('/api/v1/payroll/rules').set(auth(admin)).send({ ...body, sourceUrl: 'javascript:alert(1)' })).status).toBe(400);
    expect((await request(app).post('/api/v1/payroll/rules').set(auth(admin)).send({ ...body, sourceUrl: 'https://ird.gov.np/' })).status).toBe(201);
  });
});

describe('overtime', () => {
  it('converting a slip to substitute leave twice credits it only once', async () => {
    const slip = await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, day_type) VALUES ($1, $2, 240, 'working') RETURNING id`, [DEMO.ram, daysAgo(4)]);
    const id = slip.rows[0]!.id;
    const [a, b] = await Promise.all([
      request(app).post(`/api/v1/ot/slips/${id}/comp-off`).set(auth(hr)),
      request(app).post(`/api/v1/ot/slips/${id}/comp-off`).set(auth(hr)),
    ]);
    expect([a.status, b.status].sort()).toEqual([200, 409]);
    const credits = await db.query(`SELECT count(*)::int AS n FROM leave_ledger WHERE ref_type = 'ot_slip' AND ref_id = $1`, [id]);
    expect(credits.rows[0].n).toBe(1);
  });
  it('the OT register CSV neutralises formulas in names', async () => {
    await db.query(`UPDATE employee SET name_en = '=HYPERLINK("http://evil","x")' WHERE id = $1`, [DEMO.ram]);
    const csv = await request(app).get('/api/v1/ot/register').set(auth(hr)).query({ from: '2026-01-01', to: '2026-12-31', format: 'csv' });
    expect(csv.text).toContain(`"'=HYPERLINK(""http://evil"",""x"")"`);
    expect(csv.text).not.toMatch(/,=HYPERLINK/);
    await db.query(`UPDATE employee SET name_en = 'Ram Thapa' WHERE id = $1`, [DEMO.ram]);
  });
});

describe('device keys and payroll configuration errors', () => {
  it('HR issues a Hikvision device key once (only its hash is stored); ADMS and foreign devices are refused', async () => {
    const hik = (await db.query<{ id: string }>(`INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES ($1, 'HIK-KEY-1', 'hikvision', 'isapi', 'active') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    const res = await request(app).post(`/api/v1/devices/${hik}/key`).set(auth(hr));
    expect(res.status).toBe(201);
    expect(res.body.key).toMatch(/^[A-Za-z0-9_-]{32}$/);
    const { createHash } = await import('node:crypto');
    const stored = (await db.query('SELECT comm_key_hash FROM device WHERE id = $1', [hik])).rows[0].comm_key_hash;
    expect(stored).toBe(createHash('sha256').update(res.body.key).digest('hex'));
    expect((await request(app).post(`/api/v1/devices/${hik}/key`).set(auth(sita))).status).toBe(403);
    expect((await request(app).post(`/api/v1/devices/${DEMO.deviceId}/key`).set(auth(hr))).status).toBe(400);
    const foreign = (await db.query<{ id: string }>(`INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES ($1, 'HIK-KEY-2', 'hikvision', 'isapi', 'active') RETURNING id`, [otherCompanyId])).rows[0]!.id;
    expect((await request(app).post(`/api/v1/devices/${foreign}/key`).set(auth(hr))).status).toBe(404);
  });
});
