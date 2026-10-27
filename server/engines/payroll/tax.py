"""TDS projection (plan §11.4; port of src/tax.ts): project the year, apply the FY schedule, true up against YTD,
spread over the remaining months.
"""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from ._js import js_get, js_truthy, nullish
from .numeric import UNDEFINED, ZERO, D, Decimal, dmax, dmin, engine_context, max0, paisa, rupee, to_fixed
from .types import AppliedBand, Regime, TaxDeductionRules, TaxDetail, TaxSchedule


class TaxInput(TypedDict):
    schedule: TaxSchedule
    deductions: TaxDeductionRules
    regime: Regime
    isResident: bool
    gender: Literal['male', 'female', 'other'] | None
    maritalStatus: Literal['single', 'couple'] | None
    # Remuneration is the only income (female rebate condition, plan §6.6).
    remunerationOnlyIncome: bool
    remoteAreaClass: Literal['A', 'B', 'C', 'D'] | None
    disability: bool
    # Taxable pay in this month (earnings + employer retirement contribution if policy includes it).
    monthTaxable: Decimal
    # Part of monthTaxable that recurs every month (excludes OT, festival, bonus, arrears, encashment).
    recurringTaxable: Decimal
    # One-offs known for later months in the FY (e.g. festival allowance not yet paid).
    futureOneOffs: Decimal
    ytdTaxable: Decimal
    ytdTdsPaid: Decimal
    # Retirement contributions (employee + employer + CIT) this month and already paid this FY.
    monthRetirement: Decimal
    recurringRetirement: Decimal
    ytdRetirement: Decimal
    otherRetirementAnnual: Decimal
    lifePremium: Decimal
    healthPremium: Decimal
    medicalCost: Decimal
    # Donations to approved organisations this fiscal year (Sec 12).
    donation: NotRequired[Decimal]
    monthIndexInFy: int
    deductFullTaxNow: bool
    isFinalSettlement: bool


def _fx(v: Decimal) -> str:
    return to_fixed(v, 2)


@engine_context
def compute_tds(t: TaxInput) -> TaxDetail:
    remaining_months = 1 if t['isFinalSettlement'] else max(1, 12 - t['monthIndexInFy'] + 1)
    future_months = 0 if t['isFinalSettlement'] else remaining_months - 1
    projected = t['ytdTaxable'] + t['monthTaxable'] + t['recurringTaxable'] * D(future_months) + t['futureOneOffs']
    annual_retirement = (t['ytdRetirement'] + t['monthRetirement'] + t['recurringRetirement'] * D(future_months)
                         + t['otherRetirementAnnual'])

    d = t['deductions']
    retirement_deduction = (ZERO if t['regime'] == 'none' and annual_retirement.is_zero()
                            else dmin(annual_retirement, D(d['retirementCap']), projected / D(d['retirementDivisor'])))
    insurance = dmin(t['lifePremium'], D(d['lifePremiumCap'])) + dmin(t['healthPremium'], D(d['healthPremiumCap']))
    remote = D(js_get(d['remoteArea'], t['remoteAreaClass'])) if js_truthy(t['remoteAreaClass']) else ZERO

    schedule_bands = t['schedule']['bands']
    if not t['isResident']:
        status, bands = 'non_resident', []
    elif schedule_bands.get('all') is not None:
        status, bands = 'all', schedule_bands['all']
    else:
        status = 'couple' if t['maritalStatus'] == 'couple' else 'single'
        bands = nullish(schedule_bands.get(status), nullish(schedule_bands.get('single'), []))
    first_up_to = bands[0].get('upTo') if bands else None
    first_ceiling = D(first_up_to) if js_truthy(first_up_to) else ZERO
    disability = first_ceiling * D(d['disabilityExtraFraction']) if t['disability'] else ZERO

    adjusted = max0(projected - retirement_deduction - insurance - remote - disability)
    # Sec 12: approved donations up to the lower of the cap (Rs 1,00,000) and 5% of the adjusted taxable income.
    gift = t.get('donation')
    donation = (dmin(gift, D(nullish(d.get('donationCap'), '100000')), adjusted * D(nullish(d.get('donationFraction'), '0.05')))
                if gift is not None and gift > 0 and t['isResident'] else ZERO)
    taxable_after = max0(adjusted - donation)

    applied: list[AppliedBand] = []
    gross = ZERO
    if not t['isResident']:
        gross = projected * D(t['schedule']['nonResidentRate'])
        applied.append({'upTo': None, 'rate': t['schedule']['nonResidentRate'], 'slice': _fx(projected),
                        'tax': _fx(gross), 'waived': False})
    else:
        lower = ZERO
        for b in bands:
            up_to = b.get('upTo', UNDEFINED)
            ceiling = None if up_to is None else D(up_to)
            width = taxable_after - lower if ceiling is None else ceiling - lower
            slc = dmax(0, dmin(taxable_after - lower, width))
            # SST (1% band) is waived for contributors to the SSF or to an approved retirement fund (plan §6.6). The
            # Labour Act Sec 52 provident fund and the Citizen Investment Trust are approved retirement funds, so the
            # waiver also applies to the pf regime and to anyone with retirement contributions in the year; regime
            # 'none' with no contributions pays the 1%. Confirm with the CA (plan Gap G-1).
            waived = js_truthy(b.get('sst')) and (t['regime'] in ('ssf', 'pf') or annual_retirement > 0)
            tax = ZERO if waived else slc * D(b.get('rate', UNDEFINED))
            applied.append({'upTo': up_to, 'rate': b['rate'], 'slice': _fx(slc), 'tax': _fx(tax), 'waived': waived})
            gross = gross + tax
            if ceiling is not None:
                lower = ceiling
            if taxable_after <= lower:
                break

    # Female rebate: resident woman, filing individually, remuneration as sole income (plan §6.6).
    female_rebate = (gross * D(d['femaleRebate'])
                     if t['isResident'] and t['gender'] == 'female' and t['maritalStatus'] != 'couple'
                     and t['remunerationOnlyIncome'] else ZERO)
    # Non-residents pay the flat rate on gross remuneration without deductions or credits.
    medical_credit = dmin(t['medicalCost'] * D(d['medicalCreditRate']), D(d['medicalCreditCap'])) if t['isResident'] else ZERO
    annual_tax = max0(gross - female_rebate - medical_credit)
    due = max0(annual_tax - t['ytdTdsPaid'])
    tds_this_month = rupee(due if t['deductFullTaxNow'] or t['isFinalSettlement'] else due / D(remaining_months))

    return {
        'fiscalYearStart': t['schedule']['fiscalYearStart'], 'status': status,  # type: ignore[typeddict-item]
        'monthTaxable': _fx(paisa(t['monthTaxable'])), 'recurringTaxable': _fx(paisa(t['recurringTaxable'])),
        'recurringRetirement': _fx(paisa(t['recurringRetirement'])),
        'projectedAnnualTaxable': _fx(paisa(projected)), 'retirementDeduction': _fx(paisa(retirement_deduction)),
        'insuranceDeduction': _fx(paisa(insurance)), 'remoteAreaDeduction': _fx(remote),
        'disabilityDeduction': _fx(paisa(disability)), 'donationDeduction': _fx(paisa(donation)),
        'taxableAfterDeductions': _fx(paisa(taxable_after)), 'bands': applied, 'grossTax': _fx(paisa(gross)),
        'femaleRebate': _fx(paisa(female_rebate)), 'medicalCredit': _fx(paisa(medical_credit)),
        'annualTax': _fx(paisa(annual_tax)), 'ytdTdsPaid': _fx(paisa(t['ytdTdsPaid'])),
        'remainingMonths': remaining_months, 'tdsThisMonth': _fx(tds_this_month),
    }
