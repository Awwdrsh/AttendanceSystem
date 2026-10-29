"""
HTTP-level tests of the approval flow set-up JSON API (apps/api/src/modules/workflow/flow-routes.ts, the 'approval
flows' block of apps/api/test/roles-flows.test.ts): flows config, default chains, conditional flows, user groups,
delegation, the chain preview and the verify report, as the React app calls them under /api/v1/workflow.
"""
import json

import pytest
from django.test import Client

from hajiri.workflow import service
from tests.support.sql import execute, one

from .helpers import emp, new_object_id, unit

FOREIGN = '00000000-0000-7000-8000-00000000dead'


class _Api:
    """A Bearer JSON client with every method the React app uses."""

    def __init__(self, token: str):
        self.c = Client()
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get(self, path, data=None):
        return self.c.get(path, data or {}, **self.h)

    def _send(self, method, path, body):
        return getattr(self.c, method)(path, json.dumps(body if body is not None else {}), content_type='application/json', **self.h)

    def post(self, path, body=None):
        return self._send('post', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)

    def delete(self, path):
        return self.c.delete(path, **self.h)


def api(login) -> _Api:
    return _Api(login.token)


@pytest.fixture
def w(demo, login_as):
    service.register_handler('letter_request', lambda *a: None)
    c = demo['company_id']
    b = demo['branch_id']
    dept_head = emp(c, branch=b)
    unit_id = unit(c, 'FIN', None, dept_head)
    mgr = emp(c, org_unit=unit_id, manager=dept_head, branch=b)
    worker = emp(c, org_unit=unit_id, manager=mgr, branch=b)
    c2 = str(one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id'])
    foreign_emp = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Foreign') RETURNING id", [c2])['id'])
    yield {
        'c': c, 'demo': demo, 'dept_head': dept_head, 'unit_id': unit_id, 'mgr': mgr, 'worker': worker,
        'hr': login_as(['hr']), 'exec': login_as(['executive']),
        'mgr_l': login_as(['manager'], mgr), 'head_l': login_as(['employee'], dept_head),
        'worker_l': login_as(['employee'], worker), 'other': login_as(['employee'], emp(c, branch=b)),
        'c2': c2, 'foreign_emp': foreign_emp, 'foreign_hr': login_as(['hr'], company_id=c2),
    }
    service.unregister_handler('letter_request')


def _inbox(login):
    return [i['instanceId'] for i in api(login).get('/api/v1/workflow/inbox').json()['items']]


def _start(w, object_type, employee, **kw):
    return service.start_workflow(company_id=w['c'], object_type=object_type, object_id=new_object_id(), employee_id=employee,
                                  title='t', body='b', **kw)


def test_default_chain_through_put(w):
    hr = api(w['hr'])
    r = hr.put('/api/v1/workflow/flows/defaults/letter_request', {'steps': [{'kind': 'manager'}, {'kind': 'department_head'}]})
    assert r.status_code == 200
    assert r.json() == {'objectType': 'letter_request',
                        'steps': [{'kind': 'manager', 'role': 'manager'}, {'kind': 'department_head', 'role': 'department_head'}]}
    inst = _start(w, 'letter_request', w['worker'])
    assert [s['kind'] for s in inst['steps']] == ['manager', 'department_head']
    assert inst['id'] in _inbox(w['mgr_l'])
    assert inst['id'] not in _inbox(w['head_l'])
    assert api(w['mgr_l']).post(f"/api/v1/workflow/{inst['id']}/approve").json() == {'state': 'pending'}
    assert inst['id'] in _inbox(w['head_l'])
    assert api(w['worker_l']).post(f"/api/v1/workflow/{inst['id']}/approve").status_code == 403
    assert api(w['head_l']).post(f"/api/v1/workflow/{inst['id']}/approve").json() == {'state': 'approved'}
    d = api(w['hr']).get(f"/api/v1/workflow/{inst['id']}").json()
    assert set(d) == {'instance', 'summary', 'actions'} and d['instance']['state'] == 'approved'
    assert [a['decision'] for a in d['actions']] == ['approve', 'approve']
    assert {'step', 'decision', 'reason', 'acted_at', 'signature', 'on_behalf_of', 'actor_id', 'actor_name'} == set(d['actions'][0])
    assert api(w['foreign_hr']).get(f"/api/v1/workflow/{inst['id']}").status_code == 404
    cfg = hr.get('/api/v1/workflow/flows/config').json()
    letter = next(t for t in cfg['types'] if t['objectType'] == 'letter_request')
    assert [s['kind'] for s in letter['defaultSteps']] == ['manager', 'department_head']
    assert set(letter) == {'objectType', 'label', 'money', 'defaultSteps', 'flows'}


def test_most_specific_conditional_flow_wins_and_groups_approve(w):
    hr = api(w['hr'])
    g = hr.post('/api/v1/workflow/groups', {'name': 'Finance approvers', 'userIds': [w['other'].user_id]})
    assert g.status_code == 201 and set(g.json()) == {'id'}
    dept = hr.post('/api/v1/workflow/flows', {'objectType': 'letter_request', 'name': 'Finance department',
                                              'conditions': {'orgUnitIds': [w['unit_id']]},
                                              'steps': [{'kind': 'group', 'groupId': g.json()['id']}]})
    assert dept.status_code == 201
    person = hr.post('/api/v1/workflow/flows', {'objectType': 'letter_request', 'name': 'Just this person',
                                                'conditions': {'employeeIds': [w['mgr']]}, 'steps': [{'kind': 'role', 'role': 'hr'}]})
    assert person.status_code == 201

    p1 = api(w['worker_l']).get('/api/v1/workflow/preview', {'objectType': 'letter_request'})
    assert p1.status_code == 200
    assert p1.json()['flow'] == {'id': dept.json()['id'], 'name': 'Finance department'}
    step = p1.json()['steps'][0]
    assert set(step) == {'label', 'minAmount', 'approvers', 'notes', 'fallback'}
    assert [a['id'] for a in step['approvers']] == [w['other'].user_id]
    p2 = api(w['mgr_l']).get('/api/v1/workflow/preview', {'objectType': 'letter_request'})
    assert p2.json()['flow']['name'] == 'Just this person'

    inst = _start(w, 'letter_request', w['worker'])
    assert inst['id'] in _inbox(w['other'])
    assert inst['id'] not in _inbox(w['mgr_l'])
    assert api(w['other']).post(f"/api/v1/workflow/{inst['id']}/approve").json()['state'] == 'approved'

    cfg = hr.get('/api/v1/workflow/flows/config').json()
    flows = next(t for t in cfg['types'] if t['objectType'] == 'letter_request')['flows']
    assert {f['name'] for f in flows} == {'Finance department', 'Just this person'}
    assert set(flows[0]) == {'id', 'object_type', 'name', 'conditions', 'steps', 'priority', 'active', 'updated_at'}
    grp = next(x for x in cfg['groups'] if x['name'] == 'Finance approvers')
    assert grp['members'] == [{'id': w['other'].user_id, 'name': w['other'].email}]


def test_amount_thresholds_add_a_step_only_for_larger_requests(w):
    hr = api(w['hr'])
    hr.put('/api/v1/workflow/flows/defaults/expense_claim', {'steps': [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000}]})
    small = api(w['worker_l']).get('/api/v1/workflow/preview', {'objectType': 'expense_claim', 'amount': 5000})
    big = api(w['worker_l']).get('/api/v1/workflow/preview', {'objectType': 'expense_claim', 'amount': 25000})
    assert len(small.json()['steps']) == 1
    assert len(big.json()['steps']) == 2
    assert big.json()['steps'][1]['minAmount'] == 10000
    inst = _start(w, 'expense_claim', w['worker'], amount=4000)
    assert len(inst['steps']) == 1


def test_delegation_through_the_api(w):
    today = service.npt_today()
    mgr = api(w['mgr_l'])
    cands = mgr.get('/api/v1/workflow/delegates/candidates').json()['users']
    assert w['other'].user_id in [u['id'] for u in cands] and w['mgr_l'].user_id not in [u['id'] for u in cands]
    assert set(cands[0]) == {'id', 'name'}
    d = mgr.post('/api/v1/workflow/delegates', {'delegateId': w['other'].user_id, 'from': today, 'to': today, 'note': 'away'})
    assert d.status_code == 201 and set(d.json()) == {'id'}
    listed = mgr.get('/api/v1/workflow/delegates').json()['delegates']
    assert [x['id'] for x in listed] == [d.json()['id']]
    assert listed[0]['delegate_name'] == w['other'].email and listed[0]['from_date'] == today
    assert {'id', 'user_id', 'user_name', 'delegate_id', 'delegate_name', 'from_date', 'to_date', 'note', 'cancelled_at',
            'created_at'} == set(listed[0])
    # the delegate sees it too; HR sees everyone's only with all=1
    assert [x['id'] for x in api(w['other']).get('/api/v1/workflow/delegates').json()['delegates']] == [d.json()['id']]
    assert api(w['hr']).get('/api/v1/workflow/delegates').json()['delegates'] == []
    assert [x['id'] for x in api(w['hr']).get('/api/v1/workflow/delegates', {'all': 1}).json()['delegates']] == [d.json()['id']]
    assert api(w['worker_l']).get('/api/v1/workflow/delegates', {'all': 1}).json()['delegates'] == []
    # someone else cannot set or cancel the manager's delegation
    r = api(w['worker_l']).post('/api/v1/workflow/delegates', {'userId': w['mgr_l'].user_id, 'delegateId': w['other'].user_id,
                                                                'from': today, 'to': today})
    assert r.status_code == 403 and r.json() == {'error': 'only HR can set a delegate for someone else'}
    r = api(w['worker_l']).delete(f"/api/v1/workflow/delegates/{d.json()['id']}")
    assert r.status_code == 404 and r.json() == {'error': 'delegation not found'}
    # rules
    bad = mgr.post('/api/v1/workflow/delegates', {'delegateId': w['other'].user_id, 'from': '2099-01-02', 'to': '2099-01-01'})
    assert bad.status_code == 400 and bad.json() == {'error': 'the end date is before the start date'}
    bad = mgr.post('/api/v1/workflow/delegates', {'delegateId': w['mgr_l'].user_id, 'from': today, 'to': today})
    assert bad.json() == {'error': 'choose someone else as the delegate'}
    bad = mgr.post('/api/v1/workflow/delegates', {'delegateId': FOREIGN, 'from': today, 'to': today})
    assert bad.json() == {'error': 'the delegate must be an active user of your company'}
    bad = mgr.post('/api/v1/workflow/delegates', {'from': today})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert {i['path'] for i in bad.json()['issues']} >= {'delegateId', 'to'}
    # the owner cancels
    assert mgr.delete(f"/api/v1/workflow/delegates/{d.json()['id']}").json() == {'ok': True}
    assert mgr.get('/api/v1/workflow/delegates').json()['delegates'] == []
    assert mgr.delete(f"/api/v1/workflow/delegates/{d.json()['id']}").status_code == 404


def test_delegate_receives_approvals_of_an_away_manager(w):
    hr = api(w['hr'])
    hr.put('/api/v1/workflow/flows/defaults/training_nomination', {'steps': [{'kind': 'manager'}]})
    today = service.npt_today()
    home = str(one("SELECT id FROM leave_type WHERE company_id = %s AND code = 'HOME'", [w['c']])['id'])
    execute("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status)
               VALUES (%s, %s, %s, %s, 1, 'approved')""", [w['mgr'], home, today, today])
    inst = _start(w, 'training_nomination', w['worker'])
    assert inst['id'] not in _inbox(w['mgr_l'])
    assert inst['id'] in _inbox(w['hr'])
    assert api(w['mgr_l']).post('/api/v1/workflow/delegates', {'delegateId': w['other'].user_id, 'from': today, 'to': today}).status_code == 201
    assert inst['id'] in _inbox(w['other'])


def test_verify_lists_employees_whose_chain_reaches_nobody(w):
    orphan = emp(w['c'], branch=w['demo']['branch_id'])
    v = api(w['hr']).get('/api/v1/workflow/verify', {'objectType': 'leave_request'})
    assert v.status_code == 200
    assert v.json()['checked'] > 0
    mine = [p for p in v.json()['problems'] if p['employeeId'] == orphan]
    assert len(mine) == 1
    assert 'line manager' in mine[0]['suggestion']
    assert {'employeeId', 'empCode', 'name', 'objectType', 'typeLabel', 'flow', 'step', 'stepLabel', 'problem',
            'suggestion'} == set(mine[0])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [w['mgr'], orphan])
    v2 = api(w['hr']).get('/api/v1/workflow/verify', {'objectType': 'leave_request'})
    assert [p for p in v2.json()['problems'] if p['employeeId'] == orphan] == []
    assert api(w['hr']).get('/api/v1/workflow/verify', {'objectType': 'nope'}).status_code == 400
    assert api(w['hr']).get('/api/v1/workflow/verify').status_code == 200
    assert api(w['mgr_l']).get('/api/v1/workflow/verify').json() == {'error': 'requires role hr'}


def test_nobody_outside_hr_edits_flows_and_refs_stay_in_company(w):
    m = api(w['mgr_l'])
    r = m.get('/api/v1/workflow/flows/config')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    assert m.put('/api/v1/workflow/flows/defaults/leave_request', {'steps': [{'kind': 'manager'}]}).status_code == 403
    assert m.post('/api/v1/workflow/groups', {'name': 'x'}).status_code == 403
    assert m.post('/api/v1/workflow/flows', {'objectType': 'leave_request', 'name': 'x', 'steps': [{'kind': 'manager'}]}).status_code == 403
    assert api(w['exec']).get('/api/v1/workflow/flows/config').status_code == 403
    hr = api(w['hr'])
    r = hr.put('/api/v1/workflow/flows/defaults/leave_request', {'steps': [{'kind': 'user', 'userId': FOREIGN}]})
    assert r.status_code == 400 and r.json() == {'error': 'the approver chosen is not a user of your company'}
    r = hr.post('/api/v1/workflow/flows', {'objectType': 'leave_request', 'name': 'x', 'conditions': {'branchIds': [FOREIGN]},
                                           'steps': [{'kind': 'manager'}]})
    assert r.status_code == 400 and r.json() == {'error': 'one of the branches is not in your company'}
    assert hr.put('/api/v1/workflow/flows/defaults/not_a_type', {'steps': [{'kind': 'manager'}]}).status_code == 400
    r = hr.put('/api/v1/workflow/flows/defaults/leave_request', {'steps': [{'kind': 'role'}]})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert r.json()['issues'][0]['message'] == 'choose the role that approves'
    assert hr.put('/api/v1/workflow/flows/defaults/leave_request', {'steps': [{'kind': 'role', 'role': 'employee'}]}).status_code == 400
    assert hr.post('/api/v1/workflow/flows', {'objectType': 'leave_request', 'steps': [{'kind': 'manager'}]}).status_code == 400
    assert hr.post('/api/v1/workflow/groups', {'name': 'x', 'userIds': [FOREIGN]}).json() == \
        {'error': 'one of the members is not a user of your company'}


def test_preview_for_someone_else_needs_to_manage_them(w):
    assert api(w['worker_l']).get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['mgr']}).status_code == 403
    assert api(w['mgr_l']).get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['worker']}).status_code == 200
    assert api(w['worker_l']).get('/api/v1/workflow/preview', {'objectType': 'nope'}).status_code == 400
    r = api(w['hr']).get('/api/v1/workflow/preview', {'objectType': 'leave_request'})
    assert r.status_code == 400 and r.json() == {'error': 'employeeId required'}


def test_groups_and_flows_update_and_delete(w):
    hr = api(w['hr'])
    gid = hr.post('/api/v1/workflow/groups', {'name': 'Finance approvers', 'userIds': [w['other'].user_id]}).json()['id']
    hr.post('/api/v1/workflow/flows', {'objectType': 'letter_request', 'name': 'Finance department',
                                       'conditions': {'orgUnitIds': [w['unit_id']]}, 'steps': [{'kind': 'group', 'groupId': gid}]})
    r = hr.delete(f'/api/v1/workflow/groups/{gid}')
    assert r.status_code == 400 and 'this group is used in an approval flow' in r.json()['error']
    # rename, describe and change members
    assert hr.patch(f'/api/v1/workflow/groups/{gid}', {'name': 'Finance', 'description': 'money people',
                                                       'userIds': [w['hr'].user_id]}).json() == {'ok': True}
    g = next(x for x in hr.get('/api/v1/workflow/flows/config').json()['groups'] if x['id'] == gid)
    assert g['name'] == 'Finance' and g['description'] == 'money people' and [m['id'] for m in g['members']] == [w['hr'].user_id]
    spare = hr.post('/api/v1/workflow/groups', {'name': 'Spare'}).json()['id']
    assert hr.delete(f'/api/v1/workflow/groups/{spare}').json() == {'ok': True}
    assert hr.delete(f'/api/v1/workflow/groups/{spare}').json() == {'error': 'group not found'}
    # flows: toggle, rename, change steps, delete
    fid = hr.post('/api/v1/workflow/flows', {'objectType': 'ot_request', 'name': 'Everyone', 'steps': [{'kind': 'manager2'}]}).json()['id']
    assert hr.patch(f'/api/v1/workflow/flows/{fid}', {'active': False}).json() == {'ok': True}
    assert one('SELECT active, name FROM approval_flow WHERE id = %s', [fid]) == {'active': False, 'name': 'Everyone'}
    hr.patch(f'/api/v1/workflow/flows/{fid}', {'name': 'Renamed', 'steps': [{'kind': 'branch_head', 'label': 'Site boss'}]})
    row = one('SELECT name, steps FROM approval_flow WHERE id = %s', [fid])
    assert row['name'] == 'Renamed' and row['steps'] == [{'kind': 'branch_head', 'role': 'branch_head', 'label': 'Site boss'}]
    assert hr.patch(f'/api/v1/workflow/flows/{fid}', {'priority': 500}).status_code == 400
    assert hr.delete(f'/api/v1/workflow/flows/{fid}').json() == {'ok': True}
    r = hr.delete(f'/api/v1/workflow/flows/{fid}')
    assert r.status_code == 404 and r.json() == {'error': 'flow not found'}
    assert hr.patch('/api/v1/workflow/flows/not-a-uuid', {'active': True}).status_code == 400


def test_other_companies_ids_behave_as_not_found(w):
    hr = api(w['hr'])
    gid = hr.post('/api/v1/workflow/groups', {'name': 'Finance approvers'}).json()['id']
    fid = hr.post('/api/v1/workflow/flows', {'objectType': 'ot_request', 'name': 'Everyone', 'steps': [{'kind': 'manager'}]}).json()['id']
    did = api(w['mgr_l']).post('/api/v1/workflow/delegates', {'delegateId': w['other'].user_id, 'from': service.npt_today(),
                                                               'to': service.npt_today()}).json()['id']
    fx = api(w['foreign_hr'])
    assert fx.patch(f'/api/v1/workflow/flows/{fid}', {'name': 'mine'}).json() == {'error': 'flow not found'}
    assert fx.delete(f'/api/v1/workflow/flows/{fid}').status_code == 404
    assert fx.patch(f'/api/v1/workflow/groups/{gid}', {'name': 'mine'}).json() == {'error': 'group not found'}
    assert fx.delete(f'/api/v1/workflow/groups/{gid}').status_code == 404
    assert fx.delete(f'/api/v1/workflow/delegates/{did}').json() == {'error': 'delegation not found'}
    assert fx.get('/api/v1/workflow/delegates', {'all': 1}).json()['delegates'] == []
    cfg = fx.get('/api/v1/workflow/flows/config').json()
    assert all(t['flows'] == [] for t in cfg['types']) and cfg['groups'] == []
    assert w['hr'].user_id not in [u['id'] for u in fx.get('/api/v1/workflow/delegates/candidates').json()['users']]
    # references to another company's people and groups are refused
    assert fx.put('/api/v1/workflow/flows/defaults/leave_request', {'steps': [{'kind': 'group', 'groupId': gid}]}).json() == \
        {'error': 'the approver group does not exist'}
    assert fx.post('/api/v1/workflow/flows', {'objectType': 'leave_request', 'name': 'x', 'conditions': {'employeeIds': [w['worker']]},
                                              'steps': [{'kind': 'manager'}]}).json() == {'error': 'one of the employees is not in your company'}
    assert fx.post('/api/v1/workflow/groups', {'name': 'x', 'userIds': [w['hr'].user_id]}).status_code == 400
    assert fx.get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['worker']}).status_code == 404
    assert hr.get('/api/v1/workflow/preview', {'objectType': 'leave_request', 'employeeId': w['foreign_emp']}).status_code == 404
    # nothing changed at home
    assert one('SELECT name FROM approval_flow WHERE id = %s', [fid]) == {'name': 'Everyone'}
    assert one('SELECT name FROM user_group WHERE id = %s', [gid]) == {'name': 'Finance approvers'}


def test_the_react_app_cookie_session_reaches_the_api(w):
    c = Client()
    c.cookies['hajiri_session'] = w['hr'].token
    r = c.get('/api/v1/workflow/flows/config')
    assert r.status_code == 200 and r.json()['types']
    assert c.get('/api/v1/workflow/delegates').json() == {'delegates': []}
    assert Client().get('/api/v1/workflow/flows/config').status_code == 401
