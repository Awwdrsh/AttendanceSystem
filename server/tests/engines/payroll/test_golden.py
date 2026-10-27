"""Golden fixtures (plan §11.11; port of src/golden.test.ts): every case must reproduce exactly."""
import json
from pathlib import Path

import pytest

from engines.payroll import compute_payslip

DIR = Path(__file__).parent / 'golden'
FILES = sorted(p.name for p in DIR.glob('*.json'))


def test_has_at_least_20_fixtures():
    assert len(FILES) >= 20


@pytest.mark.parametrize('name', FILES)
def test_fixture(name):
    fx = json.loads((DIR / name).read_text(encoding='utf-8'))
    r = compute_payslip(fx['input'])
    assert {
        'gross': r['gross'], 'taxable': r['taxable'], 'tds': r['tds'], 'employeeStatutory': r['employeeStatutory'],
        'employerStatutory': r['employerStatutory'], 'net': r['net'], 'annualTax': r['tax']['annualTax'],
        'lines': [{'code': l['code'], 'type': l['type'], 'amount': l['amount']} for l in r['lines']], 'flags': r['flags'],
    } == fx['expected']
