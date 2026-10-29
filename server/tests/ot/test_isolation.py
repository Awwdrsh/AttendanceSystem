"""
Overtime across a group of companies: ids of another company are not found on every JSON endpoint, lists show only the active company, and switching company follows the person's roles there.

    Alpha (home of admin_a, who started the group: group admin) and Beta (admin_b) form a group; Gamma (admin_c)
    is outside it. Beta has an employee with an overtime slip and a pending overtime request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from django.test import Client

from hajiri.core import auth, companies
from hajiri.db.models import Company, OtRequest, OtSlip
from tests.web.factories import PW, bearer, make_company, make_employee, make_user, sign_in

from .helpers import days_ago


@dataclass
class Group:
    alpha: Company
    beta: Company
    admin_a: auth.AuthUser
    admin_b: auth.AuthUser
    admin_c: auth.AuthUser
    beta_emp: object
    alpha_slip: OtSlip
    beta_slip: OtSlip
    beta_request: OtRequest

    def token(self, person, company=None) -> str:
        token = auth.login(person.email, PW).token
        user = auth.resolve_session(token)
        if company is not None and str(company.id) != user.company_id:
            auth.switch_company(user, token, str(company.id))
        return token

    def browser(self, person, company=None) -> Client:
        c = Client()
        sign_in(c, person)
        if company is not None:
            assert c.post('/api/v1/me/companies/switch', {'companyId': str(company.id)}, content_type='application/json').status_code == 200
        return c


@pytest.fixture
def group(db) -> Group:
    alpha = make_company('ALPHA', 'Alpha Traders')
    admin_a = make_user(alpha, roles=['admin'], employee=make_employee(alpha, name='Asha Admin'), email='admin@alpha.test')
    token = auth.login(admin_a.email, PW).token
    beta = Company.objects.get(pk=companies.create_group_company(auth.resolve_session(token),
                                                                 {'code': 'BETA', 'name_en': 'Beta Foods'})['id'])
    gamma = make_company('GAMMA', 'Gamma Outside')
    admin_b = make_user(beta, roles=['admin'], employee=make_employee(beta, name='Bimal Admin'), email='admin@beta.test')
    admin_c = make_user(gamma, roles=['admin'], employee=make_employee(gamma, name='Gita Admin'), email='admin@gamma.test')
    beta_emp = make_employee(beta, name='Bina Beta')
    alpha_emp = make_employee(alpha, name='Arun Alpha')
    alpha_slip = OtSlip.objects.create(employee=alpha_emp, local_date=days_ago(2), detected_min=60, day_type='working')
    beta_slip = OtSlip.objects.create(employee=beta_emp, local_date=days_ago(2), detected_min=90, day_type='working')
    beta_request = OtRequest.objects.create(employee=beta_emp, local_date=days_ago(1), requested_min=60)
    return Group(alpha, beta, admin_a, admin_b, admin_c, beta_emp, alpha_slip, beta_slip, beta_request)


def _api(method, url, body=None):
    def run(g: Group, person, company=None):
        h = bearer(g.token(person, company))
        if method == 'get':
            return Client().get(url(g), **h)
        return Client().post(url(g), json.dumps(body or {}), content_type='application/json', **h)
    return run


CASES = {
    'api approve': _api('post', lambda g: f'/api/v1/ot/slips/{g.beta_slip.id}/approve', {'approvedMin': 30}),
    'api reject': _api('post', lambda g: f'/api/v1/ot/slips/{g.beta_slip.id}/reject', {'reason': 'not authorised'}),
    'api comp-off': _api('post', lambda g: f'/api/v1/ot/slips/{g.beta_slip.id}/comp-off'),
    'api request cancel': _api('post', lambda g: f'/api/v1/ot/requests/{g.beta_request.id}/cancel'),
    'api request for a Beta employee': _api('post', lambda g: '/api/v1/ot/requests',
                                            None),   # body filled in the test (needs the employee id)
    'api requests filtered by a Beta employee': _api('get', lambda g: f'/api/v1/ot/requests?employeeId={g.beta_emp.id}'),
    'api slips filtered by a Beta employee': _api('get', lambda g: f'/api/v1/ot/slips?employeeId={g.beta_emp.id}'),
}


def test_nothing_changed_in_beta_after_the_refused_requests(group):
    for name in ('api approve', 'api reject', 'api comp-off', 'api request cancel'):
        CASES[name](group, group.admin_a)
        CASES[name](group, group.admin_c)
    group.beta_slip.refresh_from_db()
    group.beta_request.refresh_from_db()
    assert group.beta_slip.status == OtSlip.Status.DETECTED and group.beta_slip.reviewed_by_id is None
    assert group.beta_request.status == OtRequest.Status.PENDING


@pytest.mark.parametrize('name', list(CASES))
def test_ids_of_another_company_are_not_found(group, name):
    case = CASES[name]
    if name == 'api request for a Beta employee':
        case = _api('post', lambda g: '/api/v1/ot/requests',
                    {'employeeId': str(group.beta_emp.id), 'localDate': days_ago(3), 'requestedMin': 30})
    r = case(group, group.admin_c)
    assert r.status_code == 404, f'{name}: outsider got {r.status_code}'
    r = case(group, group.admin_a)                       # group admin, working in Alpha
    assert r.status_code == 404, f'{name}: group admin working in Alpha got {r.status_code}'
    r = case(group, group.admin_b)                       # Beta's admin: the record is really there
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} {r.content[:200]}'


def test_lists_show_only_the_active_company(group):
    def slip_ids(token):
        return {s['id'] for s in Client().get('/api/v1/ot/slips', **bearer(token)).json()['slips']}

    in_alpha = group.token(group.admin_a)
    assert slip_ids(in_alpha) == {str(group.alpha_slip.id)}
    in_beta = group.token(group.admin_a, group.beta)       # group admin is admin of Beta once working there
    assert slip_ids(in_beta) == {str(group.beta_slip.id)}
    assert slip_ids(group.token(group.admin_c)) == set()

    reqs = Client().get('/api/v1/ot/requests', **bearer(in_alpha)).json()['requests']
    assert reqs == []
    reqs = Client().get('/api/v1/ot/requests', **bearer(in_beta)).json()['requests']
    assert [r['id'] for r in reqs] == [str(group.beta_request.id)]

    reg = Client().get('/api/v1/ot/register', {'from': days_ago(10), 'to': days_ago(0)}, **bearer(in_alpha)).json()
    assert [r['name_en'] for r in reg['register']] == ['Arun Alpha']

    # The cookie session of the overtime page follows a company switch.
    def names(c):
        return {s['name_en'] for s in c.get('/api/v1/ot/slips').json()['slips']}
    assert names(group.browser(group.admin_a)) == {'Arun Alpha'}
    assert names(group.browser(group.admin_a, group.beta)) == {'Bina Beta'}


def test_group_admin_decides_a_beta_slip_after_switching(group):
    c = group.browser(group.admin_a, group.beta)
    r = c.post(f'/api/v1/ot/slips/{group.beta_slip.id}/approve', {'approvedMin': 45}, content_type='application/json')
    assert r.status_code == 200, r.content
    group.beta_slip.refresh_from_db()
    assert group.beta_slip.status == OtSlip.Status.APPROVED and group.beta_slip.approved_min == 45
    assert str(group.beta_slip.reviewed_by_id) == group.admin_a.id

