"""Port of apps/gateway/src/hardening.test.ts."""
import base64
import os
import time

import pytest

from hajiri.gateway.adms import drift_seconds, parse_attlog, parse_template_upload
from hajiri.gateway.archive import RawArchive
from hajiri.gateway.jsutil import iso_ms, utc_ms
from hajiri.gateway.punch import TemplateCipher
from hajiri.gateway.store import CommandRefused, MemoryStore, assert_allowed_command

KEY = bytes([9]) * 32


@pytest.fixture
def hs():
    store = MemoryStore(max_pending_devices=1)
    device_id = store.add_device('DEMO-01').id
    store.mappings[f'{device_id}:1'] = 'emp-1'
    return store, device_id


def attlog(app, body, table='ATTLOG'):
    return app.post(f'/iclock/cdata?SN=DEMO-01&table={table}', body)


class TestAdmsHardening:
    def test_rejects_empty_or_non_numeric_status_verify_columns(self):
        res = parse_attlog('1\t2026-09-21 10:17:03\t\t1\n1\t2026-09-21 10:17:03\tx\t1\n1\t2026-09-21 10:17:03\t0\t1\n')
        assert len(res.records) == 1
        assert len(res.malformed) == 2

    def test_caps_pending_device_registrations(self, hs, make_app):
        store, _ = hs
        app = make_app(store)
        assert app.get('/iclock/cdata?SN=NEW-1&options=all').status == 200
        second = app.get('/iclock/cdata?SN=NEW-2&options=all')
        assert second.status == 403
        assert 'too many pending' in second.text
        assert 'NEW-2' not in store.devices

    def test_flags_punches_far_in_the_future_as_clock_skew_and_records_drift(self, hs, make_app):
        store, _ = hs
        app = make_app(store, max_clock_skew_min=10)
        future = iso_ms(int(time.time() * 1000) + 345 * 60_000 + 3 * 3600_000)[:19].replace('T', ' ')
        attlog(app, f'1\t{future}\t0\t1\t0\n')
        assert store.punches[0].raw['clockSkew'] is True
        assert store.punches[0].raw['clockSkewMs'] > 2 * 3600_000
        assert store.devices['DEMO-01'].clock_drift_sec > 10_000
        assert drift_seconds('2026-09-21 10:00:00', 345, utc_ms(2026, 9, 21, 4, 15, 0)) == 0

    def test_queues_recompute_for_mapped_employees_only(self, hs, make_app):
        store, _ = hs
        app = make_app(store)
        attlog(app, '1\t2026-09-21 10:17:03\t0\t1\t0\n9\t2026-09-21 10:18:03\t0\t1\t0\n')
        assert store.recompute_queue == [
            {'employeeId': 'emp-1', 'localDate': '2026-09-20', 'reason': 'punch:zkteco'},
            {'employeeId': 'emp-1', 'localDate': '2026-09-21', 'reason': 'punch:zkteco'}]

    def test_archives_raw_bodies_per_device_per_day_and_never_logs_templates_elsewhere(self, hs, make_app, tmp_path):
        store, _ = hs
        app = make_app(store, archive=RawArchive(str(tmp_path)))
        attlog(app, '1\t2026-09-21 10:17:03\t0\t1\t0\n')
        app.post('/iclock/fdata?SN=DEMO-01&table=FINGERTMP', 'PIN=1\tFID=0\tSize=4\tValid=1\tTMP=AAAA\n',
                 content_type='application/octet-stream')
        files = os.listdir(tmp_path / 'DEMO-01')
        assert len(files) == 1
        content = (tmp_path / 'DEMO-01' / files[0]).read_text('utf-8')
        assert 'cdata:ATTLOG' in content
        assert 'fdata:FINGERTMP' in content
        assert 'TMP=AAAA' in content

    def test_returns_plain_text_errors_for_oversized_bodies(self, hs, make_app):
        store, _ = hs
        app = make_app(store)
        r = attlog(app, 'x' * int(2.5 * 1024 * 1024), 'OPERLOG')
        assert r.status == 413
        assert r.text.startswith('ERROR')


class TestTemplates:
    def test_parses_fp_face_biodata_lines_and_photo_tables(self):
        p = parse_template_upload('OPERLOG', 'USER PIN=1\tName=Sita\nFP PIN=1\tFID=6\tSize=8\tValid=1\tTMP=QUJDRA==\n'
                                  'FACE PIN=1\tFID=0\tSIZE=8\tVALID=1\tTMP=QUJDRA==\nBIODATA Pin=2\tNo=0\tIndex=0\tValid=0\t'
                                  'Duress=0\tType=8\tMajorVer=12\tMinorVer=0\tFormat=0\tTmp=QUJDRA==\nweird line\n')
        assert p.users == 1
        assert [(t.modality, t.vendor_format, t.valid) for t in p.templates] == [
            ('fingerprint', 'zk_fp_v10', True), ('face', 'zk_face_v7', True), ('palm', 'zk_biodata_t8_v12.0', False)]
        assert p.unknown == 1
        assert parse_template_upload('ATTPHOTO', 'jpegbytes').photos == 1

    def test_stores_encrypted_in_the_api_format_discards_invalid_and_everything_without_a_key(self, hs, make_app):
        store, _ = hs
        with_key = make_app(store, cipher=TemplateCipher(KEY))
        store.grant_consent('emp-1', 'fingerprint')
        b64 = base64.b64encode(b'finger-template-bytes').decode()
        attlog(with_key, f'FP PIN=1\tFID=6\tSize=1\tValid=1\tTMP={b64}\nFP PIN=1\tFID=7\tSize=1\tValid=0\tTMP={b64}\n', 'OPERLOG')
        assert len(store.templates) == 1
        t = store.templates[0]
        assert (t.employee_id, t.device_user_pin, t.modality, t.vendor_format, t.key_version) == (
            'emp-1', '1', 'fingerprint', 'zk_fp_v10', 1)
        assert t.ciphertext[0] == 1
        assert len(t.ciphertext) == 29 + len(b64)
        assert b64 not in t.ciphertext.decode('latin1')
        # Without a key nothing is stored (the TS test builds the app and checks the first store is unchanged).
        other = MemoryStore()
        d = other.add_device('DEMO-01')
        other.mappings[f'{d.id}:1'] = 'emp-1'
        other.grant_consent('emp-1', 'fingerprint')
        attlog(make_app(other), f'FP PIN=1\tFID=6\tSize=1\tValid=1\tTMP={b64}\n', 'OPERLOG')
        assert other.templates == []
        assert len(store.templates) == 1


class TestCommandSafety:
    def test_always_refuses_shell_refuses_clear_data_and_firmware_unless_enabled(self, hs):
        store, device_id = hs
        with pytest.raises(CommandRefused, match='forbidden'):
            assert_allowed_command('SHELL reboot')
        with pytest.raises(CommandRefused, match='forbidden'):
            assert_allowed_command('SHELL reboot', True)
        with pytest.raises(CommandRefused, match='dangerous'):
            assert_allowed_command('CLEAR DATA')
        with pytest.raises(CommandRefused, match='dangerous'):
            assert_allowed_command('UPGRADE firmware.bin')
        assert_allowed_command('CLEAR DATA', True)
        assert_allowed_command('CLEAR LOG')
        assert_allowed_command('DATA UPDATE USERINFO PIN=1\tName=A')
        with pytest.raises(CommandRefused, match='dangerous'):
            store.enqueue_command(device_id, 'CLEAR DATA')
        assert isinstance(MemoryStore(allow_dangerous_commands=True).enqueue_command(device_id, 'CLEAR DATA'), int)
