"""The TS test's setup (documents.test.ts beforeAll) and its steps, so each pytest case can replay the ones before it."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date, timedelta

from tests.support.sql import execute, one
from hajiri.core.errors import HttpError
from hajiri.documents import services as docs
from hajiri.files import service as files
from tests.web.factories import make_company, make_employee, make_user

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def status(fn, *a, ok: int = 200, **kw) -> int:
    """The HTTP status the TS route would answer."""
    try:
        fn(*a, **kw)
    except HttpError as e:
        return e.status
    return ok


def add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def upload(user, purpose: str = 'employee_document') -> str:
    return files.upload(user, {'purpose': purpose, 'filename': 'doc.pdf', 'dataUrl': PDF})['id']


def outbox(template: str, channel: str | None = None, employee_id: str | None = None) -> int:
    sql = 'SELECT count(*)::int AS n FROM notification_outbox WHERE template = %s'
    params: list = [template]
    if channel:
        sql += ' AND channel = %s'
        params.append(channel)
    if employee_id:
        sql += ' AND employee_id = %s'
        params.append(employee_id)
    return one(sql, params)['n']


@dataclass
class World:
    co: object
    branch: str
    hr: object
    hr2: object
    sita: object
    ram: object
    peer: object
    foreign_hr: object
    sita_id: str
    ram_id: str
    peer_id: str
    ids: dict = field(default_factory=dict)

    def category(self, user, code: str) -> str:
        return next(c['id'] for c in docs.list_categories(user) if c['code'] == code)

    def template(self, code: str) -> str:
        return next(t['id'] for t in docs.list_templates(self.hr) if t['code'] == code)


def build() -> World:
    co = make_company('DEMO', 'Demo Pvt. Ltd.')
    branch = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'KTM', 'Kathmandu') RETURNING id", [co.id])['id'])
    ram_e = make_employee(co, code='E001', name='Ram Thapa', branch=branch, gender='male')
    sita_e = make_employee(co, code='E002', name='Sita Sharma', branch=branch, manager=ram_e, gender='female',
                           designation='Software Engineer', join_date=date(2024, 4, 14), email='sita@demo.test')
    make_employee(co, code='E003', name='Hari Bahadur', branch=branch)   # in every audience, has no sign-in
    peer_e = make_employee(co, code='E099', name='Peer Person', branch=branch)
    other = make_company('OTHER', 'Other Co')
    return World(co=co, branch=branch, hr=make_user(co, roles=['hr']), hr2=make_user(co, roles=['hr']),
                 sita=make_user(co, roles=['employee'], employee=sita_e),
                 ram=make_user(co, roles=['employee', 'manager'], employee=ram_e),
                 peer=make_user(co, roles=['employee'], employee=peer_e),
                 foreign_hr=make_user(other, roles=['hr'], email='hr@other.docs'),
                 sita_id=str(sita_e.id), ram_id=str(ram_e.id), peer_id=str(peer_e.id))


# ---------------------------------------------------------------- steps of the employee documents block

def employee_uploads(w: World) -> str:
    r = docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'citizenship'), 'title': 'Citizenship',
                                              'fileId': upload(w.sita)})
    w.ids['doc'] = r['id']
    return r['id']


def hr_verifies(w: World) -> None:
    docs.verify_document(w.hr, w.ids['doc'], {'decision': 'verify'})


def employee_replaces_and_deletes(w: World) -> str:
    r = docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'citizenship'), 'title': 'Citizenship',
                                              'fileId': upload(w.sita), 'replacesId': w.ids['doc']})
    docs.delete_document(w.sita, r['id'])
    return r['id']


# ---------------------------------------------------------------- steps of the letters block

def welcome_template(w: World) -> str:
    return docs.create_template(w.hr, {'code': 'welcome', 'name': 'Welcome note', 'subject': 'Welcome {{employee.first_name}}',
                                       'body': 'Welcome to {{company.name}}.'})['id']


def signatory(w: World) -> None:
    docs.save_letter_settings(w.hr, {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager', 'refPrefix': 'CV/HR'})


def set_manager(w: World) -> None:
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [w.ram_id, w.sita_id])


__all__ = ['World', 'build', 'status', 'upload', 'outbox', 'add_days', 'employee_uploads', 'hr_verifies',
           'employee_replaces_and_deletes', 'welcome_template', 'signatory', 'set_manager', 'PDF']
