import { createHash, randomBytes } from 'node:crypto';
import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { requireRole } from '../auth/middleware.js';
import { recomputeDay } from '../attendance/service.js';

import { isoDate } from '../../validation.js';

/** Terminal user names travel in a tab-separated, newline-delimited command: strip every control character. */
function deviceName(name: string): string {
  return name.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 24);
}

/** Terminal PINs are plain digits/letters; anything else could smuggle extra fields or commands into the device protocol. */
const PIN_RE = /^[A-Za-z0-9]{1,24}$/;

export function deviceRoutes({ db }: AppDeps): Router {
  const r = Router();
  r.use(requireRole('hr', 'admin'));

  r.get('/', async (req, res) => {
    const rows = await db.query(
      `SELECT d.id, d.company_id, d.branch_id, d.serial_no, d.vendor, d.protocol, d.name, d.status, d.fw_version, d.push_version, d.tz_offset_min,
              d.last_handshake_at, d.last_seen_at, d.created_at,
              (d.last_seen_at IS NULL OR d.last_seen_at < now() - interval '120 seconds') AS offline,
              (SELECT count(*) FROM device_user_map m WHERE m.device_id = d.id AND m.valid @> current_date) AS mapped_users,
              (SELECT count(*) FROM device_command c WHERE c.device_id = d.id AND c.status = 'queued') AS queued_commands
         FROM device d WHERE d.company_id = $1 OR d.company_id IS NULL ORDER BY d.status, d.created_at DESC`, [req.user!.companyId]);
    res.json({ devices: rows.rows });
  });

  /** Approve a pending device for this company (only active devices may push punches). */
  r.post('/:id/activate', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ branchId: z.uuid().optional(), name: z.string().max(120).optional() }).parse(req.body);
    if (b.branchId && !(await db.query('SELECT 1 FROM branch WHERE id = $1 AND company_id = $2', [b.branchId, req.user!.companyId])).rows[0]) throw new NotFoundError('branch not found');
    const rows = await db.query(
      `UPDATE device SET status = 'active', company_id = $2, branch_id = coalesce($3, branch_id), name = coalesce($4, name)
        WHERE id = $1 AND (company_id IS NULL OR company_id = $2) RETURNING id, serial_no, status`, [id, req.user!.companyId, b.branchId ?? null, b.name ?? null]);
    if (!rows.rows[0]) throw new NotFoundError('device not found');
    await audit(db, { actorId: req.user!.id, action: 'activate', table: 'device', rowId: id, after: rows.rows[0] });
    res.json(rows.rows[0]);
  });

  /**
   * Issue (or rotate) the shared key for a device that can send one, such as a Hikvision terminal. Only the SHA-256 is stored;
   * the key is shown once and goes into the device's listening-host URL (?key=...). ZKTeco ADMS terminals cannot send a key.
   */
  r.post('/:id/key', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const d = await db.query<{ protocol: string }>('SELECT protocol FROM device WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!d.rows[0]) throw new NotFoundError('device not found');
    if (d.rows[0].protocol === 'adms') throw new BadRequestError('ZKTeco ADMS terminals cannot send a device key; keep them on the office network');
    const key = randomBytes(24).toString('base64url');
    await db.query('UPDATE device SET comm_key_hash = $2 WHERE id = $1', [id, createHash('sha256').update(key, 'utf8').digest('hex')]);
    await audit(db, { actorId: req.user!.id, action: 'rotate_key', table: 'device', rowId: id });
    res.status(201).json({ key });
  });

  r.post('/:id/disable', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query(`UPDATE device SET status = 'disabled' WHERE id = $1 AND company_id = $2 AND vendor NOT IN ('mobile', 'kiosk', 'manual') RETURNING id, serial_no, status`, [id, req.user!.companyId]);
    if (!rows.rows[0]) throw new NotFoundError('device not found');
    await audit(db, { actorId: req.user!.id, action: 'disable', table: 'device', rowId: id });
    res.json(rows.rows[0]);
  });

  r.get('/:id/mappings', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query(
      `SELECT m.device_user_pin, m.employee_id, e.emp_code, e.name_en, m.valid::text FROM device_user_map m JOIN employee e ON e.id = m.employee_id
        JOIN device d ON d.id = m.device_id WHERE m.device_id = $1 AND d.company_id = $2 ORDER BY m.device_user_pin, lower(m.valid)`, [id, req.user!.companyId]);
    res.json({ mappings: rows.rows });
  });

  /**
   * Map a device PIN to an employee from `from` onwards. If the PIN is already mapped to someone else in that range the
   * request is refused (409) unless `replace` is set, so a typo cannot silently take over a colleague's punches. Every
   * punch for that PIN since `from` (unmapped or attributed to another employee) is re-attributed by inserting
   * superseding rows (punch_event is append-only), and the affected days are recomputed for everyone involved.
   */
  r.post('/:id/map', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ pin: z.string().regex(PIN_RE, 'PIN must be 1 to 24 letters or digits'), employeeId: z.uuid(), from: isoDate, syncToDevice: z.boolean().default(true), replace: z.boolean().default(false) }).parse(req.body);
    const dev = await db.query<{ serial_no: string; vendor: string }>('SELECT serial_no, vendor FROM device WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!dev.rows[0]) throw new NotFoundError('device not found');
    const emp = await db.query<{ emp_code: string; name_en: string }>('SELECT emp_code, name_en FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, req.user!.companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');

    const client = await db.connect();
    let dates: string[] = [];
    let previous: Array<{ employee_id: string; local_date: string }> = [];
    try {
      await client.query('BEGIN');
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`pinmap:${id}:${b.pin}`]);
      const holders = await client.query<{ employee_id: string; emp_code: string; name_en: string }>(
        `SELECT DISTINCT m.employee_id, e.emp_code, e.name_en FROM device_user_map m JOIN employee e ON e.id = m.employee_id
          WHERE m.device_id = $1 AND m.device_user_pin = $2 AND m.valid && daterange($3::date, NULL) AND m.employee_id <> $4`, [id, b.pin, b.from, b.employeeId]);
      if (holders.rows.length && !b.replace) {
        const who = holders.rows.map((h) => `${h.name_en} (${h.emp_code})`).join(', ');
        throw new ConflictError(`PIN ${b.pin} on this device is already mapped to ${who}. Confirm the replacement to move their punches since ${b.from} to ${emp.rows[0].name_en}.`);
      }
      // Close any open mapping for this PIN, then open the new one.
      await client.query(`UPDATE device_user_map SET valid = daterange(lower(valid), $3::date) WHERE device_id = $1 AND device_user_pin = $2 AND upper_inf(valid) AND lower(valid) < $3::date`, [id, b.pin, b.from]);
      await client.query(`DELETE FROM device_user_map WHERE device_id = $1 AND device_user_pin = $2 AND lower(valid) >= $3::date`, [id, b.pin, b.from]);
      await client.query(`INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES ($1, $2, $3, daterange($4, NULL))`, [id, b.pin, b.employeeId, b.from]);
      // Current (not yet superseded) punches for this PIN since `from` that are not already this employee's.
      const moving = `SELECT p.* FROM punch_event p
            WHERE p.device_id = $1 AND p.device_user_pin = $2 AND p.employee_id IS DISTINCT FROM $3 AND p.local_date >= $4::date
              AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)`;
      previous = (await client.query<{ employee_id: string; local_date: string }>(
        `SELECT DISTINCT employee_id, local_date::text FROM (${moving}) m WHERE employee_id IS NOT NULL`, [id, b.pin, b.employeeId, b.from])).rows;
      const re = await client.query<{ local_date: string }>(
        `WITH moving AS (${moving})
         INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source,
                                  raw, gateway_signature, idempotency_key, supersedes_id)
         SELECT device_id, device_sn, vendor, device_user_pin, $3, punched_at, device_local_time, local_date, direction, verify_mode, source,
                raw, gateway_signature, encode(sha256(convert_to(idempotency_key || ':reattributed:' || id::text, 'UTF8')), 'hex'), id
           FROM moving RETURNING local_date::text`, [id, b.pin, b.employeeId, b.from]);
      dates = [...new Set(re.rows.map((x) => x.local_date))];
      if (b.syncToDevice && dev.rows[0].vendor === 'zkteco') {
        await client.query('INSERT INTO device_command (device_id, command) VALUES ($1, $2)',
          [id, `DATA UPDATE USERINFO PIN=${b.pin}\tName=${deviceName(emp.rows[0].name_en)}\tPri=0`]);
      }
      await audit(client, { actorId: req.user!.id, action: 'map_pin', table: 'device_user_map', rowId: id, after: { pin: b.pin, employeeId: b.employeeId, from: b.from, reattributed: re.rowCount, replaced: holders.rows.map((h) => h.employee_id) } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }

    // Recompute both sides; a morning punch can belong to the previous day's shift.
    const day = (d: string, n: number) => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
    const work = new Set<string>();
    for (const d of dates) { work.add(`${b.employeeId}|${day(d, -1)}`); work.add(`${b.employeeId}|${d}`); }
    for (const p of previous) { work.add(`${p.employee_id}|${day(p.local_date, -1)}`); work.add(`${p.employee_id}|${p.local_date}`); }
    for (const w of work) { const [e, d] = w.split('|') as [string, string]; await recomputeDay(db, e, d); }
    res.status(201).json({ ok: true, reattributed: dates.length, dates, movedFrom: [...new Set(previous.map((p) => p.employee_id))] });
  });

  /** Punches from active devices whose PIN is not mapped to an employee (HR queue, plan §5.2). */
  r.get('/punches/unmapped', async (req, res) => {
    const rows = await db.query(
      `SELECT p.id, p.device_id, p.device_sn, p.device_user_pin, p.punched_at, p.local_date, p.verify_mode,
              (SELECT count(*) FROM punch_event q WHERE q.device_id = p.device_id AND q.device_user_pin = p.device_user_pin AND q.employee_id IS NULL) AS total_for_pin
         FROM punch_event p JOIN device d ON d.id = p.device_id
        WHERE p.employee_id IS NULL AND d.company_id = $1 AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
        ORDER BY p.ingested_at DESC LIMIT 500`, [req.user!.companyId]);
    res.json({ punches: rows.rows });
  });

  /** Queue a command for the terminal (allow-listed verbs only; SHELL is impossible by DB constraint). */
  r.post('/:id/commands', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ command: z.enum(['CHECK', 'INFO', 'REBOOT', 'LOG', 'DATA QUERY USERINFO', 'CLEAR LOG']) }).parse(req.body);
    const dev = await db.query('SELECT 1 FROM device WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!dev.rows[0]) throw new NotFoundError('device not found');
    const row = await db.query<{ id: string }>('INSERT INTO device_command (device_id, command) VALUES ($1, $2) RETURNING id', [id, b.command]);
    await audit(db, { actorId: req.user!.id, action: 'device_command', table: 'device_command', rowId: row.rows[0]!.id, after: b });
    res.status(201).json({ id: Number(row.rows[0]!.id) });
  });

  r.get('/:id/commands', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query(
      `SELECT c.id, c.command, c.status, c.return_code, c.queued_at, c.sent_at, c.acked_at FROM device_command c JOIN device d ON d.id = c.device_id
        WHERE c.device_id = $1 AND d.company_id = $2 ORDER BY c.id DESC LIMIT 100`, [id, req.user!.companyId]);
    res.json({ commands: rows.rows });
  });

  /** Push all current PIN mappings to the device as USERINFO updates. */
  r.post('/:id/sync-users', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query<{ device_user_pin: string; name_en: string }>(
      `SELECT m.device_user_pin, e.name_en FROM device_user_map m JOIN employee e ON e.id = m.employee_id JOIN device d ON d.id = m.device_id
        WHERE m.device_id = $1 AND d.company_id = $2 AND m.valid @> current_date AND e.status = 'active'`, [id, req.user!.companyId]);
    for (const m of rows.rows) {
      if (!PIN_RE.test(m.device_user_pin)) continue;
      await db.query('INSERT INTO device_command (device_id, command) VALUES ($1, $2)', [id, `DATA UPDATE USERINFO PIN=${m.device_user_pin}\tName=${deviceName(m.name_en)}\tPri=0`]);
    }
    res.json({ queued: rows.rows.length });
  });

  return r;
}
