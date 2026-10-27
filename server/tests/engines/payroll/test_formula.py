"""Port of src/formula.test.ts (plan §11.2)."""
import re

import pytest

from engines.payroll import D, EvalEnv, FormulaError, evaluate_formula, formula_references, parse_formula, validate_formula
from engines.payroll.numeric import js_str


def _rule(key, path):
    if key == 'ssf.rates' and path == 'employee':
        return D('0.11')
    if key == 'min_wage' and path == 'total':
        return D('19550')
    return D(0)


ENV = EvalEnv(vars={'BASIC': D('40000'), 'DA': D('7380'), 'paid_days': D(22), 'days_in_bs_month': D(31),
                    'is_female': True, 'regime': 'ssf'}, rule=_rule)


def ev(s):
    """String(evaluateFormula(s)): decimal.js toString for numbers, 'true'/'false' for conditions."""
    v = evaluate_formula(s, ENV)
    return ('true' if v else 'false') if isinstance(v, bool) else v if isinstance(v, str) else js_str(v)


def test_arithmetic_with_precedence_and_decimals():
    assert ev('BASIC * 0.11') == '4400'
    assert ev('1 + 2 * 3 - 4 / 8') == '6.5'
    assert ev('-(BASIC + DA)') == '-47380'
    assert re.match(r'^28387\.09677', ev('BASIC * paid_days / days_in_bs_month'))


def test_functions():
    assert ev('min(BASIC, 12170, 99999)') == '12170'
    assert ev('max(BASIC + DA, rule("min_wage", "total"))') == '47380'
    assert ev('round(BASIC * paid_days / days_in_bs_month, 2)') == '28387.1'
    assert ev('round(2.5)') == '3'
    assert ev('floor(7.9) + ceil(7.1)') == '15'
    assert ev('if(is_female and regime == "ssf", 1, 0)') == '1'
    assert ev('if(not is_female or BASIC < 1000, 1, 0)') == '0'
    assert ev('BASIC * rule("ssf.rates", "employee")') == '4400'


def test_rejects_unknown_identifiers_functions_bad_arity_division_by_zero_and_type_errors():
    with pytest.raises(FormulaError, match='unknown identifier HRA'):
        ev('BASIC + HRA')
    with pytest.raises(FormulaError, match='unknown function'):
        ev('eval("x")')
    with pytest.raises(FormulaError, match='expects'):
        ev('round()')
    with pytest.raises(FormulaError, match='division by zero'):
        ev('BASIC / 0')
    with pytest.raises(FormulaError, match='expected a number'):
        ev('BASIC + is_female')
    with pytest.raises(FormulaError, match='expected a condition'):
        ev('if(BASIC, 1, 2)')
    for src in ('BASIC +', 'BASIC.toString()', 'BASIC; DROP TABLE'):
        with pytest.raises(FormulaError):
            parse_formula(src)


def test_validate_formula_reports_problems_for_the_rule_builder():
    assert validate_formula('BASIC * 0.1', ['BASIC']) is None
    assert validate_formula('BASIC * paid_days / days_in_bs_month', ['BASIC']) is None
    assert re.search('unknown identifier HRA', validate_formula('BASIC * HRA', ['BASIC']))
    assert re.search('unknown function', validate_formula('foo(1)', []))
    assert validate_formula('(1 + ', [])


def test_formula_references_lists_component_codes_and_variables():
    assert sorted(formula_references('if(BASIC > 0, BASIC * 0.5 + DA, 0)')) == ['BASIC', 'DA']
