"""
A group of two companies (Alpha Traders, Beta Foods) and one company outside it (Gamma Outside), with people who
reach one company, two companies, or every company of the group through a group role.

    world.alpha / world.beta / world.gamma     Company rows
    world.admin_a   admin of Alpha (home); started the group, so group admin (admin in Beta too)
    world.admin_b   admin of Beta (home), with an employee record in Beta
    world.admin_c   admin of Gamma (outside the group)
    world.solo      HR of Alpha only
    world.dual      HR of Alpha (home) and manager + employee in Beta by membership, with a Beta employee record
    world.ghr       employee of Beta (home) with the group_hr role (HR in Alpha and Beta)
    world.gexec     employee of Alpha (home) with the group_executive role (executive in Alpha and Beta)

Every person is an AuthUser as create_user returns it; `world.signed_in(person, company=None)` gives a test Client
working in that company, and `world.session(person, company=None)` the (token, AuthUser) pair.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.test import Client

from hajiri.core import auth, companies
from hajiri.db.models import Company, CompanyMembership, GroupMember
from tests.web.factories import PW, htmx, make_company, make_employee, make_user, sign_in


@dataclass
class World:
    alpha: Company
    beta: Company
    gamma: Company
    admin_a: auth.AuthUser
    admin_b: auth.AuthUser
    admin_c: auth.AuthUser
    solo: auth.AuthUser
    dual: auth.AuthUser
    ghr: auth.AuthUser
    gexec: auth.AuthUser
    dual_beta_employee: object

    def session(self, person, company=None) -> tuple[str, auth.AuthUser]:
        token = auth.login(person.email, PW).token
        user = auth.resolve_session(token)
        if company is not None and str(company.id) != user.company_id:
            user = auth.switch_company(user, token, str(company.id))
        return token, user

    def signed_in(self, person, company=None) -> Client:
        c = Client()
        sign_in(c, person)
        if company is not None:
            r = c.post('/_/web/company/switch', {'company_id': str(company.id)}, **htmx())
            assert r.status_code == 204, r.content
        return c


@pytest.fixture
def world(db) -> World:
    alpha = make_company('ALPHA', 'Alpha Traders')
    admin_a = make_user(alpha, roles=['admin'], employee=make_employee(alpha, name='Asha Admin'), email='admin@alpha.test')
    token = auth.login(admin_a.email, PW).token
    beta_id = companies.create_group_company(auth.resolve_session(token), {'code': 'BETA', 'name_en': 'Beta Foods'})['id']
    beta = Company.objects.get(pk=beta_id)
    alpha.refresh_from_db()
    gamma = make_company('GAMMA', 'Gamma Outside')
    admin_b = make_user(beta, roles=['admin'], employee=make_employee(beta, name='Bimal Admin'), email='admin@beta.test')
    admin_c = make_user(gamma, roles=['admin'], employee=make_employee(gamma, name='Gita Admin'), email='admin@gamma.test')
    solo = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Sita Solo'), email='solo@alpha.test')
    dual = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Dipak Dual'), email='dual@alpha.test')
    dual_beta = make_employee(beta, name='Dipak at Beta')
    CompanyMembership.objects.create(user_id=dual.id, company=beta, roles=['manager', 'employee'], employee=dual_beta)
    ghr = make_user(beta, roles=['employee'], employee=make_employee(beta, name='Gopal Group HR'), email='ghr@beta.test')
    gexec = make_user(alpha, roles=['employee'], employee=make_employee(alpha, name='Kiran Group Exec'),
                      email='gexec@alpha.test')
    GroupMember.objects.create(user_id=ghr.id, group_id=alpha.group_id, roles=['group_hr'])
    GroupMember.objects.create(user_id=gexec.id, group_id=alpha.group_id, roles=['group_executive'])
    return World(alpha, beta, gamma, admin_a, admin_b, admin_c, solo, dual, ghr, gexec, dual_beta)
