"""JSON helpers for the ported TS API tests (supertest's request(app).<method>(url).set(auth(token)).send(body))."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from django.test import Client

from engines.attendance import local_date_of

from tests.core.fixtures import bearer

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b'a-tiny-selfie'
SELFIE = 'data:image/jpeg;base64,' + base64.b64encode(JPEG).decode()
HOME = {'lat': 27.80, 'lng': 85.40, 'accuracyM': 30, 'source': 'web'}
OFFICE = {'lat': 27.7172, 'lng': 85.3240, 'accuracyM': 20, 'source': 'web'}


class Res:
    def __init__(self, r):
        self.raw = r
        self.status = r.status_code
        self.headers = r.headers
        ct = r.headers.get('Content-Type', '')
        self.content = r.content
        self.text = r.content.decode('utf-8', 'replace') if 'image' not in ct else ''
        try:
            self.body = json.loads(r.content) if 'json' in ct else {}
        except ValueError:
            self.body = {}


def _h(tok):
    return bearer(tok) if tok else {}


def get(tok, url, query=None, **headers) -> Res:
    return Res(Client().get(f'/api/v1{url}', query or {}, **_h(tok), **headers))


def post(tok, url, body=None, **headers) -> Res:
    return Res(Client().post(f'/api/v1{url}', json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok), **headers))


def put(tok, url, body=None) -> Res:
    return Res(Client().put(f'/api/v1{url}', json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok)))


def patch(tok, url, body=None) -> Res:
    return Res(Client().patch(f'/api/v1{url}', json.dumps(body if body is not None else {}), content_type='application/json', **_h(tok)))


def delete(tok, url) -> Res:
    return Res(Client().delete(f'/api/v1{url}', **_h(tok)))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{datetime.now(timezone.utc).microsecond // 1000:03d}Z'


def today() -> str:
    return local_date_of(datetime.now(timezone.utc).timestamp() * 1000)


def add_days(d: str, n: int) -> str:
    from datetime import date
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


_n = [0]


def punch(tok, body, prefix='key'):
    _n[0] += 1
    return post(tok, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': f'{prefix}-{_n[0]}-key', **body})
