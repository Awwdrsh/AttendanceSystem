import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { NotFoundError } from '../../errors.js';
import { requireRole } from '../auth/middleware.js';
import { recomputeDay } from '../attendance/service.js';

import { isoDate } from '../../validation.js';

/** Terminal user names travel in a tab-separated, newline-delimited command: strip every control character. */
function deviceName(name: string): string {
  return name.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 24);
}

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
    const rows = await db.query(
      `UPDATE device SET status = 'active', company_id = $2, branch_id = coalesce($3, branch_id), name = coalesce($4, name)
        WHERE id = $1 AND (company_id IS NULL OR company_id = $2) RETURNING id, serial_no, status`, [id, req.user!.companyId, b.branchId ?? null, b.name ?? null]);
    if (!rows.rows[0]) throw new NotFoundError('device not found');
    await audit(db, { actorId: req.user!.id, action: 'activate', table: 'device', rowId: id, after: rows.rows[0] });
    res.json(rows.rows[0]);
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
   * Map a device PIN to an employee. Punches already received for that PIN since `from` are re-attributed by inserting
   * superseding rows (punch_event is append-only), and the affected days are recomputed.
   */
  r.post('/:id/map', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ pin: z.string().min(1).max(24), employeeId: z.uuid(), from: isoDate, syncToDevice: z.boolean().default(true) }).parse(req.body);
    const dev = await db.query<{ serial_no: string; vendor: string }>('SELECT serial_no, vendor FROM device WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!dev.rows[0]) throw new NotFoundError('device not found');
    const emp = await db.query<{ emp_code: string; name_en: string }>('SELECT emp_code, name_en FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, req.user!.companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');

    const client = await db.connect();
    let dates: string[] = [];
    try {
      await client.query('BEGIN');
      // Close any open mapping for this PIN, then open the new one.
      await client.query(`UPDATE device_user_map SET valid = daterange(lower(valid), $3::date) WHERE device_id = $1 AND device_user_pin = $2 AND upper_inf(valid) AND lower(valid) < $3::date`, [id, b.pin, b.from]);
      await client.query(`DELETE FROM device_user_map WHERE device_id = $1 AND device_user_pin = $2 AND lower(valid) >= $3::date`, [id, b.pin, b.from]);
      await client.query(`INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES ($1, $2, $3, daterange($4, NULL))`, [id, b.pin, b.employeeId, b.from]);
      const re = await client.query<{ local_date: string }>(
        `WITH unmapped AS (
           SELECT p.* FROM punch_event p
            WHERE p.device_id = $1 AND p.device_user_pin = $2 AND p.employee_id IS NULL AND p.local_date >= $4::date
              AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id))
         INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source,
                                  raw, gateway_signature, idempotency_key, supersedes_id)
         SELECT device_id, device_sn, vendor, device_user_pin, $3, punched_at, device_local_time, local_date, direction, verify_mode, source,
                raw, gateway_signature, encode(sha256(convert_to(idempotency_key || ':reattributed:' || id::text, 'UTF8')), 'hex'), id
           FROM unmapped RETURNING local_date`, [id, b.pin, b.employeeId, b.from]);
      dates = [...new Set(re.rows.map((x) => x.local_date))];
      if (b.syncToDevice && dev.rows[0].vendor === 'zkteco') {
        await client.query('INSERT INTO device_command (device_id, command) VALUES ($1, $2)',
          [id, `DATA UPDATE USERINFO PIN=${b.pin}\tName=${deviceName(emp.rows[0].name_en)}\tPri=0`]);
      }
      await audit(client, { actorId: req.user!.id, action: 'map_pin', table: 'device_user_map', rowId: id, after: { pin: b.pin, employeeId: b.employeeId, from: b.from, reattributed: re.rowCount } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }

    for (const d of dates) await recomputeDay(db, b.employeeId, d);
    res.status(201).json({ ok: true, reattributed: dates.length, dates });
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
      await db.query('INSERT INTO device_command (device_id, command) VALUES ($1, $2)', [id, `DATA UPDATE USERINFO PIN=${m.device_user_pin}\tName=${deviceName(m.name_en)}\tPri=0`]);
    }
    res.json({ queued: rows.rows.length });
  });

  return r;
}
