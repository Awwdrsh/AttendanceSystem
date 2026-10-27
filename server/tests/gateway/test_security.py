"""Port of apps/gateway/src/security.test.ts."""
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from hajiri.gateway.adms import parse_device_cmd_results
from hajiri.gateway.app import redact_url
from hajiri.gateway.archive import RawArchive
from hajiri.gateway.store import MemoryStore

from .conftest import npt_now

SERVER_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def sec():
    store = MemoryStore()
    a = store.add_device('DEV-A').id
    b = store.add_device('DEV-B').id
    store.add_device('DEV-OFF', status='disabled')
    store.mappings[f'{a}:1'] = 'emp-1'
    return store, a, b


class TestCommandResults:
    def test_accepted_only_from_an_approved_device_and_only_for_its_own_commands(self, sec, make_app):
        store, a, b = sec
        app = make_app(store)
        cid = store.enqueue_command(a, 'CHECK')
        app.get('/iclock/getrequest?SN=DEV-A')

        def ack(sn):
            return app.post(f'/iclock/devicecmd{f"?SN={sn}" if sn else ""}', f'ID={cid}&Return=0&CMD=CHECK')

        assert ack(None).status == 403
        assert ack('NOBODY').status == 403
        assert ack('DEV-OFF').status == 403
        assert ack('DEV-B').status == 200            # device B answers for a command id it never received
        assert store.commands[0].status == 'sent'
        assert ack('DEV-A').status == 200
        c = store.commands[0]
        assert (c.status, c.return_code, c.device_id) == ('acked', 0, a)
        assert b != a

    def test_ignores_out_of_range_ids_and_return_codes(self):
        assert parse_device_cmd_results('ID=99999999999999999999&Return=0\nID=-4&Return=0\nID=7&Return=99999999999\nID=8&Return=-1002') == [
            {'id': 8, 'returnCode': -1002, 'cmd': ''}]


class TestCursorAndVersionInjection:
    def test_a_stamp_with_a_line_break_never_reaches_the_handshake_options_block(self, sec, make_app):
        store, _, _ = sec
        app = make_app(store)
        app.post(f'/iclock/cdata?SN=DEV-A&table=ATTLOG&Stamp={quote(chr(53) + chr(10) + "Encrypt=1", safe="")}', f'1\t{npt_now()}\t0\t1\n')
        app.post(f'/iclock/cdata?SN=DEV-A&table=OPERLOG&OpStamp={quote("7" + chr(13) + chr(10) + "Realtime=0", safe="")}', '')
        opts = app.get('/iclock/cdata?SN=DEV-A&options=all')
        assert 'Encrypt=1' not in opts.text and 'Realtime=0' not in opts.text
        assert 'Stamp=0\n' in opts.text
        app.post('/iclock/cdata?SN=DEV-A&table=ATTLOG&Stamp=123456', '')
        assert 'ATTLOGStamp=123456\n' in app.get('/iclock/cdata?SN=DEV-A&options=all').text

    def test_push_versions_are_bounded_and_printable(self, sec, make_app):
        store, _, _ = sec
        app = make_app(store)
        app.get(f'/iclock/cdata?SN=NEW-1&options=all&pushver={quote("<script>x</script>" * 20, safe="")}')
        assert store.devices['NEW-1'].push_version is None
        app.get('/iclock/cdata?SN=NEW-2&options=all&pushver=2.4.1')
        assert store.devices['NEW-2'].push_version == '2.4.1'


class TestPunchTimeSanity:
    def test_rejects_reset_clocks_and_punches_days_ahead_keeps_small_skews_flagged(self, sec, make_app):
        store, _, _ = sec
        app = make_app(store, max_clock_skew_min=10)
        body = (f'1\t2000-01-01 00:00:05\t0\t1\n1\t{npt_now(3 * 86_400_000)}\t0\t1\n'
                f'1\t{npt_now(2 * 3600_000)}\t0\t1\n1\t{npt_now(-60_000)}\t1\t1\n')
        r = app.post('/iclock/cdata?SN=DEV-A&table=ATTLOG', body)
        assert r.text == 'OK: 4'                         # acknowledged so the terminal does not resend forever
        assert len(store.punches) == 2
        assert len([p for p in store.punches if p.raw.get('clockSkew')]) == 1
        assert all(p.local_date >= '2020-01-01' for p in store.punches)


def _hik_event():
    return {'eventType': 'AccessControllerEvent', 'dateTime': f"{npt_now().replace(' ', 'T')}+05:45",
            'AccessControllerEvent': {'majorEventType': 5, 'subEventType': 75, 'employeeNoString': '1'}}


class TestHikvisionAuthentication:
    def test_nothing_from_an_unauthenticated_sender_is_archived_or_stored(self, sec, make_app, tmp_path):
        store, _, _ = sec
        hik = store.add_device('HIK-1', vendor='hikvision', protocol='isapi', comm_key_hash=hashlib.sha256(b's3cret').hexdigest())
        store.mappings[f'{hik.id}:1'] = 'emp-1'
        app = make_app(store, archive=RawArchive(str(tmp_path)))
        assert app.post('/hikvision/event?sn=HIK-1&key=wrong', _hik_event()).status == 403
        assert app.post('/hikvision/event?sn=HIK-1', _hik_event()).status == 403
        assert os.listdir(tmp_path) == []
        assert store.punches == []
        assert app.post('/hikvision/event?sn=HIK-1', _hik_event(), headers={'x-device-key': 's3cret'}).status == 200
        assert len(store.punches) == 1
        assert os.listdir(tmp_path) == ['HIK-1']

    def test_device_keys_never_appear_in_request_logs(self, sec, make_app, caplog):
        store, _, _ = sec
        store.add_device('HIK-2', vendor='hikvision', protocol='isapi', comm_key_hash=hashlib.sha256(b'topsecret-key').hexdigest())
        app = make_app(store)
        with caplog.at_level(logging.DEBUG, logger='hajiri.gateway'):
            app.post('/hikvision/event?sn=HIK-2&key=topsecret-key', {'eventType': 'heartbeat'}, headers={'x-device-key': 'topsecret-key'})
        text = '\n'.join(r.getMessage() for r in caplog.records)
        assert '/hikvision/event?sn=HIK-2&key=[redacted]' in text
        assert 'topsecret-key' not in text
        assert redact_url('/x?a=1&KEY=abc&token=t&b=2') == '/x?a=1&KEY=[redacted]&token=[redacted]&b=2'

    def test_getrequest_polls_and_health_are_not_request_logged(self, sec, make_app, caplog):
        store, _, _ = sec
        app = make_app(store)
        with caplog.at_level(logging.INFO, logger='hajiri.gateway'):
            app.get('/iclock/getrequest?SN=DEV-A')
            app.get('/health')
        assert not [r for r in caplog.records if 'request completed' in r.getMessage()]


class TestStartup:
    def test_refuses_to_start_in_production_with_an_ephemeral_signing_key(self, tmp_path):
        env = {'PATH': os.environ.get('PATH', ''), 'NODE_ENV': 'production', 'PORT': '8137', 'GATEWAY_SIGNING_KEY': '',
               'DJANGO_SETTINGS_MODULE': 'config.settings', 'HOME': str(tmp_path)}
        r = subprocess.run([sys.executable, str(SERVER_DIR / 'manage.py'), 'run_gateway', '--port', '8137'],
                           cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0
        assert 'GATEWAY_SIGNING_KEY is required' in r.stderr
