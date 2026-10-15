import { randomUUID } from 'node:crypto';
import { Router } from 'express';
import { z } from 'zod';
import { localDateOf, localTimeMs } from '@hajiri/attendance-engine';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { assertManages, hasRole } from '../auth/middleware.js';
import { attendancePolicy } from '../attendance/repo.js';
import { parsePhoto, photoAad, PHOTO_MAX_BYTES } from './photo.js';
import { hhmm, isoDate } from '../../validation.js';
import { sha256Hex } from '../../crypto.js';
import { requireRole } from '../auth/middleware.js';
import { isAttendanceLocked, listDays, recomputeForPunch } from '../attendance/service.js';
import { ensurePseudoDevice, nptLocalTime } from './devices.js';
import { geofence, type GeoLocation } from './geo.js';

const CLOCK_TOLERANCE_MS = 10 * 60_000;

const punchBody = z.object({
  direction: z.enum(['in', 'out', 'unknown']).default('unknown'),
  clientTime: z.string().datetime({ offset: true }),
  idempotencyKey: z.string().min(8).max(64),
  lat: z.number().min(-90).max(90).optional(),
  lng: z.number().min(-180).max(180).optional(),
  accuracyM: z.number().min(0).max(10_000).optional(),
  mocked: z.boolean().optional(),
  selfie: z.string().max(Math.ceil(PHOTO_MAX_BYTES * 4 / 3) + 64).optional(),
  source: z.enum(['mobile', 'web']),
  reason: z.string().max(300).optional(),
});

/** Mobile, web and manual punches (plan §5.2, §13.4). Every punch answers with the recomputed day: never a silent success. */
export function punchRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  r.post('/', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('Your account is not linked to an employee record. Ask HR to link it.');
    const b = punchBody.parse(req.body);
    if (b.mocked) throw new BadRequestError('Punch not saved. Your phone reported a fake location. Turn off mock location and try again.');
    const photo = b.selfie ? parsePhoto(b.selfie) : null;

    // The punch time is always the server's clock: a phone clock set a few minutes back must not erase a late arrival.
    // The client time is kept in raw for audit and large differences are flagged.
    const serverNow = Date.now();
    const clientMs = Date.parse(b.clientTime);
    const clockSkewMs = clientMs - serverNow;
    const flags: string[] = [];
    const at = serverNow;
    if (Math.abs(clockSkewMs) > CLOCK_TOLERANCE_MS) flags.push('ClockSkew');

    const emp = await db.query<{ company_id: string; emp_code: string; branch_id: string | null; work_from_home: boolean }>(
      `SELECT e.company_id, e.emp_code, a.branch_id, e.work_from_home FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
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
    // Work from home: allowed for employees set up for it, and for anyone with an approved work-from-home day.
    let remote = false;
    if (geo.inside !== true) {
      remote = e.work_from_home || (await db.query(
        `SELECT 1 FROM regularisation_request WHERE employee_id = $1 AND kind = 'wfh' AND status = 'approved' AND local_date = $2 LIMIT 1`,
        [employeeId, localDateOf(at)])).rows.length > 0;
    }
    if (remote) {
      flags.push('Remote');
      // A clock-in from home needs a photo unless the company has turned that off.
      const policy = await attendancePolicy(db, e.company_id);
      if (!photo && b.direction === 'in' && policy.wfhPhotoRequired !== false) {
        throw new BadRequestError('Take a quick photo to clock in from home.', { code: 'photo_required' });
      }
    } else if (geo.inside === false) {
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
    // Photos are kept only for clock-ins and outs from home; at the office they are not needed and are discarded.
    const photoId = remote && photo ? randomUUID() : null;
    const client = await db.connect();
    let ins: { rows: Array<{ id: string; punched_at: Date; local_date: string; direction: string }> };
    try {
      await client.query('BEGIN');
    ins = await client.query<{ id: string; punched_at: Date; local_date: string; direction: string }>(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id, punched_at, local_date, direction`,
      [device.id, device.serialNo, device.vendor, e.emp_code, employeeId, new Date(at), nptLocalTime(at), localDate, b.direction, b.source, b.source,
        JSON.stringify({ lat: b.lat ?? null, lng: b.lng ?? null, accuracyM: b.accuracyM ?? null, inside: geo.inside, remote, locationId: geo.locationId, distanceM: geo.distanceM,
          clientTime: b.clientTime, clockSkewMs, reason: b.reason ?? null, flags, selfie: photoId ? 'stored' : photo ? 'discarded' : null, photoId, ip: req.ip ?? null }), key]);
      if (photoId && photo) {
        const p = ins.rows[0]!;
        await client.query(
          `INSERT INTO punch_photo (id, company_id, employee_id, punch_id, punched_at, local_date, direction, mime, bytes, image_enc) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
          [photoId, e.company_id, employeeId, p.id, p.punched_at, p.local_date, p.direction, photo.mime, photo.data.length, cipher.encrypt(photo.data.toString('base64'), photoAad(employeeId, p.id))]);
      }
      await client.query('COMMIT');
    } catch (err) { await client.query('ROLLBACK'); throw err; } finally { client.release(); }
    const result = await recomputeForPunch(db, employeeId, at, ins.rows[0]!.id);
    return res.status(201).json({
      duplicate: false, punch: ins.rows[0], flags, location: { inside: geo.inside, name: geo.locationName, distanceM: geo.distanceM },
      day: result.day, remote, photoId, message: `Clocked ${b.direction === 'out' ? 'out' : 'in'} at ${nptLocalTime(at).slice(11, 16)}${remote ? ' from home' : geo.inside === false ? ' (outside location, flagged)' : ''}`,
    });
  });

  /** ESS "Today" card: latest computed day plus raw punches for the caller. */
  r.get('/today', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const today = localDateOf(Date.now());
    const [days, punches] = await Promise.all([
      listDays(db, employeeId, today, today),
      db.query(`SELECT id, punched_at, direction, verify_mode, source, device_sn, coalesce((raw->>'remote')::boolean, false) AS remote,
                       (SELECT ph.id FROM punch_photo ph WHERE ph.punch_id = punch_event.id AND ph.image_enc IS NOT NULL) AS photo_id
                  FROM punch_event WHERE employee_id = $1 AND local_date = $2
                  AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = punch_event.id) ORDER BY punched_at`, [employeeId, today]),
    ]);
    res.json({ date: today, day: days[0] ?? null, punches: punches.rows });
  });

  /**
   * Clock-ins from home with their photos. HR sees the company, a manager their direct reports and themselves, everyone else
   * only their own. Exact coordinates are not returned; only whether a location was shared and how accurate it was.
   */
  r.get('/remote', async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, employeeId: z.uuid().optional() }).parse(req.query);
    if (q.to < q.from) throw new BadRequestError('"to" is before "from"');
    if (Date.parse(q.to) - Date.parse(q.from) > 92 * 86_400_000) throw new BadRequestError('pick a range of 3 months or less');
    const u = req.user!;
    if (q.employeeId) await assertManages(db, u, q.employeeId);
    const all = hasRole(u, 'hr');
    const rows = await db.query(
      `SELECT p.id AS punch_id, p.employee_id, e.emp_code, e.name_en, e.designation, p.punched_at, p.local_date::text, p.direction,
              ph.id AS photo_id, ph.purged_at IS NOT NULL AS photo_purged,
              (p.raw->>'lat') IS NOT NULL AS location_shared, (p.raw->>'accuracyM')::float8 AS accuracy_m, p.raw->>'reason' AS reason, p.raw->'flags' AS flags
         FROM punch_event p JOIN employee e ON e.id = p.employee_id
         LEFT JOIN punch_photo ph ON ph.punch_id = p.id
        WHERE e.company_id = $1 AND p.local_date BETWEEN $2 AND $3 AND p.raw->>'remote' = 'true'
          AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
          AND ($4::uuid IS NULL OR p.employee_id = $4)
          AND ($5 OR p.employee_id = $6::uuid OR ($7 AND EXISTS (SELECT 1 FROM employee_assignment a WHERE a.employee_id = e.id AND a.manager_id = $6::uuid AND a.valid @> p.local_date)))
        ORDER BY p.punched_at DESC LIMIT 1000`,
      [u.companyId, q.from, q.to, q.employeeId ?? null, all, u.employeeId, hasRole(u, 'manager')]);
    res.json({ punches: rows.rows });
  });

  /** The photo itself, for the employee, their manager and HR. Views by anyone other than the employee are audited. */
  r.get('/photos/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const r0 = await db.query<{ employee_id: string; punch_id: string; mime: string; image_enc: Buffer | null; company_id: string }>(
      'SELECT employee_id, punch_id, mime, image_enc, company_id FROM punch_photo WHERE id = $1', [id]);
    const row = r0.rows[0];
    if (!row || row.company_id !== req.user!.companyId) throw new NotFoundError('photo not found');
    await assertManages(db, req.user!, row.employee_id);
    if (!row.image_enc) throw new NotFoundError('this photo was deleted after the retention period');
    if (req.user!.employeeId !== row.employee_id) await audit(db, { actorId: req.user!.id, action: 'view_photo', table: 'punch_photo', rowId: id });
    const data = Buffer.from(cipher.decrypt(row.image_enc, photoAad(row.employee_id, row.punch_id)), 'base64');
    res.setHeader('Cache-Control', 'private, max-age=300');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Disposition', 'inline');
    res.type(row.mime).send(data);
  });

  /** HR manual entry with reason (plan §5.2); audited; day recomputed. */
  r.post('/manual', requireRole('hr'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), localDate: isoDate, time: hhmm, direction: z.enum(['in', 'out']), reason: z.string().min(3).max(300) }).parse(req.body);
    const emp = await db.query<{ emp_code: string }>('SELECT emp_code FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, req.user!.companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    if (await isAttendanceLocked(db, b.employeeId, b.localDate)) throw new ConflictError('this day is locked by payroll; raise a supplementary correction instead');
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
    const result = await recomputeForPunch(db, b.employeeId, at, ins.rows[0].id);
    return res.status(201).json({ duplicate: false, punchId: ins.rows[0].id, day: result.day });
  });

  return r;
}
