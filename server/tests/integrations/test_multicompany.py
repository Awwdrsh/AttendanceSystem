"""The JSON sign-in for integrations in a group of companies: /me describes the ACTIVE company."""
import pytest
from django.test import Client

from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import bearer

pytestmark = pytest.mark.django_db


def test_me_follows_the_active_company(world):
    token, _ = world.session(world.dual)
    me = Client().get('/api/v1/auth/me', **bearer(token)).json()['user']
    assert me['companyId'] == str(world.alpha.id) and me['roles'] == ['hr']
    token, _ = world.session(world.dual, world.beta)
    me = Client().get('/api/v1/auth/me', **bearer(token)).json()['user']
    assert me['companyId'] == str(world.beta.id) and set(me['roles']) == {'manager', 'employee'}
    assert me['employeeId'] == str(world.dual_beta_employee.id)
