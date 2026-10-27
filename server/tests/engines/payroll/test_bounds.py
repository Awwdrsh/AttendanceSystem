"""Port of src/bounds.test.ts."""
import pytest

from engines.payroll import PayrollError, amount_in_words_en, amount_in_words_ne, compute_payslip, format_npr
from engines.payroll.fixtures import base_input


class TestAmountBounds:
    def test_line_too_large_for_numeric_14_2_is_a_payroll_error(self):
        with pytest.raises(PayrollError, match='too large'):
            compute_payslip(base_input(base={'BASIC': '2000000000000', 'ALLOWANCE': '0'}))
        compute_payslip(base_input(base={'BASIC': '999999', 'ALLOWANCE': '0'}))


class TestNegativeZero:
    def test_amounts_that_round_to_zero_are_never_shown_as_negative(self):
        assert format_npr('-0.004') == '0.00'
        assert format_npr('-0') == '0.00'
        assert amount_in_words_en('-0.004') == 'Zero Rupees Only'
        assert not amount_in_words_ne('-0.001').startswith('ऋण')
        assert format_npr('-0.01') == '-0.01'
        assert amount_in_words_en('-1500.50') == 'Minus One Thousand Five Hundred Rupees and Fifty Paisa Only'
