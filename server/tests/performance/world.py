"""The TS performance.test.ts set-up: Sita reports to Ram; Hari and Gita report to Ram; HR without an employee; another company."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from hajiri.core.db import execute, one
from tests.web.factories import make_company, make_employee, make_user


def today() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=345)).date().isoformat()


def plus_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=345) + timedelta(days=n)).date().isoformat()


def outbox(template: str) -> int:
    return one('SELECT count(*)::int AS n FROM notification_outbox WHERE template = %s', [template])['n']


@dataclass
class World:
    co: object
    other: object
    SITA: str
    RAM: str
    HARI: str
    GITA: str
    OTHER_EMP: str
    hr: object
    sita: object
    ram: object
    hari: object
    gita: object
    other_hr: object
    sita_emp: object
    ram_emp: object


def build() -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    ram_e = make_employee(co, name='Ram Thapa', designation='Engineer')
    sita_e = make_employee(co, name='Sita Sharma', designation='Engineer', manager=ram_e)
    hari_e = make_employee(co, code='E101', name='Hari Karki', designation='Engineer', manager=ram_e)
    gita_e = make_employee(co, code='E102', name='Gita Rai', designation='Engineer', manager=ram_e)
    other = make_company('OTHERP', 'Other Co')
    other_e = make_employee(other, code='X1', name='Foreign', designation='Engineer')
    return World(
        co=co, other=other, SITA=str(sita_e.id), RAM=str(ram_e.id), HARI=str(hari_e.id), GITA=str(gita_e.id), OTHER_EMP=str(other_e.id),
        hr=make_user(co, roles=['hr']), sita=make_user(co, roles=['employee'], employee=sita_e),
        ram=make_user(co, roles=['manager'], employee=ram_e), hari=make_user(co, roles=['employee'], employee=hari_e),
        gita=make_user(co, roles=['employee'], employee=gita_e), other_hr=make_user(other, roles=['hr']),
        sita_emp=sita_e, ram_emp=ram_e)


_ = execute
