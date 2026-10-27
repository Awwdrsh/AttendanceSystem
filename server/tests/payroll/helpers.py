"""Helpers for the ported TS payroll tests: supertest-style JSON calls with a Bearer token, the demo master data
(bank, PAN, SSF ids) and attendance rows (attendanceFor)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from django.db import transaction
from django.test import Client

from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one, rows

P = '/api/v1/payroll'


class Res:
    def __init__(self, r):
        self.raw = r
        self.status = r.status_code
        self.headers = r.headers
        self.text = r.content.decode('utf-8', 'replace')
        ct = r.headers.get('Content-Type', '')
        try:
            self.body = json.loads(r.content) if 'json' in ct else {}
        except ValueError:
            self.body = {}

    def __repr__(self):
        return f'<{self.status} {self.text[:300]}>'


def _h(tok):
    return {'HTTP_AUTHORIZATION': f'Bearer {tok}'} if tok else {}


def get(tok, path, query=None) -> Res:
    return Res(Client().get(P + path, query or {}, **_h(tok)))


def post(tok, path, body=None) -> Res:
    return Res(Client().post(P + path, json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok)))


def post_text(tok, path, text, content_type='text/csv') -> Res:
    return Res(Client().post(P + path, text, content_type=content_type, **_h(tok)))


def put(tok, path, body=None) -> Res:
    return Res(Client().put(P + path, json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok)))


def patch(tok, path, body=None) -> Res:
    return Res(Client().patch(P + path, json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok)))


def delete(tok, path) -> Res:
    return Res(Client().delete(P + path, **_h(tok)))


def master_data(demo, pans=('123456789', '123456789'), ssf=('10000000001', '10000000001'), banks=None):
    """The TS beforeAll: bank, PAN and SSF ids on Sita and Ram, joined 1 Jan 2026."""
    c = field_cipher()
    for i, (key, code) in enumerate((('sita', 'E001'), ('ram', 'E002'))):
        id_ = demo[key]
        bank = banks[i] if banks else f'0012345600{code[-2:]}12'
        execute("""UPDATE employee SET pan_enc = %s, pan_bidx = 'x', bank_account_enc = %s, bank_account_last4 = '0012', bank_code = 'NMB',
                     bank_branch = '7', ssf_id = %s, join_date = '2026-01-01' WHERE id = %s""",
                [c.encrypt(pans[i], f'employee:{id_}:pan'), c.encrypt(bank, f'employee:{id_}:bank'), ssf[i], id_])


def attendance_for(employee_id, from_, to, absent=(), locked=True):
    d = date.fromisoformat(from_)
    end = date.fromisoformat(to)
    while d <= end:
        iso = d.isoformat()
        dow = (d.weekday() + 1) % 7  # JS getUTCDay: Sunday 0
        status = 'Absent' if iso in absent else 'WeeklyOff' if dow == 6 else 'Present'
        execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at)
                   VALUES (%s, %s, 1, %s, %s, 'h', %s) ON CONFLICT DO NOTHING""",
                [employee_id, iso, status, 480 if status == 'Present' else 0, datetime.now(timezone.utc) if locked else None])
        d += timedelta(days=1)


def rejects(sql, params=()) -> str:
    """The database refuses the statement; returns the error text."""
    try:
        with transaction.atomic():
            execute(sql, params)
    except Exception as e:  # noqa: BLE001
        return str(e)
    raise AssertionError(f'statement was not refused: {sql}')


def set_policy(company_id, **values):
    execute("UPDATE company_setting SET value = value || %s::jsonb WHERE company_id = %s AND key = 'payroll.policy'",
            [json.dumps(values), company_id])


def line(lines, code):
    return next((ln['amount'] for ln in lines if ln['code'] == code), None)


__all__ = ['P', 'Res', 'get', 'post', 'post_text', 'put', 'patch', 'delete', 'master_data', 'attendance_for', 'rejects', 'set_policy',
           'line', 'one', 'rows', 'execute']
