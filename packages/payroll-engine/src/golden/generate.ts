/**
 * Regenerates the golden fixtures from named scenarios:  npx tsx src/golden/generate.ts
 * Fixture expectations are the engine's output at generation time; the worked-example tests in compute.test.ts pin the
 * independently verified figures from plan.md. A fixture should only be regenerated after a reviewed rule change.
 */
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { computePayslip } from '../compute.js';
import { baseInput } from '../fixtures.js';
import type { PayrollInput } from '../types.js';

const dir = path.dirname(fileURLToPath(import.meta.url));
const e = baseInput().employee;
const d = baseInput().declaration;
const shrawan = (fy: number) => ({ bsYear: fy, bsMonth: 4, daysInMonth: 31, fiscalYearStart: fy, monthIndexInFy: 1 });
const ashwin = (fy: number) => ({ bsYear: fy, bsMonth: 6, daysInMonth: 31, fiscalYearStart: fy, monthIndexInFy: 3 });

const scenarios: Array<[string, PayrollInput]> = [
  ['01_single_ssf_2082', baseInput({ fy: 2082, declaration: { ...d, lifePremium: '30000' } })],
  ['02_couple_ssf_2082', baseInput({ fy: 2082, employee: { ...e, maritalStatus: 'couple' }, declaration: { ...d, lifePremium: '30000' } })],
  ['03_single_ssf_2083', baseInput({ fy: 2083, declaration: { ...d, lifePremium: '30000' } })],
  ['04_couple_ssf_2083_same_table', baseInput({ fy: 2083, employee: { ...e, maritalStatus: 'couple' } })],
  ['05_pf_regime', baseInput({ employee: { ...e, regime: 'pf' } })],
  ['06_no_regime_sst_payable', baseInput({ employee: { ...e, regime: 'none' } })],
  ['07_female_rebate', baseInput({ base: { BASIC: '100000', ALLOWANCE: '50000' }, employee: { ...e, gender: 'female' } })],
  ['08_mid_month_join', baseInput({ attendance: { paidDays: '22', lopDays: '0', otHours: '0' } })],
  ['09_mid_month_exit_final_settlement', baseInput({ fy: 2082, attendance: { paidDays: '10', lopDays: '0', otHours: '0' }, extras: { isFinalSettlement: true, leaveEncashDays: '30', leaveEncashDivisor: 30 }, ytd: { taxable: '544000', tdsPaid: '10000', retirementEmployee: '35200', retirementEmployer: '64000', festivalPaid: true } })],
  ['10_dashain_month_festival', baseInput({ period: ashwin(2083), extras: { monthsOfServiceInFy: 8 } })],
  ['11_loan_emi', baseInput({ loans: [{ id: 'L1', emi: '5000', remaining: '45000' }] })],
  ['12_overtime_10h', baseInput({ base: { BASIC: '30000' }, attendance: { paidDays: '31', lopDays: '0', otHours: '10' } })],
  ['13_lwp_3_days', baseInput({ attendance: { paidDays: '28', lopDays: '3', otHours: '0' } })],
  ['14_half_day', baseInput({ attendance: { paidDays: '30.5', lopDays: '0.5', otHours: '0' } })],
  ['15_non_resident', baseInput({ employee: { ...e, isResident: false } })],
  ['16_minimum_wage_worker', baseInput({ base: { BASIC: '12170', DA: '7380' } })],
  ['17_officer_no_ot', baseInput({ base: { BASIC: '150000', ALLOWANCE: '50000' }, attendance: { paidDays: '31', lopDays: '0', otHours: '12' }, employee: { ...e, otEligible: false } })],
  ['18_sst_waiver_off_high_income', baseInput({ base: { BASIC: '400000', ALLOWANCE: '100000' }, employee: { ...e, regime: 'none' } })],
  ['19_remote_area_A', baseInput({ base: { BASIC: '80000', ALLOWANCE: '20000' }, employee: { ...e, remoteAreaClass: 'A' } })],
  ['20_disability', baseInput({ base: { BASIC: '80000', ALLOWANCE: '20000' }, employee: { ...e, disability: true } })],
  ['21_cit_and_medical_credit', baseInput({ base: { BASIC: '90000', ALLOWANCE: '30000' }, declaration: { ...d, citMonthly: '10000', medicalCost: '20000' } })],
  ['22_insurance_caps', baseInput({ base: { BASIC: '120000', ALLOWANCE: '30000' }, declaration: { ...d, lifePremium: '90000', healthPremium: '50000' } })],
  ['23_top_band_39pct_2082', baseInput({ fy: 2082, base: { BASIC: '400000', ALLOWANCE: '150000' } })],
  ['24_top_band_29pct_2083', baseInput({ fy: 2083, base: { BASIC: '400000', ALLOWANCE: '150000' } })],
  ['25_fixed_30_proration', baseInput({ period: { bsYear: 2083, bsMonth: 3, daysInMonth: 32, fiscalYearStart: 2082, monthIndexInFy: 12 }, attendance: { paidDays: '30', lopDays: '2', otHours: '0' }, policy: { ...baseInput().policy, prorationBasis: 'fixed_30' }, rules: baseInput({ fy: 2082 }).rules })],
  ['26_bonus_and_arrears_inputs', baseInput({ inputs: [{ code: 'BONUS', amount: '80000' }, { code: 'ARREARS', amount: '12500.50' }] })],
  ['27_ytd_true_up_poush', baseInput({ fy: 2082, period: { bsYear: 2082, bsMonth: 9, daysInMonth: 30, fiscalYearStart: 2082, monthIndexInFy: 6 }, ytd: { taxable: '340000', tdsPaid: '7000', retirementEmployee: '22000', retirementEmployer: '40000', festivalPaid: true }, declaration: { ...d, lifePremium: '30000' } })],
  ['28_zero_attendance', baseInput({ attendance: { paidDays: '0', lopDays: '31', otHours: '0' } })],
];

for (const [name, input] of scenarios) {
  const r = computePayslip(input);
  const expected = { gross: r.gross, taxable: r.taxable, tds: r.tds, employeeStatutory: r.employeeStatutory, employerStatutory: r.employerStatutory, net: r.net,
    annualTax: r.tax.annualTax, lines: r.lines.map((l) => ({ code: l.code, type: l.type, amount: l.amount })), flags: r.flags };
  writeFileSync(path.join(dir, `${name}.json`), JSON.stringify({ name, input, expected }, null, 2) + '\n');
}
console.log(`${scenarios.length} fixtures written`);
