"""The overtime JSON API as DRF views: validation in the TS issue shape, the register's own `format` parameter,
sign-in and the Origin check, wrong methods."""
from __future__ import annotations

from django.test import Client

from tests.web.factories import bearer

from .helpers import browser, days_ago, slip


def test_validation_issues_keep_the_zod_wording(world):
    r = world.sita.post('/api/v1/ot/requests', {'localDate': '2026-13-40', 'requestedMin': 0})
    assert r.status_code == 400
    body = r.json()
    assert body['error'] == 'validation'
    assert {'path': 'localDate', 'message': 'not a valid date'} in body['issues']
    assert {'path': 'requestedMin', 'message': 'Too small: expected number to be >=1'} in body['issues']
    r = world.hr.get('/api/v1/ot/slips', {'status': 'nope'})
    assert r.status_code == 400
    assert r.json()['issues'] == [{'path': 'status', 'message': 'Invalid option: expected one of '
                                   '"detected"|"approved"|"rejected"|"posted"|"comp_off"'}]
    r = world.hr.get('/api/v1/ot/register', {'to': '2026-09-30'})
    assert r.status_code == 400 and r.json()['issues'] == [{'path': 'from', 'message': 'Required'}]
    r = world.hr.post('/api/v1/ot/slips/not-a-uuid/approve', {})
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'Invalid UUID'


def test_register_format_is_the_registers_own_parameter(world):
    slip(world.demo['ram'], '2026-09-26', 60)
    r = world.hr.get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'json'})
    assert r.status_code == 200 and r.json()['register'][0]['emp_code'] == 'E002'
    r = world.hr.get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'csv'})
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    r = world.hr.get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'xml'})
    assert r.status_code == 400
    assert r.json()['issues'] == [{'path': 'format', 'message': 'Invalid option: expected one of "json"|"csv"'}]


def test_response_shapes(world):
    sid = slip(world.demo['ram'], '2026-09-26', 60)
    s = world.hr.get('/api/v1/ot/slips', {'from': '2026-09-01'}).json()['slips'][0]
    assert s['id'] == sid and s['multiplier'] == '1.500' and s['local_date'] == '2026-09-26'
    assert s['created_at'].endswith('Z') and s['reviewed_by'] is None
    rid = world.sita.post('/api/v1/ot/requests', {'localDate': days_ago(1), 'requestedMin': 30}).json()['id']
    r = world.sita.get('/api/v1/ot/requests').json()['requests'][0]
    assert r['id'] == rid and r['status'] == 'pending' and r['requested_min'] == 30


def test_sign_in_origin_check_and_methods(world):
    assert Client().get('/api/v1/ot/slips').status_code == 401
    assert world.hr.c.delete('/api/v1/ot/slips', **bearer(world.hr.token)).status_code == 404
    # A cookie session (the React app, which sends no CSRF token) is checked against the Origin, as the TS
    # sameOriginGuard did: a foreign origin is refused, the site's own origin or no Origin at all passes.
    c = browser(world.hr.token)
    c.handler.enforce_csrf_checks = True
    sid = slip(world.demo['ram'], '2026-09-26', 60)
    r = c.post(f'/api/v1/ot/slips/{sid}/approve', '{}', content_type='application/json', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    r = c.post(f'/api/v1/ot/slips/{sid}/approve', '{}', content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert r.status_code == 200 and r.json() == {'ok': True, 'approvedMin': 60}
    # A Bearer token needs no Origin.
    sid2 = slip(world.demo['ram'], '2026-09-27', 60)
    assert world.hr.post(f'/api/v1/ot/slips/{sid2}/approve', {}).status_code == 200
