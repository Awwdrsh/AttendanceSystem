"""The /api/v1/regularisation contract the React app relies on (frontend/src/api/index.js `regularisation`, used by
MyAttendancePage, HomePage, FixPunchPage, RequestsPage and lib/offlinePunch.js), checked against the TS routes of
apps/api/src/modules/regularisation/routes.ts: the cookie session with the Origin check, the error shape, the list row
fields exactly as node-postgres returned them, the filters and the cancel path. The workflow behaviour itself is in
test_regularisation.py (the port of regularisation.test.ts and regularisation-qa.test.ts)."""
from __future__ import annotations

import json
import re
import uuid

import pytest
from django.test import Client

from hajiri.core.auth import SESSION_COOKIE
from tests.core.fixtures import bearer

from .test_regularisation import URL, npt, workday_ago

ROW_KEYS = {'id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'kind', 'punch_time', 'punch_direction', 'shift_id',
            'reason', 'attachment_key', 'status', 'workflow_instance_id', 'resulting_punch_id', 'created_by', 'created_at',
            'decided_at'}
ISO_MS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')


def api_post(login, body, url=URL):
    r = Client().post(url, json.dumps(body), content_type='application/json', **bearer(login.token))
    return r, (r.json() if r.content else {})


def api_get(login, query=None, url=URL):
    r = Client().get(url, query or {}, **bearer(login.token))
    return r, (r.json() if r.content else {})


def cookie_client(login) -> Client:
    """The browser: the hajiri_session cookie, no CSRF token (client.js never reads one)."""
    c = Client()
    c.cookies[SESSION_COOKIE] = login.token
    return c


# ---------------------------------------------------------------- authentication

def test_signed_out_requests_are_401_with_the_error_shape(demo):
    for r in (Client().get(URL), Client().post(URL, '{}', content_type='application/json'),
              Client().post(f'{URL}/{uuid.uuid4()}/cancel')):
        assert r.status_code == 401
        assert isinstance(r.json()['error'], str)


def test_the_react_app_files_and_cancels_with_the_cookie_and_no_csrf_token(demo, reg):
    c = cookie_client(reg['sita'])
    body = {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'}
    r = c.post(URL, json.dumps(body), content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201, r.content
    assert set(r.json()) == {'id', 'status'} and r.json()['status'] == 'pending'
    listed = c.get(URL)
    assert listed.status_code == 200 and [x['id'] for x in listed.json()['requests']] == [r.json()['id']]
    ok = c.post(f"{URL}/{r.json()['id']}/cancel", HTTP_ORIGIN='http://testserver')
    assert ok.status_code == 200 and ok.json() == {'ok': True}


def test_a_cookie_request_from_another_site_is_refused(demo, reg):
    c = cookie_client(reg['sita'])
    body = {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'}
    r = c.post(URL, json.dumps(body), content_type='application/json', HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert c.get(URL).json()['requests'] == []


# ---------------------------------------------------------------- POST /regularisation

def test_validation_errors_have_the_zod_shape(demo, reg):
    r, body = api_post(reg['sita'], {'localDate': '2026-13-01', 'kind': 'nap', 'reason': 'x'})
    assert r.status_code == 400
    assert isinstance(body['error'], str)
    paths = {i['path'] for i in body['issues']}
    assert {'localDate', 'kind', 'reason'} <= paths


def test_missed_punch_needs_time_and_direction_and_shift_change_needs_a_shift(demo, reg):
    d = workday_ago(2)
    r, body = api_post(reg['sita'], {'localDate': d, 'kind': 'missed_punch', 'reason': 'forgot'})
    assert r.status_code == 400 and body['error'] == 'punchTime and punchDirection are required for a missed punch'
    r, body = api_post(reg['sita'], {'localDate': d, 'kind': 'shift_change', 'reason': 'swapped'})
    assert r.status_code == 400 and body['error'] == 'shiftId is required for a shift change'
    r, body = api_post(reg['sita'], {'localDate': d, 'kind': 'shift_change', 'shiftId': str(uuid.uuid4()), 'reason': 'swap'})
    assert r.status_code == 404 and body['error'] == 'shift not found'


def test_the_react_punch_time_with_a_nepal_offset_is_accepted(demo, reg):
    # MyAttendancePage and FixPunchPage send `${date}T${time}:00+05:45`.
    d = workday_ago(2)
    r, body = api_post(reg['sita'], {'employeeId': demo['sita'], 'localDate': d, 'kind': 'missed_punch',
                                     'punchTime': f'{d}T18:00:00+05:45', 'punchDirection': 'out', 'reason': 'forgot'})
    assert r.status_code == 201, body
    _, listed = api_get(reg['sita'])
    row = listed['requests'][0]
    assert row['punch_time'] == npt(d, '18:00') and row['punch_direction'] == 'out'


def test_a_duplicate_pending_request_is_409(demo, reg):
    body = {'localDate': workday_ago(2), 'kind': 'half_day', 'reason': 'doctor'}
    assert api_post(reg['sita'], body)[0].status_code == 201
    r, out = api_post(reg['sita'], body)
    assert r.status_code == 409 and out['error'] == 'a pending request of this kind already exists for that day'


def test_an_unknown_employee_is_404(demo, reg):
    r, body = api_post(reg['hr'], {'employeeId': str(uuid.uuid4()), 'localDate': workday_ago(2), 'kind': 'wfh',
                                   'reason': 'device offline'})
    assert r.status_code == 404 and body['error'] == 'employee not found'


# ---------------------------------------------------------------- GET /regularisation

def test_list_rows_have_the_ts_fields_and_formats(demo, reg):
    d = workday_ago(3)
    r, created = api_post(reg['sita'], {'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(d, '18:00'),
                                         'punchDirection': 'out', 'reason': 'forgot to punch out'})
    assert r.status_code == 201
    r, body = api_get(reg['hr'], {'scope': 'all', 'status': 'pending'})
    assert r.status_code == 200 and set(body) == {'requests'}
    row = next(x for x in body['requests'] if x['id'] == created['id'])
    assert set(row) == ROW_KEYS
    assert row['employee_id'] == demo['sita'] and row['local_date'] == d and row['kind'] == 'missed_punch'
    assert row['punch_time'] == npt(d, '18:00') and ISO_MS.match(row['created_at'])
    assert row['status'] == 'pending' and row['decided_at'] is None and row['resulting_punch_id'] is None
    assert row['created_by'] == reg['sita'].user_id and row['shift_id'] is None and row['attachment_key'] is None
    assert isinstance(row['emp_code'], str) and isinstance(row['name_en'], str)
    uuid.UUID(row['workflow_instance_id'])


def test_list_filters_and_order(demo, reg):
    d1, d2 = workday_ago(2), workday_ago(4)
    _, a = api_post(reg['sita'], {'localDate': d2, 'kind': 'on_duty', 'reason': 'older'})
    _, b = api_post(reg['sita'], {'localDate': d1, 'kind': 'half_day', 'reason': 'newer'})
    api_post(reg['sita'], {}, url=f"{URL}/{a['id']}/cancel")
    ids = lambda q: [x['id'] for x in api_get(reg['hr'], {'scope': 'all', **q})[1]['requests']]  # noqa: E731
    assert ids({}) == [b['id'], a['id']]                     # newest first
    assert ids({'status': 'cancelled'}) == [a['id']]
    assert ids({'status': 'pending'}) == [b['id']]
    assert ids({'from': d1}) == [b['id']]
    assert ids({'to': d2}) == [a['id']]
    assert ids({'employeeId': demo['sita']}) == [b['id'], a['id']]
    assert ids({'employeeId': demo['ram']}) == []
    # RequestsPage sends status='' for "any status" (client.js drops empty values, and the server ignores them too).
    assert ids({'status': ''}) == [b['id'], a['id']]


def test_list_scopes(demo, reg):
    _, mine = api_post(reg['sita'], {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'sita'})
    _, other = api_post(reg['hr'], {'employeeId': demo['ram'], 'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'ram'})
    ids = lambda who, q=None: {x['id'] for x in api_get(reg[who], q)[1]['requests']}  # noqa: E731
    assert ids('sita') == {mine['id']}                       # an employee sees their own
    assert ids('ram') == {mine['id'], other['id']}           # a manager: their reports and themself
    assert ids('ram', {'scope': 'own'}) == {other['id']}
    assert ids('hr') >= {mine['id'], other['id']}
    r, body = api_get(reg['sita'], {'scope': 'all'})
    assert r.status_code == 403 and body['error'] == 'scope all requires HR'
    r, body = api_get(reg['sita'], {'employeeId': demo['ram']})
    assert r.status_code == 403


def test_bad_list_queries_are_400(demo, reg):
    for q in ({'status': 'done'}, {'scope': 'everyone'}, {'from': '2026-02-30'}, {'employeeId': 'nope'}):
        r, body = api_get(reg['hr'], q)
        assert r.status_code == 400, q
        assert body['issues'][0]['path'] == next(iter(q))


# ---------------------------------------------------------------- POST /regularisation/:id/cancel

def test_cancel_errors(demo, reg):
    r, body = api_post(reg['sita'], {}, url=f'{URL}/not-a-uuid/cancel')
    assert r.status_code == 400 and body['issues'][0]['message'] == 'Invalid UUID'
    r, body = api_post(reg['sita'], {}, url=f'{URL}/{uuid.uuid4()}/cancel')
    assert r.status_code == 404 and body['error'] == 'request not found'
    _, out = api_post(reg['sita'], {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'})
    assert api_post(reg['sita'], {}, url=f"{URL}/{out['id']}/cancel")[1] == {'ok': True}
    r, body = api_post(reg['sita'], {}, url=f"{URL}/{out['id']}/cancel")
    assert r.status_code == 409 and body['error'] == 'only pending requests can be cancelled (status cancelled)'
    _, listed = api_get(reg['sita'])
    row = listed['requests'][0]
    assert row['status'] == 'cancelled' and ISO_MS.match(row['decided_at'])


def test_hr_cancels_for_anyone_an_unrelated_employee_cannot(demo, reg, login_as):
    _, out = api_post(reg['sita'], {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'})
    stranger = login_as(['employee'])
    assert api_post(stranger, {}, url=f"{URL}/{out['id']}/cancel")[0].status_code == 403
    assert api_post(reg['hr'], {}, url=f"{URL}/{out['id']}/cancel")[0].status_code == 200


# ---------------------------------------------------------------- company isolation

@pytest.fixture
def other_hr(demo, login_as, other_company):
    return login_as(['hr'], company_id=str(other_company.id))


def test_another_companys_hr_sees_nothing_and_cancels_nothing(demo, reg, other_hr):
    _, out = api_post(reg['sita'], {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'})
    _, listed = api_get(other_hr, {'scope': 'all'})
    assert listed == {'requests': []}
    r, body = api_post(other_hr, {}, url=f"{URL}/{out['id']}/cancel")
    assert r.status_code == 404 and body['error'] == 'request not found'
    r, body = api_get(other_hr, {'employeeId': demo['sita']})
    assert r.status_code == 404 and body['error'] == 'employee not found'
    r, body = api_post(other_hr, {'employeeId': demo['sita'], 'localDate': workday_ago(2), 'kind': 'missed_punch',
                                  'punchTime': npt(workday_ago(2), '10:00'), 'punchDirection': 'in', 'reason': 'device'})
    assert r.status_code == 404 and body['error'] == 'employee not found'
    r, body = api_post(other_hr, {'employeeId': demo['sita'], 'localDate': workday_ago(2), 'kind': 'shift_change',
                                  'shiftId': demo['shift_id'], 'reason': 'swap'})
    assert r.status_code == 404
    # The request is untouched.
    assert api_get(reg['sita'])[1]['requests'][0]['status'] == 'pending'


# ---------------------------------------------------------------- the exact bodies of the other React callers

def test_the_offline_queue_body_with_a_browser_iso_instant_is_filed_for_the_signed_in_employee(demo, reg):
    # lib/offlinePunch.js: no employeeId, punchTime = new Date().toISOString() (milliseconds and Z), cookie session.
    d = workday_ago(2)
    at = npt(d, '09:07').replace('.000Z', '.123Z')
    c = cookie_client(reg['sita'])
    r = c.post(URL, json.dumps({'localDate': d, 'kind': 'missed_punch', 'punchTime': at, 'punchDirection': 'in',
                                'reason': 'Clocked in at 09:07 while offline (time from my phone); sent automatically.'}),
               content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201, r.content
    assert r.json()['status'] == 'pending'
    row = c.get(URL).json()['requests'][0]
    assert row['employee_id'] == demo['sita'] and row['punch_direction'] == 'in'
    assert row['punch_time'] == at and row['local_date'] == d


def test_the_home_page_wfh_request_for_a_coming_day(demo, reg):
    # HomePage: {localDate, kind: 'wfh', reason} only; work from home may be planned up to 60 days ahead.
    from datetime import timedelta
    from .test_regularisation import nepal_today
    ahead = (nepal_today() + timedelta(days=3)).isoformat()
    r, body = api_post(reg['sita'], {'localDate': ahead, 'kind': 'wfh', 'reason': 'plumber visit'})
    assert r.status_code == 201 and body['status'] == 'pending'
    row = api_get(reg['sita'])[1]['requests'][0]
    assert row['kind'] == 'wfh' and row['local_date'] == ahead and row['punch_time'] is None
    assert row['punch_direction'] is None


def test_a_manager_files_a_missed_punch_for_a_report_from_fix_punch(demo, reg):
    # FixPunchPage (non-HR): employeeId of a direct report, the +05:45 time; it stays pending for the chain.
    d = workday_ago(2)
    r, body = api_post(reg['ram'], {'employeeId': demo['sita'], 'localDate': d, 'kind': 'missed_punch',
                                    'punchTime': f'{d}T18:00:00+05:45', 'punchDirection': 'out', 'reason': 'device down'})
    assert r.status_code == 201 and body['status'] == 'pending', body
    row = api_get(reg['ram'], {'scope': 'team'})[1]['requests'][0]
    assert row['employee_id'] == demo['sita'] and row['created_by'] == reg['ram'].user_id
