"""
The TS expenses test world: HR (two), Sita (employee, grade G3, reports to Ram), Ram (manager), a stranger manager and
HR of another company, with the expense defaults seeded and the policy changes the TS 'policy' block made (mileage
NPR 20/km, a 60-day claim window, meal limits for G3 and everyone, TADA rates with a G3 city row).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from hajiri.core.clock import npt_today
from tests.support.sql import execute
from hajiri.expenses import services as ex
from hajiri.expenses.seeds import seed_expense_defaults
from tests.web.factories import make_company, make_employee, make_user, sign_in


PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def today() -> str:
    return npt_today().isoformat()


def days_ago(n: int) -> str:
    return (date.fromisoformat(today()) - timedelta(days=n)).isoformat()


def days_ahead(n: int) -> str:
    return days_ago(-n)


def upload(user, purpose: str = 'expense_receipt') -> str:
    from hajiri.files.service import upload as up
    return up(user, {'purpose': purpose, 'filename': 'bill.pdf', 'dataUrl': PDF})['id']


@dataclass
class World:
    co: object
    other: object
    hr: object
    hr2: object
    sita: object
    ram: object
    stranger: object
    other_hr: object
    sita_emp: object
    ram_emp: object
    cats: dict

    def client(self, user):
        from django.test import Client
        c = Client()
        sign_in(c, user)
        return c


def approve_both(fn, id, w: World, body: dict | None = None):
    """Manager then HR: the two-step chain for claims, trips and settlements."""
    m = fn(w.ram, id, {'decision': 'approve', **(body or {})})
    assert m['state'] == 'pending'
    h = fn(w.hr, id, {'decision': 'approve', **(body or {})})
    assert h['state'] == 'approved'


@pytest.fixture
def world(db) -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    seed_expense_defaults(str(co.id))
    ram_emp = make_employee(co, name='Ram Manager')
    sita_emp = make_employee(co, name='Sita Sharma', manager=ram_emp, grade='G3')
    hr = make_user(co, roles=['hr'], employee=make_employee(co, name='Hari HR'))
    hr2 = make_user(co, roles=['hr'], employee=make_employee(co, name='Gita HR'))
    sita = make_user(co, roles=['employee'], employee=sita_emp)
    ram = make_user(co, roles=['manager'], employee=ram_emp)
    stranger = make_user(co, roles=['manager'], employee=make_employee(co, name='Stranger'))
    other = make_company('OTHER', 'Other Co')
    seed_expense_defaults(str(other.id))
    other_hr = make_user(other, roles=['hr'], email='hr@other.local', name='Other HR')
    cats = {c['code']: c['id'] for c in ex.get_policy(sita)['categories']}
    w = World(co, other, hr, hr2, sita, ram, stranger, other_hr, sita_emp, ram_emp, cats)
    return w


@pytest.fixture
def policy_world(world) -> World:
    """The world after the TS policy block: mileage 20, window 60, meal limits, TADA rates."""
    ex.save_settings(world.hr, {'mileageRatePerKm': '20', 'limitMode': 'block', 'claimWindowDays': 60})
    ex.set_limits(world.hr, world.cats['MEALS'], {'limits': [{'grade': 'G3', 'perClaim': '1500', 'perMonth': '3000'},
                                                             {'grade': '*', 'perClaim': '800'}]})
    ex.set_tada_rates(world.hr, {'rates': [
        {'grade': '*', 'destination': 'valley', 'daRate': '1000', 'lodgingRate': '2500'},
        {'grade': '*', 'destination': 'city', 'daRate': '1500', 'lodgingRate': '3000'},
        {'grade': 'G3', 'destination': 'city', 'daRate': '2000', 'lodgingRate': '3500'},
        {'grade': '*', 'destination': 'international', 'daRate': '50', 'lodgingRate': '100'},
    ]})
    return world


