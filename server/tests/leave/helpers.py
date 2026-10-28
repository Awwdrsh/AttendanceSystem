"""Shared set-up for the leave tests: the TS setup-db freshDb (company DEMO, Sita and Ram) with HR, Sita and Ram signed
in, Sita reporting to Ram, and the leave type ids by code."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from django.test import Client

from tests.support.sql import execute, one


class Api:
    """A JSON client for Bearer requests: api.get(path, query), .post(path, body), .patch(path, body)."""

    def __init__(self, token: str):
        self.c = Client()
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get(self, path, query=None):
        return self.c.get(path, query or {}, **self.h)

    def post(self, path, body=None):
        return self.c.post(path, json.dumps(body or {}), content_type='application/json', **self.h)

    def patch(self, path, body=None):
        return self.c.patch(path, json.dumps(body or {}), content_type='application/json', **self.h)


@dataclass
class World:
    demo: dict
    hr: Api
    sita: Api
    ram: Api
    ram_user_id: str
    types: dict
    login_as: object

    @property
    def company(self) -> str:
        return self.demo['company_id']

    def t(self, code: str) -> str:
        return self.types[code]


def type_id(company_id: str, code: str) -> str:
    return str(one('SELECT id FROM leave_type WHERE company_id = %s AND code = %s', [company_id, code])['id'])


def instance_of(request_id: str) -> str:
    return str(one('SELECT workflow_instance_id FROM leave_request WHERE id = %s', [request_id])['workflow_instance_id'])


def attendance(employee_id: str, day: str, status: str = 'Present', locked: bool = False, worked_min: int | None = None) -> None:
    execute(f"""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash{', worked_min' if worked_min is not None else ''}{', locked_at' if locked else ''})
                VALUES (%s, %s, 1, %s, 'h'{', %s' if worked_min is not None else ''}{', now()' if locked else ''})""",
            [employee_id, day, status] + ([worked_min] if worked_min is not None else []))


@pytest.fixture
def world(demo, login_as):
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['manager'], demo['ram'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    codes = ('HOME', 'SICK', 'CASUAL', 'LWP', 'SUBSTITUTE', 'MATERNITY', 'PATERNITY', 'MOURNING', 'SPECIAL_UNPAID')
    return World(demo, Api(hr.token), Api(sita.token), Api(ram.token), ram.user_id,
                 {c: type_id(demo['company_id'], c) for c in codes}, login_as)
