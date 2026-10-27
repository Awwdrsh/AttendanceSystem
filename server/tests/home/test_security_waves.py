"""Ports of apps/api/test/security-wave1.test.ts and security-wave2.test.ts."""
import base64
import json

import pytest
from django.test import Client

from hajiri.core.db import execute, one
from hajiri.core.errors import BadRequestError, HttpError
from tests.web.factories import bearer, htmx

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n%%EOF').decode()


def page(who) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = who.token
    return c


def send(who, path, body):
    return Client().post(path, data=json.dumps(body), content_type='application/json', **bearer(who.token))


# ---------------------------------------------------------------- wave 1

def test_kiosk_ticket_remembers_a_qr_badge_and_cannot_be_forged(demo):
    from hajiri.kiosk.tokens import issue_ticket, verify_ticket
    kiosk = '00000000-0000-7000-8000-0000000000aa'
    qr = issue_ticket(kiosk, demo['ram'], via_qr=True)
    pin = issue_ticket(kiosk, demo['ram'], via_qr=False)
    assert verify_ticket(qr, kiosk)['viaQr'] is True
    assert verify_ticket(pin, kiosk)['viaQr'] is False
    parts = qr.split('.')
    parts[3] = 'P' + parts[3][1:]
    assert verify_ticket('.'.join(parts), kiosk) is None


def test_colleagues_do_not_see_grades(demo, login_as):
    from hajiri.people.service import card, directory
    execute("UPDATE employee SET grade = 'G7' WHERE id = %s", [demo['ram']])
    emp = login_as(['employee'], demo['sita'])
    assert 'G7' not in json.dumps(directory(emp.user), default=str)
    assert 'G7' not in json.dumps(card(emp.user, demo['ram']), default=str)
    # The pages built on them neither.
    assert b'G7' not in page(emp).get('/people').content
    assert b'G7' not in page(emp).get(f"/_/people/{demo['ram']}/card", **htmx('hj-modal-body')).content


def test_one_person_cannot_park_unlimited_uploads(demo, login_as):
    from hajiri.files.service import UPLOADS_PER_HOUR, upload
    u = login_as(['employee'], demo['ram'])
    execute("""INSERT INTO stored_file (company_id, purpose, filename, mime, bytes, sha256, uploaded_by)
               SELECT %s, 'expense_receipt', 'x.pdf', 'application/pdf', 10, 'x', %s FROM generate_series(1, %s)""",
            [demo['company_id'], u.user_id, UPLOADS_PER_HOUR])
    with pytest.raises(HttpError) as e:
        upload(u.user, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF})
    assert e.value.status == 429
    # The browser upload endpoint answers the same.
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = page(u).post('/_/files/', {'purpose': 'expense_receipt', 'file': SimpleUploadedFile('b.pdf', b'%PDF-1.4\n%%EOF', 'application/pdf')},
                     **htmx())
    assert r.status_code == 429


# ---------------------------------------------------------------- wave 2

def test_attendance_admin_cannot_add_punches_to_their_own_attendance(demo, login_as):
    me = login_as(['attendance_admin'], demo['ram'])
    body = {'localDate': '2026-10-01', 'time': '09:00', 'direction': 'in', 'reason': 'forgot to punch'}
    assert send(me, '/api/v1/punch/manual', {**body, 'employeeId': demo['ram']}).status_code == 403
    assert send(me, '/api/v1/punch/manual', {**body, 'employeeId': demo['sita']}).status_code < 300


def test_report_builder_refuses_built_in_object_names(demo):
    from hajiri.reports.adhoc import build_adhoc_query
    with pytest.raises(BadRequestError, match='unknown dataset'):
        build_adhoc_query({'dataset': 'constructor', 'columns': ['x']}, demo['company_id'], '2026-10-16', 10)
    with pytest.raises(BadRequestError, match='unknown column'):
        build_adhoc_query({'dataset': 'employees', 'columns': ['__proto__']}, demo['company_id'], '2026-10-16', 10)
