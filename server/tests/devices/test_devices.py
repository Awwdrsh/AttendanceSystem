"""Device PIN mapping, keys, commands and tenancy: the device cases of attendance.test.ts, fixes-attendance.test.ts,
qa-foundation.test.ts and security-authz.test.ts, plus the Devices page (DevicesPage.tsx) through the test client."""
import hashlib
import re

import pytest
from django.test import Client

from hajiri.attendance.service import list_days, recompute_day
from hajiri.core import auth
from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.devices import services as dev
from tests.core.fixtures import PW
from tests.core.punches import insert_punch
from tests.web.factories import sign_in

OTHER = '00000000-0000-7000-8000-00000000aaaa'


@pytest.fixture
def hr(login_as):
    return login_as(['hr'])


@pytest.fixture
def other_company(db):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHERCO', 'Other') ON CONFLICT DO NOTHING", [OTHER])
    return OTHER


def client_for(login):
    c = Client()
    sign_in(c, login.email)
    return c


def day(emp, d):
    return list_days(emp, d, d)[0]


def unmapped_for(user, pin):
    return [p for p in dev.unmapped_punches(user) if p['device_user_pin'] == pin]


# ---------------------------------------------------------------- attendance.test.ts: devices

def test_mapping_a_pin_reattributes_unmapped_punches_via_superseding_rows_and_recomputes(demo, hr):
    insert_punch(None, '2026-09-28', '10:00', '7')
    insert_punch(None, '2026-09-28', '18:00', '7')
    assert len(unmapped_for(hr.user, '7')) == 2
    m = dev.map_pin(hr.user, demo['device_id'], {'pin': '7', 'employeeId': demo['ram'], 'from': '2026-09-01'})
    assert m['reattributed'] == 1 and m['dates'] == ['2026-09-28']
    assert unmapped_for(hr.user, '7') == []
    d = day(demo['ram'], '2026-09-28')
    assert d['status'] == 'Present' and d['worked_min'] == 480
    cmd = one('SELECT command FROM device_command WHERE device_id = %s ORDER BY id DESC LIMIT 1', [demo['device_id']])
    assert re.match(r'^DATA UPDATE USERINFO PIN=7\t', cmd['command'])


def test_overlapping_mapping_for_a_different_employee_needs_confirmation_then_is_replaced(demo, hr):
    dev.map_pin(hr.user, demo['device_id'], {'pin': '7', 'employeeId': demo['ram'], 'from': '2026-09-01'})
    with pytest.raises(ConflictError):
        dev.map_pin(hr.user, demo['device_id'], {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-10-01'})
    dev.map_pin(hr.user, demo['device_id'], {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-10-01', 'replace': True})
    r = rows("SELECT employee_id, valid::text AS valid FROM device_user_map WHERE device_id = %s AND device_user_pin = '7' ORDER BY lower(valid)",
             [demo['device_id']])
    assert len(r) == 2
    assert (str(r[0]['employee_id']), r[0]['valid']) == (demo['ram'], '[2026-09-01,2026-10-01)')


# ---------------------------------------------------------------- fixes-attendance.test.ts: device PIN re-mapping

def test_replacing_a_mapped_pin_moves_punches_since_from_both_ways_and_recomputes_both(demo, hr):
    for d in ('2026-09-03', '2026-09-10'):
        insert_punch(demo['sita'], d, '10:00')
        insert_punch(demo['sita'], d, '18:00')
        recompute_day(demo['sita'], d)

    def mp(**body):
        return dev.map_pin(hr.user, demo['device_id'], {'pin': '1', **body})

    with pytest.raises(ConflictError, match='already mapped to Sita'):
        mp(employeeId=demo['ram'], **{'from': '2026-09-05'})
    assert day(demo['sita'], '2026-09-10')['status'] == 'Present'

    moved = mp(employeeId=demo['ram'], replace=True, **{'from': '2026-09-05'})
    assert moved['dates'] == ['2026-09-10'] and moved['movedFrom'] == [demo['sita']]
    assert day(demo['sita'], '2026-09-03')['status'] == 'Present'      # before `from`: untouched
    assert day(demo['sita'], '2026-09-10')['status'] == 'Absent'
    assert day(demo['ram'], '2026-09-10')['status'] == 'Present'

    back = mp(employeeId=demo['sita'], replace=True, **{'from': '2026-09-05'})
    assert back['reattributed'] == 1
    assert day(demo['sita'], '2026-09-10')['status'] == 'Present'
    assert day(demo['ram'], '2026-09-10')['status'] == 'Absent'
    current = rows("""SELECT employee_id FROM punch_event p WHERE device_user_pin = '1' AND local_date = '2026-09-10'
                        AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)""")
    assert [str(r['employee_id']) for r in current] == [demo['sita'], demo['sita']]


def test_mapping_the_same_employee_again_is_not_a_conflict(demo, hr):
    assert dev.map_pin(hr.user, demo['device_id'], {'pin': '1', 'employeeId': demo['sita'], 'from': '2026-09-05'})['ok'] is True


# ---------------------------------------------------------------- qa-foundation.test.ts: tenancy and devices

def test_hr_of_another_company_cannot_read_or_activate_this_companys_device(demo, other_company):
    auth.create_user(company_id=OTHER, email='hr@other.local', name='o', password=PW, roles=['hr'])
    other = auth.resolve_session(auth.login('hr@other.local', PW).token)
    assert other.company_id == OTHER
    assert dev.mappings(other, demo['device_id']) == []
    assert dev.commands(other, demo['device_id']) == []
    with pytest.raises(NotFoundError):
        dev.activate(other, demo['device_id'], {})


def test_mapping_from_a_date_after_the_punches_leaves_them_unmapped_superseding_rows_get_distinct_keys(demo, hr):
    insert_punch(None, '2026-09-10', '10:00', '9')
    insert_punch(None, '2026-09-10', '18:00', '9')
    later = dev.map_pin(hr.user, demo['device_id'], {'pin': '9', 'employeeId': demo['sita'], 'from': '2026-09-11'})
    assert later['reattributed'] == 0
    earlier = dev.map_pin(hr.user, demo['device_id'], {'pin': '9', 'employeeId': demo['sita'], 'from': '2026-09-01'})
    assert earlier['reattributed'] == 1 and earlier['dates'] == ['2026-09-10']
    keys = one("SELECT count(*)::int AS n, count(DISTINCT idempotency_key)::int AS distinct FROM punch_event WHERE device_user_pin = '9'")
    assert keys == {'n': 4, 'distinct': 4}
    assert unmapped_for(hr.user, '9') == []


def test_terminal_user_names_in_commands_never_contain_control_characters(demo, hr):
    e = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'CR1', %s) RETURNING id",
                [demo['company_id'], 'Bad\r\nName\tHere'])['id'])
    assert dev.map_pin(hr.user, demo['device_id'], {'pin': '77', 'employeeId': e, 'from': '2026-09-01'})['ok']
    cmd = one("SELECT command FROM device_command WHERE command LIKE 'DATA UPDATE USERINFO PIN=77%%'")
    assert cmd['command'] == 'DATA UPDATE USERINFO PIN=77\tName=Bad Name Here\tPri=0'


def test_only_allow_listed_commands_can_be_queued_and_pseudo_devices_cannot_be_disabled(demo, hr):
    with pytest.raises(ValidationError):
        dev.queue_command(hr.user, demo['device_id'], {'command': 'CLEAR DATA'})
    assert isinstance(dev.queue_command(hr.user, demo['device_id'], {'command': 'CHECK'})['id'], int)
    manual = str(one("""INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'MANUAL-QA', 'manual', 'manual', 'active')
                         RETURNING id""", [demo['company_id']])['id'])
    with pytest.raises(NotFoundError):
        dev.disable(hr.user, manual)


# ---------------------------------------------------------------- security-authz.test.ts: devices

def test_a_pin_with_control_characters_cannot_inject_commands_and_a_foreign_branch_cannot_be_attached(demo, hr, other_company):
    other_branch = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'OB', 'Other branch') RETURNING id", [OTHER])['id'])
    did = str(one("""INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'SEC-ZK-1', 'zkteco', 'adms', 'active')
                      RETURNING id""", [demo['company_id']])['id'])
    for pin in ('7\tPri=14', '7\nC:1:CLEAR DATA', '7\n'):
        with pytest.raises(ValidationError) as e:
            dev.map_pin(hr.user, did, {'pin': pin, 'employeeId': demo['sita'], 'from': '2026-01-01'})
        assert e.value.status == 400
    assert dev.map_pin(hr.user, did, {'pin': '7', 'employeeId': demo['sita'], 'from': '2026-01-01'})['ok']
    assert one("SELECT count(*)::int AS n FROM device_command WHERE device_id = %s AND command LIKE '%%Pri=14%%'", [did])['n'] == 0
    with pytest.raises(NotFoundError, match='branch not found'):
        dev.activate(hr.user, did, {'branchId': other_branch})


# ---------------------------------------------------------------- lifecycle and listing

def test_pending_device_without_a_company_is_listed_activated_and_disabled(demo, hr):
    pid = str(one("INSERT INTO device (serial_no, vendor, protocol, status) VALUES ('ZK-NEW-1', 'zkteco', 'adms', 'pending') RETURNING id")['id'])
    listed = {str(d['id']): d for d in dev.list_devices(hr.user)}
    assert listed[pid]['status'] == 'pending' and listed[pid]['offline'] is True
    assert listed[demo['device_id']]['mapped_users'] == 2
    r = dev.activate(hr.user, pid, {'name': 'Back door', 'branchId': demo['branch_id']})
    assert r == {'id': pid, 'serial_no': 'ZK-NEW-1', 'status': 'active'}
    row = one('SELECT company_id, branch_id, name FROM device WHERE id = %s', [pid])
    assert (str(row['company_id']), str(row['branch_id']), row['name']) == (demo['company_id'], demo['branch_id'], 'Back door')
    assert dev.disable(hr.user, pid)['status'] == 'disabled'
    assert [a['action'] for a in rows("SELECT action FROM audit_log WHERE table_name = 'device' AND row_id = %s ORDER BY id", [pid])] == ['activate', 'disable']


def test_sync_users_queues_one_userinfo_per_current_active_mapping(demo, hr):
    assert dev.sync_users(hr.user, demo['device_id']) == {'queued': 2}
    cmds = [c['command'] for c in dev.commands(hr.user, demo['device_id'])]
    assert 'DATA UPDATE USERINFO PIN=1\tName=Sita Sharma\tPri=0' in cmds
    assert 'DATA UPDATE USERINFO PIN=2\tName=Ram Thapa\tPri=0' in cmds


def test_device_name_matches_the_ts_rules():
    assert dev.device_name('  a\x00b\x7fc  ') == 'a b c'
    assert dev.device_name('x' * 30) == 'x' * 24


# ---------------------------------------------------------------- the Devices page
