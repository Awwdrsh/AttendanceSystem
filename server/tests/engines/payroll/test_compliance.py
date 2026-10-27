"""Port of src/compliance.test.ts: donation deduction (Sec 12) and the optimum CIT suggestion."""
from engines.payroll import D, compute_payslip, optimum_cit
from engines.payroll.fixtures import base_input
from engines.payroll.numeric import to_dp, to_fixed

DECL0 = {'lifePremium': '0', 'healthPremium': '0', 'citMonthly': '0', 'medicalCost': '0', 'otherRetirementAnnual': '0'}


class TestDonationDeduction:
    def test_deducts_approved_donations_up_to_5_percent(self):
        plain = compute_payslip(base_input(base={'BASIC': '150000', 'ALLOWANCE': '50000'}))
        with_gift = compute_payslip(base_input(base={'BASIC': '150000', 'ALLOWANCE': '50000'}, declaration={**DECL0, 'donation': '30000'}))
        assert with_gift['tax']['donationDeduction'] == '30000.00'
        assert (to_fixed(D(with_gift['tax']['taxableAfterDeductions']), 2)
                == to_fixed(D(plain['tax']['taxableAfterDeductions']) - 30000, 2))
        assert D(with_gift['tax']['annualTax']) < D(plain['tax']['annualTax'])

    def test_caps_at_lower_of_100000_and_5_percent(self):
        small = compute_payslip(base_input(declaration={**DECL0, 'donation': '500000'}))
        adjusted = D(small['tax']['taxableAfterDeductions']) + D(small['tax']['donationDeduction'])
        assert small['tax']['donationDeduction'] == to_fixed(to_dp(adjusted * D('0.05'), 2), 2)
        big = compute_payslip(base_input(base={'BASIC': '600000', 'ALLOWANCE': '200000'}, declaration={**DECL0, 'donation': '500000'}))
        assert big['tax']['donationDeduction'] == '100000.00'

    def test_zero_without_declaration_and_for_non_residents(self):
        assert compute_payslip(base_input())['tax']['donationDeduction'] == '0.00'
        nr = compute_payslip(base_input(employee={**base_input()['employee'], 'isResident': False},
                                        declaration={**DECL0, 'donation': '50000'}))
        assert nr['tax']['donationDeduction'] == '0.00'


class TestOptimumCit:
    def test_fills_the_room_and_beats_brute_force(self):
        inp = base_input(base={'BASIC': '120000', 'ALLOWANCE': '60000'})
        o = optimum_cit(inp)
        assert D(o['optimumMonthly']) > 0
        assert D(o['annualSaving']) > 0
        # Brute force over a grid: no CIT amount gives a lower tax than the suggestion.
        for c in (0, 1000, 5000, 10000, 20000, 30000, 40000, 60000):
            r = compute_payslip({**inp, 'declaration': {**inp['declaration'], 'citMonthly': str(c)}})
            assert D(r['tax']['annualTax']) >= D(o['optimumAnnualTax'])
        # One rupee less no longer reaches the lowest tax (it is the smallest such amount).
        less = compute_payslip({**inp, 'declaration': {**inp['declaration'], 'citMonthly': to_fixed(D(o['optimumMonthly']) - 1, 2)}})
        assert D(less['tax']['annualTax']) > D(o['optimumAnnualTax'])

    def test_keeps_current_cit_when_already_lowest(self):
        inp = base_input(base={'BASIC': '120000', 'ALLOWANCE': '60000'})
        o = optimum_cit(inp)
        again = optimum_cit({**inp, 'declaration': {**inp['declaration'], 'citMonthly': to_fixed(D(o['optimumMonthly']) + 5000, 2)}})
        assert again['reason'] == 'already_optimal'
        assert again['annualSaving'] == '0.00'

    def test_suggests_nothing_when_no_tax_left(self):
        assert optimum_cit(base_input(base={'BASIC': '20000', 'ALLOWANCE': '0'}))['annualSaving'] == '0.00'

    def test_spreads_the_room_over_the_months_left(self):
        early = optimum_cit(base_input(base={'BASIC': '120000', 'ALLOWANCE': '60000'}))
        late = optimum_cit(base_input(
            base={'BASIC': '120000', 'ALLOWANCE': '60000'},
            period={'bsYear': 2083, 'bsMonth': 12, 'daysInMonth': 30, 'fiscalYearStart': 2083, 'monthIndexInFy': 9},
            ytd={'taxable': '1800000', 'tdsPaid': '150000', 'retirementEmployee': '105600', 'retirementEmployer': '192000',
                 'festivalPaid': True}))
        assert late['remainingMonths'] == 4
        assert D(late['optimumMonthly']) > D(early['optimumMonthly'])
