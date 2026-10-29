"""The payroll endpoints called with the exact request bodies the React pages build (frontend/src/pages/payroll/*,
pages/employee/MyTaxPage.jsx, PayslipsPage.jsx) and the exact response values they read, compared with what the TS
routes at 65a9079 returned (payroll/routes.ts, compliance.ts, tax-savings.ts)."""
from __future__ import annotations

from engines.bs_calendar import ad_to_bs

from hajiri.core.clock import npt_today
from tests.support.sql import execute

from tests.multicompany.conftest import world  # noqa: F401 - the group-of-companies fixture

from .helpers import attendance_for
from .test_api_payroll import app, has  # noqa: F401 - the React-style browser fixture


def _setup_pay(hr, demo):
    version = hr.get('/structures')['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}),
                      (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        hr.post('/assignments', {'employeeId': emp, 'structureVersionId': version, 'from': '2026-01-01', 'baseAmounts': base}, expect=201)
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16', [], locked=True)
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16', [], locked=True)


def _locked_run(hr, demo):
    _setup_pay(hr, demo)
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)
    rid = hr.post('/runs', {'periodId': p['period']['id'], 'runType': 'regular'}, expect=201)['id']
    hr.post(f'/runs/{rid}/compute', {})
    hr.post(f'/runs/{rid}/review', {})
    hr.post(f'/runs/{rid}/approve')
    hr.post(f'/runs/{rid}/lock')
    return rid


def test_structures_page_component_editor_bodies(app):
    """StructuresPage.save(): every editor field, with '' for blank names and formulas; the edit sends everything but
    the code (and statutory)."""
    hr = app['hr']
    body = {'code': 'TRANSPORT', 'nameEn': 'Transport', 'nameNe': '', 'type': 'earning', 'formula': '', 'condition': '',
            'dependsOnPaymentDays': True, 'taxable': True, 'rounding': 'paisa', 'sortOrder': 50, 'active': True}
    assert hr.post('/components', body, expect=201) == {'code': 'TRANSPORT'}
    c = next(x for x in hr.get('/components')['components'] if x['code'] == 'TRANSPORT')
    assert c['name_en'] == 'Transport' and c['sort_order'] == 50 and c['depends_on_payment_days'] is True
    assert c['statutory'] is False and c['active'] is True
    rest = {k: v for k, v in body.items() if k != 'code'} | {'nameEn': 'Transport allowance', 'formula': 'BASIC * 0.05'}
    assert hr.patch('/components/TRANSPORT', rest) == {'ok': True}
    c = next(x for x in hr.get('/components')['components'] if x['code'] == 'TRANSPORT')
    assert c['name_en'] == 'Transport allowance' and c['formula'] == 'BASIC * 0.05'
    # A statutory row sends its type back too: refused as in the TS route (statutory components are locked).
    st = next(x for x in hr.get('/components')['components'] if x['statutory'])
    r = hr.call('patch', f"/components/{st['code']}", {'nameEn': st['name_en'], 'type': st['type'], 'formula': st['formula'] or ''},
                expect=403)
    assert r.json()['error']
    # A formula that refers to itself is a 400 with {error}.
    r = hr.call('patch', '/components/TRANSPORT', {'formula': 'TRANSPORT + 1'}, expect=400)
    assert r.json()['error']
    # saveStructure(): a new structure, then a new version of an existing one.
    made = hr.post('/structures', {'code': 'MGR', 'nameEn': 'Managers', 'components': [{'code': 'BASIC'}, {'code': 'TRANSPORT'}]},
                   expect=201)
    has(made, 'id', 'versionId')
    v2 = hr.post(f"/structures/{made['id']}/versions", {'components': [{'code': 'BASIC'}]}, expect=201)
    assert v2['version'] == 2 and v2['id']
    s = next(x for x in hr.get('/structures')['structures'] if x['code'] == 'MGR')
    assert [v['version'] for v in s['versions']] == [1, 2] and s['versions'][1]['components'] == [{'code': 'BASIC'}]


def test_hub_creates_the_period_again_for_every_new_run(app):
    """PayrollHubPage calls createPeriod before every createRun: the same month answers 201 with the same period."""
    hr = app['hr']
    a = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)['period']
    b = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)['period']
    assert a['id'] == b['id'] and a['start_ad'] == '2026-08-17' and a['end_ad'] == '2026-09-16'
    rid = hr.post('/runs', {'periodId': a['id'], 'runType': 'regular'}, expect=201)['id']
    r = hr.call('post', '/runs', {'periodId': a['id'], 'runType': 'regular'}, expect=409)
    assert rid in r.json()['error']
    run = next(x for x in hr.get('/runs')['runs'] if x['id'] == rid)
    assert (run['bs_year'], run['bs_month'], run['run_type'], run['status'], run['totals']) == (2083, 5, 'regular', 'draft', None)


def test_wizard_values_the_pages_show(app):
    """RunWizardPage: blockers with count, run totals, payslip money as 2-decimal strings, variance rows by employeeId."""
    hr, demo = app['hr'], app['demo']
    _setup_pay(hr, demo)
    execute('UPDATE attendance_day SET locked_at = NULL')
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)['period']
    rid = hr.post('/runs', {'periodId': p['id']}, expect=201)['id']
    pf = hr.post(f'/runs/{rid}/preflight')
    assert pf['ok'] is False
    b = next(x for x in pf['blockers'] if x['code'] == 'attendance_not_locked')
    assert b['message'] and b['count'] == 62
    assert hr.get(f'/runs/{rid}')['run']['status'] == 'preflight_failed'
    assert hr.get(f'/runs/{rid}')['run']['preflight']['ok'] is False
    # "Calculate anyway" with a reason; the add-input modal sends amount as typed and an optional note.
    hr.post(f'/runs/{rid}/inputs', {'inputs': [{'employeeId': demo['ram'], 'componentCode': 'ALLOWANCE', 'amount': '1500'}]}, expect=201)
    i = hr.get(f'/runs/{rid}/inputs')['inputs'][0]
    assert (i['emp_code'], i['component_code'], i['amount'], i['note']) == ('E002', 'ALLOWANCE', '1500.00', None)
    r = hr.call('post', f'/runs/{rid}/compute', {'overrideReason': 'ok'}, expect=400)
    assert r.json()['issues'][0]['path'] == 'overrideReason'
    out = hr.post(f'/runs/{rid}/compute', {'overrideReason': 'attendance checked by hand'})
    assert out['payslips'] == 2
    d = hr.get(f'/runs/{rid}')
    run = d['run']
    assert run['status'] == 'computed' and run['computed_at']
    assert set(run['totals']) == {'headcount', 'gross', 'net', 'tds', 'employeeStatutory', 'employerStatutory'}
    assert run['totals']['headcount'] == '2'
    assert run['variance']['flagged'] == 2 and run['variance']['previousNet'] == '0.00'
    assert {v['employeeId'] for v in run['variance']['byEmployee']} == {demo['sita'], demo['ram']}
    assert all(v['previousNet'] is None for v in run['variance']['byEmployee'])
    s = next(x for x in d['payslips'] if x['employee_id'] == demo['ram'])
    assert s['paid_days'] == '31.00' and s['lop_days'] == '0.00'
    for k in ('gross', 'tds', 'net', 'total_deductions', 'employer_statutory', 'employee_statutory'):
        assert isinstance(s[k], str) and len(s[k].split('.')[1]) == 2, (k, s[k])
    assert isinstance(s['flags'], list) and s['published_at'] is None
    assert d['period']['attendance_locked_at'] is None and d['period']['days'] == 31
    # Review with notes, approve (signature shown truncated), lock.
    assert hr.post(f'/runs/{rid}/review', {'notes': 'checked'}) == {'ok': True}
    a = hr.post(f'/runs/{rid}/approve')
    assert a['ok'] is True and a['approved'] is True and a['signature'].startswith('sha256:')
    assert hr.get(f'/runs/{rid}')['run']['approval_signature'] == a['signature']
    lk = hr.post(f'/runs/{rid}/lock')
    assert set(lk) == {'attendanceLocked', 'published'}
    assert hr.get(f'/runs/{rid}')['run']['locked_at']
    r = hr.call('post', f'/runs/{rid}/inputs', {'inputs': [{'employeeId': demo['ram'], 'componentCode': 'ALLOWANCE', 'amount': '1'}]},
                expect=409)
    assert r.json()['error'] == 'run is locked; inputs can no longer change'


def test_bonus_run_from_the_hub(app):
    """createBonus(): {periodId, fiscalYearStart, netProfit: String(profit)}; the toast reads r.pool."""
    hr, demo = app['hr'], app['demo']
    _locked_run(hr, demo)
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 6}, expect=201)['period']
    r = hr.post('/runs/bonus', {'periodId': p['id'], 'fiscalYearStart': 2083, 'netProfit': '1000000'}, expect=201)
    assert r['id'] and r['pool'] == '100000.00' and isinstance(r['allocation'], list)
    assert next(x for x in hr.get('/runs')['runs'] if x['id'] == r['id'])['run_type'] == 'bonus'
    r = hr.call('post', '/runs/bonus', {'periodId': p['id'], 'fiscalYearStart': 2083, 'netProfit': '-5'}, expect=400)
    assert r.json()['issues'][0]['path'] == 'netProfit'


def test_loans_and_declarations_pages(app):
    hr, sita, demo = app['hr'], app['sita'], app['demo']
    r = hr.post('/loans', {'employeeId': demo['ram'], 'kind': 'advance', 'principal': '10000.00', 'emi': '3000.00', 'startBsYear': 2083,
                           'startBsMonth': 7, 'note': 'festival advance'}, expect=201)
    assert r['installments'] == 4
    loan = hr.get('/loans', {'employeeId': demo['ram']})['loans'][0]
    assert (loan['kind'], loan['principal'], loan['emi'], loan['repaid'], loan['status'], loan['note']) == \
        ('advance', '10000.00', '3000.00', '0', 'active', 'festival advance')
    assert hr.call('post', '/loans', {'employeeId': demo['ram'], 'principal': '0.00', 'emi': '0.00', 'startBsYear': 2083,
                                      'startBsMonth': 7}, expect=400).json()['error'] == 'principal and emi must be positive'
    # DeclarationsPage, HR for someone else: recorded as verified straight away.
    body = {'employeeId': demo['ram'], 'fiscalYearStart': 2083, 'lifePremium': '25000.00', 'healthPremium': '0.00',
            'citMonthly': '1000.00', 'medicalCost': '0.00', 'otherRetirementAnnual': '0.00', 'donation': '0.00', 'submit': True}
    assert hr.put('/declarations', body) == {'ok': True, 'status': 'verified'}
    d = hr.get('/declarations', {'fiscalYearStart': 2083, 'employeeId': demo['ram']})
    assert d['pending'] is None and d['declaration']['status'] == 'verified'
    assert (d['declaration']['life_premium'], d['declaration']['cit_monthly'], d['declaration']['donation']) == ('25000.00', '1000.00', '0.00')
    # Sita's own: sent as a request; HR rejects it with a reason.
    own = {k: v for k, v in body.items() if k != 'employeeId'}
    r = sita.put('/declarations', own)
    assert r['ok'] is True and r['status'] == 'pending' and r['requestId']
    p = hr.get('/declarations', {'fiscalYearStart': 2083, 'employeeId': demo['sita']})['pending']
    assert p['life_premium'] == '25000.00' and p['proof_file_ids'] == [] and p['note'] is None
    assert hr.post(f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': 2083, 'decision': 'rejected', 'reason': 'no receipt'}) == {'ok': True}
    mine = sita.get('/tax-savings', {'fiscalYearStart': 2083})
    assert mine['pending'] is None and mine['history'][0]['status'] == 'rejected'
    assert mine['history'][0]['decision_note'] == 'no receipt'
    # An employee cannot read someone else's declaration.
    app['ram'].call('get', '/declarations', query={'fiscalYearStart': 2083, 'employeeId': demo['sita']}, expect=403)


def test_my_tax_page_estimate_submit_and_withdraw(app):
    hr, sita, demo = app['hr'], app['sita'], app['demo']
    _setup_pay(hr, demo)
    bs = ad_to_bs(npt_today().isoformat())
    fy = bs.year if bs.month >= 4 else bs.year - 1
    values = {'lifePremium': '40000.00', 'healthPremium': '0.00', 'citMonthly': '2000.00', 'medicalCost': '0.00',
              'otherRetirementAnnual': '0.00', 'donation': '0.00'}
    est = sita.post('/tax-savings/estimate', {'fiscalYearStart': fy, 'proposed': values})
    assert est['available'] is True and est['bsYear'] and est['bsMonth']
    for k in ('current', 'proposed'):
        has(est[k], 'projectedAnnualTaxable', 'annualTax', 'tdsThisMonth', 'insuranceDeduction', 'retirementDeduction', 'medicalCredit')
    assert est['proposed']['insuranceDeduction'] == '40000.00'
    has(est, 'annualSaving', 'monthlySaving')
    req = sita.post('/tax-savings', {**values, 'fiscalYearStart': fy, 'note': 'LIC receipt', 'proofFileIds': []}, expect=201)['request']
    assert req['status'] == 'pending' and req['note'] == 'LIC receipt'
    got = sita.get('/tax-savings', {'fiscalYearStart': fy})
    assert got['pending']['id'] == req['id']
    h = got['history'][0]
    assert h['id'] == req['id'] and h['proofs'] == [] and h['created_at'] and h['cit_monthly'] == '2000.00'
    assert sita.post(f"/tax-savings/{req['id']}/cancel") == {'ok': True}
    assert sita.get('/tax-savings', {'fiscalYearStart': fy})['pending'] is None
    # Another employee's request is not found.
    req2 = sita.post('/tax-savings', {**values, 'fiscalYearStart': fy, 'proofFileIds': []}, expect=201)['request']
    app['ram'].call('post', f"/tax-savings/{req2['id']}/cancel", expect=404)


def test_payslips_page_by_fiscal_year(app):
    """PayslipsPage sends {employeeId, fiscalYearStart}; an employee sees published payslips only."""
    hr, sita, demo = app['hr'], app['sita'], app['demo']
    rid = _locked_run(hr, demo)
    assert sita.get('/payslips', {'employeeId': demo['sita'], 'fiscalYearStart': 2083})['payslips'] == []
    assert len(hr.get('/payslips', {'employeeId': demo['sita'], 'fiscalYearStart': 2083})['payslips']) == 1
    hr.post(f'/runs/{rid}/publish')
    rows = sita.get('/payslips', {'employeeId': demo['sita'], 'fiscalYearStart': 2083})['payslips']
    assert len(rows) == 1 and rows[0]['run_type'] == 'regular' and (rows[0]['bs_year'], rows[0]['bs_month']) == (2083, 5)
    assert rows[0]['net'] and rows[0]['paid_days'] == '31.00'
    assert sita.get('/payslips', {'employeeId': demo['sita'], 'fiscalYearStart': 2082})['payslips'] == []
    sita.call('get', '/payslips', query={'employeeId': demo['ram']}, expect=403)
    d = sita.get(f"/payslips/{rows[0]['id']}")
    assert d['payslip']['held'] is None and d['tax']['annualTax'] is not None
    assert {line['type'] for line in d['lines']} >= {'earning', 'deduction'}


def test_held_pay_cancel_and_release_through_payroll(app):
    """RunHolds / HeldPayPage: hold, cancel while unlocked, hold again, release through the next run after lock."""
    hr, demo = app['hr'], app['demo']
    _setup_pay(hr, demo)
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)['period']
    rid = hr.post('/runs', {'periodId': p['id']}, expect=201)['id']
    hr.post(f'/runs/{rid}/compute', {})
    h = hr.post(f'/runs/{rid}/holds', {'employeeId': demo['ram'], 'reason': 'documents pending'}, expect=201)
    r = hr.call('post', f'/runs/{rid}/holds', {'employeeId': demo['ram'], 'reason': 'again'}, expect=409)
    assert r.json()['error'] == 'pay is already held for this employee in this run'
    r = hr.call('post', f"/holds/{h['id']}/release", {'mode': 'payroll'}, expect=409)
    assert r.json()['error'] == 'lock the pay run first, or cancel the hold to pay normally in this run'
    assert hr.post(f"/holds/{h['id']}/cancel") == {'ok': True}
    assert hr.get('/holds', {'status': 'open'})['holds'] == []
    assert hr.get('/holds', {'status': 'cancelled'})['holds'][0]['id'] == h['id']
    h2 = hr.post(f'/runs/{rid}/holds', {'employeeId': demo['ram'], 'reason': 'documents pending'}, expect=201)
    assert h2['id'] == h['id']
    hr.post(f'/runs/{rid}/review', {})
    hr.post(f'/runs/{rid}/approve')
    hr.post(f'/runs/{rid}/lock')
    assert hr.post(f"/holds/{h['id']}/release", {'mode': 'payroll'}) == {'ok': True}
    row = hr.get('/holds', {'status': 'open'})['holds'][0]
    assert row['status'] == 'releasing' and row['release_mode'] == 'payroll' and row['run_status'] == 'locked'
    assert row['held_at'][:10] and row['held_by_name']


def test_opening_balances_edit_sends_the_row_back(app):
    """OpeningBalancesPage edit: the row's own values (note null, festival_paid false) go back as the PUT body."""
    hr, demo = app['hr'], app['demo']
    hr.put('/opening-balances', {'employeeId': demo['ram'], 'fiscalYearStart': 2083, 'gross': '75000.00', 'taxable': '75000.00',
                                 'tdsPaid': '750.00', 'months': 3})
    r = hr.get('/opening-balances', {'fiscalYearStart': 2083})['balances'][0]
    body = {'employeeId': r['employee_id'], 'fiscalYearStart': r['fiscal_year_start'], 'gross': r['gross'], 'taxable': r['taxable'],
            'tdsPaid': r['tds_paid'], 'retirementEmployee': r['retirement_employee'], 'retirementEmployer': r['retirement_employer'],
            'festivalPaid': r['festival_paid'], 'months': r['months'], 'source': r['source'], 'note': r['note']}
    assert body['note'] is None and body['festivalPaid'] is False
    assert hr.put('/opening-balances', {**body, 'gross': '80000.00'}) == {'ok': True}
    r2 = hr.get('/opening-balances', {'fiscalYearStart': 2083})['balances']
    assert len(r2) == 1 and r2[0]['gross'] == '80000.00' and r2[0]['id'] == r['id']
    r = hr.call('put', '/opening-balances', {**body, 'taxable': '999999.00'}, expect=400)
    assert r.json()['issues'][0]['path'] == 'taxable'


def test_optimum_cit_apply_from_the_page(app):
    """OptimumCitPage.apply(): {employeeId, monthlyCit, fromBsMonth} for this month or later."""
    hr, demo = app['hr'], app['demo']
    _setup_pay(hr, demo)
    month = ad_to_bs(npt_today().isoformat()).month
    r = hr.post('/optimum-cit/apply', {'employeeId': demo['sita'], 'monthlyCit': '3000.00', 'fromBsMonth': month})
    assert r['ok'] is True and r['employee'] == 'E001' and r['fiscalYearStart'] and r['fromMonthIndex']
    row = next(e for e in hr.get('/optimum-cit')['employees'] if e['employeeId'] == demo['sita'])
    assert row['currentMonthly'] == '3000.00'
    if month != 4:  # the first month of the fiscal year has no earlier month
        earlier = 12 if month == 1 else month - 1
        r = hr.call('post', '/optimum-cit/apply', {'employeeId': demo['sita'], 'monthlyCit': '1.00', 'fromBsMonth': earlier}, expect=400)
        assert r.json()['error'] == 'choose this month or a later month of the fiscal year'
    # An account with no employee record.
    r = hr.call('get', '/optimum-cit/me', expect=400)
    assert r.json()['error'] == 'this account is not linked to an employee'


def test_report_downloads_and_certificates(app):
    hr, sita, demo = app['hr'], app['sita'], app['demo']
    rid = _locked_run(hr, demo)
    hr.post(f'/runs/{rid}/publish')
    csv = hr.call('get', '/reports/annual-tax', query={'fiscalYearStart': 2083, 'format': 'csv'})
    assert csv.headers['Content-Type'].startswith('text/csv')
    v = hr.call('get', '/reports/pay-variance', query={'runId': rid, 'format': 'csv'})
    assert v.headers['Content-Type'].startswith('text/csv')
    ll = hr.call('get', '/reports/leave-liability', query={'asOf': '2026-09-16', 'format': 'csv'})
    assert ll.headers['Content-Type'].startswith('text/csv')
    assert hr.get('/reports/leave-liability', {'asOf': '2026-09-16'})['asOf'] == '2026-09-16'
    tax = hr.get('/reports/tax-deposit', {'bsYear': 2083, 'bsMonth': 5})
    assert {c['key'] for c in tax['columns']} >= {'empCode', 'name'} and len(tax['rows']) == 2
    # MyCertificates: both kinds for "me"; HR opens anyone's by id with a purpose.
    for kind in ('tax', 'salary'):
        r = sita.call('get', '/certificates/me', query={'kind': kind, 'fiscalYearStart': 2083})
        assert r.headers['Content-Type'].startswith('text/html') and b'Sita Sharma' in r.content
    r = hr.call('get', f"/certificates/{demo['ram']}", query={'kind': 'salary', 'fiscalYearStart': 2083, 'purpose': 'Bank loan'})
    assert b'Bank loan' in r.content
    sita.call('get', f"/certificates/{demo['ram']}", query={'kind': 'tax'}, expect=404)


# ---------------------------------------------------------------- company isolation for the ids the pages send in bodies

def _alpha_browser(world):  # noqa: F811
    from .test_api_payroll import Browser
    token, _ = world.session(world.admin_a)
    return Browser(token)


def test_ids_in_bodies_and_queries_from_another_company_are_not_found(world):  # noqa: F811
    from hajiri.db import models as m

    from .test_isolation import beta_employee, held
    alpha = _alpha_browser(world)
    beta_emp = str(beta_employee(world).id)
    locked_beta = held(world).run
    beta_period = locked_beta.period
    # Pages that filter by employee (Loans, Assignments, Declarations, Payslips) and the employee picker bodies.
    for path, query in (('/loans', {'employeeId': beta_emp}), ('/assignments', {'employeeId': beta_emp}),
                        ('/declarations', {'employeeId': beta_emp, 'fiscalYearStart': 2083}),
                        ('/payslips', {'employeeId': beta_emp})):
        r = alpha.c.get('/api/v1/payroll' + path, query, HTTP_ORIGIN='http://testserver')
        assert r.status_code == 404 or r.json().get(path.strip('/'), None) in ([], None), (path, r.status_code, r.content[:200])
    alpha.call('put', '/declarations', {'employeeId': beta_emp, 'fiscalYearStart': 2083, 'submit': True}, expect=404)
    alpha.call('post', '/optimum-cit/apply', {'employeeId': beta_emp, 'monthlyCit': '100.00'}, expect=404)
    alpha.call('get', f'/certificates/{beta_emp}', query={'kind': 'salary'}, expect=404)
    # Run creation with another company's period, referenced run or employees.
    alpha.call('post', '/runs/bonus', {'periodId': str(beta_period.id), 'fiscalYearStart': 2083, 'netProfit': '1000'}, expect=404)
    own_period = alpha.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)['period']['id']
    alpha.call('post', '/runs', {'periodId': own_period, 'runType': 'arrears', 'referencesRunId': str(locked_beta.id)}, expect=404)
    alpha.call('post', '/runs', {'periodId': own_period, 'runType': 'supplementary', 'employeeIds': [beta_emp]}, expect=404)
    # Run steps the wizard calls on another company's run.
    beta_run = m.PayrollRun.objects.create(company=world.beta, period=beta_period, status=m.PayrollRun.Status.COMPUTED)
    for method, path in (('post', 'lock-attendance'), ('post', 'review'), ('post', 'approve'), ('post', 'publish'),
                         ('get', 'inputs')):
        alpha.call(method, f'/runs/{beta_run.id}/{path}', {} if method == 'post' else None, expect=404)
    alpha.call('post', f'/runs/{beta_run.id}/inputs', {'inputs': [{'employeeId': beta_emp, 'componentCode': 'ALLOWANCE', 'amount': '1'}]},
               expect=404)
    # Components are per company: editing Alpha's BASIC leaves Beta's alone.
    m.SalaryComponent.objects.get_or_create(company=world.alpha, code='TRAVEL', defaults={'name_en': 'Travel', 'type': 'earning'})
    m.SalaryComponent.objects.get_or_create(company=world.beta, code='TRAVEL', defaults={'name_en': 'Travel', 'type': 'earning'})
    alpha.patch('/components/TRAVEL', {'nameEn': 'Alpha travel'})
    assert m.SalaryComponent.objects.get(company=world.beta, code='TRAVEL').name_en == 'Travel'
    assert m.SalaryComponent.objects.get(company=world.alpha, code='TRAVEL').name_en == 'Alpha travel'
    gamma_token, _ = world.session(world.admin_c)
    from .test_api_payroll import Browser
    Browser(gamma_token).call('patch', '/components/NOPE_X', {'nameEn': 'x'}, expect=404)


def test_hub_special_run_types(app):
    """createRun(): arrears need a locked referenced run, final settlement exactly one leaver with an exit date."""
    hr, demo = app['hr'], app['demo']
    rid = _locked_run(hr, demo)
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 6}, expect=201)['period']['id']
    assert hr.call('post', '/runs', {'periodId': p, 'runType': 'arrears'}, expect=400).json()['error'] == 'arrears runs need referencesRunId'
    a = hr.post('/runs', {'periodId': p, 'runType': 'arrears', 'referencesRunId': rid}, expect=201)
    assert next(x for x in hr.get('/runs')['runs'] if x['id'] == a['id'])['run_type'] == 'arrears'
    r = hr.call('post', '/runs', {'periodId': p, 'runType': 'final_settlement', 'employeeIds': [demo['ram']], 'noticePay': '5000.00'},
                expect=400)
    assert r.json()['error'] == 'final settlement needs an exit date on the employee record'
    execute("UPDATE employee SET exit_date = '2026-09-30' WHERE id = %s", [demo['ram']])
    fs = hr.post('/runs', {'periodId': p, 'runType': 'final_settlement', 'employeeIds': [demo['ram']], 'noticePay': '5000.00'}, expect=201)
    assert hr.get(f"/runs/{fs['id']}")['run']['params'] == {'employeeIds': [demo['ram']], 'noticePay': '5000.00'}
