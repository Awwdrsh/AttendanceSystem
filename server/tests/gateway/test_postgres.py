"""Port of apps/gateway/src/postgres.test.ts: the PostgreSQL store end to end, on the Django test database."""
import base64

import pytest
from django.db import IntegrityError, transaction

from hajiri.core.crypto import FieldCipher
from hajiri.core.db import execute, one, rows
from hajiri.gateway.jsutil import dt_from_ms, utc_ms
from hajiri.gateway.punch import Signer, TemplateCipher
from hajiri.gateway.reconcile import reconcile_day
from hajiri.gateway.store import COMMAND_ACK_TIMEOUT_MS, CommandRefused, PostgresStore

KEY = bytes([5]) * 32
C = {'company': '00000000-0000-7000-8000-000000000001', 'device': '00000000-0000-7000-8000-000000000021',
     'emp': '00000000-0000-7000-8000-000000000011'}
ATTLOG = '1\t2026-09-21 10:17:03\t0\t1\t0\n1\t2026-09-21 10:17:03\t0\t1\t0\n1\t2026-09-21 10:17:05\t0\t1\t0\n7\t2026-09-21 10:30:00\t0\t15\t0\n'

pytestmark = pytest.mark.django_db


@pytest.fixture
def pg(make_app):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'T', 'T')", [C['company']])
    execute("INSERT INTO employee (id, company_id, emp_code, name_en) VALUES (%s, %s, 'E1', 'Sita')", [C['emp'], C['company']])
    execute("INSERT INTO device (id, company_id, serial_no, vendor, protocol, status) VALUES (%s, %s, 'PG-01', 'zkteco', 'adms', 'active')",
            [C['device'], C['company']])
    execute("INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES (%s, '1', %s, daterange('2026-01-01', NULL))",
            [C['device'], C['emp']])
    store = PostgresStore(max_pending_devices=1)
    app = make_app(store, signer=Signer.ephemeral_key(), cipher=TemplateCipher(KEY))
    return store, app


def test_stores_punches_dedupes_queues_recompute_records_drift(pg):
    store, app = pg
    r = app.post('/iclock/cdata?SN=PG-01&table=ATTLOG&Stamp=5', ATTLOG)
    assert r.text == 'OK: 4'
    punches = rows('SELECT device_user_pin, employee_id::text, verify_mode, local_date::text FROM punch_event ORDER BY punched_at')
    assert punches == [{'device_user_pin': '1', 'employee_id': C['emp'], 'verify_mode': 'fingerprint', 'local_date': '2026-09-21'},
                       {'device_user_pin': '7', 'employee_id': None, 'verify_mode': 'face', 'local_date': '2026-09-21'}]
    q = rows('SELECT employee_id::text, local_date::text, reason FROM recompute_queue ORDER BY local_date')
    assert q == [{'employee_id': C['emp'], 'local_date': '2026-09-20', 'reason': 'punch:zkteco'},
                 {'employee_id': C['emp'], 'local_date': '2026-09-21', 'reason': 'punch:zkteco'}]
    dev = one('SELECT attlog_stamp, clock_drift_sec, last_seen_at FROM device WHERE serial_no = %s', ['PG-01'])
    assert dev['attlog_stamp'] == '5'
    assert dev['last_seen_at'] is not None
    assert store.count_punches(C['device'], '2026-09-21') == 2
    # The stored signature and raw payload are kept as the TS gateway stored them.
    p = one("SELECT raw, gateway_signature FROM punch_event WHERE device_user_pin = '1'")
    assert p['raw'] == {'line': '1\t2026-09-21 10:17:03\t0\t1\t0', 'statusCode': 0, 'verifyCode': 1, 'workcode': '0'}
    assert len(p['gateway_signature']) == 88


def test_encrypts_templates_so_the_api_field_cipher_can_decrypt_them(pg):
    store, app = pg
    execute("INSERT INTO consent_record (employee_id, purpose, modality, notice_version) VALUES (%s, 'attendance', 'fingerprint', 'v1')",
            [C['emp']])
    b64 = base64.b64encode(b'real-template-bytes').decode()
    app.post('/iclock/fdata?SN=PG-01&table=FINGERTMP', f'PIN=1\tFID=6\tSize=1\tValid=1\tTMP={b64}\n',
             content_type='application/octet-stream')
    t = rows('SELECT employee_id::text, ciphertext, aad_hash, modality, consent_id FROM biometric_template')
    assert len(t) == 1
    t = t[0]
    assert t['employee_id'] == C['emp']
    assert t['consent_id'] is not None
    ct = bytes(t['ciphertext'])
    assert b64 not in ct.decode('latin1')
    assert FieldCipher(KEY).decrypt(ct, f"template:{C['emp']}:fingerprint:attendance") == b64
    with pytest.raises(Exception):
        FieldCipher(KEY).decrypt(ct, 'wrong')


def test_pending_cap_and_dangerous_command_refusal_apply_in_postgres_too(pg):
    store, app = pg
    assert app.get('/iclock/cdata?SN=PG-NEW-1&options=all').status == 200
    assert app.get('/iclock/cdata?SN=PG-NEW-2&options=all').status == 403
    with pytest.raises(CommandRefused, match='dangerous'):
        store.enqueue_command(C['device'], 'CLEAR DATA')
    with pytest.raises(IntegrityError, match='no_shell'):
        with transaction.atomic():
            execute("INSERT INTO device_command (device_id, command) VALUES (%s, 'SHELL ls')", [C['device']])


def test_reconcile_day_queues_a_data_query_attlog_for_the_device(pg):
    store, app = pg
    app.post('/iclock/cdata?SN=PG-01&table=ATTLOG&Stamp=5', ATTLOG)
    res = reconcile_day(store, 'PG-01', '2026-09-21')
    assert res['stored'] == 2
    assert isinstance(res['commandId'], int)
    cmd = one('SELECT command, status FROM device_command WHERE id = %s', [res['commandId']])
    assert cmd['command'] == 'DATA QUERY ATTLOG StartTime=2026-09-21 00:00:00\tEndTime=2026-09-21 23:59:59'
    poll = app.get('/iclock/getrequest?SN=PG-01')
    assert f"C:{res['commandId']}:DATA QUERY ATTLOG" in poll.text
    app.post('/iclock/devicecmd?SN=PG-01', f"ID={res['commandId']}&Return=0&CMD=DATA")
    assert one('SELECT status FROM device_command WHERE id = %s', [res['commandId']])['status'] == 'acked'
    assert reconcile_day(store, 'NOPE', '2026-09-21')['note'] == 'unknown device'


def test_reconcile_notes_for_inactive_and_non_adms_devices(pg):
    store, _ = pg
    execute("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'PG-OFF', 'zkteco', 'adms', 'disabled')", [C['company']])
    execute("INSERT INTO device (company_id, serial_no, vendor, protocol, status) VALUES (%s, 'PG-HIK', 'hikvision', 'isapi', 'active')", [C['company']])
    assert reconcile_day(store, 'PG-OFF', '2026-09-21')['note'] == 'device disabled'
    assert reconcile_day(store, 'PG-HIK', '2026-09-21')['note'] == 'reconciliation for this protocol runs from the API'
    assert [d.serial_no for d in store.list_devices(status='active', protocol='adms')] == ['PG-01']


def test_resends_an_unacknowledged_command_once_after_the_ack_timeout_then_fails_it(pg):
    store, _ = pg
    cid = store.enqueue_command(C['device'], 'INFO')
    t0 = utc_ms(2026, 9, 21, 4)
    assert store.pop_queued_commands(C['device'], dt_from_ms(t0)) == [{'id': cid, 'command': 'INFO'}]
    assert store.pop_queued_commands(C['device'], dt_from_ms(t0 + 1000)) == []
    t1 = t0 + COMMAND_ACK_TIMEOUT_MS + 1000
    assert store.pop_queued_commands(C['device'], dt_from_ms(t1)) == [{'id': cid, 'command': 'INFO'}]
    t2 = t1 + COMMAND_ACK_TIMEOUT_MS + 1000
    assert store.pop_queued_commands(C['device'], dt_from_ms(t2)) == []
    assert one('SELECT status, attempts FROM device_command WHERE id = %s', [cid]) == {'status': 'failed', 'attempts': 2}
    store.ack_command(C['device'], cid, 0)
    assert one('SELECT status FROM device_command WHERE id = %s', [cid])['status'] == 'failed'


def test_a_newline_smuggled_command_is_rejected_before_it_reaches_the_database(pg):
    store, _ = pg
    with pytest.raises(CommandRefused, match='single line'):
        store.enqueue_command(C['device'], 'CHECK\nSHELL rm -rf /')


def test_hikvision_device_registers_as_pending_in_postgres(pg):
    store, app = pg
    r = app.post('/hikvision/event?sn=PG-HIK-NEW', {'eventType': 'heartBeat'})
    assert r.status == 403
    assert one("SELECT vendor, protocol, status FROM device WHERE serial_no = 'PG-HIK-NEW'") == {
        'vendor': 'hikvision', 'protocol': 'isapi', 'status': 'pending'}
