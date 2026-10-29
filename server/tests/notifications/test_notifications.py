"""Port of apps/api/test/notifications.test.ts and the notification parts of worker.test.ts."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.support.sql import execute, one, rows
from hajiri.core.errors import NotFoundError
from hajiri.notifications.service import (
    LogSender, OutboxRow, SmtpSender, WebhookSender, daily_digest, deliver_queued, in_quiet_hours, inbox, inbox_from_query,
    mark_all_read, mark_read, notify, payslip_published_notification, quiet_hours_end, senders_from_env, strip_amounts,
)


def test_inbox_lists_own_rows_with_unread_count_marks_read_and_read_all(demo, login_as):
    me = login_as(['employee'], demo['sita'])
    mgr = login_as(['manager'])
    notify(company_id=demo['company_id'], user_id=me.user_id, template='leave.decided', title='Leave approved', body='Your leave was approved.')
    notify(company_id=demo['company_id'], user_id=me.user_id, template='x', title='Second', body='b', channels=['email'])
    notify(company_id=demo['company_id'], user_id=mgr.user_id, template='x', title='Not mine', body='b')
    r = inbox(me.user_id)
    assert [n['title'] for n in r['notifications']] == ['Second', 'Leave approved']
    assert r['unread'] == 2
    mark_read(me.user_id, str(r['notifications'][0]['id']))
    assert len(inbox_from_query(me.user_id, {'unreadOnly': 'true'})['notifications']) == 1
    assert mark_all_read(me.user_id) == 1
    assert inbox(me.user_id)['unread'] == 0
    # The extra channel is queued for the worker, the in-app row is delivered at once.
    q = rows("SELECT channel, status FROM notification_outbox WHERE title = 'Second' ORDER BY channel")
    assert [(x['channel'], x['status']) for x in q] == [('email', 'queued'), ('in_app', 'sent')]


def test_cannot_mark_another_users_notification(demo, login_as):
    me = login_as(['employee'], demo['sita'])
    mgr = login_as(['manager'])
    notify(company_id=demo['company_id'], user_id=mgr.user_id, template='x', title='Not mine', body='b')
    other = one('SELECT id FROM notification_outbox WHERE user_id = %s LIMIT 1', [mgr.user_id])
    with pytest.raises(NotFoundError, match='notification not found'):
        mark_read(me.user_id, str(other['id']))


def test_payslip_notification_has_no_amounts_and_digest_reaches_managers(demo, login_as):
    login_as(['employee'], demo['sita'])
    mgr = login_as(['manager'])
    assert not re.search(r'NPR|Rs\.|\d{1,3}(,\d{3})+|\d{5,}', payslip_published_notification('Bhadra 2083')['body'])
    assert daily_digest(demo['company_id'], '2026-09-21') == {'managers': 1, 'missing': 2}
    n = one("SELECT title FROM notification_outbox WHERE template = 'attendance.digest' AND user_id = %s", [mgr.user_id])
    assert n['title'] == '2 employees have not punched today'
    assert daily_digest(demo['company_id'], '2026-09-26') == {'managers': 0, 'missing': 0}  # Saturday


class _Sender:
    def __init__(self, channel, fn):
        self.channel, self.fn = channel, fn

    def send(self, row):
        self.fn(row)


def test_delivery_retries_with_backoff_and_defers_in_quiet_hours(demo, login_as):
    hr = login_as(['hr'])
    sent, state = [], {'fail': True}

    def sms(_row):
        if state['fail']:
            raise RuntimeError('gateway down')
    senders = [_Sender('email', lambda r: sent.append(r.title)), _Sender('sms', sms)]
    notify(company_id=demo['company_id'], user_id=hr.user_id, template='t', title='Hello', body='Net pay NPR 45,000 ready',
           channels=['email', 'sms'])
    day = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)  # 11:45 NPT
    execute("UPDATE notification_outbox SET next_attempt_at = %s WHERE title = 'Hello'", [day])
    assert deliver_queued(senders, day) == {'sent': 1, 'failed': 1, 'deferred': 0}
    assert sent == ['Hello']
    sms_row = one("SELECT status, attempts, body, next_attempt_at FROM notification_outbox WHERE channel = 'sms' AND title = 'Hello'")
    assert (sms_row['status'], sms_row['attempts']) == ('queued', 1)
    assert '45,000' not in sms_row['body']
    assert sms_row['next_attempt_at'] == day + timedelta(minutes=1)
    # Not due yet
    assert deliver_queued(senders, day + timedelta(seconds=30)) == {'sent': 0, 'failed': 0, 'deferred': 0}
    # Due but inside quiet hours (22:00 NPT = 16:15Z) -> deferred to 07:00 NPT
    night = datetime(2026, 9, 21, 16, 15, tzinfo=timezone.utc)
    assert deliver_queued(senders, night) == {'sent': 0, 'failed': 0, 'deferred': 1}
    deferred = one("SELECT next_attempt_at FROM notification_outbox WHERE channel = 'sms' AND title = 'Hello'")
    assert deferred['next_attempt_at'] == datetime(2026, 9, 22, 1, 15, tzinfo=timezone.utc)
    state['fail'] = False
    assert deliver_queued(senders, datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)) == {'sent': 1, 'failed': 0, 'deferred': 0}


def test_gives_up_after_five_attempts(demo, login_as):
    hr = login_as(['hr'])
    notify(company_id=demo['company_id'], user_id=hr.user_id, template='t', title='Mail', body='b', channels=['email'])
    at = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    for i in range(5):
        execute("UPDATE notification_outbox SET next_attempt_at = %s WHERE channel = 'email'", [at])
        deliver_queued([], at)  # no email sender configured
    r = one("SELECT status, attempts, last_error FROM notification_outbox WHERE channel = 'email'")
    assert (r['status'], r['attempts']) == ('failed', 5)
    assert r['last_error'] == 'no sender configured for email'


def test_quiet_hours_helpers_and_amount_stripping():
    assert in_quiet_hours(datetime(2026, 9, 21, 16, 15, tzinfo=timezone.utc)) is True   # 22:00 NPT
    assert in_quiet_hours(datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)) is False    # 11:45 NPT
    assert quiet_hours_end(datetime(2026, 9, 21, 16, 15, tzinfo=timezone.utc)) == datetime(2026, 9, 22, 1, 15, tzinfo=timezone.utc)
    assert strip_amounts('Net Rs. 45,000.50 and 123456 paid') == 'Net [amount] and [amount] paid'


def _row(**kw):
    base = dict(id='1', company_id=None, user_id=None, employee_id=None, channel='sms', template='t', title='T', body='B',
                payload={}, attempts=0, recipient_email='a@b.c', recipient_phone='9800000000')
    return OutboxRow(**{**base, **kw})


def test_webhook_sender_posts_json_and_fails_on_error_status():
    calls = []
    s = WebhookSender('sms', 'http://gw.local/sms', post=lambda url, body: (calls.append((url, body)), 200)[1])
    s.send(_row())
    assert calls[0] == ('http://gw.local/sms', {'channel': 'sms', 'to': '9800000000', 'title': 'T', 'body': 'B', 'template': 't',
                                                 'payload': {}, 'id': '1'})
    with pytest.raises(RuntimeError, match='sms gateway responded 502'):
        WebhookSender('sms', 'http://x', post=lambda u, b: 502).send(_row())
    with pytest.raises(RuntimeError, match='no recipient for sms'):
        WebhookSender('sms', 'http://x', post=lambda u, b: 200).send(_row(recipient_phone=None))


def test_smtp_sender_sends_mail_from_smtp_url():
    """SMTP_URL configures Django's SMTP backend (STARTTLS when offered); the message goes out from the URL's sender.
    (The hand-written SMTP client of the port is gone: Django's backend is exercised with its locmem stand-in.)"""
    from django.core import mail
    from django.core.mail import get_connection
    s = SmtpSender('smtp://hr%40acme.np:s3cret@mail.acme.np:2525?from=hr%40acme.np')
    assert (s.host, s.port, s.sender, s.user, s.password, s.tls) == ('mail.acme.np', 2525, 'hr@acme.np', 'hr@acme.np', 's3cret', False)
    real = s.connection()
    assert type(real).__name__ == 'OpportunisticTlsBackend'
    assert (real.host, real.port, real.username, real.use_ssl) == ('mail.acme.np', 2525, 'hr@acme.np', False)
    s = SmtpSender('smtp://hr%40acme.np:s3cret@mail.acme.np:2525?from=hr%40acme.np',
                   connection=get_connection('django.core.mail.backends.locmem.EmailBackend'))
    mail.outbox = []
    s.send(_row(channel='email', title='Leave approved', body='Your leave was approved.'))
    assert len(mail.outbox) == 1
    m = mail.outbox[0]
    assert (m.to, m.subject, m.from_email, m.body) == (['a@b.c'], 'Leave approved', 'hr@acme.np', 'Your leave was approved.')
    with pytest.raises(RuntimeError, match='no recipient for email'):
        s.send(_row(channel='email', recipient_email=None))


def test_senders_from_environment():
    s = {x.channel: x for x in senders_from_env({'SMTP_URL': 'smtps://u:p@smtp.local', 'SMS_WEBHOOK_URL': 'http://sms.local'})}
    assert isinstance(s['email'], SmtpSender) and s['email'].port == 465
    assert isinstance(s['sms'], WebhookSender)
    assert isinstance(s['push'], LogSender) and isinstance(s['webhook'], LogSender)
