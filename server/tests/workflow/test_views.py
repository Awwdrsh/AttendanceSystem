"""Pages and HTMX fragments: Approvals inbox, request history, approval flows set-up, delegation and the chain preview."""
import pytest
from django.test import Client

from tests.support.sql import execute, one
from hajiri.workflow import flow_admin, service
from tests.web.factories import htmx, hx_triggers, toasts

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


def test_approvals_page_lists_and_approves(w):
    f = file_leave(w['c'], w['demo']['sita'], '2026-11-20', reason='wedding')
    c = client_for(w['ram'])
    r = c.get('/approvals')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Sita Sharma' in html and 'Leave without pay' in html and '“wedding”' in html
    assert '1 request is waiting for your decision.' in html
    r = c.post(f"/_/workflow/{f['instanceId']}/approve", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Approved.'
    assert 'hj:refresh-approvals' in hx_triggers(r)
    assert one('SELECT status FROM leave_request WHERE id = %s', [f['requestId']])['status'] == 'approved'
    assert 'all caught up' in c.get('/_/workflow/inbox', **htmx('approvals')).content.decode()


def test_reject_modal_and_post(w):
    f = file_leave(w['c'], w['demo']['sita'], '2026-11-21')
    c = client_for(w['ram'])
    r = c.get(f"/_/workflow/{f['instanceId']}/reject", **htmx('hj-modal-body'))
    assert r.status_code == 200 and 'Sita Sharma will see your reason.' in r.content.decode()
    r = c.post(f"/_/workflow/{f['instanceId']}/reject", {'reason': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'a reason is required to reject'
    r = c.post(f"/_/workflow/{f['instanceId']}/reject", {'reason': 'team is short'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Rejected.'
    d = c.get(f"/_/workflow/{f['instanceId']}", **htmx('hj-modal-body')).content.decode()
    assert 'Approval history' in d and 'Reject by' in d and '“team is short”' in d and 'Signature' in d


def test_bulk_approve_toast_counts(w, login_as):
    a = file_leave(w['c'], w['demo']['sita'], '2026-11-23')
    b = file_leave(w['c'], w['demo']['sita'], '2026-11-24')
    service.decide(w['ram'].user, b['instanceId'], 'approve')
    r = client_for(w['ram']).post('/_/workflow/bulk-approve', {'ids': [a['instanceId'], b['instanceId']]}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '1 approved, 1 failed.'


def test_others_cannot_decide_or_read(w, login_as):
    f = file_leave(w['c'], w['demo']['sita'], '2026-11-25')
    other = client_for(login_as(['manager']))
    assert other.post(f"/_/workflow/{f['instanceId']}/approve", **htmx()).status_code == 403
    assert other.get(f"/_/workflow/{f['instanceId']}", **htmx()).status_code == 404
    assert client_for(w['sita']).post(f"/_/workflow/{f['instanceId']}/approve", **htmx()).status_code == 403


def test_approvals_badge_and_nav(w):
    file_leave(w['c'], w['demo']['sita'], '2026-11-26')
    assert service.inbox_count(w['ram'].user) == 1
    from hajiri.workflow.nav import BADGES
    assert BADGES['approvals'](w['ram'].user) == 1
    # an employee with something to decide sees the hub thanks to the badge
    assert BADGES['approvals'](w['sita'].user) == 0


def test_delegate_modal_and_cancel(w):
    c = client_for(w['ram'])
    r = c.get('/_/workflow/delegates', **htmx('hj-modal-body'))
    assert r.status_code == 200 and 'Hand over your approvals' in r.content.decode()
    today = service.npt_today()
    r = c.post('/_/workflow/delegates/new', {'delegate_id': w['hr'].user_id, 'from': today, 'to': today, 'note': 'Annual leave'}, **htmx())
    assert r.status_code == 200
    assert toasts(r)[0]['message'] == 'Your approvals will go to your delegate on those days.'
    assert 'Annual leave' in r.content.decode()
    assert 'Delegated to' in c.get('/_/workflow/delegates/button').content.decode()
    did = one('SELECT id FROM approver_delegate WHERE user_id = %s', [w['ram'].user_id])['id']
    r = c.post(f'/_/workflow/delegates/{did}/cancel', **htmx())
    assert r.status_code == 200
    assert one('SELECT cancelled_at IS NOT NULL AS c FROM approver_delegate WHERE id = %s', [did])['c']
    r = c.post('/_/workflow/delegates/new', {'delegate_id': w['ram'].user_id, 'from': today, 'to': today}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'choose someone else as the delegate'


def test_chain_preview_fragment(w, login_as):
    c = client_for(w['sita'])
    r = c.get('/_/workflow/preview', {'object_type': 'leave_request'}, **htmx())
    assert r.status_code == 200 and 'Approved by' in r.content.decode() and w['ram'].user.name in r.content.decode()
    # silent when it cannot be worked out (a person the user does not manage, or an unknown type)
    assert c.get('/_/workflow/preview', {'object_type': 'leave_request', 'employee_id': w['demo']['ram']}).content == b''
    assert c.get('/_/workflow/preview', {'object_type': 'nope'}).content == b''
    # nobody set: HR decides, with a note
    hr_c = client_for(w['hr'])
    r = hr_c.get('/_/workflow/preview', {'object_type': 'leave_request', 'employee_id': w['demo']['ram']})
    assert 'No line manager is set. HR decides instead.' in r.content.decode()
    assert 'Pick an employee' in hr_c.get('/_/workflow/preview', {'object_type': 'leave_request', 'pick': '1'}).content.decode()
    # the template component points at this URL
    from django.template import Context, Template
    out = Template('{% approval_chain "leave_request" employee_id=e %}').render(Context({'e': w['demo']['sita']}))
    assert '/_/workflow/preview?object_type=leave_request' in out


def test_request_history_page(w, login_as):
    file_leave(w['c'], w['demo']['sita'], '2026-11-27')
    c = client_for(w['hr'])
    r = c.get('/leave/history')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Sita Sharma' in html and 'Leave without pay' in html
    assert 'No approved leave requests' in c.get('/leave/history?status=approved').content.decode()
    assert 'No pending attendance corrections' in c.get('/leave/history?tab=reg').content.decode()
    assert 'No overtime requests' in c.get('/leave/history?tab=ot&status=any').content.decode()
    assert client_for(login_as(['payroll_admin'])).get('/leave/history', **htmx()).status_code == 403
    assert client_for(login_as(['attendance_admin'])).get('/leave/history').status_code == 200


def test_flows_page_tabs(w):
    c = client_for(w['hr'])
    r = c.get('/settings/approvals')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Expense claim' in html and 'Everyone else' in html and 'Add a flow for part of the company' in html
    flow_admin.create_group(w['hr'].user, {'name': 'Plant supervisors', 'userIds': [w['ram'].user_id]})
    g = c.get('/settings/approvals?tab=groups').content.decode()
    assert 'User groups (1)' in g and 'Plant supervisors' in g and w['ram'].user.name in g
    assert 'Check every employee' in c.get('/settings/approvals?tab=check').content.decode()
    assert client_for(w['ram']).get('/settings/approvals', **htmx()).status_code == 403


def test_steps_editor_ops(w):
    c = client_for(w['hr'])
    base = {'prefix': 'leave_request', 'money': '0', 'leave_request-0-kind': 'manager'}
    r = c.post('/_/workflow/steps', {**base, 'op': 'add'}, **htmx())
    html = r.content.decode()
    assert r.status_code == 200 and 'Step 2' in html and 'leave_request-1-role' in html
    r = c.post('/_/workflow/steps', {**base, 'leave_request-1-kind': 'group', 'op': 'kind:1'}, **htmx())
    assert 'Create a group first' in r.content.decode()
    r = c.post('/_/workflow/steps', {**base, 'leave_request-1-kind': 'role', 'leave_request-1-role': 'admin', 'op': 'up:1'}, **htmx())
    html = r.content.decode()
    assert html.index('leave_request-0-role') < html.index('leave_request-1-kind')
    r = c.post('/_/workflow/steps', {**base, 'leave_request-1-kind': 'role', 'op': 'remove:0'}, **htmx())
    assert 'Step 2' not in r.content.decode()
    r = c.post('/_/workflow/steps', {'prefix': 'expense_claim', 'money': '1', 'expense_claim-0-kind': 'manager', 'op': ''}, **htmx())
    assert 'Only from (Rs)' in r.content.decode()


def test_save_default_chain_and_flows_through_forms(w):
    c = client_for(w['hr'])
    r = c.post('/_/workflow/defaults/expense_claim', {'expense_claim-0-kind': 'manager', 'expense_claim-1-kind': 'role',
                                                      'expense_claim-1-role': 'hr', 'expense_claim-1-min': '10,000'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Expense claim: approval chain saved.'
    assert one("SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = 'expense_claim'", [w['c']])['steps'] == \
        [{'kind': 'manager', 'role': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000}]
    r = c.post('/_/workflow/defaults/leave_request', {'leave_request-0-kind': 'user'}, **htmx())
    assert r.status_code == 400 and 'choose the person who approves' in toasts(r)[0]['message']
    # a conditional flow from the drawer form
    u = unit(w['c'], 'OPS')
    assert 'New flow: Overtime' in c.get('/_/workflow/flows/new?type=ot_request', **htmx('hj-drawer-body')).content.decode()
    r = c.post('/_/workflow/flows/new?type=ot_request', {'objectType': 'ot_request', 'name': 'Ops', 'orgUnitIds': [u],
                                                         'flow-0-kind': 'department_head', 'priority': '5'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Flow saved.' and 'hj:refresh-flows' in hx_triggers(r)
    f = one("SELECT * FROM approval_flow WHERE company_id = %s AND name = 'Ops'", [w['c']])
    assert f['priority'] == 5 and f['conditions']['orgUnitIds'] == [u] and f['steps'][0]['kind'] == 'department_head'
    assert 'Edit flow: Overtime' in c.get(f"/_/workflow/flows/{f['id']}/edit", **htmx('hj-drawer-body')).content.decode()
    r = c.post(f"/_/workflow/flows/{f['id']}/edit", {'name': 'Ops team', 'flow-0-kind': 'manager', 'priority': '0'}, **htmx())
    assert r.status_code == 204
    assert one('SELECT name FROM approval_flow WHERE id = %s', [f['id']])['name'] == 'Ops team'
    assert c.post(f"/_/workflow/flows/{f['id']}/active", {'active': '0'}, **htmx()).status_code == 204
    assert one('SELECT active FROM approval_flow WHERE id = %s', [f['id']])['active'] is False
    r = c.post(f"/_/workflow/flows/{f['id']}/delete", **htmx())
    assert toasts(r)[0]['message'] == 'Flow deleted.'
    assert one('SELECT 1 AS x FROM approval_flow WHERE id = %s', [f['id']]) is None


def test_group_forms_and_verify(w):
    c = client_for(w['hr'])
    assert 'New user group' in c.get('/_/workflow/groups/new', **htmx('hj-drawer-body')).content.decode()
    r = c.post('/_/workflow/groups/new', {'name': 'Supervisors', 'description': '', 'userIds': [w['ram'].user_id]}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Group saved.'
    g = one("SELECT id, description FROM user_group WHERE name = 'Supervisors'")
    assert g['description'] is None
    r = c.post(f"/_/workflow/groups/{g['id']}/edit", {'name': 'Supervisors', 'description': 'Floor', 'userIds': []}, **htmx())
    assert r.status_code == 204
    assert one('SELECT count(*)::int AS n FROM user_group_member WHERE group_id = %s', [g['id']])['n'] == 0
    assert toasts(c.post(f"/_/workflow/groups/{g['id']}/delete", **htmx()))[0]['message'] == 'Group deleted.'
    orphan = emp(w['c'], name='Lonely Person')
    v = c.get('/_/workflow/verify', {'object_type': 'leave_request'}, **htmx()).content.decode()
    assert 'Lonely Person' in v and 'Set a line manager on the employee record' in v
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id IN (%s, %s)', [w['demo']['sita'], orphan, w['demo']['ram']])
    execute("UPDATE employee SET status = 'exited' WHERE company_id = %s AND id NOT IN (%s, %s, %s)",
            [w['c'], orphan, w['demo']['ram'], w['demo']['sita']])
    v = c.get('/_/workflow/verify', {'object_type': 'leave_request'}, **htmx()).content.decode()
    assert 'chains reach someone' in v
