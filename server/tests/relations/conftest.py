"""The relations.test.ts world: the DEMO company with a branch, Sita (reports to Ram), Ram (employee and manager), two HR
accounts without an employee, a committee-member-to-be (CM1), an outsider (OUT1), and another company with its own HR."""
import base64
from datetime import date
from types import SimpleNamespace

import pytest
from django.test import Client

from hajiri.core.db import execute, one
from tests.web.factories import make_company, make_employee, make_user, sign_in
from tests.web.fixtures import *  # noqa: F401,F403

PDF_DATA_URL = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


@pytest.fixture
def world(db):
    co = make_company('DEMO', 'Demo Company')
    branch = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'HQ', 'Head Office') RETURNING id", [co.id])['id'])

    def new_employee(code: str, **extra):
        extra.setdefault('join_date', date(2020, 1, 1))
        return make_employee(co, code=code, name=f'Person {code}', branch=branch, since=date(2020, 1, 1), **extra)

    ram_e = make_employee(co, code='E002', name='Ram Thapa', branch=branch, since=date(2020, 1, 1), join_date=date(2020, 1, 1))
    sita_e = make_employee(co, code='E001', name='Sita Sharma', branch=branch, manager=ram_e, since=date(2020, 1, 1),
                           join_date=date(2020, 1, 1))
    hr = make_user(co, roles=['hr'], name='HR One')
    hr2 = make_user(co, roles=['hr'], name='HR Two')
    sita = make_user(co, roles=['employee'], employee=sita_e)
    ram = make_user(co, roles=['employee', 'manager'], employee=ram_e)
    member_e = new_employee('CM1')
    member = make_user(co, roles=['employee'], employee=member_e)
    outsider = make_user(co, roles=['employee'], employee=new_employee('OUT1'))
    other = make_company('OTHERREL', 'Other Co')
    other_hr = make_user(other, roles=['hr'], name='Other HR', email='hr@other-rel.local')
    return SimpleNamespace(co=co, company_id=str(co.id), branch=branch, sita_e=sita_e, ram_e=ram_e, sita=sita, ram=ram, hr=hr, hr2=hr2,
                           member=member, member_e=member_e, outsider=outsider, other=other, other_hr=other_hr,
                           new_employee=new_employee, execute=execute)
