"""
Payroll in a group of companies: every company runs its own payroll (its own runs, payslips, holds, opening balances,
tax savings and company profile on certificates), and ids of another company behave as not found on every payroll
page, partial and JSON endpoint.

Each case makes one record in Beta and requests it as
- the admin of Gamma (a company outside the group): 404;
- the group admin while working in Alpha (they may switch to Beta, but are not working in it): 404;
- the admin of Beta (control): not 404, so the case really reaches the record.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from hajiri.core.settings_store import put_setting
from hajiri.db import models as m
from tests.multicompany.conftest import World, world  # noqa: F401 - the group-of-companies fixture
from tests.web.factories import htmx, make_employee


LOCKED = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


def _period(w, company=None):
    return m.PayrollPeriod.objects.create(company=company or w.beta, bs_year=2083, bs_month=7, start_ad=date(2026, 10, 18),
                                          end_ad=date(2026, 11, 16), days=30)


def payroll_run(w):
    return m.PayrollRun.objects.create(company=w.beta, period=_period(w))


def computed_run(w):
    return m.PayrollRun.objects.create(company=w.beta, period=_period(w), status=m.PayrollRun.Status.COMPUTED)


def _payslip(w, run=None, *, published=False, employee_id=None):
    run = run or m.PayrollRun.objects.create(company=w.beta, period=_period(w), status=m.PayrollRun.Status.LOCKED,
                                             locked_at=LOCKED)
    z = Decimal('0')
    return m.Payslip.objects.create(run=run, employee_id=employee_id or w.admin_b.employee_id, inputs_snapshot={'input': {}, 'result': {}},
                                    bs_days_in_month=30, paid_days=Decimal('30'), gross=Decimal('50000'), taxable=Decimal('50000'),
                                    tds=Decimal('500'), total_deductions=Decimal('500'), net=Decimal('49500'), lop_days=z, ot_hours=z,
                                    published_at=LOCKED if published else None)


def payslip(w):
    return _payslip(w, published=True)


def held(w):
    run = m.PayrollRun.objects.create(company=w.beta, period=_period(w), status=m.PayrollRun.Status.LOCKED,
                                      locked_at=LOCKED)
    _payslip(w, run)
    return m.PayrollHold.objects.create(company=w.beta, employee_id=w.admin_b.employee_id, run=run, amount=Decimal('49500'),
                                        reason='documents pending')


def opening(w):
    return m.PayrollOpeningBalance.objects.create(company=w.beta, employee_id=w.admin_b.employee_id, fiscal_year_start=2083,
                                                  gross=Decimal('100000'), taxable=Decimal('100000'))


def structure(w):
    return m.SalaryStructure.objects.create(company=w.beta, code='BETA', name_en='Beta structure')


def beta_employee(w):
    return m.Employee.objects.get(pk=w.admin_b.employee_id)


def declared_employee(w):
    m.TaxDeclaration.objects.create(employee_id=w.admin_b.employee_id, fiscal_year_start=2083, status=m.TaxDeclaration.Status.SUBMITTED)
    return beta_employee(w)


# (name, method, url with {id}, maker, htmx?[, data])
CASES = [
    # pages and partials
    ('run page', 'get', '/payroll/runs/{id}', payroll_run, False),
    ('run override form', 'get', '/_/payroll/runs/{id}/override', payroll_run, True),
    ('run input form', 'get', '/_/payroll/runs/{id}/inputs/new', payroll_run, True),
    ('run input post', 'post', '/_/payroll/runs/{id}/inputs/new', payroll_run, True,
     {'employee_id': '{emp}', 'component_code': 'ALLOWANCE', 'amount': '100'}),
    ('run hold form', 'get', '/_/payroll/runs/{id}/hold', computed_run, True),
    ('run preflight', 'post', '/_/payroll/runs/{id}/action/preflight', payroll_run, True),
    ('salary sheet print', 'get', '/_/payroll/runs/{id}/salary-sheet.html', computed_run, False),
    ('ssf sheet', 'get', '/_/payroll/runs/{id}/ssf-sheet.csv', payroll_run, False),
    ('payslip page', 'get', '/me/payslips/{id}', payslip, False),
    ('payslip print', 'get', '/_/payroll/payslips/{id}/print', payslip, False),
    ('hold release form', 'get', '/_/payroll/holds/{id}/release', held, True),
    ('hold cancel', 'post', '/_/payroll/holds/{id}/cancel', held, True),
    ('opening delete', 'post', '/_/payroll/opening-balances/{id}/delete', opening, True),
    ('cit apply form', 'get', '/_/payroll/optimum-cit/{id}/apply', beta_employee, True),
    ('certificate', 'get', '/_/payroll/certificates/{id}', beta_employee, False),
    # employee filters and prefills (the employee picker would draw the name of the id)
    ('loans of employee', 'get', '/payroll/loans?employee_id={id}', beta_employee, False),
    ('salaries of employee', 'get', '/payroll/assignments?employee_id={id}', beta_employee, False),
    ('salary form for employee', 'get', '/_/payroll/assignments/new?employee_id={id}', beta_employee, True),
    ('declaration of employee', 'get', '/payroll/declarations?employee_id={id}', beta_employee, False),
    # JSON API
    ('run api', 'get', '/api/v1/payroll/runs/{id}', payroll_run, False),
    ('run inputs api', 'get', '/api/v1/payroll/runs/{id}/inputs', payroll_run, False),
    ('run preflight api', 'post_json', '/api/v1/payroll/runs/{id}/preflight', payroll_run, False),
    ('run compute api', 'post_json', '/api/v1/payroll/runs/{id}/compute', payroll_run, False),
    ('run lock api', 'post_json', '/api/v1/payroll/runs/{id}/lock', payroll_run, False),
    ('run hold api', 'post_json', '/api/v1/payroll/runs/{id}/holds', computed_run, False,
     {'employeeId': '{emp}', 'reason': 'documents pending'}),
    ('salary sheet api', 'get', '/api/v1/payroll/runs/{id}/salary-sheet', computed_run, False),
    ('ssf sheet api', 'get', '/api/v1/payroll/runs/{id}/ssf-sheet.csv', payroll_run, False),
    ('payslip api', 'get', '/api/v1/payroll/payslips/{id}', payslip, False),
    ('payslip html api', 'get', '/api/v1/payroll/payslips/{id}/html', payslip, False),
    ('hold release api', 'post_json', '/api/v1/payroll/holds/{id}/release', held, False, {'mode': 'outside'}),
    ('hold cancel api', 'post_json', '/api/v1/payroll/holds/{id}/cancel', held, False),
    ('opening delete api', 'delete', '/api/v1/payroll/opening-balances/{id}', opening, False),
    ('structure version api', 'post_json', '/api/v1/payroll/structures/{id}/versions', structure, False,
     {'components': [{'code': 'BASIC'}]}),
    ('declaration verify api', 'post_json', '/api/v1/payroll/declarations/{id}/verify', declared_employee, False,
     {'fiscalYearStart': 2083, 'decision': 'verified'}),
    ('certificate api', 'get', '/api/v1/payroll/certificates/{id}', beta_employee, False),
    ('payslips of employee api', 'get', '/api/v1/payroll/payslips?employeeId={id}', beta_employee, False),
]


def _call(client, method, url, hx, data=None):
    headers = htmx() if hx else {}
    if method == 'post':
        return client.post(url, data or {}, **headers)
    if method == 'post_json':
        return client.post(url, json.dumps(data or {}), content_type='application/json', **headers)
    if method == 'delete':
        return client.delete(url, **headers)
    return client.get(url, **headers)


def _fill(data, w):
    return json.loads(json.dumps(data).replace('{emp}', str(w.admin_b.employee_id)))


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_payroll_ids_of_another_company_are_not_found(world, case):
    name, method, url, maker, hx, *rest = case
    obj = maker(world)
    target = url.format(id=obj.pk)
    data = _fill(rest[0], world) if rest else None
    r = _call(world.signed_in(world.admin_c), method, target, hx, data)
    assert r.status_code == 404, f'{name}: outsider got {r.status_code}'
    r = _call(world.signed_in(world.admin_a), method, target, hx, data)
    assert r.status_code == 404, f'{name}: group admin working in Alpha got {r.status_code}'
    r = _call(world.signed_in(world.admin_b), method, target, hx, data)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_ids_inside_a_body_are_checked_against_the_active_company(world):
    """An Alpha run cannot take inputs, holds or loans for a Beta employee; the ids behave as not found."""
    run = m.PayrollRun.objects.create(company=world.alpha, period=_period(world, world.alpha))
    beta_emp = str(world.admin_b.employee_id)
    c = world.signed_in(world.admin_a)
    r = c.post(f'/api/v1/payroll/runs/{run.id}/inputs', json.dumps({'inputs': [{'employeeId': beta_emp, 'componentCode': 'ALLOWANCE',
                                                                                  'amount': '100.00'}]}), content_type='application/json')
    assert r.status_code in (400, 404) and not m.PayrollInput.objects.filter(run=run).exists()
    r = c.post('/api/v1/payroll/loans', json.dumps({'employeeId': beta_emp, 'principal': '1000.00', 'emi': '100.00', 'startBsYear': 2083,
                                                    'startBsMonth': 7}), content_type='application/json')
    assert r.status_code == 404 and r.json()['error'] == 'employee not found'
    r = c.put('/api/v1/payroll/opening-balances', json.dumps({'employeeId': beta_emp, 'fiscalYearStart': 2083}),
              content_type='application/json')
    assert r.status_code == 404
    r = c.post('/api/v1/payroll/runs', json.dumps({'periodId': str(_period(world).id)}), content_type='application/json')
    assert r.status_code == 404 and r.json()['error'] == 'payroll period not found'


def test_each_company_runs_its_own_payroll_and_switching_follows_the_active_company(world):
    alpha_run = m.PayrollRun.objects.create(company=world.alpha, period=_period(world, world.alpha))
    beta_run = payroll_run(world)
    c = world.signed_in(world.admin_a)
    ids = {r['id'] for r in c.get('/api/v1/payroll/runs').json()['runs']}
    assert ids == {str(alpha_run.id)}
    assert {p['company_id'] for p in c.get('/api/v1/payroll/periods').json()['periods']} == {str(world.alpha.id)}
    page = c.get('/payroll')
    assert page.status_code == 200
    # Group admin switches to Beta: Beta's runs, and Beta's run is now found.
    assert c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx()).status_code == 204
    ids = {r['id'] for r in c.get('/api/v1/payroll/runs').json()['runs']}
    assert ids == {str(beta_run.id)}
    assert c.get(f'/payroll/runs/{beta_run.id}').status_code == 200
    assert c.get(f'/payroll/runs/{alpha_run.id}').status_code == 404


def test_group_hr_runs_payroll_in_every_company_of_the_group(world):
    """group_hr is HR in each company of the group, one company at a time; Gamma stays out of reach."""
    beta_run = payroll_run(world)
    c = world.signed_in(world.ghr, world.alpha)
    assert c.get('/api/v1/payroll/runs').status_code == 200
    assert c.get(f'/api/v1/payroll/runs/{beta_run.id}').status_code == 404
    c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx())
    assert c.get(f'/api/v1/payroll/runs/{beta_run.id}').status_code == 200
    r = c.post('/_/web/company/switch', {'company_id': str(world.gamma.id)}, **htmx())
    assert r.status_code == 404


def test_certificates_carry_the_active_companys_own_profile(world):
    """Each company has its own name, address and PAN on tax and salary certificates."""
    put_setting(world.alpha.id, 'company.profile', {'nameEn': 'Alpha Traders Pvt. Ltd.', 'pan': '600000001'})
    put_setting(world.beta.id, 'company.profile', {'nameEn': 'Beta Foods Pvt. Ltd.', 'pan': '600000002'})
    a = world.signed_in(world.admin_a).get('/api/v1/payroll/certificates/me', {'kind': 'tax', 'fiscalYearStart': 2083})
    assert a.status_code == 200
    text = a.content.decode()
    assert 'Alpha Traders Pvt. Ltd.' in text and 'PAN 600000001' in text and 'Beta Foods' not in text
    b = world.signed_in(world.admin_b).get('/api/v1/payroll/certificates/me', {'kind': 'tax', 'fiscalYearStart': 2083})
    text = b.content.decode()
    assert 'Beta Foods Pvt. Ltd.' in text and 'PAN 600000002' in text and 'Alpha Traders' not in text


def test_a_person_in_two_companies_sees_the_payslips_of_the_active_company_only(world):
    """Dipak works in Alpha (HR, home) and Beta (employee, by membership), with one employee record in each company."""
    beta_slip = _payslip(world, published=True, employee_id=world.dual_beta_employee.id)
    c = world.signed_in(world.dual)
    assert c.get(f'/me/payslips/{beta_slip.id}').status_code == 404
    c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx())
    assert c.get(f'/me/payslips/{beta_slip.id}').status_code == 200
    mine = c.get('/api/v1/payroll/payslips').json()['payslips']
    assert [p['id'] for p in mine] == [str(beta_slip.id)]


def test_another_companys_employee_cannot_be_given_a_salary(world):
    std = m.SalaryStructure.objects.create(company=world.alpha, code='ALPHA', name_en='Alpha structure')
    v = m.SalaryStructureVersion.objects.create(structure=std, version=1, components=[{'code': 'BASIC'}])
    beta_std = m.SalaryStructureVersion.objects.create(structure=structure(world), version=1, components=[{'code': 'BASIC'}])
    c = world.signed_in(world.admin_a)
    body = {'employeeId': str(world.admin_b.employee_id), 'structureVersionId': str(v.id), 'from': '2026-07-17',
            'baseAmounts': {'BASIC': '50000.00'}}
    r = c.post('/api/v1/payroll/assignments', json.dumps(body), content_type='application/json')
    assert r.status_code == 404 and r.json()['error'] == 'employee not found'
    own = make_employee(world.alpha, name='Alpha Person')
    r = c.post('/api/v1/payroll/assignments', json.dumps({**body, 'employeeId': str(own.id), 'structureVersionId': str(beta_std.id)}),
               content_type='application/json')
    assert r.status_code == 404 and r.json()['error'] == 'salary structure version not found'
    assert not m.SalaryAssignment.objects.filter(employee__in=[own.id, world.admin_b.employee_id]).exists()
