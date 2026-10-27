"""Port of the 'payroll' block of apps/api/test/fixes-payroll-auth.test.ts (the other blocks of that file belong to the
overtime, employees, settings and sign-in slices). One test with the TS steps in order."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .helpers import execute, get, patch, post


def test_payroll_regressions(demo, login_as):
    hr = login_as(['hr']).token
    std = get(hr, '/structures').body['structures'][0]['versions'][0]['id']

    # a salary revision inside the period is day-weighted (69,500 for 15 days, 60,000 for 16 days of Bhadra 2083)
    assert post(hr, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': std, 'from': '2026-01-01',
                                     'baseAmounts': {'BASIC': '69500.00'}}).status == 201
    assert post(hr, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': std, 'from': '2026-09-01',
                                     'baseAmounts': {'BASIC': '60000.00'}}).status == 201
    d = date(2026, 8, 17)
    while d <= date(2026, 9, 16):
        execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at)
                   VALUES (%s, %s, 1, 'Present', 480, 'h', now())""", [demo['sita'], d.isoformat()])
        d += timedelta(days=1)
    period = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 5}).body['period']
    run = post(hr, '/runs', {'periodId': period['id'], 'runType': 'supplementary', 'employeeIds': [demo['sita']]})
    # A regular run scoped to one employee keeps this test independent of the other demo employee's data.
    execute("UPDATE payroll_run SET run_type = 'regular' WHERE id = %s", [run.body['id']])
    c = post(hr, f"/runs/{run.body['id']}/compute", {'overrideReason': 'master data not filled in this test'})
    assert c.status == 200, c
    slip = get(hr, f"/runs/{run.body['id']}").body['payslips'][0]
    detail = get(hr, f"/payslips/{slip['id']}")
    # (69,500 x 15 + 60,000 x 16) / 31 = 64,596.77
    assert next(x for x in detail.body['lines'] if x['code'] == 'BASIC')['amount'] == '64596.77'
    assert 'SalaryRevised' in slip['flags']
    assert detail.body['payslip']['explain'][0]['code'] == 'SALARY_REVISION'
    assert re.search(r'BASIC 69500\.00 for 15 days from 2026-08-17; BASIC 60000\.00 for 16 days from 2026-09-01',
                     detail.body['payslip']['explain'][0]['text'])

    # Inputs for someone outside a scoped run are refused instead of silently dropped.
    out = post(hr, f"/runs/{run.body['id']}/inputs", {'inputs': [{'employeeId': demo['ram'], 'componentCode': 'BONUS', 'amount': '100.00'}]})
    assert out.status == 400
    assert re.search('not in it', out.body['error'])
    assert post(hr, f"/runs/{run.body['id']}/inputs", {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '100.00'}]}).status == 201

    # salary amounts for components outside the structure are refused, listing them
    r = post(hr, '/assignments', {'employeeId': demo['ram'], 'structureVersionId': std, 'from': '2026-01-01',
                                  'baseAmounts': {'BASIC': '30000.00', 'NOTACOMP': '100.00'}})
    assert r.status == 400
    assert re.search('NOTACOMP', r.body['error'])
    assert post(hr, '/assignments', {'employeeId': demo['ram'], 'structureVersionId': std, 'from': '2026-01-01', 'baseAmounts': {}}).status == 400

    # formulas that refer back to themselves, directly or through another component, are refused
    self_ = post(hr, '/components', {'code': 'LOOP', 'nameEn': 'Loop', 'type': 'earning', 'formula': 'LOOP + 1'})
    assert self_.status == 400
    assert re.search('LOOP → LOOP', self_.body['error'])
    assert post(hr, '/components', {'code': 'CYA', 'nameEn': 'A', 'type': 'earning', 'formula': 'BASIC * 0.1'}).status == 201
    assert post(hr, '/components', {'code': 'CYB', 'nameEn': 'B', 'type': 'earning', 'formula': 'CYA + 1'}).status == 201
    cyc = patch(hr, '/components/CYA', {'formula': 'CYB + 1'})
    assert cyc.status == 400
    assert re.search('CYA → CYB → CYA', cyc.body['error'])
    assert patch(hr, '/components/CYA', {'formula': 'BASIC * 0.2'}).status == 200

    # a NUL byte in a path or body is a 400, not a server error
    assert patch(hr, '/components/%00', {'nameEn': 'x'}).status == 400
    assert patch(hr, '/components/CYB', {'nameEn': 'bad\u0000name'}).status == 400
