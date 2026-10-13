import pg from 'pg';
import type { NormalizedPunch } from '../punch/event.js';
import {
  assertAllowedCommand, COMMAND_ACK_TIMEOUT_MS, COMMAND_MAX_ATTEMPTS, DEFAULT_MAX_PENDING, NEAR_DUPLICATE_WINDOW_MS,
  type DeviceRecord, type DeviceTouch, type InsertOutcome, type Store, type StoreOptions, type TemplateRecord,
} from './types.js';

interface DeviceRow { id: string; serial_no: string; vendor: string; protocol: string; status: DeviceRecord['status']; tz_offset_min: number; attlog_stamp: string; oplog_stamp: string; comm_key_hash: string | null }

const toRec = (r: DeviceRow): DeviceRecord => ({
  id: r.id, serialNo: r.serial_no, vendor: r.vendor, protocol: r.protocol, status: r.status, tzOffsetMin: r.tz_offset_min, attlogStamp: r.attlog_stamp, oplogStamp: r.oplog_stamp, commKeyHash: r.comm_key_hash,
});

const COLS = 'id, serial_no, vendor, protocol, status, tz_offset_min, attlog_stamp, oplog_stamp, comm_key_hash';

/** Shares the schema owned by apps/api (migrations/*.sql). */
export class PostgresStore implements Store {
  readonly pool: pg.Pool;
  constructor(connectionString: string, readonly opts: StoreOptions = {}) { this.pool = new pg.Pool({ connectionString, max: 10 }); }

  async getDevice(serialNo: string) {
    const r = await this.pool.query<DeviceRow>(`SELECT ${COLS} FROM device WHERE serial_no = $1`, [serialNo]);
    return r.rows[0] ? toRec(r.rows[0]) : null;
  }

  async registerPending(serialNo: string, info: { vendor: string; protocol: string; pushVersion?: string }) {
    const existing = await this.getDevice(serialNo);
    if (existing) return existing;
    const pending = await this.pool.query<{ n: string }>(`SELECT count(*)::text AS n FROM device WHERE status = 'pending'`);
    if (Number(pending.rows[0]!.n) >= (this.opts.maxPendingDevices ?? DEFAULT_MAX_PENDING)) return null;
    const r = await this.pool.query<DeviceRow>(
      `INSERT INTO device (serial_no, vendor, protocol, status, push_version, last_seen_at) VALUES ($1, $2, $3, 'pending', $4, now())
       ON CONFLICT (serial_no) DO UPDATE SET last_seen_at = now() RETURNING ${COLS}`,
      [serialNo, info.vendor, info.protocol, info.pushVersion ?? null]);
    return toRec(r.rows[0]!);
  }

  async touch(serialNo: string, p: DeviceTouch) {
    await this.pool.query(
      `UPDATE device SET last_seen_at = coalesce($2, last_seen_at), last_handshake_at = coalesce($3, last_handshake_at),
         push_version = coalesce($4, push_version), fw_version = coalesce($5, fw_version),
         attlog_stamp = coalesce($6, attlog_stamp), oplog_stamp = coalesce($7, oplog_stamp),
         clock_drift_sec = coalesce($8, clock_drift_sec)
       WHERE serial_no = $1`,
      [serialNo, p.lastSeenAt ?? null, p.lastHandshakeAt ?? null, p.pushVersion ?? null, p.fwVersion ?? null, p.attlogStamp ?? null, p.oplogStamp ?? null, p.clockDriftSec ?? null]);
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

  async enqueueRecompute(employeeId: string, localDate: string, reason: string) {
    await this.pool.query(
      `INSERT INTO recompute_queue (employee_id, local_date, reason) VALUES ($1, $2, $3)
       ON CONFLICT (employee_id, local_date) DO UPDATE SET enqueued_at = now(), reason = EXCLUDED.reason`, [employeeId, localDate, reason]);
  }

  async insertTemplate(t: TemplateRecord) {
    await this.pool.query(
      `INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
      [t.employeeId, t.deviceUserPin, t.modality, t.vendorFormat, t.ciphertext, t.keyVersion, t.aadHash, t.deviceId]);
  }

  async enqueueCommand(deviceId: string, command: string) {
    assertAllowedCommand(command, this.opts.allowDangerousCommands);
    const r = await this.pool.query<{ id: string }>('INSERT INTO device_command (device_id, command) VALUES ($1, $2) RETURNING id', [deviceId, command]);
    return Number(r.rows[0]!.id);
  }

  async popQueuedCommands(deviceId: string, now = new Date()) {
    // Resend policy: sent-but-unacknowledged commands older than the ack timeout go back to the queue once, then fail.
    await this.pool.query(
      `UPDATE device_command SET status = CASE WHEN attempts < $3::int THEN 'queued' ELSE 'failed' END
        WHERE device_id = $1 AND status = 'sent' AND sent_at < $2::timestamptz - make_interval(secs => $4::int)`,
      [deviceId, now, COMMAND_MAX_ATTEMPTS, COMMAND_ACK_TIMEOUT_MS / 1000]);
    const r = await this.pool.query<{ id: string; command: string }>(
      `UPDATE device_command SET status = 'sent', sent_at = $2::timestamptz, attempts = attempts + 1
        WHERE id IN (SELECT id FROM device_command WHERE device_id = $1 AND status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED)
        RETURNING id, command`, [deviceId, now]);
    return r.rows.map((c) => ({ id: Number(c.id), command: c.command }));
  }

  async ackCommand(id: number, returnCode: number) {
    await this.pool.query(
      `UPDATE device_command SET status = CASE WHEN $2::int >= 0 THEN 'acked' ELSE 'failed' END, return_code = $2::int, acked_at = now()
        WHERE id = $1 AND status = 'sent'`,
      [id, returnCode]);
  }

  async listDevices(filter: { status?: DeviceRecord['status']; protocol?: string } = {}) {
    const r = await this.pool.query<DeviceRow>(
      `SELECT ${COLS} FROM device WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR protocol = $2) ORDER BY serial_no`,
      [filter.status ?? null, filter.protocol ?? null]);
    return r.rows.map(toRec);
  }

  async countPunches(deviceId: string, localDate: string) {
    const r = await this.pool.query<{ n: string }>('SELECT count(*)::text AS n FROM punch_event WHERE device_id = $1 AND local_date = $2', [deviceId, localDate]);
    return Number(r.rows[0]!.n);
  }

  async end() { await this.pool.end(); }
}
