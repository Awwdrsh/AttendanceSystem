"""
Notifications (port of apps/api/src/modules/notifications). notify() writes to the transactional outbox inside the
caller's transaction: an in-app row is visible at once, email/sms/push/webhook rows are delivered by the worker
(deliver_queued) with retry backoff (5 attempts) and quiet-hour deferral for sms/push (company setting
notifications.policy). SMS bodies never contain amounts.
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Iterable, Protocol
from urllib.parse import unquote, urlparse

from hajiri.core.clock import NPT, now as _now
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import NotFoundError
from hajiri.core.schema import Bool, DateTimeStr, Int, Obj, parse

log = logging.getLogger('hajiri.notifications')

CHANNELS = ('email', 'sms', 'push', 'webhook')


def notify(*, company_id: str | None, template: str, title: str, body: str, user_id: str | None = None,
           employee_id: str | None = None, payload: dict | None = None, channels: Iterable[str] = (),
           to_email: str | None = None) -> None:
    """Always one in_app row; each extra channel is queued for the worker. Call inside the business transaction."""
    for channel in ('in_app', *channels):
        execute("""INSERT INTO notification_outbox (company_id, user_id, employee_id, channel, template, title, body, payload, status, to_email)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [company_id, user_id, employee_id, channel, template, title,
                 strip_amounts(body) if channel == 'sms' else body, json.dumps(payload or {}, default=str),
                 'sent' if channel == 'in_app' else 'queued', to_email if channel != 'in_app' else None])


def user_id_for_employee(employee_id: str) -> str | None:
    r = one("SELECT id FROM app_user WHERE employee_id = %s AND status = 'active'", [employee_id])
    return str(r['id']) if r else None


def user_ids_with_role(company_id: str, role: str) -> list[str]:
    """Active users holding a role in a company (admins always included), for HR broadcasts."""
    return [str(r['id']) for r in rows(
        "SELECT id FROM app_user WHERE company_id = %s AND status = 'active' AND (%s = ANY(roles) OR 'admin' = ANY(roles))",
        [company_id, role])]


def notify_role(company_id: str, role: str, *, template: str, title: str, body: str, payload: dict | None = None,
                channels: Iterable[str] = (), employee_id: str | None = None) -> int:
    """One notification per user with the role; returns how many."""
    ids = user_ids_with_role(company_id, role)
    for uid in ids:
        notify(company_id=company_id, user_id=uid, template=template, title=title, body=body, payload=payload,
               channels=tuple(channels), employee_id=employee_id)
    return len(ids)


def strip_amounts(text: str) -> str:
    """Salary figures never travel over SMS: currency amounts and long digit groups become [amount]."""
    text = re.sub(r'(NPR|Rs\.?|रु\.?)\s?[\d,]+(\.\d+)?', '[amount]', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{1,3}(,\d{2,3})+(\.\d+)?\b', '[amount]', text)
    return re.sub(r'\b\d{5,}(\.\d+)?\b', '[amount]', text)


def payslip_published_notification(period_label: str) -> dict:
    """Template, title, body and channels for a published payslip: the period only, never an amount."""
    return {'template': 'payslip.published', 'title': f'Payslip for {period_label} is ready',
            'body': f'Your payslip for {period_label} is ready to view in Hajiri.', 'channels': ('push', 'sms')}


# ---------------------------------------------------------------- in-app inbox

INBOX_QUERY = Obj({'limit': Int(min=1, max=100, default=30, coerce=True), 'before': DateTimeStr(optional=True),
                   'unreadOnly': Bool(coerce=True, optional=True)})


def inbox(user_id: str, *, limit: int = 30, before: str | datetime | None = None, unread_only: bool = False) -> dict:
    """The signed-in user's in-app notifications, newest first, and the unread count."""
    items = rows("""SELECT id, template, title, body, payload, created_at, read_at FROM notification_outbox
                     WHERE user_id = %s AND channel = 'in_app' AND (%s::timestamptz IS NULL OR created_at < %s::timestamptz)
                       AND (NOT %s OR read_at IS NULL)
                     ORDER BY created_at DESC, id DESC LIMIT %s""", [user_id, before, before, unread_only, limit])
    return {'notifications': items, 'unread': unread_count(user_id)}


def inbox_from_query(user_id: str, query: dict) -> dict:
    q = parse(INBOX_QUERY, query)
    return inbox(user_id, limit=q['limit'], before=q.get('before'), unread_only=q.get('unreadOnly', False))


def unread_count(user_id: str) -> int:
    return one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND channel = 'in_app' AND read_at IS NULL",
               [user_id])['n']


def mark_read(user_id: str, notification_id: str) -> dict:
    r = one("""UPDATE notification_outbox SET read_at = coalesce(read_at, now()) WHERE id = %s AND user_id = %s
               RETURNING id, read_at""", [notification_id, user_id])
    if not r:
        raise NotFoundError('notification not found')
    return r


def mark_all_read(user_id: str) -> int:
    return execute("UPDATE notification_outbox SET read_at = now() WHERE user_id = %s AND channel = 'in_app' AND read_at IS NULL",
                   [user_id])


# ---------------------------------------------------------------- delivery (worker)

@dataclass
class OutboxRow:
    id: str
    company_id: str | None
    user_id: str | None
    employee_id: str | None
    channel: str
    template: str
    title: str
    body: str
    payload: dict
    attempts: int
    recipient_email: str | None
    recipient_phone: str | None


class Sender(Protocol):
    channel: str

    def send(self, row: OutboxRow) -> None: ...


class LogSender:
    """Development sender: logs instead of sending."""

    def __init__(self, channel: str):
        self.channel = channel

    def send(self, row: OutboxRow) -> None:
        log.info('notification %s id=%s to=%s title=%s', self.channel, row.id,
                 row.recipient_email or row.recipient_phone, row.title)


class WebhookSender:
    """POSTs JSON {channel, to, title, body, template, payload, id} to an SMS/email/push gateway."""

    def __init__(self, channel: str, url: str, post=None):
        self.channel, self.url = channel, url
        self._post = post or _http_post_json

    def send(self, row: OutboxRow) -> None:
        to = row.recipient_phone if self.channel in ('sms', 'push') else row.recipient_email
        if not to and self.channel != 'webhook':
            raise RuntimeError(f'no recipient for {self.channel}')
        status = self._post(self.url, {'channel': self.channel, 'to': to, 'title': row.title, 'body': row.body,
                                       'template': row.template, 'payload': row.payload, 'id': row.id})
        if not 200 <= status < 300:
            raise RuntimeError(f'{self.channel} gateway responded {status}')


def _http_post_json(url: str, body: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body, default=str).encode(), method='POST',
                                 headers={'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status
    except urllib.error.HTTPError as e:
        return e.code


class SmtpSender:
    """Email over SMTP from SMTP_URL: smtp://user:pass@host:587?from=hr@example.com (smtps:// for implicit TLS;
    plain smtp:// upgrades with STARTTLS when the server offers it)."""
    channel = 'email'

    def __init__(self, url: str, connect=None):
        u = urlparse(url)
        if u.scheme not in ('smtp', 'smtps'):
            raise ValueError('SMTP_URL must start with smtp:// or smtps://')
        self.tls = u.scheme == 'smtps'
        self.host, self.port = u.hostname or 'localhost', u.port or (465 if self.tls else 587)
        self.user, self.password = unquote(u.username or ''), unquote(u.password or '')
        q = dict(p.split('=', 1) for p in u.query.split('&') if '=' in p)
        self.sender = unquote(q.get('from', '')) or self.user or 'hajiri@localhost'
        self._connect = connect

    def _client(self):
        if self._connect:
            return self._connect(self)
        if self.tls:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ssl.create_default_context())
        c = smtplib.SMTP(self.host, self.port, timeout=20)
        c.ehlo()
        if c.has_extn('starttls'):
            c.starttls(context=ssl.create_default_context())
            c.ehlo()
        return c

    def send(self, row: OutboxRow) -> None:
        if not row.recipient_email:
            raise RuntimeError('no recipient for email')
        msg = EmailMessage()
        msg['From'], msg['To'], msg['Subject'] = self.sender, row.recipient_email, row.title
        msg.set_content(row.body)
        c = self._client()
        try:
            if self.user:
                c.login(self.user, self.password)
            c.send_message(msg)
        finally:
            try:
                c.quit()
            except Exception:  # noqa: BLE001 - the message is already sent or failed
                pass


def senders_from_env(env: dict | None = None) -> list:
    """SMTP_URL (or EMAIL_WEBHOOK_URL) for email, SMS_WEBHOOK_URL, PUSH_WEBHOOK_URL, WEBHOOK_URL; unset channels log only."""
    import os
    from django.conf import settings
    e = os.environ if env is None else env
    smtp = e.get('SMTP_URL') or getattr(settings, 'SMTP_URL', '')
    sms = e.get('SMS_WEBHOOK_URL') or getattr(settings, 'SMS_WEBHOOK_URL', '')
    email = SmtpSender(smtp) if smtp else (WebhookSender('email', e['EMAIL_WEBHOOK_URL']) if e.get('EMAIL_WEBHOOK_URL') else LogSender('email'))
    return [
        WebhookSender('sms', sms) if sms else LogSender('sms'),
        email,
        WebhookSender('push', e['PUSH_WEBHOOK_URL']) if e.get('PUSH_WEBHOOK_URL') else LogSender('push'),
        WebhookSender('webhook', e['WEBHOOK_URL']) if e.get('WEBHOOK_URL') else LogSender('webhook'),
    ]


DEFAULT_QUIET = ('21:00', '07:00')
MAX_ATTEMPTS = 5


def _npt_minutes(at: datetime) -> int:
    t = at.astimezone(NPT)
    return t.hour * 60 + t.minute


def _hm(s: str) -> int:
    h, _, m = s.partition(':')
    return int(h or 0) * 60 + int(m or 0)


def in_quiet_hours(at: datetime, quiet: tuple[str, str] = DEFAULT_QUIET) -> bool:
    """True when `at` (Nepal time) is inside the quiet window; handles windows crossing midnight."""
    n, s, e = _npt_minutes(at), _hm(quiet[0]), _hm(quiet[1])
    return s <= n < e if s <= e else (n >= s or n < e)


def quiet_hours_end(at: datetime, quiet: tuple[str, str] = DEFAULT_QUIET) -> datetime:
    """The next instant quiet hours end (Nepal time)."""
    n, e = _npt_minutes(at), _hm(quiet[1])
    ahead = e - n if e > n else 24 * 60 - n + e
    return at + timedelta(minutes=ahead)


def deliver_queued(senders: list, at: datetime | None = None, limit: int = 100) -> dict:
    """Send due outbox rows. A failure retries after attempts² minutes and gives up after 5 tries; sms and push wait
    out quiet hours instead of being dropped."""
    at = at or _now()
    by_channel = {s.channel: s for s in senders}
    due = rows("""
        SELECT n.id, n.company_id, n.user_id, n.employee_id, n.channel, n.template, n.title, n.body, n.payload, n.attempts,
               coalesce(n.to_email, u.email, e.email) AS recipient_email, e.phone AS recipient_phone
          FROM notification_outbox n
          LEFT JOIN app_user u ON u.id = n.user_id
          LEFT JOIN employee e ON e.id = coalesce(n.employee_id, u.employee_id)
         WHERE n.status = 'queued' AND n.channel <> 'in_app' AND n.next_attempt_at <= %s
         ORDER BY n.created_at LIMIT %s""", [at, limit])
    sent = failed = deferred = 0
    quiet_cache: dict[str, tuple[str, str]] = {}
    for r in due:
        row = OutboxRow(**{**r, 'id': str(r['id']), 'company_id': r['company_id'] and str(r['company_id']),
                           'user_id': r['user_id'] and str(r['user_id']), 'employee_id': r['employee_id'] and str(r['employee_id'])})
        if row.channel in ('sms', 'push'):
            key = row.company_id or ''
            if key not in quiet_cache:
                s = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'notifications.policy'", [row.company_id])
                v = (s or {}).get('value') or {}
                quiet_cache[key] = (v.get('quietHoursStart') or DEFAULT_QUIET[0], v.get('quietHoursEnd') or DEFAULT_QUIET[1])
            q = quiet_cache[key]
            if in_quiet_hours(at, q):
                execute('UPDATE notification_outbox SET next_attempt_at = %s WHERE id = %s', [quiet_hours_end(at, q), row.id])
                deferred += 1
                continue
        sender = by_channel.get(row.channel)
        try:
            if not sender:
                raise RuntimeError(f'no sender configured for {row.channel}')
            sender.send(row)
            execute("UPDATE notification_outbox SET status = 'sent', sent_at = %s, attempts = attempts + 1 WHERE id = %s", [at, row.id])
            sent += 1
        except Exception as e:  # noqa: BLE001 - every sender failure is retried
            attempts = row.attempts + 1
            execute('UPDATE notification_outbox SET attempts = %s, last_error = %s, status = %s, next_attempt_at = %s WHERE id = %s',
                    [attempts, str(e)[:500], 'failed' if attempts >= MAX_ATTEMPTS else 'queued',
                     at + timedelta(minutes=attempts * attempts), row.id])
            failed += 1
    return {'sent': sent, 'failed': failed, 'deferred': deferred}


def daily_digest(company_id: str, local_date: str) -> dict:
    """Managers' digest of employees with no punch yet on a working day."""
    missing = rows("""
        SELECT e.emp_code, e.name_en FROM employee e
         WHERE e.company_id = %(c)s AND e.status = 'active'
           AND NOT EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = e.id AND p.local_date = %(d)s::date)
           AND NOT EXISTS (SELECT 1 FROM leave_request l WHERE l.employee_id = e.id AND l.status = 'approved'
                             AND %(d)s::date BETWEEN l.from_date AND l.to_date AND l.from_time IS NULL)
           AND NOT EXISTS (SELECT 1 FROM holiday h WHERE h.company_id = e.company_id AND h.local_date = %(d)s::date
                             AND h.branch_id IS NULL AND NOT h.optional)
           AND NOT (extract(dow from %(d)s::date)::int = ANY(e.weekly_off_days))
         ORDER BY e.emp_code""", {'c': company_id, 'd': local_date})
    if not missing:
        return {'managers': 0, 'missing': 0}
    names = ', '.join(f"{m['name_en']} ({m['emp_code']})" for m in missing[:30])
    more = f' and {len(missing) - 30} more' if len(missing) > 30 else ''
    managers = notify_role(company_id, 'manager', template='attendance.digest',
                           title=f'{len(missing)} employees have not punched today',
                           body=f'No punch recorded yet for: {names}{more}.',
                           payload={'date': local_date, 'count': len(missing)})
    return {'managers': managers, 'missing': len(missing)}


def payload_of(row: dict) -> dict[str, Any]:
    p = row.get('payload')
    return p if isinstance(p, dict) else {}
