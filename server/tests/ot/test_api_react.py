"""The overtime calls the React app makes (frontend/src/api/index.js `ot`, pages/hr/OtSlipsPage.jsx) with the session
cookie and the browser's own Origin, as fetch() sends them: every path, method, body and response field it reads."""
from __future__ import annotations

import json

from hajiri.db.models import OtRequest, OtSlip

from .helpers import browser, days_ago, slip

ORIGIN = {'HTTP_ORIGIN': 'http://testserver'}


class Browser:
    def __init__(self, token):
        self.c = browser(token)
        self.c.handler.enforce_csrf_checks = True

    def get(self, path, query=None):
        return self.c.get(path, query or {})

    def post(self, path, body=None):
        data = json.dumps(body) if body is not None else ''
        return self.c.post(path, data, content_type='application/json', **ORIGIN)


def test_slips_page_lists_approves_rejects_and_converts(world):
    hr = Browser(world.hr.token)
    a, b, c = (slip(world.demo['ram'], d, m) for d, m in (('2026-09-21', 120), ('2026-09-22', 90), ('2026-09-26', 240)))
    slips = hr.get('/api/v1/ot/slips', {'status': 'detected'}).json()['slips']
    assert {s['id'] for s in slips} == {a, b, c}
    s = next(s for s in slips if s['id'] == a)
    for key in ('name_en', 'emp_code', 'local_date', 'day_type', 'detected_min', 'approved_min', 'status'):
        assert key in s
    assert s['emp_code'] == 'E002' and s['detected_min'] == 120 and s['approved_min'] is None
    # approveSlip(id, approvedMin) and approveSlip(id) (body {})
    assert hr.post(f'/api/v1/ot/slips/{a}/approve', {'approvedMin': 60}).json() == {'ok': True, 'approvedMin': 60}
    assert hr.post(f'/api/v1/ot/slips/{b}/approve', {}).json() == {'ok': True, 'approvedMin': 90}
    # rejectSlip(id, reason)
    r = hr.post(f'/api/v1/ot/slips/{b}/reject', {'reason': 'no'})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert hr.post(f'/api/v1/ot/slips/{b}/reject', {'reason': 'not authorised'}).json() == {'ok': True}
    # compOff(id): post() with no body
    r = hr.post(f'/api/v1/ot/slips/{c}/comp-off')
    assert r.status_code == 200 and r.json() == {'ok': True, 'days': 0.5, 'expiresOn': '2026-10-17'}
    assert [OtSlip.objects.get(pk=x).status for x in (a, b, c)] == ['approved', 'rejected', 'comp_off']
    assert hr.post(f'/api/v1/ot/slips/{c}/comp-off').status_code == 409


def test_register_csv_download_with_the_cookie(world):
    slip(world.demo['ram'], '2026-09-26', 60)
    r = Browser(world.hr.token).get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'csv'})
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    assert 'E002,Ram Thapa,2026-09-26' in r.content.decode('utf-8')
    # an employee is refused with the TS role message
    r = Browser(world.sita.token).get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'csv'})
    assert r.status_code == 403 and 'requires role' in r.json()['error']


def test_requests_create_list_and_cancel_with_the_cookie(world):
    sita = Browser(world.sita.token)
    r = sita.post('/api/v1/ot/requests', {'localDate': days_ago(1), 'requestedMin': 45, 'reason': 'stock count'})
    assert r.status_code == 201
    rid = r.json()['id']
    rows = sita.get('/api/v1/ot/requests', {'status': 'pending'}).json()['requests']
    assert [x['id'] for x in rows] == [rid] and rows[0]['reason'] == 'stock count'
    assert sita.post(f'/api/v1/ot/requests/{rid}/cancel').json() == {'ok': True}
    assert OtRequest.objects.get(pk=rid).status == 'cancelled'
    r = sita.post(f'/api/v1/ot/requests/{rid}/cancel')
    assert r.status_code == 409 and r.json()['error'] == 'only pending requests can be cancelled (status cancelled)'
    # the manager sees the team's requests; a foreign Origin is refused
    assert [x['id'] for x in Browser(world.ram.token).get('/api/v1/ot/requests').json()['requests']] == [rid]
    r = sita.c.post('/api/v1/ot/requests', json.dumps({'localDate': days_ago(2), 'requestedMin': 30}),
                    content_type='application/json', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403


SLIP_KEYS = ['id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'detected_min', 'approved_min', 'multiplier',
             'day_type', 'status', 'posted_payslip_id', 'reviewed_by', 'reviewed_at', 'created_at']
REQUEST_KEYS = ['id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'requested_min', 'approved_min', 'reason',
                'status', 'workflow_instance_id', 'created_at', 'decided_at']
REGISTER_KEYS = ['emp_code', 'name_en', 'local_date', 'bs_date', 'day_type', 'detected_min', 'approved_min',
                 'multiplier', 'status']
TS_TIMESTAMP = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$'


def test_rows_have_the_ts_columns_and_value_formats(world):
    """Field by field as node-postgres gave them: dates as 'YYYY-MM-DD', numeric as a string, timestamps as Date JSON."""
    import re
    hr = Browser(world.hr.token)
    a = slip(world.demo['ram'], '2026-09-21', 120)
    slip(world.demo['ram'], '2026-09-22', 30, 'weekly_off')
    hr.post(f'/api/v1/ot/slips/{a}/approve', {'approvedMin': 60})
    slips = hr.get('/api/v1/ot/slips', {'from': '2026-09-01', 'to': '2026-09-30'}).json()['slips']
    assert [s['local_date'] for s in slips] == ['2026-09-22', '2026-09-21']     # newest day first
    fresh, done = slips
    assert list(done) == SLIP_KEYS
    assert done['multiplier'] == '1.500' and isinstance(done['detected_min'], int) and done['approved_min'] == 60
    assert done['reviewed_by'] == str(world.hr_login.user_id)
    assert re.match(TS_TIMESTAMP, done['reviewed_at']) and re.match(TS_TIMESTAMP, done['created_at'])
    assert fresh['day_type'] == 'weekly_off' and fresh['approved_min'] is None and fresh['reviewed_at'] is None
    assert fresh['posted_payslip_id'] is None

    sita = Browser(world.sita.token)
    rid = sita.post('/api/v1/ot/requests', {'localDate': days_ago(1), 'requestedMin': 45}).json()['id']
    # RequestsPage: ot.requests({scope: 'all', status})
    rows = hr.get('/api/v1/ot/requests', {'scope': 'all', 'status': 'pending'}).json()['requests']
    assert [r['id'] for r in rows] == [rid]
    r = rows[0]
    assert list(r) == REQUEST_KEYS
    assert r['local_date'] == days_ago(1) and r['requested_min'] == 45 and r['approved_min'] is None
    assert r['reason'] is None and r['decided_at'] is None and r['emp_code'] == 'E001'
    assert re.match(TS_TIMESTAMP, r['created_at']) and r['workflow_instance_id']
    for status in ('approved', 'rejected', 'cancelled'):
        assert hr.get('/api/v1/ot/requests', {'scope': 'all', 'status': status}).json() == {'requests': []}

    reg = hr.get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30'}).json()['register']
    assert [list(x) for x in reg] == [REGISTER_KEYS, REGISTER_KEYS]
    assert reg[0]['local_date'] == '2026-09-21' and reg[0]['multiplier'] == '1.500' and reg[0]['bs_date'] is None


def test_body_types_are_checked_as_zod_did(world):
    """z.number() refused "60" and true, z.string() refused 12, z.uuid() refused a number."""
    hr, sita = Browser(world.hr.token), Browser(world.sita.token)
    a = slip(world.demo['ram'], '2026-09-21', 120)
    for body in ({'approvedMin': '60'}, {'approvedMin': True}, {'approvedMin': 7.5}, {'approvedMin': None}):
        r = hr.post(f'/api/v1/ot/slips/{a}/approve', body)
        assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'approvedMin', body
    r = hr.post(f'/api/v1/ot/slips/{a}/reject', {'reason': 12345})
    assert r.status_code == 400 and r.json()['issues'] == [{'path': 'reason', 'message': 'Expected string'}]
    for body in ({'localDate': days_ago(1), 'requestedMin': '45'}, {'localDate': days_ago(1), 'requestedMin': 45, 'reason': 12},
                 {'localDate': days_ago(1), 'requestedMin': 45, 'employeeId': 12}):
        r = sita.post('/api/v1/ot/requests', body)
        assert r.status_code == 400 and r.json()['error'] == 'validation', body
    assert not OtRequest.objects.exists()
    assert OtSlip.objects.get(pk=a).status == 'detected'
    # a JSON number that is whole is still a number (60.0 == 60 in JSON)
    assert hr.post(f'/api/v1/ot/slips/{a}/approve', {'approvedMin': 60.0}).json() == {'ok': True, 'approvedMin': 60}


def test_cleared_status_filter_and_empty_number_field(world):
    """OtSlipsPage's Select is clearable (no status key sent); NumberInput emptied sends Number('') = 0."""
    hr = Browser(world.hr.token)
    a = slip(world.demo['ram'], days_ago(3), 90)
    b = slip(world.demo['ram'], days_ago(2), 60)
    hr.post(f'/api/v1/ot/slips/{b}/reject', {'reason': 'not authorised'})
    rows = hr.get('/api/v1/ot/slips', {'from': days_ago(30), 'to': days_ago(0)}).json()['slips']
    assert {s['id']: s['status'] for s in rows} == {a: 'detected', b: 'rejected'}
    assert hr.post(f'/api/v1/ot/slips/{a}/approve', {'approvedMin': 0}).json() == {'ok': True, 'approvedMin': 0}
    # a rejected slip cannot be converted to comp-off (the TS claim excluded rejected)
    assert hr.post(f'/api/v1/ot/slips/{b}/comp-off').status_code == 409
