export type Regime = 'ssf' | 'pf' | 'none';
export type ComponentType = 'earning' | 'deduction' | 'employer_contribution' | 'statistical';
export type ProrationBasis = 'bs_calendar_days' | 'fixed_30' | '26_day';

export interface ComponentDef {
  code: string;
  type: ComponentType;
  nameEn: string;
  nameNe?: string | null;
  /** DSL formula; null = amount comes from the assignment base amount, one-time inputs or a built-in rule for that code. */
  formula?: string | null;
  /** DSL condition; when false the component is skipped. */
  condition?: string | null;
  dependsOnPaymentDays: boolean;
  taxable: boolean;
  statutory?: boolean;
  rounding: 'paisa' | 'rupee';
  sortOrder: number;
}

/** Cumulative tax band: taxable income up to `upTo` (null = no ceiling) taxed at `rate`; `sst` marks the social security tax band. */
export interface TaxBand { upTo: string | null; rate: string; sst?: boolean }

export interface TaxSchedule {
  fiscalYearStart: number;
  /** FY 2083/84 onward: one table for everyone. Earlier: 'single' and 'couple'. */
  bands: { all?: TaxBand[]; single?: TaxBand[]; couple?: TaxBand[] };
  nonResidentRate: string;
}

export interface TaxDeductionRules {
  retirementCap: string;          // 500000
  retirementFraction: string;     // 1/3 as "0.333333" or use "3" divisor — we store divisor
  retirementDivisor: string;      // 3
  lifePremiumCap: string;         // 40000
  healthPremiumCap: string;       // 20000
  remoteArea: Record<'A' | 'B' | 'C' | 'D', string>;
  disabilityExtraFraction: string; // 0.5 of the first band ceiling
  medicalCreditRate: string;      // 0.15
  medicalCreditCap: string;       // 1500
  femaleRebate: string;           // 0.10
  /** Income Tax Act Sec 12: donations to approved organisations, up to the lower of this cap and a fraction of adjusted taxable income. */
  donationCap?: string;           // 100000
  donationFraction?: string;      // 0.05
}

/** Rules resolved by the caller as of the pay period (plan §6.13). Keys mirror rule_version.rule_key. */
export interface RuleSet {
  'tax.schedule': TaxSchedule;
  'tax.deductions': TaxDeductionRules;
  'ssf.rates': { employee: string; employer: string; allocation: Record<string, string> };
  'pf.rates': { employee: string; employer: string; gratuity: string };
  'ot.multiplier': { working: string; weekly_off: string; holiday: string };
  'festival.allowance': { months: string; defaultBsMonth: number };
  'min_wage': { basic: string; dearness: string; total: string; daily: string; hourly: string; partTimeHourly: string };
  [key: string]: unknown;
}

export interface PayrollPolicy {
  prorationBasis: ProrationBasis;
  hourlyDivisor: number;
  rounding: 'half_up_rupee' | 'paisa';
  /** Plan §11.4 includes the employer retirement contribution in taxable income (confirm with the CA, Gap G-1). */
  includeEmployerContributionInIncome: boolean;
  fullTime: boolean;
}

export interface EmployeeInput {
  id: string;
  code: string;
  nameEn: string;
  nameNe?: string | null;
  gender: 'male' | 'female' | 'other' | null;
  maritalStatus: 'single' | 'couple' | null;
  isResident: boolean;
  regime: Regime;
  remoteAreaClass: 'A' | 'B' | 'C' | 'D' | null;
  disability: boolean;
  otEligible: boolean;
  /** Month (1-12) chosen for the festival allowance (Sec 37(2)); null = Ashwin. */
  festivalMonth: number | null;
  /** Female rebate applies only when remuneration is the woman's sole income (plan §6.6). Default true. */
  remunerationOnlyIncome?: boolean;
}

export interface PeriodInput {
  bsYear: number;
  bsMonth: number;
  daysInMonth: number;
  fiscalYearStart: number;
  /** 1 = Shrawan ... 12 = Ashadh. */
  monthIndexInFy: number;
}

export interface AttendanceInput {
  /** Days paid in this period (calendar days of the BS month minus unpaid days). */
  paidDays: string;
  lopDays: string;
  otHours: string;
  otDayType?: 'working' | 'weekly_off' | 'holiday';
}

export interface YtdInput {
  taxable: string;
  tdsPaid: string;
  retirementEmployee: string;
  retirementEmployer: string;
  festivalPaid: boolean;
}

export interface DeclarationInput {
  lifePremium: string;
  healthPremium: string;
  citMonthly: string;
  medicalCost: string;
  otherRetirementAnnual: string;
  /** Donations to approved organisations in the fiscal year (Sec 12); optional, 0 when absent. */
  donation?: string;
}

export interface LoanInput { id: string; emi: string; remaining: string; kind?: 'loan' | 'advance' }

export interface ExtrasInput {
  /** Months of service inside this fiscal year, for festival allowance proration (default 12). */
  monthsOfServiceInFy?: number;
  forceFestivalAllowance?: boolean;
  /** Final settlement: encash this many leave days at last basic / divisor. */
  leaveEncashDays?: string;
  leaveEncashDivisor?: number;
  /** Basic at the latest rate when a salary revision was day-weighted this period; encashment uses the last basic, not the blend. */
  lastBasic?: string;
  /** Final settlement: positive = notice pay to employee, negative = deduction in lieu of notice. */
  noticePay?: string;
  isFinalSettlement?: boolean;
  deductFullTaxNow?: boolean;
  /** Loan recovery in full at final settlement. */
  recoverLoansInFull?: boolean;
  /**
   * For one-off runs (supplementary, arrears, bonus) in a period whose regular run is already locked: the regular
   * run's recurring monthly figures, so the annual projection stays correct even though this run has no salary lines.
   */
  recurringOverride?: { taxable: string; retirement: string };
}

export interface PayrollInput {
  employee: EmployeeInput;
  period: PeriodInput;
  components: ComponentDef[];
  baseAmounts: Record<string, string>;
  inputs: Array<{ code: string; amount: string; note?: string }>;
  attendance: AttendanceInput;
  ytd: YtdInput;
  declaration: DeclarationInput;
  loans: LoanInput[];
  rules: RuleSet;
  policy: PayrollPolicy;
  extras?: ExtrasInput;
}

export interface Line {
  code: string;
  type: ComponentType;
  nameEn: string;
  nameNe: string | null;
  amount: string;
  taxable: boolean;
  basis: Record<string, unknown>;
  sortOrder: number;
}

export interface TaxDetail {
  fiscalYearStart: number;
  status: 'single' | 'couple' | 'all' | 'non_resident';
  monthTaxable: string;
  recurringTaxable: string;
  recurringRetirement: string;
  projectedAnnualTaxable: string;
  retirementDeduction: string;
  insuranceDeduction: string;
  remoteAreaDeduction: string;
  disabilityDeduction: string;
  donationDeduction?: string;
  taxableAfterDeductions: string;
  bands: Array<{ upTo: string | null; rate: string; slice: string; tax: string; waived: boolean }>;
  grossTax: string;
  femaleRebate: string;
  medicalCredit: string;
  annualTax: string;
  ytdTdsPaid: string;
  remainingMonths: number;
  tdsThisMonth: string;
}

export interface PayrollResult {
  lines: Line[];
  gross: string;
  taxable: string;
  tds: string;
  employeeStatutory: string;
  employerStatutory: string;
  totalDeductions: string;
  net: string;
  paidDays: string;
  lopDays: string;
  otHours: string;
  payableFraction: string;
  tax: TaxDetail;
  ssfAllocation: Record<string, string> | null;
  loanInstallments: Array<{ loanId: string; amount: string }>;
  explain: Array<{ code: string; text: string }>;
  flags: string[];
}
