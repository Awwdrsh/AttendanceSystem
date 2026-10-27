"""Port of the 'approval flows' block of apps/api/test/roles-flows.test.ts (and the executive flows/config check)."""
import pytest
from django.test import Client

from hajiri.core.db import execute, one
from hajiri.core.errors import BadRequestError, ForbiddenError, ValidationError
from hajiri.workflow import flow_admin, service
from hajiri.workflow.flows import match_score, steps_for_amount, Subject
from tests.web.factories import htmx

from .helpers import api, emp, file_leave, leave_type, new_object_id, unit


@pytest.fixture
def flows_world(demo, login_as):
    service.register_handler('letter_request', lambda *a: None)
    c = demo['company_id']
    dept_head = emp(c, branch=demo['branch_id'])
    unit_id = unit(c, 'FIN', None, dept_head)
    mgr = emp(c, org_unit=unit_id, manager=dept_head, branch=demo['branch_id'])
    worker = emp(c, org_unit=unit_id, manager=mgr, branch=demo['branch_id'])
    w = {
        'c': c, 'demo': demo, 'dept_head': dept_head, 'unit_id': unit_id, 'mgr': mgr, 'worker': worker,
        'hr': login_as(['hr']), 'admin': login_as(['admin']),
        'mgr_l': login_as(['manager'], mgr), 'head_l': login_as(['employee'], dept_head),
        'worker_l': login_as(['employee'], worker), 'other': login_as(['employee'], emp(c, branch=demo['branch_id'])),
    }
    yield w
    service.unregister_handler('letter_request')


def _inbox(login):
    return [i['instanceId'] for i in service.inbox_for(login.user)]


def _start(w, object_type, employee, **kw):
    return service.start_workflow(company_id=w['c'], object_type=object_type, object_id=new_object_id(), employee_id=employee,
                                  title='t', body='b', **kw)


def test_default_chain_line_manager_then_department_head(flows_world):
    w = flows_world
    assert flow_admin.set_default(w['hr'].user, 'letter_request', {'steps': [{'kind': 'manager'}, {'kind': 'department_head'}]})
    inst = _start(w, 'letter_request', w['worker'])
    assert [s['kind'] for s in inst['steps']] == ['manager', 'department_head']
    assert inst['id'] in _inbox(w['mgr_l'])
    # the department head sees it only at step 2 (resolve_session gives heads the manager role)
    head_user = w['head_l'].user
    assert 'manager' in head_user.roles
    assert inst['id'] not in _inbox(w['head_l'])
    assert api(w['mgr_l'].token).post(f"/api/v1/workflow/{inst['id']}/approve", {}).json()['state'] == 'pending'
    assert inst['id'] in _inbox(w['head_l'])
    # the requester can never decide their own request
    assert api(w['worker_l'].token).post(f"/api/v1/workflow/{inst['id']}/approve", {}).status_code == 403
    assert api(w['head_l'].token).post(f"/api/v1/workflow/{inst['id']}/approve", {}).json()['state'] == 'approved'


def test_most_specific_conditional_flow_wins_and_groups_approve(flows_world):
    w = flows_world
    hr = w['hr'].user
    g = flow_admin.create_group(hr, {'name': 'Finance approvers', 'userIds': [w['other'].user_id]})
    flow_admin.create_flow(hr, {'objectType': 'letter_request', 'name': 'Finance department', 'conditions': {'orgUnitIds': [w['unit_id']]},
                                'steps': [{'kind': 'group', 'groupId': g['id']}]})
    flow_admin.create_flow(hr, {'objectType': 'letter_request', 'name': 'Just this person', 'conditions': {'employeeIds': [w['mgr']]},
                                'steps': [{'kind': 'role', 'role': 'hr'}]})
    p1 = api(w['worker_l'].token).get('/api/v1/workflow/preview', {'objectType': 'letter_request'})
    assert p1.status_code == 200
    assert p1.json()['flow']['name'] == 'Finance department'
    assert [a['id'] for a in p1.json()['steps'][0]['approvers']] == [w['other'].user_id]
    p2 = flow_admin.preview(w['mgr_l'].user, {'objectType': 'letter_request'})
    assert p2['flow']['name'] == 'Just this person'
    inst = _start(w, 'letter_request', w['worker'])
    assert inst['id'] in _inbox(w['other'])
    assert inst['id'] not in _inbox(w['mgr_l'])
    assert api(w['other'].token).post(f"/api/v1/workflow/{inst['id']}/approve", {}).json()['state'] == 'approved'


def test_amount_thresholds_add_a_step_only_for_larger_requests(flows_world):
    w = flows_world
    flow_admin.set_default(w['hr'].user, 'expense_claim', {'steps': [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000}]})
    small = flow_admin.preview(w['worker_l'].user, {'objectType': 'expense_claim', 'amount': '5000'})
    big = flow_admin.preview(w['worker_l'].user, {'objectType': 'expense_claim', 'amount': '25000'})
    assert len(small['steps']) == 1
    assert len(big['steps']) == 2
    inst = _start(w, 'expense_claim', w['worker'], amount=4000)
    assert len(inst['steps']) == 1
    assert len(_start(w, 'expense_claim', w['worker'], amount=10000)['steps']) == 2


def test_approver_on_leave_hands_over_to_delegate_or_hr(flows_world):
    w = flows_world
    flow_admin.set_default(w['hr'].user, 'training_nomination', {'steps': [{'kind': 'manager'}]})
    today = service.npt_today()
    execute("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status)
               VALUES (%s, %s, %s, %s, 1, 'approved')""", [w['mgr'], leave_type(w['c'], 'HOME'), today, today])
    inst = _start(w, 'training_nomination', w['worker'])
    routing = one('SELECT routing FROM workflow_instance WHERE id = %s', [inst['id']])['routing']
    assert 'on leave' in ' '.join(routing[0]['notes'])
    assert inst['id'] not in _inbox(w['mgr_l'])
    assert inst['id'] in _inbox(w['hr'])
    # the manager names a delegate: the delegate now decides
    mgr_user = w['mgr_l'].user_id
    d = flow_admin.add_delegate(w['mgr_l'].user, {'delegateId': w['other'].user_id, 'from': today, 'to': today})
    assert inst['id'] in _inbox(w['other'])
    # someone else cannot set or cancel the manager's delegation
    with pytest.raises(ForbiddenError):
        flow_admin.add_delegate(w['worker_l'].user, {'userId': mgr_user, 'delegateId': w['other'].user_id, 'from': today, 'to': today})
    r = Client()
    r.cookies['hajiri_session'] = w['worker_l'].token
    resp = r.post(f"/_/workflow/delegates/{d['id']}/cancel", **htmx())
    assert resp.status_code == 404
    # the delegation is listed for the manager and the delegate, and the manager may cancel it
    assert [x['id'] for x in flow_admin.list_delegates(w['other'].user)] == [d['id']]
    assert flow_admin.cancel_delegate(w['mgr_l'].user, d['id']) == {'ok': True}
    assert inst['id'] not in _inbox(w['other'])


def test_delegation_rules(flows_world):
    w = flows_world
    today = service.npt_today()
    u = w['mgr_l'].user
    with pytest.raises(BadRequestError, match='the end date is before the start date'):
        flow_admin.add_delegate(u, {'delegateId': w['other'].user_id, 'from': '2099-01-02', 'to': '2099-01-01'})
    with pytest.raises(BadRequestError, match='the delegation has already ended'):
        flow_admin.add_delegate(u, {'delegateId': w['other'].user_id, 'from': '2020-01-01', 'to': '2020-01-02'})
    with pytest.raises(BadRequestError, match='choose someone else as the delegate'):
        flow_admin.add_delegate(u, {'delegateId': u.id, 'from': today, 'to': today})
    with pytest.raises(BadRequestError, match='the delegate must be an active user of your company'):
        flow_admin.add_delegate(u, {'delegateId': '00000000-0000-7000-8000-00000000dead', 'from': today, 'to': today})
    # HR may set one for someone else
    assert flow_admin.add_delegate(w['hr'].user, {'userId': u.id, 'delegateId': w['other'].user_id, 'from': today, 'to': today})['id']
    assert u.id not in [c['id'] for c in flow_admin.delegate_candidates(u)]


def test_verify_lists_employees_whose_chain_reaches_nobody(flows_world):
    w = flows_world
    orphan = emp(w['c'], branch=w['demo']['branch_id'])
    v = flow_admin.verify(w['hr'].user, {'objectType': 'leave_request'})
    mine = [p for p in v['problems'] if p['employeeId'] == orphan]
    assert len(mine) == 1
    assert 'line manager' in mine[0]['suggestion']
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [w['mgr'], orphan])
    v2 = flow_admin.verify(w['hr'].user, {'objectType': 'leave_request'})
    assert [p for p in v2['problems'] if p['employeeId'] == orphan] == []


def test_leave_request_follows_configured_chain(flows_world, leave_handler):
    w = flows_world
    flow_admin.set_default(w['hr'].user, 'leave_request', {'steps': [{'kind': 'department_head'}]})
    f = file_leave(w['c'], w['worker'], '2026-12-07', code='HOME', reason='errand')
    items = service.inbox_for(w['head_l'].user)
    it = next((i for i in items if i['objectId'] == f['requestId']), None)
    assert it is not None
    assert not [i for i in service.inbox_for(w['mgr_l'].user) if i['objectId'] == f['requestId']]
    assert api(w['head_l'].token).post(f"/api/v1/workflow/{it['instanceId']}/approve", {}).json()['state'] == 'approved'


def test_nobody_outside_hr_edits_flows_and_refs_stay_in_company(flows_world, login_as):
    w = flows_world
    m = w['mgr_l'].user
    with pytest.raises(ForbiddenError):
        flow_admin.flows_config(m)
    with pytest.raises(ForbiddenError):
        flow_admin.set_default(m, 'leave_request', {'steps': [{'kind': 'manager'}]})
    with pytest.raises(ForbiddenError):
        flow_admin.create_group(m, {'name': 'x'})
    # through the pages too, and the executive cannot read the flows
    c = Client()
    c.cookies['hajiri_session'] = login_as(['executive']).token
    assert c.get('/settings/approvals').status_code in (302, 403)
    assert c.post('/_/workflow/groups/new', {'name': 'x'}, **htmx()).status_code == 403
    hr = w['hr'].user
    foreign = '00000000-0000-7000-8000-00000000dead'
    with pytest.raises(BadRequestError, match='the approver chosen is not a user of your company'):
        flow_admin.set_default(hr, 'leave_request', {'steps': [{'kind': 'user', 'userId': foreign}]})
    with pytest.raises(BadRequestError, match='one of the branches is not in your company'):
        flow_admin.create_flow(hr, {'objectType': 'leave_request', 'name': 'x', 'conditions': {'branchIds': [foreign]}, 'steps': [{'kind': 'manager'}]})
    with pytest.raises(ValidationError):
        flow_admin.set_default(hr, 'not_a_type', {'steps': [{'kind': 'manager'}]})
    with pytest.raises(ValidationError) as e:
        flow_admin.set_default(hr, 'leave_request', {'steps': [{'kind': 'role'}]})
    assert e.value.issues[0]['message'] == 'choose the role that approves'
    with pytest.raises(ValidationError):
        flow_admin.set_default(hr, 'leave_request', {'steps': [{'kind': 'role', 'role': 'employee'}]})
    with pytest.raises(ValidationError) as e:
        flow_admin.set_default(hr, 'leave_request', {'steps': []})
    assert e.value.issues[0]['message'] == 'a flow needs at least one step'
    with pytest.raises(ValidationError) as e:
        flow_admin.set_default(hr, 'leave_request', {'steps': [{'kind': 'manager'}] * 7})
    assert e.value.issues[0]['message'] == 'at most 6 steps'


def test_preview_for_someone_else_needs_to_manage_them(flows_world):
    w = flows_world
    r = api(w['worker_l'].token).get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['mgr']})
    assert r.status_code == 403
    assert api(w['mgr_l'].token).get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['worker']}).status_code == 200


def test_group_in_use_cannot_be_deleted(flows_world):
    w = flows_world
    hr = w['hr'].user
    g = flow_admin.create_group(hr, {'name': 'Finance approvers', 'userIds': [w['other'].user_id]})
    flow_admin.create_flow(hr, {'objectType': 'letter_request', 'name': 'Finance department', 'conditions': {'orgUnitIds': [w['unit_id']]},
                                'steps': [{'kind': 'group', 'groupId': g['id']}]})
    cfg = flow_admin.flows_config(hr)
    gid = next(x for x in cfg['groups'] if x['name'] == 'Finance approvers')['id']
    with pytest.raises(BadRequestError, match='this group is used in an approval flow'):
        flow_admin.delete_group(hr, gid)
    # unused groups go
    g2 = flow_admin.create_group(hr, {'name': 'Spare'})
    assert flow_admin.delete_group(hr, g2['id']) == {'ok': True}


def test_flow_update_toggle_and_delete(flows_world):
    w = flows_world
    hr = w['hr'].user
    f = flow_admin.create_flow(hr, {'objectType': 'ot_request', 'name': 'Everyone', 'steps': [{'kind': 'manager2'}]})
    flow_admin.update_flow(hr, f['id'], {'active': False})
    assert service.npt_today()
    assert one('SELECT active, name FROM approval_flow WHERE id = %s', [f['id']]) == {'active': False, 'name': 'Everyone'}
    flow_admin.update_flow(hr, f['id'], {'name': 'Renamed', 'steps': [{'kind': 'branch_head', 'label': 'Site boss'}]})
    row = one('SELECT name, steps FROM approval_flow WHERE id = %s', [f['id']])
    assert row['name'] == 'Renamed' and row['steps'] == [{'kind': 'branch_head', 'role': 'branch_head', 'label': 'Site boss'}]
    flow_admin.delete_flow(hr, f['id'])
    from hajiri.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        flow_admin.delete_flow(hr, f['id'])


def test_match_score_and_steps_for_amount():
    s = Subject(group_ids=['g'], designation_id='d', org_unit_ids=['u'], branch_id='b')
    assert match_score({}, 'e', s) == 0
    assert match_score({'employeeIds': ['e'], 'branchIds': ['b']}, 'e', s) == 17
    assert match_score({'groupIds': ['x']}, 'e', s) == -1
    assert match_score({'orgUnitIds': ['u'], 'designationIds': ['d']}, 'e', s) == 6
    steps = [{'kind': 'manager', 'minAmount': 100}]
    assert steps_for_amount(steps, None) == steps  # nothing left: the first step stays
    assert steps_for_amount([{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 100}], 100) == \
        [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 100}]
