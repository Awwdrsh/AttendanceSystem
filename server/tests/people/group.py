"""
Two companies of one group (Alpha, Beta) and one outside it (Gamma), with records in Beta for the isolation tests of
the people, employees and lifecycle slices:

    g.admin_a   admin of Alpha who started the group (group admin: admin in Beta too), working in Alpha
    g.admin_b   admin of Beta (home)
    g.admin_c   admin of Gamma (outside the group)
    g.dual      HR at Alpha (home) and employee + manager in Beta by membership (g.dual_beta is that record)
    g.ghr       employee of Beta with the group_hr role (HR in Alpha and Beta)
    g.beta_emp  a Beta employee reporting to g.dual_beta;  g.alpha_emp an Alpha employee

`g.client(person, company=None)` signs in (and switches company when given).
"""
from __future__ import annotations

from dataclasses import dataclass

from django.test import Client

from hajiri.core import auth, companies
from hajiri.db.models import Branch, Company, CompanyMembership, GroupMember
from tests.web.factories import PW, make_company, make_employee, make_user, sign_in


@dataclass
class Group:
    alpha: Company
    beta: Company
    gamma: Company
    admin_a: auth.AuthUser
    admin_b: auth.AuthUser
    admin_c: auth.AuthUser
    dual: auth.AuthUser
    ghr: auth.AuthUser
    dual_beta: object
    beta_emp: object
    alpha_emp: object
    beta_branch: Branch

    def client(self, person, company=None) -> Client:
        c = Client()
        sign_in(c, person)
        if company is not None:
            r = c.post('/api/v1/me/companies/switch', {'companyId': str(company.id)}, content_type='application/json')
            assert r.status_code == 200, r.content
        return c

    def session(self, person, company=None) -> auth.AuthUser:
        token = auth.login(person.email, PW).token
        user = auth.resolve_session(token)
        if company is not None and str(company.id) != user.company_id:
            user = auth.switch_company(user, token, str(company.id))
        return user


def build_group() -> Group:
    alpha = make_company('ALPHA', 'Alpha Traders')
    admin_a = make_user(alpha, roles=['admin'], employee=make_employee(alpha, name='Asha Admin'), email='admin@alpha.test')
    token = auth.login(admin_a.email, PW).token
    beta_id = companies.create_group_company(auth.resolve_session(token), {'code': 'BETA', 'name_en': 'Beta Foods'})['id']
    beta = Company.objects.get(pk=beta_id)
    alpha.refresh_from_db()
    gamma = make_company('GAMMA', 'Gamma Outside')
    admin_b = make_user(beta, roles=['admin'], employee=make_employee(beta, name='Bimal Admin'), email='admin@beta.test')
    admin_c = make_user(gamma, roles=['admin'], employee=make_employee(gamma, name='Gita Admin'), email='admin@gamma.test')
    dual = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Dipak Dual'), email='dual@alpha.test')
    branch = Branch.objects.create(company=beta, code='BKT', name_en='Bhaktapur')
    dual_beta = make_employee(beta, name='Dipak at Beta', branch=branch.id)
    CompanyMembership.objects.create(user_id=dual.id, company=beta, roles=['manager', 'employee'], employee=dual_beta)
    ghr = make_user(beta, roles=['employee'], employee=make_employee(beta, name='Gopal Group HR'), email='ghr@beta.test')
    GroupMember.objects.create(user_id=ghr.id, group_id=alpha.group_id, roles=['group_hr'])
    beta_emp = make_employee(beta, code='B100', name='Bina Beta', manager=dual_beta, branch=branch.id)
    alpha_emp = make_employee(alpha, code='A100', name='Anil Alpha')
    return Group(alpha, beta, gamma, admin_a, admin_b, admin_c, dual, ghr, dual_beta, beta_emp, alpha_emp, branch)
