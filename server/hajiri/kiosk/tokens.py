"""
Signed tokens for kiosks (port of apps/api/src/modules/kiosk/tokens.ts), keyed by the field-encryption key
(FieldCipher.blind_index is an HMAC with that key), so nothing extra has to be configured and nothing can be forged.
Byte-compatible with the TS tokens: a badge or ticket made by one stack verifies on the other.
"""
from __future__ import annotations

import base64
import hmac
import os
import re
import threading
import time

from hajiri.core.crypto import field_cipher


def _sign(s: str) -> str:
    return field_cipher().blind_index(s).lower()


def _same(a: str, b: str) -> bool:
    return len(a) == len(b) and hmac.compare_digest(a.encode(), b.encode())


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- QR badge (rotating, 30 s windows)
QR_WINDOW_MS = 30_000


def qr_window(now: int | None = None) -> int:
    return (now if now is not None else _now_ms()) // QR_WINDOW_MS


def qr_token(employee_id: str, version: int, window: int | None = None) -> str:
    w = qr_window() if window is None else window
    return f'HQ1.{employee_id}.{w}.{_sign(f"kqr|{employee_id}|{w}|{version}")}'


_QR = re.compile(r'^HQ1\.([0-9a-f-]{36})\.(\d{1,12})\.([0-9a-f]{32})$')


def parse_qr(token: str) -> dict | None:
    """The employee and window a scanned badge claims (the signature is checked once the version is known)."""
    m = _QR.match(token.strip())
    if not m:
        return None
    return {'employeeId': m.group(1), 'window': int(m.group(2)), 'sig': m.group(3)}


def verify_qr(p: dict, version: int, now: int | None = None) -> bool:
    """Valid for the current window and one either side (clock drift, a slow scan)."""
    if abs(p['window'] - qr_window(now)) > 1:
        return False
    return _same(p['sig'], _sign(f"kqr|{p['employeeId']}|{p['window']}|{version}"))


# ---------------------------------------------------------------- identify -> punch ticket (2 minutes, one kiosk)
TICKET_TTL_MS = 2 * 60_000


def issue_ticket(kiosk_id: str, employee_id: str, now: int | None = None, via_qr: bool = False) -> str:
    exp = (now if now is not None else _now_ms()) + TICKET_TTL_MS
    # The first nonce character records how the person identified (signed with the rest): Q = QR badge, P = PIN.
    nonce = ('Q' if via_qr else 'P') + base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip('=')[1:]
    return f'{kiosk_id}.{employee_id}.{exp}.{nonce}.{_sign(f"kt|{kiosk_id}|{employee_id}|{exp}|{nonce}")}'


_TICKET = re.compile(r'^([0-9a-f-]{36})\.([0-9a-f-]{36})\.(\d{13})\.([A-Za-z0-9_-]{12})\.([0-9a-f]{32})$')


def verify_ticket(ticket: str, kiosk_id: str, now: int | None = None) -> dict | None:
    m = _TICKET.match(ticket)
    if not m or m.group(1) != kiosk_id:
        return None
    exp = int(m.group(3))
    if exp < (now if now is not None else _now_ms()):
        return None
    if not _same(m.group(5), _sign(f'kt|{m.group(1)}|{m.group(2)}|{exp}|{m.group(4)}')):
        return None
    return {'employeeId': m.group(2), 'nonce': m.group(4), 'viaQr': m.group(4).startswith('Q')}


# ---------------------------------------------------------------- pairing codes and PINs
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0/O, 1/I


def pairing_code() -> str:
    b = os.urandom(8)
    s = ''.join(CODE_ALPHABET[x % len(CODE_ALPHABET)] for x in b)
    return f'{s[:4]}-{s[4:]}'


def normalise_pairing_code(c: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', c.upper())


def weak_pin(pin: str) -> bool:
    """PINs anyone would guess first are refused."""
    if re.fullmatch(r'(\d)\1+', pin):
        return True
    digits = [int(c) for c in pin]
    up = all(i == 0 or d == (digits[i - 1] + 1) % 10 for i, d in enumerate(digits))
    down = all(i == 0 or d == (digits[i - 1] + 9) % 10 for i, d in enumerate(digits))
    return up or down or pin in ('1212', '2580', '0852', '1122', '112233', '121212', '123123')


# ---------------------------------------------------------------- in-memory rate limiting (per process)
_hits: dict[str, list[int]] = {}
_lock = threading.Lock()


def allow(key: str, limit: int, window_ms: int, now: int | None = None) -> bool:
    """True if `key` has made fewer than `limit` calls in the last `window_ms` (and records this one)."""
    now = now if now is not None else _now_ms()
    with _lock:
        lst = [t for t in _hits.get(key, []) if t > now - window_ms]
        if len(lst) >= limit:
            _hits[key] = lst
            return False
        lst.append(now)
        _hits[key] = lst
        if len(_hits) > 10_000:
            for k in [k for k, v in _hits.items() if not any(t > now - window_ms for t in v)]:
                del _hits[k]
        return True


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()
