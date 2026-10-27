"""Port of apps/api/test/payroll-qa.test.ts through the JSON API. Each TS describe block is one test with its steps in
order; blocks that build on earlier ones run those first, as the TS file shares state."""
from __future__ import annotations

import re

import pytest

from engines.payroll.numeric import D

from hajiri.core import auth

from .helpers import attendance_for, execute, get, master_data, one, post, put, rows, set_policy

PASS = 'correct-horse-battery-staple'


def _login(email):
    return auth.login(email, PASS).token


@pytest.fixture
def w(demo, login_as):
    hr = login_as(['hr']).token
    b = login_as(['hr'])
    admin = login_as(['admin']).token
    master_data(demo)
    other = one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id']
    auth.create_user(company_id=str(other), employee_id=None, email='hr@other.local', name='Other HR', password=PASS, roles=['hr'])
    other_hr = _login('hr@other.local')
    std = get(hr, '/structures').body['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}), (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        post(hr, '/assignments', {'employeeId': emp, 'structureVersionId': std, 'from': '2026-01-01', 'baseAmounts': base})
    return {'demo': demo, 'hr': hr, 'hrB': b.token, 'hrB_id': b.user_id, 'admin': admin, 'other_hr': other_hr, 'other': str(other),
            'std': std, 'login_as': login_as}


def tenant_block(w):
    hr, demo, oh = w['hr'], w['demo'], w['other_hr']
    # HR of another company cannot read, verify or write declarations, assignments or payslips of this company
    put(hr, '/declarations', {'employeeId': demo['sita'], 'fiscalYearStart': 2083, 'lifePremium': '30000.00'})
    assert get(oh, '/declarations', {'employeeId': demo['sita'], 'fiscalYearStart': 2083}).status == 404
    assert post(oh, f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': 2083, 'decision': 'verified'}).status == 404
    assert put(oh, '/declarations', {'employeeId': demo['sita'], 'fiscalYearStart': 2083}).status == 404
    assert post(oh, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': w['std'], 'from': '2026-01-01',
                                     'baseAmounts': {'BASIC': '1.00'}}).status == 404
    # Our HR cannot assign a structure that belongs to another company.
    foreign = one("INSERT INTO salary_structure (company_id, code, name_en) VALUES (%s, 'X', 'X') RETURNING id", [w['other']])['id']
    fv = one("""INSERT INTO salary_structure_version (structure_id, version, components) VALUES (%s, 1, '[{"code":"BASIC"}]') RETURNING id""",
             [foreign])['id']
    assert post(hr, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': str(fv), 'from': '2026-02-01',
                                     'baseAmounts': {'BASIC': '1.00'}}).status == 404

    # rule versions are jurisdiction-wide: HR cannot add them, admin can; HR signs them off
    body = {'ruleKey': 'test.rule', 'validFromAd': '2026-01-01', 'validFromBs': '2082-09-17', 'value': {'x': 1}}
    assert post(hr, '/rules', body).status == 403
    created = post(w['admin'], '/rules', body)
    assert created.status == 201
    emp = w['login_as'](['employee']).token
    assert post(emp, f"/rules/{created.body['id']}/approve").status == 403
    assert post(hr, f"/rules/{created.body['id']}/approve").status == 200
    assert one('SELECT approved_by, approved_at FROM rule_version WHERE id = %s', [created.body['id']])['approved_at'] is not None
    assert post(hr, '/rules/00000000-0000-7000-8000-00000000ffff/approve').status == 404

    # period creation outside the calendar table is a 400, not a crash
    r = post(hr, '/periods', {'bsYear': 2099, 'bsMonth': 1})
    assert r.status == 400
    assert re.search('calendar', r.body['error'])


def guards_block(w) -> dict:
    hr, demo = w['hr'], w['demo']
    # only one regular run per period; a second one is refused while the first is still open
    period_id = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 5}).body['period']['id']
    run = post(hr, '/runs', {'periodId': period_id})
    assert run.status == 201
    run_id = run.body['id']
    again = post(hr, '/runs', {'periodId': period_id})
    assert again.status == 409
    assert re.search('continue that run', again.body['error'])

    # final settlement needs an exit date inside the period; active employees cannot be settled
    r = post(hr, '/runs', {'periodId': period_id, 'runType': 'final_settlement', 'employeeIds': [demo['ram']]})
    assert r.status == 400
    assert re.search('exit date', r.body['error'])

    # inputs reject unknown component codes and non-positive amounts
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'NOPE', 'amount': '10.00'}]}).status == 400
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '0.00'}]}).status == 400
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'GROSS', 'amount': '10.00'}]}).status == 400

    # HR can override pre-flight blockers with a reason (audited); compute without a reason is refused
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16', locked=False)
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16', locked=False)
    assert post(hr, f'/runs/{run_id}/compute').status == 409
    ok = post(hr, f'/runs/{run_id}/compute', {'overrideReason': 'month-end rush, lock follows'})
    assert ok.status == 200, ok
    assert ok.body['payslips'] == 2
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'preflight_override' AND row_id = %s", [run_id])) == 1

    # adding inputs after review sends the run back to computed; inputs after approval are refused
    assert post(hr, f'/runs/{run_id}/review').status == 200
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '1000.00'}]}).status == 201
    assert one('SELECT status FROM payroll_run WHERE id = %s', [run_id])['status'] == 'computed'
    assert post(hr, f'/runs/{run_id}/approve').status == 409
    post(hr, f'/runs/{run_id}/compute', {'overrideReason': 'month-end rush, lock follows'})
    post(hr, f'/runs/{run_id}/review')

    # separate-approver policy: when on, the HR user who prepared the run cannot approve it; another HR user can
    set_policy(demo['company_id'], separateApprover=True)
    own = post(hr, f'/runs/{run_id}/approve')
    assert own.status == 403
    assert re.search('different HR user', own.body['error'])
    assert one('SELECT status FROM payroll_run WHERE id = %s', [run_id])['status'] == 'reviewed'
    other = post(w['hrB'], f'/runs/{run_id}/approve')
    assert other.status == 200
    assert other.body['approved'] is True
    rr = one('SELECT status, checker_id FROM payroll_run WHERE id = %s', [run_id])
    assert rr['status'] == 'approved' and str(rr['checker_id']) == w['hrB_id']
    set_policy(demo['company_id'], separateApprover=False)
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '1.00'}]}).status == 409

    # locking a regular run locks the period; cross-company run access is a 404
    assert post(hr, f'/runs/{run_id}/lock').status == 200
    oh = w['other_hr']
    assert get(oh, f'/runs/{run_id}').status == 404
    assert get(oh, f'/runs/{run_id}/salary-sheet').status == 404
    ps = one('SELECT id FROM payslip WHERE run_id = %s LIMIT 1', [run_id])['id']
    assert get(oh, f'/payslips/{ps}').status == 404
    assert get(oh, f'/payslips/{ps}/html').status == 404
    assert get(oh, f'/runs/{run_id}/salary-sheet', {'format': 'csv'}).status == 404
    return {'run_id': run_id, 'period_id': period_id}


def test_tenant_isolation_and_authorisation(w):
    tenant_block(w)


def test_run_lifecycle_guards(w):
    guards_block(w)


def test_payslip_visibility(w):
    demo = w['demo']
    tenant_block(w)
    guards_block(w)
    # an employee of another company gets 404; a manager cannot read a direct report's payslips; self works
    fe = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Foreign') RETURNING id", [w['other']])['id']
    auth.create_user(company_id=w['other'], employee_id=str(fe), email='emp@other.local', name='Foreign', password=PASS, roles=['employee'])
    foreign = _login('emp@other.local')
    assert get(foreign, '/payslips', {'employeeId': demo['sita']}).status == 404
    assert get(foreign, '/declarations', {'employeeId': demo['sita'], 'fiscalYearStart': 2083}).status == 404
    # Ram manages Sita: he may see her attendance, but pay is HR/self only.
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s AND upper_inf(valid)', [demo['ram'], demo['sita']])
    manager = w['login_as'](['manager'], demo['ram']).token
    assert get(manager, '/payslips', {'employeeId': demo['sita']}).status == 403
    assert get(manager, '/payslips', {'employeeId': demo['ram']}).status == 200
    unrelated = w['login_as'](['employee'], demo['sita']).token
    assert get(unrelated, '/payslips', {'employeeId': demo['ram']}).status == 403


def test_loss_of_pay_from_attendance_flags(w):
    hr, demo = w['hr'], w['demo']
    # counts half-day leave without punches as 0.5 (1.0 if unpaid) using jsonb flag containment
    p = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 7})
    days = [
        ('2026-10-18', 'Absent', '[{"kind":"HalfDayLeave","half":"first_half"}]'),
        ('2026-10-19', 'Absent', '[{"kind":"HalfDayLeave","half":"second_half"},{"kind":"UnpaidLeave"}]'),
        ('2026-10-20', 'OnLeave', '[{"kind":"UnpaidLeave"}]'),
        ('2026-10-21', 'HalfDayLeave', '[{"kind":"UnpaidLeave"}]'),
        ('2026-10-22', 'OnLeave', '[]'),
        ('2026-10-25', 'Present', '[{"kind":"AutoClockOut"},{"kind":"Late","minutes":5}]'),
        ('2026-10-26', 'Incomplete', '[{"kind":"MissingOut"}]'),
    ]
    for d, status, flags in days:
        execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, flags, locked_at)
                   VALUES (%s, %s, 1, %s, 'h', %s::jsonb, now())""", [demo['sita'], d, status, flags])
    run = post(hr, '/runs', {'periodId': p.body['period']['id'], 'employeeIds': [demo['sita']]})
    assert run.status == 201
    c = post(hr, f"/runs/{run.body['id']}/compute", {'overrideReason': 'attendance partially entered for the LOP test'})
    assert c.status == 200, c
    r = get(hr, f"/runs/{run.body['id']}")
    # 0.5 + 1 + 1 + 0.5 + 0 + 0 + 0 = 3 days of LOP: the Incomplete day (missing punch-out) was worked, so it is paid
    assert r.body['payslips'][0]['lop_days'] == '3.00'
    assert r.body['payslips'][0]['paid_days'] == '27.00'
    pf = one('SELECT preflight FROM payroll_run WHERE id = %s', [run.body['id']])['preflight']
    inc = next(x for x in pf['warnings'] if x['code'] == 'incomplete_days')
    assert inc['count'] == 1 and inc['employees'] == ['E001: 1 day']


def test_outputs(w):
    hr, demo = w['hr'], w['demo']
    run_id = guards_block(w)['run_id']
    # the SSF sheet allocation sums to 31% of basic
    ssf = get(hr, f'/runs/{run_id}/ssf-sheet.csv')
    row = ssf.text.split('\r\n')[1].split(',')
    basic = D(row[3])
    alloc = D(row[6]) + D(row[7]) + D(row[8]) + D(row[9])
    assert abs(alloc - basic * D('0.31')) <= D('0.05')

    # payslip and salary sheet HTML escape user-controlled strings; CSV quotes commas
    execute('UPDATE employee SET name_en = %s, designation = %s WHERE id = %s',
            ['Sita <script>alert(1)</script>', '"><img src=x onerror=alert(1)>', demo['sita']])
    ps = one('SELECT id FROM payslip WHERE run_id = %s AND employee_id = %s', [run_id, demo['sita']])['id']
    html = get(hr, f'/payslips/{ps}/html')
    assert html.status == 200
    assert '<script>alert(1)</script>' not in html.text
    assert '&lt;script&gt;' in html.text
    assert '<img src=x' not in html.text
    sheet = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'html'})
    assert sheet.status == 200
    assert '<script>alert(1)</script>' not in sheet.text
    assert '&lt;script&gt;' in sheet.text
    assert '<img src=x' not in sheet.text
    execute("UPDATE employee SET name_en = 'Sharma, Sita' WHERE id = %s", [demo['sita']])
    csv = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'csv'})
    assert ',"Sharma, Sita",' in csv.text
    execute("UPDATE employee SET name_en = 'Sita Sharma', designation = NULL WHERE id = %s", [demo['sita']])


def test_final_settlement_scoping(w):
    hr, demo = w['hr'], w['demo']
    # locks only the settled employee's attendance and pro-rates the festival allowance to months served
    p = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 6})
    execute("UPDATE employee SET exit_date = '2026-10-10' WHERE id = %s", [demo['ram']])
    attendance_for(demo['ram'], '2026-09-17', '2026-10-10', locked=False)
    attendance_for(demo['sita'], '2026-09-17', '2026-10-16', locked=False)
    fs = post(hr, '/runs', {'periodId': p.body['period']['id'], 'runType': 'final_settlement', 'employeeIds': [demo['ram']]})
    assert fs.status == 201
    c = post(hr, f"/runs/{fs.body['id']}/compute")
    assert c.status == 200, c
    run = get(hr, f"/runs/{fs.body['id']}")
    d = get(hr, f"/payslips/{run.body['payslips'][0]['id']}")
    # Joined before the FY and left 10 Oct 2026: about 3 months of service -> 30,000 x 3/12 = 7,500.
    assert next(x for x in d.body['lines'] if x['code'] == 'FESTIVAL')['amount'] == '7500.00'
    post(hr, f"/runs/{fs.body['id']}/review")
    assert post(hr, f"/runs/{fs.body['id']}/approve").status == 200
    assert post(hr, f"/runs/{fs.body['id']}/lock").status == 200
    assert one("""SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = %s AND local_date BETWEEN '2026-09-17' AND '2026-10-16'
                  AND locked_at IS NOT NULL""", [demo['sita']])['n'] == 0
    assert one("""SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = %s AND local_date BETWEEN '2026-09-17' AND '2026-10-10'
                  AND locked_at IS NULL""", [demo['ram']])['n'] == 0
    assert one('SELECT attendance_locked_at FROM payroll_period WHERE id = %s', [p.body['period']['id']])['attendance_locked_at'] is None
