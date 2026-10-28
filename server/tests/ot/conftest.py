"""The TS setup-db freshDb (company DEMO, Sita and Ram) with HR, Sita and Ram signed in and Sita reporting to Ram."""
import pytest

from hajiri.db.models import Employee, EmployeeAssignment
from tests.core.fixtures import *  # noqa: F401,F403
from tests.web.fixtures import *  # noqa: F401,F403

from .helpers import Api, World


@pytest.fixture
def world(demo, login_as):
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['manager'], demo['ram'])
    EmployeeAssignment.objects.filter(employee_id=demo['sita']).update(manager_id=demo['ram'])
    return World(demo, Api(hr.token), Api(sita.token), Api(ram.token), hr, sita, ram, login_as)


@pytest.fixture
def other_mgr(world):
    """A manager in the same company who manages nobody (ot-qa.test.ts E902)."""
    m = Employee.objects.create(company_id=world.company, emp_code='E902', name_en='Unrelated Manager')
    return Api(world.login_as(['manager'], str(m.id)).token)

