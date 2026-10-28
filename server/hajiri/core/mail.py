"""
Email through Django's email framework. SMTP_URL (the TS setting) configures it:

    SMTP_URL=smtp://user:pass@mail.example.com:587?from=hr@example.com    # STARTTLS when the server offers it
    SMTP_URL=smtps://user:pass@mail.example.com:465                       # implicit TLS

config/settings.py turns it into EMAIL_BACKEND / EMAIL_HOST / ... with `email_settings(url)`; without SMTP_URL the
console backend logs messages (development, like the TS LogSender). Notification delivery sends with
`EmailSender` (the notifications Sender protocol: `.channel`, `.send(row)`), which uses `django.core.mail`, so tests
read `django.core.mail.outbox` with the locmem backend.
"""
from __future__ import annotations

import smtplib
import ssl
from urllib.parse import parse_qs, unquote, urlparse

from django.core.mail import EmailMessage, get_connection
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend


def email_settings(url: str) -> dict:
    """Django EMAIL_* settings for an SMTP_URL (empty URL: the console backend)."""
    if not url:
        return {'EMAIL_BACKEND': 'django.core.mail.backends.console.EmailBackend',
                'DEFAULT_FROM_EMAIL': 'hajiri@localhost'}
    u = urlparse(url)
    if u.scheme not in ('smtp', 'smtps'):
        raise ValueError('SMTP_URL must start with smtp:// or smtps://')
    implicit_tls = u.scheme == 'smtps'
    user, password = unquote(u.username or ''), unquote(u.password or '')
    sender = (parse_qs(u.query).get('from') or [''])[0] or user or 'hajiri@localhost'
    return {
        'EMAIL_BACKEND': 'hajiri.core.mail.OpportunisticTlsBackend',
        'EMAIL_HOST': u.hostname or 'localhost',
        'EMAIL_PORT': u.port or (465 if implicit_tls else 587),
        'EMAIL_HOST_USER': user,
        'EMAIL_HOST_PASSWORD': password,
        'EMAIL_USE_SSL': implicit_tls,
        'EMAIL_USE_TLS': False,
        'EMAIL_TIMEOUT': 20,
        'DEFAULT_FROM_EMAIL': sender,
    }


class _StartTlsWhenOffered(smtplib.SMTP):
    """A plain SMTP connection that upgrades itself with STARTTLS when the server offers it, before any login."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ehlo()
        if self.has_extn('starttls'):
            self.starttls(context=ssl.create_default_context())
            self.ehlo()


class OpportunisticTlsBackend(SmtpBackend):
    """Django's SMTP backend, upgrading a plain connection with STARTTLS when the server offers it (what the TS
    sender and nodemailer do), instead of EMAIL_USE_TLS's all-or-nothing."""

    @property
    def connection_class(self):
        return smtplib.SMTP_SSL if self.use_ssl else _StartTlsWhenOffered


class EmailSender:
    """Notification sender for the email channel (OutboxRow: id, title, body, recipient_email)."""
    channel = 'email'

    def __init__(self, connection=None):
        self._connection = connection

    def send(self, row) -> None:
        if not row.recipient_email:
            raise RuntimeError('no recipient for email')
        EmailMessage(subject=row.title, body=row.body, to=[row.recipient_email],
                     connection=self._connection or get_connection(fail_silently=False)).send()
