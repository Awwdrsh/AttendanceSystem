import pg from 'pg';
import type { NormalizedPunch } from '../punch/event.js';
import { assertAllowedCommand, NEAR_DUPLICATE_WINDOW_MS, type DeviceRecord, type DeviceTouch, type InsertOutcome, type Store } from './types.js';

interface DeviceRow { id: string; serial_no: string; vendor: string; status: DeviceRecord['status']; tz_offset_min: number; attlog_stamp: string; oplog_stamp: string }

const toRec = (r: DeviceRow): DeviceRecord => ({
  id: r.id, serialNo: r.serial_no, vendor: r.vendor, status: r.status, tzOffsetMin: r.tz_offset_min, attlogStamp: r.attlog_stamp, oplogStamp: r.oplog_stamp,
});

const COLS = 'id, serial_no, vendor, status, tz_offset_min, attlog_stamp, oplog_stamp';

/** Shares the schema owned by apps/api (migrations/0001_init.sql). */
export class PostgresStore implements Store {
  readonly pool: pg.Pool;
  constructor(connectionString: string) { this.pool = new pg.Pool({ connectionString, max: 10 }); }

  async getDevice(serialNo: string) {
    const r = await this.pool.query<DeviceRow>(`SELECT ${COLS} FROM device WHERE serial_no = $1`, [serialNo]);
    return r.rows[0] ? toRec(r.rows[0]) : null;
  }

  async registerPending(serialNo: string, info: { vendor: string; protocol: string; pushVersion?: string }) {
    const r = await this.pool.query<DeviceRow>(
      `INSERT INTO device (serial_no, vendor, protocol, status, push_version) VALUES ($1, $2, $3, 'pending', $4)
       ON CONFLICT (serial_no) DO UPDATE SET last_seen_at = now() RETURNING ${COLS}`,
      [serialNo, info.vendor, info.protocol, info.pushVersion ?? null]);
    return toRec(r.rows[0]!);
  }

  async touch(serialNo: string, p: DeviceTouch) {
    await this.pool.query(
      `UPDATE device SET last_seen_at = coalesce($2, last_seen_at), last_handshake_at = coalesce($3, last_handshake_at),
         push_version = coalesce($4, push_version), fw_version = coalesce($5, fw_version),
         attlog_stamp = coalesce($6, attlog_stamp), oplog_stamp = coalesce($7, oplog_stamp)
       WHERE serial_no = $1`,
      [serialNo, p.lastSeenAt ?? null, p.lastHandshakeAt ?? null, p.pushVersion ?? null, p.fwVersion ?? null, p.attlogStamp ?? null, p.oplogStamp ?? null]);
  }

  async resolveEmployee(deviceId: string, pin: string, localDate: string) {
    const r = await this.pool.query<{ employee_id: string }>(
      'SELECT employee_id FROM device_user_map WHERE device_id = $1 AND device_user_pin = $2 AND valid @> $3::date', [deviceId, pin, localDate]);
    return r.rows[0]?.employee_id ?? null;
  }

  async insertPunch(p: NormalizedPunch): Promise<InsertOutcome> {
    const near = await this.pool.query(
      `SELECT 1 FROM punch_event WHERE device_id = $1 AND device_user_pin = $2 AND verify_mode = $3
         AND punched_at BETWEEN $4::timestamptz - $5::interval AND $4::timestamptz + $5::interval AND idempotency_key <> $6 LIMIT 1`,
      [p.deviceId, p.deviceUserPin, p.verifyMode, p.punchedAt, `${NEAR_DUPLICATE_WINDOW_MS} milliseconds`, p.idempotencyKey]);
    if (near.rows.length > 0) return 'near_duplicate';
    const r = await this.pool.query(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
         direction, verify_mode, source, raw, gateway_signature, idempotency_key)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
       ON CONFLICT (idempotency_key, punched_at) DO NOTHING`,
      [p.deviceId, p.deviceSn, p.vendor, p.deviceUserPin, p.employeeId, p.punchedAt, p.deviceLocalTime, p.localDate,
        p.direction, p.verifyMode, p.source, JSON.stringify(p.raw), p.gatewaySignature, p.idempotencyKey]);
    return (r.rowCount ?? 0) > 0 ? 'inserted' : 'duplicate';
  }

  async enqueueCommand(deviceId: string, command: string) {
    assertAllowedCommand(command);
    const r = await this.pool.query<{ id: string }>('INSERT INTO device_command (device_id, command) VALUES ($1, $2) RETURNING id', [deviceId, command]);
    return Number(r.rows[0]!.id);
  }

  async popQueuedCommands(deviceId: string) {
    const r = await this.pool.query<{ id: string; command: string }>(
      `UPDATE device_command SET status = 'sent', sent_at = now()
        WHERE id IN (SELECT id FROM device_command WHERE device_id = $1 AND status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED)
        RETURNING id, command`, [deviceId]);
    return r.rows.map((c) => ({ id: Number(c.id), command: c.command }));
  }

  async ackCommand(id: number, returnCode: number) {
    await this.pool.query(
      `UPDATE device_command SET status = CASE WHEN $2 >= 0 THEN 'acked' ELSE 'failed' END, return_code = $2, acked_at = now() WHERE id = $1`,
      [id, returnCode]);
  }

  async end() { await this.pool.end(); }
}
