"""
Lifecycle pages checked against the TS web app after the port: the checklist editor's move up / move down and its
"N days before joining" line (TemplatesPanel.tsx), and the record-exit confirmation that names the last working day
(ExitsPage.tsx).
"""
import pytest
from django.test import Client

from hajiri.core.db import one, rows
from tests.people.conftest import build_world
from tests.web.factories import htmx, sign_in, toasts


@pytest.fixture
def world(db):
    return build_world(join_days_ago=200)


def client_for(user):
    c = Client()
    sign_in(c, user)
    return c


def test_checklist_editor_keeps_the_order_the_tasks_were_moved_to(world):
    w = world
    h = client_for(w.hr)
    r = h.get('/_/lifecycle/templates/onboarding/new', **htmx('hj-drawer-body'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Move up' in body and 'Move down' in body
    assert 'On the first day' in body and 'before' in body and 'joining' in body
    # Task 1 was moved below task 2: the posted places win over the slot order; removed (disabled) slots post nothing.
    r = h.post('/_/lifecycle/templates/onboarding/new', {
        'name': 'Ordered', 'active': 'on',
        't-0-title': 'Second', 't-0-assigneeType': 'hr', 't-0-dueOffsetDays': '2', 't-0-sort': '1',
        't-1-title': 'First', 't-1-assigneeType': 'manager', 't-1-dueOffsetDays': '-1', 't-1-sort': '0',
    }, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Checklist saved.'
    t = one("SELECT id FROM lifecycle_template WHERE company_id = %s AND name = 'Ordered'", [w.company.id])
    tasks = rows('SELECT title, sort, due_offset_days FROM lifecycle_template_task WHERE template_id = %s ORDER BY sort', [t['id']])
    assert [(x['title'], x['sort'], x['due_offset_days']) for x in tasks] == [('First', 0, -1), ('Second', 1, 2)]
    # Without places (an older form), the slot order is kept.
    h.post(f"/_/lifecycle/templates/onboarding/{t['id']}", {'name': 'Ordered', 'active': 'on', 't-0-title': 'A', 't-0-assigneeType': 'hr',
                                                            't-1-title': 'B', 't-1-assigneeType': 'hr'}, **htmx())
    assert [x['title'] for x in rows('SELECT title FROM lifecycle_template_task WHERE template_id = %s ORDER BY sort', [t['id']])] == ['A', 'B']
    r = h.get('/_/lifecycle/templates/offboarding/new', **htmx('hj-drawer-body'))
    assert 'On the last day' in r.content.decode() and 'the last day' in r.content.decode()


def test_record_exit_confirmation_names_the_last_working_day(world):
    r = client_for(world.hr).get('/_/lifecycle/exits/new', **htmx('hj-modal-body'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Record this exit?' in body
    assert ":hx-confirm=" in body and 'Hajiri.bs.fmt(lwd)' in body
