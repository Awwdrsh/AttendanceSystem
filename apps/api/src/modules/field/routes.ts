import { Router, type Request } from 'express';
import { z } from 'zod';
import { localDateOf } from '@hajiri/attendance-engine';
import { weekdayOf } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import { hashPassword, sha256Hex } from '../../crypto.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, HttpError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { assertManages, companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { dateRange, isAttendanceLocked } from '../attendance/service.js';
import { readFile, saveFile } from '../files/store.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { parsePhoto, PHOTO_MAX_BYTES } from '../punch/photo.js';
import { sendReport } from '../reports/csv.js';
import { approversFor, cancelWorkflow, startWorkflow } from '../workflow/service.js';
import { allow, pairingCode, qrToken, qrWindow, QR_WINDOW_MS, weakPin } from '../kiosk/tokens.js';
import { BREAK_KINDS, FIELD_POLICY_KEY, fieldPolicy, fieldPolicySchema } from './policy.js';
import { insertAppPunch, nextDirection } from './punches.js';
import { effectiveShift } from './swaps.js';

const PAIRING_TTL_MIN = 15;
const MAX_RANGE_DAYS = 92;

class TooManyError extends HttpError { constructor(msg: string) { super(429, msg); } }

const geo = { lat: z.number().min(-90).max(90).optional(), lng: z.number().min(-180).max(180).optional(), accuracyM: z.number().min(0).max(10_000).optional() };
const photoField = z.string().max(Math.ceil(PHOTO_MAX_BYTES * 4 / 3) + 64).optional();
const rangeQuery = z.object({ from: isoDate, to: isoDate, employeeId: z.uuid().optional() });

function checkRange(from: string, to: string, maxDays = MAX_RANGE_DAYS): void {
  if (to < from) throw new BadRequestError('"to" is before "from"');
  if (Date.parse(to) - Date.parse(from) > maxDays * 86_400_000) throw new BadRequestError(`pick a range of ${maxDays} days or less`);
}

/** HR sees everyone, a manager their direct reports (on that day) and themselves, everyone else only themselves. */
function scopeSql(alias: string, dateExpr: string, p: { all: number; me: number; mgr: number }): string {
  return `($${p.all} OR ${alias}.employee_id = $${p.me}::uuid OR ($${p.mgr} AND EXISTS (SELECT 1 FROM employee_assignment a WHERE a.employee_id = ${alias}.employee_id AND a.manager_id = $${p.me}::uuid AND a.valid @> ${dateExpr})))`;
}

async function employeeOf(db: Queryable, req: Request): Promise<{ id: string; company_id: string; emp_code: string; name_en: string; field_work: boolean }> {
  const id = req.user!.employeeId;
  if (!id) throw new BadRequestError('Your account is not linked to an employee record. Ask HR to link it.');
  const r = await db.query<{ id: string; company_id: string; emp_code: string; name_en: string; field_work: boolean }>(
    `SELECT id, company_id, emp_code, name_en, field_work FROM employee WHERE id = $1 AND status = 'active'`, [id]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

async function assertEmployeeInCompany(db: Queryable, companyId: string, employeeId: string): Promise<void> {
  const r = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
}

const visitAad = (id: string) => `field_visit:${id}`;

/**
 * Field force, breaks, locations, kiosks and shift swaps (Jibble-style time tracking on top of the attendance engine).
 * Attendance maths is unchanged: breaks are recorded and reported, visits can open the day with a field punch, and
 * approved swaps become roster rows.
 */
export function fieldRoutes({ db, cipher, pepper }: AppDeps): Router {
  const r = Router();

  // ---------------------------------------------------------------- policy
  r.get('/policy', async (req, res) => {
    res.json({ policy: await fieldPolicy(db, companyIdOf(req)) });
  });

  r.put('/policy', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const value = fieldPolicySchema.parse(req.body);
    const before = await fieldPolicy(db, companyId);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [companyId, FIELD_POLICY_KEY, JSON.stringify(value), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: FIELD_POLICY_KEY, before, after: value });
    res.json({ policy: value });
  });

  // ---------------------------------------------------------------- my day: breaks, visits, kiosk badge state
  r.get('/me', async (req, res) => {
    const e = await employeeOf(db, req);
    const today = localDateOf(Date.now());
    const [policy, direction, breaks, visits, cred] = await Promise.all([
      fieldPolicy(db, e.company_id),
      nextDirection(db, e.id, today),
      db.query(`SELECT id, kind, paid, max_min, started_at, ended_at, minutes, over_limit FROM work_break WHERE employee_id = $1 AND (local_date = $2 OR ended_at IS NULL) ORDER BY started_at`, [e.id, today]),
      db.query(`SELECT id, place, purpose, started_at, ended_at, outcome, photo_file_id IS NOT NULL AS has_photo FROM field_visit WHERE employee_id = $1 AND (local_date = $2 OR ended_at IS NULL) ORDER BY started_at`, [e.id, today]),
      db.query<{ pin_hash: string | null; locked_until: Date | null }>('SELECT pin_hash, locked_until FROM kiosk_credential WHERE employee_id = $1', [e.id]),
    ]);
    const breakTypes = BREAK_KINDS.filter((k) => policy.breaks[k].enabled).map((k) => ({ kind: k, paid: policy.breaks[k].paid, maxMin: policy.breaks[k].maxMin }));
    res.json({
      date: today, clockedIn: direction === 'out', fieldWork: e.field_work, fieldPhotoRequired: policy.fieldPhotoRequired,
      breakTypes, breaks: breaks.rows, openBreak: breaks.rows.find((b) => !b.ended_at) ?? null,
      visits: visits.rows, openVisit: visits.rows.find((v) => !v.ended_at) ?? null,
      kioskPin: { set: !!cred.rows[0]?.pin_hash, lockedUntil: cred.rows[0]?.locked_until && cred.rows[0].locked_until > new Date() ? cred.rows[0].locked_until : null },
    });
  });

  // ---------------------------------------------------------------- breaks
  r.post('/breaks/start', async (req, res) => {
    const e = await employeeOf(db, req);
    const b = z.object({ kind: z.enum(BREAK_KINDS) }).parse(req.body);
    const policy = await fieldPolicy(db, e.company_id);
    const rule = policy.breaks[b.kind];
    if (!rule.enabled) throw new BadRequestError('This kind of break is not used here.');
    if ((await nextDirection(db, e.id)) !== 'out') throw new BadRequestError('Clock in first, then start your break.');
    const ins = await db.query(
      `INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at) VALUES ($1, $2, $3, $4, $5, $6, now())
       ON CONFLICT DO NOTHING RETURNING id, kind, paid, max_min, started_at`,
      [e.company_id, e.id, localDateOf(Date.now()), b.kind, rule.paid, rule.maxMin]);
    if (!ins.rows[0]) throw new ConflictError('You are already on a break. End it first.');
    res.status(201).json({ break: ins.rows[0] });
  });

  r.post('/breaks/end', async (req, res) => {
    const e = await employeeOf(db, req);
    const u = await db.query<{ id: string; kind: string; minutes: number; max_min: number; over_limit: boolean; started_at: Date; ended_at: Date }>(
      `UPDATE work_break SET ended_at = now(), minutes = floor(extract(epoch FROM now() - started_at) / 60)::int,
              over_limit = floor(extract(epoch FROM now() - started_at) / 60) > max_min
        WHERE employee_id = $1 AND ended_at IS NULL RETURNING id, kind, minutes, max_min, over_limit, started_at, ended_at`, [e.id]);
    const br = u.rows[0];
    if (!br) throw new ConflictError('You are not on a break.');
    if (br.over_limit) {
      // The line manager (or HR when there is none) hears about a break that ran well past its limit.
      for (const userId of await approversFor(db, e.company_id, e.id, 'manager')) {
        await notify(db, {
          companyId: e.company_id, userId, employeeId: e.id, template: 'break.over_limit', title: 'Long break',
          body: `${e.name_en} took a ${br.minutes} min ${br.kind} break (limit ${br.max_min} min).`, payload: { breakId: br.id },
        });
      }
    }
    res.json({ break: br });
  });

  r.get('/breaks', async (req, res) => {
    const q = rangeQuery.extend({ overOnly: z.enum(['1', 'true']).optional(), format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    checkRange(q.from, q.to);
    const u = req.user!;
    if (q.employeeId) await assertManages(db, u, q.employeeId);
    const rows = await db.query(
      `SELECT b.id, b.employee_id, e.emp_code, e.name_en, b.local_date, b.kind, b.paid, b.max_min, b.started_at, b.ended_at,
              coalesce(b.minutes, floor(extract(epoch FROM now() - b.started_at) / 60)::int) AS minutes, b.over_limit, b.ended_at IS NULL AS open
         FROM work_break b JOIN employee e ON e.id = b.employee_id
        WHERE b.company_id = $1 AND b.local_date BETWEEN $2 AND $3 AND ($4::uuid IS NULL OR b.employee_id = $4) AND (NOT $8 OR b.over_limit)
          AND ${scopeSql('b', 'b.local_date', { all: 5, me: 6, mgr: 7 })}
        ORDER BY b.started_at DESC LIMIT 2000`,
      [companyIdOf(req), q.from, q.to, q.employeeId ?? null, hasRole(u, 'hr'), u.employeeId, hasRole(u, 'manager'), !!q.overOnly]);
    if (q.format === 'csv') {
      return sendReport(res, 'csv', `breaks-${q.from}-${q.to}`, [
        { key: 'emp_code', header: 'Employee code' }, { key: 'name_en', header: 'Name' }, { key: 'date_bs', header: 'Date (BS)' },
        { key: 'kind', header: 'Break' }, { key: 'paid', header: 'Paid' }, { key: 'start', header: 'Start' }, { key: 'end', header: 'End' },
        { key: 'minutes', header: 'Minutes' }, { key: 'max_min', header: 'Limit (min)' }, { key: 'over', header: 'Over limit' },
      ], rows.rows.map((x) => ({
        ...x, date_bs: bsLabel(x.local_date), paid: x.paid ? 'Yes' : 'No', over: x.over_limit ? 'Yes' : 'No',
        start: nptHm(x.started_at), end: x.ended_at ? nptHm(x.ended_at) : '',
      })));
    }
    return res.json({ breaks: rows.rows });
  });

  // ---------------------------------------------------------------- field visits
  r.post('/visits/start', async (req, res) => {
    const e = await employeeOf(db, req);
    if (!e.field_work) throw new ForbiddenError('Field visits are not turned on for you. Ask HR.');
    const b = z.object({ place: z.string().trim().min(1).max(160), purpose: z.string().trim().max(500).optional(), photo: photoField, ...geo }).parse(req.body);
    const policy = await fieldPolicy(db, e.company_id);
    const photo = b.photo ? parsePhoto(b.photo) : null;
    if (policy.fieldPhotoRequired && !photo) throw new BadRequestError('Take a quick photo at the place you are visiting.', { code: 'photo_required' });
    const open = await db.query('SELECT 1 FROM field_visit WHERE employee_id = $1 AND ended_at IS NULL', [e.id]);
    if (open.rows[0]) throw new ConflictError('You have a visit in progress. End it first.');
    const today = localDateOf(Date.now());
    const client = await db.connect();
    let visit: Record<string, unknown> & { id: string };
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>(
        `INSERT INTO field_visit (company_id, employee_id, local_date, place, purpose, started_at, start_lat, start_lng, start_accuracy_m)
         VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8) RETURNING id, place, purpose, started_at`,
        [e.company_id, e.id, today, b.place, b.purpose || null, b.lat ?? null, b.lng ?? null, b.accuracyM !== undefined ? Math.round(b.accuracyM) : null]);
      visit = ins.rows[0]!;
      if (photo) {
        const f = await saveFile(client, cipher, { companyId: e.company_id, purpose: 'field_visit_photo', filename: `visit-${today}.jpg`, file: { mime: photo.mime, data: photo.data }, uploadedBy: req.user!.id, attachedTo: visitAad(visit.id) });
        await client.query('UPDATE field_visit SET photo_file_id = $2 WHERE id = $1', [visit.id, f.id]);
      }
      await client.query('COMMIT');
    } catch (err) { await client.query('ROLLBACK'); throw err; } finally { client.release(); }
    // The first visit of a day opens the working day, like a clock-in from home.
    let clockedIn = false;
    if (policy.fieldVisitClockIn) {
      const any = await db.query(`SELECT 1 FROM punch_event WHERE employee_id = $1 AND local_date = $2 LIMIT 1`, [e.id, today]);
      if (!any.rows[0]) {
        const p = await insertAppPunch(db, cipher, {
          companyId: e.company_id, employeeId: e.id, empCode: e.emp_code, direction: 'in', kind: 'field', idempotency: `visit-in|${visit.id}`,
          raw: { field: true, visitId: visit.id, place: b.place, lat: b.lat ?? null, lng: b.lng ?? null, accuracyM: b.accuracyM ?? null, remote: false, flags: ['Field'], ip: req.ip ?? null },
        });
        await db.query('UPDATE field_visit SET punch_id = $2 WHERE id = $1', [visit.id, p.punchId]);
        clockedIn = true;
      }
    }
    res.status(201).json({ visit, clockedIn, message: clockedIn ? `Visit started and clocked in at ${nptHm(new Date())}.` : `Visit started at ${nptHm(new Date())}.` });
  });

  r.post('/visits/:id/end', async (req, res) => {
    const e = await employeeOf(db, req);
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ outcome: z.string().trim().max(1000).optional(), ...geo }).parse(req.body);
    const u = await db.query(
      `UPDATE field_visit SET ended_at = now(), outcome = $3, end_lat = $4, end_lng = $5, end_accuracy_m = $6
        WHERE id = $1 AND employee_id = $2 AND ended_at IS NULL RETURNING id, place, started_at, ended_at, outcome`,
      [id, e.id, b.outcome || null, b.lat ?? null, b.lng ?? null, b.accuracyM !== undefined ? Math.round(b.accuracyM) : null]);
    if (!u.rows[0]) throw new NotFoundError('No visit in progress with that id.');
    res.json({ visit: u.rows[0] });
  });

  r.get('/visits', async (req, res) => {
    const q = rangeQuery.extend({ format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    checkRange(q.from, q.to);
    const u = req.user!;
    if (q.employeeId) await assertManages(db, u, q.employeeId);
    const rows = await db.query(
      `SELECT v.id, v.employee_id, e.emp_code, e.name_en, e.designation, v.local_date, v.place, v.purpose, v.started_at, v.ended_at, v.outcome,
              v.start_lat::float8 AS start_lat, v.start_lng::float8 AS start_lng, v.start_accuracy_m, v.end_lat::float8 AS end_lat, v.end_lng::float8 AS end_lng, v.end_accuracy_m,
              v.photo_file_id IS NOT NULL AS has_photo, v.punch_id IS NOT NULL AS opened_day,
              floor(extract(epoch FROM coalesce(v.ended_at, now()) - v.started_at) / 60)::int AS minutes
         FROM field_visit v JOIN employee e ON e.id = v.employee_id
        WHERE v.company_id = $1 AND v.local_date BETWEEN $2 AND $3 AND ($4::uuid IS NULL OR v.employee_id = $4)
          AND ${scopeSql('v', 'v.local_date', { all: 5, me: 6, mgr: 7 })}
        ORDER BY v.started_at DESC LIMIT 2000`,
      [companyIdOf(req), q.from, q.to, q.employeeId ?? null, hasRole(u, 'hr'), u.employeeId, hasRole(u, 'manager')]);
    if (q.format === 'csv') {
      return sendReport(res, 'csv', `field-visits-${q.from}-${q.to}`, [
        { key: 'emp_code', header: 'Employee code' }, { key: 'name_en', header: 'Name' }, { key: 'date_bs', header: 'Date (BS)' },
        { key: 'place', header: 'Place' }, { key: 'purpose', header: 'Purpose' }, { key: 'start', header: 'Arrived' }, { key: 'end', header: 'Left' },
        { key: 'minutes', header: 'Minutes' }, { key: 'outcome', header: 'Outcome' }, { key: 'start_lat', header: 'Latitude' }, { key: 'start_lng', header: 'Longitude' },
      ], rows.rows.map((x) => ({ ...x, date_bs: bsLabel(x.local_date), start: nptHm(x.started_at), end: x.ended_at ? nptHm(x.ended_at) : '' })));
    }
    return res.json({ visits: rows.rows });
  });

  r.get('/visits/:id/photo', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const companyId = companyIdOf(req);
    const v = await db.query<{ employee_id: string; photo_file_id: string | null }>('SELECT employee_id, photo_file_id FROM field_visit WHERE id = $1 AND company_id = $2', [id, companyId]);
    const row = v.rows[0];
    if (!row) throw new NotFoundError('visit not found');
    await assertManages(db, req.user!, row.employee_id);
    if (!row.photo_file_id) throw new NotFoundError('this visit has no photo');
    const f = await readFile(db, cipher, companyId, row.photo_file_id);
    if (req.user!.employeeId !== row.employee_id) await audit(db, { actorId: req.user!.id, action: 'view_photo', table: 'field_visit', rowId: id });
    res.setHeader('Cache-Control', 'private, max-age=300');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Disposition', 'inline');
    res.type(f.mime).send(f.data);
  });

  // ---------------------------------------------------------------- locations (geofences) and who they apply to
  const locationBody = z.object({
    name: z.string().trim().min(1).max(80), address: z.string().trim().max(300).nullable().optional(), branchId: z.uuid().nullable().optional(),
    lat: z.number().min(-90).max(90), lng: z.number().min(-180).max(180), radiusM: z.number().int().min(20).max(5000).default(150), active: z.boolean().default(true),
  });
  const assertBranch = async (companyId: string, branchId: string | null | undefined) => {
    if (branchId && !(await db.query('SELECT 1 FROM branch WHERE id = $1 AND company_id = $2', [branchId, companyId])).rows[0]) throw new NotFoundError('branch not found');
  };

  r.get('/locations', requireRole('hr', 'manager'), async (req, res) => {
    const rows = await db.query(
      `SELECT l.id, l.name, l.address, l.branch_id, b.name_en AS branch_name, l.lat::float8 AS lat, l.lng::float8 AS lng, l.radius_m, l.active,
              coalesce((SELECT array_agg(employee_id) FROM location_assignment a WHERE a.location_id = l.id), '{}') AS employee_ids
         FROM location l LEFT JOIN branch b ON b.id = l.branch_id WHERE l.company_id = $1 ORDER BY l.active DESC, l.name`, [companyIdOf(req)]);
    res.json({ locations: rows.rows });
  });

  r.post('/locations', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = locationBody.parse(req.body);
    await assertBranch(companyId, b.branchId);
    const ins = await db.query<{ id: string }>(
      'INSERT INTO location (company_id, branch_id, name, address, lat, lng, radius_m, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id',
      [companyId, b.branchId ?? null, b.name, b.address || null, b.lat, b.lng, b.radiusM, b.active]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'location', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.put('/locations/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const b = locationBody.parse(req.body);
    await assertBranch(companyId, b.branchId);
    const u = await db.query('UPDATE location SET name = $3, address = $4, branch_id = $5, lat = $6, lng = $7, radius_m = $8, active = $9 WHERE id = $1 AND company_id = $2',
      [id, companyId, b.name, b.address || null, b.branchId ?? null, b.lat, b.lng, b.radiusM, b.active]);
    if (!u.rowCount) throw new NotFoundError('location not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'location', rowId: id, after: b });
    res.json({ ok: true });
  });

  r.delete('/locations/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const d = await db.query('DELETE FROM location WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    if (!d.rowCount) throw new NotFoundError('location not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'location', rowId: id });
    res.json({ ok: true });
  });

  /** Staff settings for field work: the field-work switch, work from home, and locations tied to a person. */
  r.get('/staff', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT e.id, e.emp_code, e.name_en, e.designation, e.field_work, e.work_from_home, b.name_en AS branch_name,
              coalesce((SELECT array_agg(location_id) FROM location_assignment la WHERE la.employee_id = e.id), '{}') AS location_ids,
              (c.pin_hash IS NOT NULL) AS kiosk_pin_set, (c.locked_until IS NOT NULL AND c.locked_until > now()) AS kiosk_pin_locked
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date LEFT JOIN branch b ON b.id = a.branch_id
         LEFT JOIN kiosk_credential c ON c.employee_id = e.id
        WHERE e.company_id = $1 AND e.status = 'active' ORDER BY e.emp_code`, [companyIdOf(req)]);
    res.json({ staff: rows.rows });
  });

  r.put('/staff/:employeeId', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = z.uuid().parse(req.params.employeeId);
    const b = z.object({ fieldWork: z.boolean().optional(), locationIds: z.array(z.uuid()).max(50).optional() }).parse(req.body);
    await assertEmployeeInCompany(db, companyId, employeeId);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      if (b.fieldWork !== undefined) await client.query('UPDATE employee SET field_work = $2 WHERE id = $1', [employeeId, b.fieldWork]);
      if (b.locationIds) {
        const own = await client.query<{ n: number }>('SELECT count(*)::int AS n FROM location WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, b.locationIds]);
        if (own.rows[0]!.n !== new Set(b.locationIds).size) throw new NotFoundError('location not found');
        await client.query('DELETE FROM location_assignment WHERE employee_id = $1', [employeeId]);
        for (const loc of new Set(b.locationIds)) await client.query('INSERT INTO location_assignment (location_id, employee_id, company_id) VALUES ($1, $2, $3)', [loc, employeeId, companyId]);
      }
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'employee', rowId: employeeId, after: { field: b } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- kiosks (HR)
  const newPairing = async (kioskId: string) => {
    const code = pairingCode();
    const expires = new Date(Date.now() + PAIRING_TTL_MIN * 60_000);
    await db.query('UPDATE kiosk SET pairing_code_hash = $2, pairing_expires_at = $3 WHERE id = $1', [kioskId, sha256Hex(`kiosk-pair|${code.replace('-', '')}`), expires]);
    return { pairingCode: code, expiresAt: expires };
  };
  const kioskBody = z.object({ name: z.string().trim().min(1).max(80), branchId: z.uuid().nullable().optional(), locationId: z.uuid().nullable().optional(), selfieRequired: z.boolean().default(true) });
  const assertLocation = async (companyId: string, locationId: string | null | undefined) => {
    if (locationId && !(await db.query('SELECT 1 FROM location WHERE id = $1 AND company_id = $2', [locationId, companyId])).rows[0]) throw new NotFoundError('location not found');
  };

  r.get('/kiosks', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT k.id, k.name, k.branch_id, b.name_en AS branch_name, k.location_id, l.name AS location_name, k.selfie_required, k.status,
              k.paired_at, k.last_seen_at, k.created_at, k.revoked_at, (k.locked_until IS NOT NULL AND k.locked_until > now()) AS locked,
              (k.pairing_expires_at IS NOT NULL AND k.pairing_expires_at > now()) AS pairing_open, k.pairing_expires_at,
              (SELECT count(*)::int FROM punch_event p WHERE p.source = 'kiosk' AND p.raw->>'kioskId' = k.id::text AND p.local_date = current_date) AS punches_today
         FROM kiosk k LEFT JOIN branch b ON b.id = k.branch_id LEFT JOIN location l ON l.id = k.location_id
        WHERE k.company_id = $1 ORDER BY k.status = 'revoked', k.created_at DESC`, [companyIdOf(req)]);
    res.json({ kiosks: rows.rows });
  });

  r.post('/kiosks', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = kioskBody.parse(req.body);
    await assertBranch(companyId, b.branchId);
    await assertLocation(companyId, b.locationId);
    const ins = await db.query<{ id: string }>(
      'INSERT INTO kiosk (company_id, name, branch_id, location_id, selfie_required, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
      [companyId, b.name, b.branchId ?? null, b.locationId ?? null, b.selfieRequired, req.user!.id]);
    const id = ins.rows[0]!.id;
    const pairing = await newPairing(id);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'kiosk', rowId: id, after: b });
    res.status(201).json({ id, ...pairing });
  });

  r.put('/kiosks/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = z.uuid().parse(req.params.id);
    const b = kioskBody.parse(req.body);
    await assertBranch(companyId, b.branchId);
    await assertLocation(companyId, b.locationId);
    const u = await db.query('UPDATE kiosk SET name = $3, branch_id = $4, location_id = $5, selfie_required = $6 WHERE id = $1 AND company_id = $2',
      [id, companyId, b.name, b.branchId ?? null, b.locationId ?? null, b.selfieRequired]);
    if (!u.rowCount) throw new NotFoundError('kiosk not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'kiosk', rowId: id, after: b });
    res.json({ ok: true });
  });

  /** A fresh one-time code (to pair a replacement tablet); the old tablet keeps working until the new one pairs. */
  r.post('/kiosks/:id/pairing-code', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const k = await db.query<{ status: string }>('SELECT status FROM kiosk WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    if (!k.rows[0]) throw new NotFoundError('kiosk not found');
    if (k.rows[0].status === 'revoked') throw new ConflictError('This kiosk was revoked. Add a new kiosk instead.');
    const pairing = await newPairing(id);
    await audit(db, { actorId: req.user!.id, action: 'kiosk_pairing_code', table: 'kiosk', rowId: id });
    res.json(pairing);
  });

  r.post('/kiosks/:id/revoke', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const u = await db.query(`UPDATE kiosk SET status = 'revoked', key_hash = NULL, pairing_code_hash = NULL, pairing_expires_at = NULL, revoked_at = now()
                               WHERE id = $1 AND company_id = $2 AND status <> 'revoked'`, [id, companyIdOf(req)]);
    if (!u.rowCount) throw new NotFoundError('kiosk not found');
    await audit(db, { actorId: req.user!.id, action: 'kiosk_revoked', table: 'kiosk', rowId: id });
    res.json({ ok: true });
  });

  r.post('/kiosks/:id/unlock', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const u = await db.query('UPDATE kiosk SET locked_until = NULL, failed_attempts = 0 WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    if (!u.rowCount) throw new NotFoundError('kiosk not found');
    await audit(db, { actorId: req.user!.id, action: 'kiosk_unlocked', table: 'kiosk', rowId: id });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- kiosk PIN and QR badge (self) / reset (HR)
  r.put('/kiosk-pin', async (req, res) => {
    const e = await employeeOf(db, req);
    if (!allow(`pin-set|${e.id}`, 10, 60 * 60_000)) throw new TooManyError('Too many PIN changes. Try again later.');
    const b = z.object({ pin: z.string().regex(/^\d{4,6}$/, 'PIN is 4 to 6 digits') }).parse(req.body);
    if (weakPin(b.pin)) throw new BadRequestError('That PIN is too easy to guess. Avoid repeated or consecutive digits.');
    await db.query(
      `INSERT INTO kiosk_credential (employee_id, company_id, pin_hash) VALUES ($1, $2, $3)
       ON CONFLICT (employee_id) DO UPDATE SET pin_hash = EXCLUDED.pin_hash, failed_count = 0, locked_until = NULL, updated_at = now()`,
      [e.id, e.company_id, await hashPassword(b.pin, pepper)]);
    await audit(db, { actorId: req.user!.id, action: 'kiosk_pin_set', table: 'kiosk_credential', rowId: e.id });
    res.json({ ok: true });
  });

  r.post('/staff/:employeeId/kiosk-reset', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = z.uuid().parse(req.params.employeeId);
    await assertEmployeeInCompany(db, companyId, employeeId);
    await db.query(
      `INSERT INTO kiosk_credential (employee_id, company_id) VALUES ($1, $2)
       ON CONFLICT (employee_id) DO UPDATE SET pin_hash = NULL, failed_count = 0, locked_until = NULL, qr_version = kiosk_credential.qr_version + 1, updated_at = now()`,
      [employeeId, companyId]);
    await audit(db, { actorId: req.user!.id, action: 'kiosk_pin_reset', table: 'kiosk_credential', rowId: employeeId });
    const userId = await userIdForEmployee(db, employeeId);
    if (userId) await notify(db, { companyId, userId, employeeId, template: 'kiosk.pin_reset', title: 'Kiosk PIN reset', body: 'HR reset your kiosk PIN. Set a new one under My attendance, Kiosk badge.' });
    res.json({ ok: true });
  });

  /** The rotating QR badge: a fresh signed code every 30 seconds, accepted once by a kiosk. */
  r.get('/kiosk-qr', async (req, res) => {
    const e = await employeeOf(db, req);
    const c = await db.query<{ qr_version: number }>(
      `INSERT INTO kiosk_credential (employee_id, company_id) VALUES ($1, $2) ON CONFLICT (employee_id) DO UPDATE SET employee_id = EXCLUDED.employee_id RETURNING qr_version`,
      [e.id, e.company_id]);
    const now = Date.now();
    const w = qrWindow(now);
    res.setHeader('Cache-Control', 'no-store');
    res.json({ token: qrToken(cipher, e.id, c.rows[0]!.qr_version, w), refreshInMs: (w + 1) * QR_WINDOW_MS - now, empCode: e.emp_code });
  });

  // ---------------------------------------------------------------- my roster and shift swaps
  const shiftInfo = async (companyId: string) => {
    const s = await db.query<{ id: string; code: string; name_en: string; start: string; end: string }>(
      `SELECT id, code, name_en, to_char(start_time, 'HH24:MI') AS start, to_char(end_time, 'HH24:MI') AS end FROM shift WHERE company_id = $1`, [companyId]);
    return new Map(s.rows.map((x) => [x.id, x]));
  };

  r.get('/my-roster', async (req, res) => {
    const e = await employeeOf(db, req);
    const q = z.object({ from: isoDate, to: isoDate }).parse(req.query);
    checkRange(q.from, q.to, 62);
    const shifts = await shiftInfo(e.company_id);
    const hol = await db.query<{ local_date: string; name_en: string }>(
      `SELECT h.local_date, h.name_en FROM holiday h WHERE h.company_id = $1 AND h.local_date BETWEEN $2 AND $3
          AND (h.branch_id IS NULL OR h.branch_id = (SELECT branch_id FROM employee_assignment WHERE employee_id = $4 AND valid @> h.local_date))`,
      [e.company_id, q.from, q.to, e.id]);
    const days = [];
    for (const d of dateRange(q.from, q.to)) {
      const eff = await effectiveShift(db, e.id, d);
      days.push({ date: d, weekday: weekdayOf(d), source: eff.source, shift: eff.shiftId ? shifts.get(eff.shiftId) ?? null : null, holidays: hol.rows.filter((h) => h.local_date === d).map((h) => h.name_en) });
    }
    res.json({ days });
  });

  /** Colleagues one could swap with on a date (same manager or same branch), with what they work that day. */
  r.get('/swap-colleagues', async (req, res) => {
    const e = await employeeOf(db, req);
    const q = z.object({ date: isoDate }).parse(req.query);
    const rows = await db.query<{ id: string; emp_code: string; name_en: string; designation: string | null }>(
      `SELECT e.id, e.emp_code, e.name_en, e.designation FROM employee e JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $3::date
        JOIN employee_assignment me ON me.employee_id = $2 AND me.valid @> $3::date
        WHERE e.company_id = $1 AND e.status = 'active' AND e.id <> $2
          AND (a.manager_id IS NOT DISTINCT FROM me.manager_id OR a.branch_id IS NOT DISTINCT FROM me.branch_id)
        ORDER BY e.name_en LIMIT 200`, [e.company_id, e.id, q.date]);
    const shifts = await shiftInfo(e.company_id);
    const mine = await effectiveShift(db, e.id, q.date);
    const out = [];
    for (const c of rows.rows) {
      const eff = await effectiveShift(db, c.id, q.date);
      out.push({ ...c, shift: eff.shiftId ? shifts.get(eff.shiftId) ?? null : null, off: !eff.shiftId, sameAsMine: eff.shiftId === mine.shiftId });
    }
    res.json({ mine: { shift: mine.shiftId ? shifts.get(mine.shiftId) ?? null : null, off: !mine.shiftId }, colleagues: out });
  });

  const swapSelect = `SELECT s.id, s.local_date, s.status, s.reason, s.colleague_note, s.created_at, s.decided_at, s.workflow_instance_id,
           s.requester_id, r.name_en AS requester_name, r.emp_code AS requester_code, s.colleague_id, c.name_en AS colleague_name, c.emp_code AS colleague_code,
           rs.code AS requester_shift_code, rs.name_en AS requester_shift_name, cs.code AS colleague_shift_code, cs.name_en AS colleague_shift_name
      FROM shift_swap s JOIN employee r ON r.id = s.requester_id JOIN employee c ON c.id = s.colleague_id
      LEFT JOIN shift rs ON rs.id = s.requester_shift_id LEFT JOIN shift cs ON cs.id = s.colleague_shift_id`;

  r.get('/swaps', async (req, res) => {
    const q = z.object({ scope: z.enum(['mine', 'team']).default('mine'), from: isoDate.optional(), to: isoDate.optional() }).parse(req.query);
    const u = req.user!;
    const companyId = companyIdOf(req);
    if (q.scope === 'team') {
      if (!hasRole(u, 'hr', 'manager')) throw new ForbiddenError('only managers and HR see team swaps');
      const rows = await db.query(
        `${swapSelect} WHERE s.company_id = $1 AND ($2::date IS NULL OR s.local_date >= $2) AND ($3::date IS NULL OR s.local_date <= $3)
           AND ($4 OR EXISTS (SELECT 1 FROM employee_assignment a WHERE a.employee_id IN (s.requester_id, s.colleague_id) AND a.manager_id = $5::uuid AND a.valid @> s.local_date))
         ORDER BY s.local_date DESC, s.created_at DESC LIMIT 500`, [companyId, q.from ?? null, q.to ?? null, hasRole(u, 'hr'), u.employeeId]);
      return res.json({ swaps: rows.rows });
    }
    if (!u.employeeId) return res.json({ swaps: [] });
    const rows = await db.query(`${swapSelect} WHERE s.company_id = $1 AND (s.requester_id = $2 OR s.colleague_id = $2) ORDER BY s.local_date DESC, s.created_at DESC LIMIT 200`, [companyId, u.employeeId]);
    return res.json({ swaps: rows.rows });
  });

  r.post('/swaps', async (req, res) => {
    const e = await employeeOf(db, req);
    const b = z.object({ colleagueId: z.uuid(), localDate: isoDate, reason: z.string().trim().max(500).optional() }).parse(req.body);
    if (b.localDate < localDateOf(Date.now())) throw new BadRequestError('Pick today or a later day.');
    if (b.colleagueId === e.id) throw new BadRequestError('Pick a colleague to swap with.');
    const c = await db.query<{ id: string; name_en: string }>(`SELECT id, name_en FROM employee WHERE id = $1 AND company_id = $2 AND status = 'active'`, [b.colleagueId, e.company_id]);
    if (!c.rows[0]) throw new NotFoundError('colleague not found');
    for (const id of [e.id, b.colleagueId]) if (await isAttendanceLocked(db, id, b.localDate)) throw new ConflictError('attendance for that day is locked by payroll');
    const mine = await effectiveShift(db, e.id, b.localDate);
    const theirs = await effectiveShift(db, b.colleagueId, b.localDate);
    if (mine.shiftId === theirs.shiftId) throw new BadRequestError(mine.shiftId ? 'You are both on the same shift that day.' : 'You are both off that day.');
    const ins = await db.query<{ id: string }>(
      `INSERT INTO shift_swap (company_id, requester_id, colleague_id, local_date, requester_shift_id, colleague_shift_id, reason)
       VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
      [e.company_id, e.id, b.colleagueId, b.localDate, mine.shiftId, theirs.shiftId, b.reason || null]).catch((err: { code?: string }) => {
      if (err.code === '23505') throw new ConflictError('You already have a swap request open for that day.');
      throw err;
    });
    const userId = await userIdForEmployee(db, b.colleagueId);
    if (userId) {
      await notify(db, {
        companyId: e.company_id, userId, employeeId: b.colleagueId, template: 'shift_swap.asked', title: 'Shift swap request',
        body: `${e.name_en} asks to swap shifts with you on ${bsLabel(b.localDate)}.`, payload: { swapId: ins.rows[0]!.id }, channels: ['push'],
      });
    }
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  const loadSwap = async (id: string, companyId: string) => {
    const s = await db.query<{ id: string; company_id: string; requester_id: string; colleague_id: string; local_date: string; status: string; requester_name: string; colleague_name: string }>(
      `SELECT s.*, r.name_en AS requester_name, c.name_en AS colleague_name FROM shift_swap s JOIN employee r ON r.id = s.requester_id JOIN employee c ON c.id = s.colleague_id
        WHERE s.id = $1 AND s.company_id = $2`, [id, companyId]);
    if (!s.rows[0]) throw new NotFoundError('swap request not found');
    return s.rows[0];
  };

  r.post('/swaps/:id/accept', async (req, res) => {
    const e = await employeeOf(db, req);
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ note: z.string().trim().max(500).optional() }).parse(req.body ?? {});
    const s = await loadSwap(id, e.company_id);
    if (s.colleague_id !== e.id) throw new NotFoundError('swap request not found');
    if (s.status !== 'awaiting_colleague') throw new ConflictError('This request was already answered.');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const inst = await startWorkflow(client, {
        companyId: s.company_id, objectType: 'shift_swap', objectId: s.id, employeeId: s.requester_id,
        title: 'Shift swap to approve', body: `${s.requester_name} and ${s.colleague_name} want to swap shifts on ${bsLabel(s.local_date)}.`,
      });
      await client.query(`UPDATE shift_swap SET status = 'pending', colleague_note = $2, workflow_instance_id = $3 WHERE id = $1`, [s.id, b.note || null, inst.id]);
      const userId = await userIdForEmployee(client, s.requester_id);
      if (userId) await notify(client, { companyId: s.company_id, userId, employeeId: s.requester_id, template: 'shift_swap.accepted', title: 'Swap accepted', body: `${s.colleague_name} agreed to swap on ${bsLabel(s.local_date)}. It now needs your manager's approval.` });
      await client.query('COMMIT');
    } catch (err) { await client.query('ROLLBACK'); throw err; } finally { client.release(); }
    res.json({ status: 'pending' });
  });

  r.post('/swaps/:id/decline', async (req, res) => {
    const e = await employeeOf(db, req);
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ note: z.string().trim().max(500).optional() }).parse(req.body ?? {});
    const s = await loadSwap(id, e.company_id);
    if (s.colleague_id !== e.id) throw new NotFoundError('swap request not found');
    if (s.status !== 'awaiting_colleague') throw new ConflictError('This request was already answered.');
    await db.query(`UPDATE shift_swap SET status = 'declined', colleague_note = $2, decided_at = now() WHERE id = $1`, [s.id, b.note || null]);
    const userId = await userIdForEmployee(db, s.requester_id);
    if (userId) await notify(db, { companyId: s.company_id, userId, employeeId: s.requester_id, template: 'shift_swap.declined', title: 'Swap declined', body: `${s.colleague_name} can't swap on ${bsLabel(s.local_date)}.` });
    res.json({ status: 'declined' });
  });

  r.post('/swaps/:id/cancel', async (req, res) => {
    const e = await employeeOf(db, req);
    const id = z.uuid().parse(req.params.id);
    const s = await loadSwap(id, e.company_id);
    if (s.requester_id !== e.id) throw new NotFoundError('swap request not found');
    if (!['awaiting_colleague', 'pending'].includes(s.status)) throw new ConflictError('This request is already closed.');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await client.query(`UPDATE shift_swap SET status = 'cancelled', decided_at = now() WHERE id = $1`, [s.id]);
      await cancelWorkflow(client, 'shift_swap', s.id);
      await client.query('COMMIT');
    } catch (err) { await client.query('ROLLBACK'); throw err; } finally { client.release(); }
    res.json({ status: 'cancelled' });
  });

  return r;
}

/** "14:05" in Nepal time. */
function nptHm(d: Date | string): string {
  const ms = typeof d === 'string' ? Date.parse(d) : d.getTime();
  return new Date(ms + 345 * 60_000).toISOString().slice(11, 16);
}
