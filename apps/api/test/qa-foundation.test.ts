import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import pino from 'pino';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { toCsv } from '../src/modules/reports/csv.js';
import { drainRecomputeQueue, MAX_RECOMPUTE_ATTEMPTS } from '../src/worker.js';
import { enqueueRecompute } from '../src/modules/attendance/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER, testCipher } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let admin: string; let adminId: string; let emp: string;
const OTHER = '00000000-0000-7000-8000-00000000aaaa';
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  ({ token: hr } = await loginAs(db, app, ['hr']));
  ({ token: admin, userId: adminId } = await loginAs(db, app, ['admin']));
  ({ token: emp } = await loginAs(db, app, ['employee'], DEMO.sita));
  await db.query(`INSERT INTO company (id, code, name_en) VALUES ($1, 'OTHER', 'Other Co')`, [OTHER]);
});
afterAll(async () => { await db.end(); });

async function punch(employeeId: string | null, localDate: string, hhmm: string, pin = '1', source = 'zkteco') {
  const at = new Date(localTimeMs(localDate, hhmm));
  await db.query(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
     VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', 'fingerprint', $7, $8)`,
    [DEMO.deviceId, pin, employeeId, at, `${localDate} ${hhmm}:00`, localDateOf(at.getTime()), source, `qa-${pin}-${localDate}-${hhmm}-${source}`]);
}

describe('tenancy', () => {
  it('HR of another company cannot read, recompute or map employees of this company', async () => {
    const other = await createUser(db, { companyId: OTHER, email: 'hr@other.local', name: 'o', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
    const login = await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.local', password: 'correct-horse-battery-staple' });
    const tok = login.body.token as string;
    expect(other.companyId).toBe(OTHER);
    // Another tenant's employee is indistinguishable from a missing one (404), so ids cannot be enumerated.
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}`).set(auth(tok))).status).toBe(404);
    expect((await request(app).get('/api/v1/attendance/days').set(auth(tok)).query({ employeeId: DEMO.sita, from: '2026-09-01', to: '2026-09-30' })).status).toBe(404);
    expect((await request(app).post('/api/v1/attendance/recompute').set(auth(tok)).send({ employeeId: DEMO.sita, localDate: '2026-09-21' })).status).toBe(404);
    expect((await request(app).post('/api/v1/attendance/recompute-range').set(auth(tok)).send({ employeeIds: [DEMO.sita], from: '2026-09-01', to: '2026-09-02' })).status).toBe(403);
    expect((await request(app).get(`/api/v1/devices/${DEMO.deviceId}/mappings`).set(auth(tok))).body.mappings).toEqual([]);
    expect((await request(app).get(`/api/v1/devices/${DEMO.deviceId}/commands`).set(auth(tok))).body.commands).toEqual([]);
    expect((await request(app).post(`/api/v1/devices/${DEMO.deviceId}/activate`).set(auth(tok)).send({})).status).toBe(404);
    expect((await request(app).get('/api/v1/reports/punch-audit').set(auth(tok)).query({ employeeId: DEMO.sita, from: '2026-09-01', to: '2026-09-30' })).status).toBe(404);
  });
  it('references to another company\'s shift, branch or manager are rejected on create and patch', async () => {
    const shift = await db.query<{ id: string }>(`INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES ($1, 'X', 'x', '09:00', '17:00') RETURNING id`, [OTHER]);
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'T1', nameEn: 'T', shiftId: shift.rows[0]!.id });
    expect(c.status).toBe(400);
    expect(c.body.error).toMatch(/shift does not exist/);
    const p = await request(app).patch(`/api/v1/employees/${DEMO.sita}`).set(auth(hr)).send({ managerId: DEMO.sita });
    expect(p.status).toBe(400);
    expect(p.body.error).toMatch(/own manager/);
  });
});

describe('manager scoping', () => {
  it('a manager sees direct reports only; other employees are forbidden; other tenants are not found', async () => {
    const { token: mgr } = await loginAs(db, app, ['manager'], DEMO.ram);
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}`).set(auth(mgr))).status).toBe(403);
    await db.query(`UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1 AND upper_inf(valid)`, [DEMO.sita, DEMO.ram]);
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}`).set(auth(mgr))).status).toBe(200);
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}/audit`).set(auth(mgr))).status).toBe(200);
    expect((await request(app).get('/api/v1/attendance/days').set(auth(mgr)).query({ employeeId: DEMO.sita, from: '2026-09-01', to: '2026-09-02' })).status).toBe(200);
    const ghost = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'O1', 'Outsider') RETURNING id`, [OTHER]);
    expect((await request(app).get(`/api/v1/employees/${ghost.rows[0]!.id}`).set(auth(mgr))).status).toBe(404);
    await db.query(`UPDATE employee_assignment SET manager_id = NULL WHERE employee_id = $1`, [DEMO.sita]);
  });
});

describe('auth hardening', () => {
  it('the last active admin cannot be disabled or demoted; nobody can disable themselves', async () => {
    expect((await request(app).patch(`/api/v1/auth/users/${adminId}`).set(auth(admin)).send({ status: 'disabled' })).status).toBe(400);
    const { userId: secondAdmin } = await loginAs(db, app, ['admin']);
    const ok = await request(app).patch(`/api/v1/auth/users/${secondAdmin}`).set(auth(admin)).send({ roles: ['hr'] });
    expect(ok.status).toBe(200);
    const again = await request(app).patch(`/api/v1/auth/users/${adminId}`).set(auth(admin)).send({ roles: ['hr'] });
    expect(again.status).toBe(400);
    expect(again.body.error).toMatch(/own admin role/);
  });
  it('a user flagged must_change_password can only use /auth until the password is changed', async () => {
    const created = await request(app).post('/api/v1/auth/users').set(auth(admin)).send({ email: 'temp@test.local', name: 't', password: 'temporary-passphrase-1', roles: ['hr'] });
    expect(created.status).toBe(201);
    const login = await request(app).post('/api/v1/auth/login').send({ email: 'temp@test.local', password: 'temporary-passphrase-1' });
    const tok = login.body.token as string;
    expect(login.body.user.mustChangePassword).toBe(true);
    const blocked = await request(app).get('/api/v1/employees').set(auth(tok));
    expect(blocked.status).toBe(403);
    expect(blocked.body.error).toMatch(/password change required/);
    expect((await request(app).post('/api/v1/auth/change-password').set(auth(tok)).send({ currentPassword: 'temporary-passphrase-1', newPassword: 'my-own-long-passphrase' })).status).toBe(200);
    const relogin = await request(app).post('/api/v1/auth/login').send({ email: 'temp@test.local', password: 'my-own-long-passphrase' });
    expect((await request(app).get('/api/v1/employees').set(auth(relogin.body.token))).status).toBe(200);
  });
  it('users of one company are invisible to another company\'s admin', async () => {
    const o = await createUser(db, { companyId: OTHER, email: 'admin@other.local', name: 'o', password: 'correct-horse-battery-staple', roles: ['admin'], pepper: TEST_PEPPER });
    const login = await request(app).post('/api/v1/auth/login').send({ email: 'admin@other.local', password: 'correct-horse-battery-staple' });
    expect((await request(app).patch(`/api/v1/auth/users/${adminId}`).set(auth(login.body.token)).send({ status: 'disabled' })).status).toBe(404);
    expect(o.roles).toEqual(['admin']);
  });
  it('unknown API routes answer JSON 404 and bodies over the limit answer 413', async () => {
    const r = await request(app).get('/api/v1/nothing-here').set(auth(hr));
    expect(r.status).toBe(404);
    expect(r.body).toEqual({ error: 'not found' });
    const big = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'B', nameEn: 'x'.repeat(3 * 1024 * 1024) });
    expect(big.status).toBe(413);
  });
});

describe('employees', () => {
  it('PATCH changes only the fields sent', async () => {
    await db.query(`UPDATE employee SET ot_eligible = false, name_ne = 'पुरानो' WHERE id = $1`, [DEMO.ram]);
    const r = await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ nameNe: 'राम' });
    expect(r.status).toBe(200);
    expect(r.body.employee).toMatchObject({ name_ne: 'राम', ot_eligible: false, weekly_off_days: [6], regime: 'ssf' });
  });
  it('exit date before join date is rejected; exit sets status exited', async () => {
    expect((await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ joinDate: '2026-03-01', exitDate: '2026-02-01' })).status).toBe(400);
    await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ joinDate: '2026-01-01' });
    expect((await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ exitDate: '2025-12-31' })).status).toBe(400);
    const ok = await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ exitDate: '2026-12-31' });
    expect(ok.body.employee.status).toBe('exited');
    await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ exitDate: null });
  });
  it('PAN lookup uses the blind index and is audited; employees cannot use it', async () => {
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'P1', nameEn: 'Pan Person', pan: '987654321' });
    expect(c.status).toBe(201);
    const found = await request(app).get('/api/v1/employees').set(auth(hr)).query({ pan: '987654321' });
    expect(found.body.employees.map((e: { emp_code: string }) => e.emp_code)).toEqual(['P1']);
    const none = await request(app).get('/api/v1/employees').set(auth(hr)).query({ pan: '111111111' });
    expect(none.body.employees).toEqual([]);
    expect((await db.query(`SELECT count(*)::int AS n FROM audit_log WHERE action = 'pan_lookup'`)).rows[0].n).toBe(2);
    expect((await request(app).get('/api/v1/employees').set(auth(emp)).query({ pan: '987654321' })).status).toBe(403);
  });
  it('CSV import strips a BOM, accepts quoted commas and CRLF, and caps at 5,000 rows', async () => {
    const csv = '﻿emp_code,name_en,name_ne\r\nC1,"Lama, Dorje",दोर्जे\r\n';
    const r = await request(app).post('/api/v1/employees/import').set(auth(hr)).set('Content-Type', 'text/csv').send(csv);
    expect(r.status).toBe(201);
    expect(r.body.imported).toBe(1);
    const row = await db.query(`SELECT name_en, name_ne FROM employee WHERE emp_code = 'C1'`);
    expect(row.rows[0]).toEqual({ name_en: 'Lama, Dorje', name_ne: 'दोर्जे' });
    const huge = 'emp_code,name_en\n' + Array.from({ length: 5001 }, (_, i) => `H${i},x`).join('\n');
    expect((await request(app).post('/api/v1/employees/import?dryRun=true').set(auth(hr)).set('Content-Type', 'text/csv').send(huge)).status).toBe(400);
  });
  it('self GET never returns PAN or bank account, only last 4 digits and a has_pan flag', async () => {
    await request(app).patch(`/api/v1/employees/${DEMO.sita}`).set(auth(hr)).send({ pan: '123123123', bankAccount: '5555666677778888' });
    const me = await request(app).get(`/api/v1/employees/${DEMO.sita}`).set(auth(emp));
    expect(me.status).toBe(200);
    expect(JSON.stringify(me.body)).not.toMatch(/123123123|5555666677778888|pan_enc|bank_account_enc/);
    expect(me.body.employee).toMatchObject({ has_pan: true, bank_account_last4: '8888' });
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}/sensitive`).set(auth(emp))).status).toBe(403);
  });
});

describe('devices', () => {
  it('mapping from a date after the punches leaves them unmapped; superseding rows get distinct keys', async () => {
    await punch(null, '2026-09-10', '10:00', '9');
    await punch(null, '2026-09-10', '18:00', '9');
    const later = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '9', employeeId: DEMO.sita, from: '2026-09-11' });
    expect(later.body.reattributed).toBe(0);
    const earlier = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '9', employeeId: DEMO.sita, from: '2026-09-01' });
    expect(earlier.body).toMatchObject({ reattributed: 1, dates: ['2026-09-10'] });
    const keys = await db.query<{ n: number; distinct: number }>(`SELECT count(*)::int AS n, count(DISTINCT idempotency_key)::int AS distinct FROM punch_event WHERE device_user_pin = '9'`);
    expect(keys.rows[0]).toEqual({ n: 4, distinct: 4 });
    const unmapped = await request(app).get('/api/v1/devices/punches/unmapped').set(auth(hr));
    expect(unmapped.body.punches.filter((p: { device_user_pin: string }) => p.device_user_pin === '9')).toHaveLength(0);
  });
  it('terminal user names in commands never contain control characters', async () => {
    const e = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'CR1', nameEn: 'Bad\r\nName\tHere' });
    const map = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '77', employeeId: e.body.id, from: '2026-09-01' });
    expect(map.status).toBe(201);
    const cmd = await db.query<{ command: string }>(`SELECT command FROM device_command WHERE command LIKE 'DATA UPDATE USERINFO PIN=77%'`);
    expect(cmd.rows[0]!.command).toBe('DATA UPDATE USERINFO PIN=77\tName=Bad Name Here\tPri=0');
  });
  it('only allow-listed commands can be queued and pseudo devices cannot be disabled', async () => {
    expect((await request(app).post(`/api/v1/devices/${DEMO.deviceId}/commands`).set(auth(hr)).send({ command: 'CLEAR DATA' })).status).toBe(400);
    expect((await request(app).post(`/api/v1/devices/${DEMO.deviceId}/commands`).set(auth(hr)).send({ command: 'CHECK' })).status).toBe(201);
    const manual = await db.query<{ id: string }>(`INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES ($1, 'MANUAL-QA', 'manual', 'manual', 'active') RETURNING id`, [DEMO.companyId]);
    expect((await request(app).post(`/api/v1/devices/${manual.rows[0]!.id}/disable`).set(auth(hr))).status).toBe(404);
  });
});

describe('attendance routes', () => {
  it('exceptions queue matches flags by JSON containment and includes auto clock-outs', async () => {
    await punch(DEMO.sita, '2026-09-29', '10:00');
    await punch(DEMO.sita, '2026-09-29', '18:00', '1', 'auto_clock_out');
    await request(app).post('/api/v1/attendance/recompute').set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-09-29' });
    const ex = await request(app).get('/api/v1/attendance/exceptions').set(auth(hr)).query({ from: '2026-09-29', to: '2026-09-29' });
    expect(ex.body.exceptions.map((e: { employee_id: string; status: string }) => [e.employee_id, e.status])).toContainEqual([DEMO.sita, 'Present']);
    expect(ex.body.exceptions[0].flags).toContainEqual({ kind: 'AutoClockOut' });
  });
  it('recompute-range queues employee × date pairs in one statement and rejects empty ranges', async () => {
    const r = await request(app).post('/api/v1/attendance/recompute-range').set(auth(hr)).send({ employeeIds: [DEMO.sita, DEMO.ram], from: '2026-09-01', to: '2026-09-03' });
    expect(r.body.queued).toBe(6);
    expect((await db.query(`SELECT count(*)::int AS n FROM recompute_queue`)).rows[0].n).toBe(6);
    expect((await request(app).post('/api/v1/attendance/recompute-range').set(auth(hr)).send({ employeeIds: [DEMO.sita], from: '2026-09-03', to: '2026-09-01' })).status).toBe(400);
    await db.query('DELETE FROM recompute_queue');
  });
});

describe('half-day regularisation', () => {
  it('an approved half_day regularisation counts as a paid half day for the attendance engine', async () => {
    await punch(DEMO.sita, '2026-09-30', '10:00');
    await punch(DEMO.sita, '2026-09-30', '14:00');
    await db.query(`INSERT INTO regularisation_request (employee_id, local_date, kind, reason, status, decided_at) VALUES ($1, '2026-09-30', 'half_day', 'doctor', 'approved', now())`, [DEMO.sita]);
    const r = await request(app).post('/api/v1/attendance/recompute').set(auth(hr)).send({ employeeId: DEMO.sita, localDate: '2026-09-30' });
    expect(r.body.day.status).toBe('HalfDayLeave');
    expect(r.body.day.flags).toContainEqual({ kind: 'HalfDayLeave', half: 'second_half' });
    expect(r.body.day.flags).not.toContainEqual({ kind: 'UnpaidLeave' });
  });
  it('SQL current_date follows Nepal time (session time zone)', async () => {
    const r = await db.query<{ tz: string }>(`SELECT current_setting('TimeZone') AS tz`);
    expect(r.rows[0]!.tz).toBe('Asia/Kathmandu');
  });
});

describe('worker resilience', () => {
  it('a row that keeps failing is retried with backoff and dropped after the limit with an HR notification', async () => {
    const logger = pino({ level: 'silent' });
    const ghost = '00000000-0000-7000-8000-00000000dead';
    await db.query(`ALTER TABLE recompute_queue DROP CONSTRAINT recompute_queue_employee_id_fkey`);
    await enqueueRecompute(db, [{ employeeId: ghost, localDate: '2026-09-01' }], 'test');
    let dropped = 0;
    for (let i = 0; i < MAX_RECOMPUTE_ATTEMPTS + 1; i++) {
      await db.query(`UPDATE recompute_queue SET enqueued_at = now() - interval '1 hour'`);
      const r = await drainRecomputeQueue({ db, logger, cipher: testCipher });
      dropped += r.dropped;
    }
    expect(dropped).toBe(1);
    expect((await db.query(`SELECT count(*)::int AS n FROM recompute_queue`)).rows[0].n).toBe(0);
    await db.query(`ALTER TABLE recompute_queue ADD CONSTRAINT recompute_queue_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES employee(id)`);
  });
});

describe('reports', () => {
  it('neutralises spreadsheet formula injection in CSV cells', () => {
    const csv = toCsv([{ key: 'a', header: 'A' }], [{ a: '=HYPERLINK("http://evil")' }, { a: '+1' }, { a: '-2' }, { a: '@x' }, { a: 'safe' }]);
    expect(csv).toContain(`"'=HYPERLINK(""http://evil"")"`);
    expect(csv).toContain(`'+1`);
    expect(csv).toContain(`'-2`);
    expect(csv).toContain(`'@x`);
    expect(csv).toContain('\r\nsafe\r\n');
  });
});
