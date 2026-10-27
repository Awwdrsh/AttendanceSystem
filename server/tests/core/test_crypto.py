"""crypto.py reads what the TS API wrote, and the TS API reads what crypto.py writes."""
import base64
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from hajiri.core.crypto import FieldCipher, hash_password, password_policy_error, sha256_hex, verify_password

FIXTURE = json.loads((Path(__file__).parent.parent / 'parity' / 'fixtures' / 'crypto.json').read_text())
REPO = Path(__file__).resolve().parents[3]
TSX = REPO / 'apps' / 'api' / 'node_modules' / '.bin' / 'tsx'
CIPHER = FieldCipher.from_base64(FIXTURE['key'])


def test_decrypts_ts_ciphertexts_and_matches_blind_index():
    for x in FIXTURE['cipher']:
        assert CIPHER.decrypt(base64.b64decode(x['blob']), x['aad']) == x['plain']
        assert CIPHER.blind_index(x['plain']) == x['bidx']


def test_wrong_aad_is_rejected():
    x = FIXTURE['cipher'][0]
    with pytest.raises(InvalidTag):
        CIPHER.decrypt(base64.b64decode(x['blob']), x['aad'] + 'x')


def test_verifies_ts_password_hashes():
    for x in FIXTURE['passwords']:
        assert verify_password(x['password'], x['hash'], FIXTURE['pepper'])
        assert not verify_password(x['password'] + '!', x['hash'], FIXTURE['pepper'])
        assert not verify_password(x['password'], x['hash'], 'other-pepper')


def test_session_hash_matches_ts():
    for x in FIXTURE['sha256']:
        assert sha256_hex(x['in']) == x['out']


def test_malformed_hash_is_false_not_error():
    assert not verify_password('x', 'bcrypt$whatever')
    assert not verify_password('x', 'scrypt$$8$1$a$b')


def test_password_policy():
    assert password_policy_error('short') == 'Password must be at least 12 characters'
    assert password_policy_error('aaaaaaaaaaaaaa') == 'Password is too repetitive'
    assert password_policy_error('Password1234') == 'Password is too common'
    assert password_policy_error('a long enough passphrase') is None


@pytest.mark.skipif(not TSX.exists(), reason='TS toolchain removed after cutover')
def test_ts_reads_python_output(tmp_path):
    out = {
        'key': FIXTURE['key'], 'pepper': FIXTURE['pepper'],
        'cipher': [{'plain': x['plain'], 'aad': x['aad'],
                    'blob': base64.b64encode(CIPHER.encrypt(x['plain'], x['aad'])).decode()} for x in FIXTURE['cipher']],
        'passwords': [{'password': x['password'], 'hash': hash_password(x['password'], FIXTURE['pepper'])}
                      for x in FIXTURE['passwords']],
    }
    f = tmp_path / 'py.json'
    f.write_text(json.dumps(out))
    r = subprocess.run([str(TSX), 'src/verify-crypto-parity.ts', str(f)], cwd=REPO / 'apps' / 'api',
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.strip() == 'OK', r.stdout + r.stderr
