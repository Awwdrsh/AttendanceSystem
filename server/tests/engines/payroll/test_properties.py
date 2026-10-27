"""Port of src/properties.test.ts (plan §11.11): engine invariants over generated scenarios (fast-check -> hypothesis)."""
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engines.payroll import D, compute_payslip
from engines.payroll.fixtures import base_input
from engines.payroll.numeric import to_fixed


def money(max_: int):
    return st.integers(min_value=0, max_value=max_ * 100).map(lambda p: f'{p // 100}.{p % 100:02d}')


scenario = st.fixed_dictionaries({
    'basic': st.integers(min_value=10_000, max_value=500_000).map(str),
    'allowance': money(200_000),
    'paidDays': st.integers(min_value=0, max_value=31),
    'otHours': st.integers(min_value=0, max_value=40).map(str),
    'regime': st.sampled_from(['ssf', 'pf', 'none']),
    'gender': st.sampled_from(['male', 'female']),
    'marital': st.sampled_from(['single', 'couple']),
    'fy': st.sampled_from([2082, 2083]),
    'monthIndex': st.integers(min_value=1, max_value=12),
    'life': money(100_000),
    'loanEmi': money(20_000),
})


def build(x):
    return base_input(
        fy=x['fy'], period={'bsYear': x['fy'], 'bsMonth': ((x['monthIndex'] + 2) % 12) + 1, 'daysInMonth': 31,
                            'fiscalYearStart': x['fy'], 'monthIndexInFy': x['monthIndex']},
        base={'BASIC': x['basic'], 'ALLOWANCE': x['allowance']},
        attendance={'paidDays': str(x['paidDays']), 'lopDays': str(31 - x['paidDays']), 'otHours': x['otHours']},
        employee={**base_input()['employee'], 'regime': x['regime'], 'gender': x['gender'], 'maritalStatus': x['marital']},
        declaration={**base_input()['declaration'], 'lifePremium': x['life']},
        loans=[{'id': 'L', 'emi': x['loanEmi'], 'remaining': '999999'}] if D(x['loanEmi']) > 0 else [],
    )


SETTINGS = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow])


@settings(max_examples=300, **SETTINGS)
@given(scenario)
def test_earnings_minus_deductions_is_net_and_bounds_hold(s):
    r = compute_payslip(build(s))
    earn = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'earning'), D(0))
    ded = sum((D(l['amount']) for l in r['lines'] if l['type'] == 'deduction'), D(0))
    assert to_fixed(earn - ded, 2) == r['net']
    # Net may exceed gross only by the half-rupee rounding adjustment when there are no deductions.
    assert D(r['gross']) + D('0.5') >= D(r['net'])
    assert abs(D(r['gross']) - D(r['totalDeductions']) - D(r['net'])) <= D('0.5')
    assert D(r['net']) >= 0
    assert all(D(l['amount']) >= 0 for l in r['lines'])


@settings(max_examples=100, **SETTINGS)
@given(scenario)
def test_recompute_is_idempotent(s):
    assert compute_payslip(build(s)) == compute_payslip(build(s))


@settings(max_examples=200, **SETTINGS)
@given(scenario, st.integers(min_value=1, max_value=100_000))
def test_tds_is_monotonic_in_income(s, extra):
    low = compute_payslip(build(s))
    high = compute_payslip(build({**s, 'basic': str(int(s['basic']) + extra)}))
    assert D(high['tds']) >= D(low['tds'])


@settings(max_examples=100, **SETTINGS)
@given(st.integers(min_value=1, max_value=31), st.integers(min_value=10_000, max_value=300_000))
def test_proration_across_join_exit_pair_sums_to_full_month(k, basic):
    def basic_line(paid):
        r = compute_payslip(base_input(base={'BASIC': str(basic)}, attendance={'paidDays': str(paid), 'lopDays': '0', 'otHours': '0'}))
        return D(next((l['amount'] for l in r['lines'] if l['code'] == 'BASIC'), 0))
    assert abs(basic_line(k) + basic_line(31 - k) - basic) <= D('0.01')
