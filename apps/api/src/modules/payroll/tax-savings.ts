import { Router } from 'express';
import { z } from 'zod';
import { computePayslip, D, type DeclarationInput, type TaxDetail } from '@hajiri/payroll-engine';
import { adToBs, bsMonthBounds, fiscalYearOf } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { money } from '../../validation.js';
import type { AuthUser } from '../auth/service.js';
import { companyIdOf, hasRole } from '../auth/middleware.js';
import { attachFile, fileMeta, readFile, sendStoredFile } from '../files/store.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { act, cancelWorkflow, nptToday, registerHandler, registerSummary, startWorkflow, type DecisionContext } from '../workflow/service.js';
import { resolveRules } from './rules.js';
import { activeEmployees, componentsFor, payrollPolicy, type PeriodRow, type RunRow } from './service.js';
import { effectiveCit, openingBalance } from './compliance-service.js';
import type { PayrollInput } from '@hajiri/payroll-engine';

/**
 * Tax savings self-service: an employee declares insurance premiums, retirement contributions and medical costs with
 * proofs; HR approves (workflow 'tax_declaration'); only then is tax_declaration updated (status 'verified'), and payroll
 * reads only verified declarations, so nothing unapproved ever changes pay.
 */
const amount = money.refine((v) => !v.startsWith('-'), 'must not be negative');

export const declarationBody = z.object({
  lifePremium: amount.default('0'), healthPremium: amount.default('0'), citMonthly: amount.default('0'),
  medicalCost: amount.default('0'), otherRetirementAnnual: amount.default('0'), donation: amount.default('0'),
});
export type DeclarationValues = z.infer<typeof declarationBody>;

interface RequestRow {
  id: string; company_id: string; employee_id: string; fiscal_year_start: number; life_premium: string; health_premium: string; cit_monthly: string;
  medical_cost: string; other_retirement_annual: string; donation: string; note: string | null; proof_file_ids: string[]; status: string; workflow_instance_id: string | null;
  created_at: Date; decided_at: Date | null; decision_note: string | null;
}

type DeclRow = { life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string; donation?: string; cit_from_month?: number | null; cit_prev_monthly?: string };
const toInput = (v: DeclRow | undefined, monthIndexInFy?: number): DeclarationInput => ({
  lifePremium: v?.life_premium ?? '0', healthPremium: v?.health_premium ?? '0', citMonthly: monthIndexInFy ? effectiveCit(v, monthIndexInFy) : v?.cit_monthly ?? '0',
  medicalCost: v?.medical_cost ?? '0', otherRetirementAnnual: v?.other_retirement_annual ?? '0', donation: v?.donation ?? '0',
});
const fromValues = (v: DeclarationValues): DeclarationInput => ({ ...v });

/** Current fiscal year (BS start year) in Nepal time. */
export function currentFiscalYearStart(): number { return fiscalYearOf(adToBs(nptToday())).startYear; }

// ---------------------------------------------------------------- decision handler

export async function applyTaxDeclarationDecision(db: Queryable, requestId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<RequestRow>('SELECT * FROM tax_declaration_request WHERE id = $1 FOR UPDATE', [requestId]);
  const req = r.rows[0];
  if (!req) throw new NotFoundError('tax declaration request not found');
  if (req.status !== 'pending') throw new ConflictError(`request already ${req.status}`);
  await db.query('UPDATE tax_declaration_request SET status = $2, decided_by = $3, decided_at = now(), decision_note = $4 WHERE id = $1', [requestId, state, ctx.actorId, ctx.reason]);
  if (state === 'approved') {
    await db.query(
      `INSERT INTO tax_declaration (employee_id, fiscal_year_start, life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, donation, status, verified_by, verified_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $9, 'verified', $8, now())
       ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET life_premium = EXCLUDED.life_premium, health_premium = EXCLUDED.health_premium, cit_monthly = EXCLUDED.cit_monthly,
         medical_cost = EXCLUDED.medical_cost, other_retirement_annual = EXCLUDED.other_retirement_annual, donation = EXCLUDED.donation, cit_from_month = NULL, cit_prev_monthly = 0,
         status = 'verified', verified_by = EXCLUDED.verified_by, verified_at = now(), updated_at = now()`,
      [req.employee_id, req.fiscal_year_start, req.life_premium, req.health_premium, req.cit_monthly, req.medical_cost, req.other_retirement_annual, ctx.actorId, req.donation]);
  }
  await audit(db, { actorId: ctx.actorId, action: state, table: 'tax_declaration_request', rowId: requestId, after: { fiscalYearStart: req.fiscal_year_start, reason: ctx.reason } });
  const userId = await userIdForEmployee(db, req.employee_id);
  await notify(db, {
    companyId: req.company_id, userId, employeeId: req.employee_id, template: `tax_declaration.${state}`,
    title: state === 'approved' ? 'Tax savings approved' : 'Tax savings not accepted',
    body: state === 'approved'
      ? `Your tax savings for FY ${req.fiscal_year_start}/${String(req.fiscal_year_start + 1).slice(-2)} were accepted; your monthly tax is recalculated from the next payroll.`
      : `HR did not accept your tax savings for FY ${req.fiscal_year_start}/${String(req.fiscal_year_start + 1).slice(-2)}${ctx.reason ? `: ${ctx.reason}` : ''}.`,
    payload: { requestId }, channels: ['push'],
  });
}
registerHandler('tax_declaration', applyTaxDeclarationDecision);

registerSummary('tax_declaration', async (db, inst) => {
  const r = await db.query<RequestRow>('SELECT * FROM tax_declaration_request WHERE id = $1', [inst.object_id]);
  const x = r.rows[0];
  if (!x) return {};
  const cur = await db.query<DeclRow>(
    `SELECT life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, donation FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'verified'`,
    [x.employee_id, x.fiscal_year_start]);
  return {
    fiscalYearStart: x.fiscal_year_start, lifePremium: x.life_premium, healthPremium: x.health_premium, citMonthly: x.cit_monthly, medicalCost: x.medical_cost,
    otherRetirementAnnual: x.other_retirement_annual, donation: x.donation, note: x.note, proofs: x.proof_file_ids.length, current: cur.rows[0] ? toInput(cur.rows[0]) : null,
  };
});

// ---------------------------------------------------------------- requests

export async function submitTaxSavings(db: Queryable, user: AuthUser, companyId: string, employeeId: string, fiscalYearStart: number, values: DeclarationValues,
  opts: { note?: string | null; proofFileIds?: string[] } = {}): Promise<RequestRow> {
  // One pending request per year: a new submission replaces the one HR has not decided yet.
  const prev = await db.query<{ id: string }>(`SELECT id FROM tax_declaration_request WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'pending' FOR UPDATE`, [employeeId, fiscalYearStart]);
  for (const p of prev.rows) {
    await db.query(`UPDATE tax_declaration_request SET status = 'cancelled', decided_at = now(), decision_note = 'replaced by a newer submission' WHERE id = $1`, [p.id]);
    await cancelWorkflow(db, 'tax_declaration', p.id);
  }
  const id = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
  const proofs = [...new Set(opts.proofFileIds ?? [])];
  for (const f of proofs) await attachFile(db, { companyId, fileId: f, userId: user.id, purpose: ['employee_document', 'tax_proof'], attachedTo: `tax_declaration_request:${id}` });
  const ins = await db.query<RequestRow>(
    `INSERT INTO tax_declaration_request (id, company_id, employee_id, fiscal_year_start, life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, donation, note, proof_file_ids, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $13, $10, $11, $12) RETURNING *`,
    [id, companyId, employeeId, fiscalYearStart, values.lifePremium, values.healthPremium, values.citMonthly, values.medicalCost, values.otherRetirementAnnual, opts.note ?? null, proofs, user.id, values.donation ?? '0']);
  const inst = await startWorkflow(db, {
    companyId, objectType: 'tax_declaration', objectId: id, employeeId,
    title: 'Tax savings to verify', body: `Tax savings declaration for FY ${fiscalYearStart}/${String(fiscalYearStart + 1).slice(-2)}${proofs.length ? ` with ${proofs.length} proof file(s)` : ''}.`,
  });
  await db.query('UPDATE tax_declaration_request SET workflow_instance_id = $2 WHERE id = $1', [id, inst.id]);
  await audit(db, { actorId: user.id, action: 'create', table: 'tax_declaration_request', rowId: id, after: { fiscalYearStart, ...values, proofs: proofs.length } });
  return { ...ins.rows[0]!, workflow_instance_id: inst.id };
}

export async function pendingTaxSavings(db: Queryable, employeeId: string, fiscalYearStart: number): Promise<RequestRow | null> {
  const r = await db.query<RequestRow>(`SELECT * FROM tax_declaration_request WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'pending'`, [employeeId, fiscalYearStart]);
  return r.rows[0] ?? null;
}

/**
 * HR decision from the declarations screen. Goes through the workflow engine when the request has an instance (the
 * normal case) so the approval is signed and audited the same way as from the inbox.
 */
export async function decideTaxSavings(db: Queryable, user: AuthUser, req: RequestRow, decision: 'approved' | 'rejected', reason: string | null): Promise<void> {
  if (req.workflow_instance_id) {
    await act(db, { instanceId: req.workflow_instance_id, user, decision: decision === 'approved' ? 'approve' : 'reject', reason: reason ?? (decision === 'rejected' ? 'Not accepted by HR' : null) });
    return;
  }
  if (user.employeeId === req.employee_id) throw new ForbiddenError('you cannot verify your own declaration');
  await applyTaxDeclarationDecision(db, req.id, decision, { actorId: user.id, reason, payload: {} });
}

// ---------------------------------------------------------------- tax estimate

export interface TaxEstimate {
  available: boolean; reason?: string; fiscalYearStart: number; bsYear?: number; bsMonth?: number;
  current?: TaxSummary; proposed?: TaxSummary; annualSaving?: string; monthlySaving?: string;
}
export interface TaxSummary {
  projectedAnnualTaxable: string; retirementDeduction: string; insuranceDeduction: string; donationDeduction: string; taxableAfterDeductions: string; medicalCredit: string;
  annualTax: string; tdsThisMonth: string; remainingMonths: number; declaration: DeclarationInput;
}

const summary = (t: TaxDetail, declaration: DeclarationInput): TaxSummary => ({
  projectedAnnualTaxable: t.projectedAnnualTaxable, retirementDeduction: t.retirementDeduction, insuranceDeduction: t.insuranceDeduction, donationDeduction: t.donationDeduction ?? '0.00',
  taxableAfterDeductions: t.taxableAfterDeductions, medicalCredit: t.medicalCredit, annualTax: t.annualTax, tdsThisMonth: t.tdsThisMonth,
  remainingMonths: t.remainingMonths, declaration,
});

/**
 * The payroll engine input for this month's payslip of one employee, as a real run would build it: same salary
 * structure, rules, year-to-date locked payslips (plus opening balances) and policy, full attendance, no one-offs,
 * with the verified declaration. Used by the tax estimate and the optimum CIT suggestion.
 */
export async function estimateInput(db: Queryable, companyId: string, employeeId: string, fiscalYearStart: number): Promise<{ input: PayrollInput; bsYear: number; bsMonth: number } | { reason: string }> {
  const today = nptToday();
  const bs = adToBs(today);
  const fy = fiscalYearOf(bs);
  if (fy.startYear !== fiscalYearStart) return { reason: 'Estimates are available for the current fiscal year only.' };
  const b = bsMonthBounds(bs.year, bs.month);
  const p: PeriodRow = { id: 'estimate', company_id: companyId, bs_year: bs.year, bs_month: bs.month, start_ad: b.startAd, end_ad: b.endAd, days: b.days, attendance_locked_at: null };
  const [emp] = await activeEmployees(db, companyId, p, { params: { employeeIds: [employeeId] } } as unknown as RunRow);
  if (!emp?.assignment) return { reason: 'The salary is not set up in Hajiri yet, so tax cannot be estimated.' };
  const components = await componentsFor(db, companyId, emp.assignment.structure_version_id);
  const policy = await payrollPolicy(db, companyId);
  const { rules } = await resolveRules(db, p.end_ad);
  const ytd = await db.query<{ taxable: string; tds: string; ee: string; er: string; festival: boolean }>(
    `SELECT coalesce(sum(ps.taxable), 0)::text AS taxable, coalesce(sum(ps.tds), 0)::text AS tds,
            coalesce((SELECT sum(l.amount) FROM payslip_line l WHERE l.payslip_id = ANY(array_agg(ps.id)) AND l.component_code IN ('SSF_EE', 'PF_EE', 'CIT')), 0)::text AS ee,
            coalesce(sum(ps.employer_statutory), 0)::text AS er,
            coalesce(bool_or(EXISTS (SELECT 1 FROM payslip_line l WHERE l.payslip_id = ps.id AND l.component_code = 'FESTIVAL')), false) AS festival
       FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period pp ON pp.id = r.period_id
      WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND pp.start_ad >= $2 AND pp.start_ad < $3`, [employeeId, fy.startAd, p.start_ad]);
  const y = ytd.rows[0]!;
  const ob = await openingBalance(db, employeeId, fy.startYear);
  const verified = await db.query<DeclRow>(
    `SELECT life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, donation, cit_from_month, cit_prev_monthly
       FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'verified'`, [employeeId, fiscalYearStart]);
  const monthIndexInFy = ((p.bs_month - 4 + 12) % 12) + 1;
  const serviceFrom = emp.join_date && emp.join_date > fy.startAd ? emp.join_date : fy.startAd;
  const months = serviceFrom === fy.startAd ? 12 : Math.min(12, Math.max(1, Math.round((new Date(fy.endAd).getTime() - new Date(serviceFrom).getTime()) / (30.44 * 86_400_000))));
  const plus = (a: string, b2: string | undefined) => D(a).plus(b2 ?? 0).toFixed(2);
  const input: PayrollInput = {
    employee: { id: emp.id, code: emp.emp_code, nameEn: emp.name_en, nameNe: emp.name_ne, gender: emp.gender, maritalStatus: emp.marital_status, isResident: emp.is_resident, regime: emp.regime,
      remoteAreaClass: emp.remote_area_class, disability: emp.disability, otEligible: emp.ot_eligible, festivalMonth: emp.festival_month },
    period: { bsYear: p.bs_year, bsMonth: p.bs_month, daysInMonth: p.days, fiscalYearStart: fy.startYear, monthIndexInFy },
    components, baseAmounts: emp.assignment.base_amounts, inputs: [],
    attendance: { paidDays: String(p.days), lopDays: '0', otHours: '0' },
    ytd: { taxable: plus(y.taxable, ob?.taxable), tdsPaid: plus(y.tds, ob?.tds_paid), retirementEmployee: plus(y.ee, ob?.retirement_employee),
      retirementEmployer: plus(y.er, ob?.retirement_employer), festivalPaid: Boolean(y.festival) || Boolean(ob?.festival_paid) },
    declaration: toInput(verified.rows[0], monthIndexInFy), loans: [], rules, policy, extras: { monthsOfServiceInFy: months },
  };
  return { input, bsYear: bs.year, bsMonth: bs.month };
}

/** What the payroll engine would project for this month's payslip with the verified declaration and with a proposed one (nothing is saved). */
export async function estimateTax(db: Queryable, companyId: string, employeeId: string, fiscalYearStart: number, proposed: DeclarationInput | null): Promise<TaxEstimate> {
  const built = await estimateInput(db, companyId, employeeId, fiscalYearStart);
  if ('reason' in built) return { available: false, reason: built.reason, fiscalYearStart };
  const run = (declaration: DeclarationInput) => computePayslip({ ...built.input, declaration });
  const currentDecl = built.input.declaration;
  const cur = summary(run(currentDecl).tax, currentDecl);
  const out: TaxEstimate = { available: true, fiscalYearStart, bsYear: built.bsYear, bsMonth: built.bsMonth, current: cur };
  if (proposed) {
    const prop = summary(run(proposed).tax, proposed);
    out.proposed = prop;
    out.annualSaving = D(cur.annualTax).minus(prop.annualTax).toFixed(2);
    out.monthlySaving = D(cur.tdsThisMonth).minus(prop.tdsThisMonth).toFixed(2);
  }
  return out;
}

// ---------------------------------------------------------------- routes (mounted at /payroll/tax-savings)

export function taxSavingsRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const fyParam = z.coerce.number().int().min(2070).max(2200);

  /** My (or, for HR, an employee's) tax savings for a year: the verified declaration, a pending request and history. */
  r.get('/', async (req, res) => {
    const q = z.object({ fiscalYearStart: fyParam.optional(), employeeId: z.uuid().optional() }).parse(req.query);
    const companyId = companyIdOf(req);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('this account is not linked to an employee');
    if (employeeId !== req.user!.employeeId && !hasRole(req.user!, 'hr')) throw new ForbiddenError();
    const emp = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    const fy = q.fiscalYearStart ?? currentFiscalYearStart();
    const verified = await db.query('SELECT * FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2', [employeeId, fy]);
    const history = await db.query<RequestRow>('SELECT * FROM tax_declaration_request WHERE employee_id = $1 AND fiscal_year_start = $2 ORDER BY created_at DESC LIMIT 20', [employeeId, fy]);
    const files = new Map<string, { id: string; filename: string; mime: string; bytes: number }>();
    for (const h of history.rows) for (const f of h.proof_file_ids) {
      const m = await fileMeta(db, companyId, f);
      if (m) files.set(f, { id: m.id, filename: m.filename, mime: m.mime, bytes: m.bytes });
    }
    res.json({
      fiscalYearStart: fy,
      declaration: verified.rows[0] ?? null,
      pending: history.rows.find((h) => h.status === 'pending') ?? null,
      history: history.rows.map((h) => ({ ...h, proofs: h.proof_file_ids.map((f) => files.get(f)).filter(Boolean) })),
    });
  });

  /** Submit (or replace the pending) tax savings declaration for HR to verify. */
  r.post('/', async (req, res) => {
    const b = declarationBody.extend({
      fiscalYearStart: fyParam, note: z.string().trim().max(1000).optional(), proofFileIds: z.array(z.uuid()).max(10).default([]),
    }).parse(req.body);
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('this account is not linked to an employee');
    const companyId = companyIdOf(req);
    const cur = currentFiscalYearStart();
    if (b.fiscalYearStart < cur - 1 || b.fiscalYearStart > cur + 1) throw new BadRequestError('declarations can be made for the current, previous or next fiscal year only');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`tax_savings:${employeeId}`]);
      const { fiscalYearStart, note, proofFileIds, ...values } = b;
      const row = await submitTaxSavings(client, req.user!, companyId, employeeId, fiscalYearStart, values, { note: note ?? null, proofFileIds });
      await client.query('COMMIT');
      res.status(201).json({ request: row });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  /** Withdraw my pending request. */
  r.post('/:id/cancel', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const row = await db.query<RequestRow>('SELECT * FROM tax_declaration_request WHERE id = $1 AND company_id = $2', [id, companyIdOf(req)]);
    const x = row.rows[0];
    if (!x || x.employee_id !== req.user!.employeeId) throw new NotFoundError('request not found');
    if (x.status !== 'pending') throw new ConflictError(`request already ${x.status}`);
    await db.query(`UPDATE tax_declaration_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
    await cancelWorkflow(db, 'tax_declaration', id);
    await audit(db, { actorId: req.user!.id, action: 'cancel', table: 'tax_declaration_request', rowId: id });
    res.json({ ok: true });
  });

  /** Projected tax with the verified declaration and, optionally, with proposed amounts (nothing is saved). */
  r.post('/estimate', async (req, res) => {
    const b = z.object({ fiscalYearStart: fyParam.optional(), employeeId: z.uuid().optional(), proposed: declarationBody.optional() }).parse(req.body ?? {});
    const companyId = companyIdOf(req);
    const employeeId = b.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('this account is not linked to an employee');
    if (employeeId !== req.user!.employeeId && !hasRole(req.user!, 'hr')) throw new ForbiddenError();
    const emp = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    res.json(await estimateTax(db, companyId, employeeId, b.fiscalYearStart ?? currentFiscalYearStart(), b.proposed ? fromValues(b.proposed) : null));
  });

  /** A proof file: the employee who submitted it and HR. HR views are audited. */
  r.get('/:id/files/:fileId', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const fileId = z.uuid().parse(req.params.fileId);
    const companyId = companyIdOf(req);
    const row = await db.query<RequestRow>('SELECT * FROM tax_declaration_request WHERE id = $1 AND company_id = $2', [id, companyId]);
    const x = row.rows[0];
    const own = x && x.employee_id === req.user!.employeeId;
    if (!x || (!own && !hasRole(req.user!, 'hr')) || !x.proof_file_ids.includes(fileId)) throw new NotFoundError('file not found');
    const file = await readFile(db, cipher, companyId, fileId);
    if (!own) await audit(db, { actorId: req.user!.id, action: 'view_file', table: 'tax_declaration_request', rowId: id, after: { fileId } });
    sendStoredFile(res, file, { download: req.query.download === '1' });
  });

  return r;
}
