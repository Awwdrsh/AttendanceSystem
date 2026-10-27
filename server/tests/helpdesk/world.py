"""The TS test's setup (helpdesk.test.ts beforeAll) and its steps, so each pytest case can replay the ones before it."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

from hajiri.core.db import execute
from hajiri.core.errors import HttpError
from hajiri.files import service as files
from hajiri.helpdesk import services as hd
from tests.web.factories import make_company, make_employee, make_user

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def status(fn, *a, ok: int = 200, **kw) -> int:
    """The HTTP status the TS route would answer."""
    try:
        fn(*a, **kw)
    except HttpError as e:
        return e.status
    return ok


def upload(user) -> str:
    return files.upload(user, {'purpose': 'helpdesk_attachment', 'filename': 'slip.pdf', 'dataUrl': PDF})['id']


@dataclass
class World:
    co: object
    hr: object
    sita: object
    ram: object
    it_: object
    other_hr: object
    cats: list = field(default_factory=list)
    ticket: dict | None = None
    grievance: str | None = None

    def cat(self, prefix: str) -> str:
        return next(c['id'] for c in self.cats if c['name'].startswith(prefix))


def build() -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    ram_e = make_employee(co, name='Ram')
    sita_e = make_employee(co, name='Sita', manager=ram_e)
    w = World(co=co, hr=make_user(co, roles=['hr'], employee=make_employee(co, name='Hari HR')),
              sita=make_user(co, roles=['employee'], employee=sita_e),
              ram=make_user(co, roles=['employee', 'manager'], employee=ram_e),
              it_=make_user(co, roles=['employee'], employee=make_employee(co, name='Ita IT')),
              other_hr=make_user(make_company('OTHERHD', 'Other Co'), roles=['hr'], email='hr@other-hd.local'))
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [ram_e.id, sita_e.id])
    # Grievances have their own committee flow (Employee relations); a confidential help desk topic is still possible.
    hd.create_category(w.hr, {'name': 'Confidential HR matter', 'confidential': True, 'sort': 9})
    w.cats = hd.list_categories(w.sita)
    return w


def raise_tickets(w: World) -> None:
    file_id = upload(w.sita)
    w.ticket = hd.create_ticket(w.sita, {'categoryId': w.cat('Payroll'), 'subject': 'SSF missing on payslip',
                                         'body': 'My Bhadra payslip has no SSF line.', 'priority': 'high', 'fileIds': [file_id]})
    w.ticket2 = hd.create_ticket(w.ram, {'categoryId': w.cat('Leave'), 'subject': 'Balance looks wrong', 'body': 'Please check'})


def internal_note_and_reply(w: World) -> str:
    note_file = upload(w.hr)
    hd.add_message(w.hr, w.ticket['id'], {'body': 'Check the SSF registration date.', 'internal': True, 'fileIds': [note_file]})
    hd.add_message(w.hr, w.ticket['id'], {'body': 'Fixed in the supplementary run.'})
    return note_file


def assign_and_resolve(w: World) -> None:
    hd.update_ticket(w.hr, w.ticket['id'], {'assigneeUserId': w.it_.id})
    hd.update_ticket(w.it_, w.ticket['id'], {'status': 'resolved'})


def raise_grievance(w: World) -> None:
    w.grievance = hd.create_ticket(w.sita, {'categoryId': w.cat('Confidential'), 'subject': 'Concern about my manager',
                                            'body': 'Confidential matter'})['id']
