import { Router, type Request, type Response } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import { BadRequestError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { nptToday } from '../workflow/service.js';
import { toCsv } from './csv.js';
import { ADHOC_DATASETS, buildAdhocQuery, adhocDefinition, type AdhocDefinition } from './adhoc.js';

/** A report column: `date` columns are ISO in JSON and Bikram Sambat in CSV. */
export interface ReportColumn { key: string; header: string; type?: 'text' | 'number' | 'date' | 'bool' }

/** JSON for the screen (`{ columns, rows }`), or CSV with dates in BS and formula-looking cells neutralised. */
export function sendTable(res: Response, format: string | undefined, filename: string, columns: ReportColumn[], rows: Array<Record<string, unknown>>): void {
  if (format === 'csv') {
    const dateKeys = columns.filter((c) => c.type === 'date').map((c) => c.key);
    const boolKeys = columns.filter((c) => c.type === 'bool').map((c) => c.key);
    const out = rows.map((r) => {
      const x = { ...r };
      for (const k of dateKeys) x[k] = x[k] ? bsLabel(String(x[k])) : '';
      for (const k of boolKeys) x[k] = x[k] === null || x[k] === undefined ? '' : x[k] ? 'Yes' : 'No';
      return x;
    });
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="${filename.replace(/[^a-zA-Z0-9._-]/g, '_')}.csv"`);
    res.send(toCsv(columns, out));
    return;
  }
  res.json({ columns, rows });
}

const filters = z.object({
  format: z.enum(['json', 'csv']).optional(),
  branchId: uuid.optional(),
  orgUnitId: uuid.optional(),
  status: z.enum(['active', 'exited', 'all']).default('active'),
});

type Filters = z.infer<typeof filters>;

/**
 * WHERE fragment for employee-level reports: company, status, branch, department (including its sub-units) on `asOf`,
 * and for managers their direct reports and themselves. Appends its parameters to `params`.
 */
function employeeWhere(req: Request, f: Filters, params: unknown[], asOf: string, opts: { dated?: boolean } = {}): string {
  const p = (v: unknown) => { params.push(v); return `$${params.length}`; };
  const parts = [`e.company_id = ${p(companyIdOf(req))}`];
  if (!opts.dated) {
    if (f.status === 'active') parts.push(`e.status = 'active'`);
    if (f.status === 'exited') parts.push(`e.status = 'exited'`);
  }
  const asOfP = p(asOf);
  if (f.branchId) parts.push(`EXISTS (SELECT 1 FROM employee_assignment fa WHERE fa.employee_id = e.id AND fa.valid @> ${asOfP}::date AND fa.branch_id = ${p(f.branchId)})`);
  if (f.orgUnitId) {
    parts.push(`EXISTS (SELECT 1 FROM employee_assignment fa WHERE fa.employee_id = e.id AND fa.valid @> ${asOfP}::date AND fa.org_unit_id IN (
      WITH RECURSIVE sub AS (SELECT id FROM org_unit WHERE id = ${p(f.orgUnitId)} UNION ALL SELECT o.id FROM org_unit o JOIN sub ON o.parent_id = sub.id) SELECT id FROM sub))`);
  }
  if (!hasRole(req.user!, 'hr')) {
    const me = p(req.user!.employeeId ?? null);
    parts.push(`(e.id = ${me}::uuid OR EXISTS (SELECT 1 FROM employee_assignment ma WHERE ma.employee_id = e.id AND ma.manager_id = ${me}::uuid AND ma.valid @> ${asOfP}::date))`);
  }
  return parts.join(' AND ');
}

/** Current branch, department and manager names for `e` on a date. */
const PLACEMENT = (asOf: string) => `
  LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> ${asOf}::date
  LEFT JOIN branch b ON b.id = a.branch_id
  LEFT JOIN org_unit o ON o.id = a.org_unit_id
  LEFT JOIN employee m ON m.id = a.manager_id`;

const ADDRESS = (col: string) => `nullif(concat_ws(', ', ${col}->>'street', CASE WHEN ${col}->>'ward' IS NOT NULL THEN 'Ward ' || (${col}->>'ward') END,
  ${col}->>'municipality', ${col}->>'district', ${col}->>'province'), '')`;

const PEOPLE_REPORTS = ['headcount-on-date', 'statistics', 'joiners-leavers', 'birthdays-anniversaries', 'nominees', 'emergency-contacts', 'addresses',
  'education', 'licence-register', 'record-completeness'] as const;
/** Reports a manager may run for their own team; the rest hold personal data and are HR only. */
const MANAGER_REPORTS = new Set<string>(['headcount-on-date', 'joiners-leavers']);

const savedBody = z.object({
  name: z.string().trim().min(1).max(120),
  description: z.string().trim().max(500).nullable().optional(),
  shared: z.boolean().default(false),
  definition: adhocDefinition,
}).strict();

/**
 * Report catalogue additions: People record reports (HR; two team reports for managers) and the ad-hoc report builder
 * (HR). Mounted at /reports before the attendance reports router; unknown paths fall through to it.
 */
export function reportCatalogRoutes({ db }: AppDeps): Router {
  const r = Router();
  r.use(requireRole('hr', 'manager'));

  // ---------------------------------------------------------------- People record reports

  r.get('/people/:name', async (req, res, next) => {
    const name = req.params.name as typeof PEOPLE_REPORTS[number];
    if (!(PEOPLE_REPORTS as readonly string[]).includes(name)) return next();
    if (!hasRole(req.user!, 'hr') && !MANAGER_REPORTS.has(name)) throw new ForbiddenError('this report is for HR only');
    const today = nptToday();
    const f = filters.parse(req.query);
    const params: unknown[] = [];

    switch (name) {
      case 'headcount-on-date': {
        const q = z.object({ date: isoDate.default(today) }).parse(req.query);
        const where = employeeWhere(req, f, params, q.date, { dated: true });
        const d = `$${params.indexOf(q.date) + 1}`;
        const rows = await db.query(
          `SELECT coalesce(b.name_en, '(no branch)') AS branch, coalesce(o.name_en, '(no department)') AS department, coalesce(a.employment_type, '') AS employment_type,
                  count(*)::int AS headcount, count(*) FILTER (WHERE e.gender = 'female')::int AS female, count(*) FILTER (WHERE e.gender = 'male')::int AS male,
                  count(*) FILTER (WHERE e.gender IS NULL OR e.gender = 'other')::int AS other
             FROM employee e ${PLACEMENT(d)}
            WHERE ${where} AND (e.join_date IS NULL OR e.join_date <= ${d}::date) AND (e.exit_date IS NULL OR e.exit_date >= ${d}::date)
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`, params);
        return sendTable(res, f.format, `headcount-${q.date}`, [
          { key: 'branch', header: 'Branch' }, { key: 'department', header: 'Department' }, { key: 'employment_type', header: 'Employment type' },
          { key: 'headcount', header: 'Headcount', type: 'number' }, { key: 'female', header: 'Female', type: 'number' }, { key: 'male', header: 'Male', type: 'number' },
          { key: 'other', header: 'Other or not recorded', type: 'number' }], rows.rows);
      }

      case 'statistics': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query<{ dimension: string; bucket: string; sort: number; count: number }>(
          `WITH e AS (SELECT e.* FROM employee e WHERE ${where}),
                dims AS (
                  SELECT 'Age' AS dimension, CASE WHEN dob IS NULL THEN 'Not recorded' WHEN age(${t}::date, dob) < interval '25 years' THEN 'Under 25'
                    WHEN age(${t}::date, dob) < interval '35 years' THEN '25 to 34' WHEN age(${t}::date, dob) < interval '45 years' THEN '35 to 44'
                    WHEN age(${t}::date, dob) < interval '55 years' THEN '45 to 54' ELSE '55 and over' END AS bucket,
                    CASE WHEN dob IS NULL THEN 9 WHEN age(${t}::date, dob) < interval '25 years' THEN 1 WHEN age(${t}::date, dob) < interval '35 years' THEN 2
                    WHEN age(${t}::date, dob) < interval '45 years' THEN 3 WHEN age(${t}::date, dob) < interval '55 years' THEN 4 ELSE 5 END AS sort FROM e
                  UNION ALL
                  SELECT 'Length of service', CASE WHEN join_date IS NULL THEN 'Not recorded' WHEN age(${t}::date, join_date) < interval '1 year' THEN 'Under 1 year'
                    WHEN age(${t}::date, join_date) < interval '3 years' THEN '1 to 3 years' WHEN age(${t}::date, join_date) < interval '5 years' THEN '3 to 5 years'
                    WHEN age(${t}::date, join_date) < interval '10 years' THEN '5 to 10 years' ELSE '10 years or more' END,
                    CASE WHEN join_date IS NULL THEN 9 WHEN age(${t}::date, join_date) < interval '1 year' THEN 1 WHEN age(${t}::date, join_date) < interval '3 years' THEN 2
                    WHEN age(${t}::date, join_date) < interval '5 years' THEN 3 WHEN age(${t}::date, join_date) < interval '10 years' THEN 4 ELSE 5 END FROM e
                  UNION ALL
                  SELECT 'Gender', coalesce(initcap(gender), 'Not recorded'), CASE gender WHEN 'female' THEN 1 WHEN 'male' THEN 2 WHEN 'other' THEN 3 ELSE 9 END FROM e
                  UNION ALL
                  SELECT 'Marital status', CASE marital_status WHEN 'single' THEN 'Single' WHEN 'couple' THEN 'Married' ELSE 'Not recorded' END,
                    CASE marital_status WHEN 'single' THEN 1 WHEN 'couple' THEN 2 ELSE 9 END FROM e
                  UNION ALL
                  SELECT 'Province (permanent address)', coalesce(nullif(permanent_address->>'province', ''), 'Not recorded'),
                    CASE WHEN nullif(permanent_address->>'province', '') IS NULL THEN 9 ELSE 1 END FROM e
                  UNION ALL
                  SELECT 'District (permanent address)', coalesce(nullif(permanent_address->>'district', ''), 'Not recorded'),
                    CASE WHEN nullif(permanent_address->>'district', '') IS NULL THEN 9 ELSE 1 END FROM e)
           SELECT dimension, bucket, min(sort)::int AS sort, count(*)::int AS count FROM dims GROUP BY dimension, bucket
            ORDER BY CASE dimension WHEN 'Age' THEN 1 WHEN 'Length of service' THEN 2 WHEN 'Gender' THEN 3 WHEN 'Marital status' THEN 4 WHEN 'Province (permanent address)' THEN 5 ELSE 6 END,
                     sort, count DESC, bucket`, params);
        const totals = new Map<string, number>();
        for (const x of rows.rows) totals.set(x.dimension, (totals.get(x.dimension) ?? 0) + x.count);
        const out = rows.rows.map((x) => ({ dimension: x.dimension, bucket: x.bucket, count: x.count, share: totals.get(x.dimension) ? `${((x.count / totals.get(x.dimension)!) * 100).toFixed(1)}%` : '' }));
        return sendTable(res, f.format, 'people-statistics', [
          { key: 'dimension', header: 'Measure' }, { key: 'bucket', header: 'Group' }, { key: 'count', header: 'Employees', type: 'number' }, { key: 'share', header: 'Share' }], out);
      }

      case 'joiners-leavers': {
        const q = z.object({ from: isoDate, to: isoDate }).parse(req.query);
        if (q.from > q.to) throw new BadRequestError('the start date is after the end date');
        const where = employeeWhere(req, { ...f, status: 'all' }, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        params.push(q.from, q.to);
        const fromP = `$${params.length - 1}`; const toP = `$${params.length}`;
        const rows = await db.query(
          `SELECT * FROM (
             SELECT 'Joined' AS movement, e.join_date AS date, e.emp_code, e.name_en AS name, e.designation, b.name_en AS branch, o.name_en AS department, a.employment_type,
                    NULL::text AS exit_kind
               FROM employee e ${PLACEMENT(t)} WHERE ${where} AND e.join_date BETWEEN ${fromP}::date AND ${toP}::date
             UNION ALL
             SELECT 'Left', e.exit_date, e.emp_code, e.name_en, e.designation, b.name_en, o.name_en, a.employment_type,
                    (SELECT x.kind FROM employee_exit x WHERE x.employee_id = e.id AND x.status IN ('accepted', 'completed') ORDER BY x.created_at DESC LIMIT 1)
               FROM employee e ${PLACEMENT(t)} WHERE ${where} AND e.exit_date BETWEEN ${fromP}::date AND ${toP}::date) x
            ORDER BY date, emp_code`, params).catch(async (e: { code?: string }) => {
          if (e.code !== '42P01') throw e; // employee_exit missing on a very old schema
          return { rows: [] as Record<string, unknown>[] };
        });
        const out = rows.rows.map((x) => ({ ...x, exit_kind: x.exit_kind ? String(x.exit_kind).replace('_', ' ') : '' }));
        return sendTable(res, f.format, `joiners-leavers-${q.from}_${q.to}`, [
          { key: 'movement', header: 'Movement' }, { key: 'date', header: 'Date', type: 'date' }, { key: 'emp_code', header: 'Emp code' }, { key: 'name', header: 'Name' },
          { key: 'designation', header: 'Designation' }, { key: 'branch', header: 'Branch' }, { key: 'department', header: 'Department' },
          { key: 'employment_type', header: 'Employment type' }, { key: 'exit_kind', header: 'Reason for leaving' }], out);
      }

      case 'birthdays-anniversaries': {
        const q = z.object({ from: isoDate.default(today), days: z.coerce.number().int().min(1).max(92).default(30) }).parse(req.query);
        const where = employeeWhere(req, { ...f, status: 'active' }, params, today);
        params.push(q.from, q.days);
        const fromP = `$${params.length - 1}`; const daysP = `$${params.length}`;
        const t = `$${params.indexOf(today) + 1}`;
        // Each day of the window, matched against the month and day of the birth date and the joining date (29 Feb → 28 Feb).
        const rows = await db.query(
          `WITH days AS (SELECT (${fromP}::date + g)::date AS d FROM generate_series(0, ${daysP}::int - 1) g)
           SELECT * FROM (
             SELECT 'Birthday' AS occasion, days.d AS date, e.emp_code, e.name_en AS name, o.name_en AS department, b.name_en AS branch,
                    (extract(year FROM days.d) - extract(year FROM e.dob))::int AS years
               FROM employee e ${PLACEMENT(t)} JOIN days ON e.dob IS NOT NULL
                AND to_char(days.d, 'MM-DD') = CASE WHEN to_char(e.dob, 'MM-DD') = '02-29' AND NOT (extract(year FROM days.d)::int % 4 = 0) THEN '02-28' ELSE to_char(e.dob, 'MM-DD') END
              WHERE ${where}
             UNION ALL
             SELECT 'Work anniversary', days.d, e.emp_code, e.name_en, o.name_en, b.name_en, (extract(year FROM days.d) - extract(year FROM e.join_date))::int
               FROM employee e ${PLACEMENT(t)} JOIN days ON e.join_date IS NOT NULL AND e.join_date < days.d
                AND to_char(days.d, 'MM-DD') = CASE WHEN to_char(e.join_date, 'MM-DD') = '02-29' AND NOT (extract(year FROM days.d)::int % 4 = 0) THEN '02-28' ELSE to_char(e.join_date, 'MM-DD') END
              WHERE ${where}) x
            ORDER BY date, occasion, name`, params);
        return sendTable(res, f.format, `birthdays-anniversaries-${q.from}`, [
          { key: 'date', header: 'Date', type: 'date' }, { key: 'occasion', header: 'Occasion' }, { key: 'emp_code', header: 'Emp code' }, { key: 'name', header: 'Name' },
          { key: 'department', header: 'Department' }, { key: 'branch', header: 'Branch' }, { key: 'years', header: 'Years', type: 'number' }], rows.rows);
      }

      case 'nominees': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query(
          `SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, fm.name AS nominee, replace(fm.relation, '_', ' ') AS relation, fm.dob,
                  fm.nominee_share::text AS share, fm.phone
             FROM employee e ${PLACEMENT(t)} JOIN employee_family fm ON fm.employee_id = e.id AND fm.is_nominee
            WHERE ${where} ORDER BY e.emp_code, fm.sort, fm.name`, params);
        return sendTable(res, f.format, 'nominees', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' }, { key: 'nominee', header: 'Nominee' },
          { key: 'relation', header: 'Relation' }, { key: 'dob', header: 'Date of birth', type: 'date' }, { key: 'share', header: 'Share (%)' }, { key: 'phone', header: 'Phone' }], rows.rows);
      }

      case 'emergency-contacts': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query(
          `SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, e.blood_group, c.name AS contact, c.relation, c.phone, c.alt_phone, c.address
             FROM employee e ${PLACEMENT(t)} JOIN employee_emergency_contact c ON c.employee_id = e.id
            WHERE ${where} ORDER BY e.emp_code, c.sort, c.name`, params);
        return sendTable(res, f.format, 'emergency-contacts', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' }, { key: 'blood_group', header: 'Blood group' },
          { key: 'contact', header: 'Contact' }, { key: 'relation', header: 'Relation' }, { key: 'phone', header: 'Phone' }, { key: 'alt_phone', header: 'Other phone' },
          { key: 'address', header: 'Address' }], rows.rows);
      }

      case 'addresses': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query(
          `SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, b.name_en AS branch,
                  e.permanent_address->>'province' AS province, e.permanent_address->>'district' AS district,
                  ${ADDRESS('e.permanent_address')} AS permanent_address, ${ADDRESS('e.temporary_address')} AS temporary_address
             FROM employee e ${PLACEMENT(t)} WHERE ${where} ORDER BY e.emp_code`, params);
        return sendTable(res, f.format, 'addresses', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' }, { key: 'branch', header: 'Branch' },
          { key: 'province', header: 'Province' }, { key: 'district', header: 'District' }, { key: 'permanent_address', header: 'Permanent address' },
          { key: 'temporary_address', header: 'Temporary address' }], rows.rows);
      }

      case 'education': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query(
          `SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, ed.level, ed.degree, ed.field, ed.institution, ed.board, ed.start_year, ed.end_year, ed.result
             FROM employee e ${PLACEMENT(t)} JOIN employee_education ed ON ed.employee_id = e.id
            WHERE ${where} ORDER BY e.emp_code, ed.end_year DESC NULLS LAST, ed.sort`, params);
        const LEVEL: Record<string, string> = { see: 'SEE', plus_two: '+2', diploma: 'Diploma', bachelor: 'Bachelor', master: 'Master', mphil: 'MPhil', phd: 'PhD', other: 'Other' };
        const out = rows.rows.map((x) => ({ ...x, level: LEVEL[String(x.level)] ?? x.level }));
        return sendTable(res, f.format, 'education', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' }, { key: 'level', header: 'Level' },
          { key: 'degree', header: 'Degree' }, { key: 'field', header: 'Field' }, { key: 'institution', header: 'Institution' }, { key: 'board', header: 'Board or university' },
          { key: 'start_year', header: 'From', type: 'number' }, { key: 'end_year', header: 'To', type: 'number' }, { key: 'result', header: 'Result' }], out);
      }

      case 'licence-register': {
        const q = z.object({ withinDays: z.coerce.number().int().min(0).max(3650).default(90) }).parse(req.query);
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        // ID numbers are encrypted; the register shows only whether one is on file, never the number.
        const rows = await db.query<{ expiry: string | null } & Record<string, unknown>>(
          `SELECT * FROM (
             SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, 'Passport' AS document, e.passport_no_enc IS NOT NULL AS on_file, e.passport_expiry AS expiry, NULL::text AS detail
               FROM employee e ${PLACEMENT(t)} WHERE ${where} AND (e.passport_no_enc IS NOT NULL OR e.passport_expiry IS NOT NULL)
             UNION ALL
             SELECT e.emp_code, e.name_en, o.name_en, 'Driving licence', e.licence_no_enc IS NOT NULL, e.licence_expiry, NULL
               FROM employee e ${PLACEMENT(t)} WHERE ${where} AND (e.licence_no_enc IS NOT NULL OR e.licence_expiry IS NOT NULL)
             UNION ALL
             SELECT e.emp_code, e.name_en, o.name_en, 'Citizenship', true, NULL::date, nullif(concat_ws(', ', e.citizenship_district, CASE WHEN e.citizenship_issued_on IS NOT NULL THEN 'issued' END), '')
               FROM employee e ${PLACEMENT(t)} WHERE ${where} AND e.citizenship_no_enc IS NOT NULL
             UNION ALL
             SELECT e.emp_code, e.name_en, o.name_en, dc.name || ': ' || d.title, true, d.expiry_date, CASE d.status WHEN 'verified' THEN 'Verified' WHEN 'pending' THEN 'Waiting for HR' ELSE 'Rejected' END
               FROM employee e ${PLACEMENT(t)} JOIN employee_document d ON d.employee_id = e.id AND d.is_current AND d.deleted_at IS NULL AND d.expiry_date IS NOT NULL
               JOIN document_category dc ON dc.id = d.category_id
              WHERE ${where}) x
            ORDER BY expiry NULLS LAST, emp_code`, params).catch(async (e: { code?: string }) => {
          if (e.code !== '42P01') throw e;
          return { rows: [] as Array<{ expiry: string | null } & Record<string, unknown>> };
        });
        const limit = new Date(`${today}T00:00:00Z`); limit.setUTCDate(limit.getUTCDate() + q.withinDays);
        const lim = limit.toISOString().slice(0, 10);
        const out = rows.rows.map((x) => ({
          ...x, on_file: x.on_file ? 'Recorded' : 'Not recorded',
          state: !x.expiry ? 'No expiry' : x.expiry < today ? 'Expired' : x.expiry <= lim ? 'Expiring soon' : 'Valid',
        }));
        return sendTable(res, f.format, 'licence-register', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' }, { key: 'document', header: 'Document' },
          { key: 'on_file', header: 'Number' }, { key: 'detail', header: 'Detail' }, { key: 'expiry', header: 'Expires', type: 'date' }, { key: 'state', header: 'State' }], out);
      }

      case 'record-completeness': {
        const where = employeeWhere(req, f, params, today);
        const t = `$${params.indexOf(today) + 1}`;
        const rows = await db.query<{ emp_code: string; employee: string; department: string | null; checks: Record<string, boolean> }>(
          `SELECT e.emp_code, e.name_en AS employee, o.name_en AS department,
                  jsonb_build_object(
                    'Date of birth', e.dob IS NOT NULL, 'Gender', e.gender IS NOT NULL, 'Marital status', e.marital_status IS NOT NULL,
                    'Phone', coalesce(e.phone, e.personal_phone) IS NOT NULL, 'Personal email', e.personal_email IS NOT NULL,
                    'Permanent address', nullif(e.permanent_address->>'district', '') IS NOT NULL, 'Citizenship', e.citizenship_no_enc IS NOT NULL,
                    'PAN', e.pan_enc IS NOT NULL, 'Bank account', e.bank_account_enc IS NOT NULL,
                    'SSF ID', e.regime <> 'ssf' OR nullif(e.ssf_id, '') IS NOT NULL, 'Designation', nullif(e.designation, '') IS NOT NULL,
                    'Department', a.org_unit_id IS NOT NULL, 'Manager', a.manager_id IS NOT NULL,
                    'Emergency contact', EXISTS (SELECT 1 FROM employee_emergency_contact c WHERE c.employee_id = e.id),
                    'Family', EXISTS (SELECT 1 FROM employee_family fm WHERE fm.employee_id = e.id),
                    'Education', EXISTS (SELECT 1 FROM employee_education ed WHERE ed.employee_id = e.id),
                    'Photo', e.avatar_file_id IS NOT NULL) AS checks
             FROM employee e ${PLACEMENT(t)} WHERE ${where} ORDER BY e.emp_code`, params);
        const out = rows.rows.map(({ checks, ...x }) => {
          const entries = Object.entries(checks);
          const done = entries.filter(([, v]) => v).length;
          return { ...x, complete: Math.round((done / entries.length) * 100), missing: entries.filter(([, v]) => !v).map(([k]) => k).join(', ') };
        }).sort((a, b) => a.complete - b.complete || String(a.emp_code).localeCompare(String(b.emp_code)));
        return sendTable(res, f.format, 'record-completeness', [
          { key: 'emp_code', header: 'Emp code' }, { key: 'employee', header: 'Employee' }, { key: 'department', header: 'Department' },
          { key: 'complete', header: 'Complete (%)', type: 'number' }, { key: 'missing', header: 'Missing' }], out);
      }
    }
    return next();
  });

  // ---------------------------------------------------------------- ad-hoc report builder (HR)

  r.get('/adhoc/datasets', requireRole('hr'), (_req, res) => {
    res.json({
      datasets: Object.entries(ADHOC_DATASETS).map(([key, d]) => ({
        key, label: d.label, description: d.description,
        columns: Object.entries(d.columns).map(([k, c]) => ({ key: k, label: c.label, type: c.type })),
        defaultColumns: d.defaultColumns,
      })),
      operators: ['eq', 'neq', 'contains', 'starts', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
    });
  });

  const runAdhoc = async (req: Request, res: Response, def: AdhocDefinition, format: string | undefined, name: string) => {
    const csv = format === 'csv';
    const built = buildAdhocQuery(def, companyIdOf(req), nptToday(), csv ? 10_000 : 100);
    const rows = await db.query(built.sql, built.params);
    if (csv) await audit(db, { actorId: req.user!.id, action: 'export', table: 'adhoc_report', after: { dataset: def.dataset, columns: def.columns, rows: rows.rows.length } });
    if (!csv) {
      const count = await db.query<{ n: number }>(built.countSql, built.countParams);
      res.json({ columns: built.columns, rows: rows.rows, total: count.rows[0]?.n ?? rows.rows.length, limited: (count.rows[0]?.n ?? 0) > rows.rows.length });
      return;
    }
    sendTable(res, 'csv', name, built.columns, rows.rows);
  };

  r.post('/adhoc/run', requireRole('hr'), async (req, res) => {
    const q = z.object({ format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    await runAdhoc(req, res, adhocDefinition.parse(req.body), q.format, 'report');
  });

  r.get('/adhoc/saved', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT s.id, s.name, s.description, s.shared, s.definition, s.created_by = $2 AS mine, u.name AS owner_name, s.updated_at
         FROM saved_report s LEFT JOIN app_user u ON u.id = s.created_by
        WHERE s.company_id = $1 AND (s.created_by = $2 OR s.shared) ORDER BY s.name`, [companyIdOf(req), req.user!.id]);
    res.json({ reports: rows.rows });
  });

  r.post('/adhoc/saved', requireRole('hr'), async (req, res) => {
    const b = savedBody.parse(req.body);
    const row = await db.query(
      `INSERT INTO saved_report (company_id, name, description, shared, definition, created_by) VALUES ($1, $2, $3, $4, $5, $6)
       RETURNING id, name, description, shared, definition, true AS mine, updated_at`,
      [companyIdOf(req), b.name, b.description ?? null, b.shared, JSON.stringify(b.definition), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'saved_report', rowId: row.rows[0].id, after: { name: b.name, dataset: b.definition.dataset } });
    res.status(201).json({ report: row.rows[0] });
  });

  const ownSaved = async (req: Request) => {
    const id = uuid.parse(req.params.id);
    const r0 = await db.query<{ id: string; created_by: string | null; shared: boolean; definition: AdhocDefinition; name: string }>(
      'SELECT id, created_by, shared, definition, name FROM saved_report WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    const row = r0.rows[0];
    if (!row || (row.created_by !== req.user!.id && !row.shared)) throw new NotFoundError('report not found');
    return row;
  };

  r.get('/adhoc/saved/:id/run', requireRole('hr'), async (req, res) => {
    const row = await ownSaved(req);
    const q = z.object({ format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    await runAdhoc(req, res, adhocDefinition.parse(row.definition), q.format, row.name);
  });

  r.put('/adhoc/saved/:id', requireRole('hr'), async (req, res) => {
    const row = await ownSaved(req);
    if (row.created_by !== req.user!.id) throw new ForbiddenError('only the person who saved this report can change it');
    const b = savedBody.parse(req.body);
    const u = await db.query(
      `UPDATE saved_report SET name = $2, description = $3, shared = $4, definition = $5, updated_at = now() WHERE id = $1
       RETURNING id, name, description, shared, definition, true AS mine, updated_at`, [row.id, b.name, b.description ?? null, b.shared, JSON.stringify(b.definition)]);
    res.json({ report: u.rows[0] });
  });

  r.delete('/adhoc/saved/:id', requireRole('hr'), async (req, res) => {
    const row = await ownSaved(req);
    if (row.created_by !== req.user!.id) throw new ForbiddenError('only the person who saved this report can delete it');
    await db.query('DELETE FROM saved_report WHERE id = $1', [row.id]);
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'saved_report', rowId: row.id, before: { name: row.name } });
    res.json({ ok: true });
  });

  return r;
}
