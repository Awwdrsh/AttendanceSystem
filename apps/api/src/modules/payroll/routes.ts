import { createHash } from 'node:crypto';
import { Router } from 'express';
import { z } from 'zod';
import { validateFormula, D } from '@hajiri/payroll-engine';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, money } from '../../validation.js';
import { hasRole, requireRole } from '../auth/middleware.js';
import { lockAttendance } from '../attendance/service.js';
import { buildBankFile, type BankRow } from './bank-files.js';
import { etdsCsv, etdsRows, journal, journalCsv, ssfSheetCsv } from './exports.js';
import { renderPayslipHtml } from './payslip-html.js';
import { advanceRun, approveRun, bonusAllocation, computeRun, ensurePeriod, getPeriod, getRun, lockRun, preflight, publishRun, reviewRun } from './service.js';

const STATUTORY_KEYS = new Set(['tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance', 'min_wage', 'bonus.caps', 'ssf.deposit_days', 'tds.deposit_days']);
const payrollRole = requireRole('hr', 'finance');

export function payrollRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const cid = (req: { user?: { companyId: string | null } }) => req.user!.companyId!;

  // ---------------------------------------------------------------- rule versions (plan §6.13)
  r.get('/rules', payrollRole, async (req, res) => {
    const q = z.object({ key: z.string().optional(), asOf: isoDate.optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT id, rule_key, jurisdiction, valid_from_ad, valid_to_ad, valid_from_bs, valid_to_bs, value, source_url, note, approved_by, approved_at, supersedes_id, created_at
         FROM rule_version WHERE ($1::text IS NULL OR rule_key = $1) AND ($2::date IS NULL OR (valid_from_ad <= $2 AND (valid_to_ad IS NULL OR valid_to_ad >= $2)))
        ORDER BY rule_key, valid_from_ad DESC`, [q.key ?? null, q.asOf ?? null]);
    res.json({ rules: rows.rows.map((x) => ({ ...x, locked: STATUTORY_KEYS.has(x.rule_key) })) });
  });
  r.post('/rules', payrollRole, async (req, res) => {
    const b = z.object({ ruleKey: z.string().min(1).max(60), validFromAd: isoDate, validToAd: isoDate.nullable().optional(), validFromBs: z.string(), validToBs: z.string().nullable().optional(),
      value: z.unknown(), sourceUrl: z.string().url().optional(), note: z.string().max(500).optional(), supersedesId: z.uuid().optional() }).parse(req.body);
    const rows = await db.query<{ id: string }>(
      `INSERT INTO rule_version (rule_key, valid_from_ad, valid_to_ad, valid_from_bs, valid_to_bs, value, source_url, note, supersedes_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
      [b.ruleKey, b.validFromAd, b.validToAd ?? null, b.validFromBs, b.validToBs ?? null, JSON.stringify(b.value), b.sourceUrl ?? null, b.note ?? null, b.supersedesId ?? null]);
    if (b.supersedesId) await db.query(`UPDATE rule_version SET valid_to_ad = coalesce(valid_to_ad, $2::date - 1) WHERE id = $1`, [b.supersedesId, b.validFromAd]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'rule_version', rowId: rows.rows[0]!.id, after: b });
    res.status(201).json({ id: rows.rows[0]!.id });
  });
  r.post('/rules/:id/approve', requireRole('finance'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const rows = await db.query('UPDATE rule_version SET approved_by = $2, approved_at = now() WHERE id = $1 RETURNING id', [id, req.user!.id]);
    if (!rows.rows[0]) throw new NotFoundError('rule not found');
    await audit(db, { actorId: req.user!.id, action: 'approve', table: 'rule_version', rowId: id });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- components, structures, assignments (plan §9.2)
  r.get('/components', payrollRole, async (req, res) => {
    const rows = await db.query('SELECT * FROM salary_component WHERE company_id = $1 ORDER BY sort_order, code', [cid(req)]);
    res.json({ components: rows.rows });
  });
  const componentBody = z.object({
    code: z.string().regex(/^[A-Z][A-Z0-9_]{1,30}$/), nameEn: z.string().min(1), nameNe: z.string().optional(), type: z.enum(['earning', 'deduction', 'employer_contribution', 'statistical']),
    formula: z.string().max(500).nullable().optional(), condition: z.string().max(500).nullable().optional(), dependsOnPaymentDays: z.boolean().default(false), taxable: z.boolean().default(true),
    rounding: z.enum(['paisa', 'rupee']).default('paisa'), sortOrder: z.number().int().default(100), active: z.boolean().default(true),
  });
  async function checkFormulas(companyId: string, b: { code: string; formula?: string | null | undefined; condition?: string | null | undefined }) {
    const known = (await db.query<{ code: string }>('SELECT code FROM salary_component WHERE company_id = $1', [companyId])).rows.map((x) => x.code);
    known.push(b.code);
    for (const [label, src] of [['formula', b.formula], ['condition', b.condition]] as const) {
      if (src) { const err = validateFormula(src, known); if (err) throw new BadRequestError(`${label}: ${err}`); }
    }
  }
  r.post('/components', payrollRole, async (req, res) => {
    const b = componentBody.parse(req.body);
    await checkFormulas(cid(req), b);
    await db.query(
      `INSERT INTO salary_component (company_id, code, name_en, name_ne, type, formula, condition, depends_on_payment_days, taxable, rounding, sort_order, active)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
      [cid(req), b.code, b.nameEn, b.nameNe ?? null, b.type, b.formula ?? null, b.condition ?? null, b.dependsOnPaymentDays, b.taxable, b.rounding, b.sortOrder, b.active]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'salary_component', rowId: b.code, after: b });
    res.status(201).json({ code: b.code });
  });
  r.patch('/components/:code', payrollRole, async (req, res) => {
    const code = z.string().parse(req.params.code);
    const b = componentBody.partial().parse(req.body);
    const cur = await db.query('SELECT * FROM salary_component WHERE company_id = $1 AND code = $2', [cid(req), code]);
    if (!cur.rows[0]) throw new NotFoundError('component not found');
    if (cur.rows[0].statutory && (b.formula !== undefined || b.type !== undefined)) throw new ForbiddenError('statutory components are locked (plan §13.8)');
    await checkFormulas(cid(req), { code, formula: b.formula, condition: b.condition });
    await db.query(
      `UPDATE salary_component SET name_en = coalesce($3, name_en), name_ne = coalesce($4, name_ne), formula = CASE WHEN $5 THEN $6 ELSE formula END, condition = CASE WHEN $7 THEN $8 ELSE condition END,
         depends_on_payment_days = coalesce($9, depends_on_payment_days), taxable = coalesce($10, taxable), rounding = coalesce($11, rounding), sort_order = coalesce($12, sort_order), active = coalesce($13, active)
       WHERE company_id = $1 AND code = $2`,
      [cid(req), code, b.nameEn ?? null, b.nameNe ?? null, b.formula !== undefined, b.formula ?? null, b.condition !== undefined, b.condition ?? null, b.dependsOnPaymentDays ?? null, b.taxable ?? null, b.rounding ?? null, b.sortOrder ?? null, b.active ?? null]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'salary_component', rowId: code, before: cur.rows[0], after: b });
    res.json({ ok: true });
  });

  r.get('/structures', payrollRole, async (req, res) => {
    const rows = await db.query(
      `SELECT s.id, s.code, s.name_en, (SELECT json_agg(json_build_object('id', v.id, 'version', v.version, 'components', v.components, 'frozenAt', v.frozen_at) ORDER BY v.version) FROM salary_structure_version v WHERE v.structure_id = s.id) AS versions
         FROM salary_structure s WHERE s.company_id = $1 ORDER BY s.code`, [cid(req)]);
    res.json({ structures: rows.rows });
  });
  r.post('/structures', payrollRole, async (req, res) => {
    const b = z.object({ code: z.string().min(1).max(20), nameEn: z.string().min(1), components: z.array(z.object({ code: z.string() })).min(1) }).parse(req.body);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const s = await client.query<{ id: string }>('INSERT INTO salary_structure (company_id, code, name_en) VALUES ($1, $2, $3) RETURNING id', [cid(req), b.code, b.nameEn]);
      const v = await client.query<{ id: string }>('INSERT INTO salary_structure_version (structure_id, version, components) VALUES ($1, 1, $2) RETURNING id', [s.rows[0]!.id, JSON.stringify(b.components)]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'salary_structure', rowId: s.rows[0]!.id, after: b });
      await client.query('COMMIT');
      res.status(201).json({ id: s.rows[0]!.id, versionId: v.rows[0]!.id });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });
  r.post('/structures/:id/versions', payrollRole, async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ components: z.array(z.object({ code: z.string() })).min(1) }).parse(req.body);
    const own = await db.query('SELECT 1 FROM salary_structure WHERE id = $1 AND company_id = $2', [id, cid(req)]);
    if (!own.rows[0]) throw new NotFoundError('structure not found');
    const v = await db.query<{ id: string; version: number }>(
      `INSERT INTO salary_structure_version (structure_id, version, components) VALUES ($1, (SELECT coalesce(max(version), 0) + 1 FROM salary_structure_version WHERE structure_id = $1), $2) RETURNING id, version`, [id, JSON.stringify(b.components)]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'salary_structure_version', rowId: v.rows[0]!.id, after: b });
    res.status(201).json(v.rows[0]);
  });

  r.get('/assignments', payrollRole, async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT a.employee_id, e.emp_code, e.name_en, a.structure_version_id, s.code AS structure_code, v.version, a.valid::text, a.base_amounts, a.created_at
         FROM salary_assignment a JOIN employee e ON e.id = a.employee_id JOIN salary_structure_version v ON v.id = a.structure_version_id JOIN salary_structure s ON s.id = v.structure_id
        WHERE e.company_id = $1 AND ($2::uuid IS NULL OR a.employee_id = $2) ORDER BY e.emp_code, lower(a.valid) DESC`, [cid(req), q.employeeId ?? null]);
    res.json({ assignments: rows.rows });
  });
  r.post('/assignments', payrollRole, async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), structureVersionId: z.uuid(), from: isoDate, baseAmounts: z.record(z.string(), money) }).parse(req.body);
    const emp = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, cid(req)]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      // Close the current open assignment the day before and open the new one (effective-dated, plan §9.1).
      await client.query(`UPDATE salary_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND upper_inf(valid) AND lower(valid) < $2::date`, [b.employeeId, b.from]);
      await client.query(`DELETE FROM salary_assignment WHERE employee_id = $1 AND lower(valid) >= $2::date`, [b.employeeId, b.from]);
      await client.query(`INSERT INTO salary_assignment (employee_id, structure_version_id, valid, base_amounts, created_by) VALUES ($1, $2, daterange($3, NULL), $4, $5)`,
        [b.employeeId, b.structureVersionId, b.from, JSON.stringify(b.baseAmounts), req.user!.id]);
      await client.query('UPDATE salary_structure_version SET frozen_at = coalesce(frozen_at, now()) WHERE id = $1', [b.structureVersionId]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'salary_assignment', rowId: b.employeeId, after: { ...b, baseAmounts: '***' } });
      await client.query('COMMIT');
      res.status(201).json({ ok: true });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  });

  // ---------------------------------------------------------------- declarations and loans
  r.get('/declarations', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), fiscalYearStart: z.coerce.number().int() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    if (employeeId !== req.user!.employeeId && !hasRole(req.user!, 'hr', 'finance')) throw new ForbiddenError();
    const rows = await db.query('SELECT * FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2', [employeeId, q.fiscalYearStart]);
    res.json({ declaration: rows.rows[0] ?? null });
  });
  r.put('/declarations', async (req, res) => {
    const b = z.object({ employeeId: z.uuid().optional(), fiscalYearStart: z.number().int(), lifePremium: money.default('0'), healthPremium: money.default('0'), citMonthly: money.default('0'),
      medicalCost: money.default('0'), otherRetirementAnnual: money.default('0'), submit: z.boolean().default(true) }).parse(req.body);
    const employeeId = b.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    if (employeeId !== req.user!.employeeId && !hasRole(req.user!, 'hr')) throw new ForbiddenError();
    await db.query(
      `INSERT INTO tax_declaration (employee_id, fiscal_year_start, life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, status)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
       ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET life_premium = EXCLUDED.life_premium, health_premium = EXCLUDED.health_premium, cit_monthly = EXCLUDED.cit_monthly,
         medical_cost = EXCLUDED.medical_cost, other_retirement_annual = EXCLUDED.other_retirement_annual, status = EXCLUDED.status, verified_by = NULL, verified_at = NULL, updated_at = now()`,
      [employeeId, b.fiscalYearStart, b.lifePremium, b.healthPremium, b.citMonthly, b.medicalCost, b.otherRetirementAnnual, b.submit ? 'submitted' : 'draft']);
    await audit(db, { actorId: req.user!.id, action: 'upsert', table: 'tax_declaration', rowId: employeeId, after: b });
    res.json({ ok: true });
  });
  r.post('/declarations/:employeeId/verify', payrollRole, async (req, res) => {
    const employeeId = z.uuid().parse(req.params.employeeId);
    const b = z.object({ fiscalYearStart: z.number().int(), decision: z.enum(['verified', 'rejected']) }).parse(req.body);
    const rows = await db.query(`UPDATE tax_declaration SET status = $3, verified_by = $4, verified_at = now() WHERE employee_id = $1 AND fiscal_year_start = $2 RETURNING id`, [employeeId, b.fiscalYearStart, b.decision, req.user!.id]);
    if (!rows.rows[0]) throw new NotFoundError('declaration not found');
    await audit(db, { actorId: req.user!.id, action: b.decision, table: 'tax_declaration', rowId: employeeId });
    res.json({ ok: true });
  });

  r.get('/loans', payrollRole, async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT l.*, e.emp_code, coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)::text AS repaid FROM loan l JOIN employee e ON e.id = l.employee_id
        WHERE e.company_id = $1 AND ($2::uuid IS NULL OR l.employee_id = $2) ORDER BY l.created_at DESC`, [cid(req), q.employeeId ?? null]);
    res.json({ loans: rows.rows });
  });
  r.post('/loans', payrollRole, async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), kind: z.enum(['loan', 'advance']).default('loan'), principal: money, emi: money, startBsYear: z.number().int(), startBsMonth: z.number().int().min(1).max(12), note: z.string().optional() }).parse(req.body);
    if (!D(b.emi).gt(0) || !D(b.principal).gt(0)) throw new BadRequestError('principal and emi must be positive');
    const installments = D(b.principal).div(b.emi).ceil().toNumber();
    const rows = await db.query<{ id: string }>(
      `INSERT INTO loan (employee_id, kind, principal, emi, start_bs_year, start_bs_month, installments, note, created_by) SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9 FROM employee WHERE id = $1 AND company_id = $10 RETURNING id`,
      [b.employeeId, b.kind, b.principal, b.emi, b.startBsYear, b.startBsMonth, installments, b.note ?? null, req.user!.id, cid(req)]);
    if (!rows.rows[0]) throw new NotFoundError('employee not found');
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'loan', rowId: rows.rows[0].id, after: b });
    res.status(201).json({ id: rows.rows[0].id, installments });
  });

  // ---------------------------------------------------------------- periods and runs (plan §13.6 wizard)
  r.get('/periods', payrollRole, async (req, res) => {
    const rows = await db.query(`SELECT * FROM payroll_period WHERE company_id = $1 ORDER BY bs_year DESC, bs_month DESC`, [cid(req)]);
    res.json({ periods: rows.rows });
  });
  r.post('/periods', payrollRole, async (req, res) => {
    const b = z.object({ bsYear: z.number().int(), bsMonth: z.number().int().min(1).max(12) }).parse(req.body);
    res.status(201).json({ period: await ensurePeriod(db, cid(req), b.bsYear, b.bsMonth) });
  });

  r.get('/runs', payrollRole, async (req, res) => {
    const rows = await db.query(
      `SELECT r.id, r.period_id, p.bs_year, p.bs_month, r.run_type, r.status, r.totals, r.maker_id, r.checker_id, r.computed_at, r.approved_at, r.locked_at, r.disbursed_at, r.filed_at, r.created_at
         FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.company_id = $1 ORDER BY r.created_at DESC`, [cid(req)]);
    res.json({ runs: rows.rows });
  });
  r.post('/runs', payrollRole, async (req, res) => {
    const b = z.object({ periodId: z.uuid(), runType: z.enum(['regular', 'off_cycle', 'arrears', 'supplementary', 'final_settlement']).default('regular'),
      referencesRunId: z.uuid().optional(), employeeIds: z.array(z.uuid()).optional(), noticePay: money.optional(), notes: z.string().max(1000).optional() }).parse(req.body);
    const p = await getPeriod(db, cid(req), b.periodId);
    if (b.runType === 'regular') {
      const dup = await db.query(`SELECT 1 FROM payroll_run WHERE period_id = $1 AND run_type = 'regular' AND status NOT IN ('preflight_failed') AND locked_at IS NOT NULL`, [p.id]);
      if (dup.rows[0]) throw new ConflictError('a locked regular run already exists for this period; use a supplementary or arrears run');
    }
    if (b.runType === 'arrears' && !b.referencesRunId) throw new BadRequestError('arrears runs need referencesRunId');
    if (b.runType === 'final_settlement' && !(b.employeeIds?.length === 1)) throw new BadRequestError('final settlement runs need exactly one employeeId');
    if (b.referencesRunId) { const ref = await getRun(db, cid(req), b.referencesRunId); if (!ref.locked_at) throw new BadRequestError('referenced run must be locked'); }
    const params = { ...(b.employeeIds ? { employeeIds: b.employeeIds } : {}), ...(b.noticePay ? { noticePay: b.noticePay } : {}) };
    const rows = await db.query<{ id: string }>(
      `INSERT INTO payroll_run (company_id, period_id, run_type, references_run_id, params, notes, maker_id) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
      [cid(req), p.id, b.runType, b.referencesRunId ?? null, JSON.stringify(params), b.notes ?? null, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'payroll_run', rowId: rows.rows[0]!.id, after: b });
    res.status(201).json({ id: rows.rows[0]!.id });
  });
  r.post('/runs/bonus', payrollRole, async (req, res) => {
    const b = z.object({ periodId: z.uuid(), fiscalYearStart: z.number().int(), netProfit: money }).parse(req.body);
    const p = await getPeriod(db, cid(req), b.periodId);
    const alloc = await bonusAllocation(db, cid(req), b.fiscalYearStart, b.netProfit);
    const rows = await db.query<{ id: string }>(
      `INSERT INTO payroll_run (company_id, period_id, run_type, params, maker_id) VALUES ($1, $2, 'bonus', $3, $4) RETURNING id`,
      [cid(req), p.id, JSON.stringify({ fiscalYearStart: b.fiscalYearStart, netProfit: b.netProfit, pool: alloc.pool, bonusByEmployee: alloc.byEmployee, detail: alloc.detail, employeeIds: Object.keys(alloc.byEmployee) }), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'payroll_run', rowId: rows.rows[0]!.id, after: { runType: 'bonus', pool: alloc.pool } });
    res.status(201).json({ id: rows.rows[0]!.id, pool: alloc.pool, allocation: alloc.detail });
  });
  r.get('/runs/:id', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const p = await getPeriod(db, cid(req), run.period_id);
    const slips = await db.query(
      `SELECT ps.id, ps.employee_id, e.emp_code, e.name_en, ps.paid_days, ps.lop_days, ps.ot_hours, ps.gross, ps.tds, ps.employee_statutory, ps.employer_statutory, ps.total_deductions, ps.net, ps.flags, ps.published_at
         FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 ORDER BY (jsonb_array_length(ps.flags) > 0) DESC, e.emp_code`, [run.id]);
    const files = await db.query('SELECT id, bank_code, format, filename, checksum, created_at FROM bank_file WHERE run_id = $1 ORDER BY created_at DESC', [run.id]);
    res.json({ run, period: p, payslips: slips.rows, bankFiles: files.rows });
  });
  r.post('/runs/:id/preflight', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const pf = await preflight(db, cid(req), run);
    if (['draft', 'preflight_failed'].includes(run.status)) await db.query(`UPDATE payroll_run SET status = $2, preflight = $3 WHERE id = $1`, [run.id, pf.ok ? 'draft' : 'preflight_failed', JSON.stringify(pf)]);
    res.json(pf);
  });
  r.post('/runs/:id/lock-attendance', requireRole('hr'), async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const p = await getPeriod(db, cid(req), run.period_id);
    const n = await lockAttendance(db, cid(req), p.start_ad, p.end_ad);
    await db.query('UPDATE payroll_period SET attendance_locked_at = coalesce(attendance_locked_at, now()) WHERE id = $1', [p.id]);
    await audit(db, { actorId: req.user!.id, action: 'lock_attendance', table: 'payroll_period', rowId: p.id, after: { locked: n } });
    res.json({ locked: n });
  });
  r.post('/runs/:id/inputs', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    if (run.locked_at) throw new ConflictError('run is locked');
    const b = z.object({ inputs: z.array(z.object({ employeeId: z.uuid(), componentCode: z.string(), amount: money, note: z.string().max(200).optional() })).min(1).max(5000) }).parse(req.body);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      for (const i of b.inputs) {
        await client.query(`INSERT INTO payroll_input (run_id, employee_id, component_code, amount, note, created_by) SELECT $1, $2, $3, $4, $5, $6 FROM employee WHERE id = $2 AND company_id = $7`,
          [run.id, i.employeeId, i.componentCode, i.amount, i.note ?? null, req.user!.id, cid(req)]);
      }
      await audit(client, { actorId: req.user!.id, action: 'inputs', table: 'payroll_run', rowId: run.id, after: { count: b.inputs.length } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.status(201).json({ added: b.inputs.length });
  });
  r.get('/runs/:id/inputs', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const rows = await db.query('SELECT i.id, i.employee_id, e.emp_code, i.component_code, i.amount, i.note FROM payroll_input i JOIN employee e ON e.id = i.employee_id WHERE i.run_id = $1 ORDER BY e.emp_code', [run.id]);
    res.json({ inputs: rows.rows });
  });
  r.post('/runs/:id/compute', payrollRole, async (req, res) => { res.json(await computeRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id)); });
  r.post('/runs/:id/review', payrollRole, async (req, res) => {
    const b = z.object({ notes: z.string().max(2000).optional() }).parse(req.body ?? {});
    await reviewRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id, b.notes);
    res.json({ ok: true });
  });
  r.post('/runs/:id/approve', requireRole('finance'), async (req, res) => {
    const signature = await approveRun(db, cid(req), z.uuid().parse(req.params.id), { id: req.user!.id, roles: req.user!.roles });
    res.json({ ok: true, signature });
  });
  r.post('/runs/:id/lock', payrollRole, async (req, res) => { res.json(await lockRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id)); });
  r.post('/runs/:id/disburse', requireRole('finance'), async (req, res) => { await advanceRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id, 'disbursed'); res.json({ ok: true }); });
  r.post('/runs/:id/file', requireRole('finance'), async (req, res) => { await advanceRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id, 'filed'); res.json({ ok: true }); });
  r.post('/runs/:id/publish', payrollRole, async (req, res) => { res.json({ published: await publishRun(db, cid(req), z.uuid().parse(req.params.id), req.user!.id) }); });

  // ---------------------------------------------------------------- outputs (plan §11.10)
  r.post('/runs/:id/bank-file', requireRole('finance'), async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    if (!run.locked_at) throw new ConflictError('bank files are generated from locked runs only');
    const b = z.object({ bankCode: z.enum(['NMB', 'NABIL', 'NIC_ASIA', 'EVEREST', 'PRABHU', 'GENERIC']) }).parse(req.body);
    const p = await getPeriod(db, cid(req), run.period_id);
    const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; bank_code: string | null; bank_branch: string | null; bank_account_enc: Buffer | null; phone: string | null; email: string | null; net: string }>(
      `SELECT e.id AS employee_id, e.emp_code, e.name_en, e.bank_code, e.bank_branch, e.bank_account_enc, e.phone, e.email, ps.net::text FROM payslip ps JOIN employee e ON e.id = ps.employee_id
        WHERE ps.run_id = $1 AND ps.net > 0 AND ($2 = 'GENERIC' OR e.bank_code = $2) ORDER BY e.emp_code`, [run.id, b.bankCode]);
    const bankRows: BankRow[] = rows.rows.map((x) => ({
      empCode: x.emp_code, name: x.name_en, bankCode: x.bank_code ?? '', branch: x.bank_branch ?? '', amount: x.net,
      account: x.bank_account_enc ? cipher.decrypt(x.bank_account_enc, `employee:${x.employee_id}:bank`) : '', ...(x.phone ? { mobile: x.phone } : {}), ...(x.email ? { email: x.email } : {}),
    }));
    const period = `${p.bs_year}-${String(p.bs_month).padStart(2, '0')}`;
    const file = buildBankFile(b.bankCode, bankRows, period);
    if (file.errors.length && b.bankCode !== 'GENERIC') return res.status(400).json({ error: 'bank file validation failed', details: file.errors });
    const checksum = createHash('sha256').update(file.content, 'utf8').digest('hex');
    const ins = await db.query<{ id: string }>('INSERT INTO bank_file (run_id, bank_code, format, filename, content, checksum, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id',
      [run.id, file.bankCode, file.format, file.filename, file.content, checksum, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'bank_file', table: 'bank_file', rowId: ins.rows[0]!.id, after: { bankCode: file.bankCode, rows: bankRows.length, checksum } });
    return res.status(201).json({ id: ins.rows[0]!.id, filename: file.filename, rows: bankRows.length, checksum, warnings: file.errors });
  });
  r.get('/runs/:id/bank-files/:fileId', requireRole('finance'), async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const f = await db.query<{ filename: string; content: string; format: string }>('SELECT filename, content, format FROM bank_file WHERE id = $1 AND run_id = $2', [z.uuid().parse(req.params.fileId), run.id]);
    if (!f.rows[0]) throw new NotFoundError('file not found');
    await audit(db, { actorId: req.user!.id, action: 'download', table: 'bank_file', rowId: req.params.fileId as string });
    res.setHeader('Content-Disposition', `attachment; filename="${f.rows[0].filename}"`);
    res.type(f.rows[0].format === 'txt' ? 'text/plain' : f.rows[0].format === 'tsv' ? 'text/tab-separated-values' : 'text/csv').send(f.rows[0].content);
  });
  r.get('/runs/:id/ssf-sheet.csv', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    res.setHeader('Content-Disposition', `attachment; filename="ssf-${run.id}.csv"`);
    res.type('text/csv').send(await ssfSheetCsv(db, run.id));
  });
  r.get('/runs/:id/etds.csv', requireRole('finance'), async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const rows = (await etdsRows(db, run.id)).map((x) => ({ ...x, pan: x.pan_enc ? cipher.decrypt(x.pan_enc, `employee:${x.employee_id}:pan`) : '' }));
    await audit(db, { actorId: req.user!.id, action: 'etds_export', table: 'payroll_run', rowId: run.id });
    res.setHeader('Content-Disposition', `attachment; filename="etds-${run.id}.csv"`);
    res.type('text/csv').send(etdsCsv(rows));
  });
  r.get('/runs/:id/journal.:fmt', payrollRole, async (req, res) => {
    const run = await getRun(db, cid(req), z.uuid().parse(req.params.id));
    const rows = await journal(db, cid(req), run.id);
    if (req.params.fmt === 'json') return res.json({ journal: rows });
    res.setHeader('Content-Disposition', `attachment; filename="journal-${run.id}.csv"`);
    return res.type('text/csv').send(journalCsv(rows));
  });

  // ---------------------------------------------------------------- payslips (employee self-service, plan §13.7)
  const canSeePayslip = (user: NonNullable<typeof r extends never ? never : import('../auth/service.js').AuthUser>, employeeId: string) =>
    hasRole(user, 'hr', 'finance') || user.employeeId === employeeId;

  r.get('/payslips', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), fiscalYearStart: z.coerce.number().int().optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    if (!canSeePayslip(req.user!, employeeId)) throw new ForbiddenError();
    const onlyPublished = !hasRole(req.user!, 'hr', 'finance');
    const rows = await db.query(
      `SELECT ps.id, ps.run_id, r.run_type, p.bs_year, p.bs_month, ps.paid_days, ps.lop_days, ps.ot_hours, ps.gross, ps.tds, ps.total_deductions, ps.net, ps.published_at, r.locked_at
         FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
        WHERE ps.employee_id = $1 AND r.company_id = $2 AND ($3::boolean = false OR ps.published_at IS NOT NULL)
          AND ($4::int IS NULL OR (CASE WHEN p.bs_month >= 4 THEN p.bs_year ELSE p.bs_year - 1 END) = $4)
        ORDER BY p.bs_year DESC, p.bs_month DESC, ps.created_at DESC`, [employeeId, cid(req), onlyPublished, q.fiscalYearStart ?? null]);
    res.json({ payslips: rows.rows });
  });
  async function loadPayslip(req: { user?: import('../auth/service.js').AuthUser; params: Record<string, string | undefined> }) {
    const id = z.uuid().parse(req.params.id);
    const ps = await db.query<Record<string, any>>(
      `SELECT ps.*, e.emp_code, e.name_en, e.name_ne, e.designation, e.ssf_id, e.bank_account_last4, e.pan_enc, c.name_en AS company_en, c.name_ne AS company_ne, p.bs_year, p.bs_month, p.start_ad, p.end_ad, r.locked_at, r.company_id,
              (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> p.end_ad LIMIT 1) AS department
         FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id JOIN company c ON c.id = r.company_id
        WHERE ps.id = $1`, [id]);
    const row = ps.rows[0];
    if (!row || row.company_id !== req.user!.companyId) throw new NotFoundError('payslip not found');
    if (!canSeePayslip(req.user!, row.employee_id)) throw new ForbiddenError();
    if (!hasRole(req.user!, 'hr', 'finance') && !row.published_at) throw new NotFoundError('payslip not found');
    const lines = await db.query('SELECT component_code AS code, type, name_en, name_ne, amount, basis, sort_order FROM payslip_line WHERE payslip_id = $1 ORDER BY sort_order', [id]);
    return { row, lines: lines.rows as Array<{ code: string; type: string; name_en: string; name_ne: string | null; amount: string; basis: unknown; sort_order: number }> };
  }
  r.get('/payslips/:id', async (req, res) => {
    const { row, lines } = await loadPayslip(req);
    const { pan_enc: _p, inputs_snapshot: _s, ...rest } = row;
    res.json({ payslip: rest, lines, tax: row.inputs_snapshot?.result?.tax ?? null });
  });
  r.get('/payslips/:id/html', async (req, res) => {
    const { row, lines } = await loadPayslip(req);
    const numerals = ((await db.query<{ value: { numerals?: string } }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payslip.policy'`, [cid(req)])).rows[0]?.value.numerals === 'devanagari') ? 'devanagari' : 'latin';
    const pan = row.pan_enc ? cipher.decrypt(row.pan_enc, `employee:${row.employee_id}:pan`) : null;
    const html = renderPayslipHtml({
      company: { nameEn: row.company_en, nameNe: row.company_ne }, payslipId: row.id, numerals,
      employee: { code: row.emp_code, nameEn: row.name_en, nameNe: row.name_ne, designation: row.designation, department: row.department,
        panMasked: pan ? `*****${pan.slice(-4)}` : null, ssfMasked: row.ssf_id ? `*******${String(row.ssf_id).slice(-4)}` : null, bankMasked: row.bank_account_last4 ? `****${row.bank_account_last4}` : null },
      period: { bsYear: row.bs_year, bsMonth: row.bs_month, startAd: row.start_ad, endAd: row.end_ad },
      paidDays: String(row.paid_days), lopDays: String(row.lop_days), otHours: String(row.ot_hours),
      lines: lines.map((l) => ({ code: l.code, type: l.type, nameEn: l.name_en, nameNe: l.name_ne, amount: String(l.amount) })),
      gross: String(row.gross), totalDeductions: String(row.total_deductions), employerStatutory: String(row.employer_statutory), net: String(row.net),
      ytd: { gross: row.ytd?.gross ?? '0', tds: row.ytd?.tds ?? '0', statutory: row.ytd?.statutory ?? '0' }, explain: row.explain ?? [],
    });
    res.type('text/html').send(html);
  });

  return r;
}
