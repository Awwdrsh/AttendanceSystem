"""Port of apps/api/test/payroll.test.ts through the JSON API, as the TS test. Each TS describe block is one test with
its `it` steps in order; the lifecycle block runs the setup block first, as the TS file shares state between them."""
from __future__ import annotations

import re

import pytest

from engines.payroll.numeric import D

from .helpers import attendance_for, execute, get, master_data, one, patch, post, put, rejects, rows, set_policy


@pytest.fixture
def w(demo, login_as):
    h = login_as(['hr'])
    h2 = login_as(['hr'])
    sita = login_as(['employee'], demo['sita']).token
    ram = login_as(['employee'], demo['ram']).token
    master_data(demo)
    execute("UPDATE employee SET regime = 'pf', ssf_id = NULL, pf_id = 'PF-22' WHERE id = %s", [demo['ram']])
    return {'demo': demo, 'hr': h.token, 'hr_id': h.user_id, 'hr2': h2.token, 'hr2_id': h2.user_id, 'sita': sita, 'ram': ram}


def setup_block(w) -> dict:
    hr, demo = w['hr'], w['demo']
    # seeded rule versions resolve by date and statutory keys are locked
    r = get(hr, '/rules', {'key': 'tax.schedule', 'asOf': '2026-08-01'})
    assert r.status == 200
    assert len(r.body['rules']) == 1
    assert r.body['rules'][0]['locked'] is True and r.body['rules'][0]['valid_from_ad'] == '2026-07-17'
    assert len(r.body['rules'][0]['value']['bands']['all']) == 5
    old = get(hr, '/rules', {'key': 'tax.schedule', 'asOf': '2026-01-01'})
    assert len(old.body['rules'][0]['value']['bands']['single']) == 6
    ssf = get(hr, '/rules', {'key': 'ssf.deposit_days', 'asOf': '2026-01-01'})
    assert ssf.body['rules'][0]['value']['days'] == 25

    # components validate formulas; statutory components cannot be rewritten
    bad = post(hr, '/components', {'code': 'TRANSPORT', 'nameEn': 'Transport', 'type': 'earning', 'formula': 'BASIC * HRA_X'})
    assert bad.status == 400
    assert re.search(r'unknown identifier HRA_X', bad.body['error'])
    ok = post(hr, '/components', {'code': 'TRANSPORT', 'nameEn': 'Transport', 'type': 'earning', 'formula': 'min(BASIC * 0.05, 2000)',
                                  'dependsOnPaymentDays': True})
    assert ok.status == 201
    assert patch(hr, '/components/SSF_EE', {'formula': 'BASIC * 0.05'}).status == 403
    assert get(w['sita'], '/components').status == 403

    # assigns the standard structure with base amounts (effective-dated) and freezes the version
    s = get(hr, '/structures')
    std = s.body['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}), (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        assert post(hr, '/assignments', {'employeeId': emp, 'structureVersionId': std, 'from': '2026-01-01', 'baseAmounts': base}).status == 201
    again = post(hr, '/assignments', {'employeeId': demo['ram'], 'structureVersionId': std, 'from': '2026-10-01',
                                      'baseAmounts': {'BASIC': '32000.00', 'DA': '7380.00'}})
    assert again.status == 201
    lst = get(hr, '/assignments', {'employeeId': demo['ram']})
    assert [x['valid'] for x in lst.body['assignments']] == ['[2026-10-01,)', '[2026-01-01,2026-10-01)']
    assert one('SELECT frozen_at FROM salary_structure_version WHERE id = %s', [std])['frozen_at'] is not None

    # employee submits a tax declaration; HR verifies it
    assert put(w['sita'], '/declarations', {'fiscalYearStart': 2083, 'lifePremium': '30000.00'}).status == 200
    assert put(w['sita'], '/declarations', {'employeeId': demo['ram'], 'fiscalYearStart': 2083}).status == 403
    assert post(hr, f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': 2083, 'decision': 'verified'}).status == 200
    g = get(w['sita'], '/declarations', {'fiscalYearStart': 2083})
    assert g.body['declaration']['status'] == 'verified' and g.body['declaration']['life_premium'] == '30000.00'

    # creates a loan for Ram
    ln = post(hr, '/loans', {'employeeId': demo['ram'], 'principal': '12000.00', 'emi': '5000.00', 'startBsYear': 2083, 'startBsMonth': 5})
    assert ln.status == 201
    assert ln.body['installments'] == 3
    return {'std': std}


def test_payroll_setup(w):
    setup_block(w)


def test_payroll_run_lifecycle_bhadra_2083(w):
    setup_block(w)
    hr, demo, sita_tok, ram_tok = w['hr'], w['demo'], w['sita'], w['ram']

    # creates the period from the BS calendar
    p = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 5})
    assert p.status == 201
    assert {k: p.body['period'][k] for k in ('start_ad', 'end_ad', 'days')} == {'start_ad': '2026-08-17', 'end_ad': '2026-09-16', 'days': 31}
    period_id = p.body['period']['id']

    # pre-flight blocks on unlocked attendance and missing master data, then passes
    run = post(hr, '/runs', {'periodId': period_id})
    assert run.status == 201
    run_id = run.body['id']
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16', ['2026-09-01', '2026-09-02', '2026-09-03'], locked=False)
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16', [], locked=False)
    pf = post(hr, f'/runs/{run_id}/preflight')
    assert pf.body['ok'] is False
    assert 'attendance_not_locked' in [b['code'] for b in pf.body['blockers']]
    assert post(hr, f'/runs/{run_id}/compute').status == 409
    lock = post(hr, f'/runs/{run_id}/lock-attendance')
    assert lock.body['locked'] == 62
    pf = post(hr, f'/runs/{run_id}/preflight')
    assert pf.body['ok'] is True
    assert pf.body['info']['employees'] == 2

    # one-time inputs, compute: lines and totals match the engine
    assert execute("""INSERT INTO ot_slip (employee_id, local_date, detected_min, approved_min, status, day_type)
                      VALUES (%s, '2026-08-20', 600, 600, 'approved', 'working')""", [demo['ram']]) == 1
    assert post(hr, f'/runs/{run_id}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '5000.00',
                                                            'note': 'spot bonus'}]}).status == 201
    c = post(hr, f'/runs/{run_id}/compute')
    assert c.status == 200, c
    assert c.body['payslips'] == 2
    r = get(hr, f'/runs/{run_id}')
    sita = next(x for x in r.body['payslips'] if x['employee_id'] == demo['sita'])
    ram = next(x for x in r.body['payslips'] if x['employee_id'] == demo['ram'])
    # Sita: 3 absent days of 31 -> BASIC 40,000 x 28/31 = 36,129.03; ALLOWANCE 18,064.52; BONUS 5,000; SSF 11% -> 3,974.
    assert {k: sita[k] for k in ('paid_days', 'lop_days', 'gross', 'employee_statutory')} == {
        'paid_days': '28.00', 'lop_days': '3.00', 'gross': '59193.55', 'employee_statutory': '3974.00'}
    detail = get(hr, f"/payslips/{sita['id']}")
    ln = lambda code: next((x['amount'] for x in detail.body['lines'] if x['code'] == code), None)  # noqa: E731
    assert ln('BASIC') == '36129.03'
    assert ln('BONUS') == '5000.00'
    assert ln('SSF_ER') == '7226.00'
    assert (D(detail.body['payslip']['net']) % 1).is_zero()
    assert detail.body['tax']['fiscalYearStart'] == 2083
    assert detail.body['tax']['insuranceDeduction'] == '30000.00'
    # Ram: PF regime, OT 10 h at 30,000/208 x 1.5 = 2,163; loan EMI 5,000.
    assert ram['paid_days'] == '31.00' and ram['ot_hours'] == '10.00'
    rd = get(hr, f"/payslips/{ram['id']}")
    rl = lambda code: next((x['amount'] for x in rd.body['lines'] if x['code'] == code), None)  # noqa: E731
    assert rl('OT') == '2163.00'
    assert rl('PF_EE') == '3000.00'
    assert rl('GRATUITY') == '2499.00'
    assert rl('LOAN') == '5000.00'
    assert 'FirstPayslip' in ram['flags']
    assert r.body['run']['totals']['headcount'] == '2'

    # draft salary sheet: one row per employee, a column per component, totals that match the run
    sheet = get(hr, f'/runs/{run_id}/salary-sheet')
    assert sheet.status == 200
    assert sheet.body['run']['final'] is False and sheet.body['run']['status'] == 'computed'
    assert {k: sheet.body['period'][k] for k in ('label', 'bsYear', 'bsMonth')} == {'label': 'Bhadra 2083', 'bsYear': 2083, 'bsMonth': 5}
    assert [x['empCode'] for x in sheet.body['rows']] == ['E001', 'E002']
    codes = lambda k: [x['code'] for x in sheet.body['columns'][k]]  # noqa: E731
    assert {'BASIC', 'BONUS', 'OT'} <= set(codes('earnings'))
    assert {'SSF_EE', 'PF_EE', 'LOAN'} <= set(codes('deductions'))
    assert {'SSF_ER', 'GRATUITY'} <= set(codes('employer'))
    s0 = sheet.body['rows'][0]
    assert s0['earnings']['BASIC'] == '36129.03'
    assert s0['employer']['SSF_ER'] == '7226.00'
    assert s0['bankAccount'] == '****0012'
    assert f"{D(s0['costToCompany']):.2f}" == f"{D(s0['gross']) + D(s0['employerTotal']):.2f}"
    for row in sheet.body['rows']:
        earned = sum((D(x) for x in row['earnings'].values()), D(0))
        deducted = sum((D(x) for x in row['deductions'].values()), D(0))
        assert f'{earned:.2f}' == row['gross']
        assert f'{deducted:.2f}' == row['totalDeductions']
        assert f"{D(row['gross']) - D(row['totalDeductions']) + D(row['rounding']):.2f}" == row['net']
        assert (D(row['net']) % 1).is_zero()
    r = get(hr, f'/runs/{run_id}')
    assert sheet.body['totals']['net'] == f"{D(r.body['run']['totals']['net']):.2f}"
    assert sheet.body['totals']['gross'] == f"{D(r.body['run']['totals']['gross']):.2f}"
    csv = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'csv'})
    assert 'salary-sheet-2083-05-draft.csv' in csv.headers['Content-Disposition']
    lines = csv.text.lstrip('﻿').split('\r\n')
    assert re.search(r'salary sheet, Bhadra 2083 .*DRAFT', lines[0])
    assert re.match(r'^S\.N\.,Employee code,Name,Designation,Department,Paid days,Unpaid days,OT hours,', lines[1])
    assert re.search(r',Total deductions,Rounding,Net pay,', lines[1])
    assert re.match(r'^1,E001,Sita Sharma,', lines[2])
    assert re.match(r'^,,Total \(2\),', lines[4])
    html = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'html'})
    assert 'Salary sheet' in html.text
    assert 'DRAFT' in html.text
    assert 'Prepared by' in html.text
    assert get(sita_tok, f'/runs/{run_id}/salary-sheet').status == 403
    assert get(hr, '/runs/00000000-0000-7000-8000-00000000ffff/salary-sheet').status == 404

    # HR approves (no finance step); an optional policy requires a different HR approver; then lock is immutable
    assert post(hr, f'/runs/{run_id}/approve').status == 409  # not reviewed yet
    assert post(hr, f'/runs/{run_id}/review', {'notes': 'checked variances'}).status == 200
    assert post(sita_tok, f'/runs/{run_id}/approve').status == 403
    set_policy(demo['company_id'], separateApprover=True)
    assert str(one('SELECT maker_id FROM payroll_run WHERE id = %s', [run_id])['maker_id']) == w['hr_id']
    assert post(hr, f'/runs/{run_id}/approve').status == 403
    set_policy(demo['company_id'], separateApprover=False)
    ap = post(w['hr2'], f'/runs/{run_id}/approve')
    assert ap.status == 200
    assert ap.body['approved'] is True
    assert re.match(r'^sha256:[0-9a-f]{64}$', ap.body['signature'])
    assert post(hr, f'/runs/{run_id}/lock').status == 200
    rr = one('SELECT status, locked_at, checker_id FROM payroll_run WHERE id = %s', [run_id])
    assert rr['status'] == 'locked' and str(rr['checker_id']) == w['hr2_id']
    assert re.search('locked', rejects('UPDATE payslip SET net = 1 WHERE run_id = %s', [run_id]))
    assert re.search('locked', rejects('DELETE FROM payslip_line WHERE payslip_id IN (SELECT id FROM payslip WHERE run_id = %s)', [run_id]))
    assert re.search('immutable', rejects("UPDATE payroll_run SET status = 'draft' WHERE id = %s", [run_id]))
    assert post(hr, f'/runs/{run_id}/compute').status == 409
    assert one("SELECT status FROM ot_slip WHERE employee_id = %s AND local_date = '2026-08-20'", [demo['ram']])['status'] == 'posted'
    assert len(rows('SELECT amount FROM loan_installment')) == 1

    # final salary sheet names who prepared and approved it; SSF sheet; finance-only exports are gone
    sheet = get(hr, f'/runs/{run_id}/salary-sheet')
    assert sheet.body['run']['final'] is True and sheet.body['run']['status'] == 'locked'
    assert sheet.body['run']['approvedBy']
    csv = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'csv'})
    assert 'salary-sheet-2083-05.csv' in csv.headers['Content-Disposition']
    assert 'DRAFT' not in csv.text
    html = get(hr, f'/runs/{run_id}/salary-sheet', {'format': 'html'})
    assert 'class="draft"' not in html.text
    assert re.search(r'Run locked \d+ \w+ 20\d\d\.', html.text)
    ssf = get(hr, f'/runs/{run_id}/ssf-sheet.csv')
    assert re.match(r'^E001,10000000001,Sita Sharma,36129\.03,3974\.00,7226\.00,361\.29,505\.81,97\.55,10235\.35,11200\.00$',
                    ssf.text.split('\r\n')[1]), ssf.text
    for method, path in (('post', 'bank-file'), ('get', 'etds.csv'), ('get', 'journal.json'), ('post', 'disburse'), ('post', 'file')):
        res = post(hr, f'/runs/{run_id}/{path}') if method == 'post' else get(hr, f'/runs/{run_id}/{path}')
        assert res.status == 404

    # publish notifies employees; employees see only their own published payslips; HTML renders in English
    assert len(get(sita_tok, '/payslips').body['payslips']) == 0
    assert post(hr, f'/runs/{run_id}/publish').body['published'] == 2
    mine = get(sita_tok, '/payslips')
    assert len(mine.body['payslips']) == 1
    pid = mine.body['payslips'][0]['id']
    assert get(ram_tok, f'/payslips/{pid}').status == 403
    assert get(ram_tok, '/payslips', {'employeeId': demo['sita']}).status == 403
    html = get(sita_tok, f'/payslips/{pid}/html')
    assert html.status == 200
    assert '<div class="sub">Payslip</div>' in html.text
    assert 'Bhadra 2083' in html.text
    assert '*****6789' in html.text
    assert re.search('Rupees Only', html.text)
    assert not re.search('[ऀ-ॿ]', html.text)
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'payslip.published' AND employee_id = %s",
               [demo['sita']])['n'] > 0

    # a second regular run for a locked period is refused; a supplementary run with inputs works
    assert post(hr, '/runs', {'periodId': period_id}).status == 409
    sup = post(hr, '/runs', {'periodId': period_id, 'runType': 'supplementary', 'employeeIds': [demo['sita']]})
    assert sup.status == 201
    post(hr, f"/runs/{sup.body['id']}/inputs", {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'ARREARS', 'amount': '1500.00'}]})
    c = post(hr, f"/runs/{sup.body['id']}/compute")
    assert c.status == 200, c
    assert c.body['payslips'] == 1
    # A supplementary run pays the one-time line only; its projection reuses the regular run's recurring figures.
    sr = get(hr, f"/runs/{sup.body['id']}")
    sd = get(hr, f"/payslips/{sr.body['payslips'][0]['id']}")
    codes = [x['code'] for x in sd.body['lines']]
    assert 'ARREARS' in codes
    assert 'BASIC' not in codes and 'SSF_EE' not in codes and 'LOAN' not in codes
    assert sd.body['payslip']['gross'] == '1500.00'
    assert sd.body['tax']['recurringTaxable'] == '68000.00'
    assert 'NoRecurringReference' not in sr.body['payslips'][0]['flags']

    # arrears run: raised salary effective in the locked period pays the difference
    execute("""UPDATE salary_assignment SET base_amounts = '{"BASIC":"45000.00","ALLOWANCE":"20000.00"}' WHERE employee_id = %s""", [demo['sita']])
    ar = post(hr, '/runs', {'periodId': period_id, 'runType': 'arrears', 'referencesRunId': run_id, 'employeeIds': [demo['sita']]})
    assert ar.status == 201
    c = post(hr, f"/runs/{ar.body['id']}/compute")
    assert c.status == 200, c
    rr = get(hr, f"/runs/{ar.body['id']}")
    d = get(hr, f"/payslips/{rr.body['payslips'][0]['id']}")
    # 5,000 raise x 28/31 paid days = 4,516.13
    assert next(x for x in d.body['lines'] if x['code'] == 'ARREARS')['amount'] == '4516.13'

    # final settlement run for an exiting employee encashes leave and recovers loans in full
    execute("UPDATE employee SET exit_date = '2026-10-10' WHERE id = %s", [demo['ram']])
    lt = one("SELECT id FROM leave_type WHERE company_id = %s AND code = 'HOME'", [demo['company_id']])
    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-09-30', 12, 'allocation')""",
            [demo['ram'], lt['id']])
    p6 = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 6})
    attendance_for(demo['ram'], '2026-09-17', '2026-10-10')
    fs = post(hr, '/runs', {'periodId': p6.body['period']['id'], 'runType': 'final_settlement', 'employeeIds': [demo['ram']], 'noticePay': '-3000.00'})
    assert fs.status == 201
    c = post(hr, f"/runs/{fs.body['id']}/compute")
    assert c.status == 200, c
    rr = get(hr, f"/runs/{fs.body['id']}")
    d = get(hr, f"/payslips/{rr.body['payslips'][0]['id']}")
    ln = lambda code: next((x['amount'] for x in d.body['lines'] if x['code'] == code), None)  # noqa: E731
    assert d.body['payslip']['paid_days'] == '24.00'
    assert ln('LEAVE_ENCASH') == '12387.10'   # 12 days x 32,000 / 31
    assert ln('NOTICE_DEDUCTION') == '3000.00'
    assert ln('LOAN') == '7000.00'           # remaining after one 5,000 instalment
    assert ln('FESTIVAL') is not None        # pro-rated festival allowance (Sec 37(3))
    assert 'LastMonth' in rr.body['payslips'][0]['flags']

    # bonus run allocates the 10% pool by wages with Bonus Act caps
    p7 = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 7})
    b = post(hr, '/runs/bonus', {'periodId': p7.body['period']['id'], 'fiscalYearStart': 2083, 'netProfit': '1000000.00'})
    assert b.status == 201
    assert b.body['pool'] == '100000.00'
    assert len(b.body['allocation']) == 2
    c = post(hr, f"/runs/{b.body['id']}/compute")
    assert c.status == 200, c
    assert c.body['payslips'] == 1  # Ram exited before Kartik, so only Sita is paid in this period
