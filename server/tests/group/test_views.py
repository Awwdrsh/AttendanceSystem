"""What the server-rendered group pages checked, now through the JSON the React Group hub calls: figures of the
group's companies (never Gamma's), payroll cost and the pay-hiding policy, needs-attention links, query counts that
do not grow with companies, and the Bearer clients' dashboard and reports."""
from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.core import auth
from hajiri.db.models import AuditLog, CompanySetting
from hajiri.group import services
from tests.web.factories import PW, bearer

J = 'application/json'


def test_dashboard_shows_both_companies_totals_and_payroll_cost(world, figures):
    d = world.signed_in(world.ghr).get('/api/v1/group/dashboard').json()
    assert [c['name'] for c in d['companies']] == ['Alpha Traders', 'Beta Foods']
    assert d['groupName'] == 'Alpha Traders group'
    assert d['totals']['payrollCost'] == '144000.00'               # latest locked payroll cost, both companies
    labels = {(i['companyCode'], i['label']) for i in d['needsAttention']}
    assert ('BETA', '1 clock-in device offline') in labels
    probation = next(i for i in d['needsAttention'] if i['kind'] == 'probation_due')
    assert probation['companyCode'] == 'ALPHA' and probation['next'] == '/employees/onboarding?tab=probation'
    # ghr works in Beta: Beta's items are the active company's, Alpha's switch first.
    assert {i['companyCode'] for i in d['needsAttention'] if i['active']} == {'BETA'}


def test_group_views_access(world):
    for who in ('solo', 'admin_b', 'admin_c', 'dual'):
        c = world.signed_in(getattr(world, who))
        for url in ('/api/v1/group/dashboard', '/api/v1/group/people', '/api/v1/group/reports',
                    '/api/v1/group/reports/headcount'):
            r = c.get(url)
            assert r.status_code == 403, (who, url)
            assert r.json()['error'] == services.FORBIDDEN


def test_pay_hidden_from_executives_when_the_company_says_so(world, figures):
    c = world.signed_in(world.admin_a)
    assert c.get('/api/v1/group/pay-policy').json() == {services.HIDE_PAY: False}
    r = c.put('/api/v1/group/pay-policy', json.dumps({services.HIDE_PAY: True}), content_type=J)
    assert r.status_code == 200 and r.json() == {services.HIDE_PAY: True}
    assert CompanySetting.objects.get(company=world.alpha, key=services.POLICY_KEY).value == {services.HIDE_PAY: True}
    assert AuditLog.objects.filter(table_name='company_setting', row_id=str(world.alpha.id)).exists()
    d = world.signed_in(world.gexec).get('/api/v1/group/dashboard?refresh=1').json()
    alpha = next(x for x in d['companies'] if x['code'] == 'ALPHA')
    assert alpha['paySeen'] is False and alpha['payroll'] is None
    assert d['totals']['payrollCost'] == '48000.00' and d['totals']['payHidden'] == 1
    # HR still sees it; executives and other roles cannot change it.
    assert world.signed_in(world.ghr).get('/api/v1/group/dashboard?refresh=1').json()['totals']['payrollCost'] == '144000.00'
    assert world.signed_in(world.gexec).put('/api/v1/group/pay-policy', json.dumps({services.HIDE_PAY: True}),
                                            content_type=J).status_code == 403
    assert world.signed_in(world.gexec).get('/api/v1/group/pay-policy').status_code == 403
    bad = c.put('/api/v1/group/pay-policy', json.dumps({}), content_type=J)
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert c.put('/api/v1/group/pay-policy', json.dumps({services.HIDE_PAY: False}), content_type=J).json() == {
        services.HIDE_PAY: False}


def test_dashboard_queries_do_not_grow_with_companies(world, figures):
    from hajiri.core import companies
    from hajiri.db.models import Company
    from tests.web.factories import make_employee
    c = world.signed_in(world.ghr)
    c.get('/api/v1/group/dashboard')
    with CaptureQueriesContext(connection) as two:
        c.get('/api/v1/group/dashboard')
    _, admin_a = world.session(world.admin_a)
    delta = companies.create_group_company(admin_a, {'code': 'DELTA', 'name_en': 'Delta Ltd'})
    make_employee(Company.objects.get(pk=delta['id']), name='Dev Delta')
    c.get('/api/v1/group/dashboard')
    with CaptureQueriesContext(connection) as three:
        d = c.get('/api/v1/group/dashboard').json()
    assert 'Delta Ltd' in {x['name'] for x in d['companies']}
    assert len(three) <= len(two) + 1          # the session carries one more company; the figures cost the same


def test_api_dashboard_and_reports_with_a_bearer_token(world, figures):
    token = auth.login(world.ghr.email, PW).token
    from django.test import Client
    c = Client()
    d = c.get('/api/v1/group/dashboard', **bearer(token)).json()
    assert [x['code'] for x in d['companies']] == ['ALPHA', 'BETA'] and d['totals']['headcount'] == 10
    r = c.get(f'/api/v1/group/reports/payroll?bsYear={figures.prev.year}&bsMonth={figures.prev.month}', **bearer(token))
    assert r.status_code == 200 and r.json()['rows'][-1]['company'] == 'Group total'
    assert c.get(f'/api/v1/group/reports/headcount?companyId={world.gamma.id}', **bearer(token)).status_code == 404
    assert c.get('/api/v1/group/reports/nope', **bearer(token)).status_code == 404
    bad = c.get('/api/v1/group/reports/headcount?bsMonth=13', **bearer(token))
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    outsider = auth.login(world.admin_c.email, PW).token
    assert c.get('/api/v1/group/dashboard', **bearer(outsider)).status_code == 403
    assert c.get('/api/v1/group/reports/headcount', **bearer(outsider)).status_code == 403
    for kind in services.REPORTS:
        assert c.get(f'/api/v1/group/reports/{kind}', **bearer(token)).status_code == 200, kind


@pytest.mark.parametrize('kind', list(services.REPORTS))
def test_every_report_downloads_as_csv(world, figures, kind):
    r = world.signed_in(world.ghr).get(f'/api/v1/group/reports/{kind}?format=csv')
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    assert 'Gamma Outside' not in r.content.decode('utf-8-sig')
