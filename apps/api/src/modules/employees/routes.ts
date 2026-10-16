import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { assertManages, hasRole, requireRole } from '../auth/middleware.js';
import { nptToday } from '@hajiri/attendance-engine';
import { autoStartOnboarding } from '../lifecycle/service.js';
import { recordCareerEvent } from '../people/career.js';
import { diffEmployeeRows, recordFieldChanges } from '../people/history.js';
import { peoplePolicy } from '../people/routes.js';

import { isoDate } from '../../validation.js';
const PAN_RE = /^\d{9}$/;

const employeeBody = z.object({
  empCode: z.string().min(1).max(32),
  nameEn: z.string().min(1).max(120),
  nameNe: z.string().max(120).optional(),
  gender: z.enum(['male', 'female', 'other']).optional(),
  dob: isoDate.optional(),
  maritalStatus: z.enum(['single', 'couple']).optional(),
  joinDate: isoDate.optional(),
  exitDate: isoDate.nullable().optional(),
  designation: z.string().max(120).optional(),
  grade: z.string().max(40).optional(),
  phone: z.string().max(30).optional(),
  email: z.string().email().optional(),
  otEligible: z.boolean().optional(),
  workFromHome: z.boolean().optional(),
  weeklyOffDays: z.array(z.number().int().min(0).max(6)).optional(),
  regime: z.enum(['ssf', 'pf', 'none']).optional(),
  ssfId: z.string().max(20).optional(),
  pfId: z.string().max(40).optional(),
  citId: z.string().max(40).optional(),
  pan: z.string().regex(PAN_RE, 'PAN must be 9 digits').optional(),
  bankCode: z.string().max(20).optional(),
  bankBranch: z.string().max(80).optional(),
  bankAccount: z.string().min(4).max(40).optional(),
  isResident: z.boolean().optional(),
  remoteAreaClass: z.enum(['A', 'B', 'C', 'D']).nullable().optional(),
  disability: z.boolean().optional(),
  festivalMonth: z.number().int().min(1).max(12).nullable().optional(),
  probationEnd: isoDate.nullable().optional(),
  branchId: z.uuid().optional(),
  orgUnitId: z.uuid().nullable().optional(),
  managerId: z.uuid().nullable().optional(),
  employmentType: z.enum(['regular', 'work_based', 'time_based', 'casual', 'part_time']).optional(),
  shiftId: z.uuid().optional(),
});

const PUBLIC_COLS = `e.id, e.company_id, e.emp_code, e.name_en, e.name_ne, e.gender, e.dob, e.marital_status, e.join_date, e.exit_date, e.designation, e.grade,
  e.phone, e.email, e.ot_eligible, e.work_from_home, e.weekly_off_days, e.status, e.regime, e.ssf_id, e.pf_id, e.cit_id, e.bank_code, e.bank_branch, e.bank_account_last4,
  e.is_resident, e.remote_area_class, e.disability, e.festival_month, e.probation_end, e.created_at,
  (e.pan_enc IS NOT NULL) AS has_pan,
  a.branch_id, a.org_unit_id, a.manager_id, a.employment_type,
  (SELECT s.shift_id FROM shift_assignment s WHERE s.employee_id = e.id AND s.valid @> current_date) AS shift_id`;

/** Referenced records (shift, branch, org unit, manager) must belong to the caller's company; dates must be sane. */
type EmployeeInput = z.infer<typeof employeeBody>;
type EmployeePatch = { [K in keyof EmployeeInput]?: EmployeeInput[K] | undefined };

async function validateRefs(db: AppDeps['db'], companyId: string | null, b: EmployeePatch, selfId?: string): Promise<void> {
  const check = async (table: string, id: string | null | undefined, label: string) => {
    if (!id) return;
    const r = await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (r.rows.length === 0) throw new BadRequestError(`${label} does not exist in your company`);
  };
  await check('shift', b.shiftId, 'shift');
  await check('branch', b.branchId, 'branch');
  await check('org_unit', b.orgUnitId, 'org unit');
  await check('employee', b.managerId, 'manager');
  if (b.managerId && selfId && b.managerId === selfId) throw new BadRequestError('an employee cannot be their own manager');
  if (b.joinDate && b.exitDate && b.exitDate < b.joinDate) throw new BadRequestError('exit date cannot be before join date');
  if (b.probationEnd && b.joinDate && b.probationEnd < b.joinDate) throw new BadRequestError('probation end cannot be before join date');
}

/** Keep designation_id in step with the free-text designation: link it to the catalogue entry of the same name, if any. */
async function linkDesignation(db: { query: AppDeps['db']['query'] }, employeeId: string): Promise<void> {
  await db.query(
    `UPDATE employee e SET designation_id = (SELECT d.id FROM designation d WHERE d.company_id = e.company_id AND lower(d.name_en) = lower(e.designation) ORDER BY d.active DESC LIMIT 1)
      WHERE e.id = $1`, [employeeId]);
}

/** Search text is matched literally: % and _ typed by the user are not wildcards. */
export const likeEscape = (s: string) => s.replace(/[\\%_]/g, (c) => `\\${c}`);

/** Specialist admins need the employee list and cards (not PAN, bank or edits) to do payroll and attendance work. */
const READ_ALL = ['payroll_admin', 'attendance_admin'] as const;

export function employeeRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  /** emp_code of the employee already holding this PAN in the company (other than exceptId), via the blind index. */
  const panOwner = async (companyId: string, pan: string, exceptId?: string): Promise<string | null> =>
    (await db.query<{ emp_code: string }>('SELECT emp_code FROM employee WHERE company_id = $1 AND pan_bidx = $2 AND ($3::uuid IS NULL OR id <> $3) LIMIT 1',
      [companyId, cipher.blindIndex(pan), exceptId ?? null])).rows[0]?.emp_code ?? null;
  const assertPanFree = async (companyId: string, pan: string, exceptId?: string) => {
    const owner = await panOwner(companyId, pan, exceptId);
    if (owner) throw new ConflictError(`this PAN is already recorded for employee ${owner}`);
  };

  // HR (and the payroll and attendance admins, read-only) list the whole company; a manager lists the people they manage and themselves.
  r.get('/', requireRole('hr', 'manager', ...READ_ALL), async (req, res) => {
    const q = z.object({ status: z.enum(['active', 'exited']).optional(), search: z.string().max(100).optional(), branchId: z.uuid().optional(), pan: z.string().regex(PAN_RE).optional() }).parse(req.query);
    if (q.pan && !hasRole(req.user!, 'hr')) return res.status(403).json({ error: 'PAN lookup requires HR' });
    const rows = await db.query(
      `SELECT ${PUBLIC_COLS} FROM employee e
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND ($2::text IS NULL OR e.status = $2)
          AND ($3::text IS NULL OR e.emp_code ILIKE '%' || $3 || '%' OR e.name_en ILIKE '%' || $3 || '%' OR e.name_ne ILIKE '%' || $3 || '%')
          AND ($4::uuid IS NULL OR a.branch_id = $4) AND ($5::text IS NULL OR e.pan_bidx = $5)
          AND ($6 OR e.id = $7::uuid OR hj_manages($7::uuid, e.id, current_date))
        ORDER BY e.emp_code`, [req.user!.companyId, q.status ?? null, q.search ? likeEscape(q.search) : null, q.branchId ?? null, q.pan ? cipher.blindIndex(q.pan) : null,
          hasRole(req.user!, 'hr', ...READ_ALL), req.user!.employeeId]);
    if (q.pan) await audit(db, { actorId: req.user!.id, action: 'pan_lookup', table: 'employee', after: { matches: rows.rows.length } });
    res.json({ employees: rows.rows });
  });

  r.get('/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    if (!hasRole(req.user!, ...READ_ALL)) await assertManages(db, req.user!, id);
    const rows = await db.query(
      `SELECT ${PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.id = $1 AND e.company_id = $2`, [id, req.user!.companyId]);
    const emp = rows.rows[0];
    if (!emp) throw new NotFoundError('employee not found');
    res.json({ employee: emp });
  });

  /** Sensitive fields (PAN, full bank account) decrypted on demand, HR only, audited. */
  r.get('/:id/sensitive', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query<{ pan_enc: Buffer | null; bank_account_enc: Buffer | null }>(
      'SELECT pan_enc, bank_account_enc FROM employee WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    const row = rows.rows[0];
    if (!row) throw new NotFoundError('employee not found');
    await audit(db, { actorId: req.user!.id, action: 'read_sensitive', table: 'employee', rowId: id });
    res.json({
      pan: row.pan_enc ? cipher.decrypt(row.pan_enc, `employee:${id}:pan`) : null,
      bankAccount: row.bank_account_enc ? cipher.decrypt(row.bank_account_enc, `employee:${id}:bank`) : null,
    });
  });

  r.post('/', requireRole('hr'), async (req, res) => {
    const b = employeeBody.parse(req.body);
    await validateRefs(db, req.user!.companyId, b);
    if (b.pan) await assertPanFree(req.user!.companyId!, b.pan);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>(
        `INSERT INTO employee (company_id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, designation, grade, phone, email,
           ot_eligible, weekly_off_days, regime, ssf_id, pf_id, cit_id, bank_code, bank_branch, is_resident, remote_area_class, disability, festival_month, probation_end, work_from_home)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26) RETURNING id`,
        [req.user!.companyId, b.empCode, b.nameEn, b.nameNe ?? null, b.gender ?? null, b.dob ?? null, b.maritalStatus ?? null, b.joinDate ?? null,
          b.designation ?? null, b.grade ?? null, b.phone ?? null, b.email ?? null, b.otEligible ?? true, b.weeklyOffDays ?? [6], b.regime ?? 'ssf',
          b.ssfId ?? null, b.pfId ?? null, b.citId ?? null, b.bankCode ?? null, b.bankBranch ?? null, b.isResident ?? true, b.remoteAreaClass ?? null,
          b.disability ?? false, b.festivalMonth ?? null, b.probationEnd ?? null, b.workFromHome ?? false]);
      const id = ins.rows[0]!.id;
      if (b.pan) await client.query('UPDATE employee SET pan_enc = $2, pan_bidx = $3 WHERE id = $1', [id, cipher.encrypt(b.pan, `employee:${id}:pan`), cipher.blindIndex(b.pan)]);
      if (b.bankAccount) await client.query('UPDATE employee SET bank_account_enc = $2, bank_account_last4 = $3 WHERE id = $1', [id, cipher.encrypt(b.bankAccount, `employee:${id}:bank`), b.bankAccount.slice(-4)]);
      const from = b.joinDate ?? nptToday();
      await client.query(
        `INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type) VALUES ($1, daterange($2, NULL), $3, $4, $5, $6)`,
        [id, from, b.branchId ?? null, b.orgUnitId ?? null, b.managerId ?? null, b.employmentType ?? 'regular']);
      if (b.shiftId) await client.query('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange($3, NULL))', [id, b.shiftId, from]);
      if (b.designation) await linkDesignation(client, id);
      await recordCareerEvent(client, { companyId: req.user!.companyId!, employeeId: id, kind: 'hire', effectiveDate: from, details: { ...(b.designation ? { designation: b.designation } : {}), ...(b.grade ? { grade: b.grade } : {}) }, actorId: req.user!.id });
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'employee', rowId: id, after: { ...b, pan: b.pan ? '***' : undefined, bankAccount: b.bankAccount ? '***' : undefined } });
      await autoStartOnboarding(client, req.user!.companyId!, id, b.joinDate ?? nptToday(), req.user!.id);
      await client.query('COMMIT');
      res.status(201).json({ id });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.patch('/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = employeeBody.partial().parse(req.body);
    await validateRefs(db, req.user!.companyId, b, id);
    const before = await db.query(`SELECT ${PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date WHERE e.id = $1 AND e.company_id = $2`, [id, req.user!.companyId]);
    if (!before.rows[0]) throw new NotFoundError('employee not found');
    if ((b.pan || b.bankAccount) && (await peoplePolicy(db, req.user!.companyId!)).makerChecker) {
      throw new ConflictError('PAN and bank account changes need a second HR approval: edit them under the employee\'s profile (ID documents, Bank account)');
    }
    const storedJoin = (before.rows[0] as { join_date: string | null }).join_date;
    if (b.exitDate && storedJoin && !b.joinDate && b.exitDate < storedJoin) throw new BadRequestError('exit date cannot be before join date');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const sets: string[] = []; const vals: unknown[] = [id];
      const map: Array<[keyof typeof b, string]> = [
        ['empCode', 'emp_code'], ['nameEn', 'name_en'], ['nameNe', 'name_ne'], ['gender', 'gender'], ['dob', 'dob'], ['maritalStatus', 'marital_status'],
        ['joinDate', 'join_date'], ['exitDate', 'exit_date'], ['designation', 'designation'], ['grade', 'grade'], ['phone', 'phone'], ['email', 'email'],
        ['otEligible', 'ot_eligible'], ['workFromHome', 'work_from_home'], ['weeklyOffDays', 'weekly_off_days'], ['regime', 'regime'], ['ssfId', 'ssf_id'], ['pfId', 'pf_id'], ['citId', 'cit_id'],
        ['bankCode', 'bank_code'], ['bankBranch', 'bank_branch'], ['isResident', 'is_resident'], ['remoteAreaClass', 'remote_area_class'],
        ['disability', 'disability'], ['festivalMonth', 'festival_month'], ['probationEnd', 'probation_end'],
      ];
      for (const [k, col] of map) if (b[k] !== undefined) { vals.push(b[k]); sets.push(`${col} = $${vals.length}`); }
      if (b.exitDate) sets.push(`status = 'exited'`); else if (b.exitDate === null) sets.push(`status = 'active'`);
      if (b.pan) await assertPanFree(req.user!.companyId!, b.pan, id);
      if (b.pan) { vals.push(cipher.encrypt(b.pan, `employee:${id}:pan`), cipher.blindIndex(b.pan)); sets.push(`pan_enc = $${vals.length - 1}`, `pan_bidx = $${vals.length}`); }
      if (b.bankAccount) { vals.push(cipher.encrypt(b.bankAccount, `employee:${id}:bank`), b.bankAccount.slice(-4)); sets.push(`bank_account_enc = $${vals.length - 1}`, `bank_account_last4 = $${vals.length}`); }
      if (sets.length) await client.query(`UPDATE employee SET ${sets.join(', ')}, updated_at = now() WHERE id = $1`, vals);
      if (b.branchId !== undefined || b.orgUnitId !== undefined || b.managerId !== undefined || b.employmentType !== undefined) {
        // Effective-dated: close the current assignment today and open a new one (plan §9.1 decision 2).
        const today = nptToday();
        const cur = await client.query<{ valid: string; branch_id: string | null; org_unit_id: string | null; manager_id: string | null; employment_type: string }>(
          'SELECT valid, branch_id, org_unit_id, manager_id, employment_type FROM employee_assignment WHERE employee_id = $1 AND valid @> current_date', [id]);
        const c = cur.rows[0];
        const next = { branch_id: b.branchId ?? c?.branch_id ?? null, org_unit_id: b.orgUnitId === undefined ? c?.org_unit_id ?? null : b.orgUnitId,
          manager_id: b.managerId === undefined ? c?.manager_id ?? null : b.managerId, employment_type: b.employmentType ?? c?.employment_type ?? 'regular' };
        if (c) {
          const startsToday = c.valid.startsWith(`[${today}`);
          if (startsToday) await client.query('DELETE FROM employee_assignment WHERE employee_id = $1 AND valid @> current_date', [id]);
          else await client.query('UPDATE employee_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND valid @> current_date', [id, today]);
        }
        await client.query('INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type) VALUES ($1, daterange($2, NULL), $3, $4, $5, $6)',
          [id, today, next.branch_id, next.org_unit_id, next.manager_id, next.employment_type]);
      }
      if (b.shiftId) {
        const today = nptToday();
        await client.query('UPDATE shift_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND valid @> current_date AND lower(valid) < $2::date', [id, today]);
        await client.query('DELETE FROM shift_assignment WHERE employee_id = $1 AND lower(valid) = $2::date', [id, today]);
        await client.query('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange($3, NULL))', [id, b.shiftId, today]);
      }
      if (b.designation !== undefined) await linkDesignation(client, id);
      const afterRow = await client.query(`SELECT ${PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date WHERE e.id = $1`, [id]);
      const changes = await diffEmployeeRows(client, before.rows[0] as Record<string, unknown>, afterRow.rows[0] as Record<string, unknown>);
      if (b.pan) changes.push({ field: 'pan', label: 'PAN', from: (before.rows[0] as { has_pan: boolean }).has_pan ? 'Recorded' : null, to: `•••• ${b.pan.slice(-4)}` });
      await recordFieldChanges(client, { companyId: req.user!.companyId!, employeeId: id, changes: changes.filter((c) => c.field !== 'has_pan' || !b.pan), source: 'hr_edit', actorId: req.user!.id });
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'employee', rowId: id, before: before.rows[0], after: { ...b, pan: b.pan ? '***' : undefined, bankAccount: b.bankAccount ? '***' : undefined } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    const after = await db.query(`SELECT ${PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date WHERE e.id = $1`, [id]);
    res.json({ employee: after.rows[0] });
  });

  /** Excel/CSV import with row-level validation (plan §13.8). Body: text/csv with a header row. */
  r.post('/import', requireRole('hr'), async (req, res) => {
    const text = typeof req.body === 'string' ? req.body : (req.body?.csv as string | undefined);
    if (!text) throw new BadRequestError('send CSV as text/csv body or {"csv": "..."}');
    const rows = parseCsv(text.replace(/^\uFEFF/, ''));
    if (rows.length < 2) throw new BadRequestError('CSV needs a header row and at least one data row');
    if (rows.length > 5001) throw new BadRequestError('CSV has more than 5,000 data rows; split the file');
    const header = rows[0]!.map((h) => h.trim());
    const required = ['emp_code', 'name_en'];
    for (const h of required) if (!header.includes(h)) throw new BadRequestError(`missing column ${h}`);
    const errors: Array<{ row: number; message: string }> = [];
    const valid: Array<z.infer<typeof employeeBody>> = [];
    const dryRun = req.query.dryRun === 'true';
    for (let i = 1; i < rows.length; i++) {
      const rec = Object.fromEntries(header.map((h, j) => [h, rows[i]![j]?.trim() ?? '']));
      const parsed = employeeBody.safeParse({
        empCode: rec.emp_code, nameEn: rec.name_en, nameNe: rec.name_ne || undefined, gender: rec.gender || undefined, dob: rec.dob || undefined,
        maritalStatus: rec.marital_status || undefined, joinDate: rec.join_date || undefined, designation: rec.designation || undefined, grade: rec.grade || undefined,
        phone: rec.phone || undefined, email: rec.email || undefined, regime: rec.regime || undefined, ssfId: rec.ssf_id || undefined, pan: rec.pan || undefined,
        bankCode: rec.bank_code || undefined, bankBranch: rec.bank_branch || undefined, bankAccount: rec.bank_account || undefined,
        otEligible: rec.ot_eligible ? rec.ot_eligible.toLowerCase() === 'true' || rec.ot_eligible === '1' : undefined,
        workFromHome: rec.work_from_home ? ['true', '1', 'yes'].includes(rec.work_from_home.toLowerCase()) : undefined,
      });
      if (!parsed.success) { for (const is of parsed.error.issues) errors.push({ row: i + 1, message: `${is.path.join('.')}: ${is.message}` }); continue; }
      valid.push(parsed.data);
    }
    const codes = valid.map((v) => v.empCode);
    const dup = codes.filter((c, i) => codes.indexOf(c) !== i);
    for (const d of new Set(dup)) errors.push({ row: 0, message: `duplicate emp_code in file: ${d}` });
    const pans = valid.filter((v) => v.pan).map((v) => v.pan!);
    for (const d of new Set(pans.filter((x, i) => pans.indexOf(x) !== i))) errors.push({ row: 0, message: `duplicate PAN in file: *****${d.slice(-4)}` });
    for (const v of valid) {
      if (!v.pan) continue;
      const taken = await panOwner(req.user!.companyId!, v.pan);
      if (taken) errors.push({ row: 0, message: `PAN of ${v.empCode} is already recorded for ${taken}` });
    }
    if (errors.length || dryRun) return res.status(errors.length ? 400 : 200).json({ imported: 0, valid: valid.length, errors });
    const client = await db.connect();
    let imported = 0;
    try {
      await client.query('BEGIN');
      for (const b of valid) {
        const ins = await client.query<{ id: string }>(
          `INSERT INTO employee (company_id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, designation, grade, phone, email, ot_eligible, regime, ssf_id, bank_code, bank_branch, work_from_home)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) ON CONFLICT (company_id, emp_code) DO NOTHING RETURNING id`,
          [req.user!.companyId, b.empCode, b.nameEn, b.nameNe ?? null, b.gender ?? null, b.dob ?? null, b.maritalStatus ?? null, b.joinDate ?? null, b.designation ?? null,
            b.grade ?? null, b.phone ?? null, b.email ?? null, b.otEligible ?? true, b.regime ?? 'ssf', b.ssfId ?? null, b.bankCode ?? null, b.bankBranch ?? null, b.workFromHome ?? false]);
        const id = ins.rows[0]?.id;
        if (!id) { errors.push({ row: 0, message: `emp_code ${b.empCode} already exists; skipped` }); continue; }
        if (b.pan) await client.query('UPDATE employee SET pan_enc = $2, pan_bidx = $3 WHERE id = $1', [id, cipher.encrypt(b.pan, `employee:${id}:pan`), cipher.blindIndex(b.pan)]);
        if (b.bankAccount) await client.query('UPDATE employee SET bank_account_enc = $2, bank_account_last4 = $3 WHERE id = $1', [id, cipher.encrypt(b.bankAccount, `employee:${id}:bank`), b.bankAccount.slice(-4)]);
        await client.query('INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES ($1, daterange($2, NULL), $3)', [id, b.joinDate ?? nptToday(), 'regular']);
        if (b.designation) await linkDesignation(client, id);
        await recordCareerEvent(client, { companyId: req.user!.companyId!, employeeId: id, kind: 'hire', effectiveDate: b.joinDate ?? nptToday(), details: { ...(b.designation ? { designation: b.designation } : {}), ...(b.grade ? { grade: b.grade } : {}) }, actorId: req.user!.id });
        imported++;
      }
      await audit(client, { actorId: req.user!.id, action: 'import', table: 'employee', after: { imported, skipped: errors.length } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    return res.status(201).json({ imported, errors });
  });

  /** Effective-dated assignment history. */
  r.get('/:id/assignments', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    await assertManages(db, req.user!, id);
    const rows = await db.query('SELECT employee_id, valid::text, branch_id, org_unit_id, manager_id, employment_type, probation_end FROM employee_assignment WHERE employee_id = $1 ORDER BY lower(valid)', [id]);
    res.json({ assignments: rows.rows });
  });

  /** Employee-visible audit of their own record (plan §5.11). */
  r.get('/:id/audit', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    await assertManages(db, req.user!, id);
    const rows = await db.query(`SELECT id, occurred_at, actor_id, action, table_name, row_id FROM audit_log WHERE row_id = $1 ORDER BY id DESC LIMIT 200`, [id]);
    res.json({ events: rows.rows });
  });

  return r;
}

/** Minimal RFC 4180 CSV parser (quotes, escaped quotes, CRLF). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = []; let field = ''; let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!;
    if (inQ) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
      else field += c;
    } else if (c === '"') inQ = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(field); rows.push(row); row = []; field = ''; }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((f) => f.trim() !== ''));
}
