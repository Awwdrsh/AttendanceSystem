"""crypto.py reads what the TypeScript API wrote (fixtures recorded from it), so existing data stays readable."""
import base64
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from hajiri.core.crypto import FieldCipher, hash_password, password_policy_error, sha256_hex, verify_password

FIXTURE = json.loads((Path(__file__).parent.parent / 'parity' / 'fixtures' / 'crypto.json').read_text())
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
