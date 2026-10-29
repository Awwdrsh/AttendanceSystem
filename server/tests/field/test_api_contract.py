"""The exact JSON the React pages read from the field, punch and kiosk endpoints, key for key as the TS routes returned
them (git show 65a9079:apps/api/src/modules/{field,punch,kiosk}/routes.ts): row keys, booleans that are never null,
dates as 'YYYY-MM-DD' strings, numbers as numbers. The cookie session of the browser is used throughout."""
from __future__ import annotations

import re

from tests.punch.api import SELFIE, Browser, add_days, now_iso, today
from tests.support.sql import execute, one

OFFICE = {'lat': 27.7172, 'lng': 85.3240, 'accuracyM': 20}
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
KEY = 'HTTP_X_KIOSK_KEY'


def _instant(v):
    return isinstance(v, str) and INSTANT.match(v)


def test_field_hr_shapes(demo, login_as):
    hr = Browser(login_as(['hr']).token)
    loc = hr.post('/field/locations', {'name': 'Head office', 'lat': 27.7172, 'lng': 85.324, 'radiusM': 150})
    assert loc.status == 201 and set(loc.body) == {'id'}
    assert hr.put(f"/field/staff/{demo['sita']}", {'fieldWork': True, 'locationIds': [loc.body['id']]}).body == {'ok': True}

    locs = hr.get('/field/locations').body['locations']
    assert set(locs[0]) == {'id', 'name', 'address', 'branch_id', 'branch_name', 'lat', 'lng', 'radius_m', 'active', 'employee_ids'}
    assert locs[0]['lat'] == 27.7172 and isinstance(locs[0]['lng'], float) and locs[0]['radius_m'] == 150
    assert locs[0]['employee_ids'] == [demo['sita']] and locs[0]['active'] is True and locs[0]['address'] is None

    staff = {s['id']: s for s in hr.get('/field/staff').body['staff']}
    s = staff[demo['sita']]
    assert set(s) == {'id', 'emp_code', 'name_en', 'designation', 'field_work', 'work_from_home', 'branch_name', 'location_ids',
                      'kiosk_pin_set', 'kiosk_pin_locked'}
    assert s['field_work'] is True and s['location_ids'] == [loc.body['id']]
    assert s['kiosk_pin_set'] is False and s['kiosk_pin_locked'] is False
    assert staff[demo['ram']]['location_ids'] == []

    k = hr.post('/field/kiosks', {'name': 'Gate'})
    assert k.status == 201 and set(k.body) == {'id', 'pairingCode', 'expiresAt'} and _instant(k.body['expiresAt'])
    pc = hr.post(f"/field/kiosks/{k.body['id']}/pairing-code")
    assert set(pc.body) == {'pairingCode', 'expiresAt'}
    ks = hr.get('/field/kiosks').body['kiosks']
    assert set(ks[0]) == {'id', 'name', 'branch_id', 'branch_name', 'location_id', 'location_name', 'selfie_required', 'status',
                          'paired_at', 'last_seen_at', 'created_at', 'revoked_at', 'locked', 'pairing_open', 'pairing_expires_at',
                          'punches_today'}
    # never null: the TS SQL wrote (x IS NOT NULL AND x > now())
    assert ks[0]['locked'] is False and ks[0]['pairing_open'] is True and ks[0]['punches_today'] == 0
    assert ks[0]['status'] == 'pairing' and ks[0]['paired_at'] is None
    assert hr.post(f"/field/kiosks/{k.body['id']}/revoke").body == {'ok': True}
    ks = hr.get('/field/kiosks').body['kiosks']
    assert ks[0]['pairing_open'] is False and ks[0]['locked'] is False

    pol = hr.get('/field/policy').body['policy']
    assert set(pol) >= {'geofenceMode', 'breaks', 'fieldPhotoRequired', 'fieldVisitClockIn'}
    assert set(pol['breaks']) == {'lunch', 'tea', 'prayer', 'personal'}
    assert set(pol['breaks']['tea']) == {'enabled', 'paid', 'maxMin'}


def test_field_employee_shapes(demo, login_as):
    execute('UPDATE employee SET field_work = true WHERE id = %s', [demo['ram']])
    ram = Browser(login_as(['employee', 'manager'], demo['ram']).token)
    hr = Browser(login_as(['hr']).token)

    me = ram.get('/field/me').body
    assert set(me) == {'date', 'clockedIn', 'fieldWork', 'fieldPhotoRequired', 'breakTypes', 'breaks', 'openBreak', 'visits',
                       'openVisit', 'kioskPin'}
    assert DATE.match(me['date']) and me['clockedIn'] is False and me['fieldWork'] is True
    assert me['kioskPin'] == {'set': False, 'lockedUntil': None}
    assert me['openBreak'] is None and me['openVisit'] is None
    assert all(set(t) == {'kind', 'paid', 'maxMin'} for t in me['breakTypes'])

    p = ram.post('/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'contract-in-1', 'source': 'web', **OFFICE})
    assert p.status == 201
    kind = me['breakTypes'][0]['kind']
    b = ram.post('/field/breaks/start', {'kind': kind})
    assert b.status == 201 and set(b.body['break']) == {'id', 'kind', 'paid', 'max_min', 'started_at'}
    assert _instant(b.body['break']['started_at'])
    me = ram.get('/field/me').body
    assert set(me['openBreak']) == {'id', 'kind', 'paid', 'max_min', 'started_at', 'ended_at', 'minutes', 'over_limit'}
    e = ram.post('/field/breaks/end')
    assert set(e.body['break']) == {'id', 'kind', 'minutes', 'max_min', 'over_limit', 'started_at', 'ended_at'}
    assert e.body['break']['minutes'] == 0 and e.body['break']['over_limit'] is False

    rng = {'from': today(), 'to': today()}
    br = hr.get('/field/breaks', rng).body['breaks'][0]
    assert set(br) == {'id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'kind', 'paid', 'max_min', 'started_at',
                       'ended_at', 'minutes', 'over_limit', 'open'}
    assert br['local_date'] == today() and br['open'] is False and br['minutes'] == 0
    assert hr.get('/field/breaks', {**rng, 'overOnly': 1}).body['breaks'] == []

    v = ram.post('/field/visits/start', {'place': 'Client A', 'photo': SELFIE, 'lat': 27.8, 'lng': 85.4, 'accuracyM': 20.6})
    assert v.status == 201 and set(v.body) == {'visit', 'clockedIn', 'message'}
    assert set(v.body['visit']) == {'id', 'place', 'purpose', 'started_at'} and v.body['visit']['purpose'] is None
    assert v.body['clockedIn'] is False             # already clocked in today
    me = ram.get('/field/me').body
    assert set(me['openVisit']) == {'id', 'place', 'purpose', 'started_at', 'ended_at', 'outcome', 'has_photo'}
    assert me['openVisit']['has_photo'] is True
    end = ram.post(f"/field/visits/{v.body['visit']['id']}/end", {'outcome': 'Order', 'lat': 27.8, 'lng': 85.4})
    assert set(end.body['visit']) == {'id', 'place', 'started_at', 'ended_at', 'outcome'}
    vis = hr.get('/field/visits', rng).body['visits'][0]
    assert set(vis) == {'id', 'employee_id', 'emp_code', 'name_en', 'designation', 'local_date', 'place', 'purpose', 'started_at',
                        'ended_at', 'outcome', 'start_lat', 'start_lng', 'start_accuracy_m', 'end_lat', 'end_lng', 'end_accuracy_m',
                        'has_photo', 'opened_day', 'minutes'}
    assert vis['local_date'] == today() and vis['start_lat'] == 27.8 and vis['start_accuracy_m'] == 21
    assert vis['end_accuracy_m'] is None and vis['opened_day'] is False and vis['has_photo'] is True and vis['minutes'] == 0

    assert ram.put('/field/kiosk-pin', {'pin': '4826'}).body == {'ok': True}
    assert ram.get('/field/me').body['kioskPin'] == {'set': True, 'lockedUntil': None}
    qr = ram.get('/field/kiosk-qr').body
    assert set(qr) == {'token', 'refreshInMs', 'empCode'} and isinstance(qr['refreshInMs'], int) and qr['empCode'] == 'E002'


def test_roster_and_swap_shapes(demo, login_as):
    sita = Browser(login_as(['employee'], demo['sita']).token)
    ram = Browser(login_as(['employee', 'manager'], demo['ram']).token)
    night = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'NIGHT', 'Night', '22:00', '06:00') RETURNING id",
                    [demo['company_id']])['id'])
    day = add_days(today(), 3)
    execute('DELETE FROM roster')
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)',
            [demo['sita'], day, demo['shift_id'], demo['ram'], day, night])
    d = sita.get('/field/my-roster', {'from': day, 'to': day}).body['days'][0]
    assert set(d) == {'date', 'weekday', 'source', 'shift', 'holidays'}
    assert d['date'] == day and d['source'] == 'roster' and isinstance(d['weekday'], int) and d['holidays'] == []
    assert set(d['shift']) == {'id', 'code', 'name_en', 'start', 'end'} and re.match(r'^\d{2}:\d{2}$', d['shift']['start'])

    col = sita.get('/field/swap-colleagues', {'date': day}).body
    assert set(col) == {'mine', 'colleagues'} and set(col['mine']) == {'shift', 'off'} and col['mine']['off'] is False
    c = next(x for x in col['colleagues'] if x['id'] == demo['ram'])
    assert set(c) == {'id', 'emp_code', 'name_en', 'designation', 'shift', 'off', 'sameAsMine'}
    assert c['shift']['start'] == '22:00' and c['sameAsMine'] is False

    r = sita.post('/field/swaps', {'colleagueId': demo['ram'], 'localDate': day})
    assert r.status == 201 and set(r.body) == {'id'}
    s = sita.get('/field/swaps').body['swaps'][0]
    assert set(s) == {'id', 'local_date', 'status', 'reason', 'colleague_note', 'created_at', 'decided_at', 'workflow_instance_id',
                      'requester_id', 'requester_name', 'requester_code', 'colleague_id', 'colleague_name', 'colleague_code',
                      'requester_shift_code', 'requester_shift_name', 'colleague_shift_code', 'colleague_shift_name'}
    assert s['local_date'] == day and s['status'] == 'awaiting_colleague' and s['colleague_shift_code'] == 'NIGHT'
    assert ram.post(f"/field/swaps/{r.body['id']}/accept", {}).body == {'status': 'pending'}
    hr = Browser(login_as(['hr']).token)
    team = hr.get('/field/swaps', {'scope': 'team', 'from': today(), 'to': add_days(today(), 30)}).body['swaps']
    assert [x['id'] for x in team] == [r.body['id']] and team[0]['workflow_instance_id']


def test_punch_shapes(demo, login_as):
    sita = Browser(login_as(['employee'], demo['sita']).token)
    hr = Browser(login_as(['hr']).token)
    body = {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'contract-p-1', 'source': 'web', **OFFICE}
    p = sita.post('/punch', body)
    assert p.status == 201
    assert set(p.body) == {'duplicate', 'punch', 'flags', 'location', 'day', 'remote', 'photoId', 'message'}
    assert set(p.body['punch']) == {'id', 'punched_at', 'local_date', 'direction'} and DATE.match(p.body['punch']['local_date'])
    assert _instant(p.body['punch']['punched_at'])
    assert set(p.body['location']) == {'inside', 'name', 'distanceM'} and p.body['remote'] is False
    assert p.body['message'].startswith('Clocked in at ')
    dup = sita.post('/punch', body)
    assert dup.status == 200 and set(dup.body) == {'duplicate', 'punch', 'day'} and dup.body['duplicate'] is True
    assert set(dup.body['punch']) == {'id', 'punched_at', 'local_date'}

    t = sita.get('/punch/today').body
    assert set(t) == {'date', 'day', 'punches'} and t['date'] == today()
    assert set(t['punches'][0]) == {'id', 'punched_at', 'direction', 'verify_mode', 'source', 'device_sn', 'remote', 'photo_id'}
    assert t['punches'][0]['remote'] is False and t['punches'][0]['photo_id'] is None
    assert {'status', 'first_in', 'last_out', 'worked_min', 'flags', 'shift_id'} <= set(t['day'])

    # a clock-in from home with its photo, then the remote check-ins list
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['ram']])
    ram = Browser(login_as(['employee', 'manager'], demo['ram']).token)
    h = ram.post('/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'contract-h-1', 'source': 'web',
                            'lat': 27.8, 'lng': 85.4, 'accuracyM': 30, 'selfie': SELFIE})
    assert h.status == 201 and h.body['remote'] is True and h.body['photoId']
    rows = hr.get('/punch/remote', {'from': today(), 'to': today()}).body['punches']
    assert set(rows[0]) == {'punch_id', 'employee_id', 'emp_code', 'name_en', 'designation', 'punched_at', 'local_date',
                            'direction', 'photo_id', 'photo_purged', 'location_shared', 'accuracy_m', 'reason', 'flags'}
    assert rows[0]['local_date'] == today() and rows[0]['photo_purged'] is False and rows[0]['location_shared'] is True
    assert rows[0]['accuracy_m'] == 30 and 'Remote' in rows[0]['flags'] and rows[0]['photo_id'] == h.body['photoId']
    assert ram.get('/punch/today').body['punches'][0]['photo_id'] == h.body['photoId']

    m = hr.post('/punch/manual', {'employeeId': demo['sita'], 'localDate': today(), 'time': '18:00', 'direction': 'out',
                                  'reason': 'Forgot'})
    assert m.status == 201 and set(m.body) == {'duplicate', 'punchId', 'day'} and m.body['day']['status']
    again = hr.post('/punch/manual', {'employeeId': demo['sita'], 'localDate': today(), 'time': '18:00', 'direction': 'out',
                                      'reason': 'Forgot'})
    assert again.status == 200 and again.body == {'duplicate': True}


def test_kiosk_shapes(demo, login_as):
    hr = Browser(login_as(['hr']).token)
    ram = Browser(login_as(['employee', 'manager'], demo['ram']).token)
    k = hr.post('/field/kiosks', {'name': 'Gate', 'selfieRequired': False})
    p = hr.post('/kiosk/pair', {'code': k.body['pairingCode']})
    assert set(p.body) == {'key', 'kiosk'}
    key = {KEY: p.body['key']}
    s = hr.get('/kiosk/session', **key).body
    assert set(s) == {'kiosk', 'company', 'serverTime'} and _instant(s['serverTime'])
    assert set(s['kiosk']) == {'id', 'name', 'selfieRequired', 'lockedUntil'} and s['kiosk']['lockedUntil'] is None
    assert set(s['company']) == {'name'}
    assert ram.put('/field/kiosk-pin', {'pin': '4826'}).status == 200
    who = hr.post('/kiosk/identify', {'empCode': 'e002', 'pin': '4826'}, **key).body
    assert set(who) == {'ticket', 'employee', 'nextDirection', 'selfieRequired', 'openBreak'}
    assert set(who['employee']) == {'name', 'empCode', 'designation'} and who['openBreak'] is None
    assert who['selfieRequired'] is False
    r = hr.post('/kiosk/punch', {'ticket': who['ticket']}, **key)
    assert r.status == 201 and set(r.body) == {'duplicate', 'direction', 'punchedAt', 'name', 'message'}
    assert _instant(r.body['punchedAt'])
    ks = hr.get('/field/kiosks').body['kiosks'][0]
    assert ks['status'] == 'active' and ks['punches_today'] == 1 and _instant(ks['paired_at'])


def test_field_feature_off_over_the_cookie(demo, login_as):
    """featureGate(db, 'field', [...]): 404 {error: 'feature off', feature: 'field'}; policy, locations, staff and swaps
    stay open."""
    hr = Browser(login_as(['hr']).token)
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["field"]}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    from hajiri.features.service import invalidate_features
    invalidate_features(demo['company_id'])
    for r in (hr.get('/field/kiosks'), hr.post('/field/kiosks', {'name': 'X'}), hr.get('/field/visits', {'from': today(), 'to': today()}),
              hr.put('/field/kiosk-pin', {'pin': '4826'}), hr.post('/field/breaks/start', {'kind': 'tea'})):
        assert r.status == 404 and r.body == {'error': 'feature off', 'feature': 'field'}
    for url in ('/field/policy', '/field/locations', '/field/staff', '/field/swaps'):
        assert hr.get(url).status == 200, url


def test_the_pages_bodies_with_nulls(demo, login_as):
    """LocationsPage and KiosksPage send address/branchId/locationId as null when they are not picked."""
    hr = Browser(login_as(['hr']).token)
    body = {'name': 'Depot', 'address': None, 'branchId': None, 'lat': 27.123456, 'lng': 85.654321, 'radiusM': 150, 'active': True}
    c = hr.post('/field/locations', body)
    assert c.status == 201, c.body
    assert hr.put(f"/field/locations/{c.body['id']}", {**body, 'branchId': demo['branch_id'], 'active': False}).body == {'ok': True}
    loc = hr.get('/field/locations').body['locations'][0]
    assert (loc['lat'], loc['lng'], loc['active'], loc['branch_id']) == (27.123456, 85.654321, False, demo['branch_id'])
    assert loc['branch_name']
    k = hr.post('/field/kiosks', {'name': 'Gate', 'branchId': None, 'locationId': None, 'selfieRequired': True})
    assert k.status == 201, k.body
    assert hr.put(f"/field/kiosks/{k.body['id']}", {'name': 'Gate 2', 'branchId': demo['branch_id'], 'locationId': c.body['id'],
                                                    'selfieRequired': False}).body == {'ok': True}
    ks = hr.get('/field/kiosks').body['kiosks'][0]
    assert (ks['name'], ks['location_id'], ks['location_name'], ks['selfie_required']) == ('Gate 2', c.body['id'], 'Depot', False)
