"""The group JSON endpoints the React app calls (cookie session, Origin-checked): my companies and the switcher,
group roles, the dashboard, the cross-company directory and the consolidated reports. Gamma (outside the group) is
never shown and its ids are not found."""
from __future__ import annotations

import csv
import io
import json

import pytest

from hajiri.group import services

pytestmark = pytest.mark.django_db
J = 'application/json'


def _csv(r) -> list[dict]:
    return list(csv.DictReader(io.StringIO(r.content.decode('utf-8-sig'))))


# ---------------------------------------------------------------- my companies and the switcher

def test_my_companies_lists_access_roles_and_hub_flags(world):
    c = world.signed_in(world.dual)
    d = c.get('/api/v1/me/companies').json()
    assert [x['code'] for x in d['companies']] == ['ALPHA', 'BETA']
    alpha, beta = d['companies']
    assert alpha['active'] and alpha['home'] and alpha['roles'] == ['hr']
    assert not beta['active'] and set(beta['roles']) == {'manager', 'employee'}
    assert d['groupId'] == str(world.alpha.group_id) and d['groupRoles'] == []
    # HR of Alpha only (manager in Beta): one company's figures, so no group views.
    assert d['canViewGroup'] is False and d['canAdministerGroup'] is False and d['canTransfer'] is False

    g = world.signed_in(world.admin_a).get('/api/v1/me/companies').json()
    assert g['groupRoles'] == ['group_admin'] and g['canViewGroup'] and g['canAdministerGroup'] and g['canTransfer']
    assert g['groupName']
    h = world.signed_in(world.ghr).get('/api/v1/me/companies').json()
    assert h['canViewGroup'] and h['canTransfer'] and not h['canAdministerGroup']
    alone = world.signed_in(world.admin_c).get('/api/v1/me/companies').json()
    assert [x['code'] for x in alone['companies']] == ['GAMMA'] and alone['canStartGroup'] is True
    assert alone['canViewGroup'] is False


def test_switch_company_changes_the_session_and_refuses_other_tenants(world):
    c = world.signed_in(world.dual)
    r = c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type=J)
    assert r.status_code == 200
    assert r.json()['companyId'] == str(world.beta.id) and set(r.json()['roles']) == {'manager', 'employee'}
    assert r.json()['employeeId'] == str(world.dual_beta_employee.id)
    me = c.get('/api/v1/auth/me').json()['user']
    assert me['companyId'] == str(world.beta.id)
    assert [x['code'] for x in c.get('/api/v1/me/companies').json()['companies'] if x['active']] == ['BETA']
    # A company the person cannot reach is not found; junk is a validation error.
    assert c.post('/api/v1/me/companies/switch', {'companyId': str(world.gamma.id)}, content_type=J).status_code == 404
    bad = c.post('/api/v1/me/companies/switch', {'companyId': 'nope'}, content_type=J)
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'


def test_switch_from_another_site_is_refused_but_same_origin_passes(world):
    c = world.signed_in(world.dual)
    evil = c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type=J,
                  HTTP_ORIGIN='https://evil.test')
    assert evil.status_code == 403 and evil.json()['error'] == 'cross-site request refused'
    ok = c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type=J,
                HTTP_ORIGIN='http://testserver')
    assert ok.status_code == 200


def test_signed_out_is_401(world):
    from django.test import Client
    assert Client().get('/api/v1/me/companies').status_code == 401
    assert Client().get('/api/v1/group/dashboard').status_code == 401


# ---------------------------------------------------------------- group roles

def test_group_roles_for_group_admins_only(world):
    c = world.signed_in(world.admin_a)
    listed = c.get('/api/v1/group/roles').json()['items']
    assert {r['email'] for r in listed} == {world.admin_a.email, world.ghr.email, world.gexec.email}
    r = c.post('/api/v1/group/roles', json.dumps({'email': world.dual.email, 'roles': ['group_executive']}), content_type=J)
    assert r.status_code == 201 and r.json()['roles'] == ['group_executive']
    bad = c.post('/api/v1/group/roles', json.dumps({'email': world.dual.email, 'roles': ['admin']}), content_type=J)
    assert bad.status_code == 400
    outside = c.post('/api/v1/group/roles', json.dumps({'email': world.admin_c.email, 'roles': ['group_hr']}),
                     content_type=J)
    assert outside.status_code == 404
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 204
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 404
    assert world.signed_in(world.ghr).get('/api/v1/group/roles').status_code == 403
    assert world.signed_in(world.admin_c).delete(f'/api/v1/group/roles/{world.ghr.id}').status_code == 403


# ---------------------------------------------------------------- dashboard

def test_dashboard_for_group_roles_never_counts_gamma(world, figures):
    d = world.signed_in(world.gexec).get('/api/v1/group/dashboard?refresh=1').json()
    assert [x['code'] for x in d['companies']] == ['ALPHA', 'BETA']
    assert d['totals']['headcount'] == 10 and d['totals']['pendingApprovals'] == 3
    kinds = {(i['companyCode'], i['kind']) for i in d['needsAttention']}
    assert ('BETA', 'devices_offline') in kinds and ('BETA', 'approvals') in kinds
    assert all(i['next'].startswith('/') and i['companyId'] in {str(world.alpha.id), str(world.beta.id)}
               for i in d['needsAttention'])
    assert world.signed_in(world.admin_c).get('/api/v1/group/dashboard').status_code == 403
    assert world.signed_in(world.solo).get('/api/v1/group/dashboard').status_code == 403


# ---------------------------------------------------------------- directory

def test_people_directory_filters_pages_and_breakdowns(world, figures):
    c = world.signed_in(world.ghr)
    d = c.get('/api/v1/group/people').json()
    assert d['total'] == 10 and len(d['items']) == 10 and d['page'] == 1 and d['pageSize'] == 50
    assert {p['companyCode'] for p in d['items']} == {'ALPHA', 'BETA'}
    assert [x['code'] for x in d['companies']] == ['ALPHA', 'BETA']
    by_key = {b['key']: b for b in d['breakdowns']}
    assert set(by_key) == {'company', 'branch', 'department', 'gender', 'employment_type'}
    assert sum(r['value'] for r in by_key['company']['totals']) == 10
    beta = c.get(f'/api/v1/group/people?companyId={world.beta.id}').json()
    assert beta['total'] == 4 and {p['companyCode'] for p in beta['items']} == {'BETA'}
    women = c.get('/api/v1/group/people?gender=female').json()
    assert {p['name'] for p in women['items']} == {'Jamuna Joiner', 'Bina On Leave'}
    part = c.get('/api/v1/group/people?employmentType=part_time').json()
    assert [p['name'] for p in part['items']] == ['Bina On Leave']
    everyone = c.get('/api/v1/group/people?status=all').json()
    assert everyone['total'] == 11            # the leaver too
    found = c.get('/api/v1/group/people?q=joiner').json()
    assert [p['name'] for p in found['items']] == ['Jamuna Joiner']
    paged = c.get('/api/v1/group/people?pageSize=3&page=2').json()
    assert paged['total'] == 10 and len(paged['items']) == 3
    # Gamma is outside the scope: not found; a made-up value is a validation error.
    assert c.get(f'/api/v1/group/people?companyId={world.gamma.id}').status_code == 404
    bad = c.get('/api/v1/group/people?gender=robot')
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert world.signed_in(world.admin_c).get('/api/v1/group/people').status_code == 403
    assert world.signed_in(world.solo).get('/api/v1/group/people').status_code == 403


def test_people_csv_and_breakdowns_csv(world, figures):
    c = world.signed_in(world.ghr)
    r = c.get('/api/v1/group/people?format=csv')
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    rows = _csv(r)
    assert len(rows) == 10 and {x['Company'] for x in rows} == {'Alpha Traders', 'Beta Foods'}
    assert 'Gita Admin' not in {x['Name'] for x in rows}
    b = _csv(c.get('/api/v1/group/people?format=csv&export=breakdowns'))
    assert {x['Breakdown'] for x in b} >= {'Company', 'Gender', 'Employment type'}
    assert sum(int(x['Headcount']) for x in b if x['Breakdown'] == 'Company') == 10


# ---------------------------------------------------------------- reports

def test_report_catalog_and_reports(world, figures):
    c = world.signed_in(world.ghr)
    cat = c.get('/api/v1/group/reports').json()
    assert [r['key'] for r in cat['reports']] == list(services.REPORTS)
    assert [x['code'] for x in cat['companies']] == ['ALPHA', 'BETA']
    assert cat['current']['bsMonth'] == figures.this.month and len(cat['months']) == 12 and cat['fiscalYears']
    r = c.get(f'/api/v1/group/reports/payroll?bsYear={figures.prev.year}&bsMonth={figures.prev.month}').json()
    assert r['title'] == 'Payroll summary' and r['rows'][-1]['company'] == 'Group total'
    assert {col['key'] for col in r['columns']} >= {'company'}
    one = c.get(f'/api/v1/group/reports/headcount?companyId={world.alpha.id}').json()
    assert {row['company'] for row in one['rows']} == {'Alpha Traders'}
    fy = c.get('/api/v1/group/reports/attrition?period=fy&format=csv')
    assert fy.status_code == 200 and {x['Company'] for x in _csv(fy)} == {'Alpha Traders', 'Beta Foods', 'Group total'}
    assert 'attachment' in fy['Content-Disposition']
    assert c.get(f'/api/v1/group/reports/headcount?companyId={world.gamma.id}').status_code == 404
    assert c.get('/api/v1/group/reports/nope').status_code == 404
    assert c.get('/api/v1/group/reports/headcount?bsMonth=13').status_code == 400
    outsider = world.signed_in(world.admin_c)
    assert outsider.get('/api/v1/group/reports').status_code == 403
    assert outsider.get('/api/v1/group/reports/headcount').status_code == 403
