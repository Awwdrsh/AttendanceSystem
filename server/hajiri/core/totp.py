"""RFC 6238 time-based one-time passwords (SHA-1, 6 digits, 30-second steps), as used by authenticator apps."""
import base64
import hashlib
import hmac
import os
import re
import struct
import time
from urllib.parse import quote

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
_RECOVERY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def base32_encode(buf: bytes) -> str:
    return base64.b32encode(buf).decode().rstrip('=')


def base32_decode(s: str) -> bytes:
    clean = re.sub(r'[\s=-]', '', s.upper())
    if re.search(r'[^A-Z2-7]', clean):
        raise ValueError('invalid base32')
    # Trailing bits that do not fill a byte are dropped, as in the TS decoder.
    return base64.b32decode(clean + '=' * (-len(clean) % 8))[: len(clean) * 5 // 8]


def new_totp_secret() -> str:
    """A new 160-bit secret, base32 encoded (what the authenticator app stores)."""
    return base32_encode(os.urandom(20))


def step_at(seconds: float) -> int:
    return int(seconds // TOTP_STEP_SECONDS)


def hotp(secret: bytes, counter: int) -> str:
    """HOTP (RFC 4226) for one counter value."""
    h = hmac.new(secret, struct.pack('>Q', counter), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = struct.unpack('>I', h[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_at(secret_b32: str, seconds: float) -> str:
    return hotp(base32_decode(secret_b32), step_at(seconds))


def match_totp(secret_b32: str, code: str, seconds: float | None = None) -> int | None:
    """
    The step a code belongs to, allowing one step of clock drift either way; None when it matches none. Callers
    reject a step at or before the last one used, so a code can never be replayed.
    """
    c = re.sub(r'\s', '', code)
    if not re.fullmatch(r'\d{6}', c):
        return None
    secret = base32_decode(secret_b32)
    now = step_at(time.time() if seconds is None else seconds)
    for step in (now, now - 1, now + 1):
        if hmac.compare_digest(hotp(secret, step), c):
            return step
    return None


def otpauth_uri(secret_b32: str, account: str, issuer: str = 'Hajiri') -> str:
    """otpauth:// address for the QR code an authenticator app scans."""
    q = lambda s: quote(s, safe="-_.!~*'()")  # encodeURIComponent
    return (f'otpauth://totp/{q(issuer)}:{q(account)}?secret={secret_b32}&issuer={q(issuer)}'
            f'&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}')


def new_recovery_codes(n: int = 10) -> list[str]:
    """Ten one-time recovery codes like `7KQ4-M2XD-PA9C`: 60 bits each, shown once and stored hashed."""
    codes = []
    for _ in range(n):
        chars = ''.join(_RECOVERY_ALPHABET[b % len(_RECOVERY_ALPHABET)] for b in os.urandom(12))
        codes.append(f'{chars[:4]}-{chars[4:8]}-{chars[8:12]}')
    return codes


def normalise_recovery_code(code: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', code.upper())
