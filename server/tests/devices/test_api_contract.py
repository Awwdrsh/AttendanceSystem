"""Every /api/v1/devices and /api/v1/biometrics call the React app makes (frontend/src/api/index.js `devices` and
`biometrics`), sent the way client.js sends it: the session cookie, no CSRF token, the page's own Origin, and no body
at all on the bodiless POSTs. Each response is compared key by key with the columns the TS route (devices/routes.ts,
biometrics/routes.ts at 65a9079) selected, including the bigint counters the TS API sent as strings."""
import json

import pytest
from django.test import Client

from hajiri.db.models import Company, Employee
from tests.core.punches import insert_punch
from tests.support.sql import one
from tests.web.factories import make_user, sign_in

from .test_biometrics import insert_template

SELF = {'HTTP_ORIGIN': 'http://testserver'}
EVIL = {'HTTP_ORIGIN': 'http://evil.example'}

DEVICE_KEYS = {'id', 'company_id', 'branch_id', 'serial_no', 'vendor', 'protocol', 'name', 'status', 'fw_version',
               'push_version', 'tz_offset_min', 'last_handshake_at', 'last_seen_at', 'created_at', 'offline',
               'mapped_users', 'queued_commands'}
PUNCH_KEYS = {'id', 'device_id', 'device_sn', 'device_user_pin', 'punched_at', 'local_date', 'verify_mode', 'total_for_pin'}
MAPPING_KEYS = {'device_user_pin', 'employee_id', 'emp_code', 'name_en', 'valid'}
COMMAND_KEYS = {'id', 'command', 'status', 'return_code', 'queued_at', 'sent_at', 'acked_at'}
CONSENT_KEYS = {'id', 'purpose', 'modality', 'notice_version', 'granted_at', 'revoked_at'}
TEMPLATE_KEYS = {'id', 'modality', 'vendor_format', 'device_user_pin', 'device_sn', 'key_version', 'consent_id',
                 'created_at', 'shredded_at'}


class Browser:
    """client.js: fetch() with credentials, JSON bodies, no X-CSRFToken; the browser adds the page's Origin."""

    def __init__(self, user, origin=SELF):
        self.c = Client(enforce_csrf_checks=True)
        sign_in(self.c, user)
        self.h = origin

    def get(self, path, query=None):
        return self.c.get(f'/api/v1{path}', query or {})

    def post(self, path, body=None):
        if body is None:                     # api('POST', path) with no body: no Content-Type, empty request body
            return self.c.generic('POST', f'/api/v1{path}', b'', **self.h)
        return self.c.post(f'/api/v1{path}', json.dumps(body), content_type='application/json', **self.h)


@pytest.fixture
def company(demo):
    return Company.objects.get(pk=demo['company_id'])


@pytest.fixture
def hr(company):
    return Browser(make_user(company, roles=['hr']))


@pytest.fixture
def sita(company, demo):
    return Browser(make_user(company, roles=['employee'], employee=Employee.objects.get(pk=demo['sita'])))


def _iso_z(v):
    return isinstance(v, str) and v.endswith('Z') and 'T' in v


# ---------------------------------------------------------------- DevicesPage.jsx

def test_devices_page_calls_with_the_cookie_session(demo, hr):
    dev = demo['device_id']
    # list(): {devices: [...]}, every TS column, counters as strings, offline a boolean
    r = hr.get('/devices')
    assert r.status_code == 200 and set(r.json()) == {'devices'}
    d = next(x for x in r.json()['devices'] if x['id'] == dev)
    assert set(d) == DEVICE_KEYS
    assert isinstance(d['offline'], bool) and isinstance(d['mapped_users'], str) and isinstance(d['queued_commands'], str)
    assert d['company_id'] == demo['company_id'] and isinstance(d['tz_offset_min'], int) and _iso_z(d['created_at'])

    # unmapped(): {punches: [...]}
    insert_punch(None, '2026-09-28', '10:00', '77')
    p = hr.get('/devices/punches/unmapped').json()
    assert set(p) == {'punches'}
    row = next(x for x in p['punches'] if x['device_user_pin'] == '77')
    assert set(row) == PUNCH_KEYS
    assert row['device_id'] == dev and row['local_date'] == '2026-09-28' and _iso_z(row['punched_at'])
    assert row['total_for_pin'] == '1'

    # map(): 201 {ok, reattributed, dates, movedFrom}; the page reads r.reattributed
    m = hr.post(f'/devices/{dev}/map', {'pin': '77', 'employeeId': demo['ram'], 'from': '2026-09-28'})
    assert m.status_code == 201 and m.json() == {'ok': True, 'reattributed': 1, 'dates': ['2026-09-28'], 'movedFrom': []}
    # a conflicting PIN: 409 with the message the page shows, then {replace: true} moves it
    c = hr.post(f'/devices/{dev}/map', {'pin': '77', 'employeeId': demo['sita'], 'from': '2026-09-28'})
    assert c.status_code == 409 and set(c.json()) == {'error'} and 'already mapped to' in c.json()['error']
    ok = hr.post(f'/devices/{dev}/map', {'pin': '77', 'employeeId': demo['sita'], 'from': '2026-09-28', 'replace': True})
    assert ok.status_code == 201 and ok.json()['movedFrom'] == [demo['ram']]

    # mappings(): {mappings: [...]}, valid as PostgreSQL daterange text
    ms = hr.get(f'/devices/{dev}/mappings').json()
    assert set(ms) == {'mappings'}
    mine = [x for x in ms['mappings'] if x['device_user_pin'] == '77']
    assert mine and set(mine[0]) == MAPPING_KEYS and mine[0]['valid'] == '[2026-09-28,)'
    assert mine[0]['employee_id'] == demo['sita'] and mine[0]['name_en']

    # command(): 201 {id: number}; commands(): {commands: [...]}, id as string, return_code null until acked
    q = hr.post(f'/devices/{dev}/commands', {'command': 'INFO'})
    assert q.status_code == 201 and set(q.json()) == {'id'} and isinstance(q.json()['id'], int)
    cs = hr.get(f'/devices/{dev}/commands').json()
    assert set(cs) == {'commands'}
    first = cs['commands'][0]
    assert set(first) == COMMAND_KEYS and first['id'] == str(q.json()['id'])
    assert first['command'] == 'INFO' and first['status'] == 'queued' and first['return_code'] is None
    assert _iso_z(first['queued_at']) and first['sent_at'] is None

    # syncUsers(): no body, {queued}
    s = hr.post(f'/devices/{dev}/sync-users')
    assert s.status_code == 200 and set(s.json()) == {'queued'} and isinstance(s.json()['queued'], int)

    # issueKey(): no body; ADMS terminals are refused with {error}
    k = hr.post(f'/devices/{dev}/key')
    assert k.status_code == 400 and set(k.json()) == {'error'}
    isapi = str(one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) "
                    "VALUES (%s, 'HIK-1', 'hikvision', 'isapi', 'active') RETURNING id", [demo['company_id']])['id'])
    k = hr.post(f'/devices/{isapi}/key')
    assert k.status_code == 201 and set(k.json()) == {'key'} and len(k.json()['key']) >= 32

    # activate(): {} or {name}; disable(): no body; both {id, serial_no, status}
    pending = str(one("INSERT INTO device (serial_no, vendor, protocol) VALUES ('ZK-NEW', 'zkteco', 'adms') RETURNING id")['id'])
    a = hr.post(f'/devices/{pending}/activate', {})
    assert a.status_code == 200 and a.json() == {'id': pending, 'serial_no': 'ZK-NEW', 'status': 'active'}
    a = hr.post(f'/devices/{pending}/activate', {'name': 'Front door'})
    assert a.status_code == 200
    assert next(x for x in hr.get('/devices').json()['devices'] if x['id'] == pending)['name'] == 'Front door'
    d = hr.post(f'/devices/{pending}/disable')
    assert d.status_code == 200 and d.json() == {'id': pending, 'serial_no': 'ZK-NEW', 'status': 'disabled'}


def test_a_cross_site_post_with_the_cookie_is_refused(demo, company):
    evil = Browser(make_user(company, roles=['hr']), origin=EVIL)
    r = evil.post(f"/devices/{demo['device_id']}/commands", {'command': 'INFO'})
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert evil.post(f"/biometrics/employees/{demo['sita']}/shred-all").status_code == 403
    # reads are not state-changing and pass
    assert evil.get('/devices').status_code == 200


def test_an_employee_gets_403_from_every_devices_call(demo, sita):
    dev = demo['device_id']
    for r in (sita.get('/devices'), sita.get('/devices/punches/unmapped'), sita.get(f'/devices/{dev}/mappings'),
              sita.get(f'/devices/{dev}/commands'), sita.post(f'/devices/{dev}/sync-users'),
              sita.post(f'/devices/{dev}/disable'), sita.post(f'/devices/{dev}/commands', {'command': 'INFO'})):
        assert r.status_code == 403 and r.json() == {'error': 'requires role hr or admin'}


# ---------------------------------------------------------------- MyDataPage.jsx and EmployeeDetailPage.jsx

def test_my_data_page_calls_with_the_cookie_session(demo, sita):
    me = demo['sita']
    t = sita.get('/biometrics/transparency').json()
    assert set(t) == {'stored', 'notStored', 'retention', 'access', 'withdraw', 'legalBasis'}
    assert isinstance(t['stored']['en'], list) and isinstance(t['withdraw']['en'], str)

    c = sita.post('/biometrics/consent', {'employeeId': me, 'modality': 'face', 'noticeVersion': '2026-10'})
    assert c.status_code == 201 and set(c.json()['consent']) == {'id', 'grantedAt'}
    cs = sita.get('/biometrics/consent', {'employeeId': me}).json()
    assert set(cs) == {'consents'} and set(cs['consents'][0]) == CONSENT_KEYS
    assert _iso_z(cs['consents'][0]['granted_at']) and cs['consents'][0]['revoked_at'] is None

    insert_template(demo, me, 'face')
    ts = sita.get('/biometrics/templates', {'employeeId': me}).json()
    assert set(ts) == {'templates'} and set(ts['templates'][0]) == TEMPLATE_KEYS
    assert ts['templates'][0]['shredded_at'] is None and _iso_z(ts['templates'][0]['created_at'])

    r = sita.post(f"/biometrics/consent/{cs['consents'][0]['id']}/revoke")
    assert r.status_code == 200 and r.json() == {'ok': True, 'templatesShredded': 1}
    after = sita.get('/biometrics/consent', {'employeeId': me}).json()['consents'][0]
    assert _iso_z(after['revoked_at'])

    # dsar(): download() of the JSON export
    d = sita.get('/biometrics/dsar', {'employeeId': me})
    assert d.status_code == 200 and d['Content-Type'].startswith('application/json')
    body = d.json()
    assert set(body) == {'generatedAt', 'employee', 'consents', 'templates', 'attendance', 'punches', 'leaveLedger',
                         'requests', 'notifications', 'retention'}
    assert body['employee']['id'] == me and 'has_pan' in body['employee'] and 'pan_enc' not in body['employee']


def test_employee_detail_page_biometrics_calls_with_the_cookie_session(demo, hr):
    emp = demo['ram']
    c = hr.post('/biometrics/consent', {'employeeId': emp, 'modality': 'fingerprint', 'noticeVersion': '2026-10'})
    assert c.status_code == 201
    insert_template(demo, emp, 'fingerprint')
    assert len(hr.get('/biometrics/templates', {'employeeId': emp}).json()['templates']) == 1
    r = hr.post(f'/biometrics/employees/{emp}/shred-all')
    assert r.status_code == 200 and r.json() == {'ok': True, 'shredded': 1}
    t = hr.get('/biometrics/templates', {'employeeId': emp}).json()['templates'][0]
    assert _iso_z(t['shredded_at'])
    assert hr.get('/biometrics/consent', {'employeeId': emp}).json()['consents'][0]['revoked_at'] is not None
