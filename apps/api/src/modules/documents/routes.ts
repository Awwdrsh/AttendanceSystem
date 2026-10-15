import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { attachFile, deleteFile, readFile, sendStoredFile } from '../files/store.js';
import { notify, notifyRole, userIdForEmployee } from '../notifications/service.js';
import { act, cancelWorkflow, nptToday, registerHandler, startWorkflow, type DecisionContext } from '../workflow/service.js';
import {
  companyHead, employeeLetterValues, esc, letterSettings, nextRefNo, PLACEHOLDERS, renderBody, renderLetterHtml, renderSubject,
  seedDocumentDefaults, unknownPlaceholders, type LetterValues,
} from './letters.js';

// ---------------------------------------------------------------- shared helpers
const seeded = new Set<string>();
/** Companies created before this module get their categories and built-in templates on first use. */
async function ensureDefaults(db: Queryable, companyId: string): Promise<void> {
  if (seeded.has(companyId)) return;
  await seedDocumentDefaults(db, companyId);
  seeded.add(companyId);
}

async function assertEmployeeInCompany(db: Queryable, companyId: string, employeeId: string): Promise<{ name_en: string; emp_code: string; status: string }> {
  const r = await db.query<{ name_en: string; emp_code: string; status: string }>('SELECT name_en, emp_code, status FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

async function tx<T>(db: AppDeps['db'], fn: (c: Queryable) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    const out = await fn(client);
    await client.query('COMMIT');
    return out;
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
}

const isHr = (u: AuthUser) => hasRole(u, 'hr');

// ---------------------------------------------------------------- policy audience
/** Active employees a policy is addressed to: everyone, chosen branches, or chosen departments (including sub-units). */
async function audienceIds(db: Queryable, companyId: string, p: { audience: string; audience_ids: string[] }, onlyEmployee?: string): Promise<string[]> {
  const r = await db.query<{ id: string }>(
    `WITH RECURSIVE anc(unit_id, ancestor_id, depth) AS (
        SELECT id, id, 0 FROM org_unit WHERE company_id = $1
        UNION ALL
        SELECT anc.unit_id, o.parent_id, anc.depth + 1 FROM anc JOIN org_unit o ON o.id = anc.ancestor_id WHERE o.parent_id IS NOT NULL AND anc.depth < 20)
     SELECT e.id FROM employee e
       LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $5::date
      WHERE e.company_id = $1 AND e.status = 'active' AND ($4::uuid IS NULL OR e.id = $4)
        AND CASE $2::text
              WHEN 'all' THEN true
              WHEN 'branch' THEN coalesce(a.branch_id = ANY($3::uuid[]), false)
              ELSE EXISTS (SELECT 1 FROM anc WHERE anc.unit_id = a.org_unit_id AND anc.ancestor_id = ANY($3::uuid[]))
            END`, [companyId, p.audience, p.audience_ids, onlyEmployee ?? null, nptToday()]);
  return r.rows.map((x) => x.id);
}

// ---------------------------------------------------------------- letter requests (workflow)
export async function applyLetterRequestDecision(db: Queryable, requestId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ employee_id: string; company_id: string; status: string; name: string }>(
    `SELECT lr.employee_id, lr.company_id, lr.status, t.name FROM letter_request lr JOIN letter_template t ON t.id = lr.template_id WHERE lr.id = $1 FOR UPDATE OF lr`, [requestId]);
  const lr = r.rows[0];
  if (!lr) throw new NotFoundError('letter request not found');
  if (lr.status !== 'pending') throw new ConflictError(`request already ${lr.status}`);
  await db.query('UPDATE letter_request SET status = $2, decision_reason = $3, decided_at = now() WHERE id = $1', [requestId, state, ctx.reason]);
  if (state === 'rejected') {
    await notify(db, {
      companyId: lr.company_id, userId: await userIdForEmployee(db, lr.employee_id), employeeId: lr.employee_id, template: 'letter_request.rejected',
      title: `${lr.name} request declined`, body: `HR declined your request for a ${lr.name.toLowerCase()}${ctx.reason ? `: ${ctx.reason}` : '.'}`, payload: { requestId }, channels: ['push'],
    });
  }
}
registerHandler('letter_request', applyLetterRequestDecision);

// ---------------------------------------------------------------- daily job
/**
 * Expiry alerts at 30, 7 and 0 days before a document expires (one per document and threshold; HR gets one summary per
 * company), and a weekly reminder to employees who have not acknowledged a policy that needs acknowledgement.
 */
export async function documentsDailyJob(db: Queryable, now = new Date()): Promise<{ expiryAlerts: number; policyNudges: number }> {
  const today = new Date(now.getTime() + 345 * 60_000).toISOString().slice(0, 10);
  const docs = await db.query<{ id: string; company_id: string; employee_id: string; title: string; expiry_date: string; days: number; visible_to_employee: boolean; name_en: string }>(
    `SELECT d.id, d.company_id, d.employee_id, d.title, d.expiry_date::text AS expiry_date, (d.expiry_date - $1::date) AS days, d.visible_to_employee, e.name_en
       FROM employee_document d JOIN employee e ON e.id = d.employee_id
      WHERE d.deleted_at IS NULL AND d.is_current AND d.status <> 'rejected' AND d.expiry_date IS NOT NULL AND e.status = 'active'
        AND d.expiry_date - $1::date BETWEEN -30 AND 30
      ORDER BY d.expiry_date`, [today]);
  let expiryAlerts = 0;
  const perCompany = new Map<string, string[]>();
  for (const d of docs.rows) {
    const threshold = d.days <= 0 ? 0 : d.days <= 7 ? 7 : 30;
    const ins = await db.query('INSERT INTO document_expiry_alert (document_id, threshold) VALUES ($1, $2) ON CONFLICT DO NOTHING', [d.id, threshold]);
    if (!ins.rowCount) continue;
    expiryAlerts += 1;
    const when = d.days < 0 ? `expired on ${bsLabel(d.expiry_date)}` : d.days === 0 ? 'expires today' : `expires on ${bsLabel(d.expiry_date)} (in ${d.days} days)`;
    if (d.visible_to_employee) {
      const userId = await userIdForEmployee(db, d.employee_id);
      if (userId) {
        await notify(db, {
          companyId: d.company_id, userId, employeeId: d.employee_id, template: 'document.expiring', title: `${d.title} ${d.days < 0 ? 'has expired' : 'is expiring'}`,
          body: `Your ${d.title} ${when}. Upload the renewed document under Profile, Documents & letters.`, payload: { documentId: d.id, threshold }, channels: ['push'],
        });
      }
    }
    perCompany.set(d.company_id, [...(perCompany.get(d.company_id) ?? []), `${d.name_en}: ${d.title} ${when}`]);
  }
  for (const [companyId, lines] of perCompany) {
    await notifyRole(db, companyId, 'hr', {
      template: 'document.expiring.summary', title: `${lines.length} employee document${lines.length === 1 ? '' : 's'} expiring`,
      body: lines.slice(0, 10).join('; ') + (lines.length > 10 ? `; and ${lines.length - 10} more` : ''), payload: { count: lines.length },
    });
  }

  let policyNudges = 0;
  const policies = await db.query<{ id: string; company_id: string; title: string; audience: string; audience_ids: string[] }>(
    `SELECT id, company_id, title, audience, audience_ids FROM company_policy
      WHERE archived_at IS NULL AND requires_ack AND published_at < $1::timestamptz - interval '2 days'`, [now]);
  for (const p of policies.rows) {
    const ids = await audienceIds(db, p.company_id, p);
    if (!ids.length) continue;
    const due = await db.query<{ employee_id: string }>(
      `SELECT x AS employee_id FROM unnest($2::uuid[]) x
        WHERE NOT EXISTS (SELECT 1 FROM policy_ack a WHERE a.policy_id = $1 AND a.employee_id = x)
          AND NOT EXISTS (SELECT 1 FROM policy_nudge n WHERE n.policy_id = $1 AND n.employee_id = x AND n.last_sent_at > $3::timestamptz - interval '7 days')`,
      [p.id, ids, now]);
    for (const { employee_id } of due.rows) {
      const userId = await userIdForEmployee(db, employee_id);
      if (!userId) continue;
      await notify(db, {
        companyId: p.company_id, userId, employeeId: employee_id, template: 'policy.reminder', title: `Please read: ${p.title}`,
        body: `${p.title} is waiting for your acknowledgement under Profile, Documents & letters.`, payload: { policyId: p.id },
      });
      await db.query(
        `INSERT INTO policy_nudge (policy_id, employee_id, last_sent_at) VALUES ($1, $2, $3)
         ON CONFLICT (policy_id, employee_id) DO UPDATE SET last_sent_at = EXCLUDED.last_sent_at`, [p.id, employee_id, now]);
      policyNudges += 1;
    }
  }
  return { expiryAlerts, policyNudges };
}

// ---------------------------------------------------------------- schemas
const categoryBody = z.object({
  code: z.string().regex(/^[a-z0-9_]{2,40}$/, 'lowercase letters, digits and _'),
  name: z.string().trim().min(1).max(80),
  employeeCanUpload: z.boolean().default(true),
  expiryRequired: z.boolean().default(false),
  visibleToEmployee: z.boolean().default(true),
  sortOrder: z.number().int().min(0).max(999).default(50),
});
const categoryPatch = z.object({
  name: z.string().trim().min(1).max(80).optional(),
  employeeCanUpload: z.boolean().optional(),
  expiryRequired: z.boolean().optional(),
  visibleToEmployee: z.boolean().optional(),
  sortOrder: z.number().int().min(0).max(999).optional(),
  active: z.boolean().optional(),
});

const documentBody = z.object({
  categoryId: uuid,
  title: z.string().trim().min(1).max(120),
  fileId: uuid,
  expiryDate: isoDate.nullish(),
  visibleToEmployee: z.boolean().optional(),
  notes: z.string().trim().max(500).nullish(),
  replacesId: uuid.nullish(),
});

const templateBody = z.object({
  code: z.string().regex(/^[a-z0-9_]{2,40}$/, 'lowercase letters, digits and _'),
  name: z.string().trim().min(1).max(80),
  subject: z.string().trim().min(1).max(160),
  body: z.string().trim().min(1).max(20000),
  requestable: z.boolean().default(false),
  visibleToEmployee: z.boolean().default(true),
});
const templatePatch = z.object({
  name: z.string().trim().min(1).max(80).optional(),
  subject: z.string().trim().min(1).max(160).optional(),
  body: z.string().trim().min(1).max(20000).optional(),
  requestable: z.boolean().optional(),
  visibleToEmployee: z.boolean().optional(),
  active: z.boolean().optional(),
});

const settingsBody = z.object({
  signatoryName: z.string().trim().max(80).nullable(),
  signatoryTitle: z.string().trim().max(80).nullable(),
  refPrefix: z.string().trim().regex(/^[A-Za-z0-9/_-]{1,20}$/, 'letters, digits, / _ - only'),
});

const policyBody = z.object({
  title: z.string().trim().min(1).max(160),
  description: z.string().trim().max(2000).nullish(),
  fileId: uuid,
  audience: z.enum(['all', 'branch', 'department']).default('all'),
  audienceIds: z.array(uuid).max(100).default([]),
  requiresAck: z.boolean().default(true),
});

// ---------------------------------------------------------------- rows
interface DocRow {
  id: string; company_id: string; employee_id: string; category_id: string; category_name: string; title: string; file_id: string; version: number;
  replaces_id: string | null; is_current: boolean; status: string; reject_reason: string | null; expiry_date: string | null; visible_to_employee: boolean;
  notes: string | null; uploaded_by: string | null; uploaded_by_name: string | null; verified_at: Date | null; created_at: Date; filename: string; mime: string; bytes: number;
}
const DOC_COLS = `d.id, d.company_id, d.employee_id, d.category_id, c.name AS category_name, d.title, d.file_id, d.version, d.replaces_id, d.is_current,
       d.status, d.reject_reason, d.expiry_date::text AS expiry_date, d.visible_to_employee, d.notes, d.uploaded_by, u.name AS uploaded_by_name,
       d.verified_at, d.created_at, f.filename, f.mime, f.bytes`;
const DOC_FROM = `FROM employee_document d JOIN document_category c ON c.id = d.category_id JOIN stored_file f ON f.id = d.file_id
  LEFT JOIN app_user u ON u.id = d.uploaded_by`;

export function documentsRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  // ============================================================ categories
  r.get('/categories', async (req, res) => {
    const companyId = companyIdOf(req);
    await ensureDefaults(db, companyId);
    const rows = await db.query(
      `SELECT id, code, name, employee_can_upload, expiry_required, visible_to_employee, sort_order, active FROM document_category
        WHERE company_id = $1 AND ($2 OR active) ORDER BY sort_order, name`, [companyId, isHr(req.user!)]);
    res.json({ categories: rows.rows });
  });

  r.post('/categories', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = categoryBody.parse(req.body);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO document_category (company_id, code, name, employee_can_upload, expiry_required, visible_to_employee, sort_order)
       VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`, [companyId, b.code, b.name, b.employeeCanUpload, b.expiryRequired, b.visibleToEmployee, b.sortOrder]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'document_category', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.patch('/categories/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = categoryPatch.parse(req.body);
    const u = await db.query(
      `UPDATE document_category SET name = coalesce($3, name), employee_can_upload = coalesce($4, employee_can_upload), expiry_required = coalesce($5, expiry_required),
              visible_to_employee = coalesce($6, visible_to_employee), sort_order = coalesce($7, sort_order), active = coalesce($8, active)
        WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.name ?? null, b.employeeCanUpload ?? null, b.expiryRequired ?? null, b.visibleToEmployee ?? null, b.sortOrder ?? null, b.active ?? null]);
    if (!u.rowCount) throw new NotFoundError('category not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'document_category', rowId: id, after: b });
    res.json({ ok: true });
  });

  // ============================================================ employee documents
  /** Who may see an employee's documents: HR all of them; the employee their own visible ones and their own uploads. */
  async function listDocs(req: Request, employeeId: string, includeHistory: boolean): Promise<DocRow[]> {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const hr = isHr(u);
    if (!hr && u.employeeId !== employeeId) throw new ForbiddenError('you can only see your own documents');
    await assertEmployeeInCompany(db, companyId, employeeId);
    const rows = await db.query<DocRow>(
      `SELECT ${DOC_COLS} ${DOC_FROM}
        WHERE d.company_id = $1 AND d.employee_id = $2 AND d.deleted_at IS NULL AND ($3 OR d.is_current)
          AND ($4 OR d.visible_to_employee OR d.uploaded_by = $5)
        ORDER BY c.sort_order, c.name, d.title, d.version DESC`, [companyId, employeeId, includeHistory && hr, hr, u.id]);
    return rows.rows;
  }

  r.get('/mine', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) return res.json({ documents: [] });
    await ensureDefaults(db, companyIdOf(req));
    res.json({ documents: await listDocs(req, u.employeeId, false) });
  });

  r.get('/employee/:employeeId', async (req, res) => {
    const employeeId = uuid.parse(req.params.employeeId);
    const q = z.object({ history: z.enum(['0', '1']).optional() }).parse(req.query);
    res.json({ documents: await listDocs(req, employeeId, q.history === '1') });
  });

  /** HR queues: documents awaiting verification and documents expiring soon. */
  r.get('/queue', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ days: z.coerce.number().int().min(0).max(365).default(30) }).parse(req.query);
    const pending = await db.query(
      `SELECT e.emp_code, e.name_en, ${DOC_COLS} ${DOC_FROM} JOIN employee e ON e.id = d.employee_id
        WHERE d.company_id = $1 AND d.deleted_at IS NULL AND d.status = 'pending' ORDER BY d.created_at`, [companyId]);
    const expiring = await db.query(
      `SELECT e.emp_code, e.name_en, (d.expiry_date - $3::date) AS days_left, ${DOC_COLS} ${DOC_FROM} JOIN employee e ON e.id = d.employee_id
        WHERE d.company_id = $1 AND d.deleted_at IS NULL AND d.is_current AND d.status <> 'rejected' AND e.status = 'active'
          AND d.expiry_date IS NOT NULL AND d.expiry_date <= $3::date + $2::int ORDER BY d.expiry_date`, [companyId, q.days, nptToday()]);
    res.json({ pending: pending.rows, expiring: expiring.rows });
  });

  r.post('/employee/:employeeId', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = uuid.parse(req.params.employeeId);
    const u = req.user!;
    const b = documentBody.parse(req.body);
    const hr = isHr(u);
    if (!hr && u.employeeId !== employeeId) throw new ForbiddenError('you can only upload your own documents');
    const emp = await assertEmployeeInCompany(db, companyId, employeeId);
    const cat = await db.query<{ name: string; employee_can_upload: boolean; expiry_required: boolean; visible_to_employee: boolean; active: boolean }>(
      'SELECT name, employee_can_upload, expiry_required, visible_to_employee, active FROM document_category WHERE id = $1 AND company_id = $2', [b.categoryId, companyId]);
    const c = cat.rows[0];
    if (!c || !c.active) throw new BadRequestError('choose a document category');
    if (!hr && !c.employee_can_upload) throw new ForbiddenError(`only HR can add a ${c.name.toLowerCase()}`);
    if (c.expiry_required && !b.expiryDate) throw new BadRequestError(`${c.name} needs an expiry date`);
    const id = await tx(db, async (t) => {
      let version = 1;
      if (b.replacesId) {
        const old = await t.query<{ employee_id: string; version: number; is_current: boolean; uploaded_by: string | null; status: string; visible_to_employee: boolean }>(
          'SELECT employee_id, version, is_current, uploaded_by, status, visible_to_employee FROM employee_document WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL FOR UPDATE', [b.replacesId, companyId]);
        const o = old.rows[0];
        if (!o || o.employee_id !== employeeId || (!hr && !o.visible_to_employee && o.uploaded_by !== u.id)) throw new NotFoundError('the document to replace was not found');
        if (!o.is_current) throw new ConflictError('only the latest version can be replaced');
        version = o.version + 1;
        await t.query('UPDATE employee_document SET is_current = false WHERE id = $1', [b.replacesId]);
      }
      const ins = await t.query<{ id: string }>(
        `INSERT INTO employee_document (company_id, employee_id, category_id, title, file_id, version, replaces_id, status, expiry_date, visible_to_employee, notes, uploaded_by, verified_by, verified_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING id`,
        [companyId, employeeId, b.categoryId, b.title, b.fileId, version, b.replacesId ?? null, hr ? 'verified' : 'pending', b.expiryDate ?? null,
          hr ? (b.visibleToEmployee ?? c.visible_to_employee) : true, b.notes ?? null, u.id, hr ? u.id : null, hr ? new Date() : null]);
      const docId = ins.rows[0]!.id;
      await attachFile(t, { companyId, fileId: b.fileId, userId: u.id, purpose: 'employee_document', attachedTo: `employee_document:${docId}` });
      await audit(t, { actorId: u.id, action: 'create', table: 'employee_document', rowId: docId, after: { employeeId, categoryId: b.categoryId, title: b.title, version, expiryDate: b.expiryDate ?? null } });
      if (!hr) {
        await notifyRole(t, companyId, 'hr', {
          template: 'document.uploaded', title: 'Document to verify', body: `${emp.name_en} (${emp.emp_code}) uploaded ${b.title}.`, payload: { documentId: docId, employeeId },
        });
      } else if (u.employeeId !== employeeId && (b.visibleToEmployee ?? c.visible_to_employee)) {
        await notify(t, {
          companyId, userId: await userIdForEmployee(t, employeeId), employeeId, template: 'document.added', title: 'New document in your profile',
          body: `HR added ${b.title} to your documents.`, payload: { documentId: docId },
        });
      }
      return docId;
    });
    res.status(201).json({ id });
  });

  async function loadDoc(companyId: string, id: string): Promise<DocRow> {
    const r0 = await db.query<DocRow>(`SELECT ${DOC_COLS} ${DOC_FROM} WHERE d.id = $1 AND d.company_id = $2 AND d.deleted_at IS NULL`, [id, companyId]);
    if (!r0.rows[0]) throw new NotFoundError('document not found');
    return r0.rows[0];
  }

  r.get('/:id/file', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    const d = await loadDoc(companyId, id);
    const own = u.employeeId === d.employee_id && (d.visible_to_employee || d.uploaded_by === u.id);
    if (!isHr(u) && !own) throw new NotFoundError('document not found');
    const f = await readFile(db, cipher, companyId, d.file_id);
    if (u.employeeId !== d.employee_id) {
      await audit(db, { actorId: u.id, action: 'view_document', table: 'employee_document', rowId: id, after: { employeeId: d.employee_id, title: d.title } });
    }
    sendStoredFile(res, f, { download: req.query.download === '1' });
  });

  r.post('/:id/verify', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ decision: z.enum(['verify', 'reject']), reason: z.string().trim().max(300).optional() }).parse(req.body);
    if (b.decision === 'reject' && !b.reason) throw new BadRequestError('give a reason so the employee knows what to fix');
    const d = await loadDoc(companyId, id);
    if (req.user!.employeeId === d.employee_id) throw new ForbiddenError('you cannot verify your own documents');
    await tx(db, async (t) => {
      const u = await t.query(
        `UPDATE employee_document SET status = $2, reject_reason = $3, verified_by = $4, verified_at = now() WHERE id = $1 AND status = 'pending'`,
        [id, b.decision === 'verify' ? 'verified' : 'rejected', b.decision === 'reject' ? b.reason : null, req.user!.id]);
      if (!u.rowCount) throw new ConflictError('this document is not waiting for verification');
      await audit(t, { actorId: req.user!.id, action: b.decision, table: 'employee_document', rowId: id, after: { reason: b.reason ?? null } });
      await notify(t, {
        companyId, userId: await userIdForEmployee(t, d.employee_id), employeeId: d.employee_id, template: `document.${b.decision === 'verify' ? 'verified' : 'rejected'}`,
        title: b.decision === 'verify' ? `${d.title} verified` : `${d.title} needs another look`,
        body: b.decision === 'verify' ? `HR verified your ${d.title}.` : `HR could not accept your ${d.title}: ${b.reason}. Upload it again.`, payload: { documentId: id },
      });
    });
    res.json({ ok: true });
  });

  r.patch('/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ title: z.string().trim().min(1).max(120).optional(), expiryDate: isoDate.nullish(), visibleToEmployee: z.boolean().optional(), notes: z.string().trim().max(500).nullish() }).parse(req.body);
    const before = await loadDoc(companyId, id);
    await db.query(
      `UPDATE employee_document SET title = coalesce($3, title), expiry_date = CASE WHEN $4 THEN $5::date ELSE expiry_date END,
              visible_to_employee = coalesce($6, visible_to_employee), notes = CASE WHEN $7 THEN $8 ELSE notes END WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.title ?? null, b.expiryDate !== undefined, b.expiryDate ?? null, b.visibleToEmployee ?? null, b.notes !== undefined, b.notes ?? null]);
    // A new expiry date restarts its alerts.
    if (b.expiryDate !== undefined && b.expiryDate !== before.expiry_date) await db.query('DELETE FROM document_expiry_alert WHERE document_id = $1', [id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'employee_document', rowId: id, before: { title: before.title, expiryDate: before.expiry_date, visible: before.visible_to_employee }, after: b });
    res.json({ ok: true });
  });

  /** HR deletes any document; an employee only their own upload that HR has not verified yet. The bytes are wiped. */
  r.delete('/:id', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    const d = await loadDoc(companyId, id);
    const ownPending = u.employeeId === d.employee_id && d.uploaded_by === u.id && d.status !== 'verified';
    if (!isHr(u) && !ownPending) {
      if (u.employeeId === d.employee_id) throw new ForbiddenError('only HR can delete a verified document');
      throw new NotFoundError('document not found');
    }
    await tx(db, async (t) => {
      await t.query('UPDATE employee_document SET deleted_at = now(), deleted_by = $2, is_current = false WHERE id = $1', [id, u.id]);
      // Deleting the latest version brings the previous one back.
      if (d.is_current && d.replaces_id) await t.query('UPDATE employee_document SET is_current = true WHERE id = $1 AND deleted_at IS NULL', [d.replaces_id]);
      await deleteFile(t, companyId, d.file_id);
      await audit(t, { actorId: u.id, action: 'delete', table: 'employee_document', rowId: id, before: { employeeId: d.employee_id, title: d.title, version: d.version } });
    });
    res.json({ ok: true });
  });

  // ============================================================ letter settings and templates
  r.get('/placeholders', requireRole('hr'), (_req, res) => { res.json({ placeholders: PLACEHOLDERS }); });

  r.get('/letter-settings', requireRole('hr'), async (req, res) => {
    res.json({ settings: await letterSettings(db, companyIdOf(req)) });
  });

  r.put('/letter-settings', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = settingsBody.parse(req.body);
    const before = await letterSettings(db, companyId);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, 'documents.letters', $2, $3)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`, [companyId, JSON.stringify(b), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: 'documents.letters', before, after: b });
    res.json({ settings: b });
  });

  /** HR sees every template; employees only the ones they may request. */
  r.get('/templates', async (req, res) => {
    const companyId = companyIdOf(req);
    await ensureDefaults(db, companyId);
    const hr = isHr(req.user!);
    const rows = await db.query(
      hr ? `SELECT id, code, name, subject, body, requestable, visible_to_employee, is_builtin, active, updated_at FROM letter_template WHERE company_id = $1 ORDER BY active DESC, name`
        : `SELECT id, code, name, subject FROM letter_template WHERE company_id = $1 AND active AND requestable ORDER BY name`, [companyId]);
    res.json({ templates: rows.rows });
  });

  r.post('/templates', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = templateBody.parse(req.body);
    const bad = unknownPlaceholders(b.subject, b.body);
    if (bad.length) throw new BadRequestError(`Unknown placeholder${bad.length === 1 ? '' : 's'}: ${bad.join(', ')}`, { unknown: bad });
    const ins = await db.query<{ id: string }>(
      `INSERT INTO letter_template (company_id, code, name, subject, body, requestable, visible_to_employee, updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
      [companyId, b.code, b.name, b.subject, b.body, b.requestable, b.visibleToEmployee, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'letter_template', rowId: ins.rows[0]!.id, after: { code: b.code, name: b.name } });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.patch('/templates/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = templatePatch.parse(req.body);
    const bad = unknownPlaceholders(b.subject ?? '', b.body ?? '');
    if (bad.length) throw new BadRequestError(`Unknown placeholder${bad.length === 1 ? '' : 's'}: ${bad.join(', ')}`, { unknown: bad });
    const before = await db.query('SELECT name, subject, body, requestable, visible_to_employee, active FROM letter_template WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!before.rows[0]) throw new NotFoundError('template not found');
    await db.query(
      `UPDATE letter_template SET name = coalesce($3, name), subject = coalesce($4, subject), body = coalesce($5, body), requestable = coalesce($6, requestable),
              visible_to_employee = coalesce($7, visible_to_employee), active = coalesce($8, active), updated_by = $9, updated_at = now() WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.name ?? null, b.subject ?? null, b.body ?? null, b.requestable ?? null, b.visibleToEmployee ?? null, b.active ?? null, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'letter_template', rowId: id, before: before.rows[0], after: b });
    res.json({ ok: true });
  });

  /** Build a letter for an employee; `preview` highlights missing values and shows a DRAFT watermark. */
  async function buildLetter(t: Queryable, companyId: string, tpl: { subject: string; body: string }, employeeId: string, extra: { refNo: string; purpose?: string | null; addressedTo?: string | null }, preview: boolean) {
    const values: LetterValues | null = await employeeLetterValues(t, companyId, employeeId, extra);
    if (!values) throw new NotFoundError('employee not found');
    const missing = new Set<string>();
    const subjectHtml = renderSubject(tpl.subject, values, missing, preview);
    const body = renderBody(tpl.body, values, { mark: preview });
    for (const m of body.missing) missing.add(m);
    const head = await companyHead(t, companyId);
    const s = await letterSettings(t, companyId);
    const html = renderLetterHtml({
      company: head, refNo: extra.refNo, dateBs: values.today ?? bsLabel(nptToday()), subjectHtml, bodyHtml: body.html,
      signatory: { name: s.signatoryName, title: s.signatoryTitle }, draft: preview,
    });
    const subjectText = subjectHtml.replace(/<[^>]+>/g, '').replace(/&#42;/g, '*').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    return { html, missing: [...missing], subject: subjectText };
  }

  r.post('/templates/preview', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      employeeId: uuid, templateId: uuid.optional(), subject: z.string().max(160).optional(), body: z.string().max(20000).optional(),
      purpose: z.string().max(300).nullish(), addressedTo: z.string().max(200).nullish(),
    }).parse(req.body);
    let tpl: { subject: string; body: string };
    if (b.templateId) {
      const t = await db.query<{ subject: string; body: string }>('SELECT subject, body FROM letter_template WHERE id = $1 AND company_id = $2', [b.templateId, companyId]);
      if (!t.rows[0]) throw new NotFoundError('template not found');
      tpl = { subject: b.subject ?? t.rows[0].subject, body: b.body ?? t.rows[0].body };
    } else {
      if (!b.subject || !b.body) throw new BadRequestError('subject and body are required');
      tpl = { subject: b.subject, body: b.body };
    }
    const unknown = unknownPlaceholders(tpl.subject, tpl.body);
    const out = await buildLetter(db, companyId, tpl, b.employeeId, { refNo: 'DRAFT', purpose: b.purpose ?? null, addressedTo: b.addressedTo ?? null }, true);
    res.json({ html: out.html, missing: out.missing.filter((m) => m !== 'ref_no'), unknown });
  });

  // ============================================================ issued letters
  r.post('/letters', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const b = z.object({
      templateId: uuid, employeeId: uuid, visibleToEmployee: z.boolean().optional(), purpose: z.string().trim().max(300).nullish(), addressedTo: z.string().trim().max(200).nullish(),
      requestId: uuid.optional(), allowMissing: z.boolean().default(false),
    }).parse(req.body);
    await ensureDefaults(db, companyId);
    const out = await tx(db, async (t) => {
      const tr = await t.query<{ id: string; code: string; subject: string; body: string; active: boolean; visible_to_employee: boolean }>(
        'SELECT id, code, subject, body, active, visible_to_employee FROM letter_template WHERE id = $1 AND company_id = $2', [b.templateId, companyId]);
      const tpl = tr.rows[0];
      if (!tpl || !tpl.active) throw new NotFoundError('template not found');
      await assertEmployeeInCompany(t, companyId, b.employeeId);
      let purpose = b.purpose ?? null; let addressedTo = b.addressedTo ?? null;
      if (b.requestId) {
        const rq = await t.query<{ employee_id: string; status: string; purpose: string; addressed_to: string | null; workflow_instance_id: string | null }>(
          'SELECT employee_id, status, purpose, addressed_to, workflow_instance_id FROM letter_request WHERE id = $1 AND company_id = $2 FOR UPDATE', [b.requestId, companyId]);
        const q = rq.rows[0];
        if (!q) throw new NotFoundError('letter request not found');
        if (q.employee_id !== b.employeeId) throw new BadRequestError('the request belongs to another employee');
        if (q.status !== 'pending' && q.status !== 'approved') throw new ConflictError(`request already ${q.status}`);
        purpose = purpose ?? q.purpose; addressedTo = addressedTo ?? q.addressed_to;
        if (q.status === 'pending' && q.workflow_instance_id) await act(t, { instanceId: q.workflow_instance_id, user: u, decision: 'approve' });
      }
      const s = await letterSettings(t, companyId);
      // Check for missing values before taking a reference number.
      const probe = await buildLetter(t, companyId, tpl, b.employeeId, { refNo: 'X', purpose, addressedTo }, false);
      if (probe.missing.length && !b.allowMissing) {
        throw new BadRequestError(`Fill in these details first: ${probe.missing.join(', ')}`, { code: 'missing_values', missing: probe.missing });
      }
      const refNo = await nextRefNo(t, companyId, s.refPrefix);
      const letter = await buildLetter(t, companyId, tpl, b.employeeId, { refNo, purpose, addressedTo }, false);
      const visible = b.visibleToEmployee ?? tpl.visible_to_employee;
      const ins = await t.query<{ id: string }>(
        `INSERT INTO issued_letter (company_id, employee_id, template_id, template_code, ref_no, subject, html, visible_to_employee, request_id, issued_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id`,
        [companyId, b.employeeId, tpl.id, tpl.code, refNo, letter.subject, letter.html, visible, b.requestId ?? null, u.id]);
      const letterId = ins.rows[0]!.id;
      if (b.requestId) await t.query(`UPDATE letter_request SET status = 'issued', issued_letter_id = $2, decided_at = coalesce(decided_at, now()) WHERE id = $1`, [b.requestId, letterId]);
      await audit(t, { actorId: u.id, action: 'issue', table: 'issued_letter', rowId: letterId, after: { employeeId: b.employeeId, template: tpl.code, refNo, requestId: b.requestId ?? null } });
      if (visible) {
        await notify(t, {
          companyId, userId: await userIdForEmployee(t, b.employeeId), employeeId: b.employeeId, template: 'letter.issued', title: `${letter.subject} issued`,
          body: `HR issued your letter "${letter.subject}" (${refNo}). Open it under Profile, Documents & letters.`, payload: { letterId }, channels: ['push'],
        });
      }
      return { id: letterId, refNo };
    });
    res.status(201).json(out);
  });

  r.get('/letters', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const q = z.object({ employeeId: uuid.optional(), q: z.string().max(80).optional(), limit: z.coerce.number().int().min(1).max(500).default(200) }).parse(req.query);
    const hr = isHr(u);
    if (!hr && q.employeeId && q.employeeId !== u.employeeId) throw new ForbiddenError('you can only see your own letters');
    const employeeId = hr ? q.employeeId ?? null : u.employeeId;
    if (!hr && !employeeId) return res.json({ letters: [] });
    const rows = await db.query(
      `SELECT l.id, l.employee_id, e.emp_code, e.name_en, l.template_code, l.ref_no, l.subject, l.visible_to_employee, l.request_id, l.issued_at,
              l.voided_at, l.void_reason, iu.name AS issued_by_name
         FROM issued_letter l JOIN employee e ON e.id = l.employee_id LEFT JOIN app_user iu ON iu.id = l.issued_by
        WHERE l.company_id = $1 AND ($2::uuid IS NULL OR l.employee_id = $2) AND ($3 OR (l.visible_to_employee AND l.voided_at IS NULL))
          AND ($4::text IS NULL OR l.ref_no ILIKE '%' || $4 || '%' OR e.name_en ILIKE '%' || $4 || '%' OR e.emp_code ILIKE '%' || $4 || '%' OR l.subject ILIKE '%' || $4 || '%')
        ORDER BY l.issued_at DESC LIMIT $5`, [companyId, employeeId, hr, q.q?.trim() || null, q.limit]);
    res.json({ letters: rows.rows });
  });

  r.get('/letters/:id/html', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    const l = await db.query<{ employee_id: string; html: string; visible_to_employee: boolean; voided_at: Date | null; void_reason: string | null }>(
      'SELECT employee_id, html, visible_to_employee, voided_at, void_reason FROM issued_letter WHERE id = $1 AND company_id = $2', [id, companyId]);
    const row = l.rows[0];
    const own = !!row && u.employeeId === row.employee_id && row.visible_to_employee && !row.voided_at;
    if (!row || (!isHr(u) && !own)) throw new NotFoundError('letter not found');
    let html = row.html;
    if (row.voided_at) html = html.replace('<body>', '<body><div class="wm">VOID</div>').replace('</body>', `<p class="foot">Voided: ${esc(row.void_reason ?? '')}</p></body>`);
    if (u.employeeId !== row.employee_id) await audit(db, { actorId: u.id, action: 'view_letter', table: 'issued_letter', rowId: id });
    res.setHeader('Cache-Control', 'private, no-store');
    res.type('html').send(html);
  });

  r.post('/letters/:id/void', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ reason: z.string().trim().min(3).max(300) }).parse(req.body);
    const u = await db.query(
      `UPDATE issued_letter SET voided_at = now(), voided_by = $3, void_reason = $4 WHERE id = $1 AND company_id = $2 AND voided_at IS NULL`, [id, companyId, req.user!.id, b.reason]);
    if (!u.rowCount) throw new NotFoundError('letter not found or already voided');
    await audit(db, { actorId: req.user!.id, action: 'void', table: 'issued_letter', rowId: id, after: b });
    res.json({ ok: true });
  });

  // ============================================================ letter requests
  r.post('/letter-requests', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    if (!u.employeeId) throw new BadRequestError('only employees can request letters');
    const employeeId = u.employeeId;
    const b = z.object({ templateId: uuid, purpose: z.string().trim().min(3).max(300), addressedTo: z.string().trim().max(200).nullish() }).parse(req.body);
    await ensureDefaults(db, companyId);
    const t0 = await db.query<{ name: string }>('SELECT name FROM letter_template WHERE id = $1 AND company_id = $2 AND active AND requestable', [b.templateId, companyId]);
    if (!t0.rows[0]) throw new BadRequestError('this letter cannot be requested');
    const name = t0.rows[0].name;
    const dup = await db.query(`SELECT 1 FROM letter_request WHERE employee_id = $1 AND template_id = $2 AND status IN ('pending', 'approved')`, [employeeId, b.templateId]);
    if (dup.rows[0]) throw new ConflictError(`you already have an open request for a ${name.toLowerCase()}`);
    const emp = await assertEmployeeInCompany(db, companyId, employeeId);
    const id = await tx(db, async (t) => {
      const ins = await t.query<{ id: string }>(
        'INSERT INTO letter_request (company_id, employee_id, template_id, purpose, addressed_to, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
        [companyId, employeeId, b.templateId, b.purpose, b.addressedTo ?? null, u.id]);
      const reqId = ins.rows[0]!.id;
      const inst = await startWorkflow(t, {
        companyId, objectType: 'letter_request', objectId: reqId, employeeId, title: `${name} requested`, body: `${emp.name_en} asked for a ${name.toLowerCase()}: ${b.purpose}`,
      });
      await t.query('UPDATE letter_request SET workflow_instance_id = $2 WHERE id = $1', [reqId, inst.id]);
      await audit(t, { actorId: u.id, action: 'create', table: 'letter_request', rowId: reqId, after: b });
      return reqId;
    });
    res.status(201).json({ id });
  });

  r.get('/letter-requests', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const q = z.object({ status: z.enum(['pending', 'approved', 'rejected', 'issued', 'cancelled', 'open']).optional() }).parse(req.query);
    const hr = isHr(u);
    if (!hr && !u.employeeId) return res.json({ requests: [] });
    const rows = await db.query(
      `SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, lr.template_id, t.name AS template_name, t.code AS template_code, lr.purpose, lr.addressed_to,
              lr.status, lr.decision_reason, lr.issued_letter_id, lr.created_at, lr.decided_at
         FROM letter_request lr JOIN employee e ON e.id = lr.employee_id JOIN letter_template t ON t.id = lr.template_id
        WHERE lr.company_id = $1 AND ($2 OR lr.employee_id = $3::uuid)
          AND ($4::text IS NULL OR ($4 = 'open' AND lr.status IN ('pending', 'approved')) OR lr.status = $4)
        ORDER BY lr.created_at DESC LIMIT 300`, [companyId, hr, u.employeeId, q.status ?? null]);
    res.json({ requests: rows.rows });
  });

  r.post('/letter-requests/:id/cancel', async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    const r0 = await db.query<{ employee_id: string; status: string }>('SELECT employee_id, status FROM letter_request WHERE id = $1 AND company_id = $2', [id, companyId]);
    const row = r0.rows[0];
    if (!row || (row.employee_id !== u.employeeId && !isHr(u))) throw new NotFoundError('request not found');
    if (row.status !== 'pending' && row.status !== 'approved') throw new ConflictError(`request already ${row.status}`);
    await tx(db, async (t) => {
      await t.query(`UPDATE letter_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
      await cancelWorkflow(t, 'letter_request', id);
      await audit(t, { actorId: u.id, action: 'cancel', table: 'letter_request', rowId: id });
    });
    res.json({ ok: true });
  });

  // ============================================================ company policies
  r.get('/policies', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const q = z.object({ archived: z.enum(['0', '1']).optional() }).parse(req.query);
    if (isHr(u)) {
      const rows = await db.query<{ id: string; title: string; description: string | null; audience: string; audience_ids: string[]; requires_ack: boolean; published_at: Date; archived_at: Date | null; filename: string; acks: number }>(
        `SELECT p.id, p.title, p.description, p.audience, p.audience_ids, p.requires_ack, p.published_at, p.archived_at, f.filename,
                (SELECT count(*)::int FROM policy_ack a JOIN employee e ON e.id = a.employee_id WHERE a.policy_id = p.id AND e.status = 'active') AS acks
           FROM company_policy p JOIN stored_file f ON f.id = p.file_id
          WHERE p.company_id = $1 AND ($2 OR p.archived_at IS NULL) ORDER BY p.archived_at NULLS FIRST, p.published_at DESC`, [companyId, q.archived === '1']);
      const out = [];
      for (const p of rows.rows) {
        const ids = await audienceIds(db, companyId, p);
        let myAck: Date | null = null;
        if (u.employeeId) {
          const mine = await db.query<{ acknowledged_at: Date }>('SELECT acknowledged_at FROM policy_ack WHERE policy_id = $1 AND employee_id = $2', [p.id, u.employeeId]);
          myAck = mine.rows[0]?.acknowledged_at ?? null;
        }
        out.push({ ...p, audience_size: ids.length, my_ack_at: myAck, in_audience: !!u.employeeId && !p.archived_at && ids.includes(u.employeeId) });
      }
      return res.json({ policies: out });
    }
    if (!u.employeeId) return res.json({ policies: [] });
    const rows = await db.query<{ id: string; title: string; description: string | null; audience: string; audience_ids: string[]; requires_ack: boolean; published_at: Date; filename: string; my_ack_at: Date | null }>(
      `SELECT p.id, p.title, p.description, p.audience, p.audience_ids, p.requires_ack, p.published_at, f.filename,
              (SELECT acknowledged_at FROM policy_ack a WHERE a.policy_id = p.id AND a.employee_id = $2) AS my_ack_at
         FROM company_policy p JOIN stored_file f ON f.id = p.file_id WHERE p.company_id = $1 AND p.archived_at IS NULL ORDER BY p.published_at DESC`, [companyId, u.employeeId]);
    const out = [];
    for (const p of rows.rows) {
      if ((await audienceIds(db, companyId, p, u.employeeId)).length) {
        out.push({ id: p.id, title: p.title, description: p.description, requires_ack: p.requires_ack, published_at: p.published_at, filename: p.filename, my_ack_at: p.my_ack_at, in_audience: true });
      }
    }
    res.json({ policies: out });
  });

  r.post('/policies', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = policyBody.parse(req.body);
    if (b.audience !== 'all' && !b.audienceIds.length) throw new BadRequestError(`choose at least one ${b.audience}`);
    if (b.audience !== 'all') {
      const ok = b.audience === 'branch'
        ? await db.query<{ n: number }>('SELECT count(*)::int AS n FROM branch WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, b.audienceIds])
        : await db.query<{ n: number }>('SELECT count(*)::int AS n FROM org_unit WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, b.audienceIds]);
      if (ok.rows[0]!.n !== new Set(b.audienceIds).size) throw new BadRequestError(`unknown ${b.audience}`);
    }
    const id = await tx(db, async (t) => {
      const ins = await t.query<{ id: string }>(
        `INSERT INTO company_policy (company_id, title, description, file_id, audience, audience_ids, requires_ack, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
        [companyId, b.title, b.description ?? null, b.fileId, b.audience, b.audience === 'all' ? [] : b.audienceIds, b.requiresAck, req.user!.id]);
      const pid = ins.rows[0]!.id;
      await attachFile(t, { companyId, fileId: b.fileId, userId: req.user!.id, purpose: 'company_document', attachedTo: `company_policy:${pid}` });
      const ids = await audienceIds(t, companyId, { audience: b.audience, audience_ids: b.audience === 'all' ? [] : b.audienceIds });
      for (const employeeId of ids) {
        const userId = await userIdForEmployee(t, employeeId);
        if (!userId) continue;
        await notify(t, {
          companyId, userId, employeeId, template: 'policy.published', title: b.requiresAck ? `Please read: ${b.title}` : `New company document: ${b.title}`,
          body: b.requiresAck ? `${b.title} has been published. Read it and acknowledge it under Profile, Documents & letters.` : `${b.title} is available under Profile, Documents & letters.`,
          payload: { policyId: pid },
        });
        if (b.requiresAck) await t.query('INSERT INTO policy_nudge (policy_id, employee_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [pid, employeeId]);
      }
      await audit(t, { actorId: req.user!.id, action: 'create', table: 'company_policy', rowId: pid, after: { title: b.title, audience: b.audience, requiresAck: b.requiresAck } });
      return pid;
    });
    res.status(201).json({ id });
  });

  r.patch('/policies/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = z.object({ title: z.string().trim().min(1).max(160).optional(), description: z.string().trim().max(2000).nullish(), requiresAck: z.boolean().optional(), archived: z.boolean().optional() }).parse(req.body);
    const u = await db.query(
      `UPDATE company_policy SET title = coalesce($3, title), description = CASE WHEN $4 THEN $5 ELSE description END, requires_ack = coalesce($6, requires_ack),
              archived_at = CASE WHEN $7::boolean IS NULL THEN archived_at WHEN $7 THEN coalesce(archived_at, now()) ELSE NULL END
        WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.title ?? null, b.description !== undefined, b.description ?? null, b.requiresAck ?? null, b.archived ?? null]);
    if (!u.rowCount) throw new NotFoundError('policy not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_policy', rowId: id, after: b });
    res.json({ ok: true });
  });

  async function loadPolicyFor(req: Request, id: string) {
    const companyId = companyIdOf(req);
    const p = await db.query<{ id: string; title: string; file_id: string; audience: string; audience_ids: string[]; requires_ack: boolean; archived_at: Date | null }>(
      'SELECT id, title, file_id, audience, audience_ids, requires_ack, archived_at FROM company_policy WHERE id = $1 AND company_id = $2', [id, companyId]);
    const row = p.rows[0];
    if (!row) throw new NotFoundError('policy not found');
    const u = req.user!;
    const member = !!u.employeeId && !row.archived_at && (await audienceIds(db, companyId, row, u.employeeId)).length > 0;
    if (!isHr(u) && !member) throw new NotFoundError('policy not found');
    return { companyId, row, member };
  }

  r.get('/policies/:id/file', async (req, res) => {
    const { companyId, row } = await loadPolicyFor(req, uuid.parse(req.params.id));
    sendStoredFile(res, await readFile(db, cipher, companyId, row.file_id), { download: req.query.download === '1' });
  });

  r.post('/policies/:id/acknowledge', async (req, res) => {
    const { row, member } = await loadPolicyFor(req, uuid.parse(req.params.id));
    if (!member) throw new ForbiddenError('this policy is not addressed to you');
    await db.query('INSERT INTO policy_ack (policy_id, employee_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [row.id, req.user!.employeeId]);
    res.json({ ok: true });
  });

  r.get('/policies/:id/acks', requireRole('hr'), async (req, res) => {
    const { companyId, row } = await loadPolicyFor(req, uuid.parse(req.params.id));
    const ids = await audienceIds(db, companyId, row);
    const rows = await db.query(
      `SELECT e.id AS employee_id, e.emp_code, e.name_en, a.acknowledged_at, n.last_sent_at AS last_reminded_at
         FROM employee e LEFT JOIN policy_ack a ON a.policy_id = $1 AND a.employee_id = e.id LEFT JOIN policy_nudge n ON n.policy_id = $1 AND n.employee_id = e.id
        WHERE e.id = ANY($2::uuid[]) ORDER BY a.acknowledged_at NULLS FIRST, e.emp_code`, [row.id, ids]);
    res.json({ employees: rows.rows });
  });

  r.post('/policies/:id/remind', requireRole('hr'), async (req, res) => {
    const { companyId, row } = await loadPolicyFor(req, uuid.parse(req.params.id));
    if (row.archived_at) throw new ConflictError('this policy is archived');
    const ids = await audienceIds(db, companyId, row);
    const pending = await db.query<{ id: string }>(
      'SELECT x AS id FROM unnest($2::uuid[]) x WHERE NOT EXISTS (SELECT 1 FROM policy_ack a WHERE a.policy_id = $1 AND a.employee_id = x)', [row.id, ids]);
    let sent = 0;
    for (const { id: employeeId } of pending.rows) {
      const userId = await userIdForEmployee(db, employeeId);
      if (!userId) continue;
      await notify(db, { companyId, userId, employeeId, template: 'policy.reminder', title: `Please read: ${row.title}`, body: `${row.title} is waiting for your acknowledgement under Profile, Documents & letters.`, payload: { policyId: row.id } });
      await db.query('INSERT INTO policy_nudge (policy_id, employee_id) VALUES ($1, $2) ON CONFLICT (policy_id, employee_id) DO UPDATE SET last_sent_at = now()', [row.id, employeeId]);
      sent += 1;
    }
    res.json({ sent });
  });

  return r;
}
