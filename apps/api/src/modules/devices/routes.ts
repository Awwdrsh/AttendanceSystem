import { Router } from 'express';
import { z } from 'zod';
import type { Db } from '../../db/pool.js';

export function deviceRoutes(db: Db): Router {
  const r = Router();

  r.get('/', async (_req, res) => {
    const rows = await db.query(
      `SELECT id, company_id, branch_id, serial_no, vendor, protocol, name, status, fw_version, push_version,
              last_handshake_at, last_seen_at, created_at FROM device ORDER BY created_at DESC`);
    res.json({ devices: rows.rows });
  });

  /** An admin claims a pending device for a company/branch; only active devices may push punches. */
  r.post('/:id/activate', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ companyId: z.uuid(), branchId: z.uuid().optional(), name: z.string().optional() }).parse(req.body);
    const rows = await db.query(
      `UPDATE device SET status = 'active', company_id = $2, branch_id = $3, name = coalesce($4, name)
        WHERE id = $1 RETURNING id, serial_no, status`, [id, b.companyId, b.branchId ?? null, b.name ?? null]);
    if (rows.rows.length === 0) return res.status(404).json({ error: 'device not found' });
    return res.json(rows.rows[0]);
  });

  /** Device → employee PIN mappings. */
  r.post('/:id/map', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ pin: z.string().min(1), employeeId: z.uuid(), from: z.string().regex(/^\d{4}-\d{2}-\d{2}$/) }).parse(req.body);
    await db.query(
      `INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES ($1, $2, $3, daterange($4, NULL))`,
      [id, b.pin, b.employeeId, b.from]);
    res.status(201).json({ ok: true });
  });

  /** Punches from active devices whose PIN is not mapped to an employee (HR queue, plan §5.2). */
  r.get('/punches/unmapped', async (_req, res) => {
    const rows = await db.query(
      `SELECT id, device_sn, device_user_pin, punched_at, verify_mode FROM punch_event
        WHERE employee_id IS NULL ORDER BY ingested_at DESC LIMIT 200`);
    res.json({ punches: rows.rows });
  });

  return r;
}
