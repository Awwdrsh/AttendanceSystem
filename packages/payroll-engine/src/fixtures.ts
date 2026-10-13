import { DEFAULT_COMPONENTS, rulesForFy } from './defaults.js';
import type { PayrollInput } from './types.js';

/** Shared builder for tests and golden fixtures. Defaults: Shrawan 2083 (31 days), SSF, single male, full month. */
export function baseInput(over: Partial<PayrollInput> & { base?: Record<string, string>; fy?: number } = {}): PayrollInput {
  const fy = over.fy ?? 2083;
  const period = over.period ?? { bsYear: fy, bsMonth: 4, daysInMonth: 31, fiscalYearStart: fy, monthIndexInFy: 1 };
  return {
    employee: { id: 'E1', code: 'E001', nameEn: 'Test Employee', nameNe: 'परीक्षण', gender: 'male', maritalStatus: 'single', isResident: true, regime: 'ssf',
      remoteAreaClass: null, disability: false, otEligible: true, festivalMonth: null, ...(over.employee ?? {}) },
    period,
    components: over.components ?? DEFAULT_COMPONENTS,
    baseAmounts: over.base ?? over.baseAmounts ?? { BASIC: '40000', ALLOWANCE: '20000' },
    inputs: over.inputs ?? [],
    attendance: { paidDays: String(period.daysInMonth), lopDays: '0', otHours: '0', ...(over.attendance ?? {}) },
    ytd: { taxable: '0', tdsPaid: '0', retirementEmployee: '0', retirementEmployer: '0', festivalPaid: false, ...(over.ytd ?? {}) },
    declaration: { lifePremium: '0', healthPremium: '0', citMonthly: '0', medicalCost: '0', otherRetirementAnnual: '0', ...(over.declaration ?? {}) },
    loans: over.loans ?? [],
    rules: over.rules ?? rulesForFy(fy),
    policy: { prorationBasis: 'bs_calendar_days', hourlyDivisor: 208, rounding: 'half_up_rupee', includeEmployerContributionInIncome: true, fullTime: true, ...(over.policy ?? {}) },
    ...(over.extras ? { extras: over.extras } : {}),
  };
}
