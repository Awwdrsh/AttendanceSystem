import { Decimal, D, ZERO, rupee, paisa, max0, sum } from './decimal.js';
import { evaluateFormula, formulaReferences, FormulaError, type EvalEnv, type Value } from './formula.js';
import { computeTds } from './tax.js';
import type { ComponentDef, Line, PayrollInput, PayrollResult, ProrationBasis } from './types.js';

/** Codes the engine computes itself when the component has no formula (plan §11.1 graph). */
const BUILTIN = new Set(['OT', 'FESTIVAL', 'LEAVE_ENCASH', 'SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT', 'GROSS', 'TAXABLE', 'TDS', 'LOAN']);
/** Earnings that do not recur every month (not projected for TDS). */
const ONE_OFF = new Set(['OT', 'FESTIVAL', 'BONUS', 'ARREARS', 'LEAVE_ENCASH', 'NOTICE_PAY', 'ABSENCE', 'ADVANCE']);
const STATUTORY_PHASE = new Set(['SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT']);

export class PayrollError extends Error {}

/** One below the numeric(14,2) ceiling of payslip lines. */
export const MAX_LINE_AMOUNT = new Decimal('1e12');

/** Payable fraction of the month (plan §11.3). unpaid = days in month − paid days; basis days = 29–32 | 30 | 26. */
export function payableFraction(paidDays: Decimal, daysInMonth: number, basis: ProrationBasis): Decimal {
  const basisDays = basis === 'fixed_30' ? 30 : basis === '26_day' ? 26 : daysInMonth;
  const unpaid = D(daysInMonth).minus(paidDays);
  return Decimal.max(0, Decimal.min(1, D(basisDays).minus(unpaid).div(basisDays)));
}

function phaseOf(c: ComponentDef): number {
  if (c.type === 'earning') return 1;
  if (STATUTORY_PHASE.has(c.code) || c.type === 'employer_contribution') return 2;
  if (c.type === 'statistical') return 3;
  if (c.code === 'TDS') return 4;
  if (c.code === 'LOAN') return 6;
  return 5;
}

/** Topological order inside one phase by formula references (cycle = error); stable by sortOrder otherwise. */
function order(components: ComponentDef[]): ComponentDef[] {
  const byCode = new Map(components.map((c) => [c.code, c]));
  const deps = new Map<string, string[]>();
  for (const c of components) {
    const refs = c.formula ? [...formulaReferences(c.formula)] : [];
    const cond = c.condition ? [...formulaReferences(c.condition)] : [];
    deps.set(c.code, [...refs, ...cond].filter((r) => byCode.has(r) && r !== c.code));
  }
  const out: ComponentDef[] = []; const state = new Map<string, 1 | 2>();
  const visit = (code: string, chain: string[]): void => {
    const s = state.get(code);
    if (s === 2) return;
    if (s === 1) throw new PayrollError(`formula cycle: ${[...chain, code].join(' -> ')}`);
    state.set(code, 1);
    for (const d of deps.get(code) ?? []) visit(d, [...chain, code]);
    state.set(code, 2);
    out.push(byCode.get(code)!);
  };
  for (const c of [...components].sort((a, b) => a.sortOrder - b.sortOrder)) visit(c.code, []);
  return out;
}

function ruleLookup(rules: PayrollInput['rules']): EvalEnv['rule'] {
  return (key, path) => {
    let v: unknown = rules[key];
    if (v === undefined) throw new FormulaError(`unknown rule ${key}`);
    if (path) for (const seg of path.split('.')) { v = (v as Record<string, unknown>)?.[seg]; if (v === undefined) throw new FormulaError(`rule ${key} has no ${path}`); }
    if (typeof v === 'number') return new Decimal(v);
    if (typeof v === 'string') { try { const d = new Decimal(v); return d.isNaN() ? v : d; } catch { return v; } }
    if (typeof v === 'boolean') return v;
    throw new FormulaError(`rule ${key}${path ? '.' + path : ''} is not a value`);
  };
}

export function computePayslip(input: PayrollInput): PayrollResult {
  const { employee, period, policy, rules, attendance, ytd, declaration } = input;
  const extras = input.extras ?? {};
  const flags: string[] = [];
  const explain: Array<{ code: string; text: string }> = [];
  const lines: Line[] = [];
  const amounts = new Map<string, Decimal>();
  const fromInputs = new Set<string>();

  const paidDays = D(attendance.paidDays);
  const lopDays = D(attendance.lopDays);
  const otHours = D(attendance.otHours);
  const fraction = payableFraction(paidDays, period.daysInMonth, policy.prorationBasis);
  const base = (code: string): Decimal => D(input.baseAmounts[code] ?? 0);
  const basicFull = base('BASIC');
  const inputsFor = (code: string): Decimal => sum(input.inputs.filter((i) => i.code === code).map((i) => D(i.amount)));

  const vars: Record<string, Value> = {
    days_in_bs_month: D(period.daysInMonth), paid_days: paidDays, lop_days: lopDays, hourly_divisor: D(policy.hourlyDivisor),
    ot_hours: otHours, months_in_service: D(extras.monthsOfServiceInFy ?? 12), is_female: employee.gender === 'female',
    regime: employee.regime, bs_month: D(period.bsMonth), fiscal_year: D(period.fiscalYearStart),
  };
  const env: EvalEnv = { vars, rule: ruleLookup(rules) };
  const evalNum = (src: string, code: string): Decimal => {
    const v = evaluateFormula(src, env);
    if (!(v instanceof Decimal)) throw new PayrollError(`${code}: formula must produce a number`);
    if (!v.isFinite()) throw new PayrollError(`${code}: formula produced a non-finite amount`);
    return v;
  };
  /** Pre-proration amount of every evaluated component (zero amounts included), for the recurring-income projection. */
  const unprorated = new Map<string, Decimal>();
  const roundLine = (c: ComponentDef, v: Decimal): Decimal =>
    c.rounding === 'rupee' || (policy.rounding === 'half_up_rupee' && (c.statutory || c.code === 'TDS')) ? rupee(v) : paisa(v);

  const push = (c: ComponentDef, amount: Decimal, basis: Record<string, unknown>, text?: string): void => {
    amounts.set(c.code, amount); vars[c.code] = amount;
    if (amount.isZero() && c.type !== 'statistical') return;
    lines.push({ code: c.code, type: c.type, nameEn: c.nameEn, nameNe: c.nameNe ?? null, amount: amount.toFixed(2), taxable: c.taxable, basis, sortOrder: c.sortOrder });
    if (text) explain.push({ code: c.code, text });
  };

  const ssf = rules['ssf.rates']; const pf = rules['pf.rates'];
  const festivalMonth = employee.festivalMonth ?? rules['festival.allowance'].defaultBsMonth;
  const monthIndexOf = (bsMonth: number): number => ((bsMonth - 4 + 12) % 12) + 1; // Shrawan = 1
  const loanInstallments: Array<{ loanId: string; amount: string }> = [];
  let tax: PayrollResult['tax'] | null = null;

  const builtin = (c: ComponentDef): { amount: Decimal; basis: Record<string, unknown>; text: string } | null => {
    const basic = amounts.get('BASIC') ?? ZERO;
    switch (c.code) {
      case 'OT': {
        if (!employee.otEligible || otHours.isZero()) return null;
        const mult = D(rules['ot.multiplier'][attendance.otDayType ?? 'working']);
        const hourly = basicFull.div(policy.hourlyDivisor);
        const amt = rupee(otHours.times(hourly).times(mult));
        return { amount: amt, basis: { hours: otHours.toFixed(2), hourlyBasic: paisa(hourly).toFixed(2), multiplier: mult.toFixed(2), divisor: policy.hourlyDivisor },
          text: `${otHours.toFixed(2)} h × (basic ${basicFull.toFixed(2)} ÷ ${policy.hourlyDivisor}) × ${mult.toFixed(2)} = ${amt.toFixed(2)} (Labour Act Sec 31(1))` };
      }
      case 'FESTIVAL': {
        const due = extras.forceFestivalAllowance || (period.bsMonth === festivalMonth && !ytd.festivalPaid);
        if (!due) return null;
        const months = Math.min(extras.monthsOfServiceInFy ?? 12, 12);
        const amt = paisa(basicFull.times(D(rules['festival.allowance'].months)).times(months).div(12));
        return { amount: amt, basis: { basic: basicFull.toFixed(2), monthsOfService: months }, text: `basic ${basicFull.toFixed(2)} × ${months}/12 = ${amt.toFixed(2)} (Sec 37, pro-rated)` };
      }
      case 'LEAVE_ENCASH': {
        const days = D(extras.leaveEncashDays ?? 0);
        if (days.isZero()) return null;
        const divisor = extras.leaveEncashDivisor ?? period.daysInMonth;
        const last = extras.lastBasic !== undefined ? D(extras.lastBasic) : basicFull;
        const amt = paisa(days.times(last).div(divisor));
        return { amount: amt, basis: { days: days.toFixed(2), lastBasic: last.toFixed(2), divisor }, text: `${days.toFixed(2)} days × last basic ${last.toFixed(2)} ÷ ${divisor} = ${amt.toFixed(2)} (Sec 49(2))` };
      }
      case 'SSF_EE': return employee.regime === 'ssf' ? { amount: basic.times(D(ssf.employee)), basis: { basic: basic.toFixed(2), rate: ssf.employee }, text: `${D(ssf.employee).times(100)}% of basic ${basic.toFixed(2)} (CBSS Act Sec 4)` } : null;
      case 'SSF_ER': return employee.regime === 'ssf' ? { amount: basic.times(D(ssf.employer)), basis: { basic: basic.toFixed(2), rate: ssf.employer }, text: `${D(ssf.employer).times(100)}% of basic ${basic.toFixed(2)} employer share (CBSS Act Sec 4)` } : null;
      case 'PF_EE': return employee.regime === 'pf' ? { amount: basic.times(D(pf.employee)), basis: { basic: basic.toFixed(2), rate: pf.employee }, text: `${D(pf.employee).times(100)}% of basic (Labour Act Sec 52(1))` } : null;
      case 'PF_ER': return employee.regime === 'pf' ? { amount: basic.times(D(pf.employer)), basis: { basic: basic.toFixed(2), rate: pf.employer }, text: `${D(pf.employer).times(100)}% of basic employer share (Sec 52(1))` } : null;
      case 'GRATUITY': return employee.regime === 'pf' ? { amount: basic.times(D(pf.gratuity)), basis: { basic: basic.toFixed(2), rate: pf.gratuity }, text: `${D(pf.gratuity).times(100)}% of basic gratuity accrual (Sec 53)` } : null;
      case 'CIT': { const v = D(declaration.citMonthly); return v.isZero() ? null : { amount: v, basis: { declared: v.toFixed(2) }, text: 'citizen investment trust as declared' }; }
      case 'GROSS': { const g = sum(lines.filter((l) => l.type === 'earning').map((l) => l.amount)); return { amount: g, basis: {}, text: 'sum of earnings' }; }
      case 'TAXABLE': {
        const t = sum(lines.filter((l) => (l.type === 'earning' && l.taxable) || (policy.includeEmployerContributionInIncome && l.type === 'employer_contribution' && l.taxable)).map((l) => l.amount));
        return { amount: t, basis: { includesEmployerContribution: policy.includeEmployerContributionInIncome }, text: 'taxable earnings' + (policy.includeEmployerContributionInIncome ? ' + employer retirement contribution' : '') };
      }
      case 'TDS': {
        const monthTaxable = amounts.get('TAXABLE') ?? ZERO;
        // Recurring monthly income for the annual projection (plan §11.4): the full-month (pre-proration) amount of
        // every recurring taxable earning, less any one-time input part, plus the employer retirement contribution on
        // the full basic when policy counts it as income. A formula that references an already prorated line stays
        // prorated; the true-up in later months corrects any residual.
        const recurringEarnings = sum(input.components
          .filter((c) => c.type === 'earning' && c.taxable && !ONE_OFF.has(c.code))
          .map((c) => max0((unprorated.get(c.code) ?? ZERO).minus(inputsFor(c.code)))));
        const employerRecurring = !policy.includeEmployerContributionInIncome ? ZERO
          : employee.regime === 'ssf' ? basicFull.times(D(ssf.employer))
          : employee.regime === 'pf' ? basicFull.times(D(pf.employer).plus(D(pf.gratuity))) : ZERO;
        const employeeRecurring = employee.regime === 'ssf' ? basicFull.times(D(ssf.employee)) : employee.regime === 'pf' ? basicFull.times(D(pf.employee)) : ZERO;
        const recurring = extras.recurringOverride ? D(extras.recurringOverride.taxable) : recurringEarnings.plus(employerRecurring);
        const retirement = ['SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT'].reduce((a, k) => a.plus(amounts.get(k) ?? ZERO), ZERO);
        const recurringRetirement = extras.recurringOverride ? D(extras.recurringOverride.retirement) : employeeRecurring.plus(employerRecurring).plus(D(declaration.citMonthly));
        const festivalLater = !ytd.festivalPaid && !(amounts.get('FESTIVAL') ?? ZERO).gt(0) && monthIndexOf(festivalMonth) > period.monthIndexInFy && !extras.isFinalSettlement
          ? basicFull.times(D(rules['festival.allowance'].months)) : ZERO;
        tax = computeTds({
          schedule: rules['tax.schedule'], deductions: rules['tax.deductions'], regime: employee.regime, isResident: employee.isResident, gender: employee.gender,
          maritalStatus: employee.maritalStatus, remoteAreaClass: employee.remoteAreaClass, disability: employee.disability, remunerationOnlyIncome: employee.remunerationOnlyIncome ?? true,
          monthTaxable, recurringTaxable: recurring, futureOneOffs: festivalLater, ytdTaxable: D(ytd.taxable), ytdTdsPaid: D(ytd.tdsPaid),
          monthRetirement: retirement, recurringRetirement, ytdRetirement: D(ytd.retirementEmployee).plus(D(ytd.retirementEmployer)),
          otherRetirementAnnual: D(declaration.otherRetirementAnnual), lifePremium: D(declaration.lifePremium), healthPremium: D(declaration.healthPremium),
          medicalCost: D(declaration.medicalCost), donation: D(declaration.donation ?? 0), monthIndexInFy: period.monthIndexInFy, deductFullTaxNow: Boolean(extras.deductFullTaxNow), isFinalSettlement: Boolean(extras.isFinalSettlement),
        });
        const amt = D(tax.tdsThisMonth);
        return { amount: amt, basis: { annualTax: tax.annualTax, projectedTaxable: tax.projectedAnnualTaxable, remainingMonths: tax.remainingMonths, status: tax.status },
          text: `annual tax ${tax.annualTax} on projected taxable ${tax.taxableAfterDeductions} (FY ${tax.fiscalYearStart}/${String(tax.fiscalYearStart + 1).slice(-2)}), less YTD TDS ${tax.ytdTdsPaid}, over ${tax.remainingMonths} month(s) (Income Tax Act Sec 87)` };
      }
      case 'LOAN': {
        if (input.loans.length === 0) return null;
        const gross = amounts.get('GROSS') ?? sum(lines.filter((l) => l.type === 'earning').map((l) => l.amount));
        const otherDeductions = sum(lines.filter((l) => l.type === 'deduction').map((l) => l.amount));
        let available = max0(gross.minus(otherDeductions));
        let total = ZERO;
        for (const loan of input.loans) {
          const due = extras.recoverLoansInFull ? D(loan.remaining) : Decimal.min(D(loan.emi), D(loan.remaining));
          const take = Decimal.min(due, available);
          if (take.lt(due)) flags.push('LoanCapped');
          if (take.gt(0)) { loanInstallments.push({ loanId: loan.id, amount: paisa(take).toFixed(2) }); total = total.plus(take); available = available.minus(take); }
        }
        return total.isZero() ? null : { amount: total, basis: { installments: loanInstallments }, text: `loan/advance instalments (Sec 38(i))` };
      }
    }
    return null;
  };

  // Evaluate the component graph phase by phase (plan §11.1 order).
  const phases = new Map<number, ComponentDef[]>();
  for (const c of input.components) { const p = phaseOf(c); phases.set(p, [...(phases.get(p) ?? []), c]); }
  for (const p of [1, 2, 3, 4, 5, 6]) {
    for (const c of order(phases.get(p) ?? [])) {
      if (c.condition) {
        const ok = evaluateFormula(c.condition, env);
        if (typeof ok !== 'boolean') throw new PayrollError(`${c.code}: condition must be true/false`);
        if (!ok) { vars[c.code] = ZERO; continue; }
      }
      let amount: Decimal; let basis: Record<string, unknown> = {}; let text: string | undefined;
      if (c.formula) {
        amount = evalNum(c.formula, c.code); basis = { formula: c.formula }; text = `${c.formula} = ${paisa(amount).toFixed(2)}`;
      } else if (BUILTIN.has(c.code)) {
        const b = builtin(c);
        if (!b) { vars[c.code] = ZERO; amounts.set(c.code, ZERO); continue; }
        amount = b.amount; basis = b.basis; text = b.text;
      } else {
        const b = base(c.code); const i = inputsFor(c.code);
        amount = b.plus(i);
        if (!i.isZero()) fromInputs.add(c.code);
        basis = { base: b.toFixed(2), inputs: i.toFixed(2) };
        text = i.isZero() ? `${c.nameEn} ${b.toFixed(2)}` : `${c.nameEn} ${b.toFixed(2)} + one-time ${i.toFixed(2)}`;
      }
      unprorated.set(c.code, amount);
      if (c.dependsOnPaymentDays && !fraction.eq(1)) {
        const before = amount;
        amount = amount.times(fraction);
        basis = { ...basis, prorated: { from: before.toFixed(2), paidDays: paidDays.toFixed(2), daysInMonth: period.daysInMonth, basis: policy.prorationBasis } };
        text = `${before.toFixed(2)} × ${paidDays.toFixed(2)}/${policy.prorationBasis === 'bs_calendar_days' ? period.daysInMonth : policy.prorationBasis === 'fixed_30' ? 30 : 26} paid days = ${paisa(amount.times(1)).toFixed(2)}`;
      }
      if (amount.isNegative() && c.type !== 'statistical') { flags.push(`NegativeAmount:${c.code}`); amount = ZERO; }
      if (!amount.isFinite()) throw new PayrollError(`${c.code}: amount is not finite`);
      // Payslip amounts are stored as numeric(14,2); anything larger is a formula or input mistake, not a salary.
      if (amount.abs().gte(MAX_LINE_AMOUNT)) throw new PayrollError(`${c.code}: amount ${amount.toFixed(2)} is too large (limit ${MAX_LINE_AMOUNT.toFixed(0)})`);
      amount = roundLine(c, amount);
      // Withholding cannot exceed the pay it is withheld from: TDS (Sec 87 applies to amounts paid) and the Sec 38
      // (c)-(i) deductions are capped at the pay remaining after the statutory shares, so net never goes below zero.
      // A capped TDS is caught by the true-up in later months (only what was actually withheld counts as YTD TDS).
      if (c.type === 'deduction' && p >= 4 && c.code !== 'LOAN') {
        const available = max0(sum(lines.filter((l) => l.type === 'earning').map((l) => l.amount)).minus(sum(lines.filter((l) => l.type === 'deduction').map((l) => l.amount))));
        if (amount.gt(available)) { flags.push(c.code === 'TDS' ? 'TdsCapped' : `DeductionCapped:${c.code}`); basis = { ...basis, capped: { requested: amount.toFixed(2), available: available.toFixed(2) } }; amount = available; }
      }
      push(c, amount, basis, text);
    }
  }

  // Totals (plan §11.7): gross and total deductions exclude the rounding adjustment; the ROUNDING line only exists so
  // that Σ earning lines − Σ deduction lines equals the whole-rupee net.
  const grossTotal = sum(lines.filter((l) => l.type === 'earning').map((l) => l.amount));
  const deductionTotal = sum(lines.filter((l) => l.type === 'deduction').map((l) => l.amount));
  const exact = grossTotal.minus(deductionTotal);
  const net = policy.rounding === 'half_up_rupee' ? rupee(exact) : exact;
  if (!net.eq(exact)) {
    const diff = net.minus(exact);
    lines.push({ code: 'ROUNDING', type: diff.gt(0) ? 'earning' : 'deduction', nameEn: 'Rounding', nameNe: 'पूर्णाङ्कन', amount: diff.abs().toFixed(2), taxable: false, basis: { exactNet: exact.toFixed(2) }, sortOrder: 9999 });
  }
  if (net.isNegative()) flags.push('NegativeNet');

  const min = rules['min_wage'];
  if (policy.fullTime && basicFull.gt(0) && (basicFull.lt(D(min.basic)) || basicFull.plus(base('DA')).lt(D(min.total)))) flags.push('BelowMinimumWage');

  const ssfAllocation = employee.regime === 'ssf' && amounts.has('SSF_ER')
    ? Object.fromEntries(Object.entries(ssf.allocation).map(([k, r]) => [k, paisa((amounts.get('BASIC') ?? ZERO).times(D(r))).toFixed(2)])) : null;

  const employeeStatutory = (amounts.get('SSF_EE') ?? ZERO).plus(amounts.get('PF_EE') ?? ZERO);
  const employerStatutory = (amounts.get('SSF_ER') ?? ZERO).plus(amounts.get('PF_ER') ?? ZERO).plus(amounts.get('GRATUITY') ?? ZERO);

  if (!tax) {
    tax = computeTds({ schedule: rules['tax.schedule'], deductions: rules['tax.deductions'], regime: employee.regime, isResident: employee.isResident, gender: employee.gender,
      maritalStatus: employee.maritalStatus, remoteAreaClass: employee.remoteAreaClass, disability: employee.disability, remunerationOnlyIncome: employee.remunerationOnlyIncome ?? true, monthTaxable: ZERO, recurringTaxable: ZERO, futureOneOffs: ZERO,
      ytdTaxable: ZERO, ytdTdsPaid: ZERO, monthRetirement: ZERO, recurringRetirement: ZERO, ytdRetirement: ZERO, otherRetirementAnnual: ZERO, lifePremium: ZERO, healthPremium: ZERO,
      medicalCost: ZERO, monthIndexInFy: period.monthIndexInFy, deductFullTaxNow: false, isFinalSettlement: false });
  }

  lines.sort((a, b) => a.sortOrder - b.sortOrder || a.code.localeCompare(b.code));
  return {
    lines, gross: paisa(grossTotal).toFixed(2), taxable: paisa(amounts.get('TAXABLE') ?? ZERO).toFixed(2), tds: paisa(amounts.get('TDS') ?? ZERO).toFixed(2),
    employeeStatutory: paisa(employeeStatutory).toFixed(2), employerStatutory: paisa(employerStatutory).toFixed(2), totalDeductions: paisa(deductionTotal).toFixed(2),
    net: paisa(net).toFixed(2), paidDays: paidDays.toFixed(2), lopDays: lopDays.toFixed(2), otHours: otHours.toFixed(2), payableFraction: fraction.toFixed(6),
    tax, ssfAllocation, loanInstallments, explain, flags: [...new Set(flags)],
  };
}
