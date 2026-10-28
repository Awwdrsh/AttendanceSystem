"""The gateway as a process: the WSGI app under waitress over real HTTP, the simulator, the management commands, and
the HTTP edge (404s, HEAD, paths, body encodings) a terminal can hit."""
import gzip
import io
import json
import re
import threading
import zlib

import pytest
from django.core.management import call_command

from hajiri.gateway import runtime
from hajiri.gateway.app import Gateway
from hajiri.gateway.runtime import build_gateway
from hajiri.gateway.simulate import simulate
from hajiri.gateway.store import MemoryStore
from hajiri.gateway.wsgi import application

from .conftest import SIGNER


@pytest.fixture
def demo():
    store = MemoryStore()
    d = store.add_device('DEMO-01')
    store.mappings[f'{d.id}:1'] = 'emp-1'
    return store, d


@pytest.fixture
def waitress_gw(settings, demo):
    """waitress on an ephemeral port serving the gateway WSGI app, with an in-memory store."""
    from waitress.server import create_server
    for k, v in (('DEBUG', False), ('DATA_UPLOAD_MAX_MEMORY_SIZE', None)):
        setattr(settings, k, v)         # recorded so the fixture restores what application() changes
    store, _ = demo
    runtime.configure(Gateway(store=store, signer=SIGNER))
    srv = create_server(application(), host='127.0.0.1', port=0, threads=2)
    def serve():
        try:
            srv.run()
        except (OSError, ValueError):   # the listening socket was closed by the teardown below
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{srv.effective_port}', store
    srv.task_dispatcher.shutdown()
    srv.close()
    t.join(5)


def test_simulator_against_a_live_gateway_over_http(waitress_gw):
    url, store = waitress_gw
    did = store.devices['DEMO-01'].id
    cid = store.enqueue_command(did, 'INFO')
    out = io.StringIO()
    simulate(['--url', url, '--sn', 'DEMO-01', '--pin', '1', '--time', '2026-09-21 10:17:03', '--pin', '2', '--time',
              '2026-09-21 18:01:00', '--template'], out=out)
    text = out.getvalue()
    assert 'handshake -> 200\nGET OPTION FROM: DEMO-01' in text
    assert 'ATTLOG (2 record(s)) -> 200\nOK: 2' in text
    assert 'OPERLOG template -> 200\nOK: 1' in text
    assert f'getrequest -> 200\nC:{cid}:INFO' in text
    assert f'devicecmd ack {cid} -> 200\nOK' in text
    assert [p.device_user_pin for p in store.punches] == ['1', '2']
    assert store.commands[0].status == 'acked'


def test_live_gateway_answers_health_404_and_oversized_bodies_in_plain_text(waitress_gw):
    import urllib.error
    import urllib.request
    url, _ = waitress_gw
    with urllib.request.urlopen(f'{url}/health') as r:
        assert json.loads(r.read()) == {'ok': True, 'signer': 'ephemeral', 'templates': 'discarded'}
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f'{url}/nope')
    assert e.value.code == 404
    assert e.value.headers['Content-Type'].startswith('text/plain')
    big = urllib.request.Request(f'{url}/iclock/cdata?SN=DEMO-01&table=OPERLOG', data=b'x' * (3 * 1024 * 1024), method='POST')
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(big)
    assert e.value.code == 413
    assert e.value.read() == b'ERROR: body too large'


def test_hikvision_simulator(make_app):
    store = MemoryStore()
    d = store.add_device('HIK-01', vendor='hikvision', protocol='isapi')
    store.mappings[f'{d.id}:1'] = 'emp-1'
    app = make_app(store)

    def fetch(method, url, headers, body):
        path = url.removeprefix('http://gw')
        r = app.post(path, body or b'', content_type=headers.get('Content-Type', 'text/plain')) if method == 'POST' else app.get(path)
        return r.status, r.text

    out = io.StringIO()
    simulate(['--url', 'http://gw', '--vendor', 'hikvision', '--sn', 'HIK-01', '--pin', '1', '--time', '2026-09-21 10:17:03'],
             fetch=fetch, out=out)
    assert out.getvalue() == 'heartbeat -> 200\nOK\n\nevent pin 1 -> 200\nOK\n\n'
    assert [(p.device_user_pin, p.verify_mode, p.direction) for p in store.punches] == [('1', 'face', 'in')]


def test_run_gateway_check_and_memory_mode(capsys):
    call_command('run_gateway', '--check', '--memory')
    assert re.search(r'gateway ok: signer (ephemeral|configured), templates (encrypted|discarded)', capsys.readouterr().out)


def test_build_gateway_from_the_environment():
    from hajiri.gateway.punch import generate_signing_key
    key = generate_signing_key()
    gw = build_gateway({'GATEWAY_SIGNING_KEY': key, 'GATEWAY_MAX_PENDING_DEVICES': '3', 'GATEWAY_MAX_CLOCK_SKEW_MIN': '5',
                        'GATEWAY_ALLOW_DANGEROUS_COMMANDS': 'true', 'GATEWAY_RAW_DIR': '/tmp/hajiri-raw-x'}, memory=True)
    assert gw.health() == {'ok': True, 'signer': 'configured', 'templates': 'discarded'}
    assert (gw.store.max_pending_devices, gw.store.allow_dangerous_commands, gw.max_clock_skew_min) == (3, True, 5)
    assert gw.archive.dir.endswith('hajiri-raw-x')
    with pytest.raises(ValueError, match='Invalid environment: GATEWAY_MAX_PENDING_DEVICES'):
        build_gateway({'GATEWAY_MAX_PENDING_DEVICES': '-1'}, memory=True)
    with pytest.raises(ValueError, match='DATA_ENCRYPTION_KEY'):
        build_gateway({'DATA_ENCRYPTION_KEY': 'short'}, memory=True)


@pytest.mark.django_db
def test_gateway_reconcile_command(capsys):
    from hajiri.db.models import Device
    Device.objects.create(serial_no='RC-01', vendor='zkteco', protocol='adms', status='active')
    Device.objects.create(serial_no='RC-02', vendor='hikvision', protocol='isapi', status='active')
    call_command('gateway_reconcile', '--all', '--date', '2026-09-21')
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert [(x['sn'], x['stored'], x['note']) for x in lines] == [
        ('RC-01', 0, 'query queued; re-sent records are deduplicated on ingest')]
    call_command('gateway_reconcile', '--sn', 'RC-02', '--date', '2026-09-21')
    assert json.loads(capsys.readouterr().out)['note'] == 'reconciliation for this protocol runs from the API'


class TestHttpEdge:
    def test_unknown_paths_and_methods_answer_plain_text_404(self, demo, make_app):
        app = make_app(demo[0])
        r = app.get('/nope')
        assert (r.status, r.text) == (404, 'Cannot GET /nope')
        assert r.headers['Content-Type'].startswith('text/plain')
        r = app.post('/iclock/getrequest?SN=DEMO-01', '')
        assert (r.status, r.text) == (404, 'Cannot POST /iclock/getrequest')

    def test_paths_are_case_insensitive_with_optional_trailing_slash(self, demo, make_app):
        app = make_app(demo[0])
        assert app.get('/ICLOCK/CDATA?SN=DEMO-01&options=all').text.startswith('GET OPTION FROM: DEMO-01')
        assert app.get('/iclock/getrequest/?SN=DEMO-01').text == 'OK'

    def test_head_on_a_get_route(self, demo, make_app):
        app = make_app(demo[0])
        runtime.configure(app.gw)
        r = app.c.head('/health')
        assert r.status_code == 200 and r.content == b''

    def test_gzip_and_deflate_bodies_are_inflated(self, demo, make_app):
        store, _ = demo
        app = make_app(store)
        r = app.post('/iclock/cdata?SN=DEMO-01&table=ATTLOG', gzip.compress(b'1\t2026-09-21 10:17:03\t0\t1\n'),
                     headers={'Content-Encoding': 'gzip'})
        assert r.text == 'OK: 1'
        r = app.post('/iclock/cdata?SN=DEMO-01&table=ATTLOG', zlib.compress(b'1\t2026-09-21 12:17:03\t0\t1\n'),
                     headers={'Content-Encoding': 'deflate'})
        assert r.text == 'OK: 1'
        assert len(store.punches) == 2
        assert app.post('/iclock/cdata?SN=DEMO-01&table=ATTLOG', b'x', headers={'Content-Encoding': 'br'}).status == 500

    def test_charset_is_honoured_and_unknown_charsets_fail_plainly(self, demo, make_app):
        store, _ = demo
        app = make_app(store)
        r = app.post('/iclock/cdata?SN=DEMO-01&table=ATTLOG', '1\t2026-09-21 10:17:03\t0\t1\n'.encode('utf-16'),
                     content_type='text/plain; charset=utf-16')
        assert r.text == 'OK: 1'
        r = app.post('/iclock/cdata?SN=DEMO-01&table=ATTLOG', 'x', content_type='text/plain; charset=nope-9')
        assert (r.status, r.text) == (500, 'ERROR')

    def test_fdata_allows_10mb_bodies_and_hikvision_caps_at_10mb(self, demo, make_app):
        store, _ = demo
        app = make_app(store)
        assert app.post('/iclock/fdata?SN=DEMO-01&table=ATTPHOTO', b'x' * (5 * 1024 * 1024)).status == 200
        assert app.post('/iclock/fdata?SN=DEMO-01&table=ATTPHOTO', b'x' * (10 * 1024 * 1024 + 1)).status == 413
        assert app.post('/hikvision/event?sn=X', b'x' * (10 * 1024 * 1024 + 1), content_type='application/json').status == 413

    def test_gateway_routes_are_not_part_of_the_web_app(self, settings):
        settings.ROOT_URLCONF = 'config.urls'
        from django.urls import Resolver404, resolve
        with pytest.raises(Resolver404):
            resolve('/iclock/cdata')




class TestGatewayEnvFile:
    """The gateway reads its own server/gateway.env (dotenv); the Django process has already loaded
    the web env files, whose PORT is the API's."""

    def _files(self, tmp_path, gw_text: str, web_text: str):
        gw_dir, web_dir = tmp_path / 'gateway', tmp_path / 'api'
        gw_dir.mkdir()
        web_dir.mkdir()
        (gw_dir / '.env').write_text(gw_text)
        (web_dir / '.env').write_text(web_text)
        return [gw_dir / '.env'], [web_dir / '.env']

    def test_gateway_env_file_wins_over_web_env_values_but_not_over_the_process(self, tmp_path):
        from hajiri.gateway.config import gateway_environ, load_config
        gw_files, web_files = self._files(tmp_path, 'PORT=8080\nGATEWAY_SIGNING_KEY=gwkey\nGATEWAY_RAW_DIR=data/raw\n'
                                                    'DATA_ENCRYPTION_KEY=from-gateway-file-xxxxxxxxxxxxxxxxxxxxxxxxxx\n',
                                          'PORT=3000\nDATA_ENCRYPTION_KEY=from-api-file-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n')
        # PORT and DATA_ENCRYPTION_KEY came from the API's env file; GATEWAY_MAX_CLOCK_SKEW_MIN from the real process.
        environ = {'PORT': '3000', 'DATA_ENCRYPTION_KEY': 'from-api-file-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                   'GATEWAY_MAX_CLOCK_SKEW_MIN': '7', 'GATEWAY_SIGNING_KEY': 'process-key'}
        env, origin = gateway_environ(environ, gw_files, web_files)
        assert env['PORT'] == '8080'
        assert env['DATA_ENCRYPTION_KEY'].startswith('from-gateway-file')
        assert env['GATEWAY_SIGNING_KEY'] == 'process-key'          # dotenv never overrides the process
        assert env['GATEWAY_MAX_CLOCK_SKEW_MIN'] == '7'
        assert origin['GATEWAY_RAW_DIR'] == tmp_path / 'gateway'
        assert load_config(env).PORT == 8080

    def test_a_web_only_port_is_not_the_gateway_port(self, tmp_path):
        from hajiri.gateway.config import gateway_environ, load_config
        _, web_files = self._files(tmp_path, '', 'PORT=3000\n')
        env, _ = gateway_environ({'PORT': '3000'}, [tmp_path / 'missing.env'], web_files)
        assert load_config(env).PORT == 8080
        env, _ = gateway_environ({'PORT': '9001'}, [tmp_path / 'missing.env'], web_files)   # set by the process
        assert load_config(env).PORT == 9001

    def test_raw_dir_is_relative_to_the_env_file_that_set_it(self, tmp_path, monkeypatch):
        from hajiri.gateway import config, runtime
        gw_files, web_files = self._files(tmp_path, 'GATEWAY_RAW_DIR=data/raw\n', '')
        monkeypatch.setattr(config, 'gateway_env_files', lambda: gw_files)
        monkeypatch.setattr(config, '_web_env_files', lambda: web_files)
        monkeypatch.delenv('GATEWAY_RAW_DIR', raising=False)
        monkeypatch.delenv('NODE_ENV', raising=False)
        gw = runtime.build_gateway(memory=True)
        assert gw.archive.dir == str((tmp_path / 'gateway' / 'data' / 'raw').resolve())
