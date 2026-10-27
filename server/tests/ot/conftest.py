"""The TS setup-db freshDb (company DEMO, Sita and Ram) with HR, Sita and Ram signed in and Sita reporting to Ram."""
import pytest

from hajiri.core.db import execute, one
from tests.core.fixtures import *  # noqa: F401,F403
from tests.web.fixtures import *  # noqa: F401,F403

from .helpers import Api, World


@pytest.fixture
def world(demo, login_as):
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['manager'], demo['ram'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return World(demo, Api(hr.token), Api(sita.token), Api(ram.token), hr, sita, ram, login_as)


@pytest.fixture
def other_mgr(world):
    """A manager in the same company who manages nobody (ot-qa.test.ts E902)."""
    m = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E902', 'Unrelated Manager') RETURNING id",
            [world.company])
    return Api(world.login_as(['manager'], str(m['id'])).token)

