import { randomUUID } from 'node:crypto';
import { localDateOf } from '@hajiri/attendance-engine';
import type { FieldCipher } from '../../crypto.js';
import { sha256Hex } from '../../crypto.js';
import type { Db, Queryable } from '../../db/pool.js';
import { recomputeForPunch } from '../attendance/service.js';
import { ensurePseudoDevice, nptLocalTime } from '../punch/devices.js';
import { photoAad } from '../punch/photo.js';

/** One device row per company for kiosk punches (vendor 'kiosk', so the offline alarm ignores it). */
export async function ensureKioskDevice(db: Queryable, companyId: string): Promise<{ id: string; serialNo: string; vendor: string }> {
  const c = await db.query<{ code: string }>('SELECT code FROM company WHERE id = $1', [companyId]);
  const serial = `KIOSK-${c.rows[0]!.code}`;
  await db.query(
    `INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES ($1, $2, 'kiosk', 'app', 'Kiosk tablets', 'active') ON CONFLICT (serial_no) DO NOTHING`,
    [companyId, serial]);
  const d = await db.query<{ id: string }>('SELECT id FROM device WHERE serial_no = $1', [serial]);
  return { id: d.rows[0]!.id, serialNo: serial, vendor: 'kiosk' };
}

/** Direction of the next punch today: in after nothing or an out, otherwise out. */
export async function nextDirection(db: Queryable, employeeId: string, localDate = localDateOf(Date.now())): Promise<'in' | 'out'> {
  const r = await db.query<{ direction: string }>(
    `SELECT direction FROM punch_event p WHERE employee_id = $1 AND local_date = $2
        AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id) ORDER BY punched_at DESC LIMIT 1`, [employeeId, localDate]);
  const last = r.rows[0]?.direction;
  return !last || last === 'out' ? 'in' : 'out';
}

export interface AppPunchInput {
  companyId: string;
  employeeId: string;
  empCode: string;
  direction: 'in' | 'out';
  kind: 'kiosk' | 'field';
  /** Unique per attempted punch; replays return the existing punch. */
  idempotency: string;
  raw: Record<string, unknown>;
  photo?: { mime: string; data: Buffer } | null;
}

/**
 * Store a kiosk or field punch at server time, with an optional encrypted photo (same table and retention as the
 * work-from-home photo), then recompute the day. Returns the punch and whether it was a replay.
 */
export async function insertAppPunch(db: Db, cipher: FieldCipher, p: AppPunchInput): Promise<{ duplicate: boolean; punchId: string; punchedAt: Date; localDate: string; photoId: string | null }> {
  const key = sha256Hex(`${p.kind}|${p.employeeId}|${p.idempotency}`);
  const existing = await db.query<{ id: string; punched_at: Date; local_date: string }>('SELECT id, punched_at, local_date FROM punch_event WHERE idempotency_key = $1', [key]);
  if (existing.rows[0]) return { duplicate: true, punchId: existing.rows[0].id, punchedAt: existing.rows[0].punched_at, localDate: existing.rows[0].local_date, photoId: null };
  const at = Date.now();
  const localDate = localDateOf(at);
  const device = p.kind === 'kiosk' ? await ensureKioskDevice(db, p.companyId) : await ensurePseudoDevice(db, p.companyId, 'MOBILE');
  const photoId = p.photo ? randomUUID() : null;
  const flags = Array.isArray(p.raw.flags) ? p.raw.flags : [];
  const client = await db.connect();
  let row: { id: string; punched_at: Date; local_date: string; direction: string };
  try {
    await client.query('BEGIN');
    const ins = await client.query<{ id: string; punched_at: Date; local_date: string; direction: string }>(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id, punched_at, local_date, direction`,
      [device.id, device.serialNo, device.vendor, p.empCode, p.employeeId, new Date(at), nptLocalTime(at), localDate, p.direction,
        p.kind === 'kiosk' ? 'kiosk' : 'field', p.kind, JSON.stringify({ ...p.raw, flags, photoId }), key]);
    row = ins.rows[0]!;
    if (photoId && p.photo) {
      await client.query(
        `INSERT INTO punch_photo (id, company_id, employee_id, punch_id, punched_at, local_date, direction, mime, bytes, image_enc) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
        [photoId, p.companyId, p.employeeId, row.id, row.punched_at, row.local_date, row.direction, p.photo.mime, p.photo.data.length,
          cipher.encrypt(p.photo.data.toString('base64'), photoAad(p.employeeId, row.id))]);
    }
    await client.query('COMMIT');
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  await recomputeForPunch(db, p.employeeId, at, row.id);
  return { duplicate: false, punchId: row.id, punchedAt: row.punched_at, localDate: row.local_date, photoId };
}

/** Close an open break at `at` (used when someone clocks out at a kiosk mid-break). */
export async function closeOpenBreak(db: Queryable, employeeId: string, at: Date): Promise<void> {
  await db.query(
    `UPDATE work_break SET ended_at = greatest($2::timestamptz, started_at),
            minutes = floor(extract(epoch FROM greatest($2::timestamptz, started_at) - started_at) / 60)::int,
            over_limit = floor(extract(epoch FROM greatest($2::timestamptz, started_at) - started_at) / 60) > max_min
      WHERE employee_id = $1 AND ended_at IS NULL`, [employeeId, at]);
}
