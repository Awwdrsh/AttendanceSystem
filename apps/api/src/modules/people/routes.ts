import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { attachFile, deleteFile, readFile, sendStoredFile } from '../files/store.js';
import { cancelWorkflow, isManagerOf } from '../workflow/service.js';
import { CAREER_LABEL, careerBody, cancelCareerEvent, recordCareerEvent, type CareerEventRow } from './career.js';
import { createChangeRequest, decryptPayload, PROFILE_CHANGE, registerProfileChangeWorkflow, type ChangeRequestRow } from './change-requests.js';
import { applySection, diffSection, parseSection, readProfile, SECRET_FIELDS, SECTION_LABEL, SENSITIVE_SECTIONS } from './profile.js';

export const PEOPLE_POLICY = 'people.policy';
const policySchema = z.object({ makerChecker: z.boolean().default(false) }).strict();

export async function peoplePolicy(db: Queryable, companyId: string): Promise<z.infer<typeof policySchema>> {
  const r = await db.query<{ value: unknown }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = $2`, [companyId, PEOPLE_POLICY]);
  return policySchema.parse(r.rows[0]?.value ?? {});
}

/** Search text is matched literally. */
const like = (s: string) => `%${s.replace(/[\\%_]/g, (c) => `\\${c}`)}%`;

/**
 * People and core HR: org structure, directory and org chart for everyone, the full employee profile (self and HR),
 * career events, profile change requests and field-level history.
 */
export function peopleRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  registerProfileChangeWorkflow(cipher);

  /** Same company, else 404 (never reveal other companies' ids). */
  const employeeIn = async (companyId: string, id: string) => {
    const e = await db.query<{ id: string; name_en: string; status: string; avatar_file_id: string | null }>(
      'SELECT id, name_en, status, avatar_file_id FROM employee WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!e.rows[0]) throw new NotFoundError('employee not found');
    return e.rows[0];
  };
  /** The employee themself or HR. */
  const selfOrHr = (req: Request, employeeId: string) => {
    if (hasRole(req.user!, 'hr') || req.user!.employeeId === employeeId) return;
    throw new ForbiddenError('only the employee and HR can see this');
  };
  const tx = async <T>(fn: (c: Queryable) => Promise<T>): Promise<T> => {
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const out = await fn(client);
      await client.query('COMMIT');
      return out;
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  };

  // ---------------------------------------------------------------- directory, summary, org chart (everyone)

  const DIRECTORY_COLS = `e.id, e.emp_code, e.name_en, e.designation, e.email AS work_email, e.work_phone, e.join_date, e.avatar_file_id IS NOT NULL AS has_avatar,
    a.branch_id, b.name_en AS branch_name, a.org_unit_id, o.name_en AS unit_name, a.manager_id, m.name_en AS manager_name`;
  const DIRECTORY_FROM = `employee e
    LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
    LEFT JOIN branch b ON b.id = a.branch_id LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN employee m ON m.id = a.manager_id`;

  r.get('/directory', async (req, res) => {
    const q = z.object({ search: z.string().trim().max(100).optional(), orgUnitId: z.uuid().optional(), branchId: z.uuid().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT ${DIRECTORY_COLS} FROM ${DIRECTORY_FROM}
        WHERE e.company_id = $1 AND e.status = 'active'
          AND ($2::text IS NULL OR e.name_en ILIKE $2 OR e.emp_code ILIKE $2 OR e.designation ILIKE $2 OR o.name_en ILIKE $2 OR array_to_string(e.skills, ' ') ILIKE $2)
          AND ($3::uuid IS NULL OR a.org_unit_id = $3) AND ($4::uuid IS NULL OR a.branch_id = $4)
        ORDER BY e.name_en LIMIT 1000`, [companyIdOf(req), q.search ? like(q.search) : null, q.orgUnitId ?? null, q.branchId ?? null]);
    res.json({ people: rows.rows });
  });

  r.get('/summary', async (req, res) => {
    const companyId = companyIdOf(req);
    const totals = await db.query<{ active: number; joined_30d: number; on_probation: number; leaving: number }>(
      `SELECT count(*) FILTER (WHERE e.status = 'active')::int AS active,
              count(*) FILTER (WHERE e.status = 'active' AND e.join_date > current_date - 30)::int AS joined_30d,
              count(*) FILTER (WHERE e.status = 'active' AND e.confirmed_on IS NULL AND coalesce(a.probation_end, e.probation_end) >= current_date)::int AS on_probation,
              count(*) FILTER (WHERE e.status = 'active' AND e.exit_date >= current_date)::int AS leaving
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date WHERE e.company_id = $1`, [companyId]);
    const byUnit = await db.query(
      `SELECT o.id, o.name_en AS name, count(e.id)::int AS count FROM org_unit o
         LEFT JOIN employee_assignment a ON a.org_unit_id = o.id AND a.valid @> current_date
         LEFT JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
        WHERE o.company_id = $1 GROUP BY o.id, o.name_en ORDER BY count(e.id) DESC, o.name_en`, [companyId]);
    const byBranch = await db.query(
      `SELECT b.id, b.name_en AS name, count(e.id)::int AS count FROM branch b
         LEFT JOIN employee_assignment a ON a.branch_id = b.id AND a.valid @> current_date
         LEFT JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
        WHERE b.company_id = $1 GROUP BY b.id, b.name_en ORDER BY count(e.id) DESC, b.name_en`, [companyId]);
    const t = totals.rows[0]!;
    res.json({ active: t.active, joinedLast30Days: t.joined_30d, onProbation: t.on_probation, leaving: t.leaving, byUnit: byUnit.rows, byBranch: byBranch.rows });
  });

  r.get('/org-chart', async (req, res) => {
    const rows = await db.query(
      `SELECT e.id, e.name_en, e.designation, e.avatar_file_id IS NOT NULL AS has_avatar, o.name_en AS unit_name,
              CASE WHEN m.status = 'active' THEN a.manager_id END AS manager_id
         FROM employee e
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
         LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN employee m ON m.id = a.manager_id
        WHERE e.company_id = $1 AND e.status = 'active' ORDER BY e.name_en`, [companyIdOf(req)]);
    res.json({ nodes: rows.rows });
  });

  // ---------------------------------------------------------------- org structure

  r.get('/structure', async (req, res) => {
    const companyId = companyIdOf(req);
    const [branches, units, designations] = await Promise.all([
      db.query(
        `SELECT b.id, b.code, b.name_en, b.address, b.phone, b.head_employee_id, h.name_en AS head_name,
                (SELECT count(*)::int FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.status = 'active' WHERE a.branch_id = b.id AND a.valid @> current_date) AS headcount
           FROM branch b LEFT JOIN employee h ON h.id = b.head_employee_id WHERE b.company_id = $1 ORDER BY b.code`, [companyId]),
      db.query(
        `SELECT o.id, o.code, o.name_en, o.kind, o.parent_id, o.branch_id, o.description, o.head_employee_id, h.name_en AS head_name,
                (SELECT count(*)::int FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.status = 'active' WHERE a.org_unit_id = o.id AND a.valid @> current_date) AS headcount
           FROM org_unit o LEFT JOIN employee h ON h.id = o.head_employee_id WHERE o.company_id = $1 ORDER BY o.name_en`, [companyId]),
      db.query(
        `SELECT d.id, d.code, d.name_en, d.grade, d.job_level, d.org_unit_id, d.description, d.active,
                (SELECT count(*)::int FROM employee e WHERE e.designation_id = d.id AND e.status = 'active') AS headcount
           FROM designation d WHERE d.company_id = $1 ORDER BY d.job_level NULLS LAST, d.name_en`, [companyId]),
    ]);
    res.json({ branches: branches.rows, units: units.rows, designations: designations.rows });
  });

  const hr = requireRole('hr');
  const ownedOrNull = async (companyId: string, table: 'branch' | 'org_unit' | 'employee' | 'designation', id: string | null | undefined, label: string) => {
    if (!id) return;
    const x = await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (!x.rows[0]) throw new BadRequestError(`${label} does not exist in your company`);
  };
  /** Codes are unique per company; say which one clashed instead of a bare "already exists". */
  const dupCode = (what: string) => (e: unknown): never => {
    if ((e as { code?: string }).code === '23505') throw new ConflictError(`another ${what} already uses this code`);
    throw e;
  };
  const stillUsed = (e: unknown, what: string): never => {
    if ((e as { code?: string }).code === '23503') throw new ConflictError(`this ${what} is still used by other records and cannot be deleted`);
    throw e;
  };

  const branchBody = z.object({
    code: z.string().trim().min(1).max(20), nameEn: z.string().trim().min(1).max(120),
    address: z.string().trim().max(300).nullable().optional(), phone: z.string().trim().max(30).nullable().optional(), headEmployeeId: z.uuid().nullable().optional(),
  }).strict();
  r.post('/structure/branches', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = branchBody.parse(req.body);
    await ownedOrNull(companyId, 'employee', b.headEmployeeId, 'branch head');
    const row = await db.query('INSERT INTO branch (company_id, code, name_en, address, phone, head_employee_id) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, code, name_en',
      [companyId, b.code, b.nameEn, b.address ?? null, b.phone ?? null, b.headEmployeeId ?? null]).catch(dupCode('branch'));
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'branch', rowId: row.rows[0].id, after: b });
    res.status(201).json({ branch: row.rows[0] });
  });
  r.patch('/structure/branches/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const b = branchBody.partial().parse(req.body);
    await ownedOrNull(companyId, 'employee', b.headEmployeeId, 'branch head');
    const row = await db.query(
      `UPDATE branch SET code = coalesce($3, code), name_en = coalesce($4, name_en),
              address = CASE WHEN $6 THEN $5 ELSE address END, phone = CASE WHEN $8 THEN $7 ELSE phone END,
              head_employee_id = CASE WHEN $10 THEN $9::uuid ELSE head_employee_id END
        WHERE id = $1 AND company_id = $2 RETURNING id, code, name_en`,
      [id, companyId, b.code ?? null, b.nameEn ?? null, b.address ?? null, b.address !== undefined, b.phone ?? null, b.phone !== undefined, b.headEmployeeId ?? null, b.headEmployeeId !== undefined]).catch(dupCode('branch'));
    if (!row.rows[0]) throw new NotFoundError('branch not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'branch', rowId: id, after: b });
    res.json({ branch: row.rows[0] });
  });
  r.delete('/structure/branches/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const used = await db.query('SELECT 1 FROM employee_assignment WHERE branch_id = $1 LIMIT 1', [id]);
    if (used.rows[0]) throw new ConflictError('people are (or were) assigned to this branch; move them before deleting it');
    const d = await db.query('DELETE FROM branch WHERE id = $1 AND company_id = $2', [id, companyId]).catch((e: unknown) => stillUsed(e, 'branch'));
    if (!d.rowCount) throw new NotFoundError('branch not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'branch', rowId: id });
    res.json({ ok: true });
  });

  const unitBody = z.object({
    kind: z.enum(['department', 'team']), code: z.string().trim().min(1).max(20), nameEn: z.string().trim().min(1).max(120),
    parentId: z.uuid().nullable().optional(), branchId: z.uuid().nullable().optional(), headEmployeeId: z.uuid().nullable().optional(), description: z.string().trim().max(500).nullable().optional(),
  }).strict();
  /** A unit may not sit under itself or under one of its own sub-units. */
  const assertNoCycle = async (companyId: string, selfId: string, parentId: string | null | undefined) => {
    let cur = parentId ?? null; let guard = 0;
    while (cur && guard++ < 100) {
      if (cur === selfId) throw new BadRequestError('a unit cannot sit under itself or one of its own sub-units');
      cur = (await db.query<{ parent_id: string | null }>('SELECT parent_id FROM org_unit WHERE id = $1 AND company_id = $2', [cur, companyId])).rows[0]?.parent_id ?? null;
    }
  };
  r.post('/structure/units', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = unitBody.parse(req.body);
    await ownedOrNull(companyId, 'org_unit', b.parentId, 'parent unit');
    await ownedOrNull(companyId, 'branch', b.branchId, 'branch');
    await ownedOrNull(companyId, 'employee', b.headEmployeeId, 'unit head');
    const row = await db.query(
      'INSERT INTO org_unit (company_id, kind, code, name_en, parent_id, branch_id, head_employee_id, description) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, code, name_en, kind',
      [companyId, b.kind, b.code, b.nameEn, b.parentId ?? null, b.branchId ?? null, b.headEmployeeId ?? null, b.description ?? null]).catch(dupCode('department or team'));
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'org_unit', rowId: row.rows[0].id, after: b });
    res.status(201).json({ unit: row.rows[0] });
  });
  r.patch('/structure/units/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const b = unitBody.partial().parse(req.body);
    await ownedOrNull(companyId, 'org_unit', b.parentId, 'parent unit');
    await ownedOrNull(companyId, 'branch', b.branchId, 'branch');
    await ownedOrNull(companyId, 'employee', b.headEmployeeId, 'unit head');
    if (b.parentId !== undefined) await assertNoCycle(companyId, id, b.parentId);
    const row = await db.query(
      `UPDATE org_unit SET kind = coalesce($3, kind), code = coalesce($4, code), name_en = coalesce($5, name_en),
              parent_id = CASE WHEN $7 THEN $6::uuid ELSE parent_id END, branch_id = CASE WHEN $9 THEN $8::uuid ELSE branch_id END,
              head_employee_id = CASE WHEN $11 THEN $10::uuid ELSE head_employee_id END, description = CASE WHEN $13 THEN $12 ELSE description END
        WHERE id = $1 AND company_id = $2 RETURNING id, code, name_en, kind`,
      [id, companyId, b.kind ?? null, b.code ?? null, b.nameEn ?? null, b.parentId ?? null, b.parentId !== undefined, b.branchId ?? null, b.branchId !== undefined,
        b.headEmployeeId ?? null, b.headEmployeeId !== undefined, b.description ?? null, b.description !== undefined]).catch(dupCode('department or team'));
    if (!row.rows[0]) throw new NotFoundError('unit not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'org_unit', rowId: id, after: b });
    res.json({ unit: row.rows[0] });
  });
  r.delete('/structure/units/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const kids = await db.query('SELECT 1 FROM org_unit WHERE parent_id = $1 LIMIT 1', [id]);
    if (kids.rows[0]) throw new ConflictError('this unit has sub-units; move or delete them first');
    const used = await db.query('SELECT 1 FROM employee_assignment WHERE org_unit_id = $1 LIMIT 1', [id]);
    if (used.rows[0]) throw new ConflictError('people are (or were) assigned to this unit; move them before deleting it');
    const d = await tx(async (c) => {
      await c.query('UPDATE designation SET org_unit_id = NULL WHERE org_unit_id = $1 AND company_id = $2', [id, companyId]);
      const out = await c.query('DELETE FROM org_unit WHERE id = $1 AND company_id = $2', [id, companyId]);
      if (out.rowCount) await audit(c, { actorId: req.user!.id, action: 'delete', table: 'org_unit', rowId: id });
      return out;
    }).catch((e: unknown) => stillUsed(e, 'unit'));
    if (!d.rowCount) throw new NotFoundError('unit not found');
    res.json({ ok: true });
  });

  const designationBody = z.object({
    code: z.string().trim().min(1).max(20), nameEn: z.string().trim().min(1).max(120), grade: z.string().trim().max(40).nullable().optional(),
    jobLevel: z.number().int().min(1).max(20).nullable().optional(), orgUnitId: z.uuid().nullable().optional(), description: z.string().trim().max(500).nullable().optional(),
    active: z.boolean().optional(),
  }).strict();
  r.post('/structure/designations', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = designationBody.parse(req.body);
    await ownedOrNull(companyId, 'org_unit', b.orgUnitId, 'department');
    const row = await db.query(
      'INSERT INTO designation (company_id, code, name_en, grade, job_level, org_unit_id, description, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, code, name_en, grade, job_level',
      [companyId, b.code, b.nameEn, b.grade ?? null, b.jobLevel ?? null, b.orgUnitId ?? null, b.description ?? null, b.active ?? true]).catch(dupCode('designation'));
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'designation', rowId: row.rows[0].id, after: b });
    res.status(201).json({ designation: row.rows[0] });
  });
  r.patch('/structure/designations/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const b = designationBody.partial().parse(req.body);
    await ownedOrNull(companyId, 'org_unit', b.orgUnitId, 'department');
    const row = await tx(async (c) => {
      const u = await c.query<{ id: string; name_en: string }>(
        `UPDATE designation SET code = coalesce($3, code), name_en = coalesce($4, name_en), grade = CASE WHEN $6 THEN $5 ELSE grade END,
                job_level = CASE WHEN $8 THEN $7::smallint ELSE job_level END, org_unit_id = CASE WHEN $10 THEN $9::uuid ELSE org_unit_id END,
                description = CASE WHEN $12 THEN $11 ELSE description END, active = coalesce($13, active)
          WHERE id = $1 AND company_id = $2 RETURNING id, code, name_en, grade, job_level`,
        [id, companyId, b.code ?? null, b.nameEn ?? null, b.grade ?? null, b.grade !== undefined, b.jobLevel ?? null, b.jobLevel !== undefined,
          b.orgUnitId ?? null, b.orgUnitId !== undefined, b.description ?? null, b.description !== undefined, b.active ?? null]).catch(dupCode('designation'));
      if (!u.rows[0]) throw new NotFoundError('designation not found');
      // Renaming a designation renames it on everyone who holds it.
      if (b.nameEn) await c.query('UPDATE employee SET designation = $2, updated_at = now() WHERE designation_id = $1 AND designation IS DISTINCT FROM $2', [id, b.nameEn]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'designation', rowId: id, after: b });
      return u.rows[0];
    });
    res.json({ designation: row });
  });
  r.delete('/structure/designations/:id', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const used = await db.query('SELECT 1 FROM employee WHERE designation_id = $1 LIMIT 1', [id]);
    if (used.rows[0]) throw new ConflictError('people hold this designation; mark it inactive instead');
    const d = await db.query('DELETE FROM designation WHERE id = $1 AND company_id = $2', [id, companyId]).catch((e: unknown) => stillUsed(e, 'designation'));
    if (!d.rowCount) throw new NotFoundError('designation not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'designation', rowId: id });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- policy

  r.get('/policy', hr, async (req, res) => { res.json({ policy: await peoplePolicy(db, companyIdOf(req)) }); });
  r.put('/policy', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const p = policySchema.parse(req.body);
    const before = await peoplePolicy(db, companyId);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [companyId, PEOPLE_POLICY, JSON.stringify(p), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: PEOPLE_POLICY, before, after: p });
    res.json({ policy: p });
  });

  // ---------------------------------------------------------------- my change requests and photo

  const changeBody = z.object({
    section: z.string().max(40), value: z.unknown(), reason: z.string().trim().max(500).optional(), proofFileId: z.uuid().optional(),
  }).strict();
  const REQ_COLS = ['id', 'company_id', 'employee_id', 'requested_by', 'section', 'summary', 'reason', 'proof_file_id', 'status', 'decided_by', 'decided_at', 'decision_reason', 'created_at'];

  r.post('/me/change-requests', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new ForbiddenError('only employees have a profile to change');
    const b = changeBody.parse(req.body);
    const { section, value } = parseSection(b.section, b.value);
    const row = await tx(async (c) => {
      if (b.proofFileId) await attachFile(c, { companyId, fileId: b.proofFileId, userId: req.user!.id, purpose: 'profile_change', attachedTo: `profile_change:${employeeId}` });
      const created = await createChangeRequest(c, cipher, { companyId, employeeId, requestedBy: req.user!.id, section, value, reason: b.reason ?? null, proofFileId: b.proofFileId ?? null, byHr: false });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'profile_change_request', rowId: created.id, after: { section, fields: created.summary.changes?.map((x) => x.label) } });
      return created;
    });
    res.status(201).json({ request: { ...row, sectionLabel: SECTION_LABEL[row.section] } });
  });

  r.get('/me/change-requests', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) return res.json({ requests: [] });
    const rows = await db.query<ChangeRequestRow>(`SELECT ${REQ_COLS.join(', ')} FROM profile_change_request WHERE employee_id = $1 AND company_id = $2 ORDER BY created_at DESC LIMIT 100`,
      [employeeId, companyIdOf(req)]);
    return res.json({ requests: rows.rows.map((x) => ({ ...x, sectionLabel: SECTION_LABEL[x.section] })) });
  });

  r.post('/me/change-requests/:id/cancel', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    await tx(async (c) => {
      const row = await c.query<ChangeRequestRow>('SELECT * FROM profile_change_request WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const x = row.rows[0];
      if (!x || (x.employee_id !== req.user!.employeeId && x.requested_by !== req.user!.id)) throw new NotFoundError('change request not found');
      if (x.status !== 'pending') throw new ConflictError(`this request is already ${x.status}`);
      await c.query(`UPDATE profile_change_request SET status = 'cancelled', decided_at = now(), decided_by = $2 WHERE id = $1`, [id, req.user!.id]);
      await cancelWorkflow(c, PROFILE_CHANGE, id);
    });
    res.json({ ok: true });
  });

  const avatarBody = z.object({ fileId: z.uuid() }).strict();
  const setAvatar = async (req: Request, employeeId: string, fileId: string | null) => {
    const companyId = companyIdOf(req);
    await tx(async (c) => {
      const emp = await c.query<{ avatar_file_id: string | null }>('SELECT avatar_file_id FROM employee WHERE id = $1 AND company_id = $2 FOR UPDATE', [employeeId, companyId]);
      if (!emp.rows[0]) throw new NotFoundError('employee not found');
      if (fileId) await attachFile(c, { companyId, fileId, userId: req.user!.id, purpose: 'avatar', attachedTo: `employee_avatar:${employeeId}` });
      const old = emp.rows[0].avatar_file_id;
      await c.query('UPDATE employee SET avatar_file_id = $2, updated_at = now() WHERE id = $1', [employeeId, fileId]);
      if (old && old !== fileId) await deleteFile(c, companyId, old);
      await audit(c, { actorId: req.user!.id, action: fileId ? 'update' : 'delete', table: 'employee_avatar', rowId: employeeId });
    });
  };
  r.post('/me/avatar', async (req, res) => {
    if (!req.user!.employeeId) throw new ForbiddenError('only employees have a profile photo');
    await setAvatar(req, req.user!.employeeId, avatarBody.parse(req.body).fileId);
    res.json({ ok: true });
  });
  r.delete('/me/avatar', async (req, res) => {
    if (!req.user!.employeeId) throw new ForbiddenError('only employees have a profile photo');
    await setAvatar(req, req.user!.employeeId, null);
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- change requests (HR)

  r.get('/change-requests', hr, async (req, res) => {
    const q = z.object({ status: z.enum(['pending', 'approved', 'rejected', 'cancelled', 'all']).default('pending') }).parse(req.query);
    const rows = await db.query<ChangeRequestRow>(
      `SELECT ${REQ_COLS.map((c) => `p.${c}`).join(', ')}, e.name_en AS employee_name, e.emp_code, u.name AS requested_by_name, w.id AS instance_id
         FROM profile_change_request p JOIN employee e ON e.id = p.employee_id LEFT JOIN app_user u ON u.id = p.requested_by
         LEFT JOIN workflow_instance w ON w.object_type = '${PROFILE_CHANGE}' AND w.object_id = p.id
        WHERE p.company_id = $1 AND ($2 = 'all' OR p.status = $2) ORDER BY p.created_at DESC LIMIT 300`, [companyIdOf(req), q.status]);
    res.json({ requests: rows.rows.map((x) => ({ ...x, sectionLabel: SECTION_LABEL[x.section] })) });
  });

  const loadRequest = async (req: Request, id: string) => {
    const r0 = await db.query<ChangeRequestRow & { payload_enc: Buffer }>('SELECT * FROM profile_change_request WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    const row = r0.rows[0];
    if (!row) throw new NotFoundError('change request not found');
    if (!hasRole(req.user!, 'hr') && row.employee_id !== req.user!.employeeId) throw new NotFoundError('change request not found');
    return row;
  };

  r.get('/change-requests/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const row = await loadRequest(req, id);
    const reveal = req.query.reveal === '1' && hasRole(req.user!, 'hr');
    const changes = row.status === 'pending'
      ? await diffSection(db, cipher, row.employee_id, row.section, decryptPayload(cipher, row), { reveal })
      : row.summary.changes ?? [];
    if (reveal) await audit(db, { actorId: req.user!.id, action: 'read_sensitive', table: 'profile_change_request', rowId: id });
    const { payload_enc: _enc, ...rest } = row;
    void _enc;
    res.json({ request: { ...rest, sectionLabel: SECTION_LABEL[row.section] }, changes });
  });

  r.get('/change-requests/:id/proof', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const row = await loadRequest(req, id);
    if (!row.proof_file_id) throw new NotFoundError('no proof attached');
    if (row.employee_id !== req.user!.employeeId) await audit(db, { actorId: req.user!.id, action: 'view_file', table: 'profile_change_request', rowId: id });
    sendStoredFile(res, await readFile(db, cipher, row.company_id, row.proof_file_id), { download: req.query.download === '1' });
  });

  // ---------------------------------------------------------------- one person

  r.get('/:id/card', async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const p = await db.query(`SELECT ${DIRECTORY_COLS}, e.skills, e.languages, e.status FROM ${DIRECTORY_FROM} WHERE e.id = $1 AND e.company_id = $2`, [id, companyId]);
    if (!p.rows[0]) throw new NotFoundError('person not found');
    const reports = await db.query(
      `SELECT e.id, e.name_en, e.designation, e.avatar_file_id IS NOT NULL AS has_avatar FROM employee_assignment a JOIN employee e ON e.id = a.employee_id
        WHERE a.manager_id = $1 AND a.valid @> current_date AND e.status = 'active' ORDER BY e.name_en`, [id]);
    res.json({ person: p.rows[0], reports: reports.rows });
  });

  r.get('/:id/avatar', async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const e = await employeeIn(companyId, id);
    if (!e.avatar_file_id) throw new NotFoundError('no profile photo');
    sendStoredFile(res, await readFile(db, cipher, companyId, e.avatar_file_id));
  });
  r.post('/:id/avatar', hr, async (req, res) => {
    await setAvatar(req, uuid.parse(req.params.id), avatarBody.parse(req.body).fileId);
    res.json({ ok: true });
  });
  r.delete('/:id/avatar', hr, async (req, res) => {
    await setAvatar(req, uuid.parse(req.params.id), null);
    res.json({ ok: true });
  });

  r.get('/:id/profile', async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    await employeeIn(companyId, id);
    selfOrHr(req, id);
    const reveal = req.query.reveal === '1';
    if (reveal) await audit(db, { actorId: req.user!.id, action: 'read_sensitive', table: 'employee_profile', rowId: id });
    const pending = await db.query(`SELECT id, section, created_at FROM profile_change_request WHERE employee_id = $1 AND status = 'pending'`, [id]);
    res.json({ profile: await readProfile(db, cipher, id, { reveal }), pending: pending.rows, secretFields: SECRET_FIELDS });
  });

  r.put('/:id/profile/:section', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    await employeeIn(companyId, id);
    const { section, value } = parseSection(String(req.params.section), req.body);
    const policy = await peoplePolicy(db, companyId);
    if (policy.makerChecker && SENSITIVE_SECTIONS.includes(section)) {
      const request = await tx(async (c) => {
        const created = await createChangeRequest(c, cipher, { companyId, employeeId: id, requestedBy: req.user!.id, section, value, reason: 'Edited by HR', byHr: true });
        await audit(c, { actorId: req.user!.id, action: 'create', table: 'profile_change_request', rowId: created.id, after: { section, byHr: true } });
        return created;
      });
      return res.status(202).json({ pending: true, request });
    }
    const changes = await tx(async (c) => {
      await c.query('SELECT 1 FROM employee WHERE id = $1 FOR UPDATE', [id]);
      const ch = await applySection(c, cipher, { companyId, employeeId: id, section, value, actorId: req.user!.id, source: 'hr_edit' });
      if (ch.length) await audit(c, { actorId: req.user!.id, action: 'update', table: 'employee_profile', rowId: id, after: { section, fields: ch.map((x) => x.label) } });
      return ch;
    });
    return res.json({ pending: false, changes });
  });

  r.get('/:id/history', async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    await employeeIn(companyId, id);
    selfOrHr(req, id);
    const rows = await db.query(
      `SELECT h.id, h.field, h.old_value, h.new_value, h.source, h.ref_id, h.changed_at, u.name AS changed_by_name
         FROM employee_field_history h LEFT JOIN app_user u ON u.id = h.changed_by
        WHERE h.employee_id = $1 AND h.company_id = $2 ORDER BY h.changed_at DESC, h.id DESC LIMIT 500`, [id, companyId]);
    res.json({ history: rows.rows });
  });

  r.get('/:id/career', async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    await employeeIn(companyId, id);
    const u = req.user!;
    const full = hasRole(u, 'hr') || u.employeeId === id;
    if (!full && !(hasRole(u, 'manager') && u.employeeId && (await isManagerOf(db, u.employeeId, id)))) throw new ForbiddenError('not allowed to see this career history');
    const rows = await db.query<CareerEventRow & { created_by_name: string | null }>(
      `SELECT c.*, u.name AS created_by_name FROM career_event c LEFT JOIN app_user u ON u.id = c.created_by
        WHERE c.employee_id = $1 AND c.company_id = $2 AND c.status <> 'cancelled' ORDER BY c.effective_date DESC, c.created_at DESC`, [id, companyId]);
    const names = new Map<string, string>();
    const lookup = async (table: 'branch' | 'org_unit' | 'employee' | 'designation', key: string | null | undefined) => {
      if (!key) return null;
      if (!names.has(key)) names.set(key, (await db.query<{ n: string }>(`SELECT name_en AS n FROM ${table} WHERE id = $1`, [key])).rows[0]?.n ?? '—');
      return names.get(key)!;
    };
    const events = [];
    for (const ev of rows.rows) {
      const d = ev.details;
      const parts: string[] = [];
      const designation = d.designation ?? (await lookup('designation', d.designationId));
      if (designation) parts.push(designation);
      if (d.grade) parts.push(`grade ${d.grade}`);
      const branch = await lookup('branch', d.branchId); if (branch) parts.push(`${branch} branch`);
      const unit = await lookup('org_unit', d.orgUnitId); if (unit) parts.push(unit);
      if (d.managerId !== undefined) parts.push(`reports to ${(await lookup('employee', d.managerId)) ?? 'nobody'}`);
      if (d.employmentType) parts.push(d.employmentType.replace(/_/g, '-'));
      // Managers see the event but not separation types, salary revisions or reasons.
      if (full && d.separationType) parts.push(d.separationType.replace(/_/g, ' '));
      if (full && d.revisionPercent !== undefined) parts.push(`${d.revisionPercent > 0 ? '+' : ''}${d.revisionPercent}%`);
      events.push({
        id: ev.id, kind: ev.kind, label: CAREER_LABEL[ev.kind], effectiveDate: ev.effective_date, status: ev.status, summary: parts.join(', '),
        note: full ? ev.note : null, reason: full ? d.reason ?? null : null, createdBy: ev.created_by_name, createdAt: ev.created_at,
      });
    }
    res.json({ events: full ? events : events.filter((e) => e.kind !== 'salary_revision') });
  });

  r.post('/:id/career', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const b = careerBody.parse(req.body);
    const ev = await tx(async (c) => {
      const out = await recordCareerEvent(c, { companyId, employeeId: id, kind: b.kind, effectiveDate: b.effectiveDate, details: b.details, note: b.note ?? null, actorId: req.user!.id });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'career_event', rowId: out.id, after: { employeeId: id, kind: b.kind, effectiveDate: b.effectiveDate, status: out.status } });
      return out;
    });
    res.status(201).json({ event: ev });
  });

  r.post('/:id/career/:eventId/cancel', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id); const eventId = uuid.parse(req.params.eventId);
    await tx(async (c) => {
      await cancelCareerEvent(c, companyId, id, eventId);
      await audit(c, { actorId: req.user!.id, action: 'cancel', table: 'career_event', rowId: eventId });
    });
    res.json({ ok: true });
  });

  return r;
}
