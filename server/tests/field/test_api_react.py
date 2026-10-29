"""The field endpoints as the React app calls them (frontend/src/api/field.js): the session cookie from fetch() with no
CSRF token. Every write the app makes passes from the site's own origin and is refused from a foreign one with the TS
message; the shapes the pages read are there; another company's records are not found."""
from __future__ import annotations

from hajiri.core import auth
from hajiri.core.auth import create_user
from tests.core.fixtures import PW
from tests.punch.api import CROSS_SITE, SELFIE, Browser, add_days, now_iso, today
from tests.support.sql import execute, one

OFFICE = {'lat': 27.7172, 'lng': 85.3240, 'accuracyM': 20}


def _other_company_hr():
    from hajiri.db.models import Company
    c = Company.objects.create(code='FRC2', name_en='Other Co')
    create_user(company_id=str(c.id), employee_id=None, email='hr@frc2.local', name='Other HR', password=PW, roles=['hr'])
    return Browser(auth.login('hr@frc2.local', PW).token)


def test_hr_settings_locations_staff_and_kiosks_with_the_cookie(demo, login_as):
    tok = login_as(['hr']).token
    hr, evil = Browser(tok), Browser(tok, origin='https://evil.example')
    other = _other_company_hr()

    # Settings > Locations: rules
    p = hr.get('/field/policy')
    assert p.status == 200 and p.body['policy']['geofenceMode'] == 'flag'
    assert evil.put('/field/policy', {'geofenceMode': 'block'}).body == CROSS_SITE
    r = hr.put('/field/policy', {**p.body['policy'], 'geofenceMode': 'block'})
    assert r.status == 200 and r.body['policy']['geofenceMode'] == 'block'
    hr.put('/field/policy', {'geofenceMode': 'flag'})

    # locations
    c = hr.post('/field/locations', {'name': 'Head office', 'address': 'Kathmandu', 'lat': 27.7172, 'lng': 85.3240, 'radiusM': 150})
    assert c.status == 201, c.body
    loc = c.body['id']
    assert evil.post('/field/locations', {'name': 'X', 'lat': 1, 'lng': 1}).status == 403
    assert hr.get('/field/locations').body['locations'][0]['name'] == 'Head office'
    assert hr.put(f'/field/locations/{loc}', {'name': 'HQ', 'lat': 27.7172, 'lng': 85.3240, 'radiusM': 200}).status == 200
    assert other.put(f'/field/locations/{loc}', {'name': 'Hijack', 'lat': 1, 'lng': 1}).status == 404
    assert other.delete(f'/field/locations/{loc}').status == 404
    assert evil.delete(f'/field/locations/{loc}').status == 403
    assert hr.get('/field/locations').body['locations'][0]['name'] == 'HQ'

    # staff: field work and kiosk PIN reset
    staff = hr.get('/field/staff')
    assert staff.status == 200 and any(s['id'] == demo['sita'] for s in staff.body['staff'])
    assert hr.put(f"/field/staff/{demo['sita']}", {'fieldWork': True}).status == 200
    assert other.put(f"/field/staff/{demo['sita']}", {'fieldWork': False}).status == 404
    assert hr.post(f"/field/staff/{demo['sita']}/kiosk-reset").status == 200
    assert other.post(f"/field/staff/{demo['sita']}/kiosk-reset").status == 404

    # kiosks: create, rename, new code, revoke, unlock
    k = hr.post('/field/kiosks', {'name': 'Lobby tablet'})
    assert k.status == 201 and k.body['pairingCode']
    kid = k.body['id']
    assert evil.post('/field/kiosks', {'name': 'X'}).status == 403
    assert hr.get('/field/kiosks').body['kiosks'][0]['name'] == 'Lobby tablet'
    assert hr.put(f'/field/kiosks/{kid}', {'name': 'Gate'}).status == 200
    assert hr.post(f'/field/kiosks/{kid}/pairing-code').status == 200
    assert other.post(f'/field/kiosks/{kid}/pairing-code').status == 404
    assert hr.post(f'/field/kiosks/{kid}/unlock').status == 200
    assert hr.post(f'/field/kiosks/{kid}/revoke').status == 200
    assert other.get('/field/kiosks').body['kiosks'] == []

    # Origin-less same-site requests pass (the TS sameOriginGuard)
    assert Browser(tok, origin=None).delete(f'/field/locations/{loc}').status == 200


def test_my_day_breaks_visits_badge_and_swaps_with_the_cookie(demo, login_as):
    execute("INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'Head office', 27.7172, 85.3240, 150)",
            [demo['company_id']])
    execute('UPDATE employee SET field_work = true WHERE id = %s', [demo['ram']])
    sita = Browser(login_as(['employee'], demo['sita']).token)
    ram_tok = login_as(['employee', 'manager'], demo['ram']).token
    ram = Browser(ram_tok)
    hr = Browser(login_as(['hr']).token)
    other = _other_company_hr()

    # clock in, then a tea break from the home card
    assert sita.post('/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'rct-in-0001', 'source': 'web',
                                **OFFICE}).status == 201
    me = sita.get('/field/me')
    assert me.status == 200 and me.body['clockedIn'] is True
    assert set(me.body) >= {'date', 'fieldWork', 'fieldPhotoRequired', 'breakTypes', 'breaks', 'openBreak', 'visits',
                            'openVisit', 'kioskPin'}
    assert Browser(sita.c.cookies['hajiri_session'].value, origin='https://evil.example').post(
        '/field/breaks/start', {'kind': 'tea'}).body == CROSS_SITE
    s = sita.post('/field/breaks/start', {'kind': 'tea'})
    assert s.status == 201 and s.body['break']['kind'] == 'tea'
    assert sita.get('/field/me').body['openBreak']['kind'] == 'tea'
    e = sita.post('/field/breaks/end')
    assert e.status == 200 and e.body['break']['ended_at']
    rng = {'from': today(), 'to': today()}
    assert len(hr.get('/field/breaks', rng).body['breaks']) == 1
    csv = hr.get('/field/breaks', {**rng, 'format': 'csv'})
    assert csv.status == 200 and 'text/csv' in csv.headers['Content-Type']
    assert other.get('/field/breaks', rng).body['breaks'] == []

    # a field visit with a photo (Ram is field staff)
    v = ram.post('/field/visits/start', {'place': 'Client A', 'purpose': 'Demo', 'photo': SELFIE, 'lat': 27.8, 'lng': 85.4,
                                          'accuracyM': 20})
    assert v.status == 201, v.body
    vid = v.body['visit']['id']
    assert ram.get(f'/field/visits/{vid}/photo').status == 200
    assert other.get(f'/field/visits/{vid}/photo').status == 404
    assert sita.post(f'/field/visits/{vid}/end', {}).status == 404          # only the visitor ends it
    end = ram.post(f'/field/visits/{vid}/end', {'outcome': 'Order placed', 'lat': 27.8, 'lng': 85.4})
    assert end.status == 200 and end.body['visit']['outcome'] == 'Order placed'
    lst = hr.get('/field/visits', rng)
    assert lst.body['visits'][0]['place'] == 'Client A' and lst.body['visits'][0]['has_photo'] is True
    assert 'Client A' in hr.get('/field/visits', {**rng, 'format': 'csv'}).text

    # kiosk PIN and QR badge
    assert ram.put('/field/kiosk-pin', {'pin': '1234'}).status == 400
    assert ram.put('/field/kiosk-pin', {'pin': '4826'}).status == 200
    qr = ram.get('/field/kiosk-qr')
    assert qr.status == 200 and qr.headers['Cache-Control'] == 'no-store'

    # roster and a shift swap request, cancelled by the requester
    night = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'NIGHT', 'Night', '22:00', '06:00') RETURNING id",
                    [demo['company_id']])['id'])
    day = add_days(today(), 3)
    execute('DELETE FROM roster')
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)',
            [demo['sita'], day, demo['shift_id'], demo['ram'], day, night])
    mine = sita.get('/field/my-roster', {'from': day, 'to': day})
    assert mine.status == 200 and mine.body['days'][0]['shift']['code'] == 'GEN'
    col = sita.get('/field/swap-colleagues', {'date': day})
    assert col.status == 200 and next(x for x in col.body['colleagues'] if x['id'] == demo['ram'])['shift']['code'] == 'NIGHT'
    r = sita.post('/field/swaps', {'colleagueId': demo['ram'], 'localDate': day, 'reason': 'Family event'})
    assert r.status == 201, r.body
    assert sita.post(f"/field/swaps/{r.body['id']}/accept", {}).status == 404     # only the colleague accepts
    assert sita.get('/field/swaps').body['swaps'][0]['id'] == r.body['id']
    assert sita.post(f"/field/swaps/{r.body['id']}/cancel").body['status'] == 'cancelled'
    d2 = add_days(today(), 4)
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)',
            [demo['sita'], d2, demo['shift_id'], demo['ram'], d2, night])
    r = sita.post('/field/swaps', {'colleagueId': demo['ram'], 'localDate': d2})
    assert ram.post(f"/field/swaps/{r.body['id']}/decline", {'note': 'Busy'}).body['status'] == 'declined'
