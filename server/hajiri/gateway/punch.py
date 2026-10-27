"""Normalised, signed punch events (plan §7.7, §8.3). Port of apps/gateway/src/punch/{event,signer}.ts and crypto.ts."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field, replace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engines.attendance import MIN_MS, canonical_json, local_date_of, sha256_hex
from hajiri.core.crypto import FieldCipher

from .jsutil import dt_from_ms, iso_ms, utc_ms, utc_parts


class DeviceTimeError(ValueError):
    """RangeError in the TS: the device time is malformed or not a real calendar time."""


@dataclass
class NormalizedPunch:
    device_id: str
    device_sn: str
    vendor: str
    device_user_pin: str
    employee_id: str | None
    device_local_time: str
    direction: str
    verify_mode: str
    source: str
    raw: dict = field(default_factory=dict)
    punched_at_ms: int = 0           # UTC instant
    local_date: str = ''
    idempotency_key: str = ''
    gateway_signature: str = ''

    @property
    def punched_at(self):
        return dt_from_ms(self.punched_at_ms)

    @property
    def punched_at_iso(self) -> str:
        return iso_ms(self.punched_at_ms)


_DT_RE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})')


def device_time_to_utc(device_local_time: str, tz_offset_min: int) -> int:
    """'YYYY-MM-DD HH:MM:SS' in the device's fixed offset -> UTC instant (epoch ms)."""
    m = _DT_RE.fullmatch(device_local_time)
    if not m:
        raise DeviceTimeError(f'bad device time {device_local_time}')
    y, mo, d, h, mi, sec = (int(x) for x in m.groups())
    as_utc = utc_ms(y, mo, d, h, mi, sec)
    _, um, ud, _ = utc_parts(as_utc)
    if mo < 1 or mo > 12 or h > 23 or mi > 59 or sec > 59 or um != mo or ud != d:
        raise DeviceTimeError(f'invalid device time {device_local_time}')
    return as_utc - tz_offset_min * MIN_MS


def idempotency_key(*, vendor: str, device_sn: str, device_user_pin: str, device_local_time: str, verify_mode: str) -> str:
    """plan §7.7: sha256(vendor ‖ device_sn ‖ device_user_pin ‖ device_local_time ‖ verify_mode)."""
    return sha256_hex('|'.join([vendor, device_sn, device_user_pin, device_local_time, verify_mode]))


def build_punch(base: NormalizedPunch, tz_offset_min: int, signer: Signer) -> NormalizedPunch:
    at = device_time_to_utc(base.device_local_time, tz_offset_min)
    key = idempotency_key(vendor=base.vendor, device_sn=base.device_sn, device_user_pin=base.device_user_pin,
                          device_local_time=base.device_local_time, verify_mode=base.verify_mode)
    canonical = canonical_json({
        'vendor': base.vendor, 'deviceSn': base.device_sn, 'deviceUserPin': base.device_user_pin,
        'deviceLocalTime': base.device_local_time, 'punchedAt': iso_ms(at), 'direction': base.direction,
        'verifyMode': base.verify_mode, 'idempotencyKey': key,
    })
    return replace(base, raw=dict(base.raw), punched_at_ms=at, local_date=local_date_of(at), idempotency_key=key,
                   gateway_signature=signer.sign(canonical))


def canonical_event(p: NormalizedPunch) -> str:
    """The exact text a punch's gateway_signature covers (for verification by the API or an auditor)."""
    return canonical_json({
        'vendor': p.vendor, 'deviceSn': p.device_sn, 'deviceUserPin': p.device_user_pin,
        'deviceLocalTime': p.device_local_time, 'punchedAt': iso_ms(p.punched_at_ms), 'direction': p.direction,
        'verifyMode': p.verify_mode, 'idempotencyKey': p.idempotency_key,
    })


class Signer:
    """Ed25519 signer for canonical punch events (plan §8.3: "signs the canonical event with the gateway key")."""

    def __init__(self, key: Ed25519PrivateKey, ephemeral: bool):
        self._key = key
        self.ephemeral = ephemeral
        self.public_key_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')

    @classmethod
    def from_base64_pkcs8(cls, b64: str) -> Signer:
        key = serialization.load_der_private_key(base64.b64decode(b64), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError('GATEWAY_SIGNING_KEY must be an Ed25519 key')
        return cls(key, False)

    @classmethod
    def ephemeral_key(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate(), True)

    def sign(self, canonical: str) -> str:
        return base64.b64encode(self._key.sign(canonical.encode('utf-8'))).decode('ascii')


def generate_signing_key() -> str:
    """A new GATEWAY_SIGNING_KEY: base64 of an Ed25519 PKCS#8 DER private key (what scripts/setup.sh writes)."""
    der = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(der).decode('ascii')


class TemplateCipher(FieldCipher):
    """Template encryption in exactly the API's FieldCipher format: version(1) | iv(12) | tag(16) | ciphertext,
    AES-256-GCM, plaintext = base64 text of the template bytes, so the API decrypts to the same base64 string."""


def template_aad(subject: str, modality: str) -> str:
    return f'template:{subject}:{modality}:attendance'


def aad_hash(aad: str) -> str:
    return hashlib.sha256(aad.encode('utf-8')).hexdigest()
