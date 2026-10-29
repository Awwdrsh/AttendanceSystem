"""Groups of companies and the attendance tools: another company's penalty, employee or request is not found, uploads
match employee codes of the active company only, and lists show only that company."""
from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client

from hajiri.db.models import LatePenalty, WorkflowInstance
from tests.multicompany.conftest import world  # noqa: F401 - the shared group fixture
from tests.web.factories import bearer, make_employee


def _post(token, url, body):
    return Client().post(url, json.dumps(body), content_type='application/json', **bearer(token))


@pytest.fixture
def beta(world):
    emp = make_employee(world.beta, code='SAME1', name='Bina Beta')
    penalty = LatePenalty.objects.create(company=world.beta, employee=emp, bs_year=2083, bs_month=6, late_days=3,
                                         penalty_days=0.5, unpaid_days=0.5)
    inst = WorkflowInstance.objects.create(company=world.beta, object_type='leave_request', object_id=uuid.uuid4(),
                                           employee=emp, steps=[{'role': 'manager'}])
    return {'emp': str(emp.id), 'penalty': str(penalty.id), 'inst': str(inst.id)}


def test_another_companys_penalty_employee_and_request_are_not_found(world, beta):
    for person in (world.admin_c, world.admin_a):
        token, _ = world.session(person)
        r = _post(token, f"/api/v1/attendance-tools/penalties/{beta['penalty']}/reverse", {'reason': 'device clock was wrong'})
        assert r.status_code == 404 and r.json()['error'] == 'penalty not found'
        r = _post(token, '/api/v1/attendance-tools/issues/nudge',
                  {'target': 'employee', 'employeeId': beta['emp'], 'from': '2026-10-01', 'to': '2026-10-10'})
        assert r.status_code == 404 and r.json()['error'] == 'employee not found'
        r = _post(token, '/api/v1/attendance-tools/issues/nudge', {'target': 'approver', 'instanceId': beta['inst']})
        assert r.status_code == 404 and r.json()['error'] == 'request not found'
    assert LatePenalty.objects.get(pk=beta['penalty']).status == LatePenalty.Status.APPLIED
    owner, _ = world.session(world.admin_b)
    r = _post(owner, f"/api/v1/attendance-tools/penalties/{beta['penalty']}/reverse", {'reason': 'device clock was wrong'})
    assert r.status_code == 200
    assert LatePenalty.objects.get(pk=beta['penalty']).status == LatePenalty.Status.REVERSED


def test_upload_matches_codes_of_the_active_company_only(world, beta):
    csv = 'emp_code,date_ad,in_time,out_time\nSAME1,2026-10-01,09:00,17:00\n'
    alpha, _ = world.session(world.admin_a)
    rows = _post(alpha, '/api/v1/attendance-tools/upload/check', {'csv': csv}).json()['rows']
    assert rows[0]['errors'] == ['No employee with code SAME1.']
    owner, _ = world.session(world.admin_b)
    rows = _post(owner, '/api/v1/attendance-tools/upload/check', {'csv': csv}).json()['rows']
    assert rows[0]['employeeId'] == beta['emp'] and rows[0]['errors'] == []


def test_penalty_lists_and_uploads_show_only_the_active_company(world, beta):
    alpha, _ = world.session(world.admin_a)
    owner, _ = world.session(world.admin_b)
    q = {'bsYear': 2083, 'bsMonth': 6}
    assert Client().get('/api/v1/attendance-tools/penalties', q, **bearer(alpha)).json()['lines'] == []
    assert Client().get('/api/v1/attendance-tools/uploads', **bearer(owner)).json()['uploads'] == []


def test_reversing_looks_the_penalty_up_in_the_active_company_first(world, beta):
    url = f"/api/v1/attendance-tools/penalties/{beta['penalty']}/reverse"
    body = {'reason': 'entered by mistake'}
    assert world.signed_in(world.admin_a).post(url, body, content_type='application/json').status_code == 404
    r = world.signed_in(world.admin_b).post(url, body, content_type='application/json')
    assert r.status_code == 200, r.content

