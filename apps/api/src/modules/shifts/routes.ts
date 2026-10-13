import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { hhmm, isoDate } from '../../validation.js';
import { hasRole, requireRole } from '../auth/middleware.js';
import { dateRange, enqueueRecompute } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { assertManages, nptToday } from '../workflow/service.js';
import { weekdayOf } from '@hajiri/bs-calendar';

const shiftBody = z.object({
  code: z.string().min(1).max(20),
  nameEn: z.string().min(1).max(80),
  nameNe: z.string().max(80).optional(),
  start: hhmm,
  end: hhmm,
  graceInMin: z.number().int().min(0).max(240).default(0),
  graceOutMin: z.number().int().min(0).max(240).default(0),
  earlyInWindowMin: z.number().int().min(0).max(720).default(60),
  lateOutWindowMin: z.number().int().min(0).max(720).default(120),
  breakMinutes: z.number().int().min(0).max(240).default(0),
  halfDayThresholdMin: z.number().int().min(0).max(720).default(240),
  absentThresholdMin: z.number().int().min(0).max(720).default(120),
  pairingMode: z.enum(['alternating', 'strict_log_type', 'first_last']).default('alternating'),
  roundingMin: z.number().int().min(0).max(60).default(0),
  roundingDirection: z.enum(['nearest', 'down', 'up']).default('nearest'),
});

const SHIFT_COLS = `s.id, s.code, s.name_en, s.name_ne, s.start_time::text AS start, s.end_time::text AS "end", s.grace_in_min, s.grace_out_min,
  s.early_in_window_min, s.late_out_window_min, s.break_minutes, s.half_day_threshold_min, s.absent_threshold_min, s.pairing_mode,
  s.rounding_min, s.rounding_direction, (s.end_time <= s.start_time) AS crosses_midnight,
  (SELECT count(*) FROM shift_assignment a WHERE a.shift_id = s.id AND a.valid @> current_date) AS assigned_employees`;

/** Default public holidays for BS 2083 (plan §6.10, Merokalam list); employer picks the 13/14 paid days (Sec 41). */
const HOLIDAYS_2083: Array<[string, string, string, 'all' | 'female']> = [
  ['2026-04-14', 'Nepali New Year', 'नयाँ वर्ष', 'all'],
  ['2026-05-01', 'Buddha Jayanti / Labour Day', 'बुद्ध जयन्ती / मजदुर दिवस', 'all'],
  ['2026-05-29', 'Republic Day', 'गणतन्त्र दिवस', 'all'],
  ['2026-09-14', 'Teej', 'तीज', 'female'],
  ['2026-09-19', 'Constitution Day', 'संविधान दिवस', 'all'],
  ['2026-10-04', 'Jitiya', 'जितिया', 'female'],
  ['2026-10-17', 'Dashain (Phulpati)', 'दशैं (फूलपाती)', 'all'],
  ['2026-10-18', 'Dashain (Maha Astami)', 'दशैं (महाअष्टमी)', 'all'],
  ['2026-10-19', 'Dashain (Maha Navami)', 'दशैं (महानवमी)', 'all'],
  ['2026-10-20', 'Dashain (Vijaya Dashami)', 'दशैं (विजया दशमी)', 'all'],
  ['2026-10-21', 'Dashain (Ekadashi)', 'दशैं (एकादशी)', 'all'],
  ['2026-10-22', 'Dashain (Dwadashi)', 'दशैं (द्वादशी)', 'all'],
  ['2026-10-23', 'Dashain (Trayodashi)', 'दशैं (त्रयोदशी)', 'all'],
  ['2026-11-08', 'Tihar (Kag Tihar)', 'तिहार (काग तिहार)', 'all'],
  ['2026-11-09', 'Tihar (Kukur Tihar)', 'तिहार (कुकुर तिहार)', 'all'],
  ['2026-11-10', 'Tihar (Laxmi Puja)', 'तिहार (लक्ष्मी पूजा)', 'all'],
  ['2026-11-11', 'Tihar (Govardhan Puja)', 'तिहार (गोवर्धन पूजा)', 'all'],
  ['2026-11-12', 'Tihar (Bhai Tika)', 'तिहार (भाइटीका)', 'all'],
  ['2026-11-15', 'Chhath', 'छठ', 'all'],
  ['2026-12-25', 'Christmas', 'क्रिसमस', 'all'],
];


/** Keys actually present in the request body: PATCH must not apply schema defaults to untouched fields. */
function provided<T extends Record<string, unknown>>(parsed: T, body: unknown): Partial<T> {
  const keys = body && typeof body === 'object' ? new Set(Object.keys(body as object)) : new Set<string>();
  return Object.fromEntries(Object.entries(parsed).filter(([k]) => keys.has(k))) as Partial<T>;
}

export function shiftsRoutes({ db }: AppDeps): Router {
  const r = Router();
  const companyOf = (req: { user?: { companyId: string | null } }): string => {
    if (!req.user?.companyId) throw new ForbiddenError('user is not attached to a company');
    return req.user.companyId;
  };

  async function activeEmployeeIds(companyId: string): Promise<string[]> {
    const rows = await db.query<{ id: string }>(`SELECT id FROM employee WHERE company_id = $1 AND status = 'active'`, [companyId]);
    return rows.rows.map((x) => x.id);
  }

  // ---------------------------------------------------------------- holidays (plan §5.7)
  r.get('/holidays', async (req, res) => {
    const q = z.object({ from: isoDate.optional(), to: isoDate.optional(), branchId: z.uuid().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT h.id, h.branch_id, h.local_date, h.bs_date, h.name_en, h.name_ne, h.gender_scope, h.optional FROM holiday h
        WHERE h.company_id = $1 AND ($2::date IS NULL OR h.local_date >= $2) AND ($3::date IS NULL OR h.local_date <= $3)
          AND ($4::uuid IS NULL OR h.branch_id IS NULL OR h.branch_id = $4) ORDER BY h.local_date`,
      [req.user!.companyId, q.from ?? null, q.to ?? null, q.branchId ?? null]);
    res.json({ holidays: rows.rows });
  });

  const holidayBody = z.object({
    localDate: isoDate, nameEn: z.string().min(1).max(120), nameNe: z.string().max(120).optional(), branchId: z.uuid().nullable().optional(),
    genderScope: z.enum(['all', 'female']).default('all'), optional: z.boolean().default(false),
  });

  r.post('/holidays', requireRole('hr'), async (req, res) => {
    const b = holidayBody.parse(req.body);
    // The UNIQUE constraint treats NULL branch_id as distinct, so company-wide duplicates must be refused explicitly.
    const dup = await db.query('SELECT 1 FROM holiday WHERE company_id = $1 AND branch_id IS NOT DISTINCT FROM $2 AND local_date = $3 AND name_en = $4', [req.user!.companyId, b.branchId ?? null, b.localDate, b.nameEn]);
    if (dup.rows[0]) throw new ConflictError('this holiday already exists for that date');
    const bs = await db.query<{ bs_date: string }>('SELECT bs_date FROM bs_calendar WHERE ad_date = $1', [b.localDate]);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO holiday (company_id, branch_id, local_date, bs_date, name_en, name_ne, gender_scope, optional) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
      [req.user!.companyId, b.branchId ?? null, b.localDate, bs.rows[0]?.bs_date ?? null, b.nameEn, b.nameNe ?? null, b.genderScope, b.optional]);
    const ids = await activeEmployeeIds(companyOf(req));
    await enqueueRecompute(db, ids.map((e) => ({ employeeId: e, localDate: b.localDate })), 'holiday_added');
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'holiday', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id, recomputeQueued: ids.length });
  });

  r.patch('/holidays/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = provided(holidayBody.partial().parse(req.body), req.body);
    const before = await db.query<{ local_date: string }>('SELECT local_date FROM holiday WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!before.rows[0]) throw new NotFoundError('holiday not found');
    const bs = b.localDate ? (await db.query<{ bs_date: string }>('SELECT bs_date FROM bs_calendar WHERE ad_date = $1', [b.localDate])).rows[0]?.bs_date ?? null : null;
    await db.query(
      `UPDATE holiday SET local_date = coalesce($2, local_date), bs_date = coalesce($3, bs_date), name_en = coalesce($4, name_en), name_ne = coalesce($5, name_ne),
         branch_id = CASE WHEN $8 THEN $6 ELSE branch_id END, gender_scope = coalesce($7, gender_scope), optional = coalesce($9, optional) WHERE id = $1`,
      [id, b.localDate ?? null, bs, b.nameEn ?? null, b.nameNe ?? null, b.branchId ?? null, b.genderScope ?? null, b.branchId !== undefined, b.optional ?? null]);
    const ids = await activeEmployeeIds(companyOf(req));
    const dates = new Set([before.rows[0].local_date, ...(b.localDate ? [b.localDate] : [])]);
    await enqueueRecompute(db, ids.flatMap((e) => [...dates].map((d) => ({ employeeId: e, localDate: d }))), 'holiday_changed');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'holiday', rowId: id, before: before.rows[0], after: b });
    res.json({ ok: true });
  });

  r.delete('/holidays/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const del = await db.query<{ local_date: string }>('DELETE FROM holiday WHERE id = $1 AND company_id = $2 RETURNING local_date', [id, req.user!.companyId]);
    if (!del.rows[0]) throw new NotFoundError('holiday not found');
    const ids = await activeEmployeeIds(companyOf(req));
    await enqueueRecompute(db, ids.map((e) => ({ employeeId: e, localDate: del.rows[0]!.local_date })), 'holiday_removed');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'holiday', rowId: id, before: del.rows[0] });
    res.json({ ok: true });
  });

  /** Load the default MoHA-style list for a BS year (only 2083 shipped; plan §16.7 SOP updates it each Chaitra). */
  r.post('/holidays/import-default', requireRole('hr'), async (req, res) => {
    const b = z.object({ bsYear: z.number().int() }).parse(req.body);
    if (b.bsYear !== 2083) return res.status(400).json({ error: 'default list available for BS 2083 only; add others via POST /shifts/holidays' });
    let inserted = 0;
    const insertedDates: string[] = [];
    for (const [ad, en, ne, scope] of HOLIDAYS_2083) {
      // UNIQUE with a NULL branch_id never conflicts in SQL, so idempotency is an explicit existence check.
      const ins = await db.query(
        `INSERT INTO holiday (company_id, branch_id, local_date, bs_date, name_en, name_ne, gender_scope)
         SELECT $1, NULL, $2, (SELECT bs_date FROM bs_calendar WHERE ad_date = $2), $3, $4, $5
          WHERE NOT EXISTS (SELECT 1 FROM holiday WHERE company_id = $1 AND branch_id IS NULL AND local_date = $2 AND name_en = $3)`,
        [req.user!.companyId, ad, en, ne, scope]);
      if ((ins.rowCount ?? 0) > 0) { inserted++; insertedDates.push(ad); }
    }
    if (inserted) {
      const ids = await activeEmployeeIds(companyOf(req));
      await enqueueRecompute(db, ids.flatMap((e) => insertedDates.map((ad) => ({ employeeId: e, localDate: ad }))), 'holiday_import');
    }
    await audit(db, { actorId: req.user!.id, action: 'import', table: 'holiday', after: { bsYear: b.bsYear, inserted } });
    return res.json({ inserted, total: HOLIDAYS_2083.length });
  });

  // ---------------------------------------------------------------- locations (mobile geofence, plan §5.2)
  r.get('/locations', async (req, res) => {
    const rows = await db.query('SELECT id, branch_id, name, lat, lng, radius_m FROM location WHERE company_id = $1 ORDER BY name', [req.user!.companyId]);
    res.json({ locations: rows.rows });
  });
  const locationBody = z.object({ name: z.string().min(1).max(80), branchId: z.uuid().nullable().optional(), lat: z.number().min(-90).max(90), lng: z.number().min(-180).max(180), radiusM: z.number().int().min(20).max(5000).default(150) });
  r.post('/locations', requireRole('hr'), async (req, res) => {
    const b = locationBody.parse(req.body);
    const ins = await db.query<{ id: string }>('INSERT INTO location (company_id, branch_id, name, lat, lng, radius_m) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
      [req.user!.companyId, b.branchId ?? null, b.name, b.lat, b.lng, b.radiusM]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'location', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });
  r.patch('/locations/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = provided(locationBody.partial().parse(req.body), req.body);
    const u = await db.query('UPDATE location SET name = coalesce($2, name), lat = coalesce($3, lat), lng = coalesce($4, lng), radius_m = coalesce($5, radius_m), branch_id = CASE WHEN $7 THEN $6 ELSE branch_id END WHERE id = $1 AND company_id = $8',
      [id, b.name ?? null, b.lat ?? null, b.lng ?? null, b.radiusM ?? null, b.branchId ?? null, b.branchId !== undefined, req.user!.companyId]);
    if (!u.rowCount) throw new NotFoundError('location not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'location', rowId: id, after: b });
    res.json({ ok: true });
  });
  r.delete('/locations/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const d = await db.query('DELETE FROM location WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!d.rowCount) throw new NotFoundError('location not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'location', rowId: id });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- calendar for the UI (holidays + weekly offs)
  r.get('/calendar', async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, employeeId: z.uuid().optional() }).parse(req.query);
    const dates = dateRange(q.from, q.to);
    if (dates.length > 400) return res.status(400).json({ error: 'range too long' });
    const weeklyOff = q.employeeId
      ? (await db.query<{ weekly_off_days: number[] }>('SELECT weekly_off_days FROM employee WHERE id = $1', [q.employeeId])).rows[0]?.weekly_off_days ?? [6]
      : [6];
    const hol = await db.query<{ local_date: string; name_en: string; name_ne: string | null; gender_scope: string; optional: boolean; branch_id: string | null }>(
      'SELECT local_date, name_en, name_ne, gender_scope, optional, branch_id FROM holiday WHERE company_id = $1 AND local_date BETWEEN $2 AND $3 ORDER BY local_date',
      [req.user!.companyId, q.from, q.to]);
    const bs = await db.query<{ ad_date: string; bs_date: string }>('SELECT ad_date, bs_date FROM bs_calendar WHERE ad_date BETWEEN $1 AND $2', [q.from, q.to]);
    const bsMap = new Map(bs.rows.map((x) => [x.ad_date, x.bs_date]));
    const holMap = new Map<string, typeof hol.rows>();
    for (const h of hol.rows) holMap.set(h.local_date, [...(holMap.get(h.local_date) ?? []), h]);
    return res.json({
      days: dates.map((d) => ({ date: d, bsDate: bsMap.get(d) ?? null, weekday: weekdayOf(d), weeklyOff: weeklyOff.includes(weekdayOf(d)), holidays: holMap.get(d) ?? [] })),
    });
  });

  // ---------------------------------------------------------------- roster (plan §5.3)
  r.get('/roster', requireRole('hr', 'manager'), async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, branchId: z.uuid().optional() }).parse(req.query);
    // Line managers roster their own reports only; HR/finance see the company.
    const teamOnly = !hasRole(req.user!, 'hr', 'finance');
    const emps = await db.query(
      `SELECT e.id, e.emp_code, e.name_en, e.name_ne, e.weekly_off_days,
              (SELECT s.shift_id FROM shift_assignment s WHERE s.employee_id = e.id AND s.valid @> $2::date) AS default_shift_id
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND e.status = 'active' AND ($3::uuid IS NULL OR a.branch_id = $3)
          AND (NOT $4 OR a.manager_id = $5::uuid OR e.id = $5::uuid) ORDER BY e.emp_code`,
      [req.user!.companyId, q.from, q.branchId ?? null, teamOnly, req.user!.employeeId]);
    const rows = await db.query(
      `SELECT r.employee_id, r.local_date, r.shift_id FROM roster r JOIN employee e ON e.id = r.employee_id
        LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND r.local_date BETWEEN $2 AND $3 AND (NOT $4 OR a.manager_id = $5::uuid OR e.id = $5::uuid)`,
      [req.user!.companyId, q.from, q.to, teamOnly, req.user!.employeeId]);
    res.json({ employees: emps.rows, roster: rows.rows });
  });

  r.put('/roster', requireRole('hr', 'manager'), async (req, res) => {
    const b = z.object({ rows: z.array(z.object({ employeeId: z.uuid(), localDate: isoDate, shiftId: z.uuid().nullable() })).min(1).max(5000) }).parse(req.body);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const checkedEmployees = new Set<string>();
      const checkedShifts = new Set<string>();
      for (const row of b.rows) {
        if (!checkedEmployees.has(row.employeeId)) {
          const own = await client.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [row.employeeId, req.user!.companyId]);
          if (!own.rows[0]) throw new NotFoundError(`employee ${row.employeeId} not found`);
          await assertManages(client, req.user!, row.employeeId);
          checkedEmployees.add(row.employeeId);
        }
        if (row.shiftId && !checkedShifts.has(row.shiftId)) {
          const sh = await client.query('SELECT 1 FROM shift WHERE id = $1 AND company_id = $2', [row.shiftId, req.user!.companyId]);
          if (!sh.rows[0]) throw new NotFoundError(`shift ${row.shiftId} not found`);
          checkedShifts.add(row.shiftId);
        }
        const locked = await client.query(`SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2 AND locked_at IS NOT NULL`, [row.employeeId, row.localDate]);
        if (locked.rows[0]) throw new ConflictError(`attendance for ${row.localDate} is locked by payroll`);
        await client.query(
          `INSERT INTO roster (employee_id, local_date, shift_id) VALUES ($1, $2, $3) ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id`,
          [row.employeeId, row.localDate, row.shiftId]);
      }
      await enqueueRecompute(client, b.rows.map((x) => ({ employeeId: x.employeeId, localDate: x.localDate })), 'roster_changed');
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'roster', after: { rows: b.rows.length } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ upserted: b.rows.length });
  });

  r.delete('/roster', requireRole('hr', 'manager'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), localDate: isoDate }).parse(req.body);
    await assertManages(db, req.user!, b.employeeId);
    const d = await db.query('DELETE FROM roster r USING employee e WHERE e.id = r.employee_id AND r.employee_id = $1 AND r.local_date = $2 AND e.company_id = $3', [b.employeeId, b.localDate, req.user!.companyId]);
    if (d.rowCount) await enqueueRecompute(db, [b], 'roster_removed');
    res.json({ deleted: d.rowCount ?? 0 });
  });

  /** Copy last week's roster rows onto the week starting weekStart (plan §5.3 "copy last week"). */
  r.post('/roster/copy-week', requireRole('hr', 'manager'), async (req, res) => {
    const b = z.object({ weekStart: isoDate }).parse(req.body);
    const teamOnly = !hasRole(req.user!, 'hr', 'finance');
    const rows = await db.query<{ employee_id: string; local_date: string; shift_id: string | null }>(
      `SELECT r.employee_id, r.local_date, r.shift_id FROM roster r JOIN employee e ON e.id = r.employee_id
        LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND r.local_date >= $2::date - 7 AND r.local_date < $2::date AND (NOT $3 OR a.manager_id = $4::uuid)`,
      [req.user!.companyId, b.weekStart, teamOnly, req.user!.employeeId]);
    const items: Array<{ employeeId: string; localDate: string }> = [];
    let skippedLocked = 0;
    for (const row of rows.rows) {
      const target = new Date(new Date(`${row.local_date}T00:00:00Z`).getTime() + 7 * 86_400_000).toISOString().slice(0, 10);
      const locked = await db.query('SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2 AND locked_at IS NOT NULL', [row.employee_id, target]);
      if (locked.rows[0]) { skippedLocked++; continue; }
      await db.query('INSERT INTO roster (employee_id, local_date, shift_id) VALUES ($1, $2, $3) ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id',
        [row.employee_id, target, row.shift_id]);
      items.push({ employeeId: row.employee_id, localDate: target });
    }
    await enqueueRecompute(db, items, 'roster_copied');
    await audit(db, { actorId: req.user!.id, action: 'copy_week', table: 'roster', after: { weekStart: b.weekStart, copied: items.length, skippedLocked } });
    res.json({ copied: items.length, skippedLocked });
  });

  /** Publish: tell each rostered employee their shifts for the range. */
  r.post('/roster/publish', requireRole('hr', 'manager'), async (req, res) => {
    const b = z.object({ from: isoDate, to: isoDate }).parse(req.body);
    const rows = await db.query<{ employee_id: string; n: string }>(
      `SELECT r.employee_id, count(*)::text AS n FROM roster r JOIN employee e ON e.id = r.employee_id
        WHERE e.company_id = $1 AND r.local_date BETWEEN $2 AND $3 GROUP BY r.employee_id`, [req.user!.companyId, b.from, b.to]);
    for (const row of rows.rows) {
      const userId = await userIdForEmployee(db, row.employee_id);
      await notify(db, { companyId: req.user!.companyId, userId, employeeId: row.employee_id, template: 'roster.published', title: 'Roster published',
        body: `Your shifts for ${b.from} to ${b.to} have been published (${row.n} days).`, payload: { from: b.from, to: b.to }, channels: ['push'] });
    }
    await audit(db, { actorId: req.user!.id, action: 'publish', table: 'roster', after: b });
    res.json({ notified: rows.rows.length });
  });

  // ---------------------------------------------------------------- assignments
  r.post('/assign', requireRole('hr'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), shiftId: z.uuid(), from: isoDate }).parse(req.body);
    const own = await db.query('SELECT 1 FROM employee e JOIN shift s ON s.company_id = e.company_id WHERE e.id = $1 AND s.id = $2 AND e.company_id = $3', [b.employeeId, b.shiftId, req.user!.companyId]);
    if (!own.rows[0]) throw new NotFoundError('employee or shift not found');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await client.query('DELETE FROM shift_assignment WHERE employee_id = $1 AND lower(valid) >= $2::date', [b.employeeId, b.from]);
      await client.query('UPDATE shift_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND valid @> $2::date AND lower(valid) < $2::date', [b.employeeId, b.from]);
      await client.query('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange($3, NULL))', [b.employeeId, b.shiftId, b.from]);
      await audit(client, { actorId: req.user!.id, action: 'assign', table: 'shift_assignment', rowId: b.employeeId, after: b });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    const today = nptToday();
    if (b.from <= today) await enqueueRecompute(db, dateRange(b.from < today ? today : b.from, today).map((d) => ({ employeeId: b.employeeId, localDate: d })), 'shift_assigned');
    res.status(201).json({ ok: true });
  });

  // ---------------------------------------------------------------- shift CRUD
  r.get('/', async (req, res) => {
    const rows = await db.query(`SELECT ${SHIFT_COLS} FROM shift s WHERE s.company_id = $1 ORDER BY s.code`, [req.user!.companyId]);
    res.json({ shifts: rows.rows });
  });

  r.post('/', requireRole('hr'), async (req, res) => {
    const b = shiftBody.parse(req.body);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO shift (company_id, code, name_en, name_ne, start_time, end_time, grace_in_min, grace_out_min, early_in_window_min, late_out_window_min,
         break_minutes, half_day_threshold_min, absent_threshold_min, pairing_mode, rounding_min, rounding_direction)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id`,
      [req.user!.companyId, b.code, b.nameEn, b.nameNe ?? null, b.start, b.end, b.graceInMin, b.graceOutMin, b.earlyInWindowMin, b.lateOutWindowMin,
        b.breakMinutes, b.halfDayThresholdMin, b.absentThresholdMin, b.pairingMode, b.roundingMin, b.roundingDirection]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'shift', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.get('/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query(`SELECT ${SHIFT_COLS} FROM shift s WHERE s.id = $1 AND s.company_id = $2`, [id, req.user!.companyId]);
    if (!rows.rows[0]) throw new NotFoundError('shift not found');
    res.json({ shift: rows.rows[0] });
  });

  r.patch('/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = provided(shiftBody.partial().parse(req.body), req.body);
    const before = await db.query(`SELECT ${SHIFT_COLS} FROM shift s WHERE s.id = $1 AND s.company_id = $2`, [id, req.user!.companyId]);
    if (!before.rows[0]) throw new NotFoundError('shift not found');
    const map: Array<[keyof typeof b, string]> = [
      ['code', 'code'], ['nameEn', 'name_en'], ['nameNe', 'name_ne'], ['start', 'start_time'], ['end', 'end_time'], ['graceInMin', 'grace_in_min'],
      ['graceOutMin', 'grace_out_min'], ['earlyInWindowMin', 'early_in_window_min'], ['lateOutWindowMin', 'late_out_window_min'], ['breakMinutes', 'break_minutes'],
      ['halfDayThresholdMin', 'half_day_threshold_min'], ['absentThresholdMin', 'absent_threshold_min'], ['pairingMode', 'pairing_mode'],
      ['roundingMin', 'rounding_min'], ['roundingDirection', 'rounding_direction'],
    ];
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, col] of map) if (b[k] !== undefined) { vals.push(b[k]); sets.push(`${col} = $${vals.length}`); }
    if (sets.length) await db.query(`UPDATE shift SET ${sets.join(', ')} WHERE id = $1`, vals);
    // Rule changes apply going forward: recompute unlocked days of this month for assigned employees.
    const emps = await db.query<{ employee_id: string }>('SELECT employee_id FROM shift_assignment WHERE shift_id = $1 AND valid @> current_date', [id]);
    const today = nptToday();
    const monthStart = `${today.slice(0, 8)}01`;
    await enqueueRecompute(db, emps.rows.flatMap((e) => dateRange(monthStart, today).map((d) => ({ employeeId: e.employee_id, localDate: d }))), 'shift_rules_changed');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'shift', rowId: id, before: before.rows[0], after: b });
    res.json({ ok: true, recomputeQueued: emps.rows.length });
  });

  /** Refuse to delete a shift that employees still use; the UI reassigns first (plan §13.8 destructive-action rule). */
  r.delete('/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const used = await db.query<{ assignments: string; roster: string }>(
      `SELECT (SELECT count(*) FROM shift_assignment WHERE shift_id = $1 AND upper_inf(valid))::text AS assignments, (SELECT count(*) FROM roster WHERE shift_id = $1 AND local_date >= current_date)::text AS roster`, [id]);
    const u = used.rows[0]!;
    if (Number(u.assignments) > 0 || Number(u.roster) > 0) {
      throw new ConflictError(`shift is assigned to ${u.assignments} employee(s) and ${u.roster} future roster day(s); reassign first`);
    }
    const d = await db.query('DELETE FROM shift WHERE id = $1 AND company_id = $2 AND NOT EXISTS (SELECT 1 FROM shift_assignment WHERE shift_id = $1) AND NOT EXISTS (SELECT 1 FROM roster WHERE shift_id = $1) AND NOT EXISTS (SELECT 1 FROM attendance_day WHERE shift_id = $1)', [id, req.user!.companyId]);
    if (!d.rowCount) {
      const exists = await db.query('SELECT 1 FROM shift WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
      if (!exists.rows[0]) throw new NotFoundError('shift not found');
      throw new ConflictError('shift has historical assignments or attendance; it cannot be deleted');
    }
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'shift', rowId: id });
    res.json({ ok: true });
  });

  return r;
}
