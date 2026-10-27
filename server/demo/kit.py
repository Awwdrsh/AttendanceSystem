"""Tools for the demo-suite seeder (port of apps/api/src/db/demo-suite/kit.ts).

The TS seeder drove the real JSON API in-process. The Django app has no JSON API for these modules, so the seeder calls
the same service functions the pages call, as a signed-in user: every record still goes through the same validation,
permissions, workflows, audit entries and notifications as a person clicking through the app. Each call runs in its own
transaction (savepoint), as a request did, so a refused call leaves nothing behind.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from django.db import OperationalError, transaction

from hajiri.core.auth import AuthUser, resolve_session
from hajiri.core.clock import NPT
from hajiri.core.crypto import random_token, sha256_hex
from hajiri.core.db import execute, one
from hajiri.core.errors import HttpError


@dataclass
class Actor:
    user_id: str
    employee_id: str | None
    name: str
    token: str
    user: AuthUser


class ApiError(Exception):
    """A refused service call: `<route> → <status> <message>`, as the TS seeder reported a refused request."""

    def __init__(self, status: int, body, route: str):
        self.status, self.body, self.route = status, body, route
        import json
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        super().__init__(f'{route} → {status} {text}')


class Kit:
    def __init__(self):
        self.tokens: list[str] = []

    def start(self) -> None:
        self.tokens = []

    def actor(self, user_id: str) -> Actor:
        """A one-hour session for a user; revoked again by stop()."""
        u = one('SELECT id, employee_id, name FROM app_user WHERE id = %s', [user_id])
        if not u:
            raise RuntimeError(f'user {user_id} not found')
        token = random_token()
        execute("""INSERT INTO session (token_hash, user_id, expires_at, user_agent)
                   VALUES (%s, %s, now() + interval '1 hour', 'demo-suite seeder')""", [sha256_hex(token), user_id])
        self.tokens.append(token)
        user = resolve_session(token)
        if user is None:
            raise RuntimeError(f'user {user_id} cannot sign in (inactive, or left the company)')
        return Actor(str(u['id']), str(u['employee_id']) if u['employee_id'] else None, u['name'], token, user)

    def call(self, route: str, fn, *args, **kwargs):
        """Run one service call as its own unit of work. A refusal becomes ApiError(route, status, message); a busy
        database (deadlock, lock table full) is retried like the TS seeder retried a 500."""
        attempt = 0
        while True:
            attempt += 1
            try:
                with transaction.atomic():
                    return fn(*args, **kwargs)
            except HttpError as e:
                details = getattr(e, 'details', None)
                raise ApiError(e.status, {'error': e.message, 'details': details} if details else e.message, route) from e
            except OperationalError:
                if attempt >= 4 or not transaction.get_autocommit():
                    raise
                import time
                time.sleep(0.5 * 3 ** attempt)

    def upload_pdf(self, who: Actor, purpose: str, filename: str, title: str, lines: list[str] | None = None) -> str:
        """Upload a generated one-page PDF and return the stored file id."""
        from hajiri.files.service import upload
        pdf = make_pdf(title, lines or [])
        r = self.call('POST /files', upload, who.user,
                      {'purpose': purpose, 'filename': filename, 'dataUrl': f'data:application/pdf;base64,{base64.b64encode(pdf).decode()}'})
        return str(r['id'])

    def stop(self) -> None:
        for t in self.tokens:
            execute('UPDATE session SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL', [sha256_hex(t)])
        self.tokens = []


def make_pdf(title: str, lines: list[str] | None = None) -> bytes:
    """A small, valid one-page PDF with a title and a few lines of Helvetica text."""
    import re

    def esc(s: str) -> str:
        return re.sub(r'([\\()])', r'\\\1', re.sub(r'[^\x20-\x7e]', '?', s))
    body = '\n'.join([f'BT /F1 20 Tf 72 760 Td ({esc(title)}) Tj ET',
                      *[f'BT /F1 11 Tf 72 {724 - i * 18} Td ({esc(l)}) Tj ET' for i, l in enumerate(lines or [])]])
    objs = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        f'<< /Length {len(body.encode("latin1"))} >>\nstream\n{body}\nendstream',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = '%PDF-1.4\n'
    offsets = []
    for i, o in enumerate(objs):
        offsets.append(len(out.encode('latin1')))
        out += f'{i + 1} 0 obj\n{o}\nendobj\n'
    xref = len(out.encode('latin1'))
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n' + ''.join(f'{str(o).zfill(10)} 00000 n \n' for o in offsets)
    out += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'
    return out.encode('latin1')


def pick(items, i: int):
    """Deterministic pick so a re-run on a fresh copy produces the same demo."""
    return items[i % len(items)]


def _d(iso: str | date) -> date:
    return iso if isinstance(iso, date) else date.fromisoformat(iso)


def add_days_iso(iso: str | date, n: int) -> str:
    return (_d(iso) + timedelta(days=n)).isoformat()


def week_start(iso: str | date) -> str:
    """The Sunday on or before a date (Hajiri weeks run Sunday to Saturday)."""
    d = _d(iso)
    return add_days_iso(d, -((d.weekday() + 1) % 7))


def js_day(iso: str | date) -> int:
    """JavaScript getUTCDay(): Sunday 0 ... Saturday 6."""
    return (_d(iso).weekday() + 1) % 7


def npt_instant(iso: str | date, hhmm: str) -> datetime:
    """An instant for a Nepal-time date and HH:MM."""
    h, m = (int(x) for x in hhmm.split(':'))
    d = _d(iso)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=NPT)


def iso_z(d: datetime) -> str:
    """Date.prototype.toISOString(): UTC with milliseconds and a Z."""
    u = d.astimezone(timezone.utc)
    return u.strftime('%Y-%m-%dT%H:%M:%S.') + f'{u.microsecond // 1000:03d}Z'


def base36(n: int) -> str:
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out
