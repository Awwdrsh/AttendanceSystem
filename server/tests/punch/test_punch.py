"""Ports of apps/api/test/punch.test.ts, wfh-photo.test.ts, work-from-home.test.ts and the punch case of
fixes-attendance.test.ts (through the JSON API /api/v1/punch, as the TS tests)."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from engines.attendance import local_date_of

from hajiri.attendance import api as att_api
from tests.support.sql import execute, one, rows
from hajiri.core.auth import create_user
from hajiri.worker.tasks import purge_expired_photos
from tests.core.fixtures import PW
from tests.punch.api import HOME, JPEG, OFFICE, SELFIE, get, now_iso, patch, post, punch, today

HQ = "INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'Head office', 27.7172, 85.3240, 150)"


# ================================================================ punch.test.ts

def test_post_punch(demo, login_as):
    sita = login_as(['employee'], demo['sita']).token
    hr = login_as(['hr']).token
    execute(HQ, [demo['company_id']])

    # rejects mocked locations with a plain message
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'mock-0001', 'lat': 27.7172, 'lng': 85.324,
                              'mocked': True, 'source': 'mobile'})
    assert r.status == 400
    assert 'fake location' in r.body['error']

    # inside the geofence: stores the punch, recomputes and returns the day
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'in-000001', 'lat': 27.7173, 'lng': 85.3241,
                              'accuracyM': 20, 'source': 'mobile'})
    assert r.status == 201
    assert r.body['duplicate'] is False
    assert r.body['location']['inside'] is True
    assert r.body['day']['localDate'] == today()
    import re
    assert re.fullmatch(r'Clocked in at \d\d:\d\d', r.body['message'])
    dev = one("SELECT serial_no, vendor FROM device WHERE serial_no = 'MOBILE-DEMO'")
    assert dev['vendor'] == 'mobile'

    # replaying the same idempotency key is a no-op
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'in-000001', 'lat': 27.7173, 'lng': 85.3241,
                              'source': 'mobile'})
    assert r.status == 200
    assert r.body['duplicate'] is True
    assert one('SELECT count(*)::int AS n FROM punch_event WHERE employee_id = %s', [demo['sita']])['n'] == 1

    # outside the geofence requires a reason, then is accepted and flagged
    body = {'direction': 'out', 'clientTime': now_iso(), 'idempotencyKey': 'out-00001', 'lat': 27.80, 'lng': 85.40, 'source': 'mobile'}
    no_reason = post(sita, '/punch', body)
    assert no_reason.status == 400
    assert 'm from Head office' in no_reason.body['error']
    ok = post(sita, '/punch', {**body, 'reason': 'Client site visit'})
    assert ok.status == 201
    assert 'OutsideLocation' in ok.body['flags']
    assert ok.body['location']['inside'] is False

    # a wildly wrong client clock is replaced by server time and flagged
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': '2020-01-01T00:00:00Z', 'idempotencyKey': 'skew-0001', 'lat': 27.7173,
                              'lng': 85.3241, 'source': 'web'})
    assert r.status == 201
    assert 'ClockSkew' in r.body['flags']
    assert r.body['punch']['local_date'] == today()

    # GET /punch/today returns the day and raw punches; users without an employee record get a clear error
    t = get(sita, '/punch/today')
    assert t.status == 200
    assert len(t.body['punches']) >= 3
    assert t.body['day'] is not None
    assert get(hr, '/punch/today').status == 400

    # HR manual punch is audited and recomputed; employees cannot use it
    body = {'employeeId': demo['ram'], 'localDate': '2026-09-21', 'time': '10:00', 'direction': 'in', 'reason': 'Device was down'}
    assert post(sita, '/punch/manual', body).status == 403
    r = post(hr, '/punch/manual', body)
    assert r.status == 201
    assert r.body['day']['status'] == 'Incomplete'
    assert len(rows("SELECT 1 AS x FROM audit_log WHERE action = 'manual_punch' AND row_id = %s", [r.body['punchId']])) == 1
    assert post(hr, '/punch/manual', body).body['duplicate'] is True


# ================================================================ wfh-photo.test.ts

@pytest.fixture
def photo_world(demo, login_as):
    from hajiri.db.models import Company
    hr = login_as(['hr']).token
    sita = login_as(['employee'], demo['sita']).token
    ram = login_as(['employee', 'manager'], demo['ram']).token
    other = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E099', 'Peer Person') RETURNING id", [demo['company_id']])['id']
    peer = login_as(['employee'], str(other)).token
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute(HQ, [demo['company_id']])
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['sita']])
    c = Company.objects.create(code='OTHER', name_en='Other Co')
    create_user(company_id=str(c.id), employee_id=None, email='hr@other.photo', name='Other HR', password=PW, roles=['hr'])
    from hajiri.core import auth
    foreign = auth.login('hr@other.photo', PW).token
    return {'hr': hr, 'sita': sita, 'ram': ram, 'peer': peer, 'foreign': foreign}


def test_clock_in_from_home_with_a_photo(demo, photo_world):
    w = photo_world
    hr, sita, ram, peer, foreign_hr = w['hr'], w['sita'], w['ram'], w['peer'], w['foreign']

    # asks for a photo, then stores it encrypted and links it to the punch
    missing = punch(sita, HOME)
    assert missing.status == 400
    assert missing.body['details'] == {'code': 'photo_required'}
    ok = punch(sita, {**HOME, 'selfie': SELFIE})
    assert ok.status == 201
    assert ok.body['remote'] is True
    photo_id = ok.body['photoId']
    import re
    assert re.fullmatch(r'[0-9a-f-]{36}', photo_id)
    row = one('SELECT image_enc, mime, bytes FROM punch_photo WHERE id = %s', [photo_id])
    assert row['mime'] == 'image/jpeg' and row['bytes'] == len(JPEG)
    assert JPEG not in bytes(row['image_enc'])
    t = get(sita, '/punch/today')
    assert t.body['punches'][-1]['photo_id'] == photo_id
    from hajiri.core.auth import resolve_session
    team = att_api.today(resolve_session(hr), {'date': today()})
    assert str(next(r for r in team['rows'] if str(r['employee_id']) == demo['sita'])['photo_id']) == photo_id

    # serves the photo to the employee, their manager and HR (audited), and to nobody else
    own = get(sita, f'/punch/photos/{photo_id}')
    assert own.status == 200
    assert own.headers['Content-Type'].startswith('image/jpeg')
    assert own.headers['Cache-Control'] == 'private, max-age=300'
    assert own.content == JPEG
    assert get(ram, f'/punch/photos/{photo_id}').status == 200
    assert get(hr, f'/punch/photos/{photo_id}').status == 200
    assert get(peer, f'/punch/photos/{photo_id}').status == 403
    assert get(foreign_hr, f'/punch/photos/{photo_id}').status == 404
    assert get(None, f'/punch/photos/{photo_id}').status == 401
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_photo' AND row_id = %s", [photo_id])['n'] == 2

    # lists home check-ins: HR sees everyone, a manager their team, an employee only themselves
    q = {'from': today(), 'to': today()}
    al = get(hr, '/punch/remote', q)
    assert len(al.body['punches']) == 1
    p0 = al.body['punches'][0]
    assert p0['employee_id'] == demo['sita'] and p0['photo_id'] == photo_id and p0['location_shared'] is True and p0['direction'] == 'in'
    assert 'lat' not in p0
    assert len(get(ram, '/punch/remote', q).body['punches']) == 1
    assert len(get(peer, '/punch/remote', q).body['punches']) == 0
    assert get(peer, '/punch/remote', {**q, 'employeeId': demo['sita']}).status == 403
    assert len(get(foreign_hr, '/punch/remote', q).body['punches']) == 0

    # a clock-out from home does not need a photo; office punches never keep one
    assert punch(sita, {**HOME, 'direction': 'out'}).status == 201
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['ram']])
    at_office = punch(ram, {**OFFICE, 'selfie': SELFIE})
    assert at_office.status == 201
    assert at_office.body['remote'] is False
    assert at_office.body['photoId'] is None
    assert one('SELECT count(*)::int AS n FROM punch_photo WHERE employee_id = %s', [demo['ram']])['n'] == 0

    # rejects anything that is not a real image, and oversized photos
    fake = 'data:image/jpeg;base64,' + base64.b64encode(b'<svg onload=alert(1)>').decode()
    assert punch(sita, {**HOME, 'selfie': fake}).status == 400
    assert punch(sita, {**HOME, 'selfie': 'data:text/html;base64,PGgxPg=='}).status == 400
    big = 'data:image/jpeg;base64,' + base64.b64encode(JPEG + bytes(310 * 1024)).decode()
    assert punch(sita, {**HOME, 'selfie': big}).status == 400

    # the company can turn the photo off
    execute("""UPDATE company_setting SET value = value || '{"wfhPhotoRequired": false}' WHERE company_id = %s AND key = 'attendance.policy'""",
            [demo['company_id']])
    assert punch(sita, HOME).status == 201
    execute("""UPDATE company_setting SET value = value || '{"wfhPhotoRequired": true}' WHERE company_id = %s AND key = 'attendance.policy'""",
            [demo['company_id']])

    # photos are wiped after the retention period and then report as deleted
    later = datetime.now(timezone.utc) + timedelta(days=91)
    assert purge_expired_photos(later) > 0
    gone = get(hr, f'/punch/photos/{photo_id}')
    assert gone.status == 404
    assert 'retention' in gone.body['error']
    assert one('SELECT purged_at FROM punch_photo WHERE id = %s', [photo_id])['purged_at'] is not None


# ================================================================ work-from-home.test.ts

def test_work_from_home(demo, login_as):
    hr_login = login_as(['hr'])
    sita = login_as(['employee'], demo['sita']).token
    ram = login_as(['employee', 'manager'], demo['ram']).token
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute(HQ, [demo['company_id']])

    # HR can mark an employee as allowed to work from home (the employees slice's update; here at service level)
    from hajiri.employees.services import get_employee, update_employee
    update_employee(hr_login.user, demo['ram'], {'workFromHome': True})
    e = get_employee(hr_login.user, demo['ram'])
    emp = e.get('employee', e) if isinstance(e, dict) else e
    assert emp['work_from_home'] is True

    # an employee allowed to work from home clocks in from home without a reason, tagged remote
    r = post(ram, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'wfh-ram-in1', **HOME, 'selfie': SELFIE})
    assert r.status == 201
    assert r.body['remote'] is True
    assert 'Remote' in r.body['flags']
    assert 'OutsideLocation' not in r.body['flags']
    assert r.body['message'].endswith('from home')
    t = get(ram, '/punch/today')
    assert t.body['punches'][-1]['remote'] is True
    team = att_api.today(hr_login.user, {'date': today()})
    assert next(x for x in team['rows'] if str(x['employee_id']) == demo['ram'])['remote'] is True
    assert next(x for x in team['rows'] if str(x['employee_id']) == demo['sita'])['remote'] is False

    # an office employee away from the office still needs a reason
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'wfh-sita-in1', **HOME})
    assert r.status == 400

    # work from home planned ahead and approved allows a home clock-in
    from tests.punch.api import add_days
    d = today()
    assert post(sita, '/regularisation', {'localDate': add_days(d, 61), 'kind': 'wfh', 'reason': 'too far ahead'}).status == 400
    assert post(sita, '/regularisation', {'localDate': add_days(d, 3), 'kind': 'missed_punch', 'punchTime': now_iso(), 'punchDirection': 'in',
                                          'reason': 'future correction'}).status == 400
    assert post(sita, '/regularisation', {'localDate': add_days(d, 7), 'kind': 'wfh', 'reason': 'Plumber visiting'}).status == 201
    for i in range(8, 14):
        assert post(sita, '/regularisation', {'localDate': add_days(d, i), 'kind': 'wfh', 'reason': 'Home week'}).status == 201
    req = post(sita, '/regularisation', {'localDate': d, 'kind': 'wfh', 'reason': 'Sick child at home'})
    assert req.status == 201
    inst = one('SELECT workflow_instance_id FROM regularisation_request WHERE id = %s', [req.body['id']])['workflow_instance_id']
    assert post(ram, f'/workflow/{inst}/approve', {}).status == 200
    r = post(sita, '/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'wfh-sita-in2', **HOME, 'selfie': SELFIE})
    assert r.status == 201
    assert r.body['remote'] is True


# ================================================================ fixes-attendance.test.ts (the punch case)

def test_locked_period_refuses_manual_punches(demo, login_as):
    """A period locked before any attendance row existed refuses manual punches."""
    hr = login_as(['hr']).token
    from engines.bs_calendar import bs_month_bounds
    b = bs_month_bounds(2083, 5)
    per = one("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES (%s, 2083, 5, %s, %s, %s)
                 ON CONFLICT (company_id, bs_year, bs_month) DO UPDATE SET days = EXCLUDED.days RETURNING id""",
              [demo['company_id'], b['startAd'], b['endAd'], b['days']])
    execute('UPDATE payroll_period SET attendance_locked_at = now() WHERE id = %s', [per['id']])
    manual = post(hr, '/punch/manual', {'employeeId': demo['ram'], 'localDate': '2026-08-20', 'time': '10:00', 'direction': 'in',
                                        'reason': 'device was down'})
    assert manual.status == 409
    assert one("SELECT count(*)::int AS n FROM punch_event WHERE employee_id = %s AND local_date = '2026-08-20'", [demo['ram']])['n'] == 0


# ================================================================ extra rules from routes.ts

def test_manual_punch_for_yourself_is_refused_unless_admin(demo, login_as):
    me = login_as(['hr'], demo['sita']).token
    body = {'employeeId': demo['sita'], 'localDate': '2026-09-21', 'time': '10:00', 'direction': 'in', 'reason': 'Device was down'}
    r = post(me, '/punch/manual', body)
    assert r.status == 403 and r.body['error'] == 'someone else must add punches to your own attendance'
    admin = login_as(['admin'], demo['ram']).token
    assert post(admin, '/punch/manual', {**body, 'employeeId': demo['ram']}).status == 201


def test_geofence_block_and_field_staff(demo, login_as):
    sita = login_as(['employee'], demo['sita']).token
    execute(HQ, [demo['company_id']])
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'field.policy', '{"geofenceMode": "block"}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    r = punch(sita, {'lat': 27.80, 'lng': 85.40, 'accuracyM': 20, 'source': 'web'})
    assert r.status == 400 and r.body['details'] == {'code': 'outside_location'}
    assert 'Clock in when you reach your work location.' in r.body['error']
    no_loc = punch(sita, {'source': 'web'})
    assert no_loc.status == 400 and no_loc.body['error'] == 'Your location is needed to clock in. Allow location access and try again.'
    execute('UPDATE employee SET field_work = true WHERE id = %s', [demo['sita']])
    ok = punch(sita, {'lat': 27.80, 'lng': 85.40, 'accuracyM': 20, 'source': 'web'})
    assert ok.status == 201 and 'Field' in ok.body['flags']


def test_registered_devices_only(demo, login_as):
    sita = login_as(['employee'], demo['sita']).token
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'relations.policy', '{"registeredDevicesOnly": true}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    r = punch(sita, {'source': 'web'})
    assert r.status == 403 and r.body['details'] == {'code': 'device_not_registered'}


def test_pure_helpers():
    from hajiri.punch.services import distance_m, geofence, npt_local_time, parse_photo
    assert round(distance_m(27.7172, 85.3240, 27.7172, 85.3240)) == 0
    assert 11_800 < distance_m(27.7172, 85.3240, 27.80, 85.40) < 11_900
    assert geofence(1, 1, 10, []) == {'inside': None, 'locationId': None, 'distanceM': None, 'locationName': None}
    g = geofence(27.7173, 85.3241, 20, [{'id': 'a', 'name': 'HQ', 'lat': 27.7172, 'lng': 85.3240, 'radius_m': 150}])
    assert g['inside'] is True and g['locationName'] == 'HQ'
    assert npt_local_time(0) == '1970-01-01 05:45:00'
    assert parse_photo(SELFIE).mime == 'image/jpeg'
    from hajiri.core.errors import BadRequestError
    for bad, msg in [('data:image/gif;base64,AAAA', 'The photo must be a JPEG, PNG or WebP image.'),
                     ('data:image/png;base64,' + base64.b64encode(JPEG).decode(), 'The photo could not be read. Take it again.')]:
        with pytest.raises(BadRequestError) as e:
            parse_photo(bad)
        assert e.value.message == msg
