"""Gaps found in the completeness audit: step labels survive the editor, bulk-approve confirmation and errors,
reject modal scoping, legacy addresses and the live chain component."""
import html as _html

import pytest
from django.template import Context, Template

from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError
from hajiri.workflow import flow_admin, service


from .helpers import file_leave
from .test_views import client_for, w  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('leave_handler')


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
