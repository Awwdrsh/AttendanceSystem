import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { otMinutesThisWeek } from '../src/modules/attendance/repo.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string;
beforeAll(async () => { db = await freshDb(); app = testApp(db); hr = (await loginAs(db, app, ['hr'])).token; });
afterAll(async () => { await db.end(); });

async function insertPunch(employeeId: string | null, localDate: string, hhmm: string, pin = '1', verify = 'fingerprint') {
  const at = new Date(localTimeMs(localDate, hhmm));
  const deviceLocal = `${localDate} ${hhmm}:00`;
  const r = await db.query<{ id: string }>(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
       direction, verify_mode, source, idempotency_key)
     VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', $7, 'zkteco', $8) RETURNING id`,
    [DEMO.deviceId, pin, employeeId, at, deviceLocal, localDateOf(at.getTime()), verify, `k-${pin}-${deviceLocal}-${verify}`]);
  return r.rows[0]!.id;
}
const recompute = (employeeId: string, localDate: string) =>
  request(app).post('/api/v1/attendance/recompute').set(auth(hr)).send({ employeeId, localDate });

describe('schema guarantees', () => {
  it('punch_event is append-only', async () => {
    await insertPunch(DEMO.ram, '2026-09-14', '10:00', '2');
    await expect(db.query(`UPDATE punch_event SET verify_mode = 'face' WHERE device_user_pin = '2'`)).rejects.toThrow(/append-only/);
    await expect(db.query(`DELETE FROM punch_event WHERE device_user_pin = '2'`)).rejects.toThrow(/append-only/);
  });
  it('rejects overlapping shift assignments (WITHOUT OVERLAPS)', async () => {
    await expect(db.query(
      `INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange('2026-06-01', '2026-07-01'))`,
      [DEMO.ram, DEMO.shiftId])).rejects.toThrow(/conflicting key value|overlap/i);
  });
  it('never queues a SHELL command', async () => {
    await expect(db.query(`INSERT INTO device_command (device_id, command) VALUES ($1, 'SHELL rm -rf /')`, [DEMO.deviceId]))
      .rejects.toThrow(/device_command_no_shell/);
  });
  it('audit_log is hash-chained and immutable', async () => {
    await db.query(`INSERT INTO audit_log (action, table_name, row_id, after) VALUES ('create', 'employee', 'x', '{"a":1}')`);
    await db.query(`INSERT INTO audit_log (action, table_name, row_id, after) VALUES ('update', 'employee', 'x', '{"a":2}')`);
    const rows = (await db.query<{ prev_hash: string; row_hash: string }>(`SELECT prev_hash, row_hash FROM audit_log WHERE row_id = 'x' ORDER BY id`)).rows;
    expect(rows[1]!.prev_hash).toBe(rows[0]!.row_hash);
    await expect(db.query(`UPDATE audit_log SET action = 'x'`)).rejects.toThrow(/append-only/);
  });
  it('bs_calendar is seeded', async () => {
    const r = await db.query<{ bs_date: string }>(`SELECT bs_date FROM bs_calendar WHERE ad_date = '2026-08-28'`);
    expect(r.rows[0]!.bs_date).toBe('2083-05-12');
  });
});

describe('POST /api/v1/attendance/recompute', () => {
  it('computes worked example A from stored punches and stores one version', async () => {
    await insertPunch(DEMO.sita, '2026-09-21', '10:17');
    await insertPunch(DEMO.sita, '2026-09-21', '18:40');
    const first = await recompute(DEMO.sita, '2026-09-21');
    expect(first.status).toBe(200);
    expect(first.body).toMatchObject({ changed: true, version: 1 });
    expect(first.body.day).toMatchObject({ status: 'Present', workedMin: 503, lateMin: 2, flags: [{ kind: 'Late', minutes: 2 }] });
    const again = await recompute(DEMO.sita, '2026-09-21');
    expect(again.body).toMatchObject({ changed: false, version: 1 });
    await insertPunch(DEMO.sita, '2026-09-21', '19:00');
    const third = await recompute(DEMO.sita, '2026-09-21');
    expect(third.body.version).toBe(2);
    expect(third.body.day.flags).toContainEqual({ kind: 'UnpairedPunch', punchId: expect.any(String) });
    const days = await request(app).get('/api/v1/attendance/days').set(auth(hr)).query({ employeeId: DEMO.sita, from: '2026-09-21', to: '2026-09-21' });
    expect(days.body.days).toHaveLength(1);
    expect(days.body.days[0]).toMatchObject({ version: 2, bs_date: '2083-06-05', status: 'Present' });
  });

  it('concurrent recomputes never collide on the version number', async () => {
    await insertPunch(DEMO.ram, '2026-09-15', '10:00', '2');
    await insertPunch(DEMO.ram, '2026-09-15', '18:00', '2');
    const results = await Promise.all(Array.from({ length: 6 }, () => recompute(DEMO.ram, '2026-09-15')));
    expect(results.every((r) => r.status === 200)).toBe(true);
    const versions = await db.query(`SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = $1 AND local_date = '2026-09-15'`, [DEMO.ram]);
    expect(versions.rows[0].n).toBe(1);
  });

  it('Saturday is WeeklyOff; a holiday wins over punches; approved leave wins over Absent', async () => {
    expect((await recompute(DEMO.sita, '2026-09-26')).body.day.status).toBe('WeeklyOff');
    await db.query(`INSERT INTO holiday (company_id, local_date, name_en) VALUES ($1, '2026-10-19', 'Dashain')`, [DEMO.companyId]);
    await insertPunch(DEMO.sita, '2026-10-19', '10:00');
    await insertPunch(DEMO.sita, '2026-10-19', '14:00');
    const hol = await recompute(DEMO.sita, '2026-10-19');
    expect(hol.body.day.status).toBe('Holiday');
    expect(hol.body.day.flags).toContainEqual({ kind: 'HolidayWork', workedMin: 240 });
    const lt = await db.query<{ id: string }>(`SELECT id FROM leave_type WHERE company_id = $1 AND code = 'LWP'`, [DEMO.companyId]);
    await db.query(`INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status, decided_at) VALUES ($1, $2, '2026-09-23', '2026-09-23', 1, 'approved', now())`, [DEMO.sita, lt.rows[0]!.id]);
    const leave = await recompute(DEMO.sita, '2026-09-23');
    expect(leave.body.day.status).toBe('OnLeave');
    expect(leave.body.day.flags).toContainEqual({ kind: 'UnpaidLeave' });
  });

  it('approved OT pre-approval produces a detected OT slip; removing OT deletes it', async () => {
    await db.query(`INSERT INTO ot_request (employee_id, local_date, requested_min, approved_min, status, decided_at) VALUES ($1, '2026-09-24', 120, 120, 'approved', now())`, [DEMO.sita]);
    await insertPunch(DEMO.sita, '2026-09-24', '10:00');
    await insertPunch(DEMO.sita, '2026-09-24', '19:30');
    const d = await recompute(DEMO.sita, '2026-09-24');
    expect(d.body.day.ot).toMatchObject({ minutes: 90, status: 'Detected' });
    const slip = await db.query(`SELECT detected_min, status FROM ot_slip WHERE employee_id = $1 AND local_date = '2026-09-24'`, [DEMO.sita]);
    expect(slip.rows[0]).toMatchObject({ detected_min: 90, status: 'detected' });
  });

  it('weekly OT cap counts Sunday to Saturday including Saturday', async () => {
    for (const [d, ot] of [['2026-09-13', 100], ['2026-09-19', 50], ['2026-09-20', 999], ['2026-09-12', 999]] as const) {
      await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, ot_min, inputs_hash) VALUES ($1, $2, 1, 'Present', $3, 'h')`, [DEMO.ram, d, ot]);
    }
    expect(await otMinutesThisWeek(db, DEMO.ram, '2026-09-16')).toBe(150);
  });

  it('a locked day is never rewritten', async () => {
    await recompute(DEMO.ram, '2026-09-14');
    await db.query(`UPDATE attendance_day SET locked_at = now() WHERE employee_id = $1 AND local_date = '2026-09-14'`, [DEMO.ram]);
    await insertPunch(DEMO.ram, '2026-09-14', '18:00', '2');
    const r = await recompute(DEMO.ram, '2026-09-14');
    expect(r.body).toMatchObject({ locked: true, changed: false });
  });

  it('validates input, 404s unknown employees, 400s malformed JSON', async () => {
    expect((await recompute('nope', '2026-09-21')).status).toBe(400);
    expect((await recompute('00000000-0000-7000-8000-0000000000ff', '2026-09-21')).status).toBe(404);
    const bad = await request(app).post('/api/v1/attendance/recompute').set(auth(hr)).set('Content-Type', 'application/json').send('{"broken"');
    expect(bad.status).toBe(400);
  });
});

describe('devices', () => {
  it('mapping a PIN re-attributes unmapped punches via superseding rows and recomputes', async () => {
    await insertPunch(null, '2026-09-28', '10:00', '7');
    await insertPunch(null, '2026-09-28', '18:00', '7');
    const un = await request(app).get('/api/v1/devices/punches/unmapped').set(auth(hr));
    expect(un.body.punches.filter((p: { device_user_pin: string }) => p.device_user_pin === '7')).toHaveLength(2);
    const map = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '7', employeeId: DEMO.ram, from: '2026-09-01' });
    expect(map.status).toBe(201);
    expect(map.body).toMatchObject({ reattributed: 1, dates: ['2026-09-28'] });
    const after = await request(app).get('/api/v1/devices/punches/unmapped').set(auth(hr));
    expect(after.body.punches.filter((p: { device_user_pin: string }) => p.device_user_pin === '7')).toHaveLength(0);
    const day = await request(app).get('/api/v1/attendance/days').set(auth(hr)).query({ employeeId: DEMO.ram, from: '2026-09-28', to: '2026-09-28' });
    expect(day.body.days[0]).toMatchObject({ status: 'Present', worked_min: 480 });
    const cmd = await db.query(`SELECT command FROM device_command WHERE device_id = $1 ORDER BY id DESC LIMIT 1`, [DEMO.deviceId]);
    expect(cmd.rows[0].command).toMatch(/^DATA UPDATE USERINFO PIN=7\t/);
  });
  it('overlapping mapping for a different employee needs confirmation, then is replaced from the new date', async () => {
    const refused = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '7', employeeId: DEMO.sita, from: '2026-10-01' });
    expect(refused.status).toBe(409);
    const map = await request(app).post(`/api/v1/devices/${DEMO.deviceId}/map`).set(auth(hr)).send({ pin: '7', employeeId: DEMO.sita, from: '2026-10-01', replace: true });
    expect(map.status).toBe(201);
    const rows = await db.query(`SELECT employee_id, valid::text FROM device_user_map WHERE device_id = $1 AND device_user_pin = '7' ORDER BY lower(valid)`, [DEMO.deviceId]);
    expect(rows.rows).toHaveLength(2);
    expect(rows.rows[0]).toMatchObject({ employee_id: DEMO.ram, valid: '[2026-09-01,2026-10-01)' });
  });
  it('employee create encrypts PAN and bank, duplicate code is 409, sensitive read is audited', async () => {
    const body = { empCode: 'E100', nameEn: 'Hari Lal', pan: '123456789', bankAccount: '00123456789012', regime: 'pf', joinDate: '2026-09-01', shiftId: DEMO.shiftId };
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send(body);
    expect(c.status).toBe(201);
    expect((await request(app).post('/api/v1/employees').set(auth(hr)).send(body)).status).toBe(409);
    const raw = await db.query<{ pan_enc: Buffer; bank_account_last4: string; pan_bidx: string }>('SELECT pan_enc, bank_account_last4, pan_bidx FROM employee WHERE id = $1', [c.body.id]);
    expect(raw.rows[0]!.pan_enc.toString('utf8')).not.toContain('123456789');
    expect(raw.rows[0]!.bank_account_last4).toBe('9012');
    const sens = await request(app).get(`/api/v1/employees/${c.body.id}/sensitive`).set(auth(hr));
    expect(sens.body).toEqual({ pan: '123456789', bankAccount: '00123456789012' });
    const a = await db.query(`SELECT 1 FROM audit_log WHERE action = 'read_sensitive' AND row_id = $1`, [c.body.id]);
    expect(a.rows).toHaveLength(1);
  });
  it('CSV import validates rows and reports errors by row number', async () => {
    const csv = 'emp_code,name_en,pan,join_date\nE200,Gita,223456789,2026-01-01\nE201,,12345,2026-13-40\n'; // PAN differs from E100's: duplicates are refused
    const dry = await request(app).post('/api/v1/employees/import?dryRun=true').set(auth(hr)).set('Content-Type', 'text/csv').send(csv);
    expect(dry.status).toBe(400);
    expect(dry.body.errors.map((e: { row: number }) => e.row)).toEqual([3, 3, 3]);
    const good = await request(app).post('/api/v1/employees/import').set(auth(hr)).set('Content-Type', 'text/csv').send('emp_code,name_en\nE300,Maya\n');
    expect(good.status).toBe(201);
    expect(good.body.imported).toBe(1);
  });
});
