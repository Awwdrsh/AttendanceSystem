import { Router, type Request } from 'express';
import { z } from 'zod';
import { D, Decimal } from '@hajiri/payroll-engine';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import type { AuthUser } from '../auth/service.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { attachFile, readFile, sendStoredFile } from '../files/store.js';
import { sendReport } from '../reports/csv.js';
import { act, canAct, cancelWorkflow, getInstance, isManagerOf, nptToday, startWorkflow } from '../workflow/service.js';
import { bsLabel, bsRange } from '../../bsdate.js';
import { markSourcesPaid } from './payroll.js';
import {
  checkClaim, DESTINATION_LABEL, DESTINATIONS, employeeOf, expensePolicy, money, normaliseItems, quoteTada, rs, type CheckIssue, type ClaimItemInput, type Destination,
} from './service.js';

const amount = z.string().regex(/^\d{1,10}(\.\d{1,2})?$/, 'amount with up to 2 decimals');
const optAmount = amount.nullable().optional();
const fxRate = z.string().regex(/^\d{1,6}(\.\d{1,4})?$/, 'exchange rate with up to 4 decimals');
const destination = z.enum(DESTINATIONS as [Destination, ...Destination[]]);
const itemSchema = z.object({
  expenseDate: isoDate, categoryId: uuid, description: z.string().trim().min(1).max(300),
  amount: amount.optional(), km: z.number().positive().max(5000).optional(), receiptFileId: uuid.nullable().optional(),
});
const claimSchema = z.object({ title: z.string().trim().min(1).max(120), items: z.array(itemSchema).min(1).max(50), submit: z.boolean().optional() });
const scopeSchema = z.enum(['own', 'team', 'all']);
/** Travel bills above this need the bill attached. */
const BILL_RECEIPT_ABOVE = 500;

type Scope = z.infer<typeof scopeSchema>;

function defaultScope(u: AuthUser): Scope { return hasRole(u, 'hr') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own'; }

/** SQL filter for a list: own records, a manager's direct reports (and own), or the whole company for HR. */
function scopeFilter(u: AuthUser, scope: Scope, alias: string, first: number): { sql: string; params: unknown[] } {
  if (scope === 'all') {
    if (!hasRole(u, 'hr')) throw new ForbiddenError('only HR can see everyone');
    return { sql: 'TRUE', params: [] };
  }
  if (scope === 'team') {
    if (!hasRole(u, 'manager', 'hr') || !u.employeeId) throw new ForbiddenError('only managers can see their team');
    return {
      sql: `(${alias}.employee_id = $${first} OR hj_manages($${first}::uuid, ${alias}.employee_id, current_date))`,
      params: [u.employeeId],
    };
  }
  if (!u.employeeId) throw new BadRequestError('your account is not linked to an employee');
  return { sql: `${alias}.employee_id = $${first}`, params: [u.employeeId] };
}

/** Who may read a claim or trip: the employee, their line manager, and HR. Everyone else gets a 404. */
async function assertCanSee(db: Queryable, u: AuthUser, employeeId: string): Promise<void> {
  if (hasRole(u, 'hr') || u.employeeId === employeeId) return;
  if (hasRole(u, 'manager') && u.employeeId && (await isManagerOf(db, u.employeeId, employeeId))) return;
  throw new NotFoundError('not found');
}

function selfEmployee(req: Request): string {
  const id = req.user!.employeeId;
  if (!id) throw new BadRequestError('your account is not linked to an employee');
  return id;
}

/** True when the user may decide the current workflow step (to show approve and reject buttons). */
async function canActOn(db: Queryable, user: AuthUser, instanceId: string): Promise<boolean> {
  const inst = await getInstance(db, instanceId);
  return inst ? canAct(db, user, inst) : false;
}

/** Settle a source's pending payout outside payroll; refused while a pay run being prepared holds it. */
async function payOutside(db: Queryable, sourceType: 'expense_claim' | 'travel_advance', sourceId: string): Promise<void> {
  const p = await db.query<{ id: string; payroll_run_id: string | null }>(
    `SELECT id, payroll_run_id FROM expense_payout WHERE source_type = $1 AND source_id = $2 AND status = 'pending' FOR UPDATE`, [sourceType, sourceId]);
  if (!p.rows.length) throw new ConflictError('nothing is waiting to be paid');
  if (p.rows.some((x) => x.payroll_run_id)) throw new ConflictError('This payment is already in a pay run being prepared. Pay it in that run instead.');
  await db.query(`UPDATE expense_payout SET status = 'cash', paid_amount = amount, paid_at = now() WHERE source_type = $1 AND source_id = $2 AND status = 'pending'`, [sourceType, sourceId]);
  await markSourcesPaid(db, [{ source_type: sourceType, source_id: sourceId }]);
}

async function claimRow(db: Queryable, companyId: string, id: string) {
  const r = await db.query<{ id: string; employee_id: string; title: string; status: string; total_claimed: string; total_approved: string | null; workflow_instance_id: string | null; payment_method: string | null }>(
    `SELECT id, employee_id, title, status, total_claimed::text, total_approved::text, workflow_instance_id, payment_method FROM expense_claim WHERE id = $1 AND company_id = $2`, [id, companyId]);
  if (!r.rows[0]) throw new NotFoundError('claim not found');
  return r.rows[0];
}

export function expensesRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  // ---------------------------------------------------------------- policy
  r.get('/policy', async (req, res) => {
    const companyId = companyIdOf(req);
    const [settings, cats, limits, rates] = await Promise.all([
      expensePolicy(db, companyId),
      db.query('SELECT id, code, name, kind, receipt_required_above::text AS "receiptRequiredAbove", active, sort_order AS "sortOrder" FROM expense_category WHERE company_id = $1 ORDER BY sort_order, name', [companyId]),
      db.query('SELECT id, category_id AS "categoryId", grade, per_claim::text AS "perClaim", per_month::text AS "perMonth" FROM expense_limit WHERE company_id = $1 ORDER BY grade', [companyId]),
      db.query(`SELECT grade, destination, da_rate::text AS "daRate", lodging_rate::text AS "lodgingRate" FROM tada_rate WHERE company_id = $1 ORDER BY (grade = '*') DESC, grade, destination`, [companyId]),
    ]);
    const grade = req.user!.employeeId ? (await employeeOf(db, companyId, req.user!.employeeId)).grade : null;
    const grades = hasRole(req.user!, 'hr')
      ? (await db.query<{ grade: string }>(`SELECT DISTINCT grade FROM employee WHERE company_id = $1 AND grade IS NOT NULL ORDER BY grade`, [companyId])).rows.map((g) => g.grade) : [];
    res.json({ settings, categories: cats.rows, limits: limits.rows, tadaRates: rates.rows, destinations: DESTINATIONS.map((d) => ({ value: d, label: DESTINATION_LABEL[d] })), myGrade: grade, grades });
  });

  r.put('/policy/settings', requireRole('hr'), async (req, res) => {
    const b = z.object({ mileageRatePerKm: amount, limitMode: z.enum(['block', 'warn']), claimWindowDays: z.number().int().min(7).max(365) }).parse(req.body);
    const companyId = companyIdOf(req);
    const before = await expensePolicy(db, companyId);
    await db.query(`INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'expense.policy', $2) ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value`, [companyId, JSON.stringify(b)]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: null, before: { key: 'expense.policy', ...before }, after: b });
    res.json({ settings: await expensePolicy(db, companyId) });
  });

  const categoryBody = z.object({
    code: z.string().regex(/^[A-Z][A-Z0-9_]{1,29}$/, 'capital letters, digits and _'), name: z.string().trim().min(1).max(80),
    kind: z.enum(['amount', 'mileage']), receiptRequiredAbove: optAmount, active: z.boolean().optional(), sortOrder: z.number().int().min(0).max(9999).optional(),
  });
  r.post('/categories', requireRole('hr'), async (req, res) => {
    const b = categoryBody.parse(req.body);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO expense_category (company_id, code, name, kind, receipt_required_above, active, sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
      [companyIdOf(req), b.code, b.name, b.kind, b.receiptRequiredAbove ?? null, b.active ?? true, b.sortOrder ?? 100]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'expense_category', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });

  r.patch('/categories/:id', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = categoryBody.omit({ code: true }).partial().parse(req.body);
    const u = await db.query(
      `UPDATE expense_category SET name = coalesce($3, name), kind = coalesce($4, kind),
              receipt_required_above = CASE WHEN $5::boolean THEN $6::numeric ELSE receipt_required_above END,
              active = coalesce($7, active), sort_order = coalesce($8, sort_order)
        WHERE id = $1 AND company_id = $2 RETURNING id`,
      [id, companyIdOf(req), b.name ?? null, b.kind ?? null, b.receiptRequiredAbove !== undefined, b.receiptRequiredAbove ?? null, b.active ?? null, b.sortOrder ?? null]);
    if (!u.rows[0]) throw new NotFoundError('category not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'expense_category', rowId: id, after: b });
    res.json({ ok: true });
  });

  r.put('/categories/:id/limits', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ limits: z.array(z.object({ grade: z.string().trim().min(1).max(40), perClaim: optAmount, perMonth: optAmount })).max(50) }).parse(req.body);
    const companyId = companyIdOf(req);
    const grades = b.limits.map((l) => l.grade);
    if (new Set(grades).size !== grades.length) throw new BadRequestError('each grade can appear only once');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const c = await client.query('SELECT 1 FROM expense_category WHERE id = $1 AND company_id = $2', [id, companyId]);
      if (!c.rows[0]) throw new NotFoundError('category not found');
      await client.query('DELETE FROM expense_limit WHERE category_id = $1', [id]);
      for (const l of b.limits) {
        if (!l.perClaim && !l.perMonth) continue;
        await client.query('INSERT INTO expense_limit (company_id, category_id, grade, per_claim, per_month) VALUES ($1, $2, $3, $4, $5)', [companyId, id, l.grade, l.perClaim ?? null, l.perMonth ?? null]);
      }
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'expense_limit', rowId: id, after: b.limits });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true });
  });

  r.put('/tada-rates', requireRole('hr'), async (req, res) => {
    const b = z.object({ rates: z.array(z.object({ grade: z.string().trim().min(1).max(40), destination, daRate: amount, lodgingRate: amount })).max(200) }).parse(req.body);
    const keys = b.rates.map((x) => `${x.grade}|${x.destination}`);
    if (new Set(keys).size !== keys.length) throw new BadRequestError('each grade and destination can appear only once');
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await client.query('DELETE FROM tada_rate WHERE company_id = $1', [companyId]);
      for (const x of b.rates) await client.query('INSERT INTO tada_rate (company_id, grade, destination, da_rate, lodging_rate) VALUES ($1, $2, $3, $4, $5)', [companyId, x.grade, x.destination, x.daRate, x.lodgingRate]);
      await audit(client, { actorId: req.user!.id, action: 'update', table: 'tada_rate', rowId: null, after: b.rates });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- claims
  r.post('/claims/check', async (req, res) => {
    const b = z.object({ items: z.array(itemSchema).min(1).max(50), claimId: uuid.optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const items = await normaliseItems(db, companyId, b.items as ClaimItemInput[]);
    const issues = await checkClaim(db, companyId, selfEmployee(req), items, b.claimId ?? null);
    res.json({ amounts: items.map((i) => i.amount), total: money(items.reduce((a, i) => a.plus(i.amount), D(0))), issues });
  });

  /** Replace the claim's lines, attaching each receipt to the claim. Submitting refuses blocking issues. */
  const writeItems = async (client: Queryable, companyId: string, userId: string, employeeId: string, claimId: string, raw: ClaimItemInput[], submitting: boolean) => {
    const items = await normaliseItems(client, companyId, raw);
    const issues = await checkClaim(client, companyId, employeeId, items, claimId);
    const blocks = issues.filter((i) => i.level === 'block');
    if (submitting && blocks.length) throw new BadRequestError(blocks.map((b) => b.message).join(' '), { code: 'claim_blocked', issues });
    await client.query('DELETE FROM expense_item WHERE claim_id = $1', [claimId]);
    for (const [i, it] of items.entries()) {
      if (it.receiptFileId) await attachFile(client, { companyId, fileId: it.receiptFileId, userId, purpose: 'expense_receipt', attachedTo: `expense_claim:${claimId}` });
      await client.query(
        `INSERT INTO expense_item (claim_id, expense_date, category_id, description, amount, km, receipt_file_id, sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
        [claimId, it.expenseDate, it.categoryId, it.description, it.amount, it.km ?? null, it.receiptFileId ?? null, i]);
    }
    const total = items.reduce((a, it) => a.plus(it.amount), D(0));
    await client.query('UPDATE expense_claim SET total_claimed = $2, updated_at = now() WHERE id = $1', [claimId, money(total)]);
    return { issues, total };
  };

  const submitClaim = async (client: Queryable, companyId: string, claimId: string, employeeId: string, title: string, total: string) => {
    const emp = await employeeOf(client, companyId, employeeId);
    const inst = await startWorkflow(client, {
      companyId, objectType: 'expense_claim', objectId: claimId, employeeId, amount: Number(total),
      title: 'Expense claim to approve', body: `${emp.name_en}: ${title}, ${rs(total)}`,
    });
    await client.query(`UPDATE expense_claim SET status = 'pending', submitted_at = now(), workflow_instance_id = $2, updated_at = now() WHERE id = $1`, [claimId, inst.id]);
  };

  r.post('/claims', async (req, res) => {
    const b = claimSchema.parse(req.body);
    const companyId = companyIdOf(req);
    const employeeId = selfEmployee(req);
    const emp = await employeeOf(db, companyId, employeeId);
    if (emp.status !== 'active') throw new ForbiddenError('only active employees can claim expenses');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>('INSERT INTO expense_claim (company_id, employee_id, title, created_by) VALUES ($1, $2, $3, $4) RETURNING id', [companyId, employeeId, b.title, req.user!.id]);
      const id = ins.rows[0]!.id;
      const { issues, total } = await writeItems(client, companyId, req.user!.id, employeeId, id, b.items as ClaimItemInput[], !!b.submit);
      if (b.submit) await submitClaim(client, companyId, id, employeeId, b.title, money(total));
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'expense_claim', rowId: id, after: { title: b.title, total: money(total), submitted: !!b.submit } });
      await client.query('COMMIT');
      res.status(201).json({ id, status: b.submit ? 'pending' : 'draft', total: money(total), issues });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.put('/claims/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = claimSchema.parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const c = await client.query<{ employee_id: string; status: string }>('SELECT employee_id, status FROM expense_claim WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const row = c.rows[0];
      if (!row || row.employee_id !== req.user!.employeeId) throw new NotFoundError('claim not found');
      if (row.status !== 'draft') throw new ConflictError('only a draft claim can be edited');
      await client.query('UPDATE expense_claim SET title = $2 WHERE id = $1', [id, b.title]);
      const { issues, total } = await writeItems(client, companyId, req.user!.id, row.employee_id, id, b.items as ClaimItemInput[], !!b.submit);
      if (b.submit) await submitClaim(client, companyId, id, row.employee_id, b.title, money(total));
      await client.query('COMMIT');
      res.json({ id, status: b.submit ? 'pending' : 'draft', total: money(total), issues });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.post('/claims/:id/submit', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const c = await client.query<{ employee_id: string; status: string; title: string }>('SELECT employee_id, status, title FROM expense_claim WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const row = c.rows[0];
      if (!row || row.employee_id !== req.user!.employeeId) throw new NotFoundError('claim not found');
      if (row.status !== 'draft') throw new ConflictError(`claim is ${row.status}`);
      const items = await client.query<{ expense_date: string; category_id: string; description: string; amount: string; km: string | null; receipt_file_id: string | null }>(
        'SELECT expense_date::text, category_id, description, amount::text, km::text, receipt_file_id FROM expense_item WHERE claim_id = $1 ORDER BY sort_order', [id]);
      if (!items.rows.length) throw new BadRequestError('add at least one expense');
      const raw: ClaimItemInput[] = items.rows.map((i) => ({ expenseDate: i.expense_date, categoryId: i.category_id, description: i.description, amount: i.amount, km: i.km ? Number(i.km) : undefined, receiptFileId: i.receipt_file_id }));
      const { total } = await writeItems(client, companyId, req.user!.id, row.employee_id, id, raw, true);
      await submitClaim(client, companyId, id, row.employee_id, row.title, money(total));
      await client.query('COMMIT');
      res.json({ id, status: 'pending' });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.post('/claims/:id/cancel', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const c = await claimRow(db, companyIdOf(req), id);
    if (c.employee_id !== req.user!.employeeId) throw new NotFoundError('claim not found');
    if (!['draft', 'pending'].includes(c.status)) throw new ConflictError(`a ${c.status} claim cannot be cancelled`);
    await db.query(`UPDATE expense_claim SET status = 'cancelled', updated_at = now() WHERE id = $1`, [id]);
    await cancelWorkflow(db, 'expense_claim', id);
    res.json({ id, status: 'cancelled' });
  });

  r.get('/claims', async (req, res) => {
    const q = z.object({ scope: scopeSchema.optional(), status: z.enum(['draft', 'pending', 'approved', 'rejected', 'cancelled', 'paid']).optional(), employeeId: uuid.optional() }).parse(req.query);
    const u = req.user!;
    const scope = q.scope ?? defaultScope(u);
    const f = scopeFilter(u, scope, 'c', 2);
    const params: unknown[] = [companyIdOf(req), ...f.params];
    let extra = '';
    if (q.status) { params.push(q.status); extra += ` AND c.status = $${params.length}`; }
    if (q.employeeId) { params.push(q.employeeId); extra += ` AND c.employee_id = $${params.length}`; }
    // Drafts are private to the employee.
    if (u.employeeId) { params.push(u.employeeId); extra += ` AND (c.status <> 'draft' OR c.employee_id = $${params.length})`; } else extra += ` AND c.status <> 'draft'`;
    const rows = await db.query(
      `SELECT c.id, c.title, c.status, c.total_claimed::text AS "totalClaimed", c.total_approved::text AS "totalApproved", c.submitted_at AS "submittedAt",
              c.decided_at AS "decidedAt", c.payment_method AS "paymentMethod", c.paid_at AS "paidAt", c.created_at AS "createdAt",
              e.id AS "employeeId", e.emp_code AS "empCode", e.name_en AS "employeeName",
              (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id) AS items,
              (SELECT min(i.expense_date)::text FROM expense_item i WHERE i.claim_id = c.id) AS "firstDate",
              (SELECT max(i.expense_date)::text FROM expense_item i WHERE i.claim_id = c.id) AS "lastDate",
              p.status AS "payoutStatus", pp.bs_year AS "payBsYear", pp.bs_month AS "payBsMonth"
         FROM expense_claim c JOIN employee e ON e.id = c.employee_id
         LEFT JOIN expense_payout p ON p.source_type = 'expense_claim' AND p.source_id = c.id AND p.parent_payout_id IS NULL
         LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
        WHERE c.company_id = $1 AND ${f.sql}${extra}
        ORDER BY c.created_at DESC LIMIT 500`, params);
    res.json({ claims: rows.rows });
  });

  r.get('/claims/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const c = await claimRow(db, companyId, id);
    await assertCanSee(db, req.user!, c.employee_id);
    if (c.status === 'draft' && c.employee_id !== req.user!.employeeId) throw new NotFoundError('claim not found');
    const emp = await employeeOf(db, companyId, c.employee_id);
    const items = await db.query<{ expenseDate: string; categoryId: string; description: string; amount: string; km: string | null; receiptFileId: string | null }>(
      `SELECT i.id, i.expense_date::text AS "expenseDate", i.category_id AS "categoryId", ec.name AS "categoryName", ec.kind AS "categoryKind", i.description, i.amount::text AS amount,
              i.km::text AS km, i.receipt_file_id AS "receiptFileId", f.filename AS "receiptName", f.mime AS "receiptMime", i.approved_amount::text AS "approvedAmount", i.adjust_reason AS "adjustReason"
         FROM expense_item i JOIN expense_category ec ON ec.id = i.category_id LEFT JOIN stored_file f ON f.id = i.receipt_file_id AND f.deleted_at IS NULL
        WHERE i.claim_id = $1 ORDER BY i.sort_order`, [id]);
    const full = await db.query(`SELECT decision_note AS "decisionNote", submitted_at AS "submittedAt", decided_at AS "decidedAt", paid_at AS "paidAt", created_at AS "createdAt" FROM expense_claim WHERE id = $1`, [id]);
    const payouts = await db.query(
      `SELECT p.id, p.component_code AS code, p.amount::text, p.status, p.paid_amount::text AS "paidAmount", p.paid_at AS "paidAt", pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
         FROM expense_payout p LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
        WHERE p.source_type = 'expense_claim' AND p.source_id = $1 ORDER BY p.created_at`, [id]);
    // Approvers see the same limit and receipt checks the employee saw when submitting.
    let issues: CheckIssue[] = [];
    if ((c.status === 'draft' || c.status === 'pending') && items.rows.length) {
      try {
        const norm = await normaliseItems(db, companyId, items.rows.map((i) => ({ expenseDate: i.expenseDate, categoryId: i.categoryId, description: i.description, amount: i.amount, km: i.km ? Number(i.km) : undefined, receiptFileId: i.receiptFileId })));
        issues = await checkClaim(db, companyId, c.employee_id, norm, id);
      } catch { issues = []; }
    }
    const canDecide = c.status === 'pending' && c.workflow_instance_id ? await canActOn(db, req.user!, c.workflow_instance_id) : false;
    res.json({
      claim: { id: c.id, title: c.title, status: c.status, totalClaimed: c.total_claimed, totalApproved: c.total_approved, paymentMethod: c.payment_method, ...full.rows[0],
        employee: { id: emp.id, empCode: emp.emp_code, name: emp.name_en, grade: emp.grade } },
      items: items.rows, payouts: payouts.rows, issues, canDecide,
    });
  });

  r.get('/claims/:id/items/:itemId/receipt', async (req, res) => {
    const id = uuid.parse(req.params.id); const itemId = uuid.parse(req.params.itemId);
    const companyId = companyIdOf(req);
    const c = await claimRow(db, companyId, id);
    await assertCanSee(db, req.user!, c.employee_id);
    const i = await db.query<{ receipt_file_id: string | null }>('SELECT receipt_file_id FROM expense_item WHERE id = $1 AND claim_id = $2', [itemId, id]);
    const fileId = i.rows[0]?.receipt_file_id;
    if (!fileId) throw new NotFoundError('no receipt for this line');
    if (c.employee_id !== req.user!.employeeId) await audit(db, { actorId: req.user!.id, action: 'view_receipt', table: 'expense_item', rowId: itemId });
    sendStoredFile(res, await readFile(db, cipher, companyId, fileId), { download: req.query.download === '1' });
  });

  /** Approve (optionally reducing line amounts, with a reason) or reject, as the current approver. */
  r.post('/claims/:id/review', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({
      decision: z.enum(['approve', 'reject']), reason: z.string().trim().max(1000).optional(),
      items: z.array(z.object({ id: uuid, approvedAmount: amount, reason: z.string().trim().max(300).optional() })).max(50).optional(),
    }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const c = await client.query<{ status: string; workflow_instance_id: string | null }>('SELECT status, workflow_instance_id FROM expense_claim WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const row = c.rows[0];
      if (!row) throw new NotFoundError('claim not found');
      if (row.status !== 'pending' || !row.workflow_instance_id) throw new ConflictError(`claim is ${row.status}`);
      if (b.decision === 'approve' && b.items?.length) {
        const lines = await client.query<{ id: string; amount: string; description: string }>('SELECT id, amount::text, description FROM expense_item WHERE claim_id = $1', [id]);
        const byId = new Map(lines.rows.map((l) => [l.id, l]));
        for (const it of b.items) {
          const line = byId.get(it.id);
          if (!line) throw new BadRequestError('a line does not belong to this claim');
          if (D(it.approvedAmount).gt(line.amount)) throw new BadRequestError(`"${line.description}": the approved amount cannot be more than claimed.`);
          const reduced = D(it.approvedAmount).lt(line.amount);
          if (reduced && !it.reason) throw new BadRequestError(`"${line.description}": give a reason for approving less.`);
          await client.query('UPDATE expense_item SET approved_amount = $2, adjust_reason = $3 WHERE id = $1', [it.id, money(it.approvedAmount), reduced ? it.reason : null]);
        }
        const t = await client.query<{ total: string }>('SELECT coalesce(sum(coalesce(approved_amount, amount)), 0)::text AS total FROM expense_item WHERE claim_id = $1', [id]);
        if (!D(t.rows[0]!.total).gt(0)) throw new BadRequestError('Nothing would be paid; reject the claim instead.');
      }
      const state = await act(client, { instanceId: row.workflow_instance_id, user: req.user!, decision: b.decision, reason: b.reason ?? null });
      await client.query('COMMIT');
      res.json({ state });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  /** HR paid an approved claim in cash or by transfer outside payroll; payroll will not pay it again. */
  r.post('/claims/:id/pay-outside', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ note: z.string().trim().min(3).max(300) }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const c = await client.query<{ status: string }>('SELECT status FROM expense_claim WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      if (!c.rows[0]) throw new NotFoundError('claim not found');
      if (c.rows[0].status !== 'approved') throw new ConflictError(`claim is ${c.rows[0].status}`);
      await client.query(`UPDATE expense_claim SET payment_method = 'cash' WHERE id = $1`, [id]);
      await payOutside(client, 'expense_claim', id);
      await client.query(`UPDATE expense_claim SET decision_note = concat_ws(' · ', decision_note, $2::text), updated_at = now() WHERE id = $1`, [id, `Paid outside payroll: ${b.note}`]);
      await audit(client, { actorId: req.user!.id, action: 'pay_outside_payroll', table: 'expense_claim', rowId: id, after: { note: b.note } });
      await client.query('COMMIT');
      res.json({ id, status: 'paid' });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  // ---------------------------------------------------------------- travel
  const travelBody = z.object({
    destination: z.string().trim().min(1).max(120), destinationCategory: destination, purpose: z.string().trim().min(3).max(1000),
    fromDate: isoDate, toDate: isoDate, mode: z.enum(['bus', 'air', 'office_vehicle', 'own_vehicle', 'other']),
    estimatedCost: amount.optional(), advanceRequested: amount.optional(), fxRate: fxRate.nullable().optional(),
  });

  r.post('/travel/quote', async (req, res) => {
    const b = z.object({ destinationCategory: destination, fromDate: isoDate, toDate: isoDate, fxRate: fxRate.nullable().optional() }).parse(req.body);
    res.json({ quote: await quoteTada(db, companyIdOf(req), selfEmployee(req), b.destinationCategory, b.fromDate, b.toDate, b.fxRate ?? null) });
  });

  r.post('/travel', async (req, res) => {
    const b = travelBody.parse(req.body);
    const companyId = companyIdOf(req);
    const employeeId = selfEmployee(req);
    const emp = await employeeOf(db, companyId, employeeId);
    if (emp.status !== 'active') throw new ForbiddenError('only active employees can request travel');
    if (b.toDate < nptToday()) throw new BadRequestError('This trip has already ended. Ask HR to record it.');
    const q = await quoteTada(db, companyId, employeeId, b.destinationCategory, b.fromDate, b.toDate, b.fxRate ?? null);
    const overlap = await db.query(`SELECT 1 FROM travel_request WHERE employee_id = $1 AND status IN ('pending', 'approved', 'settling') AND from_date <= $3 AND to_date >= $2`, [employeeId, b.fromDate, b.toDate]);
    if (overlap.rows[0]) throw new ConflictError('You already have a trip on these dates.');
    const advance = D(b.advanceRequested ?? '0');
    const ceiling = D(q.daAmount).plus(q.lodgingLimit).plus(b.estimatedCost ?? '0');
    if (advance.gt(ceiling)) throw new BadRequestError(`The advance cannot be more than the expected cost of the trip (${rs(ceiling)}).`);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>(
        `INSERT INTO travel_request (company_id, employee_id, destination, destination_category, purpose, from_date, to_date, mode, estimated_cost, advance_requested, fx_rate,
                                     days, nights, da_rate, lodging_rate, da_amount, lodging_limit, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) RETURNING id`,
        [companyId, employeeId, b.destination, b.destinationCategory, b.purpose, b.fromDate, b.toDate, b.mode, b.estimatedCost ?? '0', money(advance), q.fxRate,
          q.days, q.nights, q.daRate, q.lodgingRate, q.daAmount, q.lodgingLimit, req.user!.id]);
      const id = ins.rows[0]!.id;
      const inst = await startWorkflow(client, {
        companyId, objectType: 'travel_request', objectId: id, employeeId, amount: Number(ceiling.toString()),
        title: 'Travel request to approve', body: `${emp.name_en}: ${b.destination}, ${bsRange(b.fromDate, b.toDate)}${advance.gt(0) ? `, advance ${rs(advance)}` : ''}`,
      });
      await client.query('UPDATE travel_request SET workflow_instance_id = $2 WHERE id = $1', [id, inst.id]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'travel_request', rowId: id, after: { ...b, quote: q } });
      await client.query('COMMIT');
      res.status(201).json({ id, status: 'pending', quote: q });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.get('/travel', async (req, res) => {
    const q = z.object({ scope: scopeSchema.optional(), status: z.enum(['pending', 'approved', 'rejected', 'cancelled', 'settling', 'settled']).optional() }).parse(req.query);
    const u = req.user!;
    const f = scopeFilter(u, q.scope ?? defaultScope(u), 't', 2);
    const params: unknown[] = [companyIdOf(req), ...f.params];
    let extra = '';
    if (q.status) { params.push(q.status); extra = ` AND t.status = $${params.length}`; }
    const rows = await db.query(
      `SELECT t.id, t.destination, t.destination_category AS "destinationCategory", t.purpose, t.from_date::text AS "fromDate", t.to_date::text AS "toDate", t.mode, t.days,
              t.da_amount::text AS "daAmount", t.lodging_limit::text AS "lodgingLimit", t.estimated_cost::text AS "estimatedCost", t.advance_requested::text AS "advanceRequested",
              t.advance_approved::text AS "advanceApproved", t.advance_status AS "advanceStatus", t.advance_method AS "advanceMethod", t.status, t.created_at AS "createdAt",
              e.id AS "employeeId", e.emp_code AS "empCode", e.name_en AS "employeeName",
              (SELECT jsonb_build_object('id', s.id, 'status', s.status, 'net', s.net::text) FROM travel_settlement s WHERE s.travel_request_id = t.id ORDER BY s.created_at DESC LIMIT 1) AS settlement
         FROM travel_request t JOIN employee e ON e.id = t.employee_id
        WHERE t.company_id = $1 AND ${f.sql}${extra} ORDER BY t.from_date DESC LIMIT 500`, params);
    res.json({ trips: rows.rows });
  });

  const travelRow = async (q: Queryable, companyId: string, id: string, lock = false) => {
    const r2 = await q.query<{ id: string; employee_id: string; status: string; destination: string; from_date: string; to_date: string; advance_approved: string | null; advance_status: string;
      da_rate: string; lodging_rate: string; fx_rate: string | null; workflow_instance_id: string | null; advance_requested: string }>(
      `SELECT id, employee_id, status, destination, from_date::text, to_date::text, advance_approved::text, advance_status, da_rate::text, lodging_rate::text, fx_rate::text,
              workflow_instance_id, advance_requested::text
         FROM travel_request WHERE id = $1 AND company_id = $2${lock ? ' FOR UPDATE' : ''}`, [id, companyId]);
    if (!r2.rows[0]) throw new NotFoundError('travel request not found');
    return r2.rows[0];
  };

  r.get('/travel/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const t = await travelRow(db, companyId, id);
    await assertCanSee(db, req.user!, t.employee_id);
    const emp = await employeeOf(db, companyId, t.employee_id);
    const full = await db.query<Record<string, unknown> & { destinationCategory: Destination }>(
      `SELECT id, destination, destination_category AS "destinationCategory", purpose, from_date::text AS "fromDate", to_date::text AS "toDate", mode, estimated_cost::text AS "estimatedCost",
              advance_requested::text AS "advanceRequested", advance_approved::text AS "advanceApproved", advance_status AS "advanceStatus", advance_method AS "advanceMethod",
              fx_rate::text AS "fxRate", days, nights, da_rate::text AS "daRate", lodging_rate::text AS "lodgingRate", da_amount::text AS "daAmount", lodging_limit::text AS "lodgingLimit",
              status, decision_note AS "decisionNote", decided_at AS "decidedAt", created_at AS "createdAt"
         FROM travel_request WHERE id = $1`, [id]);
    const settlements = await db.query<{ status: string; workflowInstanceId: string | null }>(
      `SELECT s.id, s.status, s.actual_from::text AS "actualFrom", s.actual_to::text AS "actualTo", s.days, s.nights, s.da_amount::text AS "daAmount", s.lodging_claimed::text AS "lodgingClaimed",
              s.lodging_allowed::text AS "lodgingAllowed", s.other_bills::text AS "otherBills", s.entitlement::text, s.advance::text, s.net::text, s.note, s.decision_note AS "decisionNote",
              s.created_at AS "createdAt", s.workflow_instance_id AS "workflowInstanceId",
              coalesce((SELECT jsonb_agg(jsonb_build_object('id', b.id, 'kind', b.kind, 'billDate', b.bill_date::text, 'description', b.description, 'amount', b.amount::text,
                         'fileId', b.file_id, 'fileName', f.filename) ORDER BY b.bill_date) FROM travel_bill b LEFT JOIN stored_file f ON f.id = b.file_id WHERE b.settlement_id = s.id), '[]') AS bills
         FROM travel_settlement s WHERE s.travel_request_id = $1 ORDER BY s.created_at DESC`, [id]);
    const payouts = await db.query(
      `SELECT p.id, p.source_type AS "sourceType", p.component_code AS code, p.amount::text, p.status, p.paid_amount::text AS "paidAmount", p.paid_at AS "paidAt", pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
         FROM expense_payout p LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
        WHERE (p.source_type = 'travel_advance' AND p.source_id = $1) OR (p.source_type = 'travel_settlement' AND p.source_id IN (SELECT id FROM travel_settlement WHERE travel_request_id = $1))
        ORDER BY p.created_at`, [id]);
    const pendingSettlement = settlements.rows.find((s) => s.status === 'pending');
    const trip = full.rows[0]!;
    res.json({
      trip: { ...trip, destinationLabel: DESTINATION_LABEL[trip.destinationCategory], employee: { id: emp.id, empCode: emp.emp_code, name: emp.name_en, grade: emp.grade } },
      settlements: settlements.rows, payouts: payouts.rows,
      canDecide: t.status === 'pending' && t.workflow_instance_id ? await canActOn(db, req.user!, t.workflow_instance_id) : false,
      canDecideSettlement: pendingSettlement?.workflowInstanceId ? await canActOn(db, req.user!, pendingSettlement.workflowInstanceId) : false,
      canSettle: t.employee_id === req.user!.employeeId && t.status === 'approved' && t.from_date <= nptToday(),
    });
  });

  r.post('/travel/:id/cancel', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const t = await travelRow(client, companyId, id, true);
      if (t.employee_id !== req.user!.employeeId && !hasRole(req.user!, 'hr')) throw new NotFoundError('travel request not found');
      if (!['pending', 'approved'].includes(t.status)) throw new ConflictError(`a ${t.status} trip cannot be cancelled`);
      if (t.advance_status === 'paid') throw new ConflictError('The advance has been paid; submit a settlement instead so it can be recovered.');
      const held = await client.query(`SELECT 1 FROM expense_payout WHERE source_type = 'travel_advance' AND source_id = $1 AND status = 'pending' AND payroll_run_id IS NOT NULL`, [id]);
      if (held.rows[0]) throw new ConflictError('The advance is already in a pay run being prepared; submit a settlement instead.');
      await client.query(`UPDATE expense_payout SET status = 'cancelled' WHERE source_type = 'travel_advance' AND source_id = $1 AND status = 'pending'`, [id]);
      await client.query(`UPDATE travel_request SET status = 'cancelled', advance_status = 'none' WHERE id = $1`, [id]);
      await cancelWorkflow(client, 'travel_request', id);
      await audit(client, { actorId: req.user!.id, action: 'cancel', table: 'travel_request', rowId: id });
      await client.query('COMMIT');
      res.json({ id, status: 'cancelled' });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.post('/travel/:id/review', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ decision: z.enum(['approve', 'reject']), reason: z.string().trim().max(1000).optional(), advanceApproved: amount.optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const t = await travelRow(client, companyId, id, true);
      if (t.status !== 'pending' || !t.workflow_instance_id) throw new ConflictError(`travel request is ${t.status}`);
      if (b.decision === 'approve' && b.advanceApproved !== undefined) {
        if (D(b.advanceApproved).gt(t.advance_requested)) throw new BadRequestError('The approved advance cannot be more than requested.');
        await client.query('UPDATE travel_request SET advance_approved = $2 WHERE id = $1', [id, money(b.advanceApproved)]);
      }
      const state = await act(client, { instanceId: t.workflow_instance_id, user: req.user!, decision: b.decision, reason: b.reason ?? null });
      await client.query('COMMIT');
      res.json({ state });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.post('/travel/:id/advance-outside', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ note: z.string().trim().min(3).max(300) }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const t = await travelRow(client, companyId, id, true);
      if (t.advance_status !== 'pending') throw new ConflictError('there is no advance waiting to be paid');
      await payOutside(client, 'travel_advance', id);
      await client.query(`UPDATE travel_request SET advance_status = 'paid', advance_method = 'cash' WHERE id = $1`, [id]);
      await audit(client, { actorId: req.user!.id, action: 'pay_outside_payroll', table: 'travel_request', rowId: id, after: { note: b.note } });
      await client.query('COMMIT');
      res.json({ id, advanceStatus: 'paid' });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  // ---------------------------------------------------------------- settlement
  const billSchema = z.object({ kind: z.enum(['transport', 'lodging', 'other']), billDate: isoDate, description: z.string().trim().min(1).max(300), amount, fileId: uuid.nullable().optional() });

  r.post('/travel/:id/settlement', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ actualFrom: isoDate, actualTo: isoDate, bills: z.array(billSchema).max(60), note: z.string().trim().max(1000).optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const t = await travelRow(client, companyId, id, true);
      if (t.employee_id !== req.user!.employeeId) throw new NotFoundError('travel request not found');
      if (t.status !== 'approved') throw new ConflictError(t.status === 'settling' ? 'a settlement is already waiting for approval' : `a ${t.status} trip cannot be settled`);
      const today = nptToday();
      if (b.actualFrom > today) throw new BadRequestError('Settle the trip once it has started.');
      if (b.actualTo < b.actualFrom) throw new BadRequestError('The return date must be on or after the start date.');
      if (b.actualTo > today) throw new BadRequestError('The return date cannot be in the future.');
      b.bills.forEach((x, i) => {
        if (x.billDate < b.actualFrom || x.billDate > b.actualTo) throw new BadRequestError(`Bill ${i + 1}: the date must be within the trip.`);
        if (D(x.amount).gt(BILL_RECEIPT_ABOVE) && !x.fileId) throw new BadRequestError(`Bill ${i + 1}: attach the bill for amounts above ${rs(BILL_RECEIPT_ABOVE)}.`);
      });
      const days = Math.round((new Date(`${b.actualTo}T00:00:00Z`).getTime() - new Date(`${b.actualFrom}T00:00:00Z`).getTime()) / 86_400_000) + 1;
      if (days > 90) throw new BadRequestError('A single trip can be at most 90 days.');
      const nights = days - 1;
      const fx = D(t.fx_rate ?? 1);
      // Daily allowance for each day travelled, lodging up to the nightly limit, transport and other bills as spent.
      const da = D(t.da_rate).times(fx).times(days);
      const lodgingClaimed = b.bills.filter((x) => x.kind === 'lodging').reduce((a, x) => a.plus(x.amount), D(0));
      const lodgingAllowed = Decimal.min(lodgingClaimed, D(t.lodging_rate).times(fx).times(nights));
      const other = b.bills.filter((x) => x.kind !== 'lodging').reduce((a, x) => a.plus(x.amount), D(0));
      const entitlement = da.plus(lodgingAllowed).plus(other);
      const advance = ['pending', 'paid'].includes(t.advance_status) ? D(t.advance_approved ?? 0) : D(0);
      const net = entitlement.minus(advance);
      const ins = await client.query<{ id: string }>(
        `INSERT INTO travel_settlement (company_id, travel_request_id, employee_id, actual_from, actual_to, days, nights, da_amount, lodging_claimed, lodging_allowed, other_bills,
                                        entitlement, advance, net, note, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id`,
        [companyId, id, t.employee_id, b.actualFrom, b.actualTo, days, nights, money(da), money(lodgingClaimed), money(lodgingAllowed), money(other), money(entitlement), money(advance), money(net), b.note ?? null, req.user!.id]);
      const sid = ins.rows[0]!.id;
      for (const x of b.bills) {
        if (x.fileId) await attachFile(client, { companyId, fileId: x.fileId, userId: req.user!.id, purpose: ['travel_bill', 'expense_receipt'], attachedTo: `travel_settlement:${sid}` });
        await client.query('INSERT INTO travel_bill (settlement_id, kind, bill_date, description, amount, file_id) VALUES ($1, $2, $3, $4, $5, $6)', [sid, x.kind, x.billDate, x.description, money(x.amount), x.fileId ?? null]);
      }
      const emp = await employeeOf(client, companyId, t.employee_id);
      const inst = await startWorkflow(client, {
        companyId, objectType: 'travel_settlement', objectId: sid, employeeId: t.employee_id, amount: Number(entitlement.toString()),
        title: 'Travel settlement to approve', body: `${emp.name_en}: ${t.destination}, ${net.gte(0) ? `pay ${rs(net)}` : `recover ${rs(net.neg())}`}`,
      });
      await client.query('UPDATE travel_settlement SET workflow_instance_id = $2 WHERE id = $1', [sid, inst.id]);
      await client.query(`UPDATE travel_request SET status = 'settling' WHERE id = $1`, [id]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'travel_settlement', rowId: sid, after: { entitlement: money(entitlement), advance: money(advance), net: money(net) } });
      await client.query('COMMIT');
      res.status(201).json({ id: sid, daAmount: money(da), lodgingAllowed: money(lodgingAllowed), otherBills: money(other), entitlement: money(entitlement), advance: money(advance), net: money(net) });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.post('/settlements/:id/review', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ decision: z.enum(['approve', 'reject']), reason: z.string().trim().max(1000).optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const s = await client.query<{ status: string; workflow_instance_id: string | null }>('SELECT status, workflow_instance_id FROM travel_settlement WHERE id = $1 AND company_id = $2 FOR UPDATE', [id, companyId]);
      const row = s.rows[0];
      if (!row) throw new NotFoundError('settlement not found');
      if (row.status !== 'pending' || !row.workflow_instance_id) throw new ConflictError(`settlement is ${row.status}`);
      const state = await act(client, { instanceId: row.workflow_instance_id, user: req.user!, decision: b.decision, reason: b.reason ?? null });
      await client.query('COMMIT');
      res.json({ state });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  r.get('/settlements/:id/bills/:billId/file', async (req, res) => {
    const id = uuid.parse(req.params.id); const billId = uuid.parse(req.params.billId);
    const companyId = companyIdOf(req);
    const s = await db.query<{ employee_id: string; file_id: string | null }>(
      'SELECT s.employee_id, b.file_id FROM travel_settlement s JOIN travel_bill b ON b.settlement_id = s.id WHERE s.id = $1 AND s.company_id = $2 AND b.id = $3', [id, companyId, billId]);
    const row = s.rows[0];
    if (!row) throw new NotFoundError('bill not found');
    await assertCanSee(db, req.user!, row.employee_id);
    if (!row.file_id) throw new NotFoundError('no file for this bill');
    if (row.employee_id !== req.user!.employeeId) await audit(db, { actorId: req.user!.id, action: 'view_receipt', table: 'travel_bill', rowId: billId });
    sendStoredFile(res, await readFile(db, cipher, companyId, row.file_id), { download: req.query.download === '1' });
  });

  // ---------------------------------------------------------------- reports (HR)
  r.get('/reports/spend', requireRole('hr'), async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, groupBy: z.enum(['category', 'department', 'month', 'employee']).default('category'), format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    if (q.to < q.from) throw new BadRequestError('the end date must be after the start date');
    const companyId = companyIdOf(req);
    const lines = await db.query<{ expense_date: string; category: string; department: string | null; emp_code: string; name_en: string; amount: string }>(
      `SELECT i.expense_date::text, ec.name AS category, ou.name_en AS department, e.emp_code, e.name_en, coalesce(i.approved_amount, i.amount)::text AS amount
         FROM expense_item i JOIN expense_claim c ON c.id = i.claim_id JOIN expense_category ec ON ec.id = i.category_id JOIN employee e ON e.id = c.employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> i.expense_date LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
        WHERE c.company_id = $1 AND c.status IN ('approved', 'paid') AND i.expense_date BETWEEN $2 AND $3
       UNION ALL
       SELECT s.actual_to::text, 'Travel', ou.name_en, e.emp_code, e.name_en, s.entitlement::text
         FROM travel_settlement s JOIN employee e ON e.id = s.employee_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> s.actual_to LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
        WHERE s.company_id = $1 AND s.status = 'approved' AND s.actual_to BETWEEN $2 AND $3`, [companyId, q.from, q.to]);
    const groups = new Map<string, { key: string; amount: Decimal; count: number; sort: string }>();
    for (const l of lines.rows) {
      const key = q.groupBy === 'category' ? l.category : q.groupBy === 'department' ? (l.department ?? 'No department')
        : q.groupBy === 'employee' ? `${l.emp_code} ${l.name_en}` : bsLabel(l.expense_date).replace(/^\d+ /, '');
      const g = groups.get(key) ?? { key, amount: D(0), count: 0, sort: l.expense_date };
      g.amount = g.amount.plus(l.amount); g.count += 1; if (l.expense_date < g.sort) g.sort = l.expense_date;
      groups.set(key, g);
    }
    const label = { category: 'Category', department: 'Department', month: 'Month', employee: 'Employee' }[q.groupBy];
    const sorted = [...groups.values()].sort((a, b) => (q.groupBy === 'month' ? a.sort.localeCompare(b.sort) : b.amount.comparedTo(a.amount)));
    const rows = sorted.map((g) => ({ group: g.key, items: g.count, amount: money(g.amount) }));
    sendReport(res, q.format, `expense-spend-by-${q.groupBy}`, [{ key: 'group', header: label }, { key: 'items', header: 'Items' }, { key: 'amount', header: 'Amount (NPR)' }], rows);
  });

  r.get('/reports/advances', requireRole('hr'), async (req, res) => {
    const q = z.object({ format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    const rows = await db.query<Record<string, unknown> & { fromDate: string; toDate: string }>(
      `SELECT t.id, e.emp_code AS "empCode", e.name_en AS "employeeName", t.destination, t.from_date::text AS "fromDate", t.to_date::text AS "toDate",
              t.advance_approved::text AS advance, t.advance_status AS "advanceStatus", t.status
         FROM travel_request t JOIN employee e ON e.id = t.employee_id
        WHERE t.company_id = $1 AND t.advance_status IN ('pending', 'paid') AND t.status IN ('approved', 'settling') ORDER BY t.to_date`, [companyIdOf(req)]);
    const out = rows.rows.map((x) => ({ ...x, trip: bsRange(x.fromDate, x.toDate) }));
    sendReport(res, q.format, 'outstanding-travel-advances', [
      { key: 'empCode', header: 'Code' }, { key: 'employeeName', header: 'Employee' }, { key: 'destination', header: 'Destination' }, { key: 'trip', header: 'Trip' },
      { key: 'advance', header: 'Advance (NPR)' }, { key: 'advanceStatus', header: 'Advance' }, { key: 'status', header: 'Trip status' },
    ], out);
  });

  r.get('/reports/pending-payment', requireRole('hr'), async (req, res) => {
    const q = z.object({ format: z.enum(['json', 'csv']).optional() }).parse(req.query);
    const rows = await db.query<Record<string, unknown> & { code: string; bsYear: number | null; bsMonth: number | null }>(
      `SELECT p.id, e.emp_code AS "empCode", e.name_en AS "employeeName", p.source_type AS "sourceType", p.source_id AS "sourceId", p.component_code AS code, p.amount::text, p.note,
              p.created_at AS "createdAt", pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
         FROM expense_payout p JOIN employee e ON e.id = p.employee_id LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
        WHERE p.company_id = $1 AND p.status = 'pending' ORDER BY e.emp_code, p.created_at`, [companyIdOf(req)]);
    const out = rows.rows.map((x) => ({ ...x, kind: x.code === 'EXP_RECOVERY' ? 'Recover' : 'Pay', inRun: x.bsYear ? `${x.bsYear}-${String(x.bsMonth).padStart(2, '0')}` : 'Next pay run' }));
    sendReport(res, q.format, 'expenses-pending-payment', [
      { key: 'empCode', header: 'Code' }, { key: 'employeeName', header: 'Employee' }, { key: 'note', header: 'For' }, { key: 'kind', header: 'Pay or recover' },
      { key: 'amount', header: 'Amount (NPR)' }, { key: 'inRun', header: 'Pay run' },
    ], out);
  });

  return r;
}
