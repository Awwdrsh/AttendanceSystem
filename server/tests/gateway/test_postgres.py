"""Port of apps/gateway/src/postgres.test.ts: the PostgreSQL store end to end, on the Django test database."""
import base64
from datetime import date

import pytest
from django.db import IntegrityError, transaction

from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.core.crypto import FieldCipher
from hajiri.db.models import (
    BiometricTemplate, Company, ConsentRecord, Device, DeviceCommand, DeviceUserMap, Employee, PunchEvent, RecomputeQueue,
)
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
    Company.objects.create(id=C['company'], code='T', name_en='T')
    Employee.objects.create(id=C['emp'], company_id=C['company'], emp_code='E1', name_en='Sita')
    Device.objects.create(id=C['device'], company_id=C['company'], serial_no='PG-01', vendor=Device.Vendor.ZKTECO,
                          protocol=Device.Protocol.ADMS, status=Device.Status.ACTIVE)
    DeviceUserMap.objects.create(device_id=C['device'], device_user_pin='1', employee_id=C['emp'],
                                 valid=DateRange(date(2026, 1, 1), None))
    store = PostgresStore(max_pending_devices=1)
    app = make_app(store, signer=Signer.ephemeral_key(), cipher=TemplateCipher(KEY))
    return store, app


def test_stores_punches_dedupes_queues_recompute_records_drift(pg):
    store, app = pg
    r = app.post('/iclock/cdata?SN=PG-01&table=ATTLOG&Stamp=5', ATTLOG)
    assert r.text == 'OK: 4'
    punches = [(pin, emp and str(emp), mode, str(d)) for pin, emp, mode, d in PunchEvent.objects.order_by('punched_at')
               .values_list('device_user_pin', 'employee_id', 'verify_mode', 'local_date')]
    assert punches == [('1', C['emp'], 'fingerprint', '2026-09-21'), ('7', None, 'face', '2026-09-21')]
    q = [(str(e), str(d), r) for e, d, r in RecomputeQueue.objects.order_by('local_date')
         .values_list('employee_id', 'local_date', 'reason')]
    assert q == [(C['emp'], '2026-09-20', 'punch:zkteco'), (C['emp'], '2026-09-21', 'punch:zkteco')]
    dev = Device.objects.get(serial_no='PG-01')
    assert dev.attlog_stamp == '5'
    assert dev.last_seen_at is not None
    assert store.count_punches(C['device'], '2026-09-21') == 2
    # The stored signature and raw payload are kept as the TS gateway stored them.
    p = PunchEvent.objects.values('raw', 'gateway_signature').get(device_user_pin='1')
    assert p['raw'] == {'line': '1\t2026-09-21 10:17:03\t0\t1\t0', 'statusCode': 0, 'verifyCode': 1, 'workcode': '0'}
    assert len(p['gateway_signature']) == 88


def test_encrypts_templates_so_the_api_field_cipher_can_decrypt_them(pg):
    store, app = pg
    ConsentRecord.objects.create(employee_id=C['emp'], purpose='attendance', modality='fingerprint', notice_version='v1')
    b64 = base64.b64encode(b'real-template-bytes').decode()
    app.post('/iclock/fdata?SN=PG-01&table=FINGERTMP', f'PIN=1\tFID=6\tSize=1\tValid=1\tTMP={b64}\n',
             content_type='application/octet-stream')
    t = list(BiometricTemplate.objects.values('employee_id', 'ciphertext', 'aad_hash', 'modality', 'consent_id'))
    assert len(t) == 1
    t = t[0]
    assert str(t['employee_id']) == C['emp']
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
            DeviceCommand.objects.create(device_id=C['device'], command='SHELL ls')


def test_reconcile_day_queues_a_data_query_attlog_for_the_device(pg):
    store, app = pg
    app.post('/iclock/cdata?SN=PG-01&table=ATTLOG&Stamp=5', ATTLOG)
    res = reconcile_day(store, 'PG-01', '2026-09-21')
    assert res['stored'] == 2
    assert isinstance(res['commandId'], int)
    cmd = DeviceCommand.objects.values('command', 'status').get(id=res['commandId'])
    assert cmd['command'] == 'DATA QUERY ATTLOG StartTime=2026-09-21 00:00:00\tEndTime=2026-09-21 23:59:59'
    poll = app.get('/iclock/getrequest?SN=PG-01')
    assert f"C:{res['commandId']}:DATA QUERY ATTLOG" in poll.text
    app.post('/iclock/devicecmd?SN=PG-01', f"ID={res['commandId']}&Return=0&CMD=DATA")
    assert DeviceCommand.objects.get(id=res['commandId']).status == DeviceCommand.Status.ACKED
    assert reconcile_day(store, 'NOPE', '2026-09-21')['note'] == 'unknown device'


def test_reconcile_notes_for_inactive_and_non_adms_devices(pg):
    store, _ = pg
    Device.objects.create(company_id=C['company'], serial_no='PG-OFF', vendor='zkteco', protocol='adms', status='disabled')
    Device.objects.create(company_id=C['company'], serial_no='PG-HIK', vendor='hikvision', protocol='isapi', status='active')
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
    assert DeviceCommand.objects.values('status', 'attempts').get(id=cid) == {'status': 'failed', 'attempts': 2}
    store.ack_command(C['device'], cid, 0)
    assert DeviceCommand.objects.get(id=cid).status == DeviceCommand.Status.FAILED


def test_a_newline_smuggled_command_is_rejected_before_it_reaches_the_database(pg):
    store, _ = pg
    with pytest.raises(CommandRefused, match='single line'):
        store.enqueue_command(C['device'], 'CHECK\nSHELL rm -rf /')


def test_hikvision_device_registers_as_pending_in_postgres(pg):
    store, app = pg
    r = app.post('/hikvision/event?sn=PG-HIK-NEW', {'eventType': 'heartBeat'})
    assert r.status == 403
    assert Device.objects.values('vendor', 'protocol', 'status').get(serial_no='PG-HIK-NEW') == {
        'vendor': 'hikvision', 'protocol': 'isapi', 'status': 'pending'}
