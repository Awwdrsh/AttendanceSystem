"""Default tax schedules, statutory rates and the standard component set (port of src/defaults.ts)."""
from __future__ import annotations

from .types import ComponentDef, RuleSet, TaxSchedule

# FY 2082/83 (Finance Act 2082): six bands, single and couple (plan §6.6).
TAX_SCHEDULE_2082: TaxSchedule = {
    'fiscalYearStart': 2082,
    'nonResidentRate': '0.25',
    'bands': {
        'single': [
            {'upTo': '500000', 'rate': '0.01', 'sst': True}, {'upTo': '700000', 'rate': '0.10'},
            {'upTo': '1000000', 'rate': '0.20'}, {'upTo': '2000000', 'rate': '0.30'},
            {'upTo': '5000000', 'rate': '0.36'}, {'upTo': None, 'rate': '0.39'},
        ],
        'couple': [
            {'upTo': '600000', 'rate': '0.01', 'sst': True}, {'upTo': '800000', 'rate': '0.10'},
            {'upTo': '1100000', 'rate': '0.20'}, {'upTo': '2000000', 'rate': '0.30'},
            {'upTo': '5000000', 'rate': '0.36'}, {'upTo': None, 'rate': '0.39'},
        ],
    },
}

# FY 2083/84 (Finance Act 2083): one table for all resident individuals (plan §6.6).
TAX_SCHEDULE_2083: TaxSchedule = {
    'fiscalYearStart': 2083,
    'nonResidentRate': '0.25',
    'bands': {
        'all': [
            {'upTo': '1000000', 'rate': '0.01', 'sst': True}, {'upTo': '1500000', 'rate': '0.10'},
            {'upTo': '2500000', 'rate': '0.20'}, {'upTo': '4000000', 'rate': '0.27'}, {'upTo': None, 'rate': '0.29'},
        ],
    },
}

DEFAULT_RULES: RuleSet = {
    'tax.schedule': TAX_SCHEDULE_2083,
    'tax.deductions': {
        'retirementCap': '500000', 'retirementFraction': '0.333333', 'retirementDivisor': '3', 'lifePremiumCap': '40000',
        'healthPremiumCap': '20000', 'remoteArea': {'A': '50000', 'B': '40000', 'C': '30000', 'D': '10000'},
        'disabilityExtraFraction': '0.5', 'medicalCreditRate': '0.15', 'medicalCreditCap': '1500', 'femaleRebate': '0.10',
        'donationCap': '100000', 'donationFraction': '0.05',
    },
    'ssf.rates': {'employee': '0.11', 'employer': '0.20',
                  'allocation': {'medical': '0.01', 'accident': '0.014', 'dependent': '0.0027', 'old_age': '0.2833'}},
    'pf.rates': {'employee': '0.10', 'employer': '0.10', 'gratuity': '0.0833'},
    'ot.multiplier': {'working': '1.5', 'weekly_off': '1.5', 'holiday': '1.5'},
    'festival.allowance': {'months': '1', 'defaultBsMonth': 6},
    'min_wage': {'basic': '12170', 'dearness': '7380', 'total': '19550', 'daily': '754', 'hourly': '101', 'partTimeHourly': '107'},
}


def rules_for_fy(fiscal_year_start: int) -> RuleSet:
    """DEFAULT_RULES with the tax schedule of that fiscal year (a shallow copy, like the TS spread)."""
    return {**DEFAULT_RULES, 'tax.schedule': TAX_SCHEDULE_2082 if fiscal_year_start <= 2082 else TAX_SCHEDULE_2083}


def _c(code: str, type_: str, name_en: str, name_ne: str, depends: bool, taxable: bool, rounding: str, sort_order: int,
       statutory: bool = False) -> ComponentDef:
    c = {'code': code, 'type': type_, 'nameEn': name_en, 'nameNe': name_ne, 'dependsOnPaymentDays': depends,
         'taxable': taxable}
    if statutory:
        c['statutory'] = True
    c['rounding'] = rounding
    c['sortOrder'] = sort_order
    return c  # type: ignore[return-value]


# Standard component set (plan §11.1). Codes are the engine's built-in vocabulary.
DEFAULT_COMPONENTS: list[ComponentDef] = [
    _c('BASIC', 'earning', 'Basic salary', 'आधारभूत तलब', True, True, 'paisa', 10),
    _c('DA', 'earning', 'Dearness allowance', 'महँगी भत्ता', True, True, 'paisa', 20),
    _c('HRA', 'earning', 'House rent allowance', 'घर भाडा भत्ता', True, True, 'paisa', 30),
    _c('ALLOWANCE', 'earning', 'Other allowance', 'अन्य भत्ता', True, True, 'paisa', 40),
    _c('OT', 'earning', 'Overtime', 'ओभरटाइम', False, True, 'rupee', 50),
    _c('FESTIVAL', 'earning', 'Festival allowance', 'चाडपर्व खर्च', False, True, 'paisa', 60),
    _c('ARREARS', 'earning', 'Arrears', 'बक्यौता', False, True, 'paisa', 70),
    _c('BONUS', 'earning', 'Bonus', 'बोनस', False, True, 'paisa', 80),
    _c('LEAVE_ENCASH', 'earning', 'Leave encashment', 'बिदा नगदीकरण', False, True, 'paisa', 90),
    _c('NOTICE_PAY', 'earning', 'Notice pay', 'सूचना अवधि भुक्तानी', False, True, 'paisa', 95),
    _c('SSF_EE', 'deduction', 'SSF contribution (employee)', 'सामाजिक सुरक्षा कोष (कर्मचारी)', False, False, 'rupee', 200, True),
    _c('PF_EE', 'deduction', 'Provident fund (employee)', 'सञ्चय कोष (कर्मचारी)', False, False, 'rupee', 210, True),
    _c('CIT', 'deduction', 'Citizen investment trust', 'नागरिक लगानी कोष', False, False, 'paisa', 220),
    _c('TDS', 'deduction', 'Income tax (TDS)', 'आयकर', False, False, 'rupee', 230, True),
    _c('INSURANCE_LIFE', 'deduction', 'Life insurance premium', 'जीवन बीमा', False, False, 'paisa', 240),
    _c('INSURANCE_HEALTH', 'deduction', 'Health insurance premium', 'स्वास्थ्य बीमा', False, False, 'paisa', 250),
    _c('ABSENCE', 'deduction', 'Absence deduction', 'अनुपस्थिति कट्टी', False, False, 'paisa', 260),
    _c('ADVANCE', 'deduction', 'Salary advance recovery', 'पेश्की असुली', False, False, 'paisa', 270),
    _c('NOTICE_DEDUCTION', 'deduction', 'Deduction in lieu of notice', 'सूचना अवधि कट्टी', False, False, 'paisa', 275),
    _c('LOAN', 'deduction', 'Loan instalment', 'ऋण किस्ता', False, False, 'paisa', 280),
    _c('SSF_ER', 'employer_contribution', 'SSF contribution (employer)', 'सामाजिक सुरक्षा कोष (रोजगारदाता)', False, True, 'rupee', 300, True),
    _c('PF_ER', 'employer_contribution', 'Provident fund (employer)', 'सञ्चय कोष (रोजगारदाता)', False, True, 'rupee', 310, True),
    _c('GRATUITY', 'employer_contribution', 'Gratuity accrual', 'उपदान', False, True, 'rupee', 320, True),
    _c('GROSS', 'statistical', 'Gross pay', 'कुल तलब', False, False, 'paisa', 400),
    _c('TAXABLE', 'statistical', 'Taxable pay', 'करयोग्य आय', False, False, 'paisa', 410),
]
