"""Port of src/qa.test.ts: formula DSL safety, money totals and tax edge cases."""
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engines.payroll import DEFAULT_COMPONENTS, D, Decimal, EvalEnv, FormulaError, compute_payslip, evaluate_formula, parse_formula, validate_formula
from engines.payroll.fixtures import base_input
from engines.payroll.numeric import to_fixed

ENV = EvalEnv(vars={'BASIC': D(40000), 'paid_days': D(31), 'regime': 'ssf', 'is_female': False},
              rule=lambda k, _p=None: D(1) if k == 'x' else 'text')


def line(r, code):
    return next((l['amount'] for l in r['lines'] if l['code'] == code), None)


def emp(**kw):
    return {**base_input()['employee'], **kw}


def _earning(code, formula):
    return {'code': code, 'type': 'earning', 'nameEn': code, 'formula': formula, 'dependsOnPaymentDays': False,
            'taxable': True, 'rounding': 'paisa', 'sortOrder': 1}


class TestFormulaDslSafety:
    @settings(max_examples=2000, deadline=None)
    @given(st.text(max_size=120))
    def test_only_ever_throws_formula_error_for_random_strings(self, src):
        try:
            evaluate_formula(src, ENV)
        except FormulaError:
            pass

    TOKENS = ['BASIC', 'paid_days', 'regime', 'is_female', '(', ')', ',', '+', '-', '*', '/', '<', '>=', '==', 'and', 'or',
              'not', 'if', 'min', 'max', 'round', 'floor', 'rule', '"x"', '"ssf"', '0', '1', '2.5', '1e400', '-1e400',
              '999999999999999999999', 'constructor', '__proto__', 'toString', 'true']

    @settings(max_examples=3000, deadline=None)
    @given(st.lists(st.sampled_from(TOKENS), max_size=40))
    def test_only_ever_throws_formula_error_for_token_soup(self, parts):
        try:
            v = evaluate_formula(' '.join(parts), ENV)
            if isinstance(v, Decimal):
                assert v.is_finite()
        except FormulaError:
            pass

    def test_prototype_names_are_not_identifiers(self):
        for name in ('constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf'):
            with pytest.raises(FormulaError, match='unknown identifier'):
                evaluate_formula(name, ENV)
            assert re.search('unknown identifier', validate_formula(name, ['BASIC']))

    def test_deep_nesting_is_rejected_instead_of_overflowing_the_stack(self):
        for src in ('(' * 500 + '1' + ')' * 500, '-' * 500 + '1', 'not ' * 300 + 'true', 'min(' * 200 + '1' + ')' * 200):
            with pytest.raises(FormulaError, match='nested too deeply'):
                parse_formula(src)
        with pytest.raises(FormulaError, match='longer than'):
            parse_formula('1 + ' * 3000 + '1')

    def test_decimal_range_errors_and_non_finite_results_become_formula_error(self):
        for src in ('round(1.5, 2.5)', 'round(1.5, 99)', 'round(1.5, -1)', '1e9000000000000001 - 1e9000000000000001'):
            with pytest.raises(FormulaError):
                evaluate_formula(src, ENV)
        with pytest.raises(FormulaError, match='finite'):
            evaluate_formula('1e9000000000000001 * 2', ENV)
        with pytest.raises(FormulaError):
            evaluate_formula('rule("y") + 1', ENV)  # rule returns a non-numeric string

    def test_component_formula_producing_non_finite_amount_fails_loudly(self):
        with pytest.raises(Exception, match='BAD|finite'):
            compute_payslip(base_input(components=[*DEFAULT_COMPONENTS, _earning('BAD', '1e9999999999999999')]))
        with pytest.raises(FormulaError):
            compute_payslip(base_input(components=[*DEFAULT_COMPONENTS, _earning('BAD', 'BASIC / 0')]))

    def test_rule_with_non_numeric_string_value_does_not_crash_the_lookup(self):
        rules = {**base_input()['rules'], 'label': 'bs_calendar_days'}
        assert compute_payslip(base_input(rules=rules))['net'] is not None
        comps = [*DEFAULT_COMPONENTS, _earning('X', 'if(rule("label") == "bs_calendar_days", 10, 0)')]
        assert line(compute_payslip(base_input(components=comps, rules=rules)), 'X') == '10.00'


class TestMoneyAndTotals:
    def test_gross_and_total_deductions_exclude_rounding_adjustment(self):
        r = compute_payslip(base_input(base={'BASIC': '33333.33', 'ALLOWANCE': '0.01'}))
        assert r['gross'] == '33333.34'
        assert D(r['net']) % 1 == 0
        earn = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'earning'), D(0))
        ded = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'deduction'), D(0))
        assert to_fixed(earn - ded, 2) == r['net']
        assert abs(D(r['gross']) - D(r['totalDeductions']) - D(r['net'])) < D('0.5')

    def test_non_statutory_deductions_are_capped_so_net_never_goes_negative(self):
        r = compute_payslip(base_input(base={'BASIC': '20000'}, inputs=[{'code': 'ABSENCE', 'amount': '50000'},
                                                                         {'code': 'ADVANCE', 'amount': '5000'}]))
        assert D(r['net']) >= 0
        assert 'DeductionCapped:ABSENCE' in r['flags']
        assert 'DeductionCapped:ADVANCE' in r['flags']
        assert 'NegativeNet' not in r['flags']
        assert line(r, 'SSF_EE') == '2200.00'  # statutory share untouched
        assert line(r, 'ADVANCE') is None  # nothing left after ABSENCE took the rest

    def test_statutory_shares_never_capped_tds_capped_and_flagged(self):
        r = compute_payslip(base_input(base={'BASIC': '20000'}, inputs=[{'code': 'ABSENCE', 'amount': '50000'}]))
        assert line(r, 'SSF_EE') == '2200.00'
        assert line(r, 'ABSENCE') == '17800.00'
        none = compute_payslip(base_input(employee=emp(regime='none'), attendance={'paidDays': '0', 'lopDays': '31', 'otHours': '0'}))
        assert none['net'] == '0.00'
        assert none['tds'] == '0.00'
        assert 'TdsCapped' in none['flags']
        assert 'NegativeNet' not in none['flags']

    def test_recurring_income_uses_full_month_amounts_when_prorated(self):
        full = compute_payslip(base_input(attendance={'paidDays': '31', 'lopDays': '0', 'otHours': '0'}))
        part = compute_payslip(base_input(attendance={'paidDays': '10', 'lopDays': '21', 'otHours': '0'}))
        assert part['tax']['recurringTaxable'] == full['tax']['recurringTaxable']
        assert part['tax']['recurringTaxable'] == '68000.00'
        zero = compute_payslip(base_input(attendance={'paidDays': '0', 'lopDays': '31', 'otHours': '0'}))
        assert zero['tax']['recurringTaxable'] == '68000.00'

    def test_one_time_inputs_on_a_recurring_component_are_not_projected(self):
        r = compute_payslip(base_input(inputs=[{'code': 'BASIC', 'amount': '10000'}]))
        assert line(r, 'BASIC') == '50000.00'
        assert r['tax']['recurringTaxable'] == '68000.00'

    def test_recurring_override_drives_the_projection(self):
        r = compute_payslip(base_input(
            fy=2082, base={}, inputs=[{'code': 'ARREARS', 'amount': '50000'}],
            ytd={'taxable': '68000', 'tdsPaid': '1477', 'retirementEmployee': '4400', 'retirementEmployer': '8000', 'festivalPaid': False},
            period={'bsYear': 2082, 'bsMonth': 4, 'daysInMonth': 31, 'fiscalYearStart': 2082, 'monthIndexInFy': 1},
            extras={'recurringOverride': {'taxable': '68000', 'retirement': '12400'}}))
        # The festival projection needs a basic, which a one-off run has none of.
        assert r['tax']['projectedAnnualTaxable'] == to_fixed(D(68000) + 50000 + D(68000) * 11, 2)
        assert D(r['tds']) > 0


def _at(taxable, fy, **over):
    # Income such that projected taxable after deductions lands exactly on `taxable`: regime none, no deductions.
    from engines.payroll.numeric import ENGINE_CONTEXT
    monthly = ENGINE_CONTEXT.divide(D(taxable), D(12))
    return compute_payslip(base_input(fy=fy, base={'ALLOWANCE': to_fixed(monthly, 6)}, employee=emp(regime='none', **over)))


class TestTaxEdgeCases:
    def test_fy_2083_band_boundary_at_1000000(self):
        edge = _at(1_000_000, 2083)
        assert edge['tax']['taxableAfterDeductions'] == '1000000.00'
        assert edge['tax']['annualTax'] == '10000.00'
        over = _at(1_000_012, 2083)
        assert to_fixed(D(over['tax']['annualTax']) - 10000, 2) == '1.20'
        assert over['tax']['bands'][1]['rate'] == '0.10'

    def test_fy_2082_single_vs_couple_thresholds(self):
        assert _at(600_000, 2082)['tax']['annualTax'] == '15000.00'  # 5,000 SST + 100,000 x 10%
        assert _at(600_000, 2082, maritalStatus='couple')['tax']['annualTax'] == '6000.00'  # all SST in the couple band
        assert _at(600_000, 2083, maritalStatus='couple')['tax']['status'] == 'all'

    def test_sst_waiver(self):
        assert compute_payslip(base_input(employee=emp(regime='pf')))['tax']['bands'][0]['waived'] is True
        cit = compute_payslip(base_input(employee=emp(regime='none'), declaration={**base_input()['declaration'], 'citMonthly': '1000'}))
        assert cit['tax']['bands'][0]['waived'] is True
        assert compute_payslip(base_input(employee=emp(regime='none')))['tax']['bands'][0]['waived'] is False

    def test_female_rebate_requires_remuneration_only_income_non_residents_get_no_credits(self):
        base = {'BASIC': '100000', 'ALLOWANCE': '50000'}
        f = compute_payslip(base_input(base=base, employee=emp(gender='female')))
        other = compute_payslip(base_input(base=base, employee=emp(gender='female', remunerationOnlyIncome=False)))
        assert D(f['tax']['femaleRebate']) > 0
        assert other['tax']['femaleRebate'] == '0.00'
        nr = compute_payslip(base_input(base=base, employee=emp(isResident=False, gender='female'),
                                        declaration={**base_input()['declaration'], 'medicalCost': '20000', 'lifePremium': '30000'}))
        assert nr['tax']['medicalCredit'] == '0.00'
        assert nr['tax']['femaleRebate'] == '0.00'
        assert nr['tax']['annualTax'] == to_fixed(D(nr['tax']['projectedAnnualTaxable']) * D('0.25'), 2)

    def test_final_month_tds_is_annual_minus_ytd_never_negative(self):
        ashadh = {'bsYear': 2083, 'bsMonth': 3, 'daysInMonth': 32, 'fiscalYearStart': 2082, 'monthIndexInFy': 12}
        r = compute_payslip(base_input(fy=2082, period=ashadh, ytd={'taxable': '748000', 'tdsPaid': '10000', 'retirementEmployee': '48400',
                                                                   'retirementEmployer': '88000', 'festivalPaid': True},
                                       declaration={**base_input()['declaration'], 'lifePremium': '30000'}))
        assert r['tax']['remainingMonths'] == 1
        assert r['tax']['annualTax'] == '13720.00'
        assert r['tds'] == '3720.00'
        over = compute_payslip(base_input(fy=2082, period=ashadh, ytd={'taxable': '748000', 'tdsPaid': '99999', 'retirementEmployee': '48400',
                                                                      'retirementEmployer': '88000', 'festivalPaid': True}))
        assert over['tds'] == '0.00'

    def test_proration_edge_cases(self):
        def b(paid, days):
            return compute_payslip(base_input(base={'BASIC': '31000'},
                                              period={'bsYear': 2083, 'bsMonth': 8, 'daysInMonth': days, 'fiscalYearStart': 2083, 'monthIndexInFy': 5},
                                              attendance={'paidDays': str(paid), 'lopDays': str(days - paid), 'otHours': '0'}))
        assert line(b(29, 29), 'BASIC') == '31000.00'
        assert line(b(32, 32), 'BASIC') == '31000.00'
        assert line(b(1, 29), 'BASIC') == '1068.97'
        assert line(b(31, 32), 'BASIC') == '30031.25'
        assert line(b(0, 32), 'BASIC') is None
        assert b(0, 32)['net'] == '0.00'
        assert 'NegativeNet' not in b(0, 32)['flags']
