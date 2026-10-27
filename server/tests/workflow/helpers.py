"""Shared helpers for the workflow tests (the TS tests filed leave through the leave API; the leave slice is ported
separately, so these insert the request row and start its approval the same way leave does)."""
from __future__ import annotations

import itertools
import json

import pytest
from django.test import Client

from hajiri.core.db import execute, one
from hajiri.workflow import service

_n = itertools.count(1)


@pytest.fixture
def leave_handler(db):
    """A decision handler for leave_request that sets the status, unless the leave slice registered its own."""
    had = service.handler_for('leave_request')
    if had is None:
        def apply(object_id, state, ctx):
            execute('UPDATE leave_request SET status = %s, decided_at = now() WHERE id = %s', [state, object_id])
        service.register_handler('leave_request', apply)
    yield
    if had is None:
        service.unregister_handler('leave_request')


def leave_type(company_id: str, code: str) -> str:
    return str(one('SELECT id FROM leave_type WHERE company_id = %s AND code = %s', [company_id, code])['id'])


def file_leave(company_id: str, employee_id: str, day: str, code: str = 'LWP', reason: str | None = None) -> dict:
    """{'requestId', 'instanceId'} for a one-day leave request waiting for approval."""
    lt = leave_type(company_id, code)
    lr = one("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, reason, status)
                VALUES (%s, %s, %s, %s, 1, %s, 'pending') RETURNING id""", [employee_id, lt, day, day, reason])
    inst = service.start_workflow(company_id=company_id, object_type='leave_request', object_id=str(lr['id']),
                                  employee_id=employee_id, title='Leave request', body='Leave waiting for approval')
    execute('UPDATE leave_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], lr['id']])
    return {'requestId': str(lr['id']), 'instanceId': inst['id']}


def new_object_id() -> str:
    return str(one('SELECT uuidv7() AS id')['id'])


def emp(company_id: str, *, org_unit=None, branch=None, manager=None, designation=None, code=None, name=None) -> str:
    n = next(_n)
    r = one("""INSERT INTO employee (company_id, emp_code, name_en, join_date, designation_id)
               VALUES (%s, %s, %s, '2025-01-01', %s) RETURNING id""",
            [company_id, code or f'RF{n:03d}', name or f'Person {n}', designation])
    execute("""INSERT INTO employee_assignment (employee_id, valid, org_unit_id, branch_id, manager_id, employment_type)
               VALUES (%s, daterange('2025-01-01', NULL), %s, %s, %s, 'regular')""", [r['id'], org_unit, branch, manager])
    return str(r['id'])


def unit(company_id: str, code: str, parent=None, head=None) -> str:
    return str(one("""INSERT INTO org_unit (company_id, kind, code, name_en, parent_id, head_employee_id)
                      VALUES (%s, 'department', %s, %s, %s, %s) RETURNING id""", [company_id, code, code, parent, head])['id'])


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
