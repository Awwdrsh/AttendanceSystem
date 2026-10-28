"""
The TS people/lifecycle tests' world: the DEMO company with Sita (reports to Ram), Ram (employee + manager), a peer,
two HR users, and HR of another company.
"""
import base64
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from hajiri.core.auth import AuthUser
from hajiri.core.clock import npt_today
from tests.support.sql import execute, one
from tests.web.factories import make_company, make_employee, make_user
from tests.web.fixtures import *  # noqa: F401,F403

PNG = 'data:image/png;base64,' + base64.b64encode(bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) + b'tiny-avatar').decode()
PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n%%EOF').decode()


def add_days(d, n: int):
    d = d if isinstance(d, date) else date.fromisoformat(d)
    return (d + timedelta(days=n)).isoformat()


def upload(user: AuthUser, purpose: str, data_url: str, filename: str = 'f') -> str:
    from hajiri.files.service import upload as up
    return up(user, {'purpose': purpose, 'filename': filename, 'dataUrl': data_url})['id']


@dataclass
class World:
    company: object
    other: object
    branch_id: str
    sita_id: str
    ram_id: str
    peer_id: str
    hr: AuthUser
    hr2: AuthUser
    sita: AuthUser
    ram: AuthUser
    peer: AuthUser
    foreign_hr: AuthUser


def build_world(join_days_ago: int | None = None) -> World:
    from hajiri.company_settings.seed import seed_defaults
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    seed_defaults(str(co.id))   # setup-db.ts seedDefaults: approval chains (profile change -> HR, resignation -> manager, HR)
    br = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'KTM', 'Kathmandu') RETURNING id", [co.id])['id'])
    ram = make_employee(co, code='E002', name='Ram Bahadur', branch=br)
    sita = make_employee(co, code='E001', name='Sita Sharma', branch=br, manager=ram)
    peer = make_employee(co, code='E077', name='Peer Person', branch=br, since=date(2025, 1, 1))
    join = date(2024, 1, 1) if join_days_ago is None else npt_today() - timedelta(days=join_days_ago)
    execute('UPDATE employee SET join_date = %s WHERE id IN (%s, %s)', [join, sita.id, ram.id])
    execute("UPDATE employee SET join_date = '2025-01-01' WHERE id = %s", [peer.id])
    other = make_company('OTHERP', 'Other Co')
    return World(
        company=co, other=other, branch_id=br, sita_id=str(sita.id), ram_id=str(ram.id), peer_id=str(peer.id),
        hr=make_user(co, roles=['hr']), hr2=make_user(co, roles=['hr']), sita=make_user(co, roles=['employee'], employee=sita),
        ram=make_user(co, roles=['employee', 'manager'], employee=ram), peer=make_user(co, roles=['employee'], employee=peer),
        foreign_hr=make_user(other, roles=['hr'], email='hr@other.people'),
    )


@pytest.fixture
def world(db):
    return build_world()
