import { Router } from 'express';
import { z } from 'zod';
import { localDateOf, localTimeMs } from '@hajiri/attendance-engine';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, NotFoundError } from '../../errors.js';
import { hhmm, isoDate } from '../../validation.js';
import { sha256Hex } from '../../crypto.js';
import { requireRole } from '../auth/middleware.js';
import { listDays, recomputeDay } from '../attendance/service.js';
import { ensurePseudoDevice, nptLocalTime } from './devices.js';
import { geofence, type GeoLocation } from './geo.js';

const CLOCK_TOLERANCE_MS = 10 * 60_000;
const SELFIE_MAX_BYTES = 300 * 1024;

const punchBody = z.object({
  direction: z.enum(['in', 'out', 'unknown']).default('unknown'),
  clientTime: z.string().datetime({ offset: true }),
  idempotencyKey: z.string().min(8).max(64),
  lat: z.number().min(-90).max(90).optional(),
  lng: z.number().min(-180).max(180).optional(),
  accuracyM: z.number().min(0).max(10_000).optional(),
  mocked: z.boolean().optional(),
  selfie: z.string().optional(),
  source: z.enum(['mobile', 'web']),
  reason: z.string().max(300).optional(),
});

/** Mobile, web and manual punches (plan §5.2, §13.4). Every punch answers with the recomputed day: never a silent success. */
export function punchRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.post('/', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('Your account is not linked to an employee record. Ask HR to link it.');
    const b = punchBody.parse(req.body);
    if (b.mocked) throw new BadRequestError('Punch not saved. Your phone reported a fake location. Turn off mock location and try again.');
    if (b.selfie && Buffer.byteLength(b.selfie, 'utf8') > SELFIE_MAX_BYTES) throw new BadRequestError('Selfie is too large (max 300 KB).');

    const serverNow = Date.now();
    const clientMs = Date.parse(b.clientTime);
    const clockSkewMs = clientMs - serverNow;
    const flags: string[] = [];
    let at = clientMs;
    if (Math.abs(clockSkewMs) > CLOCK_TOLERANCE_MS) { at = serverNow; flags.push('ClockSkew'); }

    const emp = await db.query<{ company_id: string; emp_code: string; branch_id: string | null }>(
      `SELECT e.company_id, e.emp_code, a.branch_id FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.id = $1 AND e.status = 'active'`, [employeeId]);
    const e = emp.rows[0];
    if (!e) throw new NotFoundError('employee not found');

    let geo: ReturnType<typeof geofence> = { inside: null, locationId: null, distanceM: null, locationName: null };
    if (b.lat !== undefined && b.lng !== undefined) {
      const locs = await db.query<GeoLocation>(
        `SELECT id, name, lat::float8 AS lat, lng::float8 AS lng, radius_m FROM location WHERE company_id = $1 AND (branch_id IS NULL OR branch_id = $2)`,
        [e.company_id, e.branch_id]);
      geo = geofence(b.lat, b.lng, b.accuracyM ?? 50, locs.rows);
    } else {
      const any = await db.query('SELECT 1 FROM location WHERE company_id = $1 LIMIT 1', [e.company_id]);
      if (any.rows.length) { geo = { inside: false, locationId: null, distanceM: null, locationName: null }; flags.push('NoLocation'); }
    }
    if (geo.inside === false) {
      flags.push('OutsideLocation');
      if (!b.reason?.trim()) throw new BadRequestError(geo.distanceM !== null
        ? `You are ${geo.distanceM} m from ${geo.locationName}. Add a short reason to punch anyway.`
        : 'Your location was not available. Add a short reason to punch anyway.');
    }

    const device = await ensurePseudoDevice(db, e.company_id, b.source === 'mobile' ? 'MOBILE' : 'WEB');
    const key = sha256Hex(`app|${employeeId}|${b.idempotencyKey}`);
    const existing = await db.query<{ id: string; punched_at: Date; local_date: string }>('SELECT id, punched_at, local_date FROM punch_event WHERE idempotency_key = $1', [key]);
    if (existing.rows[0]) {
      const day = await listDays(db, employeeId, existing.rows[0].local_date, existing.rows[0].local_date);
      return res.json({ duplicate: true, punch: existing.rows[0], day: day[0] ?? null });
    }
    const localDate = localDateOf(at);
    const ins = await db.query<{ id: string; punched_at: Date; local_date: string; direction: string }>(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id, punched_at, local_date, direction`,
      [device.id, device.serialNo, device.vendor, e.emp_code, employeeId, new Date(at), nptLocalTime(at), localDate, b.direction, b.source, b.source,
        JSON.stringify({ lat: b.lat ?? null, lng: b.lng ?? null, accuracyM: b.accuracyM ?? null, inside: geo.inside, locationId: geo.locationId, distanceM: geo.distanceM,
          clientTime: b.clientTime, clockSkewMs, reason: b.reason ?? null, flags, selfie: b.selfie ? 'discarded' : null, ip: req.ip ?? null }), key]);
    const result = await recomputeDay(db, employeeId, localDate);
    return res.status(201).json({
      duplicate: false, punch: ins.rows[0], flags, location: { inside: geo.inside, name: geo.locationName, distanceM: geo.distanceM },
      day: result.day, message: `Punched ${b.direction === 'out' ? 'OUT' : 'IN'} at ${nptLocalTime(at).slice(11, 16)}${geo.inside === false ? ' (outside location, flagged)' : ''}`,
    });
  });

  /** ESS "Today" card: latest computed day plus raw punches for the caller. */
  r.get('/today', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const today = localDateOf(Date.now());
    const [days, punches] = await Promise.all([
      listDays(db, employeeId, today, today),
      db.query(`SELECT id, punched_at, direction, verify_mode, source, device_sn FROM punch_event WHERE employee_id = $1 AND local_date = $2
                  AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = punch_event.id) ORDER BY punched_at`, [employeeId, today]),
    ]);
    res.json({ date: today, day: days[0] ?? null, punches: punches.rows });
  });

  /** HR manual entry with reason (plan §5.2); audited; day recomputed. */
  r.post('/manual', requireRole('hr'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), localDate: isoDate, time: hhmm, direction: z.enum(['in', 'out']), reason: z.string().min(3).max(300) }).parse(req.body);
    const emp = await db.query<{ emp_code: string }>('SELECT emp_code FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, req.user!.companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    const device = await ensurePseudoDevice(db, req.user!.companyId!, 'MANUAL');
    const at = localTimeMs(b.localDate, b.time);
    const key = sha256Hex(`manual|${b.employeeId}|${b.localDate}|${b.time}|${b.direction}`);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'manual', 'manual', $10, $11) ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id`,
      [device.id, device.serialNo, device.vendor, emp.rows[0].emp_code, b.employeeId, new Date(at), nptLocalTime(at), b.localDate, b.direction,
        JSON.stringify({ reason: b.reason, enteredBy: req.user!.id }), key]);
    if (!ins.rows[0]) return res.status(200).json({ duplicate: true });
    await audit(db, { actorId: req.user!.id, action: 'manual_punch', table: 'punch_event', rowId: ins.rows[0].id, after: b });
    const result = await recomputeDay(db, b.employeeId, b.localDate);
    return res.status(201).json({ duplicate: false, punchId: ins.rows[0].id, day: result.day });
  });

  return r;
}
