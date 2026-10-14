import { Router } from 'express';
import { z } from 'zod';
import { adToBs, formatBs, weekdayOf } from '@hajiri/bs-calendar';
import { computeDay, DEFAULT_POLICY, type AttendancePolicy, type DayContext } from '@hajiri/attendance-engine';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { requireRole } from '../auth/middleware.js';
import * as repo from '../attendance/repo.js';
import { dateRange } from '../attendance/service.js';
import { policyVersion, SETTING_KEYS, settingDescriptions, settingSchemas, type SettingKey } from './schemas.js';

export function settingsRoutes({ db }: AppDeps): Router {
  const r = Router();

  /** Public-ish profile for the app header (any signed-in user). */
  r.get('/company', async (req, res) => {
    const c = await db.query<{ id: string; code: string; name_en: string; name_ne: string | null }>('SELECT id, code, name_en, name_ne FROM company WHERE id = $1', [req.user!.companyId]);
    if (!c.rows[0]) throw new NotFoundError('company not found');
    const p = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'company.profile'`, [req.user!.companyId]);
    const todayAd = new Date(Date.now() + 345 * 60_000).toISOString().slice(0, 10);
    let todayBs: string | null = null;
    try { todayBs = formatBs(adToBs(todayAd)); } catch { todayBs = null; }
    const profile = p.rows[0]?.value ?? {};
    res.json({ company: { ...c.rows[0], nameEn: (profile.nameEn as string) ?? c.rows[0].name_en, nameNe: (profile.nameNe as string) ?? c.rows[0].name_ne, logoDataUrl: profile.logoDataUrl ?? null, address: profile.address ?? null }, today: { ad: todayAd, bs: todayBs } });
  });

  r.use(requireRole('hr', 'admin'));

  r.get('/', async (req, res) => {
    const rows = await db.query<{ key: string; value: unknown; updated_at: Date; updated_by: string | null }>('SELECT key, value, updated_at, updated_by FROM company_setting WHERE company_id = $1', [req.user!.companyId]);
    const byKey = new Map(rows.rows.map((x) => [x.key, x]));
    // An unsaved company profile defaults to the company's registered name, so the value can be edited and saved back as is.
    const companyName = (await db.query<{ name_en: string }>('SELECT name_en FROM company WHERE id = $1', [req.user!.companyId])).rows[0]?.name_en ?? 'Company';
    const settings = SETTING_KEYS.map((key) => {
      const row = byKey.get(key);
      const schema = settingSchemas[key];
      const value = row ? row.value : (schema.safeParse(key === 'company.profile' ? { nameEn: companyName } : {}).data ?? null);
      return { key, description: settingDescriptions[key], value, updatedAt: row?.updated_at ?? null, updatedBy: row?.updated_by ?? null };
    });
    res.json({ settings, locked: { otCapDayMin: 240, otCapWeekMin: 1440, note: 'Labour Act 2074 Sec 30(1): overtime caps cannot be raised above 4 h/day and 24 h/week.' } });
  });

  r.put('/:key', async (req, res) => {
    const key = req.params.key as SettingKey;
    if (!SETTING_KEYS.includes(key)) throw new NotFoundError(`unknown setting ${req.params.key}`);
    const schema = settingSchemas[key];
    const parsed = schema.parse(req.body) as Record<string, unknown>;
    if (key === 'attendance.policy') parsed.version = policyVersion(parsed);
    const before = await db.query<{ value: unknown }>('SELECT value FROM company_setting WHERE company_id = $1 AND key = $2', [req.user!.companyId, key]);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [req.user!.companyId, key, JSON.stringify(parsed), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: key, before: before.rows[0]?.value ?? null, after: parsed });
    res.json({ key, value: parsed });
  });

  /**
   * "Applying to last month" preview (plan §13.8): recompute in memory with a proposed attendance policy and compare
   * late marks, half-days, absents and detected OT against what is stored. No writes.
   */
  r.post('/preview/attendance', async (req, res) => {
    const b = z.object({ policy: z.record(z.string(), z.unknown()), from: isoDate, to: isoDate, employeeIds: z.array(z.uuid()).max(200).optional() }).parse(req.body);
    const dates = dateRange(b.from, b.to);
    if (dates.length === 0 || dates.length > 31) throw new BadRequestError('preview range must be 1 to 31 days');
    const proposedRaw = settingSchemas['attendance.policy'].parse(b.policy) as Record<string, unknown>;
    const proposed: AttendancePolicy = {
      version: 'preview', holidayWork: proposedRaw.holidayWork as AttendancePolicy['holidayWork'],
      ot: { ...DEFAULT_POLICY.ot, minThresholdMin: proposedRaw.otMinThresholdMin as number, roundingMin: proposedRaw.otRoundingMin as number,
        requirePreapproval: proposedRaw.requirePreapproval as boolean, capDayMin: proposedRaw.otCapDayMin as number, capWeekMin: proposedRaw.otCapWeekMin as number,
        nightWindowEnabled: proposedRaw.nightWindowEnabled as boolean, nightPremiumRate: proposedRaw.nightPremiumRate as number },
    };
    const emps = await db.query<repo.RangeEmployee>(
      `SELECT id, gender, ot_eligible, weekly_off_days, company_id, join_date, exit_date FROM employee WHERE company_id = $1 AND status = 'active' AND ($2::uuid[] IS NULL OR id = ANY($2)) ORDER BY emp_code LIMIT 200`,
      [req.user!.companyId, b.employeeIds ?? null]);
    const counts = { late: 0, halfDay: 0, absent: 0, otDetected: 0, otMinutes: 0 };
    const current = { late: 0, halfDay: 0, absent: 0, otDetected: 0, otMinutes: 0 };
    const affected = new Map<string, number>();
    // All inputs for the range in a handful of queries; the loop below is pure computation.
    const inputs = await repo.loadRangeInputs(db, req.user!.companyId!, emps.rows, dates);
    for (const e of emps.rows) {
      for (const d of dates) {
        const x = inputs.get(`${e.id}|${d}`)!;
        const employed = (!e.join_date || d >= e.join_date) && (!e.exit_date || d <= e.exit_date);
        if (!x.shift || !employed) continue;
        const ctx: DayContext = { holiday: x.holiday, weeklyOff: e.weekly_off_days.includes(weekdayOf(d)), leave: x.leave, wfh: x.wfh, otEligible: e.ot_eligible, otPreapprovedMin: x.otPreapprovedMin, otMinutesThisWeek: x.otMinutesThisWeek };
        const day = computeDay({ employeeId: e.id, localDate: d, shift: x.shift, punches: x.punches, ctx, policy: proposed });
        if (day.lateMin > 0) counts.late++;
        if (day.status === 'HalfDay') counts.halfDay++;
        if (day.status === 'Absent') counts.absent++;
        if (day.ot.minutes > 0) { counts.otDetected++; counts.otMinutes += day.ot.minutes; }
        // "Affected" means the outcome would change, not the hash (the preview policy always hashes differently).
        const st = x.stored;
        if (st && (st.status !== day.status || st.late_min !== day.lateMin || st.ot_min !== day.ot.minutes)) affected.set(e.id, (affected.get(e.id) ?? 0) + 1);
      }
    }
    const cur = await db.query<{ late: number; half: number; absent: number; ot: number; otmin: number }>(
      `SELECT count(*) FILTER (WHERE v.late_min > 0)::int AS late, count(*) FILTER (WHERE v.status = 'HalfDay')::int AS half, count(*) FILTER (WHERE v.status = 'Absent')::int AS absent,
              count(*) FILTER (WHERE v.ot_min > 0)::int AS ot, coalesce(sum(v.ot_min), 0)::int AS otmin
         FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = $1 AND v.local_date BETWEEN $2 AND $3 AND ($4::uuid[] IS NULL OR e.id = ANY($4))`,
      [req.user!.companyId, b.from, b.to, b.employeeIds ?? null]);
    const c = cur.rows[0]!;
    Object.assign(current, { late: c.late, halfDay: c.half, absent: c.absent, otDetected: c.ot, otMinutes: c.otmin });
    res.json({ range: { from: b.from, to: b.to, days: dates.length }, employees: emps.rows.length, proposed: counts, current, affectedEmployees: affected.size,
      summary: `Applying to ${b.from}..${b.to}: ${counts.late} late marks (was ${current.late}), ${counts.halfDay} half-days (was ${current.halfDay}), ${counts.absent} absents (was ${current.absent}), OT on ${counts.otDetected} days (was ${current.otDetected}).` });
  });

  // ---------------------------------------------------------------- branches
  r.get('/branches', async (req, res) => {
    const rows = await db.query('SELECT id, code, name_en, name_ne, tz_offset_min, created_at FROM branch WHERE company_id = $1 ORDER BY code', [req.user!.companyId]);
    res.json({ branches: rows.rows });
  });
  r.post('/branches', async (req, res) => {
    const b = z.object({ code: z.string().min(1).max(20), nameEn: z.string().min(1).max(120), nameNe: z.string().max(120).optional() }).parse(req.body);
    const row = await db.query('INSERT INTO branch (company_id, code, name_en, name_ne) VALUES ($1, $2, $3, $4) RETURNING id, code, name_en, name_ne', [req.user!.companyId, b.code, b.nameEn, b.nameNe ?? null]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'branch', rowId: row.rows[0].id, after: b });
    res.status(201).json({ branch: row.rows[0] });
  });
  r.patch('/branches/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ code: z.string().min(1).max(20).optional(), nameEn: z.string().min(1).max(120).optional(), nameNe: z.string().max(120).nullable().optional() }).parse(req.body);
    const row = await db.query(`UPDATE branch SET code = coalesce($3, code), name_en = coalesce($4, name_en), name_ne = CASE WHEN $6 THEN $5 ELSE name_ne END
                                  WHERE id = $1 AND company_id = $2 RETURNING id, code, name_en, name_ne`, [id, req.user!.companyId, b.code ?? null, b.nameEn ?? null, b.nameNe ?? null, b.nameNe !== undefined]);
    if (!row.rows[0]) throw new NotFoundError('branch not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'branch', rowId: id, after: b });
    res.json({ branch: row.rows[0] });
  });

  // ---------------------------------------------------------------- org units
  r.get('/org-units', async (req, res) => {
    const rows = await db.query('SELECT id, branch_id, parent_id, kind, code, name_en, name_ne FROM org_unit WHERE company_id = $1 ORDER BY code', [req.user!.companyId]);
    res.json({ orgUnits: rows.rows });
  });
  /** Branch and parent ids in an org-unit body must belong to the caller's company, and a unit cannot be its own parent. */
  const checkOrgRefs = async (companyId: string | null, b: { branchId?: string | null | undefined; parentId?: string | null | undefined }, selfId?: string) => {
    if (b.branchId && !(await db.query('SELECT 1 FROM branch WHERE id = $1 AND company_id = $2', [b.branchId, companyId])).rows[0]) throw new NotFoundError('branch not found');
    if (b.parentId && !(await db.query('SELECT 1 FROM org_unit WHERE id = $1 AND company_id = $2', [b.parentId, companyId])).rows[0]) throw new NotFoundError('parent unit not found');
    if (selfId && b.parentId === selfId) throw new BadRequestError('an org unit cannot be its own parent');
  };
  r.post('/org-units', async (req, res) => {
    const b = z.object({ kind: z.enum(['department', 'team']), code: z.string().min(1).max(20), nameEn: z.string().min(1).max(120), nameNe: z.string().max(120).optional(), branchId: z.uuid().optional(), parentId: z.uuid().optional() }).parse(req.body);
    await checkOrgRefs(req.user!.companyId, b);
    const row = await db.query('INSERT INTO org_unit (company_id, branch_id, parent_id, kind, code, name_en, name_ne) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, branch_id, parent_id, kind, code, name_en, name_ne',
      [req.user!.companyId, b.branchId ?? null, b.parentId ?? null, b.kind, b.code, b.nameEn, b.nameNe ?? null]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'org_unit', rowId: row.rows[0].id, after: b });
    res.status(201).json({ orgUnit: row.rows[0] });
  });
  r.patch('/org-units/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ nameEn: z.string().min(1).max(120).optional(), nameNe: z.string().max(120).nullable().optional(), branchId: z.uuid().nullable().optional(), parentId: z.uuid().nullable().optional() }).parse(req.body);
    await checkOrgRefs(req.user!.companyId, b, id);
    const row = await db.query(`UPDATE org_unit SET name_en = coalesce($3, name_en), name_ne = CASE WHEN $5 THEN $4 ELSE name_ne END,
        branch_id = CASE WHEN $7 THEN $6 ELSE branch_id END, parent_id = CASE WHEN $9 THEN $8 ELSE parent_id END
      WHERE id = $1 AND company_id = $2 RETURNING id, branch_id, parent_id, kind, code, name_en, name_ne`,
      [id, req.user!.companyId, b.nameEn ?? null, b.nameNe ?? null, b.nameNe !== undefined, b.branchId ?? null, b.branchId !== undefined, b.parentId ?? null, b.parentId !== undefined]);
    if (!row.rows[0]) throw new NotFoundError('org unit not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'org_unit', rowId: id, after: b });
    res.json({ orgUnit: row.rows[0] });
  });

  return r;
}
