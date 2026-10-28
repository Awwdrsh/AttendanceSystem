import pytest

from tests.core.fixtures import *  # noqa: F401,F403
from tests.web.fixtures import *  # noqa: F401,F403


@pytest.fixture
def reg(demo, login_as):
    """The TS beforeAll: HR, Sita (employee) and Ram (manager), with Sita reporting to Ram."""
    from hajiri.db.models import EmployeeAssignment
    EmployeeAssignment.objects.filter(employee_id=demo['sita']).update(manager_id=demo['ram'])
    return {
        'hr': login_as(['hr']),
        'sita': login_as(['employee'], demo['sita']),
        'ram': login_as(['manager'], demo['ram']),
    }
