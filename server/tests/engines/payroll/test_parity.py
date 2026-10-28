"""Parity with the TS engine beyond the golden files.

The fixtures in tests/parity/fixtures/payroll_*.json were recorded from the TS engine (packages/payroll-engine/src/
dump-parity.ts, removed with the TypeScript stack; see commit 1bbe88b) and are a fixed record.
Every case must match exactly: full payslip results (lines, basis, explain text, tax detail, flags), formula parse
trees, values and error messages, the TDS sweep, amounts in words and the optimum CIT suggestion.
"""
import json
from pathlib import Path

import pytest

from engines.payroll import (
    DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083, D, DecimalError, EvalEnv, FormulaError,
    PayrollError, amount_in_words_en, amount_in_words_ne, compute_payslip, compute_tds, evaluate_formula, format_npr,
    formula_references, optimum_cit, parse_formula, payable_fraction, paisa, rules_for_fy, rupee, validate_formula,
)
from engines.payroll._js import typeof
from engines.payroll.numeric import Decimal, js_str, to_fixed

FIXTURES = Path(__file__).parent.parent.parent / 'parity' / 'fixtures'


def load(name):
    path = FIXTURES / f'payroll_{name}.json'
    if not path.exists():
        pytest.fail(f'{path} is missing; restore it from git (it was recorded from the TS engine)')
    return json.loads(path.read_text(encoding='utf-8'))


def attempt(f):
    """{'ok': value} or {'error': <TS class name>, 'message': ...}, the shape the TS dump writes."""
    try:
        return {'ok': f()}
    except FormulaError as e:
        return {'error': 'FormulaError', 'message': str(e)}
    except PayrollError as e:
        return {'error': 'PayrollError', 'message': str(e)}
    except DecimalError as e:
        return {'error': 'Error', 'message': str(e)}
    except Exception as e:  # noqa: BLE001 - any other exception is a parity failure, reported as such
        return {'error': f'python:{type(e).__name__}', 'message': str(e)}


def plain(v):
    """JSON round trip, so tuples and the like compare as the TS JSON does."""
    return json.loads(json.dumps(v))


def unpack(inp):
    out = dict(inp)
    if out['components'] == 'DEFAULT':
        out['components'] = DEFAULT_COMPONENTS
    if isinstance(out['rules'], dict) and '$rulesForFy' in out['rules']:
        out['rules'] = rules_for_fy(out['rules']['$rulesForFy'])
    return out


def check(cases, expected_of, actual_of):
    mismatches = []
    for i, case in enumerate(cases):
        expected = expected_of(case)
        actual = plain(actual_of(case))
        if actual != expected:
            mismatches.append((i, case.get('name', case.get('src', case.get('v'))), expected, actual))
    detail = '\n'.join(f'#{i} {name!r}\n  ts: {exp}\n  py: {act}' for i, name, exp, act in mismatches[:5])
    assert not mismatches, f'{len(mismatches)} of {len(cases)} cases differ:\n{detail}'


def test_defaults():
    ts = load('defaults')
    assert plain({'DEFAULT_COMPONENTS': DEFAULT_COMPONENTS, 'DEFAULT_RULES': DEFAULT_RULES,
                  'TAX_SCHEDULE_2082': TAX_SCHEDULE_2082, 'TAX_SCHEDULE_2083': TAX_SCHEDULE_2083}) == ts


def test_payslips():
    cases = load('payslips')
    assert len(cases) > 500
    check(cases, lambda c: c['result'], lambda c: attempt(lambda: compute_payslip(unpack(c['input']))))


def _rule(key, path):
    if key == 'ssf.rates' and path == 'employee':
        return D('0.11')
    if key == 'min_wage' and path == 'total':
        return D('19550')
    if key == 'text':
        return 'text'
    if key == 'flag':
        return True
    if key == 'raw':
        return 7
    return D(0)


# Same variables and rule() closure as the TS dump.
ENV = EvalEnv(vars={'BASIC': D('40000'), 'DA': D('7380'), 'paid_days': D(22), 'days_in_bs_month': D(31), 'is_female': True,
                    'regime': 'ssf', 'NEG': D('-0'), 'BIG': D('1e25'), 'SMALL': D('1e-20'), 'label': 'abc', 'num': 5,
                    'nul': None}, rule=_rule)


def _value(v):
    if isinstance(v, Decimal):
        return {'d': js_str(v)}
    if isinstance(v, bool):
        return {'b': v}
    if isinstance(v, str):
        return {'s': v}
    return {'other': typeof(v)}


def test_formula():
    cases = load('formula')
    assert len(cases) > 3000

    def run(c):
        src = c['src']
        return {'src': src, 'ast': attempt(lambda: parse_formula(src)),
                'value': attempt(lambda: _value(evaluate_formula(src, ENV))),
                'refs': attempt(lambda: list(formula_references(src))),
                'validate': validate_formula(src, ['BASIC', 'DA', 'NEG'])}
    check(cases, lambda c: c, run)


_DECIMAL_FIELDS = ('monthTaxable', 'recurringTaxable', 'futureOneOffs', 'ytdTaxable', 'ytdTdsPaid', 'monthRetirement',
                   'recurringRetirement', 'ytdRetirement', 'otherRetirementAnnual', 'lifePremium', 'healthPremium',
                   'medicalCost', 'donation')


def _tax_input(inp):
    t = {**inp, 'schedule': TAX_SCHEDULE_2082 if inp['schedule'] == 2082 else TAX_SCHEDULE_2083,
         'deductions': DEFAULT_RULES['tax.deductions']}
    for k in _DECIMAL_FIELDS:
        if k in t:
            t[k] = D(t[k])
    return t


def test_tax_sweep():
    data = load('tax')
    assert len(data['tds']) >= 1500
    check(data['tds'], lambda c: c['result'], lambda c: attempt(lambda: compute_tds(_tax_input(c['input']))))
    check(data['fractions'], lambda c: c['result'],
          lambda c: attempt(lambda: to_fixed(payable_fraction(D(c['paid']), c['days'], c['basis']), 10)))


def test_words_and_money():
    cases = load('words')
    assert len(cases) > 1000

    def run(c):
        v = c['v']
        finite = v not in ('Infinity', 'NaN')
        return {
            'v': v,
            'en': attempt(lambda: amount_in_words_en(v)) if finite else None,
            'ne': attempt(lambda: amount_in_words_ne(v)) if finite else None,
            'npr': attempt(lambda: format_npr(v)), 'nprDeva': attempt(lambda: format_npr(v, 'devanagari')),
            'D': attempt(lambda: js_str(D(v))), 'fixed2': attempt(lambda: to_fixed(D(v), 2)),
            'fixed0': attempt(lambda: to_fixed(D(v), 0)),
            'rupee': attempt(lambda: js_str(rupee(v))), 'paisa': attempt(lambda: js_str(paisa(v))),
        }
    check(cases, lambda c: c, run)


def test_optimum_cit():
    cases = load('optimum')
    check(cases, lambda c: c['result'], lambda c: attempt(lambda: optimum_cit(unpack(c['input']))))
