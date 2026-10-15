import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { assertManages, hasRole, requireRole } from '../auth/middleware.js';
import { dateRange, enqueueRecompute, listDays, recomputeDay, INLINE_RECOMPUTE_MAX } from './service.js';

import { isoDate } from '../../validation.js';

export function attendanceRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.post('/recompute', requireRole('hr', 'manager'), async (req, res) => {
    const body = z.object({ employeeId: z.uuid(), localDate: isoDate }).parse(req.body);
    await assertManages(db, req.user!, body.employeeId);
    res.json(await recomputeDay(db, body.employeeId, body.localDate));
  });

  /** Bulk: queue a range for the worker (e.g. after a holiday edit). */
  r.post('/recompute-range', requireRole('hr'), async (req, res) => {
    const b = z.object({ employeeIds: z.array(z.uuid()).min(1).max(5000), from: isoDate, to: isoDate }).parse(req.body);
    const dates = dateRange(b.from, b.to);
    if (dates.length === 0 || dates.length > 62) return res.status(400).json({ error: 'range must be 1 to 62 days' });
    const owned = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM employee WHERE company_id = $1 AND id = ANY($2)', [req.user!.companyId, b.employeeIds]);
    if (owned.rows[0]!.n !== new Set(b.employeeIds).size) return res.status(403).json({ error: 'some employees are not in your company' });
    const ids = [...new Set(b.employeeIds)];
    // Small ranges are recomputed right away so the result is visible on return; larger ones go to the worker.
    if (ids.length * dates.length <= INLINE_RECOMPUTE_MAX) {
      for (const e of ids) for (const d of dates) await recomputeDay(db, e, d);
      return res.json({ queued: 0, recomputed: ids.length * dates.length });
    }
    await enqueueRecompute(db, ids.flatMap((e) => dates.map((d) => ({ employeeId: e, localDate: d }))), 'manual');
    return res.json({ queued: ids.length * dates.length, recomputed: 0 });
  });

  r.get('/days', async (req, res) => {
    const q = z.object({ employeeId: z.uuid(), from: isoDate, to: isoDate }).parse(req.query);
    await assertManages(db, req.user!, q.employeeId);
    res.json({ days: await listDays(db, q.employeeId, q.from, q.to) });
  });

  /** Raw punches an employee may see for themselves (plan §13.13 "My punches"). */
  r.get('/punches', async (req, res) => {
    const q = z.object({ employeeId: z.uuid(), from: isoDate, to: isoDate }).parse(req.query);
    await assertManages(db, req.user!, q.employeeId);
    const rows = await db.query(
      `SELECT p.id, p.punched_at, p.local_date, p.direction, p.verify_mode, p.source, p.device_sn, p.supersedes_id,
              (SELECT count(*) FROM punch_event s WHERE s.supersedes_id = p.id) > 0 AS superseded
         FROM punch_event p WHERE p.employee_id = $1 AND p.local_date BETWEEN $2 AND $3 ORDER BY p.punched_at`,
      [q.employeeId, q.from, q.to]);
    res.json({ punches: rows.rows });
  });

  /** Exceptions queue (plan §5.4): latest days with anomalies in a range; HR company-wide, a manager for direct reports. */
  r.get('/exceptions', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, status: z.string().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT v.id, v.employee_id, e.emp_code, e.name_en, e.name_ne, v.local_date, v.bs_date, v.status, v.worked_min, v.late_min, v.early_min, v.ot_min, v.flags
         FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
        WHERE e.company_id = $1 AND v.local_date BETWEEN $2 AND $3 AND v.locked_at IS NULL
          AND (v.status IN ('Incomplete', 'Absent') OR v.flags @> '[{"kind":"OTCapExceeded"}]' OR v.flags @> '[{"kind":"OTPreapprovalMissing"}]'
               OR v.flags @> '[{"kind":"UnpairedPunch"}]' OR v.flags @> '[{"kind":"AutoClockOut"}]')
          AND ($4::text IS NULL OR v.status = $4)
          AND ($5 OR e.id = $6::uuid OR EXISTS (SELECT 1 FROM employee_assignment a WHERE a.employee_id = e.id AND a.manager_id = $6::uuid AND a.valid @> v.local_date))
        ORDER BY v.local_date DESC, e.emp_code LIMIT 1000`,
      [req.user!.companyId, q.from, q.to, q.status ?? null, hasRole(req.user!, 'hr'), req.user!.employeeId]);
    res.json({ exceptions: rows.rows });
  });

  /** Team Today (plan §3 manager screen): everyone's latest status for one date. */
  r.get('/today', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ date: isoDate }).parse(req.query);
    // HR sees the whole company; a manager sees only their direct reports (as of that day) and themselves.
    const companyWide = hasRole(req.user!, 'hr');
    const rows = await db.query(
      `SELECT e.id AS employee_id, e.emp_code, e.name_en, e.name_ne, v.status, v.first_in, v.last_out, v.late_min, v.worked_min, v.flags,
              coalesce(v.status = 'WFH', false) OR EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = e.id AND p.local_date = $2 AND p.raw->>'remote' = 'true') AS remote,
              (SELECT ph.id FROM punch_photo ph WHERE ph.employee_id = e.id AND ph.local_date = $2 AND ph.image_enc IS NOT NULL ORDER BY ph.punched_at LIMIT 1) AS photo_id
         FROM employee e LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = $2
        WHERE e.company_id = $1 AND e.status = 'active'
          AND ($3 OR e.id = $4::uuid OR EXISTS (SELECT 1 FROM employee_assignment a WHERE a.employee_id = e.id AND a.manager_id = $4::uuid AND a.valid @> $2::date))
        ORDER BY e.emp_code`,
      [req.user!.companyId, q.date, companyWide, req.user!.employeeId]);
    res.json({ date: q.date, rows: rows.rows });
  });

  return r;
}
