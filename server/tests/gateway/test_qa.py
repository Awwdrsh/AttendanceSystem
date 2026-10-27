"""Port of apps/gateway/src/qa.test.ts."""
import hashlib
import json
import os
from urllib.parse import quote

import pytest
from django.db.utils import OperationalError

from hajiri.gateway.adms import is_valid_device_time, parse_attlog
from hajiri.gateway.archive import RawArchive, safe_serial
from hajiri.gateway.hikvision import extract_event_json, parse_iso_with_offset, parse_multipart, xml_event_to_json
from hajiri.gateway.jsutil import dt_from_ms, iso_ms, utc_ms
from hajiri.gateway.punch import DeviceTimeError, device_time_to_utc
from hajiri.gateway.store import COMMAND_ACK_TIMEOUT_MS, CommandRefused, MemoryStore, assert_allowed_command


@pytest.fixture
def qa():
    store = MemoryStore()
    device_id = store.add_device('DEMO-01').id
    store.mappings[f'{device_id}:1'] = 'emp-1'
    return store, device_id


def attlog(app, sn, body):
    return app.post(f'/iclock/cdata?SN={quote(sn, safe="")}&table=ATTLOG', body)


class TestSerialsAndArchivePaths:
    def test_sanitises_serials_so_they_can_never_leave_the_archive_directory(self):
        assert safe_serial('..') == 'unknown'
        assert safe_serial('../../etc') == '______etc'
        assert safe_serial('A/B\\C.D') == 'A_B_C_D'
        assert len(safe_serial('x' * 100)) == 64

    def test_archive_never_writes_outside_its_directory(self, tmp_path):
        a = RawArchive(str(tmp_path))
        f = a.append('../../escape', 'cdata', 'x')
        assert f and os.path.abspath(f).startswith(os.path.abspath(tmp_path) + os.sep)
        assert not (tmp_path.parent / 'escape').exists()
        assert os.listdir(tmp_path) == ['______escape']

    def test_archive_caps_files_per_day(self, tmp_path, monkeypatch):
        import hajiri.gateway.archive as arch
        monkeypatch.setattr(arch, 'MAX_FILE_BYTES', 10)
        a = RawArchive(str(tmp_path))
        now = utc_ms(2026, 9, 21, 4)
        written = [a.append('DEV', 'cdata', 'x' * 20, now_ms=now) for _ in range(7)]
        assert sorted(os.listdir(tmp_path / 'DEV')) == ['2026-09-21.1.log', '2026-09-21.2.log', '2026-09-21.3.log',
                                                         '2026-09-21.4.log', '2026-09-21.log']
        assert written[5:] == [None, None]
        assert a.dropped == 2
        assert (tmp_path / 'DEV' / '2026-09-21.log').read_text().startswith('--- 2026-09-21T04:00:00.000Z cdata 20B\n')

    def test_rejects_over_long_or_hostile_serials_at_the_edge(self, qa, make_app):
        store, _ = qa
        app = make_app(store)
        assert app.get(f'/iclock/cdata?SN={"A" * 65}&options=all').status == 400
        assert app.get(f'/iclock/cdata?SN={quote("../x", safe="")}&options=all').status == 400
        assert app.get(f'/iclock/getrequest?SN={quote("a\nb", safe="")}').status == 404
        assert len(store.devices) == 1


class TestCommandSafety:
    def test_refuses_multiline_zero_width_and_case_varied_shell_and_control_device(self):
        for cmd, msg in [('DATA UPDATE USERINFO PIN=1\nSHELL reboot', 'single line'), ('CHECK\r\nSHELL ls', 'single line'),
                         ('​SHELL ls', 'SHELL'), ('\u0000shell ls', 'SHELL'), ('  Shell', 'SHELL'),
                         ('CONTROL DEVICE 010101FF05', 'dangerous'), ('', 'empty')]:
            with pytest.raises(CommandRefused, match=msg):
                assert_allowed_command(cmd)
        assert_allowed_command('CONTROL DEVICE 010101FF05', True)
        assert_allowed_command('DATA UPDATE USERINFO PIN=1\tName=Sita\tPri=0')

    def test_resends_an_unacknowledged_command_once_after_the_ack_timeout_then_fails_it(self, qa):
        store, device_id = qa
        cid = store.enqueue_command(device_id, 'CHECK')
        t0 = utc_ms(2026, 9, 21, 4)
        assert store.pop_queued_commands(device_id, dt_from_ms(t0)) == [{'id': cid, 'command': 'CHECK'}]
        assert store.pop_queued_commands(device_id, dt_from_ms(t0 + 60_000)) == []
        t1 = t0 + COMMAND_ACK_TIMEOUT_MS + 1000
        assert store.pop_queued_commands(device_id, dt_from_ms(t1)) == [{'id': cid, 'command': 'CHECK'}]
        t2 = t1 + COMMAND_ACK_TIMEOUT_MS + 1000
        assert store.pop_queued_commands(device_id, dt_from_ms(t2)) == []
        assert (store.commands[0].status, store.commands[0].attempts) == ('failed', 2)
        # a late ack for a failed command is ignored
        store.ack_command(device_id, cid, 0)
        assert store.commands[0].status == 'failed'


class TestTimeValidation:
    def test_rejects_calendar_impossible_device_times_instead_of_rolling_them_over(self):
        assert is_valid_device_time('2026-13-40 10:00:00') is False
        assert is_valid_device_time('2026-02-29 10:00:00') is False
        assert is_valid_device_time('2028-02-29 10:00:00') is True
        assert is_valid_device_time('2026-09-21 24:00:00') is False
        assert len(parse_attlog('1\t2026-13-40 10:00:00\t0\t1\n').malformed) == 1
        with pytest.raises(DeviceTimeError):
            device_time_to_utc('2026-02-30 10:00:00', 345)
        assert parse_iso_with_offset('2026-02-30T10:00:00+05:45') is None

    def test_accepts_very_old_buffered_punches_without_skew_flags(self, qa, make_app):
        store, _ = qa
        app = make_app(store, max_clock_skew_min=10)
        r = attlog(app, 'DEMO-01', '1\t2024-01-05 10:00:00\t0\t1\n')
        assert r.text == 'OK: 1'
        assert 'clockSkew' not in store.punches[0].raw
        assert store.devices['DEMO-01'].clock_drift_sec is None   # old replays never update drift

    def test_honours_the_per_device_timezone_offset(self, qa, make_app):
        store, _ = qa
        d = store.add_device('UTC-01', tz_offset_min=0)
        store.mappings[f'{d.id}:1'] = 'emp-1'
        attlog(make_app(store), 'UTC-01', '1\t2026-09-21 10:17:03\t0\t1\n')
        assert store.punches[0].punched_at_iso == '2026-09-21T10:17:03.000Z'


class _DownStore(MemoryStore):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def get_device(self, serial_no):
        raise self.exc


class TestStorageOutage:
    def test_returns_503_plain_text_when_the_store_is_unreachable_so_the_terminal_retries(self, make_app):
        err = ConnectionRefusedError(61, 'connect ECONNREFUSED 127.0.0.1:5433')
        r = attlog(make_app(_DownStore(err)), 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\n')
        assert r.status == 503
        assert r.text.startswith('ERROR')
        assert r.headers['Content-Type'].startswith('text/plain')

    def test_postgres_connection_failures_and_admin_shutdown_also_answer_503(self, make_app):
        e1 = OperationalError('connection failed: connection to server at "127.0.0.1", port 5433 failed: Connection refused')
        assert attlog(make_app(_DownStore(e1)), 'DEMO-01', '').status == 503
        e2 = RuntimeError('boom')
        e2.sqlstate = '57P01'
        assert attlog(make_app(_DownStore(e2)), 'DEMO-01', '').status == 503

    def test_other_errors_answer_500_plain_text(self, make_app):
        r = attlog(make_app(_DownStore(KeyError('x'))), 'DEMO-01', '')
        assert (r.status, r.text) == (500, 'ERROR')
        assert r.headers['Content-Type'].startswith('text/plain')


HIK_EVENT = {'ipAddress': '10.0.0.5', 'dateTime': '2026-09-21T10:17:03+05:45', 'eventType': 'AccessControllerEvent',
             'AccessControllerEvent': {'majorEventType': 5, 'subEventType': 75, 'employeeNoString': '1', 'attendanceStatus': 'checkIn'}}
XML = '''<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<ipAddress>10.0.0.5</ipAddress><portNo>80</portNo><protocol>HTTP</protocol><channelID>1</channelID>
<dateTime>2026-09-21T10:17:03+05:45</dateTime><activePostCount>1</activePostCount>
<eventType>AccessControllerEvent</eventType><eventState>active</eventState>
<AccessControllerEvent><deviceName>Front &amp; Back</deviceName><majorEventType>5</majorEventType><subEventType>113</subEventType>
<employeeNoString>1</employeeNoString><serialNo>HIK-01</serialNo><currentVerifyMode>fingerPrint</currentVerifyMode><attendanceStatus>checkOut</attendanceStatus></AccessControllerEvent>
</EventNotificationAlert>'''


def _hik(store, **kw):
    d = store.add_device('HIK-01', vendor='hikvision', protocol='isapi', **kw)
    store.mappings[f'{d.id}:1'] = 'emp-1'
    return d


class TestHikvisionKeyXmlMultipart:
    def test_requires_the_per_device_key_when_configured(self, qa, make_app):
        store, _ = qa
        _hik(store, comm_key_hash=hashlib.sha256(b's3cret').hexdigest())
        app = make_app(store)
        assert app.post('/hikvision/event?sn=HIK-01', HIK_EVENT).status == 403
        assert app.post('/hikvision/event?sn=HIK-01&key=wrong', HIK_EVENT).status == 403
        assert store.punches == []
        assert app.post('/hikvision/event?sn=HIK-01&key=s3cret', HIK_EVENT).status == 200
        later = {**HIK_EVENT, 'dateTime': '2026-09-21T11:00:00+05:45'}
        assert app.post('/hikvision/event?sn=HIK-01', later, headers={'x-device-key': 's3cret'}).status == 200
        assert len(store.punches) == 2

    def test_adms_devices_are_not_blocked_by_a_configured_key(self, qa, make_app):
        store, _ = qa
        store.devices['DEMO-01'].comm_key_hash = 'abc'
        assert attlog(make_app(store), 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\n').status == 200

    def test_parses_xml_events_from_older_firmware_and_ingests_them(self, qa, make_app):
        store, _ = qa
        j = xml_event_to_json(XML)
        assert (j['fromXml'], j['dateTime'], j['eventType']) == (True, '2026-09-21T10:17:03+05:45', 'AccessControllerEvent')
        ace = j['AccessControllerEvent']
        assert (ace['majorEventType'], ace['subEventType'], ace['employeeNoString'], ace['deviceName'], ace['attendanceStatus']) == (
            '5', '113', '1', 'Front & Back', 'checkOut')
        _hik(store)
        r = make_app(store).post('/hikvision/event?sn=HIK-01', XML, content_type='application/xml')
        assert r.status == 200
        assert len(store.punches) == 1
        p = store.punches[0]
        assert (p.verify_mode, p.direction, p.employee_id, p.device_local_time) == ('fingerprint', 'out', 'emp-1', '2026-09-21 10:17:03')
        assert xml_event_to_json('<other/>') is None

    def test_multipart_with_lf_only_separators_and_a_picture_part(self, qa, make_app):
        store, _ = qa
        _hik(store)
        b = 'xyz'
        body = b''.join([
            f'--{b}\nContent-Disposition: form-data; name="event_log"\nContent-Type: application/json\n\n{json.dumps(HIK_EVENT)}\n'.encode(),
            f'--{b}\nContent-Disposition: form-data; name="Picture"; filename="p.jpg"\nContent-Type: image/jpeg\n\n'.encode(),
            b'\xff' * 200_000,
            f'\n--{b}--\n'.encode(),
        ])
        parts = parse_multipart(body, f'multipart/form-data; boundary={b}')
        assert list(parts) == ['event_log', 'Picture']
        r = make_app(store).post('/hikvision/event?sn=HIK-01', body, content_type=f'multipart/form-data; boundary={b}')
        assert r.status == 200
        assert len(store.punches) == 1
        assert extract_event_json(b'   ', 'application/json') is None

    def test_unknown_serials_not_archived_and_impossible_dates_counted_malformed(self, qa, make_app, tmp_path):
        store, _ = qa
        app = make_app(store, archive=RawArchive(str(tmp_path)))
        app.post('/hikvision/event?sn=NEW-77', HIK_EVENT)
        assert not (tmp_path / 'NEW-77').exists()
        _hik(store)
        r = app.post('/hikvision/event?sn=HIK-01', {**HIK_EVENT, 'dateTime': '2026-02-30T10:00:00+05:45'})
        assert r.status == 200
        assert store.punches == []
        assert (tmp_path / 'HIK-01').exists()


def test_iso_ms_matches_to_iso_string():
    assert iso_ms(utc_ms(2026, 9, 21, 4, 32, 3)) == '2026-09-21T04:32:03.000Z'
