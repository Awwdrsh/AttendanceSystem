"""Group pages and JSON endpoints through django.test.Client: figures of the group's companies, never Gamma's; 403
for people outside group views; CSV downloads with BS dates; switch-and-open links."""
from __future__ import annotations

import csv
import io

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.core import auth
from hajiri.core.bsdate import bs_label
from hajiri.db.models import AuditLog, CompanySetting
from hajiri.group import services
from tests.web.factories import PW, bearer, htmx, toasts


def _csv(r) -> list[dict]:
    text = r.content.decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------- /group

def test_overview_shows_both_companies_and_totals(world, figures):
    page = world.signed_in(world.ghr).get('/group')
    html = page.content.decode()
    assert page.status_code == 200
    assert 'Alpha Traders' in html and 'Beta Foods' in html and 'Gamma Outside' not in html
    assert 'Alpha Traders group' in html
    assert 'Needs attention' in html and '1 clock-in device offline' in html
    assert 'NPR 1,44,000' in html                                    # latest locked payroll cost, both companies
    # Items of the active company are plain links; others switch first.
    assert 'href="/settings/devices"' in html                          # Beta is ghr's active company
    assert 'name="next" value="/employees/onboarding?tab=probation"' in html


def test_overview_tabs_and_admin_sections(world, figures):
    page = world.signed_in(world.admin_a).get('/group').content.decode()
    assert 'href="/group/people"' in page and 'href="/group/reports"' in page
    assert 'Give group roles' in page and 'Add a company' in page and 'Pay figures in group views' in page
    page = world.signed_in(world.gexec).get('/group').content.decode()
    assert 'Give group roles' not in page and 'Add a company' not in page and 'Pay figures in group views' not in page


def test_overview_access(world):
    assert world.signed_in(world.solo).get('/group', **htmx()).status_code == 403
    # A standalone company's admin may open it to start a group, but sees no group figures.
    page = world.signed_in(world.admin_c).get('/group')
    assert page.status_code == 200 and b'not part of a group yet' in page.content
    assert b'href="/group/people"' not in page.content
    page = world.signed_in(world.admin_b).get('/group')
    assert page.status_code == 200 and b'Group figures are for group roles' in page.content


def test_overview_hides_pay_from_executives_when_the_company_says_so(world, figures):
    c = world.signed_in(world.admin_a)
    r = c.post('/_/group/pay-policy', {'hide': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Executives no longer see pay figures of this company.'
    assert CompanySetting.objects.get(company=world.alpha, key=services.POLICY_KEY).value == {services.HIDE_PAY: True}
    assert AuditLog.objects.filter(table_name='company_setting', row_id=str(world.alpha.id)).exists()
    page = world.signed_in(world.gexec).get('/group').content.decode()
    assert 'NPR 96,000' not in page and 'NPR 48,000' in page and '(1 hidden)' in page
    assert world.signed_in(world.gexec).post('/_/group/pay-policy', {'hide': 'on'}, **htmx()).status_code == 403
    r = c.post('/_/group/pay-policy', {}, **htmx())
    assert r.status_code == 204 and CompanySetting.objects.get(company=world.alpha, key=services.POLICY_KEY).value == {
        services.HIDE_PAY: False}


def test_overview_page_queries_do_not_grow_with_companies(world, figures):
    from hajiri.core import companies
    from hajiri.db.models import Company
    from tests.web.factories import make_employee
    c = world.signed_in(world.ghr)
    c.get('/group')
    with CaptureQueriesContext(connection) as two:
        c.get('/group')
    _, admin_a = world.session(world.admin_a)
    delta = companies.create_group_company(admin_a, {'code': 'DELTA', 'name_en': 'Delta Ltd'})
    make_employee(Company.objects.get(pk=delta['id']), name='Dev Delta')
    c.get('/group')
    with CaptureQueriesContext(connection) as three:
        page = c.get('/group')
    assert b'Delta Ltd' in page.content
    assert len(three) <= len(two) + 1          # the session carries one more company; the figures cost the same


# ---------------------------------------------------------------- /group/people

def test_people_directory_across_companies(world, figures):
    c = world.signed_in(world.ghr)
    page = c.get('/group/people')
    html = page.content.decode()
    assert page.status_code == 200
    assert 'Asha Admin' in html and 'Bina On Leave' in html and 'Gita Admin' not in html
    assert 'Lalit Leaver' not in html                                   # exited people only with status=all
    assert 'Lalit Leaver' in c.get('/group/people?status=all').content.decode()
    beta_only = c.get(f'/group/people?company={world.beta.id}').content.decode()
    assert 'Bina On Leave' in beta_only and 'Asha Admin' not in beta_only
    # A company outside the scope is not a valid choice: it filters nothing in.
    foreign = c.get(f'/group/people?company={world.gamma.id}').content.decode()
    assert 'Gita Admin' not in foreign
    assert 'Part time' in html and 'Employment type' in html


def test_people_csv_and_breakdowns_csv(world, figures):
    c = world.signed_in(world.ghr)
    rows = _csv(c.get('/group/people?group-people-export=csv'))
    names = {r['Name'] for r in rows}
    assert 'Gita Admin' not in names and {'Asha Admin', 'Jamuna Joiner', 'Bina On Leave'} <= names
    jamuna = next(r for r in rows if r['Name'] == 'Jamuna Joiner')
    assert jamuna['Company'] == 'Alpha Traders' and jamuna['Joined'] == bs_label(figures.this.start)
    b = _csv(c.get('/group/people?export=breakdowns'))
    by_company = {r['Value']: int(r['Headcount']) for r in b if r['Breakdown'] == 'Company'}
    assert by_company == {'Alpha Traders': 6, 'Beta Foods': 4}
    assert {'Branch', 'Department', 'Gender', 'Employment type'} <= {r['Breakdown'] for r in b}


def test_people_table_reloads_over_htmx(world, figures):
    r = world.signed_in(world.ghr).get('/group/people?group-people-q=Bina', **htmx('group-people'))
    assert r.status_code == 200 and b'Bina On Leave' in r.content and b'Asha Admin' not in r.content
    assert b'<html' not in r.content


@pytest.mark.parametrize('url', ['/group/people', '/group/reports', '/group/reports?format=csv'])
@pytest.mark.parametrize('who', ['solo', 'admin_b', 'admin_c', 'dual'])
def test_people_and_reports_refuse_non_group_users(world, who, url):
    r = world.signed_in(getattr(world, who)).get(url, **htmx())
    assert r.status_code == 403


# ---------------------------------------------------------------- /group/reports

def test_reports_page_and_csv(world, figures):
    c = world.signed_in(world.ghr)
    page = c.get('/group/reports')
    html = page.content.decode()
    assert page.status_code == 200 and 'Headcount' in html and 'Group total' in html and 'Gamma' not in html
    q = f'report=payroll&period=month&bs_year={figures.prev.year}&bs_month={figures.prev.month}'
    page = c.get(f'/group/reports?{q}').content.decode()
    assert 'Payroll summary' in page and '80000.00' in page
    r = c.get(f'/group/reports?{q}&format=csv')
    assert r['Content-Type'].startswith('text/csv') and f'group-payroll-{figures.prev.key}' in r['Content-Disposition']
    rows = _csv(r)
    assert [(x['Company'], x['Gross'], x['TDS'], x['Net']) for x in rows] == [
        ('Alpha Traders', '80000.00', '1000.00', '70200.00'), ('Beta Foods', '40000.00', '500.00', '35100.00'),
        ('Group total', '120000.00', '1500.00', '105300.00')]
    assert rows[0]['BS month'] == figures.prev.label


def test_reports_filters_and_partial(world, figures):
    c = world.signed_in(world.ghr)
    r = c.get(f'/group/reports?report=attendance&company={world.alpha.id}', **htmx('group-report'))
    assert r.status_code == 200 and b'id="group-report"' in r.content and b'<html' not in r.content
    assert b'Alpha Traders' in r.content and b'Beta Foods' not in r.content
    # A company outside the scope falls back to all companies of the scope (form choice), never Gamma.
    r = c.get(f'/group/reports?report=headcount&company={world.gamma.id}')
    assert r.status_code == 200 and b'Gamma' not in r.content and b'Beta Foods' in r.content
    fy = c.get('/group/reports?report=attrition&period=fy&format=csv')
    assert fy.status_code == 200 and {x['Company'] for x in _csv(fy)} == {'Alpha Traders', 'Beta Foods', 'Group total'}
    for kind in services.REPORTS:
        assert c.get(f'/group/reports?report={kind}').status_code == 200, kind


# ---------------------------------------------------------------- JSON API

def test_api_dashboard_and_reports(world, figures):
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
