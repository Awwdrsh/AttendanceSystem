"""Port of apps/gateway/src/app.test.ts."""
import base64
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hajiri.gateway.adms import parse_attlog, parse_device_cmd_results
from hajiri.gateway.punch import Signer, device_time_to_utc, idempotency_key
from hajiri.gateway.jsutil import iso_ms
from hajiri.gateway.store import CommandRefused, MemoryStore


@pytest.fixture
def env(make_app):
    store = MemoryStore()
    device_id = store.add_device('DEMO-01').id
    store.mappings[f'{device_id}:1'] = 'emp-1'
    return store, device_id, make_app(store)


def attlog(app, sn, body, extra=''):
    return app.post(f'/iclock/cdata?SN={sn}&table=ATTLOG{extra}', body)


class TestHandshake:
    def test_unknown_sn_is_stored_as_pending_and_gets_no_options(self, env):
        store, _, app = env
        r = app.get('/iclock/cdata?SN=NEW-99&options=all&pushver=2.4.1')
        assert r.status == 200
        assert r.text == 'OK'
        d = store.devices['NEW-99']
        assert (d.status, d.push_version) == ('pending', '2.4.1')

    def test_active_device_gets_the_options_block_with_stamps(self, env):
        store, _, app = env
        store.devices['DEMO-01'].attlog_stamp = '12345'
        r = app.get('/iclock/cdata?SN=DEMO-01&options=all&pushver=2.4.1')
        assert re.match(r'^GET OPTION FROM: DEMO-01\n', r.text)
        assert 'ATTLOGStamp=12345' in r.text
        assert 'Realtime=1' in r.text
        assert 'Delay=10' in r.text


class TestAttlogUpload:
    def test_parses_records_resolves_employees_signs_and_stores(self, env):
        store, _, app = env
        r = attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\t0\n2\t2026-09-21 10:18:00\t0\t15\t0\n', '&Stamp=777')
        assert r.status == 200
        assert r.text == 'OK: 2'
        assert len(store.punches) == 2
        p = store.punches[0]
        assert (p.device_user_pin, p.employee_id, p.direction, p.verify_mode, p.local_date) == (
            '1', 'emp-1', 'in', 'fingerprint', '2026-09-21')
        # 10:17:03 NPT = 04:32:03 UTC
        assert p.punched_at_iso == '2026-09-21T04:32:03.000Z'
        assert len(p.gateway_signature) > 40
        assert (store.punches[1].employee_id, store.punches[1].verify_mode) == (None, 'face')
        assert store.devices['DEMO-01'].attlog_stamp == '777'

    def test_replayed_records_are_idempotent_and_near_duplicates_within_5s_are_ignored(self, env):
        store, _, app = env
        line = '1\t2026-09-21 10:17:03\t0\t1\t0\n'
        attlog(app, 'DEMO-01', line)
        attlog(app, 'DEMO-01', line)
        attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:06\t0\t1\t0\n')
        attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:09\t0\t1\t0\n')
        assert len(store.punches) == 2

    def test_never_dedupes_across_devices(self, env):
        store, _, app = env
        store.add_device('DEMO-02')
        attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\t0\n')
        attlog(app, 'DEMO-02', '1\t2026-09-21 10:17:03\t0\t1\t0\n')
        assert len(store.punches) == 2

    def test_skips_malformed_lines_but_still_acknowledges_the_good_ones(self, env):
        store, _, app = env
        r = attlog(app, 'DEMO-01', 'garbage\n1\tnot-a-time\t0\t1\n1\t2026-09-21 18:40:00\t1\t1\t0\n')
        assert r.text == 'OK: 1'
        assert len(store.punches) == 1
        assert store.punches[0].direction == 'out'

    def test_rejects_data_from_pending_or_unknown_devices(self, env):
        store, _, app = env
        app.get('/iclock/cdata?SN=NEW-99&options=all')
        assert attlog(app, 'NEW-99', '1\t2026-09-21 10:17:03\t0\t1\t0\n').status == 403
        assert attlog(app, 'GHOST', '1\t2026-09-21 10:17:03\t0\t1\t0\n').status == 403
        assert store.punches == []


class TestCommandChannel:
    def test_delivers_queued_commands_once_and_records_results(self, env):
        store, device_id, app = env
        cid = store.enqueue_command(device_id, 'DATA UPDATE USERINFO PIN=3\tName=Hari\tPri=0')
        first = app.get('/iclock/getrequest?SN=DEMO-01')
        assert first.text == f'C:{cid}:DATA UPDATE USERINFO PIN=3\tName=Hari\tPri=0\n'
        assert app.get('/iclock/getrequest?SN=DEMO-01').text == 'OK'
        app.post('/iclock/devicecmd?SN=DEMO-01', f'ID={cid}&Return=0&CMD=DATA')
        assert (store.commands[0].status, store.commands[0].return_code) == ('acked', 0)

    def test_marks_failed_results(self, env):
        store, device_id, app = env
        cid = store.enqueue_command(device_id, 'CHECK')
        app.get('/iclock/getrequest?SN=DEMO-01')
        app.post('/iclock/devicecmd?SN=DEMO-01', f'ID={cid}&Return=-1002&CMD=CHECK')
        assert (store.commands[0].status, store.commands[0].return_code) == ('failed', -1002)

    def test_shell_can_never_be_queued(self, env):
        store, device_id, _ = env
        with pytest.raises(CommandRefused, match='forbidden'):
            store.enqueue_command(device_id, 'SHELL reboot')
        with pytest.raises(CommandRefused, match='forbidden'):
            store.enqueue_command(device_id, '  shell ls')

    def test_pending_devices_get_ok_with_no_commands_unknown_devices_404(self, env):
        _, _, app = env
        app.get('/iclock/cdata?SN=NEW-99&options=all')
        assert app.get('/iclock/getrequest?SN=NEW-99').text == 'OK'
        assert app.get('/iclock/getrequest?SN=GHOST').status == 404


class TestHelpers:
    def test_parse_attlog_handles_crlf_and_optional_workcode(self):
        res = parse_attlog('1\t2026-01-01 00:00:00\t0\t1\r\n2\t2026-01-01 00:00:01\t1\t15\t7\r\n')
        assert len(res.records) == 2
        assert res.records[1].workcode == '7'
        assert res.malformed == []

    def test_parse_device_cmd_results_handles_multiple_lines(self):
        assert parse_device_cmd_results('ID=1&Return=0&CMD=DATA\nID=2&Return=-30&CMD=CHECK\n') == [
            {'id': 1, 'returnCode': 0, 'cmd': 'DATA'}, {'id': 2, 'returnCode': -30, 'cmd': 'CHECK'}]

    def test_idempotency_key_and_device_time_offset(self):
        a = idempotency_key(vendor='zkteco', device_sn='A', device_user_pin='1', device_local_time='2026-09-21 10:17:03', verify_mode='fingerprint')
        b = idempotency_key(vendor='zkteco', device_sn='B', device_user_pin='1', device_local_time='2026-09-21 10:17:03', verify_mode='fingerprint')
        assert a != b
        assert iso_ms(device_time_to_utc('2026-09-21 00:00:00', 345)) == '2026-09-20T18:15:00.000Z'

    def test_signer_round_trips_from_a_base64_pkcs8_key(self):
        der = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        s = Signer.from_base64_pkcs8(base64.b64encode(der).decode())
        assert s.ephemeral is False
        assert len(s.sign('x')) == 88
