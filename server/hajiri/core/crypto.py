"""Field encryption, password hashing and tokens. Port of apps/api/src/crypto.ts; the formats are byte-compatible."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from functools import cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings


class FieldCipher:
    """
    Field-level encryption for PAN, bank accounts and biometric templates (plan §12.2, §9.1 decision 5).
    AES-256-GCM with a 32-byte key from DATA_ENCRYPTION_KEY (base64). Layout:
        version(1) | iv(12) | tag(16) | ciphertext
    One versioned key for now; the format is already envelope-shaped so it can be re-wrapped without a schema change.
    """

    def __init__(self, key: bytes, key_version: int = 1):
        if len(key) != 32:
            raise ValueError('DATA_ENCRYPTION_KEY must be 32 bytes (base64 of 32 random bytes)')
        self._key = key
        self._aead = AESGCM(key)
        self.key_version = key_version

    @classmethod
    def from_base64(cls, b64: str) -> FieldCipher:
        return cls(base64.b64decode(b64))

    def encrypt(self, plaintext: str, aad: str) -> bytes:
        iv = os.urandom(12)
        sealed = self._aead.encrypt(iv, plaintext.encode('utf-8'), aad.encode('utf-8'))
        # cryptography returns ciphertext | tag; the stored layout puts the tag first.
        return bytes([self.key_version]) + iv + sealed[-16:] + sealed[:-16]

    def decrypt(self, blob: bytes, aad: str) -> str:
        blob = bytes(blob)
        if len(blob) < 29:
            raise ValueError('ciphertext too short')
        iv, tag, ct = blob[1:13], blob[13:29], blob[29:]
        return self._aead.decrypt(iv, ct + tag, aad.encode('utf-8')).decode('utf-8')

    def blind_index(self, value: str) -> str:
        """Deterministic blind index for equality lookups on encrypted fields (e.g. PAN)."""
        return hmac.new(self._key, ('bidx:' + value.strip().upper()).encode('utf-8'), hashlib.sha256).hexdigest()[:32]


@cache
def field_cipher() -> FieldCipher:
    if not settings.DATA_ENCRYPTION_KEY:
        raise RuntimeError('DATA_ENCRYPTION_KEY is not set')
    return FieldCipher.from_base64(settings.DATA_ENCRYPTION_KEY)


# ---------------------------------------------------------------- passwords
# scrypt with a server-side pepper; parameters are stored with the hash so they can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P, KEYLEN = 1 << 15, 8, 1, 64
_MAXMEM = 128 * 1024 * 1024


def _pepper(pepper: str | None) -> str:
    return settings.PASSWORD_PEPPER if pepper is None else pepper


def hash_password(password: str, pepper: str | None = None) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt((password + _pepper(pepper)).encode('utf-8'), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=_MAXMEM, dklen=KEYLEN)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}'


def verify_password(password: str, stored: str, pepper: str | None = None) -> bool:
    parts = stored.split('$')
    if len(parts) != 6 or parts[0] != 'scrypt' or not all(parts[1:]):
        return False
    _, n, r, p, salt_b64, hash_b64 = parts
    expected = base64.b64decode(hash_b64)
    actual = hashlib.scrypt((password + _pepper(pepper)).encode('utf-8'), salt=base64.b64decode(salt_b64), n=int(n),
                            r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


_COMMON = {'password1234', 'hajiri123456', '123456789012', 'qwertyuiop12'}


def password_policy_error(pw: str) -> str | None:
    """NIST SP 800-63B-4: length over composition, at least 12 characters (plan §12.4)."""
    if len(pw) < 12:
        return 'Password must be at least 12 characters'
    if len(pw) > 128:
        return 'Password must be at most 128 characters'
    if len(set(pw)) == 1:
        return 'Password is too repetitive'
    if pw.lower() in _COMMON:
        return 'Password is too common'
    return None


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(s: str) -> str:
    """Keyed hash used for session and token lookups (the key is a fixed label, as in the TS API)."""
    return hmac.new(b'hajiri-session', s.encode('utf-8'), hashlib.sha256).hexdigest()
