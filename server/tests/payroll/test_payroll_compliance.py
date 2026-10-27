"""Port of apps/api/test/payroll-compliance.test.ts through the JSON API. The TS file shares state between its describe
blocks (opening balances feed the Bhadra run, whose hold is released in Ashwin, ...), so it is one ordered scenario
here, split into the TS blocks as functions; each test runs the blocks it depends on first."""
from __future__ import annotations

import re

import pytest

from engines.payroll.numeric import D

from .helpers import attendance_for, execute, get, master_data, one, post, post_text, put, set_policy


@pytest.fixture
def w(demo, login_as):
    hr = login_as(['hr']).token
    mgr = login_as(['manager']).token
    sita = login_as(['employee'], demo['sita']).token
    ram = login_as(['employee'], demo['ram']).token
    master_data(demo, pans=('123456701', '123456702'), ssf=('100000000001', '100000000002'), banks=('0012345601', '0012345602'))
    version = get(hr, '/structures').body['structures'][0]['versions'][0]['id']
    post(hr, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': version, 'from': '2026-01-01',
                              'baseAmounts': {'BASIC': '60000.00', 'ALLOWANCE': '140000.00'}})
    post(hr, '/assignments', {'employeeId': demo['ram'], 'structureVersionId': version, 'from': '2026-01-01',
                              'baseAmounts': {'BASIC': '30000.00', 'DA': '7380.00'}})
    attendance_for(demo['sita'], '2026-08-17', '2026-10-17')
    attendance_for(demo['ram'], '2026-08-17', '2026-10-17')
    return {'demo': demo, 'hr': hr, 'mgr': mgr, 'sita': sita, 'ram': ram}


def run_through(w, period, before_lock=None) -> str:
    hr = w['hr']
    p = post(hr, '/periods', period)
    run = post(hr, '/runs', {'periodId': p.body['period']['id']})
    assert run.status == 201, run
    c = post(hr, f"/runs/{run.body['id']}/compute")
    assert c.status == 200, c
    if before_lock:
        before_lock(run.body['id'])
    assert post(hr, f"/runs/{run.body['id']}/review", {}).status == 200
    assert post(hr, f"/runs/{run.body['id']}/approve").status == 200
    assert post(hr, f"/runs/{run.body['id']}/lock").status == 200
    return run.body['id']


def opening_block(w):
    hr, demo = w['hr'], w['demo']
    # HR records prior income for a mid-year joiner; employees and managers cannot
    body = {'employeeId': demo['ram'], 'fiscalYearStart': 2083, 'gross': '75000.00', 'taxable': '75000.00', 'tdsPaid': '750.00',
            'retirementEmployee': '6600.00', 'retirementEmployer': '0', 'months': 1, 'source': 'previous_employer'}
    assert put(w['ram'], '/opening-balances', body).status == 403
    assert put(w['mgr'], '/opening-balances', body).status == 403
    assert put(hr, '/opening-balances', {**body, 'taxable': '999999.00'}).status == 400
    assert put(hr, '/opening-balances', {**body, 'employeeId': '00000000-0000-7000-8000-0000000000ff'}).status == 404
    assert put(hr, '/opening-balances', body).status == 200
    lst = get(hr, '/opening-balances', {'fiscalYearStart': 2083})
    assert len(lst.body['balances']) == 1
    b0 = lst.body['balances'][0]
    assert isinstance(b0['emp_code'], str) and b0['taxable'] == '75000.00' and b0['tds_paid'] == '750.00'

    # CSV import is all or nothing and reports row errors
    code = one('SELECT emp_code FROM employee WHERE id = %s', [demo['sita']])['emp_code']
    bad = f'emp_code,fiscal_year_start,gross,taxable,tds_paid\n{code},2082,10000,10000,100\nNOPE,2082,1,1,0\n'
    r1 = post_text(hr, '/opening-balances/import', bad)
    assert r1.status == 400
    assert r1.body['details']['errors'][0]['row'] == 3
    assert one('SELECT count(*)::int AS n FROM payroll_opening_balance WHERE fiscal_year_start = 2082')['n'] == 0
    good = f'emp_code,fiscal_year_start,gross,taxable,tds_paid,festival_paid\n{code},2082,"1,20,000",120000,1200,yes\n'
    r2 = post_text(hr, '/opening-balances/import', good)
    assert r2.status == 200, r2
    assert r2.body['imported'] == 1
    row = one('SELECT gross::text, festival_paid FROM payroll_opening_balance WHERE employee_id = %s AND fiscal_year_start = 2082', [demo['sita']])
    assert row == {'gross': '120000.00', 'festival_paid': True}


def held_block(w) -> dict:
    hr, demo = w['hr'], w['demo']
    out: dict = {}

    # opening balances flow into year-to-date; a hold is listed in pre-flight; release needs a locked run
    def before(run_id):
        slip = one("SELECT inputs_snapshot->'input'->'ytd' AS ytd FROM payslip WHERE run_id = %s AND employee_id = %s", [run_id, demo['ram']])
        assert slip['ytd']['taxable'] == '75000.00' and slip['ytd']['tdsPaid'] == '750.00'
        assert post(w['sita'], f'/runs/{run_id}/holds', {'employeeId': demo['sita'], 'reason': 'documents pending'}).status == 403
        h = post(hr, f'/runs/{run_id}/holds', {'employeeId': demo['sita'], 'reason': 'documents pending'})
        assert h.status == 201, h
        assert post(hr, f'/runs/{run_id}/holds', {'employeeId': demo['sita'], 'reason': 'again'}).status == 409
        assert post(hr, f"/holds/{h.body['id']}/release", {'mode': 'payroll'}).status == 409
        pf = post(hr, f'/runs/{run_id}/preflight')
        assert 'pay_held' in [x['code'] for x in pf.body['warnings']]
        # Recomputing keeps the hold and its amount follows the net.
        assert post(hr, f'/runs/{run_id}/compute').status == 200
        after = one("""SELECT h.amount::text, ps.net::text FROM payroll_hold h JOIN payslip ps ON ps.run_id = h.run_id AND ps.employee_id = h.employee_id
                       WHERE h.id = %s""", [h.body['id']])
        assert after['amount'] == after['net']
    out['bhadra'] = run_through(w, {'bsYear': 2083, 'bsMonth': 5}, before)
    holds = get(hr, '/holds')
    assert len(holds.body['holds']) == 1
    assert holds.body['holds'][0]['status'] == 'held' and holds.body['holds'][0]['reason'] == 'documents pending'

    # the employee sees the payslip marked on hold, without HR's reason
    post(hr, f"/runs/{out['bhadra']}/publish")
    lst = get(w['sita'], '/payslips')
    pid = lst.body['payslips'][0]['id']
    js = get(w['sita'], f'/payslips/{pid}')
    assert js.body['payslip']['held'] == {'status': 'held', 'reason': None}
    html = get(w['sita'], f'/payslips/{pid}/html')
    assert re.search('Payment on hold', html.text)

    # a locked run's hold cannot be cancelled; releasing through payroll pays it once in the next run, untaxed
    hold = get(hr, '/holds').body['holds'][0]
    assert post(hr, f"/holds/{hold['id']}/cancel").status == 409
    assert post(hr, f"/holds/{hold['id']}/release", {'mode': 'payroll', 'note': 'documents received'}).status == 200
    assert post(hr, f"/holds/{hold['id']}/release", {'mode': 'payroll'}).status == 409

    def before2(run_id):
        pf = post(hr, f'/runs/{run_id}/preflight')
        assert 'held_pay_release' in [x['code'] for x in pf.body['warnings']]
        ps = one('SELECT ps.id, ps.taxable::text FROM payslip ps WHERE ps.run_id = %s AND ps.employee_id = %s', [run_id, demo['sita']])
        ln = one("SELECT amount::text FROM payslip_line WHERE payslip_id = %s AND component_code = 'HELD_RELEASE'", [ps['id']])
        assert ln['amount'] == hold['amount']
        earnings = one("""SELECT coalesce(sum(amount), 0)::text AS t FROM payslip_line WHERE payslip_id = %s AND type = 'earning'
                          AND component_code NOT IN ('HELD_RELEASE', 'ROUNDING')""", [ps['id']])
        assert D(ps['taxable']) <= D(earnings['t']) + 40000  # released pay is not taxable again
    out['ashwin'] = run_through(w, {'bsYear': 2083, 'bsMonth': 6}, before2)
    done = one('SELECT status, release_run_id FROM payroll_hold WHERE id = %s', [hold['id']])
    assert done['status'] == 'released' and str(done['release_run_id']) == out['ashwin']

    # an unlocked run's hold can be cancelled; releasing outside payroll needs no run
    p = post(hr, '/periods', {'bsYear': 2083, 'bsMonth': 7})
    sup = post(hr, '/runs', {'periodId': p.body['period']['id'], 'runType': 'off_cycle', 'employeeIds': [demo['ram']]})
    post(hr, f"/runs/{sup.body['id']}/inputs", {'inputs': [{'employeeId': demo['ram'], 'componentCode': 'BONUS', 'amount': '5000.00'}]})
    assert post(hr, f"/runs/{sup.body['id']}/compute").status == 200
    h = post(hr, f"/runs/{sup.body['id']}/holds", {'employeeId': demo['ram'], 'reason': 'check bank'})
    assert post(hr, f"/holds/{h.body['id']}/cancel").status == 200
    assert one('SELECT status FROM payroll_hold WHERE id = %s', [h.body['id']])['status'] == 'cancelled'
    return out


def optimum_block(w):
    hr, demo = w['hr'], w['demo']
    # suggests the CIT that lowers the employee's tax most; employees see only their own
    me = get(w['sita'], '/optimum-cit/me')
    assert me.status == 200
    assert me.body['available'] is True, me
    assert D(me.body['optimumMonthly']) > 0
    assert D(me.body['annualSaving']) > 0
    assert get(w['sita'], '/optimum-cit').status == 403
    allc = get(hr, '/optimum-cit')
    assert {demo['sita'], demo['ram']} <= {e['employeeId'] for e in allc.body['employees']}

    # HR applies it from this month; the employee is told and the estimate changes
    me = get(w['sita'], '/optimum-cit/me')
    assert post(w['sita'], '/optimum-cit/apply', {'employeeId': demo['sita'], 'monthlyCit': me.body['optimumMonthly']}).status == 403
    ap = post(hr, '/optimum-cit/apply', {'employeeId': demo['sita'], 'monthlyCit': me.body['optimumMonthly']})
    assert ap.status == 200, ap
    d = one('SELECT cit_monthly::text, status FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = 2083', [demo['sita']])
    assert d == {'cit_monthly': f"{D(me.body['optimumMonthly']):.2f}", 'status': 'verified'}
    again = get(w['sita'], '/optimum-cit/me')
    assert again.body['reason'] == 'already_optimal'
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'payroll.cit_applied' AND employee_id = %s",
               [demo['sita']])['n'] > 0


def donations_block(w):
    hr, demo = w['hr'], w['demo']
    # an employee declares a donation; it counts only after HR approves
    sub = post(w['ram'], '/tax-savings', {'fiscalYearStart': 2083, 'donation': '10000.00'})
    assert sub.status == 201, sub
    assert sub.body['request']['donation'] == '10000.00'
    decl = one('SELECT donation::text FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = 2083', [demo['ram']])
    assert (decl or {}).get('donation', '0.00') == '0.00'
    assert post(hr, f"/declarations/{demo['ram']}/verify", {'fiscalYearStart': 2083, 'decision': 'verified'}).status == 200
    decl = one('SELECT donation::text FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = 2083', [demo['ram']])
    assert decl['donation'] == '10000.00'
    est = post(w['ram'], '/tax-savings/estimate', {})
    assert est.body['current']['declaration']['donation'] == '10000.00'
    assert 'donationDeduction' in est.body['current']


def reports_block(w, runs: dict):
    hr, demo = w['hr'], w['demo']
    # an employee opens their own tax and salary certificates, not a colleague's
    tax = get(w['sita'], '/certificates/me', {'kind': 'tax', 'fiscalYearStart': 2083})
    assert tax.status == 200
    assert re.search('Tax deduction certificate', tax.text)
    assert re.search('123456701', tax.text)
    assert re.search('Bhadra 2083', tax.text)
    assert not re.search('<script', tax.text, re.I)
    sal = get(w['sita'], '/certificates/me', {'kind': 'salary', 'purpose': 'Bank loan'})
    assert sal.status == 200
    assert re.search('Salary certificate', sal.text)
    assert re.search('Bank loan', sal.text)
    assert get(w['ram'], f"/certificates/{demo['sita']}").status == 404
    assert get(w['mgr'], f"/certificates/{demo['sita']}").status == 404
    assert get(hr, f"/certificates/{demo['sita']}", {'fiscalYearStart': 2083}).status == 200
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'certificate' AND row_id = %s", [demo['sita']])['n'] > 0

    # annual tax summary includes opening balances and masks PAN on screen
    r = get(hr, '/reports/annual-tax', {'fiscalYearStart': 2083})
    assert r.status == 200
    ram = next(x for x in r.body['rows'] if x['employeeId'] == demo['ram'])
    assert ram['opening'] == 'yes'
    assert re.match(r'^\*+', ram['pan'])
    csv = get(hr, '/reports/annual-tax', {'fiscalYearStart': 2083, 'format': 'csv'})
    assert ord(csv.text[0]) == 0xFEFF
    assert re.search('123456702', csv.text)
    assert get(w['sita'], '/reports/annual-tax').status == 403

    # tax and retirement deposit reports for a month
    tx = get(hr, '/reports/tax-deposit', {'bsYear': 2083, 'bsMonth': 5})
    assert tx.status == 200
    assert len(tx.body['rows']) == 2
    for row in tx.body['rows']:
        assert f"{D(row['sst']) + D(row['incomeTax']):.2f}" == row['tds']
    rf = get(hr, '/reports/retirement-deposit', {'bsYear': 2083, 'bsMonth': 5, 'format': 'csv'})
    assert rf.status == 200
    assert re.search('SSF employee', rf.text)
    assert get(w['mgr'], '/reports/tax-deposit', {'bsYear': 2083, 'bsMonth': 5}).status == 403

    # pay variance explains the change against the previous month
    v = get(hr, '/reports/pay-variance', {'runId': runs['ashwin']})
    assert v.status == 200
    assert v.body['previous']['bsMonth'] == 5
    first = next(x for x in v.body['rows'] if x['name'])
    assert 'reasons' in first
    assert any(re.search('One-time|Tax changed', x['reasons']) for x in v.body['rows'])

    # leave liability values encashable balances at basic / divisor
    home = one("SELECT id FROM leave_type WHERE company_id = %s AND code = 'HOME'", [demo['company_id']])
    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-09-01', 10, 'allocation')""",
            [demo['ram'], home['id']])
    r = get(hr, '/reports/leave-liability', {'asOf': '2026-10-01'})
    assert r.status == 200
    row = next((x for x in r.body['rows'] if x['leaveType'] == 'Home leave' and x['balance'] == '10.00'), None)
    assert row
    from decimal import ROUND_HALF_UP
    assert row['amount'] == str((D(30000) / D(r.body['divisor']) * 10).quantize(D('0.01'), rounding=ROUND_HALF_UP))
    assert D(r.body['total']) > 0


def test_opening_balances(w):
    opening_block(w)


def test_held_pay_and_the_bhadra_run(w):
    opening_block(w)
    held_block(w)


def test_optimum_cit(w):
    optimum_block(w)


def test_donations_in_tax_savings(w):
    donations_block(w)


def test_certificates_and_reports(w):
    opening_block(w)
    runs = held_block(w)
    optimum_block(w)
    donations_block(w)
    reports_block(w, runs)


def test_publishing_on_lock(w):
    # with the setting on, locking a run publishes its payslips
    set_policy(w['demo']['company_id'], publishOnLock=True)
    run = run_through(w, {'bsYear': 2083, 'bsMonth': 7})
    ps = one('SELECT count(*)::int AS n, count(published_at)::int AS p FROM payslip WHERE run_id = %s', [run])
    assert ps['n'] > 0
    assert ps['p'] == ps['n']
