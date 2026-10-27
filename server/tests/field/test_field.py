"""Port of apps/api/test/field.test.ts: field force, breaks, geofence policy, kiosks with PIN/QR, and shift swaps (through
the JSON API, as the TS test). Each TS describe block is one test with its steps in order."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.auth import create_user
from hajiri.core.db import execute, one, rows
from hajiri.field.services import close_stale_breaks
from tests.core.fixtures import PW
from tests.punch.api import SELFIE, add_days, get, post, punch, put, today

OFFICE = {'lat': 27.7172, 'lng': 85.3240, 'accuracyM': 20, 'source': 'web'}
FAR = {'lat': 27.80, 'lng': 85.40, 'accuracyM': 20, 'source': 'web'}


@pytest.fixture
def w(demo, login_as):
    from hajiri.db.models import Company
    hr = login_as(['hr']).token
    sita = login_as(['employee'], demo['sita']).token
    ram = login_as(['employee', 'manager'], demo['ram']).token
    hari_id = str(one("""INSERT INTO employee (company_id, emp_code, name_en, weekly_off_days) VALUES (%s, 'E003', 'Hari Karki', '{6}') RETURNING id""",
                      [demo['company_id']])['id'])
    execute("INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES (%s, daterange('2026-01-01', NULL), %s, 'regular')",
            [hari_id, demo['branch_id']])
    execute("INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange('2026-01-01', NULL))", [hari_id, demo['shift_id']])
    hari = login_as(['employee'], hari_id).token
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    c = Company.objects.create(code='FLD2', name_en='Other Co')
    create_user(company_id=str(c.id), employee_id=None, email='hr@fld2.local', name='Other HR', password=PW, roles=['hr'])
    foreign = auth.login('hr@fld2.local', PW).token

    def new_employee(code):
        i = str(one('INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, %s, %s) RETURNING id', [demo['company_id'], code, f'Person {code}'])['id'])
        execute("INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES (%s, daterange('2026-01-01', NULL), %s, 'regular')",
                [i, demo['branch_id']])
        execute("INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange('2026-01-01', NULL))", [i, demo['shift_id']])
        return {'id': i, 'token': login_as(['employee'], i).token}
    return {'hr': hr, 'sita': sita, 'ram': ram, 'hari': hari, 'hari_id': hari_id, 'foreign': foreign, 'new_employee': new_employee}


def _setup_hq(w):
    return post(w['hr'], '/field/locations', {'name': 'Head office', 'address': 'Kathmandu', 'lat': 27.7172, 'lng': 85.3240, 'radiusM': 150})


def test_locations_and_geofence_policy(demo, w):
    hr, sita, foreign = w['hr'], w['sita'], w['foreign']
    # HR manages locations; employees cannot
    assert post(sita, '/field/locations', {'name': 'X', 'lat': 1, 'lng': 1}).status == 403
    c = _setup_hq(w)
    assert c.status == 201
    hq = c.body['id']
    assert post(hr, '/field/locations', {'name': 'Bad', 'lat': 200, 'lng': 1}).status == 400
    lst = get(hr, '/field/locations')
    assert len(lst.body['locations']) == 1
    assert lst.body['locations'][0]['name'] == 'Head office' and lst.body['locations'][0]['address'] == 'Kathmandu'
    assert lst.body['locations'][0]['active'] is True
    assert len(get(foreign, '/field/locations').body['locations']) == 0
    assert put(foreign, f'/field/locations/{hq}', {'name': 'Hijack', 'lat': 1, 'lng': 1}).status == 404

    # policy defaults to flag; only HR may change it
    assert get(sita, '/field/policy').body['policy']['geofenceMode'] == 'flag'
    assert put(sita, '/field/policy', {'geofenceMode': 'off'}).status == 403
    r = put(hr, '/field/policy', {'geofenceMode': 'block'})
    assert r.status == 200
    assert r.body['policy']['breaks']['lunch']['paid'] is False and r.body['policy']['breaks']['lunch']['maxMin'] == 60

    # block refuses a punch outside; off ignores locations; flag asks for a reason
    g = w['new_employee']('G001')
    blocked = punch(g['token'], FAR)
    assert blocked.status == 400
    assert blocked.body['details'] == {'code': 'outside_location'}
    put(hr, '/field/policy', {'geofenceMode': 'flag'})
    flagged = punch(g['token'], FAR)
    assert flagged.status == 400
    assert 'reason' in flagged.body['error']
    put(hr, '/field/policy', {'geofenceMode': 'off'})
    off = punch(g['token'], FAR)
    assert off.status == 201
    assert off.body['location']['inside'] is None
    put(hr, '/field/policy', {'geofenceMode': 'block'})

    # locations tied to a person replace the branch locations
    g = w['new_employee']('G002')
    site = post(hr, '/field/locations', {'name': 'Site B', 'lat': 27.80, 'lng': 85.40, 'radiusM': 200})
    assert put(hr, f"/field/staff/{g['id']}", {'locationIds': [site.body['id']]}).status == 200
    assert punch(g['token'], OFFICE).status == 400  # HQ is not one of this person's locations
    at_site = punch(g['token'], FAR)
    assert at_site.status == 201
    assert at_site.body['location']['inside'] is True and at_site.body['location']['name'] == 'Site B'
    assert put(hr, f"/field/staff/{g['id']}", {'locationIds': ['00000000-0000-7000-8000-00000000dead']}).status == 404
    assert put(foreign, f"/field/staff/{g['id']}", {'fieldWork': True}).status == 404


def test_breaks(demo, w):
    hr, sita, ram, hari, foreign = w['hr'], w['sita'], w['ram'], w['hari'], w['foreign']
    _setup_hq(w)
    # needs a clock-in; one open break at a time; ends with minutes
    assert post(sita, '/field/breaks/start', {'kind': 'tea'}).status == 400
    assert punch(sita, OFFICE).status == 201
    s = post(sita, '/field/breaks/start', {'kind': 'tea'})
    assert s.status == 201
    assert s.body['break']['kind'] == 'tea' and s.body['break']['paid'] is True and s.body['break']['max_min'] == 15
    assert post(sita, '/field/breaks/start', {'kind': 'lunch'}).status == 409
    assert post(sita, '/field/breaks/start', {'kind': 'nap'}).status == 400
    me = get(sita, '/field/me')
    assert me.body['clockedIn'] is True
    assert me.body['openBreak']['kind'] == 'tea'
    from hajiri.attendance import api as att_api
    team = att_api.today(auth.resolve_session(ram), {'date': today()})
    assert next(r for r in team['rows'] if str(r['employee_id']) == demo['sita'])['on_break'] == 'tea'
    e = post(sita, '/field/breaks/end')
    assert e.status == 200
    assert e.body['break']['over_limit'] is False
    assert post(sita, '/field/breaks/end').status == 409

    # an over-long break is flagged and the manager is told
    post(sita, '/field/breaks/start', {'kind': 'lunch'})
    execute("UPDATE work_break SET started_at = now() - interval '75 minutes' WHERE employee_id = %s AND ended_at IS NULL", [demo['sita']])
    e = post(sita, '/field/breaks/end')
    assert e.body['break']['over_limit'] is True and e.body['break']['minutes'] == 75
    n = one("""SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'break.over_limit'
                AND user_id = (SELECT id FROM app_user WHERE employee_id = %s)""", [demo['ram']])['n']
    assert n == 1

    # break lists are scoped: self, manager, HR; CSV export works
    rng = {'from': today(), 'to': today()}
    assert len(get(sita, '/field/breaks', rng).body['breaks']) == 2
    assert len(get(ram, '/field/breaks', {**rng, 'overOnly': '1'}).body['breaks']) == 1
    assert len(get(hari, '/field/breaks', rng).body['breaks']) == 0
    assert get(hari, '/field/breaks', {**rng, 'employeeId': demo['sita']}).status == 403
    assert len(get(hr, '/field/breaks', rng).body['breaks']) == 2
    assert len(get(foreign, '/field/breaks', rng).body['breaks']) == 0
    csv = get(hr, '/field/breaks', {**rng, 'format': 'csv'})
    assert 'text/csv' in csv.headers['Content-Type']
    assert 'Over limit' in csv.text
    assert get(hr, '/field/breaks', {'from': '2026-01-01', 'to': '2026-12-31'}).status == 400

    # the nightly job closes a break left open from an earlier day
    execute("""INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at)
               VALUES (%s, %s, %s, 'tea', true, 15, %s)""",
            [demo['company_id'], w['hari_id'], add_days(today(), -2), datetime.now(timezone.utc) - timedelta(days=2)])
    assert close_stale_breaks() == 1
    assert one('SELECT minutes FROM work_break WHERE employee_id = %s', [w['hari_id']])['minutes'] == 15
    execute('DELETE FROM work_break WHERE employee_id = %s', [w['hari_id']])


def test_field_visits(demo, w):
    hr, sita, hari, foreign, hari_id = w['hr'], w['sita'], w['hari'], w['foreign'], w['hari_id']
    _setup_hq(w)
    put(hr, '/field/policy', {'geofenceMode': 'block'})
    # only field staff record visits
    assert post(hari, '/field/visits/start', {'place': 'Client A'}).status == 403
    assert put(sita, f'/field/staff/{hari_id}', {'fieldWork': True}).status == 403
    assert put(hr, f'/field/staff/{hari_id}', {'fieldWork': True}).status == 200

    # the first visit of the day opens the day with a field punch; photo stored encrypted
    s = post(hari, '/field/visits/start', {'place': 'Client A', 'purpose': 'Demo', 'photo': SELFIE, **FAR})
    assert s.status == 201
    assert s.body['clockedIn'] is True
    visit_id = s.body['visit']['id']
    p = rows("SELECT source, direction, raw->'flags' AS flags FROM punch_event WHERE employee_id = %s", [hari_id])
    assert p == [{'source': 'field', 'direction': 'in', 'flags': ['Field']}]
    f = one('SELECT f.data_enc FROM field_visit v JOIN stored_file f ON f.id = v.photo_file_id WHERE v.id = %s', [visit_id])
    assert b'a-tiny-selfie' not in bytes(f['data_enc'])
    assert post(hari, '/field/visits/start', {'place': 'Client B'}).status == 409

    # field staff punching away from the office are tagged Field, not refused
    r = punch(hari, {'lat': 27.95, 'lng': 85.55, 'accuracyM': 20, 'source': 'web', 'direction': 'out'})
    assert r.status == 201
    assert 'Field' in r.body['flags']

    # photo access: self and HR yes, colleagues no, other company no
    assert get(hari, f'/field/visits/{visit_id}/photo').status == 200
    assert get(hr, f'/field/visits/{visit_id}/photo').status == 200
    assert get(sita, f'/field/visits/{visit_id}/photo').status == 403
    assert get(foreign, f'/field/visits/{visit_id}/photo').status == 404

    # end, list and export
    assert post(sita, f'/field/visits/{visit_id}/end', {'outcome': 'x'}).status == 404
    e = post(hari, f'/field/visits/{visit_id}/end', {'outcome': 'Order placed', **{k: v for k, v in FAR.items() if k != 'source'}})
    assert e.status == 200
    assert e.body['visit']['outcome'] == 'Order placed'
    lst = get(hr, '/field/visits', {'from': today(), 'to': today()})
    v0 = lst.body['visits'][0]
    assert v0['place'] == 'Client A' and v0['has_photo'] is True and v0['opened_day'] is True and v0['start_lat'] == 27.8
    assert len(get(sita, '/field/visits', {'from': today(), 'to': today()}).body['visits']) == 0
    csv = get(hr, '/field/visits', {'from': today(), 'to': today(), 'format': 'csv'})
    assert 'Client A' in csv.text

    # a required photo is enforced
    put(hr, '/field/policy', {'geofenceMode': 'block', 'fieldPhotoRequired': True})
    r = post(hari, '/field/visits/start', {'place': 'Client C'})
    assert r.status == 400
    assert r.body['details'] == {'code': 'photo_required'}
    put(hr, '/field/policy', {'geofenceMode': 'flag'})


def _kiosk(key, url, body=None, method='post'):
    from tests.punch.api import Res
    c = Client()
    if method == 'get':
        return Res(c.get(f'/api/v1/kiosk{url}', **({'HTTP_X_KIOSK_KEY': key} if key else {})))
    return Res(c.post(f'/api/v1/kiosk{url}', json.dumps(body or {}), content_type='application/json',
                      **({'HTTP_X_KIOSK_KEY': key} if key else {})))


def test_kiosks(demo, w):
    hr, sita, ram, foreign = w['hr'], w['sita'], w['ram'], w['foreign']
    # HR creates a kiosk with a one-time pairing code
    assert post(sita, '/field/kiosks', {'name': 'Lobby'}).status == 403
    c = post(hr, '/field/kiosks', {'name': 'Lobby tablet'})
    assert c.status == 201
    assert re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', c.body['pairingCode'])
    kiosk_id, code = c.body['id'], c.body['pairingCode']
    assert len(get(foreign, '/field/kiosks').body['kiosks']) == 0
    k0 = get(hr, '/field/kiosks').body['kiosks'][0]
    assert k0['name'] == 'Lobby tablet' and k0['status'] == 'pairing' and k0['pairing_open'] is True

    # pairs once; the key authenticates the tablet; no key, no access
    assert _kiosk(None, '/pair', {'code': 'AAAA-BBBB'}).status == 400
    p = _kiosk(None, '/pair', {'code': code.lower()})
    assert p.status == 200
    key = p.body['key']
    assert _kiosk(None, '/pair', {'code': code}).status == 400
    assert _kiosk(None, '/session', method='get').status == 401
    s = _kiosk(key, '/session', method='get')
    assert s.body['kiosk']['name'] == 'Lobby tablet' and s.body['kiosk']['selfieRequired'] is True
    raw = one('SELECT key_hash FROM kiosk WHERE id = %s', [kiosk_id])
    assert key not in raw['key_hash']

    # PIN: weak ones refused; wrong PINs counted and locked; HR resets
    assert _kiosk(key, '/identify', {'empCode': 'E002', 'pin': '4826'}).status == 400  # no PIN set yet
    assert put(ram, '/field/kiosk-pin', {'pin': '1234'}).status == 400
    assert put(ram, '/field/kiosk-pin', {'pin': '12a4'}).status == 400
    assert put(ram, '/field/kiosk-pin', {'pin': '4826'}).status == 200
    for i in range(1, 5):
        wr = _kiosk(key, '/identify', {'empCode': 'E002', 'pin': '1111'})
        assert wr.status == 400
        assert f'{5 - i} tr' in wr.body['error']
    assert _kiosk(key, '/identify', {'empCode': 'E002', 'pin': '1111'}).status == 429
    assert _kiosk(key, '/identify', {'empCode': 'E002', 'pin': '4826'}).status == 429
    assert post(ram, f"/field/staff/{demo['ram']}/kiosk-reset").status == 403
    assert post(hr, f"/field/staff/{demo['ram']}/kiosk-reset").status == 200
    assert put(ram, '/field/kiosk-pin', {'pin': '4826'}).status == 200

    # identify -> selfie -> punch at server time; tickets are bound to the kiosk and single-use
    ident = _kiosk(key, '/identify', {'empCode': 'e002', 'pin': '4826'})
    assert ident.status == 200
    assert ident.body['employee']['name'] == 'Ram Thapa' and ident.body['employee']['empCode'] == 'E002'
    assert ident.body['nextDirection'] == 'in' and ident.body['selfieRequired'] is True
    no_photo = _kiosk(key, '/punch', {'ticket': ident.body['ticket']})
    assert no_photo.body['details'] == {'code': 'photo_required'}
    ok = _kiosk(key, '/punch', {'ticket': ident.body['ticket'], 'selfie': SELFIE})
    assert ok.status == 201
    assert ok.body['direction'] == 'in'
    again = _kiosk(key, '/punch', {'ticket': ident.body['ticket'], 'selfie': SELFIE})
    assert again.body['duplicate'] is True
    t = ident.body['ticket']
    tampered = t[:-1] + ('b' if t[-1] == 'a' else 'a')
    assert _kiosk(key, '/punch', {'ticket': tampered, 'selfie': SELFIE}).status == 400
    assert rows("SELECT source, raw->>'kioskId' AS kiosk FROM punch_event WHERE employee_id = %s", [demo['ram']]) == [{'source': 'kiosk', 'kiosk': kiosk_id}]
    assert one('SELECT count(*)::int AS n FROM punch_photo WHERE employee_id = %s', [demo['ram']])['n'] == 1

    # QR badge: valid once, tampered refused
    qr = get(ram, '/field/kiosk-qr')
    assert qr.body['token'].startswith('HQ1.')
    ident = _kiosk(key, '/identify', {'qr': qr.body['token']})
    assert ident.status == 200
    assert ident.body['nextDirection'] == 'out'
    assert 'already used' in _kiosk(key, '/identify', {'qr': qr.body['token']}).body['error']
    forged = qr.body['token'].replace(demo['ram'], demo['sita'])
    assert _kiosk(key, '/identify', {'qr': forged}).status == 400

    # revoked kiosks stop working
    assert post(foreign, f'/field/kiosks/{kiosk_id}/revoke').status == 404
    assert post(hr, f'/field/kiosks/{kiosk_id}/revoke').status == 200
    assert _kiosk(key, '/session', method='get').status == 401
    assert post(hr, f'/field/kiosks/{kiosk_id}/pairing-code').status == 409


def test_shift_swaps(demo, w):
    sita, ram, hari, hari_id = w['sita'], w['ram'], w['hari'], w['hari_id']
    night = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'NIGHT', 'Night', '22:00', '06:00') RETURNING id",
                    [demo['company_id']])['id'])
    execute('DELETE FROM roster')
    day = add_days(today(), 3)

    # same shift cannot be swapped; colleagues list shows shifts
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)',
            [demo['sita'], day, demo['shift_id'], hari_id, day, demo['shift_id']])
    assert 'same shift' in post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': day}).body['error']
    execute('UPDATE roster SET shift_id = %s WHERE employee_id = %s AND local_date = %s', [night, hari_id, day])
    c = get(sita, '/field/swap-colleagues', {'date': day})
    assert next(x for x in c.body['colleagues'] if x['id'] == hari_id)['shift']['code'] == 'NIGHT'

    # request -> colleague accepts -> manager approves -> roster swapped
    r = post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': day, 'reason': 'Family event'})
    assert r.status == 201
    assert post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': day}).status == 409
    assert post(sita, f"/field/swaps/{r.body['id']}/accept").status == 404  # only the colleague accepts
    assert post(hari, f"/field/swaps/{r.body['id']}/accept", {'note': 'Fine'}).status == 200
    inbox = get(ram, '/workflow/inbox')
    item = next(i for i in inbox.body['items'] if i['objectType'] == 'shift_swap')
    assert item['summary']['colleague_name'] == 'Hari Karki'
    assert item['summary']['requester_shift_code'] == 'GEN' and item['summary']['colleague_shift_code'] == 'NIGHT'
    assert post(ram, f"/workflow/{item['instanceId']}/approve").status == 200
    roster = {str(x['employee_id']): str(x['shift_id']) for x in rows('SELECT employee_id, shift_id FROM roster WHERE local_date = %s', [day])}
    assert roster == {demo['sita']: night, hari_id: demo['shift_id']}
    assert get(sita, '/field/swaps').body['swaps'][0]['status'] == 'approved'
    mine = get(sita, '/field/my-roster', {'from': day, 'to': day})
    assert mine.body['days'][0]['shift']['code'] == 'NIGHT'
    assert len(get(ram, '/field/swaps', {'scope': 'team'}).body['swaps']) == 1
    assert get(sita, '/field/swaps', {'scope': 'team'}).status == 403

    # decline and cancel close the request; other companies and past days refused
    d2 = add_days(today(), 4)
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)',
            [demo['sita'], d2, demo['shift_id'], hari_id, d2, night])
    a = post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': d2})
    assert post(hari, f"/field/swaps/{a.body['id']}/decline", {'note': 'Busy'}).body['status'] == 'declined'
    b = post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': d2})
    assert b.status == 201
    assert post(hari, f"/field/swaps/{b.body['id']}/cancel").status == 404
    assert post(sita, f"/field/swaps/{b.body['id']}/cancel").body['status'] == 'cancelled'
    assert post(sita, '/field/swaps', {'colleagueId': hari_id, 'localDate': add_days(today(), -1)}).status == 400
    foreign = str(one("INSERT INTO employee (company_id, emp_code, name_en) SELECT id, 'Z1', 'Zed' FROM company WHERE code = 'FLD2' RETURNING id")['id'])
    assert post(sita, '/field/swaps', {'colleagueId': foreign, 'localDate': d2}).status == 404


def test_field_feature_off_gates_breaks_visits_and_kiosks(demo, w):
    """featureGate(db, 'field', ['/breaks', '/visits', '/kiosks', '/kiosk-pin', '/kiosk-qr'])."""
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["field"]}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    from hajiri.features.service import invalidate_features
    invalidate_features(demo['company_id'])
    for url in ('/field/kiosks', '/field/kiosk-qr', '/field/breaks?from=2026-10-01&to=2026-10-01'):
        r = get(w['hr'], url)
        assert r.status == 404 and r.body['error'] == 'feature off'
    assert get(w['hr'], '/field/policy').status == 200
    assert get(w['hr'], '/field/locations').status == 200
