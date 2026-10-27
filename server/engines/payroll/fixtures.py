"""Shared input builder for tests and golden fixtures (port of src/fixtures.ts).

Defaults: Shrawan 2083 (31 days), SSF, single male, full month. Keyword arguments use the TS names, plus base= for
baseAmounts and fy= for the fiscal year.
"""
from __future__ import annotations

from typing import Any

from ._js import js_truthy, nullish
from .defaults import DEFAULT_COMPONENTS, rules_for_fy
from .numeric import js_num_str
from .types import PayrollInput


def base_input(**over: Any) -> PayrollInput:
    fy = nullish(over.get('fy'), 2083)
    period = nullish(over.get('period'), {'bsYear': fy, 'bsMonth': 4, 'daysInMonth': 31, 'fiscalYearStart': fy,
                                          'monthIndexInFy': 1})
    out: dict[str, Any] = {
        'employee': {'id': 'E1', 'code': 'E001', 'nameEn': 'Test Employee', 'nameNe': 'परीक्षण', 'gender': 'male',
                     'maritalStatus': 'single', 'isResident': True, 'regime': 'ssf', 'remoteAreaClass': None,
                     'disability': False, 'otEligible': True, 'festivalMonth': None, **(over.get('employee') or {})},
        'period': period,
        'components': nullish(over.get('components'), DEFAULT_COMPONENTS),
        'baseAmounts': nullish(over.get('base'), nullish(over.get('baseAmounts'), {'BASIC': '40000', 'ALLOWANCE': '20000'})),
        'inputs': nullish(over.get('inputs'), []),
        'attendance': {'paidDays': js_num_str(period['daysInMonth']), 'lopDays': '0', 'otHours': '0',
                       **(over.get('attendance') or {})},
        'ytd': {'taxable': '0', 'tdsPaid': '0', 'retirementEmployee': '0', 'retirementEmployer': '0', 'festivalPaid': False,
                **(over.get('ytd') or {})},
        'declaration': {'lifePremium': '0', 'healthPremium': '0', 'citMonthly': '0', 'medicalCost': '0',
                        'otherRetirementAnnual': '0', **(over.get('declaration') or {})},
        'loans': nullish(over.get('loans'), []),
        'rules': nullish(over.get('rules'), rules_for_fy(fy)),
        'policy': {'prorationBasis': 'bs_calendar_days', 'hourlyDivisor': 208, 'rounding': 'half_up_rupee',
                   'includeEmployerContributionInIncome': True, 'fullTime': True, **(over.get('policy') or {})},
    }
    if js_truthy(over.get('extras')):
        out['extras'] = over['extras']
    return out  # type: ignore[return-value]
