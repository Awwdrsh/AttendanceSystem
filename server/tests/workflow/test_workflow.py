"""Port of apps/api/test/workflow.test.ts and workflow-qa.test.ts."""
import pytest

from hajiri.core import auth
from hajiri.core.db import execute, one, rows
from hajiri.workflow import service

from .helpers import api, file_leave, leave_type

pytestmark = pytest.mark.usefixtures('leave_handler')


@pytest.fixture
def world(demo, login_as):
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['manager'], demo['ram'])
    other = login_as(['manager'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return {'hr': hr, 'sita': sita, 'ram': ram, 'other': other, 'demo': demo}


# ---------------------------------------------------------------- workflow.test.ts

def test_two_step_chain_inbox_scoped_bulk_approve_timeline(world):
    d = world['demo']
    execute("""UPDATE workflow_definition SET steps = '[{"role":"manager"},{"role":"hr"}]'
                WHERE company_id = %s AND object_type = 'leave_request'""", [d['company_id']])
    file_leave(d['company_id'], d['sita'], '2026-11-02')
    file_leave(d['company_id'], d['sita'], '2026-11-03')
    assert api(world['other'].token).get('/api/v1/workflow/inbox').json()['items'] == []
    mgr = api(world['ram'].token).get('/api/v1/workflow/inbox').json()['items']
    assert len(mgr) == 2
    assert mgr[0]['step'] == 0 and mgr[0]['totalSteps'] == 2
    bulk = api(world['ram'].token).post('/api/v1/workflow/bulk-approve', {'ids': [i['instanceId'] for i in mgr]}).json()
    assert all(r['state'] == 'pending' for r in bulk['results'])
    assert api(world['ram'].token).get('/api/v1/workflow/inbox').json()['items'] == []
    hr_inbox = api(world['hr'].token).get('/api/v1/workflow/inbox').json()['items']
    assert len(hr_inbox) == 2
    assert hr_inbox[0]['step'] == 1
    final = api(world['hr'].token).post(f"/api/v1/workflow/{hr_inbox[0]['instanceId']}/approve", {}).json()
    assert final['state'] == 'approved'
    tl = api(world['hr'].token).get(f"/api/v1/workflow/{hr_inbox[0]['instanceId']}").json()
    assert len(tl['actions']) == 2
    assert tl['instance']['state'] == 'approved'
    assert one('SELECT status FROM leave_request WHERE id = %s', [hr_inbox[0]['objectId']]) == {'status': 'approved'}


def test_pending_older_than_finds_stale_approvals_with_approvers(world):
    d = world['demo']
    file_leave(d['company_id'], d['sita'], '2026-11-04')
    execute("UPDATE workflow_instance SET created_at = now() - interval '30 hours' WHERE state = 'pending'")
    stale = service.pending_older_than(24)
    assert len(stale) == 1
    assert len(stale[0]['approvers']) > 0


def test_summary_signature_and_audit(world):
    d = world['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-05', reason='family')
    item = api(world['ram'].token).get('/api/v1/workflow/inbox').json()['items'][0]
    assert item['summary']['leaveType'] == 'LWP' and item['summary']['days'] == 1 and item['summary']['reason'] == 'family'
    assert item['summary']['from'] == '2026-11-05' and item['summary']['balanceAfter'] == -1
    assert item['employee']['empCode'] == 'E001'
    # the first approver was notified
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'leave_request.pending' AND user_id = %s",
               [world['ram'].user_id])['n'] >= 1
    assert api(world['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).json()['state'] == 'approved'
    a = one('SELECT * FROM approval_action WHERE instance_id = %s', [f['instanceId']])
    assert a['signature'] == service.signature_of(f['instanceId'], 0, world['ram'].user_id, 'approve', a['acted_at'])
    assert one("SELECT action FROM audit_log WHERE table_name = 'leave_request' AND row_id = %s", [f['requestId']])['action'] == 'approve'


def test_reject_needs_a_reason_and_decided_twice_conflicts(world):
    d = world['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-06')
    r = api(world['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/reject", {})
    assert r.status_code == 400 and r.json()['error'] == 'a reason is required to reject'
    assert api(world['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/reject", {'reason': 'busy week'}).json()['state'] == 'rejected'
    assert one('SELECT status FROM leave_request WHERE id = %s', [f['requestId']]) == {'status': 'rejected'}
    again = api(world['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {})
    assert again.status_code == 409 and again.json()['error'] == 'request already rejected'


def test_cancel_workflow_and_sla_hook(world):
    d = world['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-07')
    from hajiri.worker import registry
    fn = registry.hook('workflow.approvers_for_step')
    inst = service.get_instance(f['instanceId'])
    assert fn(d['company_id'], d['sita'], inst['steps'][0]) == [world['ram'].user_id]
    service.cancel_workflow('leave_request', f['requestId'])
    assert service.get_instance(f['instanceId'])['state'] == 'cancelled'
    assert service.inbox_for(world['ram'].user) == []


# ---------------------------------------------------------------- workflow-qa.test.ts

@pytest.fixture
def qa(world, login_as, demo):
    admin = login_as(['admin'], None)
    mgr_emp = one("""INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E900', 'Other Manager') RETURNING id""",
                  [demo['company_id']])['id']
    execute("""INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')""",
            [mgr_emp])
    other_mgr = login_as(['manager'], str(mgr_emp))
    c2 = one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id']
    foreign_emp = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Foreign') RETURNING id", [c2])['id']
    foreign = login_as(['hr'], company_id=str(c2))
    return {**world, 'admin': admin, 'other_mgr': other_mgr, 'foreign': foreign, 'foreign_emp': str(foreign_emp)}


def test_manager_not_line_manager_cannot_approve_see_or_be_shown(qa):
    d = qa['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-10', reason='qa')
    o = api(qa['other_mgr'].token)
    assert f['instanceId'] not in [i['instanceId'] for i in o.get('/api/v1/workflow/inbox').json()['items']]
    assert o.post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).status_code == 403
    assert o.post(f"/api/v1/workflow/{f['instanceId']}/reject", {'reason': 'no'}).status_code == 403
    assert o.get(f"/api/v1/workflow/{f['instanceId']}").status_code == 404
    assert api(qa['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).json()['state'] == 'approved'


def test_another_company_cannot_see_the_instance(qa):
    d = qa['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-11', reason='qa')
    fx = api(qa['foreign'].token)
    assert fx.get(f"/api/v1/workflow/{f['instanceId']}").status_code == 404
    assert fx.post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).status_code == 404
    bulk = fx.post('/api/v1/workflow/bulk-approve', {'ids': [f['instanceId']]}).json()
    assert 'not found' in bulk['results'][0]['error']
    assert one('SELECT state FROM workflow_instance WHERE id = %s', [f['instanceId']]) == {'state': 'pending'}


def test_admin_cannot_approve_own_request(qa):
    d = qa['demo']
    admin_emp = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E904', 'Admin Person') RETURNING id",
                    [d['company_id']])['id']
    execute("""INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')""",
            [admin_emp])
    execute('UPDATE app_user SET employee_id = %s WHERE id = %s', [admin_emp, qa['admin'].user_id])
    f = file_leave(d['company_id'], str(admin_emp), '2026-11-12')
    assert api(qa['admin'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).status_code == 403
    execute('UPDATE app_user SET employee_id = NULL WHERE id = %s', [qa['admin'].user_id])
    # without the employee link the admin may decide it
    assert api(qa['admin'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).json()['state'] == 'approved'


def test_assert_manages_for_filing_on_behalf(qa):
    d = qa['demo']
    with pytest.raises(Exception) as e:
        service.assert_manages(qa['other_mgr'].user, d['sita'])
    assert str(getattr(e.value, 'message', e.value)) == 'not allowed to act for this employee'
    service.assert_manages(auth.resolve_session(qa['ram'].token), d['sita'])


def test_acting_on_behalf_is_hr_only_and_names_a_real_approver(qa):
    d = qa['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-16', reason='qa')
    ram_user = str(one('SELECT id FROM app_user WHERE employee_id = %s', [d['ram']])['id'])
    assert api(qa['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {'onBehalfOf': ram_user}).status_code == 403
    assert api(qa['hr'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {'onBehalfOf': qa['admin'].user_id}).status_code == 400
    ok = api(qa['hr'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {'onBehalfOf': ram_user})
    assert ok.json()['state'] == 'approved'
    assert str(one('SELECT on_behalf_of FROM approval_action WHERE instance_id = %s', [f['instanceId']])['on_behalf_of']) == ram_user


def test_definition_change_does_not_alter_in_flight_instances(qa):
    d = qa['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-17', reason='qa')
    execute("""UPDATE workflow_definition SET steps = '[{"role":"manager"},{"role":"hr"},{"role":"admin"}]'
                WHERE company_id = %s AND object_type = 'leave_request'""", [d['company_id']])
    assert api(qa['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {}).json()['state'] == 'approved'


def test_view_permissions(qa):
    d = qa['demo']
    f = file_leave(d['company_id'], d['sita'], '2026-11-18')
    inst = service.get_instance(f['instanceId'])
    assert service.can_view(qa['sita'].user, inst)
    assert service.can_view(qa['ram'].user, inst)
    assert service.can_view(qa['hr'].user, inst)
    assert not service.can_view(qa['other_mgr'].user, inst)
    assert not service.can_view(qa['foreign'].user, inst)
    assert not service.can_act(qa['sita'].user, inst)
    assert rows('SELECT 1 FROM leave_type WHERE id = %s', [leave_type(d['company_id'], 'LWP')])
