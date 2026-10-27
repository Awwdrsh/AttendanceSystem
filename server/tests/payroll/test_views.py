"""The payroll pages and their HTMX fragments, through django.test.Client with a real session cookie: every page renders,
the forms call the same services with the TS toasts, and access follows the TS route guards."""
from __future__ import annotations

import re

import pytest
from django.test import Client

from hajiri.core.auth import SESSION_COOKIE
from tests.web.factories import htmx, toasts

from .helpers import attendance_for, execute, get, master_data, one, post, rows


def client_for(token: str) -> Client:
    c = Client()
    c.cookies[SESSION_COOKIE] = token
    return c


@pytest.fixture
def v(demo, login_as):
    hr = login_as(['hr'])
    hr2 = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    master_data(demo)
    std = get(hr.token, '/structures').body['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}), (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        assert post(hr.token, '/assignments', {'employeeId': emp, 'structureVersionId': std, 'from': '2026-01-01', 'baseAmounts': base}).status == 201
    return {'demo': demo, 'hr': client_for(hr.token), 'hr_tok': hr.token, 'hr2': client_for(hr2.token), 'sita': client_for(sita.token),
            'sita_tok': sita.token, 'std': std}


def _redirect(r) -> str:
    return r.headers.get('HX-Redirect') or ''


def test_every_payroll_page_renders_for_hr(v):
    c = v['hr']
    for url, text in (('/payroll', 'No pay runs yet'), ('/payroll/structures', 'Statutory components'), ('/payroll/structures?tab=structures', 'Standard structure'),
                      ('/payroll/assignments', 'Sita Sharma'), ('/payroll/declarations', 'Pick an employee'), ('/payroll/loans', 'No loans or advances'),
                      ('/payroll/reports', 'Tax deposit'), ('/payroll/reports?kind=funds', 'Retirement funds'), ('/payroll/reports?kind=variance', 'No regular pay runs yet'),
                      ('/payroll/reports?kind=liability', 'Total liability'), ('/payroll/reports?kind=annual', 'Annual tax'),
                      ('/payroll/reports?kind=certs', 'Salary certificate'), ('/payroll/optimum-cit', 'Can save tax'), ('/payroll/held-pay', 'No pay on hold'),
                      ('/payroll/opening-balances', 'No opening balances'), ('/payroll/rules', 'tax.schedule')):
        r = c.get(url)
        assert r.status_code == 200, url
        assert text in r.content.decode(), (url, text)
    # Modals
    for url in ('/_/payroll/runs/new', '/_/payroll/runs/bonus', '/_/payroll/components/new', '/_/payroll/components/BASIC/edit',
                '/_/payroll/structures/new', '/_/payroll/structures/new?code=STD', '/_/payroll/assignments/new', '/_/payroll/loans/new',
                '/_/payroll/opening-balances/edit', '/_/payroll/opening-balances/import'):
        r = c.get(url, **htmx('hj-modal-body'))
        assert r.status_code == 200, url
        assert 'data-modal-title' in r.content.decode(), url


def test_employees_cannot_open_payroll_pages(v):
    s = v['sita']
    r = s.get('/payroll')
    assert r.status_code in (302, 403)
    r = s.post('/_/payroll/runs/new', {'bs_year': 2083, 'bs_month': 5}, **htmx())
    assert r.status_code == 403
    assert toasts(r)[0]['message'] == 'requires role hr or payroll_admin'
    # The payslips hub is theirs.
    assert s.get('/me/payslips').status_code == 200
    assert s.get('/me/payslips/tax').status_code == 200


def test_pay_run_through_the_pages(v):
    c, demo = v['hr'], v['demo']
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16', ['2026-09-01'], locked=False)
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16', locked=False)
    r = c.post('/_/payroll/runs/new', {'bs_year': '2083', 'bs_month': '5', 'run_type': 'regular'}, **htmx())
    assert r.status_code == 204
    m = re.match(r'^/payroll/runs/([0-9a-f-]{36})$', _redirect(r))
    assert m
    run_id = m.group(1)
    assert toasts(r)[0]['message'] == 'Pay run created. Start with the pre-flight checks.'
    # A second regular run for the period is refused with the TS message.
    r = c.post('/_/payroll/runs/new', {'bs_year': '2083', 'bs_month': '5', 'run_type': 'regular'}, **htmx())
    assert r.status_code == 409 and 'continue that run instead' in toasts(r)[0]['message']

    page = c.get(f'/payroll/runs/{run_id}').content.decode()
    assert 'Bhadra 2083' in page and 'Pre-flight checks' in page and 'Checks not run yet' in page
    r = c.post(f'/_/payroll/runs/{run_id}/action/preflight', **htmx('preflight'))
    assert r.status_code == 200
    assert 'Attendance for the period is not locked yet' in r.content.decode()
    assert toasts(r)[0]['message'] == 'Pre-flight checks complete.'
    assert 'Calculate anyway' in r.content.decode()
    assert c.get(f'/_/payroll/runs/{run_id}/override', **htmx('hj-modal-body')).status_code == 200
    r = c.post(f'/_/payroll/runs/{run_id}/action/compute', **htmx())
    assert r.status_code == 409
    assert toasts(r)[0]['message'].startswith('pre-flight blockers must be resolved')
    r = c.post(f'/_/payroll/runs/{run_id}/action/lock_attendance', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Attendance locked for the period.'

    # One-time input, then calculate
    r = c.get(f'/_/payroll/runs/{run_id}/inputs/new', **htmx('hj-modal-body'))
    assert 'BONUS, Bonus (earning)' in r.content.decode()
    r = c.post(f'/_/payroll/runs/{run_id}/inputs/new', {'employee_id': demo['sita'], 'component_code': 'BONUS', 'amount': '5000', 'note': 'spot'}, **htmx())
    assert r.status_code == 204 and _redirect(r).endswith('?step=1')
    assert 'spot' in c.get(f'/payroll/runs/{run_id}?step=1').content.decode()
    r = c.post(f'/_/payroll/runs/{run_id}/action/compute', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Payslips calculated.'
    page = c.get(f'/payroll/runs/{run_id}').content.decode()
    assert 'Calculate and review' in page and 'Sita Sharma' in page and 'FirstPayslip' in page and 'Mark reviewed' in page

    # Hold Sita's pay from the review step, then undo it on Held pay (the run is not locked)
    r = c.get(f'/_/payroll/runs/{run_id}/hold', **htmx('hj-modal-body'))
    assert 'Sita Sharma (E001)' in r.content.decode()
    r = c.post(f'/_/payroll/runs/{run_id}/hold', {'employee_id': demo['sita'], 'reason': 'documents pending'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Pay held. Release it under Payroll, Held pay once the issue is settled.'
    held = c.get('/payroll/held-pay').content.decode()
    assert 'documents pending' in held and 'Undo hold' in held
    hold_id = str(one('SELECT id FROM payroll_hold WHERE run_id = %s', [run_id])['id'])
    r = c.post(f'/_/payroll/holds/{hold_id}/cancel', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Hold removed.'

    # Draft salary sheet on the last step, with downloads
    page = c.get(f'/payroll/runs/{run_id}?step=4').content.decode()
    assert 'Salary sheet, Bhadra 2083' in page and 'Download for Excel' in page and 'Draft' in page
    csv = c.get(f'/_/payroll/runs/{run_id}/salary-sheet.csv')
    assert csv.status_code == 200 and 'salary-sheet-2083-05-draft.csv' in csv.headers['Content-Disposition']
    assert 'Prepared by' in c.get(f'/_/payroll/runs/{run_id}/salary-sheet.html').content.decode()

    # Review, approve (a second HR user), lock and publish
    r = c.post(f'/_/payroll/runs/{run_id}/action/review', {'notes': 'checked'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Marked as reviewed.'
    assert 'Approve run' in c.get(f'/payroll/runs/{run_id}').content.decode()
    r = v['hr2'].post(f'/_/payroll/runs/{run_id}/action/approve', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Run approved.'
    page = c.get(f'/payroll/runs/{run_id}').content.decode()
    assert 'Signed' in page and 'Lock run' in page
    r = c.post(f'/_/payroll/runs/{run_id}/action/lock', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Run locked. Attendance for the period is now read-only.'
    page = c.get(f'/payroll/runs/{run_id}').content.decode()
    assert 'Publish payslips' in page and 'SSF sheet' in page
    ssf = c.get(f'/_/payroll/runs/{run_id}/ssf-sheet.csv')
    assert ssf.status_code == 200 and ssf.content.decode().startswith('emp_code,ssf_id')
    r = c.post(f'/_/payroll/runs/{run_id}/action/publish', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Payslips published and employees notified.'
    assert 'Payslips are published' in c.get(f'/payroll/runs/{run_id}?step=4').content.decode()
    runs = c.get('/payroll').content.decode()
    assert 'Bhadra 2083' in runs and 'Regular' in runs and '1 pay period opened so far.' in runs

    # The employee's payslips, detail, printable payslip and certificates
    s = v['sita']
    lst = s.get('/me/payslips').content.decode()
    assert 'Bhadra 2083' in lst and 'Net pay this year' in lst and 'Tax certificate' in lst
    pid = str(one('SELECT id FROM payslip WHERE run_id = %s AND employee_id = %s', [run_id, demo['sita']])['id'])
    detail = s.get(f'/me/payslips/{pid}').content.decode()
    assert 'Basic salary' in detail and 'Paid by your employer' in detail and 'How this pay was calculated' in detail
    pr = s.get(f'/_/payroll/payslips/{pid}/print')
    assert pr.status_code == 200 and '<div class="sub">Payslip</div>' in pr.content.decode()
    cert = s.get('/_/payroll/certificates/me', {'kind': 'tax', 'fiscalYearStart': '2083'})
    assert cert.status_code == 200 and 'Tax deduction certificate' in cert.content.decode()
    ram_pid = str(one('SELECT id FROM payslip WHERE run_id = %s AND employee_id = %s', [run_id, demo['ram']])['id'])
    other = s.get(f'/me/payslips/{ram_pid}')
    assert other.status_code == 403 and 'Payslip not available' in other.content.decode()

    # Reports over the locked run
    assert 'Sita Sharma' in c.get('/payroll/reports?kind=tax&bs_year=2083&bs_month=5').content.decode()
    var = c.get('/payroll/reports?kind=variance').content.decode()
    assert 'No earlier locked regular run to compare with yet.' in var and 'New in payroll' in var
    r = c.get('/_/payroll/reports/annual', {'fy': '2083'}, **htmx('report'))
    assert r.status_code == 200 and 'E001' in r.content.decode()
    r = c.get('/_/payroll/certificates/pick', {'employee_id': demo['sita'], 'kind': 'salary', 'purpose': 'Bank loan', 'fiscalYearStart': '2083'})
    assert r.status_code == 200 and 'Bank loan' in r.content.decode()
    assert 'certificate' in [x['action'] for x in rows('SELECT action FROM audit_log WHERE row_id = %s', [demo['sita']])]


def test_components_structures_and_salaries(v):
    c, demo = v['hr'], v['demo']
    r = c.post('/_/payroll/components/new', {'code': 'transport', 'name_en': 'Transport', 'type': 'earning', 'formula': 'BASIC * HRA_X',
                                             'rounding': 'paisa', 'sort_order': '100', 'taxable': 'on', 'active': 'on'}, **htmx())
    assert r.status_code == 400 and 'unknown identifier HRA_X' in toasts(r)[0]['message']
    r = c.post('/_/payroll/components/new', {'code': 'transport', 'name_en': 'Transport', 'type': 'earning', 'formula': 'min(BASIC * 0.05, 2000)',
                                             'rounding': 'paisa', 'sort_order': '100', 'taxable': 'on', 'active': 'on', 'depends': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Component saved.'
    assert one("SELECT depends_on_payment_days FROM salary_component WHERE company_id = %s AND code = 'TRANSPORT'",
               [demo['company_id']])['depends_on_payment_days'] is True
    # A statutory component can be renamed: its disabled type and formula are not sent.
    r = c.post('/_/payroll/components/SSF_EE/edit', {'name_en': 'SSF (employee share)', 'rounding': 'rupee', 'sort_order': '200', 'active': 'on'}, **htmx())
    assert r.status_code == 204
    assert one("SELECT name_en FROM salary_component WHERE company_id = %s AND code = 'SSF_EE'", [demo['company_id']])['name_en'] == 'SSF (employee share)'
    r = c.post('/_/payroll/structures/new', {'code': 'std', 'name_en': 'Standard structure', 'components': ['BASIC', 'ALLOWANCE', 'TRANSPORT']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Structure saved.'
    assert one("SELECT max(v.version) AS n FROM salary_structure_version v JOIN salary_structure s ON s.id = v.structure_id WHERE s.code = 'STD'")['n'] == 2
    page = c.get('/payroll/structures?tab=structures').content.decode()
    assert 'v2' in page
    r = c.get('/_/payroll/assignments/amounts', {'structure_version_id': v['std']}, **htmx('as-amounts'))
    assert 'amount_BASIC' in r.content.decode()
    r = c.post('/_/payroll/assignments/new', {'employee_id': demo['ram'], 'structure_version_id': v['std'], 'from': '2026-10-01',
                                              'amount_BASIC': '32000.00', 'amount_DA': '7380', 'amount_OT': ''}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Salary assigned.'
    page = c.get('/payroll/assignments', {'employee_id': demo['ram']}, **htmx('assignments')).content.decode()
    assert 'From' in page and '32,000.00' in page
    # Loans
    r = c.post('/_/payroll/loans/new', {'employee_id': demo['ram'], 'kind': 'advance', 'principal': '12000', 'emi': '5000',
                                        'start_bs_year': '2083', 'start_bs_month': '5'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Advance created with 3 instalments.'
    assert 'Advance' in c.get('/payroll/loans').content.decode()


def test_declarations_tax_savings_and_proofs(v):
    c, demo, s = v['hr'], v['demo'], v['sita']
    # The employee declares on Tax savings; the estimate fragment shows the effect.
    page = s.get('/me/payslips/tax').content.decode()
    assert 'Your declaration' in page and 'Nothing declared' in page
    r = s.post('/_/payroll/tax/estimate', {'fy': str(_cur_fy()), 'life_premium': '30000', 'base': '{}'}, **htmx('tax-estimate'))
    assert r.status_code == 200 and 'Tax for the year' in r.content.decode() and 'With your new figures' in r.content.decode()
    r = s.post('/_/payroll/tax/submit', {'fy': str(_cur_fy()), 'life_premium': '30000', 'note': 'policy 123'}, **htmx())
    assert r.status_code == 204
    t = toasts(r)[0]
    assert t['message'] == 'Sent to HR. Your tax changes once they verify it.' and t['title'] == 'Tax savings submitted'
    page = s.get('/me/payslips/tax').content.decode()
    assert 'Waiting for HR' in page and 'Withdraw' in page and 'Life 30,000' in page
    # HR sees it on Tax declarations and verifies it through the approval workflow.
    page = c.get('/payroll/declarations', {'employee_id': demo['sita'], 'fy': str(_cur_fy())}).content.decode()
    assert 'Waiting for verification: “policy 123”' in page and 'Verify declaration' in page and 'No proof files attached.' in page
    r = c.post('/_/payroll/declarations/verify', {'employee_id': demo['sita'], 'fy': str(_cur_fy()), 'decision': 'verified'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Declaration verified.'
    d = one('SELECT life_premium::text, status FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s', [demo['sita'], _cur_fy()])
    assert d == {'life_premium': '30000.00', 'status': 'verified'}
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'tax_declaration.approved' AND employee_id = %s",
               [demo['sita']])['n'] > 0
    # HR entering for someone else saves it as verified.
    r = c.post('/_/payroll/declarations/save', {'employee_id': demo['ram'], 'fy': str(_cur_fy()), 'health_premium': '15000'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Declaration saved and verified. Payroll uses it from the next run.'
    # A withdrawn request is cancelled with its approval.
    s.post('/_/payroll/tax/submit', {'fy': str(_cur_fy()), 'medical_cost': '5000'}, **htmx())
    req = one("SELECT id FROM tax_declaration_request WHERE employee_id = %s AND status = 'pending'", [demo['sita']])['id']
    r = s.post(f'/_/payroll/tax/{req}/cancel', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Request withdrawn.'
    assert one("SELECT state FROM workflow_instance WHERE object_id = %s", [req])['state'] == 'cancelled'


def _cur_fy() -> int:
    from hajiri.payroll.tax_savings import current_fiscal_year_start
    return current_fiscal_year_start()


def test_opening_balances_page(v):
    c, demo = v['hr'], v['demo']
    fy = _cur_fy()
    r = c.post('/_/payroll/opening-balances/edit', {'employee_id': demo['ram'], 'fy': str(fy), 'gross': '75000', 'taxable': '999999',
                                                     'source': 'previous_employer'}, **htmx())
    assert r.status_code == 400 and 'taxable income cannot be more than gross' in toasts(r)[0]['message']
    r = c.post('/_/payroll/opening-balances/edit', {'employee_id': demo['ram'], 'fy': str(fy), 'gross': '75000', 'taxable': '75000',
                                                     'tds_paid': '750', 'months': '1', 'source': 'previous_employer'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Opening balance saved. It counts from the next pay run.'
    page = c.get('/payroll/opening-balances').content.decode()
    assert 'Ram Thapa' in page and 'Previous employer' in page
    bad = 'emp_code,fiscal_year_start,gross,taxable,tds_paid\nE001,2082,10000,10000,100\nNOPE,2082,1,1,0\n'
    r = c.post('/_/payroll/opening-balances/import', {'csv': bad}, **htmx())
    assert r.status_code == 422 and 'Row 3: no employee with code NOPE' in r.content.decode()
    r = c.post('/_/payroll/opening-balances/import', {'csv': 'emp_code,fiscal_year_start,gross,taxable\nE001,2082,"1,20,000",120000\n'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '1 opening balance imported.'
    oid = one('SELECT id FROM payroll_opening_balance WHERE employee_id = %s AND fiscal_year_start = %s', [demo['ram'], fy])['id']
    r = c.post(f'/_/payroll/opening-balances/{oid}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Deleted.'


def test_optimum_cit_and_rules_pages(v):
    c, demo, s = v['hr'], v['demo'], v['sita']
    execute("UPDATE salary_assignment SET base_amounts = '{\"BASIC\":\"60000.00\",\"ALLOWANCE\":\"140000.00\"}' WHERE employee_id = %s", [demo['sita']])
    page = c.get('/payroll/optimum-cit').content.decode()
    assert 'Sita Sharma' in page and 'Apply' in page
    form = c.get(f"/_/payroll/optimum-cit/{demo['sita']}/apply", **htmx('hj-modal-body')).content.decode()
    assert 'Set CIT for Sita Sharma' in form
    amount = re.search(r'name="monthly_cit" type="number" min="0" step="0.01" value="([0-9.]+)"', form).group(1)
    card = s.get('/_/payroll/tax/optimum', **htmx()).content.decode()
    assert 'in tax this year' in card and 'Use this amount' in card
    r = c.post(f"/_/payroll/optimum-cit/{demo['sita']}/apply", {'monthly_cit': amount}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'].startswith("Sita Sharma's CIT is now NPR ")
    assert 'Your CIT is already working for you' in s.get('/_/payroll/tax/optimum', **htmx()).content.decode()
    # Rules: view and approve
    rid = str(one("SELECT id FROM rule_version WHERE rule_key = 'min_wage' AND valid_to_ad IS NULL")['id'])
    view = c.get(f'/_/payroll/rules/{rid}', **htmx('hj-modal-body')).content.decode()
    assert 'Jurisdiction' in view and '19550' in view
    r = c.post(f'/_/payroll/rules/{rid}/approve', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Rule approved.'


def test_bonus_run_and_held_pay_release_pages(v):
    c, demo = v['hr'], v['demo']
    r = c.post('/_/payroll/runs/bonus', {'bs_year': '2083', 'bs_month': '7', 'fiscal_year_start': '2083', 'net_profit': '1000000'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Bonus run created with a pool of NPR 1,00,000.00.'
    assert 'Bonus run' in c.get(_redirect(r)).content.decode()
    # Release from Held pay needs a locked run: run Bhadra through, hold, lock, release.
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16')
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16')
    p = post(v['hr_tok'], '/periods', {'bsYear': 2083, 'bsMonth': 5}).body['period']['id']
    run_id = post(v['hr_tok'], '/runs', {'periodId': p}).body['id']
    assert post(v['hr_tok'], f'/runs/{run_id}/compute').status == 200
    hold = post(v['hr_tok'], f'/runs/{run_id}/holds', {'employeeId': demo['sita'], 'reason': 'documents pending'}).body['id']
    post(v['hr_tok'], f'/runs/{run_id}/review')
    post(v['hr_tok'], f'/runs/{run_id}/approve')
    post(v['hr_tok'], f'/runs/{run_id}/lock')
    page = c.get('/payroll/held-pay').content.decode()
    assert 'Release' in page
    form = c.get(f'/_/payroll/holds/{hold}/release', **htmx('hj-modal-body')).content.decode()
    assert "Release Sita Sharma's pay" in form
    r = c.post(f'/_/payroll/holds/{hold}/release', {'mode': 'outside', 'note': 'cheque'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Recorded as paid outside payroll.'
    assert 'Released' in c.get('/payroll/held-pay?status=released').content.decode()


def test_hub_tabs_and_payslips_hub(v):
    page = v['hr'].get('/payroll').content.decode()
    for tab in ('Salary structures', 'Employee salaries', 'Tax declarations', 'Loans &amp; advances', 'Optimum CIT', 'Held pay',
                'Opening balances', 'Tax &amp; SSF rules'):
        assert tab in page, tab
    mine = v['sita'].get('/me/payslips').content.decode()
    assert 'Tax savings' in mine and 'No payslips for this fiscal year yet' in mine
