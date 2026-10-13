import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import { createApp } from '../src/app.js';
import type { Db } from '../src/db/pool.js';
import { DEMO, freshDb } from './setup-db.js';

let db: Db;
beforeAll(async () => { db = await freshDb(); });
afterAll(async () => { await db.end(); });

async function insertPunch(employeeId: string, localDate: string, hhmm: string, pin = '1', verify = 'fingerprint') {
  const at = new Date(localTimeMs(localDate, hhmm));
  const deviceLocal = `${localDate} ${hhmm}:00`;
  await db.query(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
       direction, verify_mode, source, idempotency_key)
     VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', $7, 'zkteco', $8)`,
    [DEMO.deviceId, pin, employeeId, at, deviceLocal, localDateOf(at.getTime()), verify, `k-${employeeId}-${deviceLocal}-${verify}`]);
}

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
    const rows = (await db.query<{ prev_hash: string; row_hash: string }>('SELECT prev_hash, row_hash FROM audit_log ORDER BY id')).rows;
    expect(rows[0]!.prev_hash).toBe('0'.repeat(64));
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
    const app = createApp(db);
    await insertPunch(DEMO.sita, '2026-09-21', '10:17');
    await insertPunch(DEMO.sita, '2026-09-21', '18:40');

    const first = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.sita, localDate: '2026-09-21' });
    expect(first.status).toBe(200);
    expect(first.body.changed).toBe(true);
    expect(first.body.version).toBe(1);
    expect(first.body.day).toMatchObject({ status: 'Present', workedMin: 503, lateMin: 2, flags: [{ kind: 'Late', minutes: 2 }] });

    // Same inputs -> same hash -> no new version.
    const again = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.sita, localDate: '2026-09-21' });
    expect(again.body.changed).toBe(false);
    expect(again.body.version).toBe(1);

    // A late-arriving punch changes the inputs -> version 2.
    await insertPunch(DEMO.sita, '2026-09-21', '19:00');
    const third = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.sita, localDate: '2026-09-21' });
    expect(third.body.version).toBe(2);
    expect(third.body.day.status).toBe('Present');
    expect(third.body.day.flags).toContainEqual({ kind: 'UnpairedPunch', punchId: expect.any(String) });

    const days = await request(app).get('/api/v1/attendance/days').query({ employeeId: DEMO.sita, from: '2026-09-21', to: '2026-09-21' });
    expect(days.body.days).toHaveLength(1);
    expect(days.body.days[0]).toMatchObject({ version: 2, bs_date: '2083-06-05', status: 'Present' });
  });

  it('Saturday is WeeklyOff and a holiday wins over punches', async () => {
    const app = createApp(db);
    const sat = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.sita, localDate: '2026-09-26' });
    expect(sat.body.day.status).toBe('WeeklyOff');

    await db.query(`INSERT INTO holiday (company_id, local_date, name_en) VALUES ($1, '2026-10-19', 'Dashain')`, [DEMO.companyId]);
    await insertPunch(DEMO.sita, '2026-10-19', '10:00');
    await insertPunch(DEMO.sita, '2026-10-19', '14:00');
    const hol = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.sita, localDate: '2026-10-19' });
    expect(hol.body.day.status).toBe('Holiday');
    expect(hol.body.day.flags).toContainEqual({ kind: 'HolidayWork', workedMin: 240 });
  });

  it('a locked day is never rewritten', async () => {
    const app = createApp(db);
    await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.ram, localDate: '2026-09-14' });
    await db.query(`UPDATE attendance_day SET locked_at = now() WHERE employee_id = $1 AND local_date = '2026-09-14'`, [DEMO.ram]);
    await insertPunch(DEMO.ram, '2026-09-14', '18:00', '2');
    const r = await request(app).post('/api/v1/attendance/recompute').send({ employeeId: DEMO.ram, localDate: '2026-09-14' });
    expect(r.body.locked).toBe(true);
    expect(r.body.changed).toBe(false);
  });

  it('validates input and 404s unknown employees', async () => {
    const app = createApp(db);
    expect((await request(app).post('/api/v1/attendance/recompute').send({ employeeId: 'nope', localDate: '2026-09-21' })).status).toBe(400);
    expect((await request(app).post('/api/v1/attendance/recompute')
      .send({ employeeId: '00000000-0000-7000-8000-0000000000ff', localDate: '2026-09-21' })).status).toBe(404);
  });
});
