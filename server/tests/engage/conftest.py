"""The engage.test.ts world: a company with two departments, Sita (reports to Ram), Ram (employee and manager), five
more people (Person 0 also reports to Ram; 0-2 in Engineering, 3-4 in Operations), an HR account without an employee,
and another company with its own HR."""
from types import SimpleNamespace

import pytest
from django.test import Client

from hajiri.db.models import Branch, OrgUnit
from hajiri.engage import services
from tests.web.factories import make_company, make_employee, make_user, sign_in
from tests.web.fixtures import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _fresh_suggestion_guard():
    services._suggestion_hits.clear()
    yield
    services._suggestion_hits.clear()


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


@pytest.fixture
def world(db):
    co = make_company('DEMO', 'Demo Company')
    branch = Branch.objects.create(company=co, code='HQ', name_en='Head Office').id
    dept_a = str(OrgUnit.objects.create(company=co, kind=OrgUnit.Kind.DEPARTMENT, code='ENG', name_en='Engineering').id)
    dept_b = str(OrgUnit.objects.create(company=co, kind=OrgUnit.Kind.DEPARTMENT, code='OPS', name_en='Operations').id)
    ram_e = make_employee(co, code='E002', name='Ram Thapa', branch=branch, org_unit=dept_a)
    sita_e = make_employee(co, code='E001', name='Sita Sharma', branch=branch, org_unit=dept_a, manager=ram_e)
    hr = make_user(co, roles=['hr'], name='HR Person')
    sita = make_user(co, roles=['employee'], employee=sita_e)
    ram = make_user(co, roles=['employee', 'manager'], employee=ram_e)
    extra = []
    for i in range(5):
        e = make_employee(co, code=f'E10{i}', name=f'Person {i}', branch=branch, org_unit=dept_a if i < 3 else dept_b,
                          manager=ram_e if i == 0 else None)
        extra.append(SimpleNamespace(id=str(e.id), emp=e, user=make_user(co, roles=['employee'], employee=e)))
    other = make_company('OTHERENG', 'Other Co')
    foreign_hr = make_user(other, roles=['hr'], name='Other HR')
    return SimpleNamespace(co=co, company_id=str(co.id), branch=str(branch), dept_a=dept_a, dept_b=dept_b, hr=hr, sita=sita,
                           ram=ram, sita_id=str(sita_e.id), ram_id=str(ram_e.id), sita_e=sita_e, extra=extra,
                           other=other, foreign_hr=foreign_hr)


@pytest.fixture(autouse=True)
def _urlconf(settings):
    """Fall back to a URLconf without broken apps when another slice's urls.py does not import yet."""
    import importlib
    try:
        importlib.import_module('config.urls')
        import django.urls
        django.urls.get_resolver().url_patterns  # noqa: B018
    except Exception:  # noqa: BLE001
        settings.ROOT_URLCONF = 'tests.engage.urls_resilient'
    yield
