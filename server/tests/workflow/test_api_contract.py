"""
Field-by-field contract of the approvals JSON API against what the React app reads (frontend/src/pages/manager/
ApprovalsPage.jsx, components/DelegateApprovals.jsx, ApprovalChainPreview.jsx, pages/hr/ApprovalFlowsPage.jsx) and
what the TS routes returned (65a9079 apps/api/src/modules/workflow/routes.ts, flow-routes.ts, service.ts): exact keys,
JSON types (numbers vs numeric strings), date and timestamp formats, list wrappers and the error shape.
"""
import json
import re
from datetime import date, datetime, timezone

import pytest
from django.test import Client

from hajiri.db.models import OtRequest, PayrollPeriod, PayrollRun, RegularisationRequest
from hajiri.workflow import service
from tests.support.sql import execute, one

from .helpers import api, file_leave

pytestmark = pytest.mark.usefixtures('leave_handler')

ISO_MS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MISSING = '00000000-0000-7000-8000-00000000beef'


@pytest.fixture
def world(demo, login_as):
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    c2 = str(one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id'])
    return {'demo': demo, 'c': demo['company_id'], 'hr': login_as(['hr']), 'sita': login_as(['employee'], demo['sita']),
            'ram': login_as(['manager'], demo['ram']), 'foreign_hr': login_as(['hr'], company_id=c2)}


def _start(world, object_type, object_id, employee_id):
    return service.start_workflow(company_id=world['c'], object_type=object_type, object_id=str(object_id),
                                  employee_id=employee_id, title='t', body='b')


def _item(login, instance_id):
    items = api(login.token).get('/api/v1/workflow/inbox').json()['items']
    return next(i for i in items if i['instanceId'] == instance_id)


def test_inbox_leave_card_keys_types_and_formats(world):
    f = file_leave(world['c'], world['demo']['sita'], '2026-11-05', reason='family')
    r = api(world['ram'].token).get('/api/v1/workflow/inbox')
    assert r.status_code == 200 and set(r.json()) == {'items'} and isinstance(r.json()['items'], list)
    it = _item(world['ram'], f['instanceId'])
    assert set(it) == {'instanceId', 'objectType', 'objectId', 'step', 'totalSteps', 'createdAt', 'employee', 'summary', 'routing'}
    assert it['objectType'] == 'leave_request' and it['objectId'] == f['requestId']
    assert it['step'] == 0 and it['totalSteps'] == 1
    assert ISO_MS.match(it['createdAt'])
    assert set(it['employee']) == {'id', 'empCode', 'nameEn', 'nameNe'} and it['employee']['id'] == world['demo']['sita']
    assert isinstance(it['routing'], list)
    s = it['summary']
    assert set(s) == {'leaveType', 'leaveTypeName', 'from', 'to', 'halfDay', 'days', 'reason', 'balanceNow', 'balanceAfter',
                      'teamOverlap'}
    assert (s['from'], s['to'], s['halfDay'], s['reason'], s['leaveType']) == ('2026-11-05', '2026-11-05', None, 'family', 'LWP')
    # Number(...) in the TS service: JSON numbers, never numeric strings
    assert s['days'] == 1 and type(s['days']) is int
    assert isinstance(s['balanceNow'], (int, float)) and s['balanceAfter'] == s['balanceNow'] - 1
    assert s['teamOverlap'] == []


def test_inbox_hourly_leave_has_time_window(world):
    f = file_leave(world['c'], world['demo']['sita'], '2026-11-06')
    execute("UPDATE leave_request SET from_time = '10:00', to_time = '12:30', hours = 2.5, days_requested = 0.3 WHERE id = %s",
            [f['requestId']])
    s = _item(world['ram'], f['instanceId'])['summary']
    assert (s['fromTime'], s['toTime'], s['hours'], s['days']) == ('10:00', '12:30', 2.5, 0.3)


def test_inbox_correction_and_overtime_cards(world):
    sita = world['demo']['sita']
    punch = datetime(2026, 11, 2, 3, 15, tzinfo=timezone.utc)
    reg = RegularisationRequest.objects.create(employee_id=sita, local_date=date(2026, 11, 2), kind='missed_punch',
                                               punch_time=punch, punch_direction='in', reason='forgot')
    ot = OtRequest.objects.create(employee_id=sita, local_date=date(2026, 11, 3), requested_min=90, reason='release')
    ri = _start(world, 'regularisation_request', reg.pk, sita)
    oi = _start(world, 'ot_request', ot.pk, sita)
    rs = _item(world['ram'], ri['id'])['summary']
    assert rs == {'local_date': '2026-11-02', 'kind': 'missed_punch', 'punch_time': '2026-11-02T03:15:00.000Z',
                  'punch_direction': 'in', 'shift_id': None, 'reason': 'forgot'}
    os_ = _item(world['ram'], oi['id'])['summary']
    assert os_ == {'local_date': '2026-11-03', 'requested_min': 90, 'reason': 'release'}
    # the detail view summarises the same way as the inbox
    assert api(world['ram'].token).get(f"/api/v1/workflow/{ri['id']}").json()['summary'] == rs


def test_payroll_run_card_counts_as_strings_like_postgres_bigint(world):
    p = PayrollPeriod.objects.create(company_id=world['c'], bs_year=2083, bs_month=7, start_ad=date(2026, 10, 18),
                                     end_ad=date(2026, 11, 16), days=30)
    run = PayrollRun.objects.create(company_id=world['c'], period=p)
    inst = _start(world, 'payroll_run', run.pk, None)
    d = api(world['hr'].token).get(f"/api/v1/workflow/{inst['id']}").json()
    assert d['summary'] == {'run_type': 'regular', 'status': 'draft', 'bs_year': 2083, 'bs_month': 7, 'payslips': '0',
                            'total_net': '0'}


def test_detail_instance_and_timeline_shapes(world):
    f = file_leave(world['c'], world['demo']['sita'], '2026-11-09', reason='x')
    assert api(world['ram'].token).post(f"/api/v1/workflow/{f['instanceId']}/approve", {'reason': 'ok'}).json() == {'state': 'approved'}
    r = api(world['sita'].token).get(f"/api/v1/workflow/{f['instanceId']}")
    assert r.status_code == 200
    d = r.json()
    inst = d['instance']
    # `SELECT * FROM workflow_instance`
    assert set(inst) == {'id', 'company_id', 'object_type', 'object_id', 'employee_id', 'steps', 'current_step', 'state',
                         'created_at', 'completed_at', 'routing', 'amount', 'flow_id'}
    assert inst['state'] == 'approved' and inst['current_step'] == 1 and inst['amount'] is None
    assert ISO_MS.match(inst['created_at']) and ISO_MS.match(inst['completed_at'])
    assert [s.get('role') for s in inst['steps']] == ['manager']       # ApprovalsPage: steps.map((s) => s.role)
    [a] = d['actions']
    assert (a['decision'], a['reason'], a['step'], a['on_behalf_of'], a['actor_id']) == ('approve', 'ok', 0, None, world['ram'].user_id)
    assert ISO_MS.match(a['acted_at']) and re.fullmatch(r'[0-9a-f]{64}', a['signature']) and a['actor_name']


def test_decision_errors_use_the_ts_shape(world):
    f = file_leave(world['c'], world['demo']['sita'], '2026-11-10')
    ram = api(world['ram'].token)
    r = ram.get('/api/v1/workflow/not-a-uuid')
    assert r.status_code == 400 and r.json()['error'] == 'validation' and r.json()['issues'][0]['message'] == 'Invalid UUID'
    assert ram.post('/api/v1/workflow/not-a-uuid/approve').status_code == 400
    r = ram.post(f'/api/v1/workflow/{MISSING}/approve')
    assert r.status_code == 404 and r.json() == {'error': 'approval not found'}
    assert ram.get(f'/api/v1/workflow/{MISSING}').json() == {'error': 'approval not found'}
    r = ram.post(f"/api/v1/workflow/{f['instanceId']}/approve", {'approvedMin': 999})
    assert r.status_code == 400 and r.json()['error'] == 'validation' and r.json()['issues'][0]['path'] == 'approvedMin'
    r = ram.post(f"/api/v1/workflow/{f['instanceId']}/reject", {'reason': '  '})
    assert r.status_code == 400 and r.json() == {'error': 'a reason is required to reject'}
    assert ram.post(f"/api/v1/workflow/{f['instanceId']}/reject", {'reason': 'busy week'}).json() == {'state': 'rejected'}
    r = ram.post(f"/api/v1/workflow/{f['instanceId']}/approve")
    assert r.status_code == 409 and r.json() == {'error': 'request already rejected'}
    # another company: not found, and the bulk result names the id
    fx = api(world['foreign_hr'].token)
    assert fx.post(f"/api/v1/workflow/{f['instanceId']}/reject", {'reason': 'nope'}).status_code == 404


def test_bulk_approve_results_and_validation(world):
    a = file_leave(world['c'], world['demo']['sita'], '2026-11-11')
    ram = api(world['ram'].token)
    r = ram.post('/api/v1/workflow/bulk-approve', {'ids': [a['instanceId'], MISSING]})
    assert r.status_code == 200
    assert r.json() == {'results': [{'id': a['instanceId'], 'state': 'approved'}, {'id': MISSING, 'error': 'approval not found'}]}
    again = ram.post('/api/v1/workflow/bulk-approve', {'ids': [a['instanceId']]}).json()
    assert again == {'results': [{'id': a['instanceId'], 'error': 'request already approved'}]}
    r = ram.post('/api/v1/workflow/bulk-approve', {'ids': []})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert ram.post('/api/v1/workflow/bulk-approve', {'ids': ['x']}).status_code == 400
    # another company's HR: the id is reported as not found and nothing changes
    b = file_leave(world['c'], world['demo']['sita'], '2026-11-12')
    fx = api(world['foreign_hr'].token).post('/api/v1/workflow/bulk-approve', {'ids': [b['instanceId']]}).json()
    assert fx == {'results': [{'id': b['instanceId'], 'error': 'approval not found'}]}
    assert one('SELECT state FROM workflow_instance WHERE id = %s', [b['instanceId']]) == {'state': 'pending'}
    assert api(world['foreign_hr'].token).get('/api/v1/workflow/inbox').json() == {'items': []}


def test_flow_config_and_delegation_formats(world):
    hr = api(world['hr'].token)
    c = Client()
    h = {'HTTP_AUTHORIZATION': f"Bearer {world['hr'].token}"}
    fid = json.loads(c.post('/api/v1/workflow/flows', json.dumps({
        'objectType': 'expense_claim', 'name': '  Big claims ', 'conditions': {'branchIds': [world['demo']['branch_id']]},
        'steps': [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000.0, 'label': ' Finance '}],
    }), content_type='application/json', **h).content)['id']
    cfg = hr.get('/api/v1/workflow/flows/config').json()
    assert set(cfg) == {'types', 'groups'}
    t = next(x for x in cfg['types'] if x['objectType'] == 'expense_claim')
    assert t['money'] is True and t['label'] == 'Expense claim'
    assert next(x for x in cfg['types'] if x['objectType'] == 'leave_request')['money'] is False
    [f] = t['flows']
    assert f['id'] == fid and f['object_type'] == 'expense_claim' and f['name'] == 'Big claims'
    assert f['priority'] == 0 and f['active'] is True and ISO_MS.match(f['updated_at'])
    assert f['conditions'] == {'employeeIds': [], 'groupIds': [], 'designationIds': [], 'orgUnitIds': [],
                               'branchIds': [world['demo']['branch_id']]}
    assert f['steps'] == [{'kind': 'manager', 'role': 'manager'},
                          {'kind': 'role', 'role': 'hr', 'minAmount': 10000, 'label': 'Finance'}]
    # the edit drawer sends the whole flow back (toStepInput shape) and the switch sends {active}
    assert hr_patch(world, fid, {'name': 'Big', 'conditions': f['conditions'], 'priority': 0,
                                 'steps': [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000}]}) == {'ok': True}
    assert hr_patch(world, fid, {'active': False}) == {'ok': True}
    assert one('SELECT name, active, priority FROM approval_flow WHERE id = %s', [fid]) == {'name': 'Big', 'active': False, 'priority': 0}
    # a group saved from the drawer with an empty description sends null
    gid = hr.post('/api/v1/workflow/groups', {'name': 'G', 'description': 'd', 'userIds': []}).json()['id']
    c.patch(f'/api/v1/workflow/groups/{gid}', json.dumps({'name': 'G', 'description': None, 'userIds': []}),
            content_type='application/json', **h)
    g = next(x for x in hr.get('/api/v1/workflow/flows/config').json()['groups'] if x['id'] == gid)
    assert g == {'id': gid, 'name': 'G', 'description': None, 'members': []}
    # delegations: AD dates as 'YYYY-MM-DD' (DelegateApprovals compares them with todayNpt()), timestamps ISO
    today = service.npt_today()
    did = api(world['ram'].token).post('/api/v1/workflow/delegates',
                                       {'delegateId': world['hr'].user_id, 'from': today, 'to': '2099-01-01'}).json()['id']
    [d] = api(world['ram'].token).get('/api/v1/workflow/delegates').json()['delegates']
    assert d['id'] == did and d['user_id'] == world['ram'].user_id and d['delegate_id'] == world['hr'].user_id
    assert DAY.match(d['from_date']) and d['from_date'] == today and d['to_date'] == '2099-01-01'
    assert ISO_MS.match(d['created_at']) and d['cancelled_at'] is None and d['note'] is None
    r = api(world['ram'].token).post('/api/v1/workflow/delegates', {'delegateId': world['hr'].user_id, 'from': '2020-01-01',
                                                                    'to': '2020-01-02'})
    assert r.status_code == 400 and r.json() == {'error': 'the delegation has already ended'}


def hr_patch(world, fid, body):
    r = Client().patch(f'/api/v1/workflow/flows/{fid}', json.dumps(body), content_type='application/json',
                       HTTP_AUTHORIZATION=f"Bearer {world['hr'].token}")
    assert r.status_code == 200, r.content
    return r.json()


def test_preview_shape_for_the_chain_preview_component(world):
    r = api(world['sita'].token).get('/api/v1/workflow/preview', {'objectType': 'leave_request'})
    assert r.status_code == 200
    body = r.json()
    assert body['flow'] is None
    [s] = body['steps']
    assert s['minAmount'] is None and isinstance(s['notes'], list) and s['fallback'] is False
    assert s['approvers'] == [{'id': world['ram'].user_id, 'name': one('SELECT name FROM app_user WHERE id = %s', [world['ram'].user_id])['name']}]
    # amount comes as a query string (?amount=25000) and is coerced
    assert api(world['sita'].token).get('/api/v1/workflow/preview', {'objectType': 'expense_claim', 'amount': '25000'}).status_code == 200
    r = api(world['sita'].token).get('/api/v1/workflow/preview', {'objectType': 'expense_claim', 'amount': 'abc'})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
