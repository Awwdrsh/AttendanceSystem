"""Ports of apps/gateway/src/endpoints.test.ts and fixes.test.ts."""
import base64
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hajiri.gateway.app import affected_dates
from hajiri.gateway.jsutil import utc_ms
from hajiri.gateway.punch import Signer, TemplateCipher
from hajiri.gateway.store import MemoryStore


def _ms(iso: str) -> int:
    d, t = iso.rstrip('Z').split('T')
    y, m, dd = (int(x) for x in d.split('-'))
    h, mi, s = (int(x) for x in t.split(':'))
    return utc_ms(y, m, dd, h, mi, s)


class TestOperationalEndpoints:
    def test_health_reports_ephemeral_signer_and_discarded_templates_by_default(self, make_app):
        r = make_app(MemoryStore(), signer=Signer.ephemeral_key()).get('/health')
        assert r.status == 200
        assert r.json() == {'ok': True, 'signer': 'ephemeral', 'templates': 'discarded'}
        assert r.headers['Content-Type'] == 'application/json; charset=utf-8'
        assert r.text == '{"ok":true,"signer":"ephemeral","templates":"discarded"}'

    def test_health_reports_configured_signer_and_encrypted_templates(self, make_app):
        der = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        signer = Signer.from_base64_pkcs8(base64.b64encode(der).decode())
        app = make_app(MemoryStore(), signer=signer, cipher=TemplateCipher(bytes([3]) * 32))
        assert app.get('/health').json() == {'ok': True, 'signer': 'configured', 'templates': 'encrypted'}

    def test_public_key_serves_the_pem_that_verifies_gateway_signatures(self, make_app):
        signer = Signer.ephemeral_key()
        r = make_app(MemoryStore(), signer=signer).get('/public-key')
        assert r.status == 200
        assert re.match(r'text/plain', r.headers['Content-Type'])
        assert re.fullmatch(r'-----BEGIN PUBLIC KEY-----\n[\s\S]+-----END PUBLIC KEY-----\n', r.text)
        sig = signer.sign('canonical-event')
        pub = serialization.load_pem_public_key(r.text.encode())
        assert isinstance(pub, Ed25519PublicKey)
        pub.verify(base64.b64decode(sig), b'canonical-event')   # raises if invalid


CIPHER = TemplateCipher(bytes([9]) * 32)


@pytest.fixture
def fix(make_app):
    store = MemoryStore()
    device_id = store.add_device('FIX-01').id
    store.mappings[f'{device_id}:1'] = 'emp-1'
    return store, make_app


def post(app, body, table='ATTLOG'):
    return app.post(f'/iclock/cdata?SN=FIX-01&table={table}', body)


def fp() -> str:
    return f"FP PIN=1\tFID=6\tSize=1\tValid=1\tTMP={base64.b64encode(b'finger').decode()}\n"


class TestTemplatesNeedConsent:
    def test_stores_with_consent_drops_after_withdrawal_and_for_unmapped_pins(self, fix):
        store, make_app = fix
        app = make_app(store, cipher=CIPHER)
        consent_id = store.grant_consent('emp-1', 'fingerprint')
        post(app, fp(), 'OPERLOG')
        assert len(store.templates) == 1
        assert store.templates[0].consent_id == consent_id
        store.revoke_consent('emp-1', 'fingerprint')
        post(app, fp(), 'OPERLOG')
        assert len(store.templates) == 1
        post(app, fp().replace('PIN=1', 'PIN=99'), 'OPERLOG')
        assert len(store.templates) == 1

    def test_a_fingerprint_consent_does_not_cover_a_face_template(self, fix):
        store, make_app = fix
        app = make_app(store, cipher=CIPHER)
        store.grant_consent('emp-1', 'fingerprint')
        face = f"FACE PIN=1\tFID=0\tSize=1\tValid=1\tTMP={base64.b64encode(b'face').decode()}\n"
        post(app, face, 'OPERLOG')
        assert store.templates == []
        store.grant_consent('emp-1', 'face')
        post(app, face, 'OPERLOG')
        assert [t.modality for t in store.templates] == ['face']


class TestRecomputeDates:
    def test_afternoon_punch_only_its_day_morning_punch_also_previous_day(self):
        assert affected_dates(_ms('2026-09-21T09:15:00Z'), '2026-09-21') == ['2026-09-21']                # 15:00 NPT
        assert affected_dates(_ms('2026-09-21T00:15:00Z'), '2026-09-21') == ['2026-09-20', '2026-09-21']  # 06:00 NPT
        assert affected_dates(_ms('2026-10-01T00:15:00Z'), '2026-10-01')[0] == '2026-09-30'

    def test_night_shift_punch_out_at_0610_queues_the_day_the_shift_started(self, fix):
        store, make_app = fix
        app = make_app(store)
        post(app, '1\t2026-09-21 06:10:00\t1\t1\t0\n')
        assert [q['localDate'] for q in store.recompute_queue] == ['2026-09-20', '2026-09-21']
