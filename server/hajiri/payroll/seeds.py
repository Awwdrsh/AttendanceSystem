"""Statutory rule versions and the standard salary components (port of payroll/seed.ts). Rule versions are
jurisdiction-wide, not per company; idempotent on (rule_key, valid_from_ad)."""
from __future__ import annotations

from django.db import transaction

from engines.payroll import DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083

from hajiri.db.models import RuleVersion, SalaryComponent, SalaryStructure, SalaryStructureVersion
from hajiri.worker.registry import company_seeder

RULES = [
    {'key': 'tax.schedule', 'fromAd': '2025-07-17', 'toAd': '2026-07-16', 'fromBs': '2082-04-01', 'toBs': '2083-03-32', 'value': TAX_SCHEDULE_2082,
     'source': 'https://lawalpine.com/blog/salary-tax-calculator-nepal-2082-83', 'note': 'Finance Act 2082: six bands, single and couple'},
    {'key': 'tax.schedule', 'fromAd': '2026-07-17', 'toAd': None, 'fromBs': '2083-04-01', 'toBs': None, 'value': TAX_SCHEDULE_2083,
     'source': 'https://frontline.com.np/blog/nepal-income-tax-slabs-fy-2083-84/', 'note': 'Finance Act 2083: single table; verify against the IRD circular (Gap G-1)'},
    {'key': 'tax.deductions', 'fromAd': '2025-07-17', 'toAd': None, 'fromBs': '2082-04-01', 'toBs': None, 'value': DEFAULT_RULES['tax.deductions'],
     'source': 'https://notarynepal.com/blog/tax-rate-in-nepal'},
    {'key': 'ssf.rates', 'fromAd': '2019-07-17', 'toAd': None, 'fromBs': '2076-04-01', 'toBs': None, 'value': DEFAULT_RULES['ssf.rates'],
     'source': 'https://pioneerlaw.com/?p=198'},
    {'key': 'pf.rates', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None, 'value': DEFAULT_RULES['pf.rates'],
     'source': 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', 'note': 'Labour Act 2074 Sec 52-53'},
    {'key': 'ot.multiplier', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None, 'value': DEFAULT_RULES['ot.multiplier'],
     'source': 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', 'note': 'Sec 31(1); weekly-off/holiday work may instead earn substitute leave (Sec 42)'},
    {'key': 'festival.allowance', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None, 'value': DEFAULT_RULES['festival.allowance'],
     'source': 'https://faolex.fao.org/docs/pdf/NEP225978.pdf', 'note': 'Sec 37'},
    {'key': 'min_wage', 'fromAd': '2023-08-17', 'toAd': '2025-07-16', 'fromBs': '2080-05-01', 'toBs': '2082-03-32',
     'value': {'basic': '10820', 'dearness': '6480', 'total': '17300', 'daily': '667', 'hourly': '89', 'partTimeHourly': '95'},
     'source': 'https://pradhanlaw.com/publications/minimum-remuneration-notice-2082-2025-ad', 'note': 'superseded Notice 2080'},
    {'key': 'min_wage', 'fromAd': '2025-07-17', 'toAd': None, 'fromBs': '2082-04-01', 'toBs': None, 'value': DEFAULT_RULES['min_wage'],
     'source': 'https://thehimalayantimes.com/nepal/govt-hikes-workers-minimum-wage-to-rs-19550-per-month', 'note': 'Notice 2082'},
    {'key': 'bonus.caps', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None,
     'value': {'poolRate': '0.10', 'monthsCapLow': 8, 'monthsCapHigh': 6, 'lowPayThreshold': '39100', 'minWorkedFraction': '0.5', 'payWithinMonths': 8},
     'source': 'https://nepallaws.com/Laws/bonus-act-2030/section-7-amount-to-be-obtained-for-bonus-and-its-assessment/'},
    {'key': 'ssf.deposit_days', 'fromAd': '2019-07-17', 'toAd': '2025-07-29', 'fromBs': '2076-04-01', 'toBs': '2082-04-13', 'value': {'days': 15},
     'source': 'https://pioneerlaw.com/?p=1337'},
    {'key': 'ssf.deposit_days', 'fromAd': '2025-07-30', 'toAd': None, 'fromBs': '2082-04-14', 'toBs': None, 'value': {'days': 25, 'lateInterest': '0.10'},
     'source': 'https://nepallaws.com/Laws/the-contribution-based-social-security-act-2074/chapter-2-provision-related-to-contribution-based-social-security-scheme/section-4-contribution-to-be-deposited/'},
    {'key': 'tds.deposit_days', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None,
     'value': {'days': 25, 'lateInterest': '0.15', 'annualReturnBy': 'Ashwin end'}, 'source': 'https://lawalpine.com/blog/tds-in-nepal'},
    {'key': 'payroll.proration', 'fromAd': '2017-09-04', 'toAd': None, 'fromBs': '2074-05-19', 'toBs': None,
     'value': {'default': 'bs_calendar_days', 'alternatives': ['fixed_30', '26_day'], 'hourlyDivisor': 208}, 'source': 'Common payroll practice in Nepal (not set by law)'},
]


def seed_payroll_defaults(company_id: str) -> dict[str, int]:
    rules = 0
    for r in RULES:
        _, created = RuleVersion.objects.get_or_create(
            rule_key=r['key'], jurisdiction='NP', valid_from_ad=r['fromAd'],
            defaults={'valid_to_ad': r['toAd'], 'valid_from_bs': r['fromBs'], 'valid_to_bs': r['toBs'], 'value': r['value'],
                      'source_url': r['source'], 'note': r.get('note')})
        rules += created
    have = set(SalaryComponent.objects.for_company(company_id).values_list('code', flat=True))
    missing = [SalaryComponent(company_id=company_id, code=c['code'], name_en=c['nameEn'], name_ne=c.get('nameNe'), type=c['type'],
                               formula=c.get('formula'), condition=c.get('condition'), depends_on_payment_days=c['dependsOnPaymentDays'],
                               taxable=c['taxable'], statutory=bool(c.get('statutory', False)), rounding=c['rounding'],
                               sort_order=c['sortOrder'])
               for c in DEFAULT_COMPONENTS if c['code'] not in have]
    SalaryComponent.objects.bulk_create(missing, ignore_conflicts=True)
    structures = 0
    with transaction.atomic():
        s, created = SalaryStructure.objects.get_or_create(company_id=company_id, code='STD', defaults={'name_en': 'Standard structure'})
        if created:
            SalaryStructureVersion.objects.create(structure=s, version=1, components=[{'code': c['code']} for c in DEFAULT_COMPONENTS])
            structures = 1
    return {'rules': rules, 'components': len(missing), 'structures': structures}


@company_seeder('payroll', order=10)
def _seed(company_id):
    return seed_payroll_defaults(company_id)
