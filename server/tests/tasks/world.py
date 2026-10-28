"""The TS test's setup (tasks.test.ts beforeAll) and its steps, so each pytest case can replay the ones before it."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, timedelta

from hajiri.core.clock import npt_today
from hajiri.db.models import EmployeeAssignment
from hajiri.core.errors import HttpError
from hajiri.files import service as files
from hajiri.tasks import services as tk
from tests.web.factories import make_company, make_employee, make_user

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def plus(n: int) -> str:
    return (npt_today() + timedelta(days=n)).isoformat()


def today() -> str:
    return npt_today().isoformat()


def status(fn, *a, ok: int = 200, **kw) -> int:
    """The HTTP status the TS route would answer."""
    try:
        fn(*a, **kw)
    except HttpError as e:
        return e.status
    return ok


def upload(user, filename: str = 'count.pdf') -> str:
    return files.upload(user, {'purpose': 'task_attachment', 'filename': filename, 'dataUrl': PDF})['id']


@dataclass
class World:
    co: object
    hr: object
    sita: object
    ram: object
    other: object
    sita_e: object
    ram_e: object
    board_id: str = ''
    task_id: str = ''


def build() -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    ram_e = make_employee(co, name='Ram Bahadur')
    sita_e = make_employee(co, name='Sita Sharma')
    gita_e = make_employee(co, code='E004', name='Gita Rai', since=date(2026, 1, 1))
    w = World(co=co, hr=make_user(co, roles=['hr'], employee=make_employee(co, name='Hari HR')),
              sita=make_user(co, roles=['employee'], employee=sita_e),
              ram=make_user(co, roles=['manager'], employee=ram_e),
              other=make_user(co, roles=['employee'], employee=gita_e), sita_e=sita_e, ram_e=ram_e)
    EmployeeAssignment.objects.filter(employee=sita_e).update(manager=ram_e)
    return w


def team_board(w: World) -> World:
    """'a manager sets up a board with their reports' and 'tasks can be assigned to members only'."""
    w.board_id = tk.create_board(w.ram, {'name': 'Ops', 'addMyReports': True})['id']
    w.task_id = tk.create_task(w.ram, w.board_id, {'title': 'Count stock', 'assigneeIds': [w.sita.id], 'dueDate': today()})['id']
    return w
