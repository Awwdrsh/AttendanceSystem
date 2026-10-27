"""Port of src/compute.test.ts: worked examples from plan.md, statutory regimes and the component graph."""
import pytest

from engines.payroll import DEFAULT_COMPONENTS, D, PayrollError, compute_payslip, payable_fraction
from engines.payroll.fixtures import base_input
from engines.payroll.numeric import to_fixed

DECL0 = {'lifePremium': '0', 'healthPremium': '0', 'citMonthly': '0', 'medicalCost': '0', 'otherRetirementAnnual': '0'}


def line(r, code):
    return next((l['amount'] for l in r['lines'] if l['code'] == code), None)


def find(r, code):
    return next((l for l in r['lines'] if l['code'] == code), None)


def emp(**kw):
    return {**base_input()['employee'], **kw}


def subset(actual, expected):
    """toMatchObject for a flat dict."""
    return all(actual.get(k) == v for k, v in expected.items())


class TestWorkedExamples:
    def test_fy_2083_single_ssf_life_30000_tds_0(self):
        r = compute_payslip(base_input(fy=2083, declaration={**DECL0, 'lifePremium': '30000'}))
        assert r['tax']['projectedAnnualTaxable'] == '856000.00'
        assert r['tax']['retirementDeduction'] == '148800.00'
        assert r['tax']['taxableAfterDeductions'] == '677200.00'
        assert r['tax']['annualTax'] == '0.00'
        assert r['tds'] == '0.00'
        assert line(r, 'SSF_EE') == '4400.00'
        assert line(r, 'SSF_ER') == '8000.00'
        assert r['gross'] == '60000.00'
        assert r['net'] == '55600.00'
        assert r['ssfAllocation'] == {'medical': '400.00', 'accident': '560.00', 'dependent': '108.00', 'old_age': '11332.00'}

    def test_same_employee_fy_2082_single_annual_tax_17720_monthly_1477(self):
        r = compute_payslip(base_input(fy=2082, declaration={**DECL0, 'lifePremium': '30000'}))
        assert r['tax']['taxableAfterDeductions'] == '677200.00'
        assert subset(r['tax']['bands'][0], {'upTo': '500000', 'waived': True, 'tax': '0.00'})
        assert subset(r['tax']['bands'][1], {'slice': '177200.00', 'tax': '17720.00'})
        assert r['tax']['annualTax'] == '17720.00'
        assert r['tds'] == '1477.00'
        assert r['net'] == '54123.00'

    def test_couple_fy_2082_uses_the_couple_table(self):
        r = compute_payslip(base_input(fy=2082, employee=emp(maritalStatus='couple'),
                                       declaration={**base_input()['declaration'], 'lifePremium': '30000'}))
        assert r['tax']['status'] == 'couple'
        assert subset(r['tax']['bands'][1], {'slice': '77200.00', 'tax': '7720.00'})
        assert r['tds'] == '643.00'

    def test_proration_joined_shrawan_10(self):
        r = compute_payslip(base_input(base={'BASIC': '40000'}, attendance={'paidDays': '22', 'lopDays': '0', 'otHours': '0'}))
        assert line(r, 'BASIC') == '28387.10'
        assert r['payableFraction'] == '0.709677'
        assert to_fixed(payable_fraction(D(28), 30, 'fixed_30'), 4) == '0.9333'
        assert to_fixed(payable_fraction(D(32), 32, 'fixed_30'), 4) == '1.0000'
        assert to_fixed(payable_fraction(D(24), 26, '26_day'), 4) == '0.9231'

    def test_ot_pay_10h_at_basic_30000(self):
        r = compute_payslip(base_input(base={'BASIC': '30000'}, attendance={'paidDays': '31', 'lopDays': '0', 'otHours': '10'}))
        assert line(r, 'OT') == '2163.00'
        assert subset(find(r, 'OT')['basis'], {'hourlyBasic': '144.23', 'multiplier': '1.50'})
        none = compute_payslip(base_input(base={'BASIC': '30000'}, attendance={'paidDays': '31', 'lopDays': '0', 'otHours': '10'},
                                          employee=emp(otEligible=False)))
        assert line(none, 'OT') is None

    def test_festival_allowance_ashwin_8_months(self):
        ashwin = {'bsYear': 2083, 'bsMonth': 6, 'daysInMonth': 31, 'fiscalYearStart': 2083, 'monthIndexInFy': 3}
        r = compute_payslip(base_input(period=ashwin, extras={'monthsOfServiceInFy': 8}))
        assert line(r, 'FESTIVAL') == '26666.67'
        assert to_fixed(D(r['taxable']), 2) == '94666.67'
        paid = compute_payslip(base_input(period=ashwin, ytd={**base_input()['ytd'], 'festivalPaid': True}))
        assert line(paid, 'FESTIVAL') is None
        chosen = compute_payslip(base_input(period={'bsYear': 2083, 'bsMonth': 9, 'daysInMonth': 30, 'fiscalYearStart': 2083,
                                                    'monthIndexInFy': 6}, employee=emp(festivalMonth=9)))
        assert line(chosen, 'FESTIVAL') == '40000.00'

    def test_final_settlement_encash_30_days(self):
        r = compute_payslip(base_input(
            fy=2082, extras={'isFinalSettlement': True, 'leaveEncashDays': '30', 'leaveEncashDivisor': 30, 'recoverLoansInFull': True},
            ytd={'taxable': '544000', 'tdsPaid': '10000', 'retirementEmployee': '35200', 'retirementEmployer': '64000', 'festivalPaid': True},
            loans=[{'id': 'L1', 'emi': '5000', 'remaining': '12000'}]))
        assert line(r, 'LEAVE_ENCASH') == '40000.00'
        assert line(r, 'LOAN') == '12000.00'
        assert r['tax']['remainingMonths'] == 1
        assert r['tax']['projectedAnnualTaxable'] == '652000.00'

    def test_minimum_wage_flag(self):
        assert 'BelowMinimumWage' in compute_payslip(base_input(base={'BASIC': '12000', 'DA': '7550'}))['flags']
        assert 'BelowMinimumWage' not in compute_payslip(base_input(base={'BASIC': '12170', 'DA': '7380'}))['flags']
        part_time = base_input(base={'BASIC': '12000'}, policy={**base_input()['policy'], 'fullTime': False})
        assert 'BelowMinimumWage' not in compute_payslip(part_time)['flags']


class TestStatutoryRegimesAndDeductions:
    def test_pf_regime(self):
        r = compute_payslip(base_input(employee=emp(regime='pf')))
        assert line(r, 'PF_EE') == '4000.00'
        assert line(r, 'PF_ER') == '4000.00'
        assert line(r, 'GRATUITY') == '3332.00'
        assert line(r, 'SSF_EE') is None
        assert r['employerStatutory'] == '7332.00'

    def test_regime_none_pays_sst(self):
        r = compute_payslip(base_input(employee=emp(regime='none')))
        assert r['employeeStatutory'] == '0.00'
        assert subset(r['tax']['bands'][0], {'waived': False})
        assert D(r['tax']['annualTax']) > 0

    def test_female_rebate(self):
        m = compute_payslip(base_input(base={'BASIC': '100000', 'ALLOWANCE': '50000'}))
        f = compute_payslip(base_input(base={'BASIC': '100000', 'ALLOWANCE': '50000'}, employee=emp(gender='female')))
        assert to_fixed(D(f['tax']['femaleRebate']), 2) == to_fixed(D(m['tax']['grossTax']) * D('0.10'), 2)
        assert D(f['tds']) < D(m['tds'])

    def test_non_resident_flat_25(self):
        r = compute_payslip(base_input(employee=emp(isResident=False)))
        assert r['tax']['status'] == 'non_resident'
        assert r['tax']['annualTax'] == to_fixed(D(r['tax']['projectedAnnualTaxable']) * D('0.25'), 2)

    def test_insurance_caps_remote_area_disability_medical_credit(self):
        r = compute_payslip(base_input(base={'BASIC': '100000', 'ALLOWANCE': '60000'}, employee=emp(remoteAreaClass='A', disability=True),
                                       declaration={**DECL0, 'lifePremium': '90000', 'healthPremium': '50000', 'medicalCost': '20000'}))
        assert r['tax']['insuranceDeduction'] == '60000.00'
        assert r['tax']['remoteAreaDeduction'] == '50000.00'
        assert r['tax']['disabilityDeduction'] == '500000.00'
        assert r['tax']['medicalCredit'] == '1500.00'

    def test_loan_emi_capped_at_available_net(self):
        r = compute_payslip(base_input(base={'BASIC': '20000'}, loans=[{'id': 'A', 'emi': '5000', 'remaining': '30000'},
                                                                      {'id': 'B', 'emi': '20000', 'remaining': '20000'}]))
        assert r['loanInstallments'][0] == {'loanId': 'A', 'amount': '5000.00'}
        assert 'LoanCapped' in r['flags']
        assert D(r['net']) >= 0

    def test_cit_counts_toward_retirement_deduction(self):
        r = compute_payslip(base_input(employee=emp(regime='none'), declaration={**base_input()['declaration'], 'citMonthly': '5000'}))
        assert line(r, 'CIT') == '5000.00'
        assert r['tax']['retirementDeduction'] == '60000.00'


def _earning(code, formula, sort_order, condition=None):
    c = {'code': code, 'type': 'earning', 'nameEn': code, 'formula': formula, 'dependsOnPaymentDays': False,
         'taxable': True, 'rounding': 'paisa', 'sortOrder': sort_order}
    if condition:
        c['condition'] = condition
    return c


class TestComponentGraph:
    def test_formula_components_in_dependency_order_and_conditions_skip(self):
        comps = [*[c for c in DEFAULT_COMPONENTS if c['code'] != 'HRA'],
                 _earning('HRA', 'BASIC * 0.2', 30, condition='BASIC > 10000'), _earning('TRANSPORT', 'HRA / 2', 5)]
        r = compute_payslip(base_input(components=comps))
        assert line(r, 'HRA') == '8000.00'
        assert line(r, 'TRANSPORT') == '4000.00'
        skipped = compute_payslip(base_input(components=comps, base={'BASIC': '9000'}))
        assert line(skipped, 'HRA') is None
        assert line(skipped, 'TRANSPORT') is None

    def test_detects_formula_cycles(self):
        comps = [*DEFAULT_COMPONENTS, _earning('X', 'Y + 1', 1), _earning('Y', 'X + 1', 2)]
        with pytest.raises(PayrollError, match='cycle'):
            compute_payslip(base_input(components=comps))

    def test_one_time_inputs_not_projected_as_recurring(self):
        r = compute_payslip(base_input(inputs=[{'code': 'BONUS', 'amount': '50000'}, {'code': 'ABSENCE', 'amount': '1290.32'}]))
        assert line(r, 'BONUS') == '50000.00'
        assert line(r, 'ABSENCE') == '1290.32'
        assert r['tax']['recurringTaxable'] == '68000.00'
        assert r['tax']['monthTaxable'] == '118000.00'

    def test_sum_of_lines_equals_totals_and_rounding_line_keeps_net_whole(self):
        r = compute_payslip(base_input(base={'BASIC': '33333.33', 'ALLOWANCE': '0.01'}))
        earn = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'earning'), D(0))
        ded = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'deduction'), D(0))
        assert to_fixed(earn - ded, 2) == r['net']
        assert D(r['net']) % 1 == 0
        assert any(l['code'] == 'ROUNDING' for l in r['lines'])

    def test_tds_projection_trues_up_against_ytd(self):
        r = compute_payslip(base_input(
            fy=2082, period={'bsYear': 2082, 'bsMonth': 9, 'daysInMonth': 30, 'fiscalYearStart': 2082, 'monthIndexInFy': 6},
            ytd={'taxable': '340000', 'tdsPaid': '7000', 'retirementEmployee': '22000', 'retirementEmployer': '40000', 'festivalPaid': True},
            declaration={**base_input()['declaration'], 'lifePremium': '30000'}))
        assert r['tax']['remainingMonths'] == 7
        # 340,000 + 68,000 x 7 = 816,000 projected; retirement 62,000 + 12,400 x 7 = 148,800 -> taxable 637,200
        # -> tax 13,720 - 7,000 paid = 6,720 / 7 = 960
        assert r['tax']['projectedAnnualTaxable'] == '816000.00'
        assert r['tax']['annualTax'] == '13720.00'
        assert r['tds'] == '960.00'
