"""
pytest fixtures built on tests.web.factories. In a slice's tests/<app>/conftest.py:

    from tests.web.fixtures import *  # noqa: F401,F403
"""
import pytest

from .factories import make_company, make_employee, make_user, sign_in, signed_in_client  # noqa: F401

__all__ = ['acme', 'other_company', 'employee', 'hr_client', 'employee_client', 'manager_setup', 'make_client']


@pytest.fixture
def acme(db):
    return make_company('ACME', 'Acme Pvt. Ltd.')


@pytest.fixture
def other_company(db):
    return make_company('OTHER', 'Other Pvt. Ltd.')


@pytest.fixture
def employee(acme):
    return make_employee(acme, name='Sita Sharma')


@pytest.fixture
def hr_client(acme):
    """(client, user) signed in as HR with a linked employee."""
    return signed_in_client(acme, roles=['hr'])


@pytest.fixture
def employee_client(acme):
    return signed_in_client(acme, roles=['employee'])


@pytest.fixture
def manager_setup(acme):
    """A manager (signed in) with one direct report: (client, manager_user, manager_employee, report_employee)."""
    boss = make_employee(acme, name='Ram Manager')
    report = make_employee(acme, name='Sita Report', manager=boss)
    client, user = signed_in_client(acme, roles=['manager'], employee=boss)
    return client, user, boss, report


@pytest.fixture
def make_client(acme):
    """Factory: make_client(roles=['payroll_admin'], employee=True) -> (client, user)."""
    def _make(roles=('employee',), **kw):
        return signed_in_client(acme, roles=roles, **kw)
    return _make
