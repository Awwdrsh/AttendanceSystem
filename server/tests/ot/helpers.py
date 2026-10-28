"""Helpers for the overtime tests."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from django.test import Client

from hajiri.core.clock import npt_today
from hajiri.db.models import LeaveType, OtSlip


class Api:
    """A JSON client for Bearer requests: api.get(path, query), api.post(path, body)."""

    def __init__(self, token: str):
        self.token = token
        self.c = Client()
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get(self, path, query=None):
        return self.c.get(path, query or {}, **self.h)

    def post(self, path, body=None):
        return self.c.post(path, json.dumps(body or {}), content_type='application/json', **self.h)


@dataclass
class World:
    demo: dict
    hr: Api
    sita: Api
    ram: Api
    hr_login: object
    sita_login: object
    ram_login: object
    login_as: object

    @property
    def company(self) -> str:
        return self.demo['company_id']


def days_ago(n: int) -> str:
    return (npt_today() - timedelta(days=n)).isoformat()


def slip(employee_id: str, day: str, minutes: int, day_type: str = 'working') -> str:
    return str(OtSlip.objects.create(employee_id=employee_id, local_date=day, detected_min=minutes, day_type=day_type).id)


def sub_type(company_id: str) -> str:
    return str(LeaveType.objects.for_company(company_id).values_list('id', flat=True).get(code='SUBSTITUTE'))


def browser(token: str) -> Client:
    """A browser-like client signed in with the session cookie."""
    from hajiri.core.auth import SESSION_COOKIE
    c = Client()
    c.cookies[SESSION_COOKIE] = token
    return c
