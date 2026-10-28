"""The shifts JSON API (DRF, /api/v1/shifts/...) for Bearer-token clients: the TS paths, bodies, answers and status
codes of routes.ts, with the same rules and messages as the pages."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from hajiri.db.models import Holiday, Location, Roster, Shift, ShiftAssignment
from tests.web.factories import bearer


@pytest.fixture
def api(demo, login_as):
    """api(roles, employee_id=None) -> request(method, path, body=None) for a fresh Bearer session."""
    def make(roles, employee_id=None):
        login = login_as(roles, employee_id)
        c, h = Client(), bearer(login.token)

        def call(method, path, body=None, **query):
            url = f'/api/v1/shifts{path}'
            if method == 'get':
                return c.get(url, query, **h)
            return getattr(c, method)(url, json.dumps(body if body is not None else {}), content_type='application/json', **h)
        return call
    return make


def test_shift_crud_answers_like_the_ts_routes(api, demo):
    hr = api(['hr'])
    listed = hr('get', '').json()['shifts']
    gen = next(s for s in listed if s['code'] == 'GEN')
    assert gen['start'] == '10:00:00' and gen['crosses_midnight'] is False and gen['assigned_employees'] == 2
    r = hr('post', '', {'code': 'NIGHT', 'nameEn': 'Night', 'start': '22:00', 'end': '06:00'})
    assert r.status_code == 201
    sid = r.json()['id']
    s = hr('get', f'/{sid}').json()['shift']
    assert s['crosses_midnight'] is True and s['late_out_window_min'] == 360 and s['pairing_mode'] == 'alternating'
    assert hr('patch', f'/{sid}', {'graceInMin': 7}).json() == {'ok': True, 'recomputeQueued': 0}
    assert Shift.objects.get(pk=sid).grace_in_min == 7
    assert hr('delete', f'/{sid}').json() == {'ok': True}
    assert hr('get', f'/{sid}').status_code == 404 and hr('get', f'/{sid}').json() == {'error': 'shift not found'}


def test_validation_and_role_errors_have_the_ts_shape(api, demo):
    hr, emp = api(['hr']), api(['employee'])
    r = hr('post', '', {'code': 'X', 'nameEn': 'X', 'start': '25:00', 'end': '06:00', 'graceInMin': '5'})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    issues = {i['path']: i['message'] for i in r.json()['issues']}
    assert issues['start'] == 'HH:MM' and 'graceInMin' in issues          # zod refused numeric strings
    r = emp('post', '', {'code': 'X', 'nameEn': 'X', 'start': '09:00', 'end': '17:00'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr or attendance_admin'}
    assert emp('get', '').status_code == 200                             # every signed-in user reads the shifts
    assert Client().get('/api/v1/shifts').status_code == 401


def test_holidays_and_import(api, demo):
    hr = api(['hr'])
    r = hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Anniversary'})
    assert r.status_code == 201 and set(r.json()) == {'id', 'recomputeQueued'}
    hid = r.json()['id']
    assert hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Anniversary'}).status_code == 409
    got = hr('get', '/holidays', **{'from': '2026-12-01', 'to': '2026-12-31'}).json()['holidays']
    assert [h['local_date'] for h in got if h['id'] == hid] == ['2026-12-31']
    assert hr('get', '/holidays', **{'from': 'tomorrow'}).status_code == 400
    assert hr('patch', f'/holidays/{hid}', {'optional': True}).json() == {'ok': True}
    assert Holiday.objects.get(pk=hid).optional is True
    assert hr('delete', f'/holidays/{hid}').json() == {'ok': True}
    assert hr('delete', f'/holidays/{hid}').status_code == 404
    r = hr('post', '/holidays/import-default', {'bsYear': 2083})
    assert r.status_code == 200 and r.json() == {'inserted': 42, 'total': 42}
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json() == {'inserted': 0, 'total': 42}
    r = hr('post', '/holidays/import-default', {'bsYear': 2084})
    assert r.status_code == 400 and r.json()['error'] == 'default list available for BS 2083 only; add others via POST /shifts/holidays'


def test_locations(api, demo):
    hr = api(['hr'])
    r = hr('post', '/locations', {'name': 'HQ', 'lat': 27.7172, 'lng': 85.324})
    assert r.status_code == 201
    lid = r.json()['id']
    loc = next(x for x in hr('get', '/locations').json()['locations'] if x['id'] == lid)
    assert loc['radius_m'] == 150 and loc['lat'] == '27.717200'                # numeric columns as strings, like pg
    assert hr('patch', f'/locations/{lid}', {'radiusM': 5}).status_code == 400
    assert hr('patch', f'/locations/{lid}', {}).json() == {'ok': True}
    assert hr('patch', f'/locations/{lid}', {'radiusM': 400}).json() == {'ok': True}
    assert Location.objects.get(pk=lid).radius_m == 400
    assert hr('delete', f'/locations/{lid}').json() == {'ok': True}
    assert hr('delete', f'/locations/{lid}').status_code == 404


def test_calendar_roster_and_assign(api, demo):
    hr = api(['hr'])
    days = hr('get', '/calendar', **{'from': '2026-10-17', 'to': '2026-10-18'}).json()['days']
    assert [d['date'] for d in days] == ['2026-10-17', '2026-10-18'] and days[0]['weeklyOff'] is True   # Saturday
    assert hr('get', '/calendar', **{'from': '2026-10-18', 'to': '2026-10-17'}).json() == {'error': 'range must be 1 to 400 days'}
    cell = {'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': None}
    assert hr('put', '/roster', {'rows': [cell]}).json() == {'upserted': 1}
    grid = hr('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'}).json()
    assert {'employee_id': demo['sita'], 'local_date': '2026-11-02', 'shift_id': None} in grid['roster']
    assert any(e['id'] == demo['sita'] and e['default_shift_id'] == demo['shift_id'] for e in grid['employees'])
    assert hr('post', '/roster/copy-week', {'weekStart': '2026-11-08'}).json() == {'copied': 1, 'skippedLocked': 0}
    assert hr('post', '/roster/publish', {'from': '2026-11-01', 'to': '2026-11-14'}).json() == {'notified': 1}
    assert hr('delete', '/roster', {'employeeId': demo['sita'], 'localDate': '2026-11-02'}).json() == {'deleted': 1}
    assert Roster.objects.filter(employee_id=demo['sita']).count() == 1
    r = hr('post', '/assign', {'employeeId': demo['sita'], 'shiftId': demo['shift_id'], 'from': '2027-01-01'})
    assert r.status_code == 201 and r.json() == {'ok': True}
    spans = sorted((a.valid.lower, a.valid.upper) for a in ShiftAssignment.objects.filter(employee_id=demo['sita']))
    assert spans[-1][0].isoformat() == '2027-01-01' and spans[-1][1] is None and spans[-2][1].isoformat() == '2027-01-01'
    assert api(['employee'])('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'}).status_code == 403
