"""Home (HomePage.tsx and App.tsx Home): who lands where, the clock-in card and its punch flow, the month and requests."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.core.db import execute, one
from hajiri.home import services as s
from tests.web.factories import htmx, hx_triggers, make_employee, signed_in_client, toasts


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.fixture
def sita(demo, login_as):
    return client_for(login_as(['employee'], demo['sita']))


def test_landing_by_role(acme):
    emp, _ = signed_in_client(acme, roles=['employee'])
    r = emp.get('/')
    assert r.status_code == 200 and b'Clock in' in r.content and b"Today's activity" in r.content
    hr, _ = signed_in_client(acme, roles=['hr'], employee=None)
    assert hr.get('/')['Location'] == '/overview'
    mgr, _ = signed_in_client(acme, roles=['manager'], employee=None)
    assert mgr.get('/')['Location'] == '/approvals'
    plain, _ = signed_in_client(acme, roles=['executive'], employee=None)
    r = plain.get('/')
    assert r.status_code == 200 and b'No employee record linked' in r.content
    # An HR person who is also an employee sees their own day.
    hr_emp, _ = signed_in_client(acme, roles=['hr'])
    assert hr_emp.get('/').status_code == 200
    # Signed out: to the sign-in page.
    assert Client().get('/')['Location'] == '/login'


def test_greeting_is_the_page_title_and_the_card_starts_empty(acme):
    emp = make_employee(acme, name='Sita Sharma')
    c, _ = signed_in_client(acme, roles=['employee'], employee=emp)
    html = c.get('/').content.decode()
    assert ', Sita</h1>' in html
    assert 'Not clocked in yet' in html and 'No shift assigned for today' in html
    assert 'Nothing yet. Your clock-ins and clock-outs for today will show here.' in html
    assert 'Nothing waiting' in html and 'Work from home' in html and '/me/attendance?correct=1' in html


def test_clock_in_then_out_from_the_card(sita, demo):
    r = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0001', 'lat': '', 'lng': ''}, **htmx())
    assert r.status_code == 200, r.content
    t = toasts(r)
    assert t[0]['message'].startswith('Clocked in at') and t[0]['tone'] == 'ok'
    assert t[1]['message'] == 'Location was not available, so this was recorded without it.'
    assert r['HX-Retarget'] == '#home-today' and 'hj:home-punched' in hx_triggers(r)
    html = r.content.decode()
    assert 'Clocked in at' in html and 'Worked so far' in html and 'Clock out' in html
    # The same tap again is recorded once.
    again = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0001'}, **htmx())
    assert toasts(again)[0]['message'].startswith('Already recorded: recorded at')  # TS: the stored punch carries no direction
    assert one("SELECT count(*)::int AS n FROM punch_event WHERE employee_id = %s", [demo['sita']])['n'] == 1
    out = sita.post('/_/home/punch', {'direction': 'out', 'idempotency_key': 'home-test-0002', 'lat': '27.7', 'lng': '85.3',
                                      'accuracy': '12'}, **htmx())
    assert toasts(out)[0]['message'].startswith('Clocked out at') and len(toasts(out)) == 1
    assert 'Clocked out at' in out.content.decode()


def test_outside_location_asks_for_a_reason(sita, demo):
    execute("""INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'HQ', 27.7, 85.3, 100)""", [demo['company_id']])
    r = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0003', 'lat': '27.8', 'lng': '85.4'}, **htmx())
    assert r.status_code == 200 and r['HX-Retarget'] == '#hj-modal-body'
    html = r.content.decode()
    assert "You're outside your work location" in html and 'Add a short reason to punch anyway.' in html
    assert 'home-test-0003' in html  # the retry keeps the same key
    ok = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0003', 'lat': '27.8', 'lng': '85.4',
                                     'reason': 'client site'}, **htmx())
    assert ok.status_code == 200 and '(outside location, flagged)' in toasts(ok)[0]['message']
    assert 'hj:close-modal' in hx_triggers(ok)


def test_work_from_home_needs_a_photo(sita, demo):
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['sita']])
    html = sita.get('/').content.decode()
    assert 'Clock in from home' in html and 'At the office? Clock in without a photo' in html
    assert 'You can clock in from home. Your clock-ins away from the office are marked Remote.' in html
    assert 'hx-get="/_/regularisation/wfh"' not in html  # no Work from home request button when allowed
    r = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0004'}, **htmx())
    assert r.status_code == 204
    assert hx_triggers(r)['hj:home-camera'] == {'direction': 'in', 'reason': ''}
    assert toasts(r)[0]['message'] == 'You are clocking in from home, so a quick photo is needed.'


def test_late_clock_in_closed_shows_the_modal(sita, demo):
    from hajiri.attendance_tools import services as at
    from hajiri.core.errors import ConflictError

    def refuse(**kw):
        raise ConflictError('Clock-in closed at 10:30. Raise a time request.', {'code': 'late_blocked'})
    orig = at.assert_clock_in_allowed
    at.assert_clock_in_allowed = refuse
    try:
        r = sita.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'home-test-0005'}, **htmx())
    finally:
        at.assert_clock_in_allowed = orig
    assert r.status_code == 200 and r['HX-Retarget'] == '#hj-modal-body'
    assert 'Clock-in is closed' in r.content.decode() and 'Clock-in closed at 10:30.' in r.content.decode()


def test_other_refusals_toast_with_the_ts_title(acme):
    c, _ = signed_in_client(acme, roles=['hr'], employee=None)
    r = c.post('/_/home/punch', {'direction': 'out', 'idempotency_key': 'home-test-0006'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['title'] == 'Clock-out not saved'
    c2, _ = signed_in_client(acme, roles=['employee'])
    r = c2.post('/_/home/punch', {'direction': 'in', 'idempotency_key': 'short'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['title'] == 'Clock-in not saved'
    assert 'idempotencyKey' in toasts(r)[0]['message']


def test_month_balances_and_pending_requests(sita, demo):
    from hajiri.core.clock import npt_today
    d = npt_today().isoformat()
    execute("""INSERT INTO regularisation_request (employee_id, local_date, kind, reason, status, created_by)
               SELECT %s, %s, 'wfh', 'delivery', 'pending', u.id FROM app_user u WHERE u.employee_id = %s LIMIT 1""",
            [demo['sita'], d, demo['sita']])
    html = sita.get('/').content.decode()
    assert 'Work from home' in html and 'Pending requests' in html and 'This month' in html and 'Present days' in html
    frag = sita.get('/_/home/requests', **htmx()).content.decode()
    assert 'Work from home' in frag and 'Nothing waiting' not in frag
    assert 'Absent days' in sita.get('/_/home/month', **htmx()).content.decode()


def test_describe_request_and_worked_minutes():
    assert s.describe_request({'kind': 'regularisation', 'detail': '2026-10-15 wfh'}) == {'title': 'Work from home', 'detail': 'Thu, 29 Ashwin 2083'}
    assert s.describe_request({'kind': 'leave', 'detail': '2026-10-15 to 2026-10-17'})['detail'] == 'Thu, 29 Ashwin 2083 to Sat, 31 Ashwin 2083'
    assert s.describe_request({'kind': 'leave', 'detail': '2026-10-15 to 2026-10-15'})['detail'] == 'Thu, 29 Ashwin 2083'
    assert s.describe_request({'kind': 'ot', 'detail': '2026-10-15 120 min'}) == {'title': 'Overtime', 'detail': 'Thu, 29 Ashwin 2083, 2 h'}
    assert s.describe_request({'kind': 'shift_swap', 'detail': 'x'}) == {'title': 'Shift swap', 'detail': 'x'}
    t0 = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
    ps = [{'punched_at': t0, 'direction': 'in'}, {'punched_at': t0 + timedelta(minutes=90), 'direction': 'out'},
          {'punched_at': t0 + timedelta(minutes=100), 'direction': 'in'}]
    assert s.worked_from_punches(ps, (t0 + timedelta(minutes=130)).timestamp() * 1000) == 120
    assert s.next_direction([]) == 'in' and s.next_direction(ps[:1]) == 'out' and s.next_direction(ps[:2]) == 'in'
    assert s.month_stats({'byStatus': {'Present': 3, 'WFH': 1, 'Absent': 2}, 'lateMin': 20, 'otMin': 0})[0]['value'] == 4
    st = s.month_stats({'byStatus': {'Absent': 2}, 'lateMin': 20, 'otMin': 0})
    assert st[1]['tone'] == 'warn' and st[3]['tone'] == 'bad' and 'tone' not in st[2]


def test_working_today_hides_missing_out_flags():
    from hajiri.core.clock import npt_today
    d = npt_today().isoformat()
    day = {'status': 'Incomplete', 'first_in': '2026-10-15T04:00:00Z', 'last_out': None, 'worked_min': 0, 'shift_id': None,
           'late_min': 5, 'flags': [{'kind': 'MissingOut'}, {'kind': 'Late', 'minutes': 5}]}
    card = s.today_card({'date': d, 'day': day, 'punches': [{'id': '1', 'punched_at': '2026-10-15T04:00:00Z', 'direction': 'in',
                                                             'source': 'web', 'remote': False}]}, None, [])
    assert card['status'] == 'Working' and card['flags'] == ['Late by 5 min'] and card['clocked_in']


def test_notifications_page_and_bell(demo, login_as):
    lg = login_as(['employee'], demo['sita'])
    c = client_for(lg)
    r = c.get('/me/notifications')
    assert b'No notifications yet' in r.content and b'You are all caught up' in r.content
    for i in range(2):
        execute("""INSERT INTO notification_outbox (company_id, user_id, channel, template, title, body)
                   VALUES (%s, %s, 'in_app', 't', %s, 'body text')""", [demo['company_id'], lg.user_id, f'Note {i}'])
    html = c.get('/me/notifications').content.decode()
    assert '2 unread' in html and 'Note 0' in html and 'aria-label="Unread"' in html
    nid = one('SELECT id FROM notification_outbox WHERE title = %s', ['Note 0'])['id']
    assert c.post(f'/_/notifications/{nid}/read', **htmx()).status_code == 204
    frag = c.get('/me/notifications', **htmx('notifications-list')).content.decode()
    assert '1 unread' in frag and 'Mark all as read' in frag
    from hajiri.home.nav import BADGES
    assert BADGES['notifications'](lg.user) == 1
    assert c.post('/_/notifications/read-all', **htmx()).status_code == 204
    assert 'You are all caught up' in c.get('/me/notifications').content.decode()
