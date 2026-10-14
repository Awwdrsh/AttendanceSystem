import { createHash } from 'node:crypto';
import { bsMonthName } from '../../bsdate.js';
import { computePayslip, D, Decimal, type ComponentDef, type PayrollInput, type PayrollResult, type PayrollPolicy, type LoanInput } from '@hajiri/payroll-engine';
import { adToBs, bsMonthBounds, fiscalYear, fiscalYearOf } from '@hajiri/bs-calendar';
import type { Db, Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, HttpError, NotFoundError } from '../../errors.js';
import { lockAttendance } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { resolveRules } from './rules.js';

export interface PeriodRow { id: string; company_id: string; bs_year: number; bs_month: number; start_ad: string; end_ad: string; days: number; attendance_locked_at: Date | null }
export interface RunRow {
  id: string; company_id: string; period_id: string; run_type: string; status: string; rounding_policy: string; references_run_id: string | null;
  preflight: unknown; variance: unknown; maker_id: string | null; checker_id: string | null; approval_signature: string | null; locked_at: Date | null;
  params: Record<string, unknown>; totals: Record<string, unknown> | null; notes: string | null; created_at: Date; computed_at: Date | null; approved_at: Date | null;
}

export async function getPeriod(db: Queryable, companyId: string, periodId: string): Promise<PeriodRow> {
  const r = await db.query<PeriodRow>('SELECT * FROM payroll_period WHERE id = $1 AND company_id = $2', [periodId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('payroll period not found');
  return r.rows[0];
}

export async function ensurePeriod(db: Queryable, companyId: string, bsYear: number, bsMonth: number): Promise<PeriodRow> {
  const b = bsMonthBounds(bsYear, bsMonth);
  const r = await db.query<PeriodRow>(
    `INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT (company_id, bs_year, bs_month) DO UPDATE SET days = EXCLUDED.days RETURNING *`, [companyId, bsYear, bsMonth, b.startAd, b.endAd, b.days]);
  return r.rows[0]!;
}

export async function getRun(db: Queryable, companyId: string, runId: string): Promise<RunRow> {
  const r = await db.query<RunRow>('SELECT * FROM payroll_run WHERE id = $1 AND company_id = $2', [runId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('payroll run not found');
  return r.rows[0];
}

export async function payrollPolicy(db: Queryable, companyId: string): Promise<PayrollPolicy & { payDay: number; twoApproverThreshold: string; encashmentDivisor: string }> {
  const r = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payroll.policy'`, [companyId]);
  const v = r.rows[0]?.value ?? {};
  return {
    prorationBasis: (v.prorationBasis as PayrollPolicy['prorationBasis']) ?? 'bs_calendar_days',
    hourlyDivisor: typeof v.hourlyDivisor === 'number' ? v.hourlyDivisor : 208,
    rounding: (v.rounding as PayrollPolicy['rounding']) === 'paisa' ? 'paisa' : 'half_up_rupee',
    includeEmployerContributionInIncome: v.includeEmployerContributionInIncome !== false,
    fullTime: true,
    payDay: typeof v.payDay === 'number' ? v.payDay : 25,
    twoApproverThreshold: String(v.twoApproverThreshold ?? '5000000'),
    encashmentDivisor: String(v.encashmentDivisor ?? 'bs_days'),
  };
}

// ---------------------------------------------------------------- pre-flight (plan §13.6 step 1)
export interface Preflight { ok: boolean; blockers: Array<{ code: string; message: string; count?: number; employees?: string[] }>; warnings: Array<{ code: string; message: string; count?: number; employees?: string[] }>; info: Record<string, unknown> }

export async function preflight(db: Queryable, companyId: string, run: RunRow): Promise<Preflight> {
  const p = await getPeriod(db, companyId, run.period_id);
  const blockers: Preflight['blockers'] = []; const warnings: Preflight['warnings'] = [];
  const emps = await activeEmployees(db, companyId, p, run);
  const ids = emps.map((e) => e.id);

  const unlocked = await db.query<{ n: string }>(
    `SELECT count(*)::text AS n FROM v_attendance_latest v WHERE v.employee_id = ANY($1) AND v.local_date BETWEEN $2 AND $3 AND v.locked_at IS NULL`, [ids, p.start_ad, p.end_ad]);
  if (Number(unlocked.rows[0]?.n) > 0 && run.run_type === 'regular') blockers.push({ code: 'attendance_not_locked', message: 'Attendance for the period is not locked yet', count: Number(unlocked.rows[0]!.n) });

  const missingRows = await db.query<{ emp_code: string; missing: string }>(
    `SELECT e.emp_code, concat_ws(', ', CASE WHEN e.bank_account_enc IS NULL THEN 'bank account' END, CASE WHEN e.pan_enc IS NULL THEN 'PAN' END,
            CASE WHEN e.regime = 'ssf' AND e.ssf_id IS NULL THEN 'SSF id' END, CASE WHEN e.regime = 'pf' AND e.pf_id IS NULL THEN 'PF id' END) AS missing
       FROM employee e WHERE e.id = ANY($1)`, [ids]);
  const missing = missingRows.rows.filter((r) => r.missing);
  if (missing.length) blockers.push({ code: 'missing_master_data', message: 'Employees missing bank/PAN/SSF details', count: missing.length, employees: missing.map((m) => `${m.emp_code}: ${m.missing}`) });

  const noAssign = emps.filter((e) => !e.assignment);
  if (noAssign.length) blockers.push({ code: 'no_salary_assignment', message: 'Employees without a salary assignment for the period', count: noAssign.length, employees: noAssign.map((e) => e.emp_code) });

  const incomplete = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM v_attendance_latest WHERE employee_id = ANY($1) AND local_date BETWEEN $2 AND $3 AND status = 'Incomplete'`, [ids, p.start_ad, p.end_ad]);
  if (Number(incomplete.rows[0]?.n) > 0) warnings.push({ code: 'incomplete_days', message: 'Days with a missing punch will count as unpaid unless regularised', count: Number(incomplete.rows[0]!.n) });
  const pendingReg = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM regularisation_request WHERE employee_id = ANY($1) AND local_date BETWEEN $2 AND $3 AND status = 'pending'`, [ids, p.start_ad, p.end_ad]);
  if (Number(pendingReg.rows[0]?.n) > 0) warnings.push({ code: 'pending_regularisations', message: 'Regularisation requests still pending', count: Number(pendingReg.rows[0]!.n) });
  const pendingLeave = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM leave_request WHERE employee_id = ANY($1) AND status = 'pending' AND from_date <= $3 AND to_date >= $2`, [ids, p.start_ad, p.end_ad]);
  if (Number(pendingLeave.rows[0]?.n) > 0) warnings.push({ code: 'pending_leave', message: 'Leave requests pending that affect this period', count: Number(pendingLeave.rows[0]!.n) });
  const otDetected = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM ot_slip WHERE employee_id = ANY($1) AND local_date BETWEEN $2 AND $3 AND status = 'detected'`, [ids, p.start_ad, p.end_ad]);
  if (Number(otDetected.rows[0]?.n) > 0) warnings.push({ code: 'ot_unapproved', message: 'Detected overtime not yet approved (only approved hours are paid)', count: Number(otDetected.rows[0]!.n) });

  const joiners = emps.filter((e) => e.join_date && e.join_date >= p.start_ad && e.join_date <= p.end_ad).map((e) => e.emp_code);
  const leavers = emps.filter((e) => e.exit_date && e.exit_date >= p.start_ad && e.exit_date <= p.end_ad).map((e) => e.emp_code);
  return { ok: blockers.length === 0, blockers, warnings, info: { employees: emps.length, joiners, leavers, period: { bsYear: p.bs_year, bsMonth: p.bs_month, startAd: p.start_ad, endAd: p.end_ad, days: p.days } } };
}

// ---------------------------------------------------------------- compute (plan §11.1, §13.6 step 3)
interface EmpRow {
  id: string; emp_code: string; name_en: string; name_ne: string | null; gender: 'male' | 'female' | 'other' | null; marital_status: 'single' | 'couple' | null;
  is_resident: boolean; regime: 'ssf' | 'pf' | 'none'; remote_area_class: 'A' | 'B' | 'C' | 'D' | null; disability: boolean; ot_eligible: boolean; festival_month: number | null;
  join_date: string | null; exit_date: string | null; assignment: { structure_version_id: string; base_amounts: Record<string, string>; valid: string } | null;
}

async function activeEmployees(db: Queryable, companyId: string, p: PeriodRow, run: RunRow): Promise<EmpRow[]> {
  const only = Array.isArray(run.params.employeeIds) ? (run.params.employeeIds as string[]) : null;
  const r = await db.query<EmpRow>(
    `SELECT e.id, e.emp_code, e.name_en, e.name_ne, e.gender, e.marital_status, e.is_resident, e.regime, e.remote_area_class, e.disability, e.ot_eligible, e.festival_month,
            e.join_date::text, e.exit_date::text,
            (SELECT jsonb_build_object('structure_version_id', a.structure_version_id, 'base_amounts', a.base_amounts, 'valid', a.valid::text)
               FROM salary_assignment a WHERE a.employee_id = e.id AND a.valid && daterange($2::date, $3::date, '[]') ORDER BY upper_inf(a.valid) DESC, lower(a.valid) DESC LIMIT 1) AS assignment
       FROM employee e
      WHERE e.company_id = $1 AND (e.join_date IS NULL OR e.join_date <= $3) AND (e.exit_date IS NULL OR e.exit_date >= $2)
        AND ($4::uuid[] IS NULL OR e.id = ANY($4))
      ORDER BY e.emp_code`, [companyId, p.start_ad, p.end_ad, only]);
  return r.rows;
}

async function componentsFor(db: Queryable, companyId: string, structureVersionId: string): Promise<ComponentDef[]> {
  const v = await db.query<{ components: Array<{ code: string }> }>('SELECT components FROM salary_structure_version WHERE id = $1', [structureVersionId]);
  if (!v.rows[0]) throw new BadRequestError('salary structure version not found');
  const codes = v.rows[0].components.map((c) => c.code);
  const r = await db.query<{ code: string; name_en: string; name_ne: string | null; type: ComponentDef['type']; formula: string | null; condition: string | null; depends_on_payment_days: boolean; taxable: boolean; statutory: boolean; rounding: 'paisa' | 'rupee'; sort_order: number }>(
    'SELECT * FROM salary_component WHERE company_id = $1 AND code = ANY($2) AND active', [companyId, codes]);
  return r.rows.map((c) => ({ code: c.code, type: c.type, nameEn: c.name_en, nameNe: c.name_ne, formula: c.formula, condition: c.condition, dependsOnPaymentDays: c.depends_on_payment_days, taxable: c.taxable, statutory: c.statutory, rounding: c.rounding, sortOrder: c.sort_order }));
}

function monthsBetween(fromAd: string, toAd: string): number {
  const a = new Date(fromAd); const b = new Date(toAd);
  return Math.max(0, Math.round((b.getTime() - a.getTime()) / (30.44 * 86_400_000)));
}

export interface ComputeOverride { reason: string; roles: string[] }

export async function computeRun(db: Db, companyId: string, runId: string, actorId: string, override?: ComputeOverride): Promise<{ payslips: number; totals: Record<string, string>; variance: unknown }> {
  const run = await getRun(db, companyId, runId);
  if (!['draft', 'preflight_failed', 'computed', 'reviewed'].includes(run.status)) throw new ConflictError(`run is ${run.status}; cannot compute`);
  const pf = await preflight(db, companyId, run);
  if (!pf.ok) {
    // Blockers stop the run unless HR explicitly overrides with a reason (plan §13.6: blockers vs warnings); the override is audited.
    const canOverride = override && override.reason.trim().length >= 5 && (override.roles.includes('hr') || override.roles.includes('admin'));
    if (!canOverride) {
      await db.query(`UPDATE payroll_run SET status = 'preflight_failed', preflight = $2 WHERE id = $1`, [runId, JSON.stringify(pf)]);
      throw new HttpError(409, 'pre-flight blockers must be resolved before computing (HR may override with a reason)', pf);
    }
    pf.info = { ...pf.info, override: { reason: override!.reason, by: actorId, at: new Date().toISOString() } };
    await audit(db, { actorId, action: 'preflight_override', table: 'payroll_run', rowId: runId, after: { reason: override!.reason, blockers: pf.blockers.map((b) => b.code) } });
  }
  const p = await getPeriod(db, companyId, run.period_id);
  const policy = await payrollPolicy(db, companyId);
  const { rules, ids: ruleIds } = await resolveRules(db, p.end_ad);
  const fy = fiscalYearOf(adToBs(p.start_ad));
  const monthIndexInFy = ((p.bs_month - 4 + 12) % 12) + 1;
  const emps = await activeEmployees(db, companyId, p, run);
  const prev = await previousNets(db, companyId, run);

  const client = await db.connect();
  const results: Array<{ emp: EmpRow; result: PayrollResult; input: PayrollInput; payslipId: string }> = [];
  const componentCache = new Map<string, ComponentDef[]>();
  const isFinal = run.run_type === 'final_settlement';
  // Only regular and final-settlement runs carry the salary itself; every other run type pays one-time lines only.
  const carriesSalary = run.run_type === 'regular' || isFinal;
  try {
    await client.query('BEGIN');
    await client.query('DELETE FROM payslip_line WHERE payslip_id IN (SELECT id FROM payslip WHERE run_id = $1)', [runId]);
    await client.query('DELETE FROM payslip WHERE run_id = $1', [runId]);
    for (const emp of emps) {
      if (!emp.assignment) continue;
      let components = componentCache.get(emp.assignment.structure_version_id);
      if (!components) { components = await componentsFor(client, companyId, emp.assignment.structure_version_id); componentCache.set(emp.assignment.structure_version_id, components); }
      // Employment window inside the period.
      const from = emp.join_date && emp.join_date > p.start_ad ? emp.join_date : p.start_ad;
      const to = emp.exit_date && emp.exit_date < p.end_ad ? emp.exit_date : p.end_ad;
      const employedDays = Math.round((new Date(to).getTime() - new Date(from).getTime()) / 86_400_000) + 1;
      const att = await client.query<{ lop: string; ot_min: string; rows: string }>(
        `SELECT coalesce(sum(CASE
                  -- half-day leave with no punches: the leave half is paid unless the leave type is unpaid, the other half is absent
                  WHEN status = 'Absent' AND flags @> '[{"kind":"HalfDayLeave"}]' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 1
                  WHEN status = 'Absent' AND flags @> '[{"kind":"HalfDayLeave"}]' THEN 0.5
                  WHEN status IN ('Absent', 'Incomplete') THEN 1
                  WHEN status = 'HalfDay' THEN 0.5
                  WHEN status = 'OnLeave' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 1
                  WHEN status = 'HalfDayLeave' AND flags @> '[{"kind":"UnpaidLeave"}]' THEN 0.5
                  ELSE 0 END), 0)::text AS lop,
                count(*)::text AS rows,
                (SELECT coalesce(sum(approved_min), 0) FROM ot_slip s WHERE s.employee_id = $1 AND s.local_date BETWEEN $2 AND $3 AND s.status IN ('approved', 'posted'))::text AS ot_min
           FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3`, [emp.id, from, to]);
      const lop = D(att.rows[0]?.lop ?? 0);
      const paidDays = D(employedDays).minus(lop);
      const otHours = D(att.rows[0]?.ot_min ?? 0).div(60).toDecimalPlaces(2);

      // Year-to-date from every LOCKED payslip of this fiscal year, including other runs of the same period (a locked
      // regular run before a supplementary one). Only locked runs count: an unlocked run may still be recomputed.
      const ytd = await client.query<{ taxable: string; tds: string; ee: string; er: string; festival: boolean; gross: string }>(
        `SELECT coalesce(sum(ps.taxable), 0)::text AS taxable, coalesce(sum(ps.tds), 0)::text AS tds,
                coalesce((SELECT sum(l.amount) FROM payslip_line l WHERE l.payslip_id = ANY(array_agg(ps.id)) AND l.component_code IN ('SSF_EE', 'PF_EE', 'CIT')), 0)::text AS ee,
                coalesce(sum(ps.employer_statutory), 0)::text AS er, coalesce(sum(ps.gross), 0)::text AS gross,
                coalesce(bool_or(EXISTS (SELECT 1 FROM payslip_line l WHERE l.payslip_id = ps.id AND l.component_code = 'FESTIVAL')), false) AS festival
           FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period pp ON pp.id = r.period_id
          WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.id <> $4 AND pp.start_ad >= $2 AND pp.start_ad <= $3`, [emp.id, fy.startAd, p.start_ad, runId]);
      const y = ytd.rows[0]!;
      // One-off runs have no salary lines, so the annual projection borrows the recurring figures of the latest locked
      // regular payslip of this fiscal year (plan §11.4 keeps projecting the year).
      let recurringOverride: { taxable: string; retirement: string } | undefined;
      if (!carriesSalary) {
        const ref = await client.query<{ tax: { recurringTaxable?: string; recurringRetirement?: string } | null }>(
          `SELECT ps.inputs_snapshot->'result'->'tax' AS tax FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period pp ON pp.id = r.period_id
            WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND pp.start_ad >= $2 AND pp.start_ad <= $3
            ORDER BY pp.start_ad DESC, r.locked_at DESC LIMIT 1`, [emp.id, fy.startAd, p.start_ad]);
        const t = ref.rows[0]?.tax;
        if (t?.recurringTaxable) recurringOverride = { taxable: t.recurringTaxable, retirement: t.recurringRetirement ?? '0' };
      }
      const decl = await client.query<{ life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string }>(
        `SELECT life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2 AND status IN ('submitted', 'verified')`, [emp.id, fy.startYear]);
      const dcl = decl.rows[0];
      const loans = await client.query<{ id: string; emi: string; principal: string; paid: string; kind: 'loan' | 'advance' }>(
        `SELECT l.id, l.emi, l.principal, l.kind, coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)::text AS paid
           FROM loan l WHERE l.employee_id = $1 AND l.status = 'active' AND (l.start_bs_year, l.start_bs_month) <= ($2, $3)`, [emp.id, p.bs_year, p.bs_month]);
      const loanInputs: LoanInput[] = loans.rows.map((l) => ({ id: l.id, emi: l.emi, remaining: D(l.principal).minus(l.paid).toFixed(2), kind: l.kind })).filter((l) => D(l.remaining).gt(0));
      const inputs = (await client.query<{ component_code: string; amount: string; note: string | null }>('SELECT component_code, amount, note FROM payroll_input WHERE run_id = $1 AND employee_id = $2', [runId, emp.id])).rows
        .map((i) => ({ code: i.component_code, amount: i.amount, ...(i.note ? { note: i.note } : {}) }));

      // Festival allowance proration (Sec 37(3)): months of service inside the fiscal year, bounded by join and exit dates.
      const serviceFrom = emp.join_date && emp.join_date > fy.startAd ? emp.join_date : fy.startAd;
      const serviceTo = emp.exit_date && emp.exit_date < fy.endAd ? emp.exit_date : fy.endAd;
      const extras: NonNullable<PayrollInput['extras']> = {
        monthsOfServiceInFy: serviceFrom === fy.startAd && serviceTo === fy.endAd ? 12 : Math.min(12, Math.max(1, monthsBetween(serviceFrom, serviceTo))),
        ...(recurringOverride ? { recurringOverride } : {}),
      };
      if (isFinal) {
        const enc = await client.query<{ days: string }>(
          `SELECT coalesce(sum(ll.delta_days), 0)::text AS days FROM leave_ledger ll JOIN leave_type lt ON lt.id = ll.leave_type_id WHERE ll.employee_id = $1 AND lt.encashable`, [emp.id]);
        extras.isFinalSettlement = true; extras.recoverLoansInFull = true; extras.deductFullTaxNow = true;
        extras.leaveEncashDays = D(enc.rows[0]?.days ?? 0).toFixed(2);
        extras.leaveEncashDivisor = policy.encashmentDivisor === 'bs_days' ? p.days : Number(policy.encashmentDivisor) || p.days;
        extras.forceFestivalAllowance = !y.festival;
        if (typeof run.params.noticePay === 'string' && !D(run.params.noticePay).isZero()) {
          inputs.push(D(run.params.noticePay).gt(0) ? { code: 'NOTICE_PAY', amount: D(run.params.noticePay).toFixed(2) } : { code: 'NOTICE_DEDUCTION', amount: D(run.params.noticePay).abs().toFixed(2) });
        }
      }
      const baseAmounts = carriesSalary ? emp.assignment.base_amounts : {};
      const input: PayrollInput = {
        employee: { id: emp.id, code: emp.emp_code, nameEn: emp.name_en, nameNe: emp.name_ne, gender: emp.gender, maritalStatus: emp.marital_status, isResident: emp.is_resident, regime: emp.regime,
          remoteAreaClass: emp.remote_area_class, disability: emp.disability, otEligible: emp.ot_eligible, festivalMonth: emp.festival_month },
        period: { bsYear: p.bs_year, bsMonth: p.bs_month, daysInMonth: p.days, fiscalYearStart: fy.startYear, monthIndexInFy },
        components, baseAmounts, inputs,
        attendance: carriesSalary ? { paidDays: paidDays.toFixed(2), lopDays: lop.toFixed(2), otHours: run.run_type === 'regular' ? otHours.toFixed(2) : '0' }
          : { paidDays: String(p.days), lopDays: '0', otHours: '0' },
        ytd: { taxable: y.taxable, tdsPaid: y.tds, retirementEmployee: y.ee, retirementEmployer: y.er, festivalPaid: Boolean(y.festival) },
        declaration: { lifePremium: dcl?.life_premium ?? '0', healthPremium: dcl?.health_premium ?? '0', citMonthly: dcl?.cit_monthly ?? '0', medicalCost: dcl?.medical_cost ?? '0', otherRetirementAnnual: dcl?.other_retirement_annual ?? '0' },
        loans: run.run_type === 'regular' || isFinal ? loanInputs : [], rules, policy, extras,
      };
      if (!carriesSalary && run.run_type !== 'arrears' && run.run_type !== 'bonus' && inputs.length === 0) continue; // nothing to pay in a one-off run
      if (run.run_type === 'arrears') {
        const ref = await referencedGross(client, run.references_run_id!, emp.id);
        if (!ref) continue;
        // Recompute the referenced period as if the current assignment had applied then (same attendance, same one-time inputs).
        const { recurringOverride: _ro, ...asIfExtras } = extras;
        const asIf = computePayslip({ ...input, extras: asIfExtras, baseAmounts: emp.assignment.base_amounts, inputs: ref.inputs, loans: [], attendance: ref.attendance, ytd: ref.ytd });
        // Compare earnings before the net-rounding adjustment so paisa differences are not lost.
        const asIfGross = asIf.lines.filter((l) => l.type === 'earning' && l.code !== 'ROUNDING').reduce((a, l) => a.plus(l.amount), D(0));
        const diff = asIfGross.minus(ref.gross);
        if (!diff.gt(0)) continue;
        input.inputs = [{ code: 'ARREARS', amount: diff.toFixed(2), note: `difference vs run ${run.references_run_id}` }];
      }
      if (run.run_type === 'bonus') {
        const bonus = (run.params.bonusByEmployee as Record<string, string> | undefined)?.[emp.id];
        if (!bonus || !D(bonus).gt(0)) continue;
        input.inputs = [{ code: 'BONUS', amount: D(bonus).toFixed(2), note: 'Bonus Act 2030' }];
      }
      const result = computePayslip(input);
      const flags = [...result.flags];
      const prevNet = prev.get(emp.id);
      if (run.run_type === 'regular') {
        if (prevNet === undefined) flags.push('FirstPayslip');
        else if (D(prevNet).gt(0) && D(result.net).minus(prevNet).abs().div(prevNet).gt('0.10')) flags.push('NetChangedOver10Pct');
        if (paidDays.lte(0)) flags.push('ZeroAttendance');
        if (otHours.gt(20)) flags.push('HighOvertime');
      }
      if (!carriesSalary && !recurringOverride) flags.push('NoRecurringReference');
      if (emp.exit_date && emp.exit_date <= p.end_ad) flags.push('LastMonth');
      const ps = await client.query<{ id: string }>(
        `INSERT INTO payslip (run_id, employee_id, inputs_snapshot, rule_version_ids, bs_days_in_month, paid_days, lop_days, ot_hours, gross, taxable, tds, employee_statutory, employer_statutory, total_deductions, net, ytd, explain, flags)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) RETURNING id`,
        [runId, emp.id, JSON.stringify({ input, result: { tax: result.tax, ssfAllocation: result.ssfAllocation, loanInstallments: result.loanInstallments, payableFraction: result.payableFraction } }), ruleIds, p.days,
          result.paidDays, result.lopDays, result.otHours, result.gross, result.taxable, result.tds, result.employeeStatutory, result.employerStatutory, result.totalDeductions, result.net,
          JSON.stringify({ gross: D(y.gross).plus(result.gross).toFixed(2), taxable: D(y.taxable).plus(result.taxable).toFixed(2), tds: D(y.tds).plus(result.tds).toFixed(2), statutory: D(y.ee).plus(result.employeeStatutory).toFixed(2) }),
          JSON.stringify(result.explain), JSON.stringify(flags)]);
      for (const l of result.lines) {
        await client.query('INSERT INTO payslip_line (payslip_id, component_code, type, name_en, name_ne, amount, basis, sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)',
          [ps.rows[0]!.id, l.code, l.type, l.nameEn, l.nameNe, l.amount, JSON.stringify(l.basis), l.sortOrder]);
      }
      results.push({ emp, result, input, payslipId: ps.rows[0]!.id });
    }
    const totals = {
      headcount: String(results.length),
      gross: results.reduce((a, r) => a.plus(r.result.gross), D(0)).toFixed(2),
      net: results.reduce((a, r) => a.plus(r.result.net), D(0)).toFixed(2),
      tds: results.reduce((a, r) => a.plus(r.result.tds), D(0)).toFixed(2),
      employeeStatutory: results.reduce((a, r) => a.plus(r.result.employeeStatutory), D(0)).toFixed(2),
      employerStatutory: results.reduce((a, r) => a.plus(r.result.employerStatutory), D(0)).toFixed(2),
    };
    const variance = {
      flagged: results.filter((r) => r.result.flags.length || prev.get(r.emp.id) === undefined).length,
      previousNet: [...prev.values()].reduce((a, n) => a.plus(n), D(0)).toFixed(2),
      byEmployee: results.map((r) => ({ employeeId: r.emp.id, empCode: r.emp.emp_code, net: r.result.net, previousNet: prev.get(r.emp.id) ?? null, flags: r.result.flags })),
    };
    await client.query(`UPDATE payroll_run SET status = 'computed', computed_at = now(), preflight = $2, totals = $3, variance = $4, maker_id = coalesce(maker_id, $5) WHERE id = $1`,
      [runId, JSON.stringify(pf), JSON.stringify(totals), JSON.stringify(variance), actorId]);
    await audit(client, { actorId, action: 'compute', table: 'payroll_run', rowId: runId, after: totals });
    await client.query('COMMIT');
    return { payslips: results.length, totals, variance };
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
}

async function previousNets(db: Queryable, companyId: string, run: RunRow): Promise<Map<string, string>> {
  const r = await db.query<{ employee_id: string; net: string }>(
    `SELECT ps.employee_id, ps.net FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
      WHERE r.company_id = $1 AND r.run_type = 'regular' AND r.locked_at IS NOT NULL AND r.id <> $2
        AND (p.bs_year, p.bs_month) = (SELECT p2.bs_year, p2.bs_month FROM payroll_run r2 JOIN payroll_period p2 ON p2.id = r2.period_id
                                        WHERE r2.company_id = $1 AND r2.run_type = 'regular' AND r2.locked_at IS NOT NULL AND r2.id <> $2
                                          AND (p2.bs_year, p2.bs_month) < (SELECT bs_year, bs_month FROM payroll_period WHERE id = $3)
                                        ORDER BY p2.bs_year DESC, p2.bs_month DESC LIMIT 1)`, [companyId, run.id, run.period_id]);
  return new Map(r.rows.map((x) => [x.employee_id, x.net]));
}

async function referencedGross(db: Queryable, refRunId: string, employeeId: string): Promise<{ gross: string; attendance: PayrollInput['attendance']; inputs: PayrollInput['inputs']; ytd: PayrollInput['ytd'] } | null> {
  const r = await db.query<{ gross: string; inputs_snapshot: { input: PayrollInput } }>(
    `SELECT coalesce((SELECT sum(l.amount) FROM payslip_line l WHERE l.payslip_id = ps.id AND l.type = 'earning' AND l.component_code <> 'ROUNDING'), 0)::text AS gross, ps.inputs_snapshot
       FROM payslip ps WHERE ps.run_id = $1 AND ps.employee_id = $2`, [refRunId, employeeId]);
  const row = r.rows[0];
  return row ? { gross: row.gross, attendance: row.inputs_snapshot.input.attendance, inputs: row.inputs_snapshot.input.inputs, ytd: row.inputs_snapshot.input.ytd } : null;
}

// ---------------------------------------------------------------- lifecycle (plan §9.5, §11.8)
export async function reviewRun(db: Db, companyId: string, runId: string, actorId: string, notes?: string): Promise<void> {
  const run = await getRun(db, companyId, runId);
  if (run.status !== 'computed') throw new ConflictError(`run is ${run.status}; compute it first`);
  await db.query(`UPDATE payroll_run SET status = 'reviewed', reviewed_at = now(), maker_id = $2, notes = coalesce($3, notes) WHERE id = $1`, [runId, actorId, notes ?? null]);
  await audit(db, { actorId, action: 'review', table: 'payroll_run', rowId: runId });
}

export interface ApprovalResult { signature: string; approved: boolean; pendingSecondApproval: boolean }

/**
 * Checker approval (plan §11.8, §12.4): a finance user other than the maker signs. Above payroll.policy.twoApproverThreshold
 * (total net) a second, different finance approver is required before the run becomes approved.
 */
export async function approveRun(db: Db, companyId: string, runId: string, checker: { id: string; roles: string[] }): Promise<ApprovalResult> {
  const run = await getRun(db, companyId, runId);
  if (run.status !== 'reviewed') throw new ConflictError(`run is ${run.status}; it must be reviewed by the maker first`);
  if (run.maker_id === checker.id) throw new ForbiddenError('the maker cannot approve their own run (maker-checker)');
  if (!checker.roles.includes('finance') && !checker.roles.includes('admin')) throw new ForbiddenError('approval requires the finance role');
  const policy = await payrollPolicy(db, companyId);
  const totalNet = D((run.totals?.net as string | undefined) ?? '0');
  const needsTwo = totalNet.gt(policy.twoApproverThreshold);
  const first = run.params.firstApproval as { checkerId: string; signature: string; at: string } | undefined;
  const payload = JSON.stringify({ runId, totals: run.totals, checkerId: checker.id, at: new Date().toISOString() });
  const signature = `sha256:${createHash('sha256').update(payload).digest('hex')}`;
  if (needsTwo && !first) {
    await db.query(`UPDATE payroll_run SET params = params || $2::jsonb WHERE id = $1`, [runId, JSON.stringify({ firstApproval: { checkerId: checker.id, signature, at: new Date().toISOString() } })]);
    await audit(db, { actorId: checker.id, action: 'approve_first', table: 'payroll_run', rowId: runId, after: { signature, payload, threshold: policy.twoApproverThreshold } });
    return { signature, approved: false, pendingSecondApproval: true };
  }
  if (needsTwo && first && first.checkerId === checker.id) throw new ConflictError('a second, different finance approver is required above the two-approver threshold');
  await db.query(`UPDATE payroll_run SET status = 'approved', approved_at = now(), checker_id = $2, approval_signature = $3 WHERE id = $1`, [runId, checker.id, signature]);
  await audit(db, { actorId: checker.id, action: 'approve', table: 'payroll_run', rowId: runId, after: { signature, payload, ...(first ? { firstApproval: first } : {}) } });
  return { signature, approved: true, pendingSecondApproval: false };
}

export async function lockRun(db: Db, companyId: string, runId: string, actorId: string): Promise<{ attendanceLocked: number }> {
  const run = await getRun(db, companyId, runId);
  if (run.status !== 'approved') throw new ConflictError(`run is ${run.status}; approve it first`);
  const p = await getPeriod(db, companyId, run.period_id);
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    // A run scoped to specific employees (final settlement, supplementary) locks only their attendance; a regular run locks the period.
    const only = Array.isArray(run.params.employeeIds) ? (run.params.employeeIds as string[]) : null;
    let n: number;
    if (only) {
      const r = await client.query(
        `UPDATE attendance_day a SET locked_at = now() FROM v_attendance_latest v
          WHERE a.id = v.id AND v.employee_id = ANY($1) AND v.local_date BETWEEN $2 AND $3 AND a.locked_at IS NULL`, [only, p.start_ad, p.end_ad]);
      n = r.rowCount ?? 0;
    } else {
      n = await lockAttendance(client, companyId, p.start_ad, p.end_ad);
      await client.query(`UPDATE payroll_period SET attendance_locked_at = coalesce(attendance_locked_at, now()) WHERE id = $1`, [p.id]);
    }
    await client.query(`UPDATE payroll_run SET status = 'locked', locked_at = now() WHERE id = $1`, [runId]);
    if (run.run_type === 'regular') {
      await client.query(`UPDATE ot_slip s SET status = 'posted', posted_payslip_id = ps.id FROM payslip ps
         WHERE ps.run_id = $1 AND ps.employee_id = s.employee_id AND s.local_date BETWEEN $2 AND $3 AND s.status = 'approved'`, [runId, p.start_ad, p.end_ad]);
    }
    const slips = await client.query<{ id: string; employee_id: string; inputs_snapshot: { result: { loanInstallments: Array<{ loanId: string; amount: string }> } } }>('SELECT id, employee_id, inputs_snapshot FROM payslip WHERE run_id = $1', [runId]);
    for (const s of slips.rows) {
      for (const li of s.inputs_snapshot.result.loanInstallments ?? []) {
        await client.query('INSERT INTO loan_installment (loan_id, payslip_id, amount) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', [li.loanId, s.id, li.amount]);
        await client.query(`UPDATE loan SET status = 'closed' WHERE id = $1 AND principal <= (SELECT coalesce(sum(amount), 0) FROM loan_installment WHERE loan_id = $1)`, [li.loanId]);
      }
    }
    await audit(client, { actorId, action: 'lock', table: 'payroll_run', rowId: runId, after: { attendanceLocked: n } });
    await client.query('COMMIT');
    return { attendanceLocked: n };
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
}

export async function advanceRun(db: Db, companyId: string, runId: string, actorId: string, to: 'disbursed' | 'filed'): Promise<void> {
  const run = await getRun(db, companyId, runId);
  const allowed: Record<string, string> = { disbursed: 'locked', filed: 'disbursed' };
  if (run.status !== allowed[to]) throw new ConflictError(`run is ${run.status}; cannot mark ${to}`);
  await db.query(`UPDATE payroll_run SET status = $2, ${to === 'disbursed' ? 'disbursed_at' : 'filed_at'} = now() WHERE id = $1`, [runId, to]);
  await audit(db, { actorId, action: to, table: 'payroll_run', rowId: runId });
}

export async function publishRun(db: Db, companyId: string, runId: string, actorId: string): Promise<number> {
  const run = await getRun(db, companyId, runId);
  if (!run.locked_at) throw new ConflictError('only locked runs can be published');
  const p = await getPeriod(db, companyId, run.period_id);
  const rows = await db.query<{ id: string; employee_id: string }>('UPDATE payslip SET published_at = now() WHERE run_id = $1 AND published_at IS NULL RETURNING id, employee_id', [runId]);
  for (const r of rows.rows) {
    const userId = await userIdForEmployee(db, r.employee_id);
    await notify(db, { companyId, userId, employeeId: r.employee_id, template: 'payslip.published', title: 'Your payslip is ready',
      body: `Your payslip for ${bsMonthName(p.bs_year, p.bs_month)} is ready to view.`, payload: { payslipId: r.id }, channels: ['push'] });
  }
  await audit(db, { actorId, action: 'publish', table: 'payroll_run', rowId: runId, after: { payslips: rows.rowCount } });
  return rows.rowCount ?? 0;
}

// ---------------------------------------------------------------- bonus pool (plan §11.6)
export async function bonusAllocation(db: Queryable, companyId: string, fiscalYearStart: number, netProfit: string): Promise<{ pool: string; byEmployee: Record<string, string>; detail: Array<Record<string, unknown>> }> {
  const { rules } = await resolveRules(db, fiscalYear(fiscalYearStart).endAd);
  const caps = (rules['bonus.caps'] ?? { poolRate: '0.10', monthsCapLow: 8, monthsCapHigh: 6, lowPayThreshold: '39100', minWorkedFraction: '0.5' }) as { poolRate: string; monthsCapLow: number; monthsCapHigh: number; lowPayThreshold: string; minWorkedFraction: string };
  const fy = fiscalYear(fiscalYearStart);
  const pool = D(netProfit).times(caps.poolRate);
  const r = await db.query<{ employee_id: string; emp_code: string; wages: string; months: string; worked: string; scheduled: string }>(
    `SELECT e.id AS employee_id, e.emp_code,
            coalesce((SELECT sum(ps.gross) FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                       WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND p.start_ad BETWEEN $2 AND $3), 0)::text AS wages,
            coalesce((SELECT count(*) FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                       WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND p.start_ad BETWEEN $2 AND $3), 0)::text AS months,
            (SELECT count(*) FROM v_attendance_latest v WHERE v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3 AND v.status IN ('Present', 'HalfDay', 'HalfDayLeave', 'WFH', 'OnLeave'))::text AS worked,
            (SELECT count(*) FROM v_attendance_latest v WHERE v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3 AND v.status NOT IN ('WeeklyOff', 'Holiday', 'OffShift'))::text AS scheduled
       FROM employee e WHERE e.company_id = $1`, [companyId, fy.startAd, fy.endAd]);
  const eligible = r.rows.filter((x) => D(x.wages).gt(0) && (D(x.scheduled).isZero() || D(x.worked).div(x.scheduled).gte(caps.minWorkedFraction)));
  const totalWages = eligible.reduce((a, x) => a.plus(x.wages), D(0));
  const byEmployee: Record<string, string> = {};
  const detail: Array<Record<string, unknown>> = [];
  for (const x of eligible) {
    const monthly = D(x.wages).div(Math.max(1, Number(x.months)));
    const capMonths = monthly.lte(caps.lowPayThreshold) ? caps.monthsCapLow : caps.monthsCapHigh;
    const share = totalWages.isZero() ? D(0) : pool.times(x.wages).div(totalWages);
    const amount = Decimal.min(share, monthly.times(capMonths)).toDecimalPlaces(2);
    byEmployee[x.employee_id] = amount.toFixed(2);
    detail.push({ employeeId: x.employee_id, empCode: x.emp_code, wages: x.wages, share: share.toFixed(2), capMonths, amount: amount.toFixed(2) });
  }
  return { pool: pool.toFixed(2), byEmployee, detail };
}
