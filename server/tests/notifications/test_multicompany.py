"""Notifications in a group of companies: role broadcasts reach members and group-role holders of the company, a
member's employee record leads to their account, and the inbox can be read per company."""
import pytest

from hajiri.notifications.service import (
    inbox, mark_all_read, notify, notify_role, unread_count, user_id_for_employee, user_ids_with_role,
)
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def test_role_broadcasts_reach_everyone_who_works_in_the_company(world):
    hr_beta = set(user_ids_with_role(str(world.beta.id), 'hr'))
    # Beta's own admin, the group admin (admin everywhere) and the group HR person; not Alpha-only HR.
    assert {world.admin_b.id, world.admin_a.id, world.ghr.id} <= hr_beta
    assert world.solo.id not in hr_beta and world.admin_c.id not in hr_beta
    managers = set(user_ids_with_role(str(world.beta.id), 'manager'))
    assert world.dual.id in managers                                  # manager in Beta by membership
    assert world.dual.id not in set(user_ids_with_role(str(world.alpha.id), 'manager'))
    n = notify_role(str(world.beta.id), 'manager', template='x', title='Team note', body='b')
    assert n == len(managers)
    assert inbox(world.dual.id, company_id=str(world.beta.id))['notifications'][0]['title'] == 'Team note'


def test_a_members_employee_record_leads_to_their_account(world):
    assert user_id_for_employee(str(world.dual_beta_employee.id)) == world.dual.id
    assert user_id_for_employee(world.dual.employee_id) == world.dual.id
    assert user_id_for_employee(None) is None


def test_the_inbox_per_company(world):
    a, b = str(world.alpha.id), str(world.beta.id)
    notify(company_id=a, user_id=world.dual.id, template='x', title='From Alpha', body='a')
    notify(company_id=b, user_id=world.dual.id, template='x', title='From Beta', body='b')
    notify(company_id=None, user_id=world.dual.id, template='x', title='Account notice', body='c')
    titles = lambda cid: [n['title'] for n in inbox(world.dual.id, company_id=cid)['notifications']]  # noqa: E731
    assert titles(a) == ['Account notice', 'From Alpha']
    assert titles(b) == ['Account notice', 'From Beta']
    assert len(titles(None)) == 3
    assert unread_count(world.dual.id, company_id=a) == 2 and unread_count(world.dual.id) == 3
    assert mark_all_read(world.dual.id, company_id=b) == 2
    assert unread_count(world.dual.id) == 1


def test_marking_read_stays_in_the_active_company(world):
    from django.test import Client

    from hajiri.db.models import NotificationOutbox
    from tests.core.fixtures import bearer
    from tests.web.factories import htmx
    a, b = str(world.alpha.id), str(world.beta.id)
    notify(company_id=a, user_id=world.dual.id, template='x', title='From Alpha', body='a')
    notify(company_id=b, user_id=world.dual.id, template='x', title='From Beta', body='b')
    alpha_note = NotificationOutbox.objects.get(user_id=world.dual.id, title='From Alpha')
    token, _ = world.session(world.dual, world.beta)
    c = Client()
    # Working in Beta, Alpha's notification is not found and read-all leaves it unread.
    r = c.post(f'/api/v1/notifications/{alpha_note.id}/read', **bearer(token))
    assert r.status_code == 404 and r.json() == {'error': 'notification not found'}
    assert c.post('/api/v1/notifications/read-all', **bearer(token)).json() == {'marked': 1}
    assert [n['title'] for n in c.get('/api/v1/notifications', **bearer(token)).json()['notifications']] == ['From Beta']
    alpha_note.refresh_from_db()
    assert alpha_note.read_at is None
    # The bell's HTMX actions follow the active company too.
    page = world.signed_in(world.dual, world.beta)
    assert page.post(f'/_/notifications/{alpha_note.id}/read', **htmx()).status_code == 404
    assert page.post('/_/notifications/read-all', **htmx()).status_code == 204
    alpha_note.refresh_from_db()
    assert alpha_note.read_at is None
    assert unread_count(world.dual.id, company_id=a) == 1
