"""Gaps found in the completeness audit: step labels survive the editor, bulk-approve confirmation and errors,
reject modal scoping, legacy addresses and the live chain component."""
import html as _html

import pytest
from django.template import Context, Template

from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError
from hajiri.workflow import flow_admin, service
from tests.web.factories import htmx, toasts

from .helpers import file_leave
from .test_views import client_for, w  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('leave_handler')


def test_step_label_survives_the_editor_but_not_a_kind_change(w):  # noqa: F811
    hr = w['hr']
    execute("""INSERT INTO workflow_definition (company_id, object_type, steps) VALUES (%s, 'ot_request', %s)
               ON CONFLICT (company_id, object_type) DO UPDATE SET steps = EXCLUDED.steps""",
            [w['c'], '[{"kind": "branch_head", "role": "branch_head", "label": "Site boss"}]'])
    c = client_for(hr)
    page = c.get('/settings/approvals?tab=flows&open=ot_request').content.decode()
    assert 'name="ot_request-0-label" value="Site boss"' in page
    # saving the editor as rendered keeps the label (toStepInput keeps it in the TS editor)
    r = c.post('/_/workflow/defaults/ot_request', {'ot_request-0-kind': 'branch_head', 'ot_request-0-label': 'Site boss'}, **htmx())
    assert r.status_code == 204
    assert one("SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = 'ot_request'", [w['c']])['steps'] == \
        [{'kind': 'branch_head', 'role': 'branch_head', 'label': 'Site boss'}]
    # moving keeps it, changing the kind drops it
    r = c.post('/_/workflow/steps', {'prefix': 'ot_request', 'money': '0', 'ot_request-0-kind': 'branch_head',
                                     'ot_request-0-label': 'Site boss', 'op': 'add'}, **htmx())
    assert 'value="Site boss"' in r.content.decode()
    r = c.post('/_/workflow/steps', {'prefix': 'ot_request', 'money': '0', 'ot_request-0-kind': 'manager',
                                     'ot_request-0-label': 'Site boss', 'op': 'kind:0'}, **htmx())
    assert 'Site boss' not in r.content.decode()


def test_bulk_confirm_names_the_count(w):  # noqa: F811
    file_leave(w['c'], w['demo']['sita'], '2026-12-01')
    file_leave(w['c'], w['demo']['sita'], '2026-12-02')
    html = _html.unescape(client_for(w['ram']).get('/approvals').content.decode())
    assert 'Approve all (2)' in html
    assert 'hx-confirm="2 requests will be approved in one go. Reject any that need it first."' in html
    assert 'data-confirm-label="Approve 2"' in html


def test_bulk_approve_reports_any_failure_and_goes_on(w):  # noqa: F811
    a = file_leave(w['c'], w['demo']['sita'], '2026-12-03')
    b = file_leave(w['c'], w['demo']['sita'], '2026-12-04')
    had = service.handler_for('leave_request')

    def flaky(object_id, state, ctx):
        if object_id == a['requestId']:
            raise RuntimeError('ledger is locked')
        had(object_id, state, ctx)
    service.register_handler('leave_request', flaky)
    try:
        res = service.bulk_approve(w['ram'].user, [a['instanceId'], b['instanceId']])
    finally:
        service.register_handler('leave_request', had)
    assert res == [{'id': a['instanceId'], 'error': 'ledger is locked'}, {'id': b['instanceId'], 'state': 'approved'}]
    # the failed one rolled back whole: still pending, no signed action
    assert service.get_instance(a['instanceId'])['state'] == 'pending'
    assert one('SELECT count(*)::int AS n FROM approval_action WHERE instance_id = %s', [a['instanceId']])['n'] == 0


def test_reject_modal_only_for_people_who_may_read_it(w, login_as):  # noqa: F811
    f = file_leave(w['c'], w['demo']['sita'], '2026-12-05')
    stranger = login_as(['employee'])
    r = client_for(stranger).get(f"/_/workflow/{f['instanceId']}/reject", **htmx('hj-modal-body'))
    assert r.status_code == 404
    assert client_for(w['ram']).get(f"/_/workflow/{f['instanceId']}/reject", **htmx('hj-modal-body')).status_code == 200


def test_legacy_addresses_redirect(w):  # noqa: F811
    c = client_for(w['hr'])
    r = c.get('/manager/approvals')
    assert r.status_code in (301, 302) and r['Location'] == '/approvals'
    r = c.get('/hr/requests?status=approved')
    assert r.status_code in (301, 302) and r['Location'] == '/leave/history?status=approved'


def test_live_chain_component_reads_the_form(w):  # noqa: F811
    out = Template('{% load workflow_tags %}{% approval_chain_live "expense_claim" employee_id=e amount=5 %}').render(
        Context({'e': w['demo']['sita']}))
    assert 'id="chain-expense_claim"' in out and '/_/workflow/preview?object_type=expense_claim' in out
    assert 'from:closest form' in out and 'hj:chain-expense_claim from:body' in out
    flow_admin.set_default(w['hr'].user, 'expense_claim', {'steps': [{'kind': 'manager'}, {'kind': 'role', 'role': 'hr', 'minAmount': 10000}]})
    c = client_for(w['sita'])
    # what the component sends: object_type from its URL, then the form's employee and amount fields
    small = c.get('/_/workflow/preview?object_type=expense_claim&employee_id=&amount=5000').content.decode()
    big = c.get('/_/workflow/preview?object_type=expense_claim&employee_id=&amount=25000').content.decode()
    assert small.count('ti-arrow-right') == 0 and big.count('ti-arrow-right') == 1


def test_preview_of_unknown_type_and_bad_amount(w):  # noqa: F811
    from hajiri.core.errors import ValidationError
    with pytest.raises(ValidationError):
        flow_admin.preview(w['sita'].user, {'objectType': 'nope'})
    with pytest.raises(ValidationError):
        flow_admin.preview(w['sita'].user, {'objectType': 'leave_request', 'amount': '-1'})
    # someone without an employee record must name one
    with pytest.raises(BadRequestError, match='employeeId required'):
        flow_admin.preview(w['hr'].user, {'objectType': 'leave_request'})


def test_next_step_notification_and_fallback_routing(w):  # noqa: F811
    d = w['demo']
    execute("""UPDATE workflow_definition SET steps = '[{"role":"manager"},{"role":"hr"}]'
                WHERE company_id = %s AND object_type = 'leave_request'""", [d['company_id']])
    f = file_leave(d['company_id'], d['sita'], '2026-12-08')
    assert service.decide(w['ram'].user, f['instanceId'], 'approve') == 'pending'
    n = one("""SELECT title, body, payload FROM notification_outbox
                WHERE user_id = %s AND template = 'leave_request.pending' AND channel = 'push' ORDER BY created_at DESC LIMIT 1""", [w['hr'].user_id])
    assert n['title'] == 'Approval needed' and n['body'] == 'leave request awaiting your decision (step 2)'
    assert n['payload'] == {'instanceId': f['instanceId'], 'objectType': 'leave_request', 'objectId': f['requestId'], 'step': 1}
    # nobody set at the first step: HR decides and the instance says why
    g = file_leave(d['company_id'], d['ram'], '2026-12-09')
    routing = service.get_instance(g['instanceId'])['routing']
    assert routing[0]['step'] == 0 and routing[0]['notes'] == ['No line manager is set', 'HR decides instead']
    item = next(i for i in service.inbox_for(w['hr'].user) if i['instanceId'] == g['instanceId'])
    assert item['routing'] == ['No line manager is set', 'HR decides instead']
