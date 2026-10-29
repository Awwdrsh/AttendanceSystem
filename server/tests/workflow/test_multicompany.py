"""
Approvals in a group of companies (tests/multicompany world: Alpha and Beta in one group, Gamma outside):
approvals, flows, groups and delegations of one company are not found from another (404, never 403), approvers come
from the people who can act in the request's company (members and group-role holders included), and the employee a
member acts as is the one of the active company.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.errors import BadRequestError, NotFoundError
from hajiri.db.models import ApprovalFlow, ApproverDelegate, LeaveRequest, LeaveType, UserGroup, WorkflowDefinition
from hajiri.workflow import flow_admin, service
from hajiri.workflow.flows import employee_users, resolve_step
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import PW, make_employee

pytestmark = pytest.mark.django_db


@pytest.fixture
def letters():
    had = service.handler_for('letter_request')
    service.register_handler('letter_request', lambda *a: None)
    yield
    if had is None:
        service.unregister_handler('letter_request')
    else:
        service.register_handler('letter_request', had)


def _start(company, employee, steps=None, object_type='letter_request'):
    if steps is not None:
        WorkflowDefinition.objects.update_or_create(company_id=company.id, object_type=object_type, defaults={'steps': steps})
    return service.start_workflow(company_id=str(company.id), object_type=object_type, object_id=str(uuid.uuid4()),
                                  employee_id=str(employee.id), title='Letter request', body='A letter is waiting')


def _bearer(world, person, company=None):
    token, _user = world.session(person, company)
    c = Client()
    h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    class _C:
        def get(self, path):
            return c.get(path, **h)

        def post(self, path, body=None):
            return c.post(path, json.dumps(body or {}), content_type='application/json', **h)
    return _C()


@pytest.fixture
def beta_request(world, letters):
    """A Beta employee reporting to Dipak's Beta record (Dipak is a member of Beta, HR at home in Alpha)."""
    worker = make_employee(world.beta, name='Bina Worker', manager=world.dual_beta_employee)
    inst = _start(world.beta, worker, [{'kind': 'manager', 'role': 'manager'}])
    return {'worker': worker, 'inst': inst}


# ---------------------------------------------------------------- approvers across the group

def test_a_member_approves_as_their_employee_record_in_that_company(world, beta_request):
    inst = beta_request['inst']
    _t, dual_beta = world.session(world.dual, world.beta)
    _t, dual_alpha = world.session(world.dual)
    assert dual_alpha.company_id == str(world.alpha.id)
    # the line manager is Dipak's Beta record: the membership account decides it, in Beta only
    assert [i['instanceId'] for i in service.inbox_for(dual_beta)] == [inst['id']]
    assert service.inbox_for(dual_alpha) == []
    assert str(world.dual.id) in [str(u) for u in employee_users([world.dual_beta_employee.id]).values_list('id', flat=True)]
    assert service.decide(dual_beta, inst['id'], 'approve') == 'approved'


def test_group_hr_is_an_hr_approver_in_every_company_of_the_group(world, letters):
    worker = make_employee(world.alpha, name='Arun Worker')
    inst = _start(world.alpha, worker, [{'kind': 'role', 'role': 'hr'}])
    _t, ghr_alpha = world.session(world.ghr, world.alpha)
    assert inst['id'] in [i['instanceId'] for i in service.inbox_for(ghr_alpha)]
    res = resolve_step(str(world.alpha.id), str(worker.id), {'kind': 'role', 'role': 'hr'})
    assert str(world.ghr.id) in res.users and str(world.admin_c.id) not in res.users
    # working in Beta, the same person does not see Alpha's request
    _t, ghr_beta = world.session(world.ghr, world.beta)
    assert inst['id'] not in [i['instanceId'] for i in service.inbox_for(ghr_beta)]


def test_named_approver_and_group_members_must_work_in_the_company(world):
    _t, admin_a = world.session(world.admin_a)
    with pytest.raises(BadRequestError, match='not a user of your company'):
        flow_admin.set_default(admin_a, 'letter_request', {'steps': [{'kind': 'user', 'userId': str(world.admin_c.id)}]})
    # people of Alpha count, including group-role holders whose home is Beta
    flow_admin.set_default(admin_a, 'letter_request', {'steps': [{'kind': 'user', 'userId': str(world.ghr.id)}]})
    with pytest.raises(BadRequestError, match='one of the members is not a user of your company'):
        flow_admin.create_group(admin_a, {'name': 'Mixed', 'userIds': [str(world.admin_c.id)]})
    gid = flow_admin.create_group(admin_a, {'name': 'Approvers', 'userIds': [str(world.solo.id), str(world.ghr.id)]})['id']
    assert UserGroup.objects.for_company(world.alpha.id).filter(pk=gid).exists()


def test_delegations_apply_in_the_company_they_were_set_in(world, letters):
    boss = world.dual_beta_employee
    worker = make_employee(world.beta, name='Bina Worker', manager=boss)
    today = date.today()
    # Dipak delegates his ALPHA approvals to Sita: his Beta approvals still come to him
    ApproverDelegate.objects.create(company_id=world.alpha.id, user_id=world.dual.id, delegate_id=world.solo.id,
                                    from_date=today - timedelta(days=1), to_date=today + timedelta(days=5))
    res = resolve_step(str(world.beta.id), str(worker.id), {'kind': 'manager'})
    assert res.users == [str(world.dual.id)]


# ---------------------------------------------------------------- another company's ids are not found

def test_beta_admin_reads_and_decides_the_beta_approval(world, beta_request):
    inst = beta_request['inst']
    api = _bearer(world, world.admin_b)
    assert api.get(f"/api/v1/workflow/{inst['id']}").status_code == 200
    assert api.post(f"/api/v1/workflow/{inst['id']}/approve").json()['state'] == 'approved'


def test_a_flow_condition_or_delegate_from_another_company_is_refused(world):
    _t, admin_a = world.session(world.admin_a)
    beta_emp = make_employee(world.beta, name='Beta Person')
    with pytest.raises(BadRequestError, match='one of the employees is not in your company'):
        flow_admin.create_flow(admin_a, {'objectType': 'letter_request', 'name': 'x', 'steps': [{'kind': 'manager'}],
                                         'conditions': {'employeeIds': [str(beta_emp.id)]}})
    with pytest.raises(BadRequestError, match='active user of your company'):
        flow_admin.add_delegate(admin_a, {'delegateId': str(world.admin_c.id), 'from': date.today().isoformat(),
                                          'to': (date.today() + timedelta(days=2)).isoformat()})
    with pytest.raises(NotFoundError, match='employee not found'):
        flow_admin.preview(admin_a, {'objectType': 'leave_request', 'employeeId': str(beta_emp.id)})
    _t, admin_b = world.session(world.admin_b)
    did = flow_admin.add_delegate(admin_b, {'delegateId': str(world.dual.id), 'from': date.today().isoformat(),
                                            'to': (date.today() + timedelta(days=2)).isoformat()})['id']
    c = world.signed_in(world.admin_a)
    assert c.delete(f'/api/v1/workflow/delegates/{did}').status_code == 404
    with pytest.raises(NotFoundError):
        flow_admin.cancel_delegate(admin_a, did)
    assert ApproverDelegate.objects.get(pk=did).cancelled_at is None


def test_preview_and_chain_in_the_active_company(world):
    """Dipak in Beta previews his own chain as his Beta record; the employee picker of Alpha cannot name a Beta record."""
    _t, dual_beta = world.session(world.dual, world.beta)
    assert dual_beta.employee_id == str(world.dual_beta_employee.id)
    WorkflowDefinition.objects.update_or_create(company_id=world.beta.id, object_type='letter_request',
                                                defaults={'steps': [{'kind': 'manager', 'role': 'manager'}]})
    out = flow_admin.preview(dual_beta, {'objectType': 'letter_request'})
    assert out['steps'][0]['label'] == 'Line manager' and out['steps'][0]['notes'] == ['No line manager is set', 'HR decides instead']
    _t, dual_alpha = world.session(world.dual)
    with pytest.raises(NotFoundError, match='employee not found'):
        flow_admin.preview(dual_alpha, {'objectType': 'letter_request', 'employeeId': str(world.dual_beta_employee.id)})


def test_switching_company_changes_the_inbox_badge(world, beta_request):
    token = auth.login(world.dual.email, PW).token
    user = auth.resolve_session(token)
    assert service.inbox_count(user) == 0
    user = auth.switch_company(user, token, str(world.beta.id))
    assert service.inbox_count(user) == 1


@pytest.mark.parametrize('who', ['outsider', 'group_admin_in_alpha'])
def test_beta_approval_is_not_found_from_elsewhere(world, beta_request, who):
    inst = beta_request['inst']
    person = world.admin_c if who == 'outsider' else world.admin_a
    api = _bearer(world, person)
    assert api.get(f"/api/v1/workflow/{inst['id']}").status_code == 404
    assert api.post(f"/api/v1/workflow/{inst['id']}/approve").status_code == 404
    assert api.post(f"/api/v1/workflow/{inst['id']}/reject", {'reason': 'no'}).status_code == 404
    assert api.post('/api/v1/workflow/bulk-approve', {'ids': [inst['id']]}).json()['results'] == [
        {'id': inst['id'], 'error': 'approval not found'}]
    assert inst['id'] not in [i['instanceId'] for i in api.get('/api/v1/workflow/inbox').json()['items']]
    c = world.signed_in(person)          # the approvals page's cookie session
    assert c.get(f"/api/v1/workflow/{inst['id']}").status_code == 404
    assert c.post(f"/api/v1/workflow/{inst['id']}/reject", {'reason': 'no'}, content_type='application/json').status_code == 404
    assert c.post(f"/api/v1/workflow/{inst['id']}/approve", {}, content_type='application/json').status_code == 404
    assert service.get_instance(inst['id'])['state'] == 'pending'


def test_group_admin_decides_after_switching_to_beta(world, beta_request):
    inst = beta_request['inst']
    c = world.signed_in(world.admin_a, world.beta)
    r = c.post(f"/api/v1/workflow/{inst['id']}/approve", {}, content_type='application/json')
    assert r.status_code == 200, r.content
    assert service.get_instance(inst['id'])['state'] != 'pending'


@pytest.mark.parametrize('who', ['outsider', 'group_admin_in_alpha'])
def test_beta_flow_set_up_is_not_found_from_elsewhere(world, who):
    _t, admin_b = world.session(world.admin_b)
    flow_id = flow_admin.create_flow(admin_b, {'objectType': 'letter_request', 'name': 'Beta letters',
                                               'steps': [{'kind': 'role', 'role': 'hr'}]})['id']
    group_id = flow_admin.create_group(admin_b, {'name': 'Beta approvers', 'userIds': [str(world.admin_b.id)]})['id']
    person = world.admin_c if who == 'outsider' else world.admin_a
    c = world.signed_in(person)
    W = '/api/v1/workflow'
    assert c.patch(f'{W}/flows/{flow_id}', {'name': 'x'}, content_type='application/json').status_code == 404
    assert c.patch(f'{W}/flows/{flow_id}', {'active': False}, content_type='application/json').status_code == 404
    assert c.delete(f'{W}/flows/{flow_id}').status_code == 404
    assert c.patch(f'{W}/groups/{group_id}', {'name': 'x'}, content_type='application/json').status_code == 404
    assert c.delete(f'{W}/groups/{group_id}').status_code == 404
    listed = c.get(f'{W}/flows/config').content.decode()
    assert 'Beta letters' not in listed and 'Beta approvers' not in listed
    assert ApprovalFlow.objects.filter(pk=flow_id, active=True).exists()
    assert UserGroup.objects.filter(pk=group_id).exists()
    # Beta's admin still has them
    cb = world.signed_in(world.admin_b)
    listed = cb.get(f'{W}/flows/config').content.decode()
    assert 'Beta letters' in listed and 'Beta approvers' in listed


def test_request_history_lists_only_the_active_company(world):
    lt_a = LeaveType.objects.create(company=world.alpha, code='CL', name_en='Casual leave')
    lt_b = LeaveType.objects.create(company=world.beta, code='CL', name_en='Casual leave')
    ea = make_employee(world.alpha, name='Alpha Leaver')
    eb = make_employee(world.beta, name='Beta Leaver')
    for e, lt in ((ea, lt_a), (eb, lt_b)):
        LeaveRequest.objects.create(employee=e, leave_type=lt, from_date=date(2026, 12, 1), to_date=date(2026, 12, 1),
                                    days_requested=1)
    c = world.signed_in(world.admin_a)
    html = c.get('/api/v1/leave/requests', {'scope': 'all'}).content.decode()
    assert 'Alpha Leaver' in html and 'Beta Leaver' not in html
    html = world.signed_in(world.admin_a, world.beta).get('/api/v1/leave/requests', {'scope': 'all'}).content.decode()
    assert 'Beta Leaver' in html and 'Alpha Leaver' not in html

