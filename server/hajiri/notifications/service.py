"""
Notifications (port of apps/api/src/modules/notifications). notify() writes to the transactional outbox inside the
caller's transaction: an in-app row is visible at once, email/sms/push/webhook rows are delivered by the worker
(deliver_queued) with retry backoff (5 attempts) and quiet-hour deferral for sms/push (company setting
notifications.policy). SMS bodies never contain amounts. Email goes through Django's email framework (SMTP_URL).

People of a company are its home accounts, members of the group with access and group-role holders
(hajiri.core.users), so HR broadcasts and notices reach everyone who works there. A person's inbox shows the
notifications of the company they are working in (plus account notices that belong to no company).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Protocol

from django.core.mail import EmailMessage, get_connection
from django.db.models import Case, Exists, F, OuterRef, Q, When
from django.db.models.functions import Coalesce, Now
from rest_framework import serializers

from hajiri.core import users as core_users
from hajiri.core.clock import NPT, now as _now
from hajiri.core.errors import NotFoundError
from hajiri.core.mail import EmailSender, email_settings
from hajiri.core.scoping import is_uuid
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import validate
from hajiri.db.models import AppUser, CompanyMembership, Employee, Holiday, LeaveRequest, NotificationOutbox, PunchEvent

log = logging.getLogger('hajiri.notifications')

Channel = NotificationOutbox.Channel
Status = NotificationOutbox.Status
CHANNELS = (Channel.EMAIL, Channel.SMS, Channel.PUSH, Channel.WEBHOOK)


def _json(payload: dict | None) -> dict:
    """The payload as JSON values (ids and dates as strings), as JSON.stringify stored them."""
    return json.loads(json.dumps(payload or {}, default=str))


def notify(*, company_id: str | None, template: str, title: str, body: str, user_id: str | None = None,
           employee_id: str | None = None, payload: dict | None = None, channels: Iterable[str] = (),
           to_email: str | None = None) -> None:
    """Always one in_app row; each extra channel is queued for the worker. Call inside the business transaction."""
    data = _json(payload)
    NotificationOutbox.objects.bulk_create([
        NotificationOutbox(
            company_id=company_id, user_id=user_id, employee_id=employee_id, channel=channel, template=template, title=title,
            body=strip_amounts(body) if channel == Channel.SMS else body, payload=data,
            status=Status.SENT if channel == Channel.IN_APP else Status.QUEUED,
            to_email=to_email if channel != Channel.IN_APP else None)
        for channel in (Channel.IN_APP, *channels)])


def user_id_for_employee(employee_id: str) -> str | None:
    """The active account that works as this employee: the home account (app_user.employee_id) or a member of the
    group whose membership in the employee's company is this record."""
    if not employee_id:
        return None
    member = CompanyMembership.objects.filter(user=OuterRef('pk'), employee_id=employee_id,
                                              status=CompanyMembership.Status.ACTIVE)
    uid = (AppUser.objects.filter(status=AppUser.Status.ACTIVE).filter(Q(employee_id=employee_id) | Exists(member))
           .order_by(Case(When(employee_id=employee_id, then=0), default=1), 'id').values_list('id', flat=True).first())
    return str(uid) if uid else None


def user_ids_with_role(company_id: str, role: str) -> list[str]:
    """Active people holding a role in a company (admins always included), for HR broadcasts: home accounts, members
    with access and group-role holders."""
    return core_users.user_ids_with_role(company_id, role)


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

class InboxQuery(serializers.Serializer):
    """?limit=&before=&unreadOnly= for the inbox."""
    limit = serializers.IntegerField(min_value=1, max_value=100, default=30)
    before = serializers.DateTimeField(required=False)
    unreadOnly = serializers.BooleanField(required=False, default=False)


INBOX_FIELDS = ('id', 'template', 'title', 'body', 'payload', 'created_at', 'read_at')


def _mine(user_id: str, company_id: str | None = None):
    """A person's in-app notifications; with company_id, only those of that company and those of no company."""
    qs = NotificationOutbox.objects.filter(user_id=user_id, channel=Channel.IN_APP)
    if company_id:
        qs = qs.filter(Q(company_id=company_id) | Q(company__isnull=True))
    return qs


def inbox(user_id: str, *, limit: int = 30, before: str | datetime | None = None, unread_only: bool = False,
          company_id: str | None = None) -> dict:
    """The signed-in user's in-app notifications, newest first, and the unread count. Pass the ACTIVE company to see
    only what belongs to it (a person who works in two companies sees each company's notices there)."""
    qs = _mine(user_id, company_id)
    if before:
        qs = qs.filter(created_at__lt=before)
    if unread_only:
        qs = qs.filter(read_at__isnull=True)
    items = list(qs.order_by('-created_at', '-id').values(*INBOX_FIELDS)[:limit])
    return {'notifications': items, 'unread': unread_count(user_id, company_id=company_id)}


def inbox_from_query(user_id: str, query: dict, *, company_id: str | None = None) -> dict:
    q = validate(InboxQuery, query)
    return inbox(user_id, limit=q['limit'], before=q.get('before'), unread_only=q['unreadOnly'], company_id=company_id)


def unread_count(user_id: str, *, company_id: str | None = None) -> int:
    return _mine(user_id, company_id).filter(read_at__isnull=True).count()


def mark_read(user_id: str, notification_id: str, *, company_id: str | None = None) -> dict:
    """Mark one of the person's notifications read (another person's id is not found; with company_id, so is one of
    another company)."""
    if not is_uuid(notification_id):
        raise NotFoundError('notification not found')
    mine = _mine(user_id, company_id).filter(pk=notification_id)
    if not mine.update(read_at=Coalesce('read_at', Now())):
        raise NotFoundError('notification not found')
    return mine.values('id', 'read_at').first()


def mark_all_read(user_id: str, *, company_id: str | None = None) -> int:
    return _mine(user_id, company_id).filter(read_at__isnull=True).update(read_at=Now())


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


class SmtpSender(EmailSender):
    """Email through Django's SMTP backend (hajiri.core.mail.OpportunisticTlsBackend) configured from an SMTP_URL:
    smtp://user:pass@host:587?from=hr@example.com (smtps:// for implicit TLS; plain smtp:// upgrades with STARTTLS
    when the server offers it). `connection` replaces the SMTP connection (tests pass Django's locmem backend)."""

    def __init__(self, url: str, connection=None):
        cfg = email_settings(url)
        self.url = url
        self.host, self.port = cfg['EMAIL_HOST'], cfg['EMAIL_PORT']
        self.user, self.password = cfg['EMAIL_HOST_USER'], cfg['EMAIL_HOST_PASSWORD']
        self.tls = cfg['EMAIL_USE_SSL']
        self.sender = cfg['DEFAULT_FROM_EMAIL']
        super().__init__(connection)

    def connection(self):
        return self._connection or get_connection(
            'hajiri.core.mail.OpportunisticTlsBackend', host=self.host, port=self.port, username=self.user,
            password=self.password, use_ssl=self.tls, use_tls=False, timeout=20, fail_silently=False)

    def send(self, row) -> None:
        if not row.recipient_email:
            raise RuntimeError('no recipient for email')
        EmailMessage(subject=row.title, body=row.body, from_email=self.sender, to=[row.recipient_email],
                     connection=self.connection()).send()


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


def _quiet_hours(company_id: str | None) -> tuple[str, str]:
    v = (get_setting(company_id, 'notifications.policy') if company_id else None) or {}
    return (v.get('quietHoursStart') or DEFAULT_QUIET[0], v.get('quietHoursEnd') or DEFAULT_QUIET[1])


def due_rows(at: datetime, limit: int = 100) -> list[OutboxRow]:
    """Queued rows whose time has come, with their recipient: the explicit address, else the account's email, else
    the employee's (the row's employee, or the account's own employee record); the phone from that employee."""
    employee = lambda f: Case(When(employee__isnull=False, then=F(f'employee__{f}')),  # noqa: E731
                              default=F(f'user__employee__{f}'))
    qs = (NotificationOutbox.objects
          .filter(status=Status.QUEUED, next_attempt_at__lte=at).exclude(channel=Channel.IN_APP)
          .annotate(recipient_email=Coalesce('to_email', 'user__email', employee('email')),
                    recipient_phone=employee('phone'))
          .order_by('created_at', 'id')
          .values('id', 'company_id', 'user_id', 'employee_id', 'channel', 'template', 'title', 'body', 'payload',
                  'attempts', 'recipient_email', 'recipient_phone')[:limit])
    return [OutboxRow(**{**r, 'id': str(r['id']), 'company_id': r['company_id'] and str(r['company_id']),
                         'user_id': r['user_id'] and str(r['user_id']), 'employee_id': r['employee_id'] and str(r['employee_id'])})
            for r in qs]


def deliver_queued(senders: list, at: datetime | None = None, limit: int = 100) -> dict:
    """Send due outbox rows. A failure retries after attempts² minutes and gives up after 5 tries; sms and push wait
    out quiet hours instead of being dropped."""
    at = at or _now()
    by_channel = {s.channel: s for s in senders}
    sent = failed = deferred = 0
    quiet_cache: dict[str, tuple[str, str]] = {}
    for row in due_rows(at, limit):
        this = NotificationOutbox.objects.filter(pk=row.id)
        if row.channel in (Channel.SMS, Channel.PUSH):
            key = row.company_id or ''
            if key not in quiet_cache:
                quiet_cache[key] = _quiet_hours(row.company_id)
            q = quiet_cache[key]
            if in_quiet_hours(at, q):
                this.update(next_attempt_at=quiet_hours_end(at, q))
                deferred += 1
                continue
        sender = by_channel.get(row.channel)
        try:
            if not sender:
                raise RuntimeError(f'no sender configured for {row.channel}')
            sender.send(row)
            this.update(status=Status.SENT, sent_at=at, attempts=F('attempts') + 1)
            sent += 1
        except Exception as e:  # noqa: BLE001 - every sender failure is retried
            attempts = row.attempts + 1
            this.update(attempts=attempts, last_error=str(e)[:500],
                        status=Status.FAILED if attempts >= MAX_ATTEMPTS else Status.QUEUED,
                        next_attempt_at=at + timedelta(minutes=attempts * attempts))
            failed += 1
    return {'sent': sent, 'failed': failed, 'deferred': deferred}


def missing_punch(company_id: str, local_date: str | date):
    """Active employees with no punch on a working day: not on full-day leave, not a company-wide holiday, not their
    weekly day off."""
    d = date.fromisoformat(str(local_date))
    dow = (d.weekday() + 1) % 7                           # Sunday = 0, as extract(dow)
    punched = PunchEvent.objects.filter(employee=OuterRef('pk'), local_date=d)
    on_leave = LeaveRequest.objects.filter(employee=OuterRef('pk'), status=LeaveRequest.Status.APPROVED, from_date__lte=d,
                                           to_date__gte=d, from_time__isnull=True)
    holiday = Holiday.objects.filter(company=OuterRef('company'), local_date=d, branch__isnull=True, optional=False)
    return (Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
            .exclude(Exists(punched)).exclude(Exists(on_leave)).exclude(Exists(holiday))
            .exclude(weekly_off_days__contains=[dow]).order_by('emp_code'))


def daily_digest(company_id: str, local_date: str) -> dict:
    """Managers' digest of employees with no punch yet on a working day."""
    missing = list(missing_punch(company_id, local_date).values('emp_code', 'name_en'))
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
