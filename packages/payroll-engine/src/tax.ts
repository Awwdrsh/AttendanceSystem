import { Decimal, D, ZERO, rupee, paisa, max0 } from './decimal.js';
import type { Regime, TaxDeductionRules, TaxDetail, TaxSchedule } from './types.js';

export interface TaxInput {
  schedule: TaxSchedule;
  deductions: TaxDeductionRules;
  regime: Regime;
  isResident: boolean;
  gender: 'male' | 'female' | 'other' | null;
  maritalStatus: 'single' | 'couple' | null;
  /** Remuneration is the only income (female rebate condition, plan §6.6). */
  remunerationOnlyIncome: boolean;
  remoteAreaClass: 'A' | 'B' | 'C' | 'D' | null;
  disability: boolean;
  /** Taxable pay in this month (earnings + employer retirement contribution if policy includes it). */
  monthTaxable: Decimal;
  /** Part of monthTaxable that recurs every month (excludes OT, festival, bonus, arrears, encashment). */
  recurringTaxable: Decimal;
  /** One-offs known for later months in the FY (e.g. festival allowance not yet paid). */
  futureOneOffs: Decimal;
  ytdTaxable: Decimal;
  ytdTdsPaid: Decimal;
  /** Retirement contributions (employee + employer + CIT) this month and already paid this FY. */
  monthRetirement: Decimal;
  recurringRetirement: Decimal;
  ytdRetirement: Decimal;
  otherRetirementAnnual: Decimal;
  lifePremium: Decimal;
  healthPremium: Decimal;
  medicalCost: Decimal;
  /** Donations to approved organisations this fiscal year (Sec 12). */
  donation?: Decimal;
  monthIndexInFy: number;
  deductFullTaxNow: boolean;
  isFinalSettlement: boolean;
}

/** Plan §11.4: project the year, apply the FY schedule, true up against YTD, spread over remaining months. */
export function computeTds(t: TaxInput): TaxDetail {
  const remainingMonths = t.isFinalSettlement ? 1 : Math.max(1, 12 - t.monthIndexInFy + 1);
  const futureMonths = t.isFinalSettlement ? 0 : remainingMonths - 1;
  const projected = t.ytdTaxable.plus(t.monthTaxable).plus(t.recurringTaxable.times(futureMonths)).plus(t.futureOneOffs);
  const annualRetirement = t.ytdRetirement.plus(t.monthRetirement).plus(t.recurringRetirement.times(futureMonths)).plus(t.otherRetirementAnnual);

  const d = t.deductions;
  const retirementDeduction = t.regime === 'none' && annualRetirement.isZero() ? ZERO
    : Decimal.min(annualRetirement, D(d.retirementCap), projected.div(D(d.retirementDivisor)));
  const insurance = Decimal.min(t.lifePremium, D(d.lifePremiumCap)).plus(Decimal.min(t.healthPremium, D(d.healthPremiumCap)));
  const remote = t.remoteAreaClass ? D(d.remoteArea[t.remoteAreaClass]) : ZERO;

  let status: TaxDetail['status'];
  let bands: TaxSchedule['bands'][keyof TaxSchedule['bands']];
  if (!t.isResident) { status = 'non_resident'; bands = []; }
  else if (t.schedule.bands.all) { status = 'all'; bands = t.schedule.bands.all; }
  else { status = t.maritalStatus === 'couple' ? 'couple' : 'single'; bands = t.schedule.bands[status] ?? t.schedule.bands.single ?? []; }
  const firstCeiling = bands?.[0]?.upTo ? D(bands[0]!.upTo!) : ZERO;
  const disability = t.disability ? firstCeiling.times(D(d.disabilityExtraFraction)) : ZERO;

  const adjusted = max0(projected.minus(retirementDeduction).minus(insurance).minus(remote).minus(disability));
  // Sec 12: approved donations up to the lower of the cap (Rs 1,00,000) and 5% of the adjusted taxable income.
  const donation = t.donation && t.donation.gt(0) && t.isResident
    ? Decimal.min(t.donation, D(d.donationCap ?? '100000'), adjusted.times(D(d.donationFraction ?? '0.05'))) : ZERO;
  const taxableAfter = max0(adjusted.minus(donation));

  const applied: TaxDetail['bands'] = [];
  let gross = ZERO;
  if (!t.isResident) {
    gross = projected.times(D(t.schedule.nonResidentRate));
    applied.push({ upTo: null, rate: t.schedule.nonResidentRate, slice: projected.toFixed(2), tax: gross.toFixed(2), waived: false });
  } else {
    let lower = ZERO;
    for (const b of bands ?? []) {
      const ceiling = b.upTo === null ? null : D(b.upTo);
      const width = ceiling === null ? taxableAfter.minus(lower) : ceiling.minus(lower);
      const slice = Decimal.max(0, Decimal.min(taxableAfter.minus(lower), width));
      // SST (1% band) is waived for contributors to the SSF or to an approved retirement fund (plan §6.6). The Labour
      // Act Sec 52 provident fund and the Citizen Investment Trust are approved retirement funds, so the waiver also
      // applies to the pf regime and to anyone with retirement contributions in the year; regime 'none' with no
      // contributions pays the 1%. Confirm with the CA (plan Gap G-1).
      const waived = Boolean(b.sst) && (t.regime === 'ssf' || t.regime === 'pf' || annualRetirement.gt(0));
      const tax = waived ? ZERO : slice.times(D(b.rate));
      applied.push({ upTo: b.upTo, rate: b.rate, slice: slice.toFixed(2), tax: tax.toFixed(2), waived });
      gross = gross.plus(tax);
      if (ceiling !== null) lower = ceiling;
      if (taxableAfter.lte(lower)) break;
    }
  }

  // Female rebate: resident woman, filing individually, remuneration as sole income (plan §6.6).
  const femaleRebate = t.isResident && t.gender === 'female' && t.maritalStatus !== 'couple' && t.remunerationOnlyIncome ? gross.times(D(d.femaleRebate)) : ZERO;
  // Non-residents pay the flat rate on gross remuneration without deductions or credits.
  const medicalCredit = t.isResident ? Decimal.min(t.medicalCost.times(D(d.medicalCreditRate)), D(d.medicalCreditCap)) : ZERO;
  const annualTax = max0(gross.minus(femaleRebate).minus(medicalCredit));
  const due = max0(annualTax.minus(t.ytdTdsPaid));
  const tdsThisMonth = rupee(t.deductFullTaxNow || t.isFinalSettlement ? due : due.div(remainingMonths));

  return {
    fiscalYearStart: t.schedule.fiscalYearStart, status,
    monthTaxable: paisa(t.monthTaxable).toFixed(2), recurringTaxable: paisa(t.recurringTaxable).toFixed(2), recurringRetirement: paisa(t.recurringRetirement).toFixed(2),
    projectedAnnualTaxable: paisa(projected).toFixed(2), retirementDeduction: paisa(retirementDeduction).toFixed(2),
    insuranceDeduction: paisa(insurance).toFixed(2), remoteAreaDeduction: remote.toFixed(2), disabilityDeduction: paisa(disability).toFixed(2), donationDeduction: paisa(donation).toFixed(2),
    taxableAfterDeductions: paisa(taxableAfter).toFixed(2), bands: applied, grossTax: paisa(gross).toFixed(2),
    femaleRebate: paisa(femaleRebate).toFixed(2), medicalCredit: paisa(medicalCredit).toFixed(2), annualTax: paisa(annualTax).toFixed(2),
    ytdTdsPaid: paisa(t.ytdTdsPaid).toFixed(2), remainingMonths, tdsThisMonth: tdsThisMonth.toFixed(2),
  };
}
