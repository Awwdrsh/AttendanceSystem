"""
The world of apps/api/test/timesheets.test.ts, rebuilt for each test, and the describe blocks as replayable steps
(the TS suite ran its blocks in order against one database; here a test replays the blocks before its own).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import DatabaseError

from hajiri.core.clock import now as _now
from hajiri.core.errors import HttpError, from_db_error
from hajiri.timesheets import services as ts
from tests.web.factories import make_company, make_employee, make_user


def status(fn, *args, **kwargs) -> int:
    """The HTTP status the TS route would have answered: 200 on success, else the error's status."""
    try:
        fn(*args, **kwargs)
        return 200
    except HttpError as e:
        return e.status
    except DatabaseError as e:
        mapped = from_db_error(e)
        if mapped is None:
            raise
        return mapped.status


def today_npt() -> str:
    return (_now() + timedelta(minutes=345)).date().isoformat()


def add_days(iso: str, n: int) -> str:
    return ts.add_days_iso(iso, n)


@dataclass
class World:
    company: object
    other_company: object
    sita_emp: object
    ram_emp: object
    hari_emp: object
    other_emp: object
    hr: object
    sita: object
    ram: object
    hari: object
    other: object
    outsider: object
    WEEK: str = ''
    MON: str = ''
    TUE: str = ''
    ids: dict = field(default_factory=dict)


def make_world() -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    ram_emp = make_employee(co, code='E002', name='Ram Thapa')
    sita_emp = make_employee(co, code='E001', name='Sita Sharma', manager=ram_emp)
    # Hari works in the same company but not for Ram.
    hari_emp = make_employee(co, code='E099', name='Hari Karki')
    oth = make_company('OTH', 'Other Co')
    other_emp = make_employee(oth, code='X001', name='Outsider')
    week = ts.week_start_of(add_days(today_npt(), -14))   # a past Sunday
    return World(
        company=co, other_company=oth, sita_emp=sita_emp, ram_emp=ram_emp, hari_emp=hari_emp, other_emp=other_emp,
        hr=make_user(co, roles=['hr'], name='HR Person'),
        sita=make_user(co, roles=['employee'], employee=sita_emp),
        ram=make_user(co, roles=['manager'], employee=ram_emp),
        hari=make_user(co, roles=['employee'], employee=hari_emp),
        other=make_user(co, roles=['manager']),        # a manager with no employee record and no team
        outsider=make_user(oth, roles=['hr'], employee=other_emp, name='Other HR'),
        WEEK=week, MON=add_days(week, 1), TUE=add_days(week, 2),
    )
