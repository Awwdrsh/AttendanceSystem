"""Kiosk tokens (tokens.ts) and the kiosk tablet rules of kiosk/routes.ts not covered by field.test.ts: the kiosk-wide
lockout, the feature switch, the page itself."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from tests.support.sql import execute, one
from hajiri.kiosk import tokens
from tests.punch.api import Res, SELFIE, post, put


def test_tokens_round_trip_and_tamper():
    eid = '00000000-0000-7000-8000-000000000011'
    kid = '00000000-0000-7000-8000-0000000000aa'
    w = tokens.qr_window()
    t = tokens.qr_token(eid, 1, w)
    p = tokens.parse_qr(t)
    assert p == {'employeeId': eid, 'window': w, 'sig': t.split('.')[-1]}
    now = w * tokens.QR_WINDOW_MS
    assert tokens.verify_qr(p, 1, now)
    assert tokens.verify_qr(p, 1, now + tokens.QR_WINDOW_MS)          # one window either side
    assert not tokens.verify_qr(p, 1, now + 2 * tokens.QR_WINDOW_MS)
    assert not tokens.verify_qr(p, 2, now)                             # HR reset bumps the version
    assert tokens.parse_qr('HQ1.not-a-token') is None
    ticket = tokens.issue_ticket(kid, eid, now=now, via_qr=True)
    v = tokens.verify_ticket(ticket, kid, now=now)
    assert v['employeeId'] == eid and v['viaQr'] is True and len(v['nonce']) == 12
    assert tokens.verify_ticket(ticket, kid, now=now + tokens.TICKET_TTL_MS + 1) is None
    assert tokens.verify_ticket(ticket, eid, now=now) is None
    assert tokens.verify_ticket(tokens.issue_ticket(kid, eid, now=now), kid, now=now)['viaQr'] is False


def test_pairing_code_and_weak_pins():
    import re
    assert re.fullmatch(r'[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}', tokens.pairing_code())
    assert tokens.normalise_pairing_code('abcd-efgh') == 'ABCDEFGH'
    for weak in ('1111', '1234', '4321', '9012', '2580', '121212', '000000'):
        assert tokens.weak_pin(weak), weak
    for ok in ('4826', '1357', '820461'):
        assert not tokens.weak_pin(ok), ok


def test_rate_limit():
    assert all(tokens.allow('k', 3, 1000, now=10) for _ in range(3))
    assert not tokens.allow('k', 3, 1000, now=10)
    assert tokens.allow('k', 3, 1000, now=2000)


def _k(key, url, body=None, get=False):
    c = Client()
    h = {'HTTP_X_KIOSK_KEY': key} if key else {}
    if get:
        return Res(c.get(f'/api/v1/kiosk{url}', **h))
    return Res(c.post(f'/api/v1/kiosk{url}', json.dumps(body or {}), content_type='application/json', **h))


@pytest.fixture
def paired(demo, login_as):
    hr = login_as(['hr']).token
    c = post(hr, '/field/kiosks', {'name': 'Gate', 'selfieRequired': False})
    key = _k(None, '/pair', {'code': c.body['pairingCode']}).body['key']
    return {'hr': hr, 'id': c.body['id'], 'key': key}


def test_kiosk_locks_after_many_wrong_attempts_and_hr_unpauses(demo, paired):
    key = paired['key']
    execute('UPDATE kiosk SET failed_attempts = 19 WHERE id = %s', [paired['id']])
    r = _k(key, '/identify', {'empCode': 'NOPE', 'pin': '4826'})
    assert r.status == 400
    assert r.body['error'] == 'Employee code or PIN is wrong. Set your kiosk PIN in the app under My attendance.'
    assert one('SELECT locked_until IS NOT NULL AS l FROM kiosk WHERE id = %s', [paired['id']])['l']
    r = _k(key, '/identify', {'empCode': 'E002', 'pin': '4826'})
    assert r.status == 429 and r.body['error'] == 'This kiosk is paused after too many wrong attempts. Try again in a few minutes.'
    assert _k(key, '/session', get=True).body['kiosk']['lockedUntil'] is not None
    assert post(paired['hr'], f"/field/kiosks/{paired['id']}/unlock").status == 200
    assert _k(key, '/session', get=True).body['kiosk']['lockedUntil'] is None


def test_kiosk_punch_without_selfie_and_messages(demo, paired, login_as):
    ram = login_as(['employee', 'manager'], demo['ram']).token
    assert put(ram, '/field/kiosk-pin', {'pin': '4826'}).status == 200
    key = paired['key']
    who = _k(key, '/identify', {'empCode': 'E002', 'pin': '4826'})
    assert who.body['selfieRequired'] is False
    p = _k(key, '/punch', {'ticket': who.body['ticket']})
    assert p.status == 201
    assert p.body['message'].startswith('Clocked in at ') and p.body['message'].endswith('Have a good day, Ram!')
    dup = _k(key, '/punch', {'ticket': who.body['ticket']})
    assert dup.status == 200 and dup.body['message'].startswith('Already recorded at ')
    who = _k(key, '/identify', {'empCode': 'E002', 'pin': '4826'})
    assert who.body['nextDirection'] == 'out'
    p = _k(key, '/punch', {'ticket': who.body['ticket'], 'selfie': SELFIE})
    assert p.body['direction'] == 'out' and p.body['message'].endswith('See you tomorrow, Ram!')
    assert one("SELECT vendor FROM device WHERE serial_no = 'KIOSK-DEMO'")['vendor'] == 'kiosk'
    assert _k(key, '/punch', {'ticket': 'x' * 30}).body['error'] == 'This sign-in has expired. Enter your code and PIN again.'


def test_kiosk_key_rules_and_feature_off(demo, paired):
    assert _k('short', '/session', get=True).body['error'] == 'This tablet is not paired. Enter a pairing code from HR.'
    assert _k('x' * 40, '/session', get=True).body['error'] == 'This tablet is no longer paired. Ask HR for a new pairing code.'
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["field"]}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    from hajiri.features.service import invalidate_features
    invalidate_features(demo['company_id'])
    r = _k(paired['key'], '/session', get=True)
    assert r.status == 404 and r.body['error'] == 'feature off'


def test_pairing_is_rate_limited(db):
    for _ in range(10):
        assert _k(None, '/pair', {'code': 'AAAA-BBBB'}).status == 400
    r = _k(None, '/pair', {'code': 'AAAA-BBBB'})
    assert r.status == 429 and r.body['error'] == 'Too many pairing attempts. Wait a few minutes and try again.'


def test_kiosk_page_needs_no_session(db):
    r = Client().get('/kiosk')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'hjKiosk()' in html and 'kiosk/kiosk.js' in html
    assert 'Set up this kiosk' in html and 'Scan my QR badge' in html
