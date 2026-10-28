"""Lifecycle pages through django.test.Client: onboarding tabs, checklist drawer, probation, exits, assets, home and help desk blocks."""
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core.clock import npt_today
from tests.support.sql import execute, one
from hajiri.employees import service as emp
from hajiri.lifecycle import api
from tests.people.conftest import build_world
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts


def plus(n):
    return (npt_today() + timedelta(days=n)).isoformat()


@pytest.fixture
def world(db):
    return build_world(join_days_ago=200)


def client_for(user):
    c = Client()
    sign_in(c, user)
    return c


def test_onboarding_tabs_render_for_hr_only(world):
    w = world
    h = client_for(w.hr)
    for tab in ('joiners', 'probation', 'templates', 'assets', 'settings'):
        r = h.get(f'/employees/onboarding?tab={tab}')
        assert r.status_code == 200, tab
    assert b'Standard onboarding' in h.get('/employees/onboarding?tab=templates').content
    for tab in ('exits', 'templates', 'questions'):
        assert h.get(f'/employees/exits?tab={tab}').status_code == 200
    assert b'What is the main reason you are leaving?' in h.get('/employees/exits?tab=questions').content
    assert client_for(w.sita).get('/employees/onboarding').status_code in (302, 403)


def test_feature_off_hides_lifecycle(world):
    w = world
    set_features_off(w.company, 'lifecycle')
    h = client_for(w.hr)
    assert h.get('/employees/onboarding').status_code == 302
    assert h.get('/_/lifecycle/my-checklist', **htmx()).status_code == 404


def test_start_onboarding_open_drawer_tick_tasks(world):
    w = world
    h = client_for(w.hr)
    new_id = emp.create_employee(w.hr, {'empCode': 'NEW1', 'nameEn': 'New Joiner', 'joinDate': plus(5), 'managerId': w.ram_id})
    r = h.get('/employees/onboarding?tab=joiners')
    assert b'New Joiner' in r.content and b'Onboarding now' in r.content
    case = api.cases(w.hr, kind='onboarding')[0]
    r = h.get(f"/_/lifecycle/cases/{case['id']}", **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'Onboarding: New Joiner' in r.content and b'Add a task' in r.content
    r = h.post('/_/lifecycle/cases/new', {'employee_id': new_id}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'onboarding is already in progress for this employee'
    t = api.case_detail(w.hr, case['id'])['tasks'][0]
    r = h.post(f"/_/lifecycle/tasks/{t['id']}", {'status': 'done'}, **htmx())
    assert r.status_code == 204 and f"hj:refresh-case-{case['id']}" in hx_triggers(r)
    r = h.post(f"/_/lifecycle/tasks/{t['id']}/comments", {'body': 'Done today'}, **htmx())
    assert b'Done today' in r.content
    r = h.post(f"/_/lifecycle/cases/{case['id']}/tasks", {'title': 'Order cards', 'assigneeType': 'manager'}, **htmx())
    assert toasts(r)[0]['message'] == 'Task added.'
    # The manager sees their tasks on the Home card; a stranger cannot tick them.
    r = client_for(w.ram).get('/_/lifecycle/my-checklist', **htmx())
    assert b'Checklist tasks for others' in r.content and b'Joining' in r.content
    mt = next(x for x in api.my_tasks(w.ram) if x['assignee_type'] == 'manager')
    assert client_for(w.peer).post(f"/_/lifecycle/tasks/{mt['id']}", {'status': 'done'}, **htmx()).status_code == 404


def test_probation_review_and_confirm_views(world):
    w = world
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(10), w.sita_id])
    h = client_for(w.hr)
    assert b'Sita Sharma' in h.get('/employees/onboarding?tab=probation').content
    m = client_for(w.ram)
    assert b'Send for approval' in m.get(f'/_/lifecycle/probation/{w.sita_id}/review', **htmx()).content
    r = m.post(f'/_/lifecycle/probation/{w.sita_id}/review', {'rating': '4', 'recommendation': 'confirm', 'comments': 'Reliable.'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Review sent to HR for approval.'
    r = h.post(f'/_/lifecycle/probation/{w.ram_id}/confirm', {'confirmedOn': npt_today().isoformat()}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Ram Bahadur is confirmed.'
    r = h.get(f'/_/lifecycle/employees/{w.sita_id}', **htmx())
    assert b'Review waiting for HR' in r.content


def test_exit_flow_views_assets_and_interview(world):
    w = world
    h = client_for(w.hr)
    r = h.post('/_/lifecycle/exits/new', {'employee_id': w.peer_id, 'kind': 'contract_end', 'lastWorkingDay': plus(-2)}, **htmx())
    assert r.status_code == 200 and r['HX-Retarget'] == '#hj-drawer-body' and b'Final settlement' in r.content
    assert toasts(r)[0]['message'] == 'Exit recorded and clearance started.'
    xid = str(one('SELECT id FROM employee_exit WHERE employee_id = %s', [w.peer_id])['id'])
    assert b'Peer Person' in h.get('/employees/exits').content
    r = h.post('/_/lifecycle/assets/new', {'tag': 'LT-1', 'name': 'Laptop', 'condition': 'good', 'cost': '1500'}, **htmx())
    assert toasts(r)[0]['message'] == 'Asset added.'
    aid = str(one("SELECT id FROM asset WHERE tag = 'LT-1'")['id'])
    r = h.post(f'/_/lifecycle/assets/{aid}/assign', {'employee_id': w.sita_id}, **htmx())
    assert toasts(r)[0]['message'] == 'Asset handed over.'
    assert b'With someone' in h.get('/employees/onboarding?tab=assets').content
    r = h.post(f'/_/lifecycle/assets/{aid}/return', {'condition': 'lost'}, **htmx())
    assert toasts(r)[0]['message'] == 'Return recorded.'
    assert one('SELECT status FROM asset WHERE id = %s', [aid])['status'] == 'retired'
    qs = api.questions(w.hr)
    data = {f"q-{q['id']}": ('4' if q['kind'] == 'rating' else q['options'][0] if q['kind'] == 'choice' else 'Fine') for q in qs}
    r = h.post(f'/_/lifecycle/exits/{xid}/interview', data, **htmx())
    assert toasts(r)[0]['message'] == 'Thank you. Your answers were saved.'
    r = h.get(f'/_/lifecycle/exits/{xid}', **htmx())
    assert b'Recorded by' in r.content
    r = h.post(f'/_/lifecycle/exits/{xid}/complete', {'skipOpenTasks': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Exit completed.'
    assert one('SELECT status FROM employee WHERE id = %s', [w.peer_id])['status'] == 'exited'


def test_leaving_block_resign_and_withdraw(world):
    w = world
    c = client_for(w.sita)
    r = c.get('/_/lifecycle/my-leaving', **htmx())
    assert r.status_code == 200 and b'Leaving the company' in r.content and b'Resign' in r.content
    assert b'Submit resignation' in c.get('/_/lifecycle/my-leaving?form=1', **htmx()).content
    r = c.post('/_/lifecycle/resign', {'proposedLastWorkingDay': plus(10), 'reasonCategory': 'Further studies'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Your resignation was sent to your manager.'
    r = c.get('/_/lifecycle/my-leaving', **htmx())
    assert b'Awaiting approval' in r.content and b'Withdraw resignation' in r.content
    xid = str(one('SELECT id FROM employee_exit WHERE employee_id = %s', [w.sita_id])['id'])
    r = c.post(f'/_/lifecycle/exits/{xid}/withdraw', **htmx())
    assert toasts(r)[0]['message'] == 'Resignation withdrawn.'
    r = c.post('/_/lifecycle/resign', {'proposedLastWorkingDay': plus(-1)}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'the last working day cannot be in the past'


def test_templates_and_questions_and_settings_save(world):
    w = world
    h = client_for(w.hr)
    h.get('/employees/onboarding?tab=templates')
    r = h.post('/_/lifecycle/templates/onboarding/new', {'name': 'Engineering', 't-0-title': 'Repo access', 't-0-assigneeType': 'hr',
                                                         't-0-dueOffsetDays': '1', 'active': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Checklist saved.'
    r = h.post('/_/lifecycle/questions', {'q-0-question': 'Anything else?', 'q-0-kind': 'text', 'q-0-active': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Questions saved.'
    assert [q['question'] for q in api.questions(w.sita)] == ['Anything else?']
    r = h.post('/_/lifecycle/settings', {'noticeDays': '15', 'probationReminderDays': '10, 3', 'autoStartOnboarding': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Settings saved.'
    assert api.get_settings(w.hr) == {'noticeDays': 15, 'autoStartOnboarding': True, 'probationReminderDays': [10, 3], 'selfExitInterview': False}
