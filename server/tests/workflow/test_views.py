"""Pages and HTMX fragments: Approvals inbox, request history, approval flows set-up, delegation and the chain preview."""
import pytest
from django.test import Client

from tests.support.sql import execute, one
from hajiri.workflow import flow_admin, service


from .helpers import emp, file_leave, unit

pytestmark = pytest.mark.usefixtures('leave_handler')


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.fixture
def w(demo, login_as):
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return {'demo': demo, 'c': demo['company_id'], 'hr': login_as(['hr']), 'ram': login_as(['manager'], demo['ram']),
            'sita': login_as(['employee'], demo['sita'])}


def test_approvals_badge_and_nav(w):
    file_leave(w['c'], w['demo']['sita'], '2026-11-26')
    assert service.inbox_count(w['ram'].user) == 1
    from hajiri.workflow.nav import BADGES
    assert BADGES['approvals'](w['ram'].user) == 1
    # an employee with something to decide sees the hub thanks to the badge
    assert BADGES['approvals'](w['sita'].user) == 0
