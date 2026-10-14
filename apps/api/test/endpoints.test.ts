import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

/** Endpoints not exercised by the feature suites: one authorised call (status + shape) and one refused call each. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let admin: string; let sita: string; let ram: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  admin = (await loginAs(db, app, ['admin'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager', 'employee'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
});
afterAll(async () => { await db.end(); });

const api = (path: string) => `/api/v1${path}`;

describe('health', () => {
  it('GET /health is public and reports the database', async () => {
    const r = await request(app).get('/health');
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true, db: 'up' });
  });
});

describe('auth and people', () => {
  it('GET /auth/users lists company users for HR only', async () => {
    const r = await request(app).get(api('/auth/users')).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.users.length).toBeGreaterThanOrEqual(4);
    expect(r.body.users[0]).toEqual(expect.objectContaining({ id: expect.any(String), email: expect.any(String), roles: expect.any(Array), status: expect.any(String) }));
    expect(r.body.users[0]).not.toHaveProperty('password_hash');
    expect((await request(app).get(api('/auth/users')).set(auth(sita))).status).toBe(403);
    expect((await request(app).get(api('/auth/users'))).status).toBe(401);
  });
  it('GET /employees/:id/assignments returns effective-dated history to self, manager and HR', async () => {
    const r = await request(app).get(api(`/employees/${DEMO.sita}/assignments`)).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.assignments[0]).toMatchObject({ employee_id: DEMO.sita, valid: '[2026-01-01,)', branch_id: DEMO.branchId, manager_id: DEMO.ram });
    expect((await request(app).get(api(`/employees/${DEMO.sita}/assignments`)).set(auth(sita))).status).toBe(200);
    expect((await request(app).get(api(`/employees/${DEMO.sita}/assignments`)).set(auth(ram))).status).toBe(200);
    expect((await request(app).get(api(`/employees/${DEMO.ram}/assignments`)).set(auth(sita))).status).toBe(403);
  });
  it('GET /attendance/punches returns raw punches for an employee and range', async () => {
    await db.query(`INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
      VALUES ($1, 'DEMO-01', 'zkteco', '1', $2, '2026-09-01T04:20:00Z', '2026-09-01 10:05:00', '2026-09-01', 'unknown', 'fingerprint', 'zkteco', 'ep-1')`, [DEMO.deviceId, DEMO.sita]);
    const r = await request(app).get(api('/attendance/punches')).set(auth(sita)).query({ employeeId: DEMO.sita, from: '2026-09-01', to: '2026-09-02' });
    expect(r.status).toBe(200);
    expect(r.body.punches).toHaveLength(1);
    expect(r.body.punches[0]).toMatchObject({ local_date: '2026-09-01', superseded: false });
    expect((await request(app).get(api('/attendance/punches')).set(auth(sita)).query({ employeeId: DEMO.ram, from: '2026-09-01', to: '2026-09-02' })).status).toBe(403);
    expect((await request(app).get(api('/attendance/punches')).set(auth(sita)).query({ employeeId: DEMO.sita })).status).toBe(400);
  });
});

describe('devices', () => {
  it('GET /devices lists devices with health counters', async () => {
    const r = await request(app).get(api('/devices')).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.devices.find((d: { id: string }) => d.id === DEMO.deviceId)).toMatchObject({ serial_no: 'DEMO-01', offline: true, mapped_users: '2', queued_commands: '0' });
    expect((await request(app).get(api('/devices')).set(auth(sita))).status).toBe(403);
  });
  it('POST /devices/:id/sync-users queues one USERINFO command per mapped active employee', async () => {
    const r = await request(app).post(api(`/devices/${DEMO.deviceId}/sync-users`)).set(auth(hr)).send({});
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ queued: 2 });
    const cmds = await db.query<{ command: string }>(`SELECT command FROM device_command WHERE device_id = $1 AND status = 'queued' ORDER BY command`, [DEMO.deviceId]);
    expect(cmds.rows.map((c) => c.command)).toEqual([expect.stringMatching(/^DATA UPDATE USERINFO PIN=1\tName=/), expect.stringMatching(/^DATA UPDATE USERINFO PIN=2\tName=/)]);
    expect((await request(app).post(api('/devices/00000000-0000-7000-8000-0000000000ff/sync-users')).set(auth(hr)).send({})).body).toEqual({ queued: 0 });
    expect((await request(app).post(api(`/devices/${DEMO.deviceId}/sync-users`)).set(auth(sita)).send({})).status).toBe(403);
  });
});

describe('shifts, holidays, locations and roster', () => {
  let holidayId = '';
  it('GET /shifts/holidays filters by range; PATCH and DELETE edit a holiday (HR only)', async () => {
    const c = await request(app).post(api('/shifts/holidays')).set(auth(hr)).send({ localDate: '2026-12-31', nameEn: 'Company anniversary' });
    expect(c.status).toBe(201);
    holidayId = c.body.id;
    const list = await request(app).get(api('/shifts/holidays')).set(auth(sita)).query({ from: '2026-12-01', to: '2026-12-31' });
    expect(list.status).toBe(200);
    expect(list.body.holidays.find((h: { id: string }) => h.id === holidayId)).toMatchObject({ name_en: 'Company anniversary', optional: false, gender_scope: 'all' });
    expect((await request(app).get(api('/shifts/holidays')).set(auth(sita)).query({ from: '2027-01-01', to: '2027-01-02' })).body.holidays).toEqual([]);

    expect((await request(app).patch(api(`/shifts/holidays/${holidayId}`)).set(auth(sita)).send({ nameEn: 'x' })).status).toBe(403);
    const p = await request(app).patch(api(`/shifts/holidays/${holidayId}`)).set(auth(hr)).send({ nameEn: 'Founders day', optional: true, localDate: '2026-12-30' });
    expect(p.status).toBe(200);
    const row = await db.query('SELECT name_en, optional, local_date::text, gender_scope FROM holiday WHERE id = $1', [holidayId]);
    expect(row.rows[0]).toEqual({ name_en: 'Founders day', optional: true, local_date: '2026-12-30', gender_scope: 'all' });
    const rq = await db.query(`SELECT count(*)::int AS n FROM recompute_queue WHERE local_date IN ('2026-12-30', '2026-12-31')`);
    expect(rq.rows[0]!.n).toBeGreaterThan(0);

    expect((await request(app).delete(api(`/shifts/holidays/${holidayId}`)).set(auth(sita))).status).toBe(403);
    expect((await request(app).delete(api(`/shifts/holidays/${holidayId}`)).set(auth(hr))).status).toBe(200);
    expect((await request(app).delete(api(`/shifts/holidays/${holidayId}`)).set(auth(hr))).status).toBe(404);
    expect((await request(app).patch(api(`/shifts/holidays/${holidayId}`)).set(auth(hr)).send({ nameEn: 'gone' })).status).toBe(404);
  });
  it('PATCH /shifts/locations/:id updates a geofence', async () => {
    const c = await request(app).post(api('/shifts/locations')).set(auth(hr)).send({ name: 'HQ', lat: 27.7, lng: 85.3 });
    expect(c.status).toBe(201);
    const p = await request(app).patch(api(`/shifts/locations/${c.body.id}`)).set(auth(hr)).send({ radiusM: 300, name: 'Kathmandu HQ' });
    expect(p.status).toBe(200);
    expect((await db.query('SELECT name, radius_m FROM location WHERE id = $1', [c.body.id])).rows[0]).toEqual({ name: 'Kathmandu HQ', radius_m: 300 });
    expect((await request(app).patch(api(`/shifts/locations/${c.body.id}`)).set(auth(hr)).send({ radiusM: 5 })).status).toBe(400);
    expect((await request(app).patch(api(`/shifts/locations/${c.body.id}`)).set(auth(sita)).send({ radiusM: 300 })).status).toBe(403);
    expect((await request(app).patch(api('/shifts/locations/00000000-0000-7000-8000-0000000000ff')).set(auth(hr)).send({ radiusM: 300 })).status).toBe(404);
  });
  it('PATCH /shifts/:id changes shift rules (HR only)', async () => {
    const p = await request(app).patch(api(`/shifts/${DEMO.shiftId}`)).set(auth(hr)).send({ graceInMin: 10, nameEn: 'General 10-18 (10 min grace)' });
    expect(p.status).toBe(200);
    expect((await db.query('SELECT grace_in_min, name_en FROM shift WHERE id = $1', [DEMO.shiftId])).rows[0]).toEqual({ grace_in_min: 10, name_en: 'General 10-18 (10 min grace)' });
    expect((await request(app).patch(api(`/shifts/${DEMO.shiftId}`)).set(auth(sita)).send({ graceInMin: 0 })).status).toBe(403);
    expect((await request(app).patch(api('/shifts/00000000-0000-7000-8000-0000000000ff')).set(auth(hr)).send({ graceInMin: 0 })).status).toBe(404);
  });
  it('DELETE /shifts/roster removes a roster day; POST /shifts/roster/publish notifies rostered employees', async () => {
    const up = await request(app).put(api('/shifts/roster')).set(auth(hr)).send({ rows: [{ employeeId: DEMO.sita, localDate: '2026-11-02', shiftId: DEMO.shiftId }] });
    expect([200, 201]).toContain(up.status);
    const pub = await request(app).post(api('/shifts/roster/publish')).set(auth(hr)).send({ from: '2026-11-01', to: '2026-11-07' });
    expect(pub.status).toBe(200);
    expect(pub.body).toEqual({ notified: 1 });
    expect((await request(app).post(api('/shifts/roster/publish')).set(auth(sita)).send({ from: '2026-11-01', to: '2026-11-07' })).status).toBe(403);
    // A manager publishes only for their direct reports.
    const otherMgr = await loginAs(db, app, ['manager'], null);
    expect((await request(app).post(api('/shifts/roster/publish')).set(auth(otherMgr.token)).send({ from: '2026-11-01', to: '2026-11-07' })).body).toEqual({ notified: 0 });
    expect((await request(app).post(api('/shifts/roster/publish')).set(auth(ram)).send({ from: '2026-11-01', to: '2026-11-07' })).body).toEqual({ notified: 1 });

    expect((await request(app).delete(api('/shifts/roster')).set(auth(sita)).send({ employeeId: DEMO.sita, localDate: '2026-11-02' })).status).toBe(403);
    const del = await request(app).delete(api('/shifts/roster')).set(auth(ram)).send({ employeeId: DEMO.sita, localDate: '2026-11-02' });
    expect(del.status).toBe(200);
    expect(del.body).toEqual({ deleted: 1 });
    expect((await request(app).delete(api('/shifts/roster')).set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-11-02' })).body).toEqual({ deleted: 0 });
  });
});

describe('leave and overtime lists', () => {
  it('GET/POST /leave/types: everyone reads, HR creates', async () => {
    const list = await request(app).get(api('/leave/types')).set(auth(sita));
    expect(list.status).toBe(200);
    expect(list.body.types.length).toBeGreaterThan(0);
    expect(list.body.types[0]).toEqual(expect.objectContaining({ id: expect.any(String), code: expect.any(String), name_en: expect.any(String), paid: expect.any(Boolean) }));
    const c = await request(app).post(api('/leave/types')).set(auth(hr)).send({ code: 'STUDY', nameEn: 'Study leave', paid: false, maxConsecutiveDays: 10 });
    expect(c.status).toBe(201);
    expect((await request(app).get(api('/leave/types')).set(auth(sita))).body.types.find((t: { id: string }) => t.id === c.body.id)).toMatchObject({ code: 'STUDY', paid: false, statutory: false });
    expect((await request(app).post(api('/leave/types')).set(auth(hr)).send({ code: 'STUDY', nameEn: 'Again' })).status).toBe(409);
    expect((await request(app).post(api('/leave/types')).set(auth(sita)).send({ code: 'X', nameEn: 'X' })).status).toBe(403);
  });
  it('GET /leave/requests scopes own / team / all', async () => {
    const t = (await request(app).get(api('/leave/types')).set(auth(sita))).body.types.find((x: { code: string }) => x.code === 'STUDY');
    const c = await request(app).post(api('/leave/requests')).set(auth(sita)).send({ leaveTypeId: t.id, from: '2026-11-16', to: '2026-11-16', reason: 'exam' });
    expect(c.status).toBe(201);
    const own = await request(app).get(api('/leave/requests')).set(auth(sita));
    expect(own.status).toBe(200);
    expect(own.body.requests).toHaveLength(1);
    expect(own.body.requests[0]).toMatchObject({ employee_id: DEMO.sita, type_code: 'STUDY', status: 'pending' });
    expect((await request(app).get(api('/leave/requests')).set(auth(ram))).body.requests.map((x: { id: string }) => x.id)).toContain(c.body.id);
    expect((await request(app).get(api('/leave/requests')).set(auth(hr)).query({ status: 'approved' })).body.requests).toEqual([]);
    expect((await request(app).get(api('/leave/requests')).set(auth(sita)).query({ scope: 'all' })).status).toBe(403);
    expect((await request(app).get(api('/leave/requests')).set(auth(sita)).query({ employeeId: DEMO.ram })).status).toBe(403);
  });
  it('GET /ot/requests lists and POST /ot/requests/:id/cancel cancels a pending request', async () => {
    await db.query('UPDATE employee SET ot_eligible = true WHERE id = $1', [DEMO.sita]);
    const c = await request(app).post(api('/ot/requests')).set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-11-03', requestedMin: 60, reason: 'release' });
    expect(c.status).toBe(201);
    const own = await request(app).get(api('/ot/requests')).set(auth(sita));
    expect(own.status).toBe(200);
    expect(own.body.requests[0]).toMatchObject({ id: c.body.id, employee_id: DEMO.sita, requested_min: 60, status: 'pending' });
    expect((await request(app).get(api('/ot/requests')).set(auth(sita)).query({ scope: 'all' })).status).toBe(403);

    expect((await request(app).post(api(`/ot/requests/${c.body.id}/cancel`)).set(auth(sita)).send({})).status).toBe(200);
    expect((await db.query('SELECT status FROM ot_request WHERE id = $1', [c.body.id])).rows[0].status).toBe('cancelled');
    expect((await request(app).post(api(`/ot/requests/${c.body.id}/cancel`)).set(auth(sita)).send({})).status).toBe(409);
    expect((await request(app).post(api('/ot/requests/00000000-0000-7000-8000-0000000000ff/cancel')).set(auth(sita)).send({})).status).toBe(404);

    const other = await loginAs(db, app, ['employee'], null);
    const c2 = await request(app).post(api('/ot/requests')).set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-11-04', requestedMin: 30 });
    expect((await request(app).post(api(`/ot/requests/${c2.body.id}/cancel`)).set(auth(other.token)).send({})).status).toBe(403);
  });
});

describe('settings', () => {
  it('PATCH /settings/branches/:id renames a branch (HR/admin only)', async () => {
    const r = await request(app).patch(api(`/settings/branches/${DEMO.branchId}`)).set(auth(admin)).send({ nameEn: 'Kathmandu HQ' });
    expect(r.status).toBe(200);
    expect(r.body.branch).toMatchObject({ id: DEMO.branchId, code: 'HQ', name_en: 'Kathmandu HQ' });
    expect((await request(app).patch(api(`/settings/branches/${DEMO.branchId}`)).set(auth(sita)).send({ nameEn: 'x' })).status).toBe(403);
    expect((await request(app).patch(api('/settings/branches/00000000-0000-7000-8000-0000000000ff')).set(auth(hr)).send({ nameEn: 'x' })).status).toBe(404);
  });
  it('GET /settings/org-units lists departments and teams', async () => {
    const c = await request(app).post(api('/settings/org-units')).set(auth(hr)).send({ kind: 'department', code: 'ENG', nameEn: 'Engineering' });
    expect(c.status).toBe(201);
    const r = await request(app).get(api('/settings/org-units')).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.orgUnits).toEqual([expect.objectContaining({ kind: 'department', code: 'ENG', name_en: 'Engineering' })]);
    expect((await request(app).get(api('/settings/org-units')).set(auth(sita))).status).toBe(403);
  });
});

describe('payroll setup and run lists', () => {
  const P = (path: string) => api(`/payroll${path}`);
  it('POST /payroll/structures creates version 1; POST /structures/:id/versions adds the next version', async () => {
    const s = await request(app).post(P('/structures')).set(auth(hr)).send({ code: 'EP', nameEn: 'Endpoint structure', components: [{ code: 'BASIC' }] });
    expect(s.status).toBe(201);
    expect(s.body).toEqual({ id: expect.any(String), versionId: expect.any(String) });
    const v = await request(app).post(P(`/structures/${s.body.id}/versions`)).set(auth(hr)).send({ components: [{ code: 'BASIC' }, { code: 'GRADE' }] });
    expect(v.status).toBe(201);
    expect(v.body).toMatchObject({ id: expect.any(String), version: 2 });
    const list = await request(app).get(P('/structures')).set(auth(hr));
    expect(list.body.structures.find((x: { id: string }) => x.id === s.body.id).versions.map((x: { version: number }) => x.version)).toEqual([1, 2]);
    expect((await request(app).post(P('/structures')).set(auth(hr)).send({ code: 'EP', nameEn: 'dup', components: [{ code: 'BASIC' }] })).status).toBe(409);
    expect((await request(app).post(P('/structures/00000000-0000-7000-8000-0000000000ff/versions')).set(auth(hr)).send({ components: [{ code: 'BASIC' }] })).status).toBe(404);
    expect((await request(app).post(P('/structures')).set(auth(sita)).send({ code: 'NO', nameEn: 'no', components: [{ code: 'BASIC' }] })).status).toBe(403);
  });
  it('GET /payroll/loans, /periods, /runs and /runs/:id/inputs list for HR and refuse employees', async () => {
    const loan = await request(app).post(P('/loans')).set(auth(hr)).send({ employeeId: DEMO.sita, principal: '10000', emi: '2500', startBsYear: 2083, startBsMonth: 7 });
    expect(loan.status).toBe(201);
    const loans = await request(app).get(P('/loans')).set(auth(hr)).query({ employeeId: DEMO.sita });
    expect(loans.status).toBe(200);
    expect(loans.body.loans[0]).toMatchObject({ id: loan.body.id, emp_code: 'E001', installments: 4, repaid: '0' });

    const period = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 7 });
    expect(period.status).toBe(201);
    const periods = await request(app).get(P('/periods')).set(auth(hr));
    expect(periods.status).toBe(200);
    expect(periods.body.periods[0]).toMatchObject({ id: period.body.period.id, bs_year: 2083, bs_month: 7 });

    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: period.body.period.id });
    expect(run.status).toBe(201);
    const runs = await request(app).get(P('/runs')).set(auth(hr));
    expect(runs.status).toBe(200);
    expect(runs.body.runs[0]).toMatchObject({ id: run.body.id, bs_year: 2083, bs_month: 7, run_type: 'regular', status: 'draft' });

    const comp = (await request(app).get(P('/components')).set(auth(hr))).body.components.find((c: { type: string; active: boolean; code: string }) => c.type === 'earning' && c.active);
    expect((await request(app).post(P(`/runs/${run.body.id}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: comp.code, amount: '1500', note: 'one-off' }] })).status).toBe(201);
    const inputs = await request(app).get(P(`/runs/${run.body.id}/inputs`)).set(auth(hr));
    expect(inputs.status).toBe(200);
    expect(inputs.body.inputs).toEqual([expect.objectContaining({ employee_id: DEMO.sita, emp_code: 'E001', component_code: comp.code, note: 'one-off' })]);
    expect(Number(inputs.body.inputs[0].amount)).toBe(1500);

    for (const path of ['/loans', '/periods', '/runs', `/runs/${run.body.id}/inputs`]) {
      expect((await request(app).get(P(path)).set(auth(sita))).status, path).toBe(403);
    }
    expect((await request(app).get(P('/runs/00000000-0000-7000-8000-0000000000ff/inputs')).set(auth(hr))).status).toBe(404);
  });
});

describe('biometrics', () => {
  it('GET /biometrics/templates/unmapped and /biometrics/stats (HR only)', async () => {
    await db.query(`INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
                    VALUES (NULL, '99', 'fingerprint', 'zk_fp_v10', '\\x00'::bytea, 1, 'h', $1)`, [DEMO.deviceId]);
    const u = await request(app).get(api('/biometrics/templates/unmapped')).set(auth(hr));
    expect(u.status).toBe(200);
    expect(u.body.templates).toEqual([expect.objectContaining({ modality: 'fingerprint', device_user_pin: '99', device_sn: 'DEMO-01' })]);
    expect(u.body.templates[0]).not.toHaveProperty('ciphertext');
    const s = await request(app).get(api('/biometrics/stats')).set(auth(hr));
    expect(s.status).toBe(200);
    expect(s.body.byModality).toEqual([{ modality: 'fingerprint', active: 1, shredded: 0, unmapped: 1 }]);
    expect((await request(app).get(api('/biometrics/templates/unmapped')).set(auth(sita))).status).toBe(403);
    expect((await request(app).get(api('/biometrics/stats')).set(auth(sita))).status).toBe(403);
  });
});
