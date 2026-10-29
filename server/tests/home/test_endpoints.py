"""
Port of apps/api/test/endpoints.test.ts: one authorised call (status and shape) and one refused call for endpoints the
feature suites do not reach. Where a non-browser client still has the JSON route it is called over HTTP with the TS path;
where the route became a page, the call goes to the same service function and the page is checked for the refusal.
"""
import json

import pytest
from django.test import Client

from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from tests.web.factories import bearer

NOPE = '00000000-0000-7000-8000-0000000000ff'


@pytest.fixture
def w(demo, login_as):
    """hr, admin, sita (employee) and ram (manager and employee, Sita's manager), as the TS beforeAll."""
    x = {'hr': login_as(['hr']), 'admin': login_as(['admin']), 'sita': login_as(['employee'], demo['sita']),
         'ram': login_as(['manager', 'employee'], demo['ram']), 'demo': demo, 'login_as': login_as}
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return x


def get(who, path, **query):
    return Client().get(path, query, **bearer(who.token))


def send(method, who, path, body=None):
    c = Client()
    fn = getattr(c, method)
    return fn(path, data=json.dumps(body or {}), content_type='application/json', **bearer(who.token))


def page(who) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = who.token
    return c


# ---------------------------------------------------------------- health

def test_health_is_public_and_reports_the_database(db):
    r = Client().get('/health')
    assert r.status_code == 200 and r.json() == {'ok': True, 'db': 'up'}


# ---------------------------------------------------------------- auth and people

def test_raw_punches_for_an_employee_and_range(w):
    from hajiri.attendance.api import punches
    d = w['demo']
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
                                        direction, verify_mode, source, idempotency_key)
               VALUES (%s, 'DEMO-01', 'zkteco', '1', %s, '2026-09-01T04:20:00Z', '2026-09-01 10:05:00', '2026-09-01', 'unknown',
                       'fingerprint', 'zkteco', 'ep-1')""", [d['device_id'], d['sita']])
    p = punches(w['sita'].user, {'employeeId': d['sita'], 'from': '2026-09-01', 'to': '2026-09-02'})
    assert len(p) == 1 and str(p[0]['local_date']) == '2026-09-01' and p[0]['superseded'] is False
    with pytest.raises(ForbiddenError):
        punches(w['sita'].user, {'employeeId': d['ram'], 'from': '2026-09-01', 'to': '2026-09-02'})
    with pytest.raises(BadRequestError):
        punches(w['sita'].user, {'employeeId': d['sita']})


# ---------------------------------------------------------------- devices

# ---------------------------------------------------------------- shifts, holidays, locations and roster

def test_location_geofence_update(w):
    from hajiri.shifts import services as sh
    hr, sita = w['hr'].user, w['sita'].user
    lid = sh.create_location(hr, {'name': 'HQ', 'lat': 27.7, 'lng': 85.3})['id']
    sh.update_location(hr, lid, {'radiusM': 300, 'name': 'Kathmandu HQ'})
    assert one('SELECT name, radius_m FROM location WHERE id = %s', [lid]) == {'name': 'Kathmandu HQ', 'radius_m': 300}
    with pytest.raises(ValidationError):
        sh.update_location(hr, lid, {'radiusM': 5})
    with pytest.raises(ForbiddenError):
        sh.update_location(sita, lid, {'radiusM': 300})
    with pytest.raises(NotFoundError):
        sh.update_location(hr, NOPE, {'radiusM': 300})


# ---------------------------------------------------------------- leave and overtime lists (JSON routes)

def test_leave_types_everyone_reads_hr_creates(w):
    lst = get(w['sita'], '/api/v1/leave/types')
    assert lst.status_code == 200 and len(lst.json()['types']) > 0
    t0 = lst.json()['types'][0]
    assert isinstance(t0['id'], str) and isinstance(t0['code'], str) and isinstance(t0['name_en'], str) and isinstance(t0['paid'], bool)
    c = send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'paid': False, 'maxConsecutiveDays': 10})
    assert c.status_code == 201
    mine = next(t for t in get(w['sita'], '/api/v1/leave/types').json()['types'] if t['id'] == c.json()['id'])
    assert (mine['code'], mine['paid'], mine['statutory']) == ('STUDY', False, False)
    assert send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Again'}).status_code == 409
    assert send('post', w['sita'], '/api/v1/leave/types', {'code': 'X', 'nameEn': 'X'}).status_code == 403


def test_leave_requests_scope_own_team_all(w):
    send('post', w['hr'], '/api/v1/leave/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'paid': False, 'maxConsecutiveDays': 10})
    t = next(x for x in get(w['sita'], '/api/v1/leave/types').json()['types'] if x['code'] == 'STUDY')
    c = send('post', w['sita'], '/api/v1/leave/requests', {'leaveTypeId': t['id'], 'from': '2026-11-16', 'to': '2026-11-16', 'reason': 'exam'})
    assert c.status_code == 201, c.content
    own = get(w['sita'], '/api/v1/leave/requests')
    assert own.status_code == 200 and len(own.json()['requests']) == 1
    r0 = own.json()['requests'][0]
    assert (r0['employee_id'], r0['type_code'], r0['status']) == (w['demo']['sita'], 'STUDY', 'pending')
    assert c.json()['id'] in [x['id'] for x in get(w['ram'], '/api/v1/leave/requests').json()['requests']]
    assert get(w['hr'], '/api/v1/leave/requests', status='approved').json()['requests'] == []
    assert get(w['sita'], '/api/v1/leave/requests', scope='all').status_code == 403
    assert get(w['sita'], '/api/v1/leave/requests', employeeId=w['demo']['ram']).status_code == 403


def test_ot_requests_list_and_cancel(w):
    d = w['demo']
    execute('UPDATE employee SET ot_eligible = true WHERE id = %s', [d['sita']])
    c = send('post', w['hr'], '/api/v1/ot/requests', {'employeeId': d['sita'], 'localDate': '2026-11-03', 'requestedMin': 60, 'reason': 'release'})
    assert c.status_code == 201, c.content
    own = get(w['sita'], '/api/v1/ot/requests')
    r0 = own.json()['requests'][0]
    assert own.status_code == 200 and (r0['id'], r0['employee_id'], r0['requested_min'], r0['status']) == (c.json()['id'], d['sita'], 60, 'pending')
    assert get(w['sita'], '/api/v1/ot/requests', scope='all').status_code == 403
    assert send('post', w['sita'], f"/api/v1/ot/requests/{c.json()['id']}/cancel").status_code == 200
    assert one('SELECT status FROM ot_request WHERE id = %s', [c.json()['id']])['status'] == 'cancelled'
    assert send('post', w['sita'], f"/api/v1/ot/requests/{c.json()['id']}/cancel").status_code == 409
    assert send('post', w['sita'], f'/api/v1/ot/requests/{NOPE}/cancel').status_code == 404
    other = w['login_as'](['employee'], None)
    c2 = send('post', w['hr'], '/api/v1/ot/requests', {'employeeId': d['sita'], 'localDate': '2026-11-04', 'requestedMin': 30})
    assert send('post', other, f"/api/v1/ot/requests/{c2.json()['id']}/cancel").status_code == 403


# ---------------------------------------------------------------- settings (pages and services)

# ---------------------------------------------------------------- payroll setup and run lists (JSON routes)

def P(path):
    return f'/api/v1/payroll{path}'


def test_structures_and_versions(w):
    s = send('post', w['hr'], P('/structures'), {'code': 'EP', 'nameEn': 'Endpoint structure', 'components': [{'code': 'BASIC'}]})
    assert s.status_code == 201, s.content
    assert set(s.json()) == {'id', 'versionId'}
    v = send('post', w['hr'], P(f"/structures/{s.json()['id']}/versions"), {'components': [{'code': 'BASIC'}, {'code': 'GRADE'}]})
    assert v.status_code == 201 and v.json()['version'] == 2 and isinstance(v.json()['id'], str)
    lst = get(w['hr'], P('/structures')).json()['structures']
    assert [x['version'] for x in next(x for x in lst if x['id'] == s.json()['id'])['versions']] == [1, 2]
    assert send('post', w['hr'], P('/structures'), {'code': 'EP', 'nameEn': 'dup', 'components': [{'code': 'BASIC'}]}).status_code == 409
    assert send('post', w['hr'], P(f'/structures/{NOPE}/versions'), {'components': [{'code': 'BASIC'}]}).status_code == 404
    assert send('post', w['sita'], P('/structures'), {'code': 'NO', 'nameEn': 'no', 'components': [{'code': 'BASIC'}]}).status_code == 403


# ---------------------------------------------------------------- biometrics

def test_unmapped_templates_and_stats_for_hr_only(w):
    from hajiri.devices import biometrics as bio
    d = w['demo']
    execute("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (NULL, '99', 'fingerprint', 'zk_fp_v10', '\\x00'::bytea, 1, 'h', %s)""", [d['device_id']])
    u = bio.unmapped_templates(w['hr'].user)
    assert len(u) == 1 and (u[0]['modality'], u[0]['device_user_pin'], u[0]['device_sn']) == ('fingerprint', '99', 'DEMO-01')
    assert 'ciphertext' not in u[0]
    st = bio.stats(w['hr'].user)
    st = st['byModality'] if isinstance(st, dict) else st
    assert [{k: int(v) if k != 'modality' else v for k, v in x.items()} for x in st] == [{'modality': 'fingerprint', 'active': 1, 'shredded': 0, 'unmapped': 1}]
    with pytest.raises(ForbiddenError):
        bio.unmapped_templates(w['sita'].user)
    with pytest.raises(ForbiddenError):
        bio.stats(w['sita'].user)
