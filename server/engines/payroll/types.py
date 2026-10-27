"""Engine input and output shapes (port of src/types.ts).

Keys stay camelCase, exactly as in the TS engine and the golden JSON fixtures, so inputs can be passed straight from
JSON and results compared field for field with the TS output. Money is a decimal string ('40000', '4400.00').
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

Regime = Literal['ssf', 'pf', 'none']
ComponentType = Literal['earning', 'deduction', 'employer_contribution', 'statistical']
ProrationBasis = Literal['bs_calendar_days', 'fixed_30', '26_day']
Gender = Literal['male', 'female', 'other']
MaritalStatus = Literal['single', 'couple']
RemoteArea = Literal['A', 'B', 'C', 'D']


class ComponentDef(TypedDict):
    code: str
    type: ComponentType
    nameEn: str
    nameNe: NotRequired[str | None]
    # DSL formula; None = amount comes from the assignment base amount, one-time inputs or a built-in rule for that code.
    formula: NotRequired[str | None]
    # DSL condition; when false the component is skipped.
    condition: NotRequired[str | None]
    dependsOnPaymentDays: bool
    taxable: bool
    statutory: NotRequired[bool]
    rounding: Literal['paisa', 'rupee']
    sortOrder: int


class TaxBand(TypedDict):
    """Cumulative band: taxable income up to upTo (None = no ceiling) taxed at rate; sst marks the social security tax band."""
    upTo: str | None
    rate: str
    sst: NotRequired[bool]


class TaxBands(TypedDict, total=False):
    # FY 2083/84 onward: one table for everyone ('all'). Earlier: 'single' and 'couple'.
    all: list[TaxBand]
    single: list[TaxBand]
    couple: list[TaxBand]


class TaxSchedule(TypedDict):
    fiscalYearStart: int
    bands: TaxBands
    nonResidentRate: str


class TaxDeductionRules(TypedDict):
    retirementCap: str
    retirementFraction: str
    retirementDivisor: str
    lifePremiumCap: str
    healthPremiumCap: str
    remoteArea: dict[str, str]
    disabilityExtraFraction: str
    medicalCreditRate: str
    medicalCreditCap: str
    femaleRebate: str
    # Income Tax Act Sec 12: donations to approved organisations, up to the lower of the cap and a fraction of
    # adjusted taxable income.
    donationCap: NotRequired[str]
    donationFraction: NotRequired[str]


# Rules resolved by the caller as of the pay period (plan §6.13). Keys mirror rule_version.rule_key; extra keys are
# allowed (formulas read them with rule()), so this is a plain dict in practice.
RuleSet = TypedDict('RuleSet', {
    'tax.schedule': TaxSchedule,
    'tax.deductions': TaxDeductionRules,
    'ssf.rates': dict[str, Any],
    'pf.rates': dict[str, str],
    'ot.multiplier': dict[str, str],
    'festival.allowance': dict[str, Any],
    'min_wage': dict[str, str],
})


class PayrollPolicy(TypedDict):
    prorationBasis: ProrationBasis
    hourlyDivisor: int
    rounding: Literal['half_up_rupee', 'paisa']
    # Plan §11.4 includes the employer retirement contribution in taxable income (confirm with the CA, Gap G-1).
    includeEmployerContributionInIncome: bool
    fullTime: bool


class EmployeeInput(TypedDict):
    id: str
    code: str
    nameEn: str
    nameNe: NotRequired[str | None]
    gender: Gender | None
    maritalStatus: MaritalStatus | None
    isResident: bool
    regime: Regime
    remoteAreaClass: RemoteArea | None
    disability: bool
    otEligible: bool
    # Month (1-12) chosen for the festival allowance (Sec 37(2)); None = Ashwin.
    festivalMonth: int | None
    # Female rebate applies only when remuneration is the woman's sole income (plan §6.6). Default True.
    remunerationOnlyIncome: NotRequired[bool]


class PeriodInput(TypedDict):
    bsYear: int
    bsMonth: int
    daysInMonth: int
    fiscalYearStart: int
    # 1 = Shrawan ... 12 = Ashadh.
    monthIndexInFy: int


class AttendanceInput(TypedDict):
    # Days paid in this period (calendar days of the BS month minus unpaid days).
    paidDays: str
    lopDays: str
    otHours: str
    otDayType: NotRequired[Literal['working', 'weekly_off', 'holiday']]


class YtdInput(TypedDict):
    taxable: str
    tdsPaid: str
    retirementEmployee: str
    retirementEmployer: str
    festivalPaid: bool


class DeclarationInput(TypedDict):
    lifePremium: str
    healthPremium: str
    citMonthly: str
    medicalCost: str
    otherRetirementAnnual: str
    # Donations to approved organisations in the fiscal year (Sec 12); 0 when absent.
    donation: NotRequired[str]


class LoanInput(TypedDict):
    id: str
    emi: str
    remaining: str
    kind: NotRequired[Literal['loan', 'advance']]


class RecurringOverride(TypedDict):
    taxable: str
    retirement: str


class ExtrasInput(TypedDict, total=False):
    # Months of service inside this fiscal year, for festival allowance proration (default 12).
    monthsOfServiceInFy: int
    forceFestivalAllowance: bool
    # Final settlement: encash this many leave days at last basic / divisor.
    leaveEncashDays: str
    leaveEncashDivisor: int
    # Basic at the latest rate when a salary revision was day-weighted this period; encashment uses the last basic.
    lastBasic: str
    # Final settlement: positive = notice pay to employee, negative = deduction in lieu of notice.
    noticePay: str
    isFinalSettlement: bool
    deductFullTaxNow: bool
    # Loan recovery in full at final settlement.
    recoverLoansInFull: bool
    # For one-off runs (supplementary, arrears, bonus) in a period whose regular run is already locked: the regular
    # run's recurring monthly figures, so the annual projection stays correct without salary lines.
    recurringOverride: RecurringOverride


class OneTimeInput(TypedDict):
    code: str
    amount: str
    note: NotRequired[str]


class PayrollInput(TypedDict):
    employee: EmployeeInput
    period: PeriodInput
    components: list[ComponentDef]
    baseAmounts: dict[str, str]
    inputs: list[OneTimeInput]
    attendance: AttendanceInput
    ytd: YtdInput
    declaration: DeclarationInput
    loans: list[LoanInput]
    rules: RuleSet
    policy: PayrollPolicy
    extras: NotRequired[ExtrasInput]


class Line(TypedDict):
    code: str
    type: ComponentType
    nameEn: str
    nameNe: str | None
    amount: str
    taxable: bool
    basis: dict[str, Any]
    sortOrder: int


class AppliedBand(TypedDict):
    upTo: str | None
    rate: str
    slice: str
    tax: str
    waived: bool


class TaxDetail(TypedDict):
    fiscalYearStart: int
    status: Literal['single', 'couple', 'all', 'non_resident']
    monthTaxable: str
    recurringTaxable: str
    recurringRetirement: str
    projectedAnnualTaxable: str
    retirementDeduction: str
    insuranceDeduction: str
    remoteAreaDeduction: str
    disabilityDeduction: str
    donationDeduction: NotRequired[str]
    taxableAfterDeductions: str
    bands: list[AppliedBand]
    grossTax: str
    femaleRebate: str
    medicalCredit: str
    annualTax: str
    ytdTdsPaid: str
    remainingMonths: int
    tdsThisMonth: str


class LoanInstallment(TypedDict):
    loanId: str
    amount: str


class Explain(TypedDict):
    code: str
    text: str


class PayrollResult(TypedDict):
    lines: list[Line]
    gross: str
    taxable: str
    tds: str
    employeeStatutory: str
    employerStatutory: str
    totalDeductions: str
    net: str
    paidDays: str
    lopDays: str
    otHours: str
    payableFraction: str
    tax: TaxDetail
    ssfAllocation: dict[str, str] | None
    loanInstallments: list[LoanInstallment]
    explain: list[Explain]
    flags: list[str]
