"""The /api/v1/devices JSON endpoints (devices/routes.ts) as the React Devices page calls them: the HTTP-level
assertions of endpoints.test.ts, attendance.test.ts, fixes-attendance.test.ts, qa-foundation.test.ts and
security-authz.test.ts, plus response shapes, validation, sign-in and tenancy."""
import hashlib
import re

import pytest

from hajiri.attendance.service import list_days, recompute_day
from hajiri.db.models import Employee
from tests.core.punches import insert_punch
from tests.support.sql import execute, one, rows

from .jsonapi import Api

OTHER = '00000000-0000-7000-8000-00000000aaaa'
OTHER_BRANCH = '00000000-0000-7000-8000-00000000aab1'
MISSING = '00000000-0000-7000-8000-0000000000ff'


@pytest.fixture
def hr(login_as):
    return Api(login_as(['hr']).token)


@pytest.fixture
def sita(login_as, demo):
    return Api(login_as(['employee'], demo['sita']).token)


@pytest.fixture
def other_company(db):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHERCO', 'Other') ON CONFLICT DO NOTHING", [OTHER])
    execute("INSERT INTO branch (id, company_id, code, name_en) VALUES (%s, %s, 'OB', 'Other branch') ON CONFLICT DO NOTHING",
            [OTHER_BRANCH, OTHER])
    return OTHER


@pytest.fixture
def other_hr(other_company, login_as):
    return Api(login_as(['hr'], company_id=other_company).token)


def url(device_id, action=''):
    return f'/api/v1/devices/{device_id}' + (f'/{action}' if action else '')


def day_status(emp, d):
    return list_days(emp, d, d)[0]['status']


# ---------------------------------------------------------------- endpoints.test.ts

def test_list_devices_with_health_counters(demo, hr, sita):
    r = hr.get('/api/v1/devices')
    assert r.status_code == 200
    d = next(x for x in r.json()['devices'] if x['id'] == demo['device_id'])
    assert d['serial_no'] == 'DEMO-01' and d['offline'] is True
    # PostgreSQL count(*) is a bigint, which the TS API sent as a string.
    assert d['mapped_users'] == '2' and d['queued_commands'] == '0'
    assert set(d) >= {'company_id', 'branch_id', 'vendor', 'protocol', 'name', 'status', 'fw_version', 'push_version',
                      'tz_offset_min', 'last_handshake_at', 'last_seen_at', 'created_at'}
    assert d['created_at'].endswith('Z')
    assert sita.get('/api/v1/devices').status_code == 403
    assert sita.get('/api/v1/devices').json() == {'error': 'requires role hr or admin'}


def test_sync_users_queues_one_userinfo_command_per_mapped_active_employee(demo, hr, sita):
    r = hr.post(url(demo['device_id'], 'sync-users'))
    assert r.status_code == 200 and r.json() == {'queued': 2}
    cmds = [c['command'] for c in rows("SELECT command FROM device_command WHERE device_id = %s AND status = 'queued' ORDER BY command",
                                       [demo['device_id']])]
    assert re.match(r'^DATA UPDATE USERINFO PIN=1\tName=', cmds[0]) and re.match(r'^DATA UPDATE USERINFO PIN=2\tName=', cmds[1])
    assert hr.post(url(MISSING, 'sync-users')).json() == {'queued': 0}
    assert sita.post(url(demo['device_id'], 'sync-users')).status_code == 403


# ---------------------------------------------------------------- attendance.test.ts: devices

def test_mapping_a_pin_reattributes_unmapped_punches_and_recomputes(demo, hr):
    insert_punch(None, '2026-09-28', '10:00', '7')
    insert_punch(None, '2026-09-28', '18:00', '7')
    un = hr.get('/api/v1/devices/punches/unmapped').json()['punches']
    mine = [p for p in un if p['device_user_pin'] == '7']
    assert len(mine) == 2
    assert mine[0]['total_for_pin'] == '2' and mine[0]['local_date'] == '2026-09-28' and mine[0]['device_sn'] == 'DEMO-01'
    m = hr.post(url(demo['device_id'], 'map'), {'pin': '7', 'employeeId': demo['ram'], 'from': '2026-09-01'})
    assert m.status_code == 201
    assert m.json() == {'ok': True, 'reattributed': 1, 'dates': ['2026-09-28'], 'movedFrom': []}
    after = hr.get('/api/v1/devices/punches/unmapped').json()['punches']
    assert [p for p in after if p['device_user_pin'] == '7'] == []
    d = list_days(demo['ram'], '2026-09-28', '2026-09-28')[0]
    assert d['status'] == 'Present' and d['worked_min'] == 480
    cmd = one('SELECT command FROM device_command WHERE device_id = %s ORDER BY id DESC LIMIT 1', [demo['device_id']])
    assert re.match(r'^DATA UPDATE USERINFO PIN=7\t', cmd['command'])


def test_overlapping_mapping_for_a_different_employee_needs_confirmation_then_is_replaced(demo, hr):
    assert hr.post(url(demo['device_id'], 'map'), {'pin': '7', 'employeeId': demo['ram'], 'from': '2026-09-01'}).status_code == 201
    refused = hr.post(url(demo['device_id'], 'map'), {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-10-01'})
    assert refused.status_code == 409
    m = hr.post(url(demo['device_id'], 'map'), {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-10-01', 'replace': True})
    assert m.status_code == 201
    maps = hr.get(url(demo['device_id'], 'mappings')).json()['mappings']
    sevens = [x for x in maps if x['device_user_pin'] == '7']
    assert [(x['employee_id'], x['valid']) for x in sevens] == [(demo['ram'], '[2026-09-01,2026-10-01)'),
                                                               (demo['sita'], '[2026-10-01,)')]
    assert set(sevens[0]) == {'device_user_pin', 'employee_id', 'emp_code', 'name_en', 'valid'}


# ---------------------------------------------------------------- fixes-attendance.test.ts: device PIN re-mapping

def test_replacing_a_mapped_pin_moves_punches_both_ways_and_recomputes_both_people(demo, hr):
    for d in ('2026-09-03', '2026-09-10'):
        insert_punch(demo['sita'], d, '10:00')
        insert_punch(demo['sita'], d, '18:00')
        recompute_day(demo['sita'], d)

    def mp(**body):
        return hr.post(url(demo['device_id'], 'map'), {'pin': '1', **body})
    refused = mp(employeeId=demo['ram'], **{'from': '2026-09-05'})
    assert refused.status_code == 409
    assert re.search(r'already mapped to Sita', refused.json()['error'])
    assert day_status(demo['sita'], '2026-09-10') == 'Present'

    moved = mp(employeeId=demo['ram'], replace=True, **{'from': '2026-09-05'})
    assert moved.status_code == 201
    assert moved.json()['dates'] == ['2026-09-10'] and moved.json()['movedFrom'] == [demo['sita']]
    assert day_status(demo['sita'], '2026-09-03') == 'Present'
    assert day_status(demo['sita'], '2026-09-10') == 'Absent'
    assert day_status(demo['ram'], '2026-09-10') == 'Present'

    back = mp(employeeId=demo['sita'], replace=True, **{'from': '2026-09-05'})
    assert back.status_code == 201 and back.json()['reattributed'] == 1
    assert day_status(demo['sita'], '2026-09-10') == 'Present'
    assert day_status(demo['ram'], '2026-09-10') == 'Absent'


def test_mapping_the_same_employee_again_is_not_a_conflict(demo, hr):
    r = hr.post(url(demo['device_id'], 'map'), {'pin': '1', 'employeeId': demo['sita'], 'from': '2026-09-05'})
    assert r.status_code == 201


# ---------------------------------------------------------------- qa-foundation.test.ts

def test_hr_of_another_company_sees_nothing_of_this_companys_device(demo, other_hr):
    assert other_hr.get(url(demo['device_id'], 'mappings')).json() == {'mappings': []}
    assert other_hr.get(url(demo['device_id'], 'commands')).json() == {'commands': []}
    assert other_hr.post(url(demo['device_id'], 'activate'), {}).status_code == 404
    assert other_hr.post(url(demo['device_id'], 'disable')).status_code == 404
    assert other_hr.post(url(demo['device_id'], 'key')).status_code == 404
    assert other_hr.post(url(demo['device_id'], 'commands'), {'command': 'CHECK'}).status_code == 404
    assert other_hr.post(url(demo['device_id'], 'sync-users')).json() == {'queued': 0}
    r = other_hr.post(url(demo['device_id'], 'map'), {'pin': '5', 'employeeId': demo['sita'], 'from': '2026-09-01'})
    assert r.status_code == 404 and r.json() == {'error': 'device not found'}
    assert demo['device_id'] not in {d['id'] for d in other_hr.get('/api/v1/devices').json()['devices']}


def test_mapping_after_the_punches_leaves_them_unmapped_and_superseding_rows_get_distinct_keys(demo, hr):
    insert_punch(None, '2026-09-10', '10:00', '9')
    insert_punch(None, '2026-09-10', '18:00', '9')
    later = hr.post(url(demo['device_id'], 'map'), {'pin': '9', 'employeeId': demo['sita'], 'from': '2026-09-11'})
    assert later.json()['reattributed'] == 0
    earlier = hr.post(url(demo['device_id'], 'map'), {'pin': '9', 'employeeId': demo['sita'], 'from': '2026-09-01'})
    assert earlier.json()['reattributed'] == 1 and earlier.json()['dates'] == ['2026-09-10']
    keys = one("SELECT count(*)::int AS n, count(DISTINCT idempotency_key)::int AS d FROM punch_event WHERE device_user_pin = '9'")
    assert (keys['n'], keys['d']) == (4, 4)
    unmapped = hr.get('/api/v1/devices/punches/unmapped').json()['punches']
    assert [p for p in unmapped if p['device_user_pin'] == '9'] == []


def test_terminal_user_names_in_commands_never_contain_control_characters(demo, hr):
    e = Employee.objects.create(company_id=demo['company_id'], emp_code='CR1', name_en='Bad\r\nName\tHere')
    r = hr.post(url(demo['device_id'], 'map'), {'pin': '77', 'employeeId': str(e.id), 'from': '2026-09-01'})
    assert r.status_code == 201
    cmd = one("SELECT command FROM device_command WHERE command LIKE 'DATA UPDATE USERINFO PIN=77%%'")
    assert cmd['command'] == 'DATA UPDATE USERINFO PIN=77\tName=Bad Name Here\tPri=0'


def test_only_allow_listed_commands_can_be_queued_and_pseudo_devices_cannot_be_disabled(demo, hr):
    bad = hr.post(url(demo['device_id'], 'commands'), {'command': 'CLEAR DATA'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert bad.json()['issues'][0]['path'] == 'command'
    ok = hr.post(url(demo['device_id'], 'commands'), {'command': 'CHECK'})
    assert ok.status_code == 201 and isinstance(ok.json()['id'], int)
    listed = hr.get(url(demo['device_id'], 'commands')).json()['commands']
    assert listed[0]['id'] == str(ok.json()['id']) and listed[0]['command'] == 'CHECK' and listed[0]['status'] == 'queued'
    assert set(listed[0]) == {'id', 'command', 'status', 'return_code', 'queued_at', 'sent_at', 'acked_at'}
    manual = one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'MANUAL-QA', 'manual', 'manual', 'active') RETURNING id",
                 [demo['company_id']])
    assert hr.post(url(manual['id'], 'disable')).status_code == 404


# ---------------------------------------------------------------- security-authz.test.ts

def test_a_pin_with_control_characters_cannot_inject_commands_and_a_foreign_branch_cannot_be_attached(demo, hr, other_company):
    dev = one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'SEC-ZK-1', 'zkteco', 'adms', 'active') RETURNING id",
              [demo['company_id']])
    did = str(dev['id'])
    bad = hr.post(url(did, 'map'), {'pin': '7\tPri=14', 'employeeId': demo['sita'], 'from': '2026-01-01'})
    assert bad.status_code == 400
    assert bad.json()['issues'] == [{'path': 'pin', 'message': 'PIN must be 1 to 24 letters or digits'}]
    assert hr.post(url(did, 'map'), {'pin': '7\nC:1:CLEAR DATA', 'employeeId': demo['sita'], 'from': '2026-01-01'}).status_code == 400
    assert hr.post(url(did, 'map'), {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-01-01'}).status_code == 201
    assert one("SELECT count(*)::int AS n FROM device_command WHERE device_id = %s AND command LIKE '%%Pri=14%%'", [did])['n'] == 0
    r = hr.post(url(did, 'activate'), {'branchId': OTHER_BRANCH})
    assert r.status_code == 404 and r.json() == {'error': 'branch not found'}


def test_hr_issues_a_device_key_once_adms_and_foreign_devices_are_refused(demo, hr, sita, other_company):
    hik = str(one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'HIK-KEY-1', 'hikvision', 'isapi', 'active') RETURNING id",
                  [demo['company_id']])['id'])
    r = hr.post(url(hik, 'key'))
    assert r.status_code == 201
    assert re.fullmatch(r'[A-Za-z0-9_-]{32}', r.json()['key'])
    stored = one('SELECT comm_key_hash FROM device WHERE id = %s', [hik])['comm_key_hash']
    assert stored == hashlib.sha256(r.json()['key'].encode()).hexdigest()
    assert sita.post(url(hik, 'key')).status_code == 403
    adms = hr.post(url(demo['device_id'], 'key'))
    assert adms.status_code == 400
    assert adms.json() == {'error': 'ZKTeco ADMS terminals cannot send a device key; keep them on the office network'}
    foreign = str(one("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'HIK-KEY-2', 'hikvision', 'isapi', 'active') RETURNING id",
                      [other_company])['id'])
    assert hr.post(url(foreign, 'key')).status_code == 404


# ---------------------------------------------------------------- lifecycle and contract details

def test_activate_a_pending_terminal_and_disable_it(demo, hr, other_hr):
    pending = str(one("INSERT INTO device (serial_no, vendor, protocol) VALUES ('NEW-ZK-1', 'zkteco', 'adms') RETURNING id")['id'])
    # An unclaimed terminal is offered to every company until one activates it.
    assert pending in {d['id'] for d in other_hr.get('/api/v1/devices').json()['devices']}
    a = hr.post(url(pending, 'activate'), {'branchId': demo['branch_id'], 'name': 'Back door'})
    assert a.status_code == 200 and a.json() == {'id': pending, 'serial_no': 'NEW-ZK-1', 'status': 'active'}
    row = one('SELECT company_id, branch_id, name FROM device WHERE id = %s', [pending])
    assert (str(row['company_id']), str(row['branch_id']), row['name']) == (demo['company_id'], demo['branch_id'], 'Back door')
    assert pending not in {d['id'] for d in other_hr.get('/api/v1/devices').json()['devices']}
    assert other_hr.post(url(pending, 'activate'), {}).status_code == 404
    d = hr.post(url(pending, 'disable'))
    assert d.status_code == 200 and d.json() == {'id': pending, 'serial_no': 'NEW-ZK-1', 'status': 'disabled'}
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action IN ('activate', 'disable') AND row_id = %s", [pending])['n'] == 2


def test_validation_sign_in_and_methods(demo, hr, sita):
    assert Api(None).get('/api/v1/devices').status_code == 401
    assert Api(None).get('/api/v1/devices').json() == {'error': 'unauthorized'}
    # The role check comes before the id is read, as the TS router-level requireRole did.
    assert sita.post(url('nope', 'disable')).status_code == 403
    r = hr.post(url('nope', 'disable'))
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'Invalid UUID'
    r = hr.post(url(demo['device_id'], 'map'), {'pin': '7'})
    assert r.status_code == 400
    paths = {i['path'] for i in r.json()['issues']}
    assert {'employeeId', 'from'} <= paths
    assert hr.post(url(demo['device_id'], 'map'), {'pin': '7', 'employeeId': MISSING, 'from': '2026-09-01'}).json() == \
        {'error': 'employee not found'}
    assert hr.post(url(MISSING, 'activate'), {}).json() == {'error': 'device not found'}
    assert hr.post(url(demo['device_id'], 'activate'), {'name': 'x' * 121}).status_code == 400
    assert hr.post_raw(url(demo['device_id'], 'commands'), '{"broken"').status_code == 400
    assert hr.delete('/api/v1/devices').status_code == 404
    assert hr.get(url(demo['device_id'], 'map')).status_code == 404
