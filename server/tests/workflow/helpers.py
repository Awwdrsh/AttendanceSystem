"""Shared helpers for the workflow tests (the TS tests filed leave through the leave API; the leave slice is ported
separately, so these insert the request row and start its approval the same way leave does)."""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import date

import pytest
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models.functions import Now
from django.test import Client

from hajiri.db.models import Employee, EmployeeAssignment, LeaveRequest, LeaveType, OrgUnit
from hajiri.workflow import service

_n = itertools.count(1)


@pytest.fixture
def leave_handler(db):
    """A decision handler for leave_request that sets the status, unless the leave slice registered its own."""
    had = service.handler_for('leave_request')
    if had is None:
        def apply(object_id, state, ctx):
            LeaveRequest.objects.filter(pk=object_id).update(status=state, decided_at=Now())
        service.register_handler('leave_request', apply)
    yield
    if had is None:
        service.unregister_handler('leave_request')


def leave_type(company_id: str, code: str) -> str:
    return str(LeaveType.objects.for_company(company_id).values_list('id', flat=True).get(code=code))


def file_leave(company_id: str, employee_id: str, day: str, code: str = 'LWP', reason: str | None = None) -> dict:
    """{'requestId', 'instanceId'} for a one-day leave request waiting for approval."""
    lt = leave_type(company_id, code)
    lr = LeaveRequest.objects.create(employee_id=employee_id, leave_type_id=lt, from_date=day, to_date=day, days_requested=1,
                                     reason=reason, status=LeaveRequest.Status.PENDING)
    inst = service.start_workflow(company_id=company_id, object_type='leave_request', object_id=str(lr.pk),
                                  employee_id=employee_id, title='Leave request', body='Leave waiting for approval')
    LeaveRequest.objects.filter(pk=lr.pk).update(workflow_instance_id=inst['id'])
    return {'requestId': str(lr.pk), 'instanceId': inst['id']}


def new_object_id() -> str:
    return str(uuid.uuid4())


def emp(company_id: str, *, org_unit=None, branch=None, manager=None, designation=None, code=None, name=None) -> str:
    n = next(_n)
    e = Employee.objects.create(company_id=company_id, emp_code=code or f'RF{n:03d}', name_en=name or f'Person {n}',
                                join_date=date(2025, 1, 1), designation_ref_id=designation)
    EmployeeAssignment.objects.create(employee=e, valid=DateRange(date(2025, 1, 1), None), org_unit_id=org_unit,
                                      branch_id=branch, manager_id=manager,
                                      employment_type=EmployeeAssignment.EmploymentType.REGULAR)
    return str(e.pk)


def unit(company_id: str, code: str, parent=None, head=None) -> str:
    return str(OrgUnit.objects.create(company_id=company_id, kind=OrgUnit.Kind.DEPARTMENT, code=code, name_en=code,
                                      parent_id=parent, head_employee_id=head).pk)


def api(token: str):
    """A JSON client for Bearer requests: api(tok).get(path), .post(path, body)."""
    c = Client()
    h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    class _C:
        def get(self, path, data=None):
            return c.get(path, data or {}, **h)

        def post(self, path, body=None):
            return c.post(path, json.dumps(body or {}), content_type='application/json', **h)
    return _C()
