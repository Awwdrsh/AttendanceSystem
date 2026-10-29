"""The /api/v1/payroll endpoints as the React app (frontend/src/api/index.js, compliance.js, security.js and the payroll
pages) calls them: the session cookie sent by fetch() with the browser's Origin header and no CSRF token, the paths,
query parameters and bodies the app sends, and the JSON fields the pages read. The TS assertions themselves are
ported in test_payroll.py, test_payroll_compliance.py, test_payroll_qa.py, test_fixes_payroll.py and
test_tax_savings_selfservice.py; this file pins the contract the web app depends on."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from tests.multicompany.conftest import world  # noqa: F401 - the group-of-companies fixture

from .helpers import attendance_for, master_data
from .test_isolation import computed_run, held, opening, payslip

P = '/api/v1/payroll'
ORIGIN = 'http://testserver'


class Browser:
    """The React app's fetch(): the hajiri_session cookie, credentials: 'include', the page's Origin, no CSRF token
    (Django's CSRF check would refuse every such POST, so the test client enforces it)."""

    def __init__(self, token, origin=ORIGIN):
        self.c = Client(enforce_csrf_checks=True)
        self.c.cookies['hajiri_session'] = token
        self.origin = origin

    def _json(self, r):
        assert 'json' in r.headers.get('Content-Type', ''), (r.status_code, r.content[:300])
        return r.json()

    def call(self, method, path, body=None, query=None, expect=200):
        url = P + path
        if method == 'get':
            r = self.c.get(url, query or {}, HTTP_ORIGIN=self.origin)
        else:
            fn = getattr(self.c, method)
            data = json.dumps(body if body is not None else {}) if method != 'delete' or body is not None else None
            kw = {'content_type': 'application/json'} if data is not None else {}
            r = fn(url, data, HTTP_ORIGIN=self.origin, **kw) if data is not None else fn(url, HTTP_ORIGIN=self.origin)
        assert r.status_code == expect, (method, path, r.status_code, r.content[:400])
        return r

    def get(self, path, query=None, expect=200):
        return self._json(self.call('get', path, query=query, expect=expect))

    def post(self, path, body=None, expect=200):
        return self._json(self.call('post', path, body, expect=expect))

    def put(self, path, body=None, expect=200):
        return self._json(self.call('put', path, body, expect=expect))

    def patch(self, path, body=None, expect=200):
        return self._json(self.call('patch', path, body, expect=expect))

    def delete(self, path, expect=200):
        return self._json(self.call('delete', path, expect=expect))


def has(obj, *keys):
    """The page reads these fields: each is present (it may be null)."""
    missing = [k for k in keys if k not in obj]
    assert not missing, f'missing {missing} in {sorted(obj)}'


@pytest.fixture
def app(demo, login_as):
    master_data(demo)
    return {'demo': demo, 'hr': Browser(login_as(['hr']).token), 'sita': Browser(login_as(['employee'], demo['sita']).token),
            'ram': Browser(login_as(['employee'], demo['ram']).token)}


def test_cookie_requests_from_the_react_app_work_and_other_sites_are_refused(demo, login_as):
    token = login_as(['hr']).token
    ok = Browser(token).post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)
    assert ok['period']['days'] == 31
    evil = Browser(token, origin='https://evil.example')
    r = evil.call('post', '/periods', {'bsYear': 2083, 'bsMonth': 6}, expect=403)
    assert r.json() == {'error': 'cross-site request refused'}
    assert evil.get('/periods')['periods']  # reads pass
    # A Bearer token (integrations) needs no Origin.
    r = Client().post(P + '/periods', json.dumps({'bsYear': 2083, 'bsMonth': 6}), content_type='application/json',
                      HTTP_AUTHORIZATION=f'Bearer {token}')
    assert r.status_code == 201


def test_signed_out_is_401_and_employees_cannot_open_hr_pages(app):
    r = Client().get(P + '/runs')
    assert r.status_code == 401 and r.json()['error']
    for path in ('/runs', '/periods', '/components', '/structures', '/loans', '/assignments', '/holds', '/optimum-cit',
                 '/opening-balances', '/reports/leave-liability', '/compliance/years'):
        r = app['sita'].call('get', path, expect=403)
        assert r.json()['error']


def test_setup_pages_read_the_fields_they_show(app):
    hr, demo = app['hr'], app['demo']
    # RulesPage
    rules = hr.get('/rules')['rules']
    assert rules
    has(rules[0], 'id', 'rule_key', 'valid_from_ad', 'valid_to_ad', 'locked', 'approved_at', 'source_url', 'value')
    # StructuresPage: components and structures with versions
    comps = hr.get('/components')['components']
    has(comps[0], 'code', 'name_en', 'name_ne', 'type', 'formula', 'condition', 'rounding', 'sort_order', 'statutory', 'taxable',
        'depends_on_payment_days', 'active')
    made = hr.post('/components', {'code': 'TRANSPORT', 'nameEn': 'Transport', 'type': 'earning', 'formula': 'min(BASIC * 0.05, 2000)'},
                   expect=201)
    assert made
    hr.patch('/components/TRANSPORT', {'nameEn': 'Transport allowance'})
    structs = hr.get('/structures')['structures']
    has(structs[0], 'id', 'code', 'name_en', 'versions')
    has(structs[0]['versions'][0], 'id', 'version', 'components', 'frozenAt')
    version = structs[0]['versions'][0]['id']
    # AssignmentsPage: /payroll/assignments?employeeId=
    hr.post('/assignments', {'employeeId': demo['sita'], 'structureVersionId': version, 'from': '2026-01-01',
                             'baseAmounts': {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}}, expect=201)
    a = hr.get('/assignments', {'employeeId': demo['sita']})['assignments']
    has(a[0], 'employee_id', 'emp_code', 'name_en', 'structure_code', 'version', 'valid', 'base_amounts')
    # LoansPage
    loan = hr.post('/loans', {'employeeId': demo['sita'], 'principal': '12000.00', 'emi': '5000.00', 'startBsYear': 2083,
                              'startBsMonth': 5}, expect=201)
    assert loan['installments'] == 3
    loans = hr.get('/loans', {'employeeId': demo['sita']})['loans']
    has(loans[0], 'id', 'emp_code', 'kind', 'principal', 'emi', 'installments', 'repaid', 'start_bs_year', 'start_bs_month',
        'status', 'note')
    # DeclarationsPage and MyTaxPage (the old declarations endpoint and tax savings self-service)
    app['sita'].put('/declarations', {'fiscalYearStart': 2083, 'lifePremium': '30000.00'})
    d = hr.get('/declarations', {'fiscalYearStart': 2083, 'employeeId': demo['sita']})
    has(d, 'declaration', 'pending')
    hr.post(f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': 2083, 'decision': 'verified'})
    d = app['sita'].get('/declarations', {'fiscalYearStart': 2083})['declaration']
    has(d, 'life_premium', 'health_premium', 'cit_monthly', 'other_retirement_annual', 'medical_cost', 'donation')
    mine = app['sita'].get('/tax-savings', {'fiscalYearStart': 2083})
    has(mine, 'fiscalYearStart', 'declaration', 'pending', 'history')


def test_a_pay_run_through_the_wizard_reads_the_fields_it_shows(app):
    hr, demo = app['hr'], app['demo']
    version = hr.get('/structures')['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}), (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        hr.post('/assignments', {'employeeId': emp, 'structureVersionId': version, 'from': '2026-01-01', 'baseAmounts': base}, expect=201)
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16', ['2026-09-01'], locked=False)
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16', [], locked=False)

    # PayrollHubPage: createPeriod then createRun; the list shows bs_year/bs_month/run_type/status/totals/locked_at
    p = hr.post('/periods', {'bsYear': 2083, 'bsMonth': 5}, expect=201)
    assert len(hr.get('/periods')['periods']) == 1
    run = hr.post('/runs', {'periodId': p['period']['id'], 'runType': 'regular'}, expect=201)
    rid = run['id']
    listed = hr.get('/runs')['runs']
    has(listed[0], 'id', 'bs_year', 'bs_month', 'run_type', 'status', 'totals', 'created_at', 'computed_at', 'locked_at')

    # RunWizardPage: preflight, lock attendance, inputs, compute, review, approve, lock, publish
    pf = hr.post(f'/runs/{rid}/preflight')
    has(pf, 'ok', 'blockers')
    assert hr.post(f'/runs/{rid}/lock-attendance')['locked'] == 62
    assert hr.post(f'/runs/{rid}/preflight')['ok'] is True
    hr.post(f'/runs/{rid}/inputs', {'inputs': [{'employeeId': demo['sita'], 'componentCode': 'BONUS', 'amount': '5000.00',
                                                'note': 'spot bonus'}]}, expect=201)
    assert len(hr.get(f'/runs/{rid}/inputs')['inputs']) == 1
    assert hr.post(f'/runs/{rid}/compute', {})['payslips'] == 2
    detail = hr.get(f'/runs/{rid}')
    has(detail, 'run', 'period', 'payslips')
    has(detail['run'], 'id', 'status', 'run_type', 'totals', 'locked_at')
    s = next(x for x in detail['payslips'] if x['employee_id'] == demo['sita'])
    has(s, 'id', 'employee_id', 'emp_code', 'name_en', 'paid_days', 'lop_days', 'gross', 'tds', 'net', 'flags', 'published_at')

    # RunHolds: hold Ram's pay on the computed run
    hold = hr.post(f'/runs/{rid}/holds', {'employeeId': demo['ram'], 'reason': 'documents pending'}, expect=201)
    assert hold

    # SalarySheet (draft)
    sheet = hr.get(f'/runs/{rid}/salary-sheet')
    has(sheet, 'run', 'period', 'columns', 'rows', 'totals')
    has(sheet['columns'], 'earnings', 'deductions', 'employer')
    has(sheet['columns']['earnings'][0], 'code', 'name')
    has(sheet['rows'][0], 'employeeId', 'empCode', 'name', 'designation', 'paidDays', 'lopDays', 'earnings', 'deductions', 'employer',
        'gross', 'totalDeductions', 'rounding', 'net', 'employerTotal', 'costToCompany', 'flags')
    csv = hr.call('get', f'/runs/{rid}/salary-sheet', query={'format': 'csv'})
    assert 'salary-sheet-2083-05-draft.csv' in csv.headers['Content-Disposition']
    assert csv.headers['Content-Type'].startswith('text/csv')
    html = hr.call('get', f'/runs/{rid}/salary-sheet', query={'format': 'html'})
    assert html.headers['Content-Type'].startswith('text/html')

    hr.post(f'/runs/{rid}/review', {})
    assert hr.post(f'/runs/{rid}/approve')['approved'] is True
    hr.post(f'/runs/{rid}/lock')
    ssf = hr.call('get', f'/runs/{rid}/ssf-sheet.csv')
    assert ssf.headers['Content-Type'].startswith('text/csv')
    assert hr.post(f'/runs/{rid}/publish')['published'] >= 1

    # HeldPayPage: open holds, then release outside payroll
    holds = hr.get('/holds', {'status': 'open'})['holds']
    has(holds[0], 'id', 'run_id', 'emp_code', 'name_en', 'amount', 'reason', 'status', 'bs_year', 'bs_month', 'held_at',
        'held_by_name', 'run_status', 'release_bs_year', 'release_bs_month')
    hr.post(f"/holds/{holds[0]['id']}/release", {'mode': 'outside', 'note': 'paid by cheque'})

    # PayslipsPage and PayslipDetailPage (employee)
    mine = app['sita'].get('/payslips')['payslips']
    has(mine[0], 'id', 'run_type', 'bs_year', 'bs_month', 'paid_days', 'lop_days', 'gross', 'total_deductions', 'net')
    d = app['sita'].get(f"/payslips/{mine[0]['id']}")
    has(d, 'payslip', 'lines', 'tax')
    has(d['payslip'], 'bs_year', 'bs_month', 'emp_code', 'name_en', 'paid_days', 'lop_days', 'ot_hours', 'gross', 'taxable', 'tds',
        'total_deductions', 'net', 'ytd', 'explain')
    assert 'pan_enc' not in d['payslip'] and 'inputs_snapshot' not in d['payslip']
    has(d['lines'][0], 'code', 'type', 'name_en', 'amount')
    page = app['sita'].call('get', f"/payslips/{mine[0]['id']}/html")
    assert page.headers['Content-Type'].startswith('text/html')
    app['ram'].call('get', f"/payslips/{mine[0]['id']}", expect=403)

    # PayrollReportsPage
    fy = hr.get('/compliance/years')
    assert fy
    annual = hr.get('/reports/annual-tax', {'fiscalYearStart': 2083})
    has(annual, 'columns', 'rows')
    has(annual['columns'][0], 'key', 'header')
    for report in ('tax-deposit', 'retirement-deposit'):
        r = hr.get(f'/reports/{report}', {'bsYear': 2083, 'bsMonth': 5})
        has(r, 'columns', 'rows')
        csv = hr.call('get', f'/reports/{report}', query={'bsYear': 2083, 'bsMonth': 5, 'format': 'csv'})
        assert csv.headers['Content-Type'].startswith('text/csv')
    v = hr.get('/reports/pay-variance', {'runId': rid})
    has(v, 'previous', 'rows')
    ll = hr.get('/reports/leave-liability')
    has(ll, 'asOf', 'divisor', 'total', 'byDepartment', 'rows')

    # OptimumCitPage and the employee's card
    allcit = hr.get('/optimum-cit')
    has(allcit, 'fiscalYearStart', 'employees')
    has(allcit['employees'][0], 'employeeId', 'empCode', 'name', 'available', 'currentMonthly', 'optimumMonthly', 'currentAnnualTax',
        'optimumAnnualTax', 'annualSaving', 'reason')
    me = app['sita'].get('/optimum-cit/me')
    has(me, 'available', 'currentMonthly', 'optimumMonthly', 'currentAnnualTax', 'optimumAnnualTax', 'annualSaving', 'reason')

    # MyCertificates opens the certificate URL in a new tab
    cert = app['sita'].call('get', '/certificates/me', query={'kind': 'tax', 'fiscalYearStart': 2083})
    assert cert.headers['Content-Type'].startswith('text/html')


def test_opening_balances_page(app):
    hr, demo = app['hr'], app['demo']
    hr.put('/opening-balances', {'employeeId': demo['ram'], 'fiscalYearStart': 2083, 'gross': '75000.00', 'taxable': '75000.00',
                                 'tdsPaid': '750.00', 'months': 3})
    rows = hr.get('/opening-balances', {'fiscalYearStart': 2083})['balances']
    has(rows[0], 'id', 'employee_id', 'emp_code', 'name_en', 'fiscal_year_start', 'gross', 'taxable', 'tds_paid', 'months',
        'retirement_employee', 'retirement_employer', 'festival_paid', 'source', 'note')
    # importOpenings sends JSON {csv}; a bad row is a 400 whose body lists the row errors
    bad = hr.call('post', '/opening-balances/import', {'csv': 'emp_code,fiscal_year_start,gross\nNOPE,2083,1000\n'}, expect=400)
    body = bad.json()
    errors = (body.get('details') or body).get('errors')
    assert errors and {'row', 'message'} <= set(errors[0])
    # A JSON body that is not an object is "no CSV" (400), never a server error.
    for wrong in ([], ['emp_code'], 'emp_code'):
        hr.call('post', '/opening-balances/import', wrong, expect=400)
    ok = hr.post('/opening-balances/import', {'csv': 'emp_code,fiscal_year_start,gross,taxable\nE001,2083,1000.00,1000.00\n'})
    assert ok['imported'] == 1
    hr.delete(f"/opening-balances/{rows[0]['id']}")
    assert [r['emp_code'] for r in hr.get('/opening-balances', {'fiscalYearStart': 2083})['balances']] == ['E001']


# ---------------------------------------------------------------- company isolation through the React app's calls

CASES = [
    ('run hold', 'post', '/runs/{id}/holds', computed_run, {'employeeId': '{emp}', 'reason': 'documents pending'}),
    ('salary sheet', 'get', '/runs/{id}/salary-sheet', computed_run, None),
    ('run review', 'post', '/runs/{id}/review', computed_run, {}),
    ('run approve', 'post', '/runs/{id}/approve', computed_run, None),
    ('run publish', 'post', '/runs/{id}/publish', computed_run, None),
    ('payslip', 'get', '/payslips/{id}', payslip, None),
    ('hold release', 'post', '/holds/{id}/release', held, {'mode': 'outside'}),
    ('opening delete', 'delete', '/opening-balances/{id}', opening, None),
    ('pay variance', 'get', '/reports/pay-variance?runId={id}', computed_run, None),
]


def _browser(world, person, company=None):
    token, _ = world.session(person, company)
    return Browser(token)


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_another_companys_payroll_ids_are_not_found(world, case):  # noqa: F811
    name, method, url, maker, body = case
    obj = maker(world)
    path, _, q = url.format(id=obj.pk).partition('?')
    query = dict(x.split('=') for x in q.split('&')) if q else None
    data = json.loads(json.dumps(body).replace('{emp}', str(world.admin_b.employee_id))) if body is not None else None
    for person in (world.admin_c, world.admin_a):
        b = _browser(world, person)
        r = b.call(method, path, query=query, expect=404) if method == 'get' else b.call(method, path, data, expect=404)
        assert r.json()['error'], name
    b = _browser(world, world.admin_b)
    r = b.c.generic(method.upper(), P + path + (f'?{q}' if q else ''), json.dumps(data) if data is not None else '',
                    content_type='application/json', HTTP_ORIGIN=ORIGIN)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_lists_follow_the_active_company(world):  # noqa: F811
    held(world)
    alpha = _browser(world, world.admin_a)
    assert alpha.get('/runs')['runs'] == []
    assert alpha.get('/holds', {'status': 'open'})['holds'] == []
    beta = _browser(world, world.admin_a, world.beta)
    assert len(beta.get('/runs')['runs']) == 1
    assert len(beta.get('/holds', {'status': 'open'})['holds']) == 1
