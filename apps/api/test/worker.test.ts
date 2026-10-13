import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import pino from 'pino';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { enqueueRecompute } from '../src/modules/attendance/service.js';
import { deliverQueued, inQuietHours, notify, quietHoursEnd, stripAmounts, type OutboxRow, type Sender } from '../src/modules/notifications/service.js';
import { autoClockOut, detectOfflineDevices, runOnce, slaReminders, type WorkerDeps } from '../src/worker.js';
import { DEMO, freshDb, loginAs, testApp, testCipher } from './setup-db.js';

let db: Db; let deps: WorkerDeps; let hrUser: string;
beforeAll(async () => {
  db = await freshDb();
  const app = testApp(db);
  hrUser = (await loginAs(db, app, ['hr'])).userId;
  deps = { db, logger: pino({ level: 'silent' }), cipher: testCipher, senders: [] };
});
afterAll(async () => { await db.end(); });

async function punch(employeeId: string, localDate: string, hhmm: string, pin: string) {
  const at = new Date(localTimeMs(localDate, hhmm));
  await db.query(`INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
    VALUES ($1, 'DEMO-01', 'zkteco', $2, $3, $4, $5, $6, 'unknown', 'fingerprint', 'zkteco', $7)`,
    [DEMO.deviceId, pin, employeeId, at, `${localDate} ${hhmm}:00`, localDateOf(at.getTime()), `w-${pin}-${localDate}-${hhmm}`]);
}

describe('worker', () => {
  it('runOnce drains the recompute queue and computes days', async () => {
    await punch(DEMO.sita, '2026-09-21', '10:00', '1');
    await punch(DEMO.sita, '2026-09-21', '18:00', '1');
    await enqueueRecompute(db, [{ employeeId: DEMO.sita, localDate: '2026-09-21' }, { employeeId: DEMO.ram, localDate: '2026-09-21' }], 'test');
    const out = await runOnce(deps, ['queue']);
    expect(out.queue).toEqual({ processed: 2, failed: 0, dropped: 0 });
    expect((await db.query('SELECT count(*)::int AS n FROM recompute_queue')).rows[0].n).toBe(0);
    const day = await db.query(`SELECT status, worked_min FROM v_attendance_latest WHERE employee_id = $1 AND local_date = '2026-09-21'`, [DEMO.sita]);
    expect(day.rows[0]).toMatchObject({ status: 'Present', worked_min: 480 });
  });

  it('nightly sweep queues the last 3 days for every active employee', async () => {
    const out = await runOnce(deps, ['nightly']);
    expect(out.nightly).toBe(2 * 3);
    expect((await db.query('SELECT count(*)::int AS n FROM recompute_queue')).rows[0].n).toBe(6);
    await runOnce(deps, ['queue']);
  });

  it('device offline: notifies HR once, escalates at 15 min, recovers, and dedupes', async () => {
    const now = new Date();
    await db.query(`UPDATE device SET last_seen_at = $2 WHERE id = $1`, [DEMO.deviceId, new Date(now.getTime() - 5 * 60_000)]);
    expect(await detectOfflineDevices(deps, now)).toEqual({ offline: 1, escalated: 0, recovered: 0 });
    expect(await detectOfflineDevices(deps, new Date(now.getTime() + 60_000))).toEqual({ offline: 0, escalated: 0, recovered: 0 });
    const n1 = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'device.offline' AND channel = 'in_app' AND user_id = $1`, [hrUser]);
    expect(n1.rows[0].n).toBe(1);
    expect(await detectOfflineDevices(deps, new Date(now.getTime() + 12 * 60_000))).toEqual({ offline: 0, escalated: 1, recovered: 0 });
    expect(await detectOfflineDevices(deps, new Date(now.getTime() + 13 * 60_000))).toEqual({ offline: 0, escalated: 0, recovered: 0 });
    await db.query(`UPDATE device SET last_seen_at = $2 WHERE id = $1`, [DEMO.deviceId, new Date(now.getTime() + 14 * 60_000)]);
    expect(await detectOfflineDevices(deps, new Date(now.getTime() + 14 * 60_000 + 10_000))).toEqual({ offline: 0, escalated: 0, recovered: 1 });
    expect(await detectOfflineDevices(deps, new Date(now.getTime() + 14 * 60_000 + 20_000))).toEqual({ offline: 0, escalated: 0, recovered: 0 });
  });

  it('delivers queued notifications through senders with retry backoff and quiet-hour deferral', async () => {
    const sent: string[] = [];
    let fail = true;
    const senders: Sender[] = [
      { channel: 'email', send: async (r: OutboxRow) => { sent.push(r.title); } },
      { channel: 'sms', send: async () => { if (fail) throw new Error('gateway down'); } },
    ];
    await notify(db, { companyId: DEMO.companyId, userId: hrUser, template: 't', title: 'Hello', body: 'Net pay NPR 45,000 ready', channels: ['email', 'sms'] });
    const day = new Date('2026-09-21T06:00:00Z'); // 11:45 NPT
    await db.query(`UPDATE notification_outbox SET next_attempt_at = $1 WHERE title = 'Hello'`, [day]);
    const r1 = await deliverQueued(db, senders, day);
    expect(r1).toEqual({ sent: 1, failed: 1, deferred: 0 });
    expect(sent).toEqual(['Hello']);
    const smsRow = await db.query<{ status: string; attempts: number; body: string; next_attempt_at: Date }>(`SELECT status, attempts, body, next_attempt_at FROM notification_outbox WHERE channel = 'sms' AND title = 'Hello'`);
    expect(smsRow.rows[0]).toMatchObject({ status: 'queued', attempts: 1 });
    expect(smsRow.rows[0]!.body).not.toContain('45,000');
    expect(smsRow.rows[0]!.next_attempt_at.getTime()).toBe(day.getTime() + 60_000);
    // Not due yet
    expect(await deliverQueued(db, senders, new Date(day.getTime() + 30_000))).toEqual({ sent: 0, failed: 0, deferred: 0 });
    // Due but inside quiet hours (22:00 NPT = 16:15Z) -> deferred to 07:00 NPT
    const night = new Date('2026-09-21T16:15:00Z');
    expect(await deliverQueued(db, senders, night)).toEqual({ sent: 0, failed: 0, deferred: 1 });
    const deferred = await db.query<{ next_attempt_at: Date }>(`SELECT next_attempt_at FROM notification_outbox WHERE channel = 'sms' AND title = 'Hello'`);
    expect(deferred.rows[0]!.next_attempt_at.toISOString()).toBe('2026-09-22T01:15:00.000Z');
    fail = false;
    expect(await deliverQueued(db, senders, new Date('2026-09-22T02:00:00Z'))).toEqual({ sent: 1, failed: 0, deferred: 0 });
  });

  it('quiet-hours helpers and amount stripping', () => {
    expect(inQuietHours(new Date('2026-09-21T16:15:00Z'))).toBe(true);   // 22:00 NPT
    expect(inQuietHours(new Date('2026-09-21T06:00:00Z'))).toBe(false);  // 11:45 NPT
    expect(quietHoursEnd(new Date('2026-09-21T16:15:00Z')).toISOString()).toBe('2026-09-22T01:15:00.000Z');
    expect(stripAmounts('Net Rs. 45,000.50 and 123456 paid')).toBe('Net [amount] and [amount] paid');
  });

  it('SLA reminders go to the approver after 24 h and HR after 48 h, once per day', async () => {
    const created = new Date(Date.now() - 50 * 3_600_000);
    await db.query(`INSERT INTO workflow_instance (company_id, object_type, object_id, employee_id, steps, created_at) VALUES ($1, 'leave_request', uuidv7(), $2, '[{"role":"manager"}]', $3)`, [DEMO.companyId, DEMO.sita, created]);
    expect(await slaReminders(deps)).toEqual({ reminded: 1, escalated: 1 });
    expect(await slaReminders(deps)).toEqual({ reminded: 0, escalated: 0 });
    const hrRows = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'workflow.escalation' AND channel = 'in_app' AND user_id = $1`, [hrUser]);
    expect(hrRows.rows[0].n).toBe(1);
  });

  it('auto clock-out inserts an OUT at shift end once the policy delay has passed, then recompute closes the day', async () => {
    await db.query(`UPDATE company_setting SET value = value || '{"autoClockOutAfterMin": 60}' WHERE company_id = $1 AND key = 'attendance.policy'`, [DEMO.companyId]);
    await punch(DEMO.ram, '2026-09-22', '10:05', '2');
    await runOnce({ ...deps }, ['queue']);
    await enqueueRecompute(db, [{ employeeId: DEMO.ram, localDate: '2026-09-22' }], 't'); await runOnce(deps, ['queue']);
    expect((await db.query(`SELECT status FROM v_attendance_latest WHERE employee_id = $1 AND local_date = '2026-09-22'`, [DEMO.ram])).rows[0].status).toBe('Incomplete');
    expect(await autoClockOut(deps, new Date(localTimeMs('2026-09-22', '18:30')))).toBe(0);
    expect(await autoClockOut(deps, new Date(localTimeMs('2026-09-22', '19:30')))).toBe(1);
    expect(await autoClockOut(deps, new Date(localTimeMs('2026-09-22', '20:30')))).toBe(0);
    await runOnce(deps, ['queue']);
    const day = await db.query(`SELECT status, worked_min FROM v_attendance_latest WHERE employee_id = $1 AND local_date = '2026-09-22'`, [DEMO.ram]);
    expect(day.rows[0]).toMatchObject({ status: 'Present', worked_min: 475 });
    const note = await db.query(`SELECT 1 FROM notification_outbox WHERE template = 'attendance.auto_clock_out' AND employee_id = $1 AND channel = 'in_app'`, [DEMO.ram]);
    expect(note.rows).toHaveLength(1);
  });

  it('accrual runs once per company for the previous BS month and is recorded', async () => {
    const r1 = await runOnce(deps, ['accrual'], new Date('2026-09-21T06:00:00Z'));
    expect(r1.accrual).toBe(1);
    const r2 = await runOnce(deps, ['accrual'], new Date('2026-09-21T06:00:00Z'));
    expect(r2.accrual).toBe(0);
    const s = await db.query<{ value: { month: string } }>(`SELECT value FROM company_setting WHERE key = 'leave.accrual.last'`);
    expect(s.rows[0]!.value.month).toBe('2083-05');
  });
});
