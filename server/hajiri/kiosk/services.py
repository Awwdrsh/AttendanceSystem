"""
Shared clock-in tablets (port of apps/api/src/modules/kiosk/routes.ts). A tablet pairs once with a one-time code from
HR and then sends its kiosk key in the X-Kiosk-Key header. Employees identify with their code and PIN (or a rotating QR
badge), get a two-minute ticket, take a selfie and punch at server time.

Failure counters (wrong PINs, wrong codes) are never inside transaction.atomic: raising the error must not roll the
count back.

Public interface:
- KIOSK_HEADER, PIN_MAX_FAILS, PIN_LOCK_MIN, KIOSK_MAX_FAILS, KIOSK_LOCK_MIN
- pair(body, ip) -> {'key', 'kiosk': {'id', 'name'}}
- authenticate(key) -> kiosk row (raises 401 / 404 'feature off')
- session(kiosk) -> {'kiosk', 'company', 'serverTime'}
- identify(kiosk, body) -> {'ticket', 'employee', 'nextDirection', 'selfieRequired', 'openBreak'}
- punch(kiosk, body, ip) -> (status, body)
"""
from __future__ import annotations

from hajiri.core.audit import audit
from hajiri.core.clock import now as _now
from hajiri.core.crypto import random_token, sha256_hex, verify_password
from hajiri.core.db import execute, one
from hajiri.core.errors import BadRequestError, ConflictError, HttpError, NotFoundError, UnauthorizedError
from hajiri.core.schema import Obj, Str, Union, parse

from . import tokens

KIOSK_HEADER = 'X-Kiosk-Key'
PIN_MAX_FAILS = 5
PIN_LOCK_MIN = 15
KIOSK_MAX_FAILS = 20
KIOSK_LOCK_MIN = 5


class TooManyError(HttpError):
    status, default_message = 429, 'too many requests'


# ---------------------------------------------------------------- pairing

def pair(body: dict, ip: str | None) -> dict:
    if not tokens.allow(f'pair|{ip}', 10, 10 * 60_000):
        raise TooManyError('Too many pairing attempts. Wait a few minutes and try again.')
    b = parse(Obj({'code': Str(min=4, max=20)}), body)
    h = sha256_hex(f'kiosk-pair|{tokens.normalise_pairing_code(b["code"])}')
    k = one("""SELECT id, company_id, name FROM kiosk WHERE pairing_code_hash = %s AND pairing_expires_at > now() AND status <> 'revoked'""", [h])
    if not k:
        raise BadRequestError('That pairing code is wrong or has expired. Ask HR for a new one.')
    key = random_token(32)
    execute("""UPDATE kiosk SET key_hash = %s, status = 'active', pairing_code_hash = NULL, pairing_expires_at = NULL, paired_at = now(),
                      last_seen_at = now(), failed_attempts = 0, locked_until = NULL WHERE id = %s""",
            [sha256_hex(f'kiosk-key|{key}'), str(k['id'])])
    audit('kiosk_paired', 'kiosk', row_id=str(k['id']), after={'ip': ip})
    return {'key': key, 'kiosk': {'id': str(k['id']), 'name': k['name']}}


# ---------------------------------------------------------------- the paired-kiosk gate

def authenticate(key: str | None) -> dict:
    from hajiri.features.service import feature_on
    if not key or len(key) < 20 or len(key) > 100:
        raise UnauthorizedError('This tablet is not paired. Enter a pairing code from HR.')
    k = one("""SELECT id, company_id, name, branch_id, location_id, selfie_required, locked_until, last_seen_at FROM kiosk
                WHERE key_hash = %s AND status = 'active'""", [sha256_hex(f'kiosk-key|{key}')])
    if not k:
        raise UnauthorizedError('This tablet is no longer paired. Ask HR for a new pairing code.')
    k = {**k, 'id': str(k['id']), 'company_id': str(k['company_id']),
         'location_id': str(k['location_id']) if k['location_id'] else None}
    if not feature_on(k['company_id'], 'field'):
        raise NotFoundError('feature off')
    if not k['last_seen_at'] or (_now() - k['last_seen_at']).total_seconds() > 60:
        execute('UPDATE kiosk SET last_seen_at = now() WHERE id = %s', [k['id']])
    return k


def session(k: dict) -> dict:
    c = one("""SELECT c.name_en, s.value FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'company.profile'
                WHERE c.id = %s""", [k['company_id']])
    name = ((c or {}).get('value') or {}).get('nameEn') if c else None
    return {
        'kiosk': {'id': k['id'], 'name': k['name'], 'selfieRequired': k['selfie_required'],
                  'lockedUntil': k['locked_until'] if k['locked_until'] and k['locked_until'] > _now() else None},
        'company': {'name': name if name is not None else (c['name_en'] if c else '')},
        'serverTime': _now(),
    }


def _kiosk_locked(k: dict) -> bool:
    return bool(k['locked_until']) and k['locked_until'] > _now()


def _kiosk_failure(k: dict) -> None:
    """A wrong code or PIN counts against the kiosk too; many failures lock the tablet for a few minutes."""
    u = one('UPDATE kiosk SET failed_attempts = failed_attempts + 1 WHERE id = %s RETURNING failed_attempts', [k['id']])
    if (u['failed_attempts'] if u else 0) >= KIOSK_MAX_FAILS:
        execute('UPDATE kiosk SET failed_attempts = 0, locked_until = now() + make_interval(mins => %s) WHERE id = %s', [KIOSK_LOCK_MIN, k['id']])
        audit('kiosk_locked', 'kiosk', row_id=k['id'])


IDENTIFY = Union(
    Obj({'empCode': Str(trim=True, min=1, max=32), 'pin': Str(regex=r'^\d{4,6}$', regex_message='PIN is 4 to 6 digits')}),
    Obj({'qr': Str(min=10, max=200)}),
)


def identify(k: dict, body: dict) -> dict:
    from hajiri.field.services import next_direction
    if _kiosk_locked(k):
        raise TooManyError('This kiosk is paused after too many wrong attempts. Try again in a few minutes.')
    if not tokens.allow(f"identify|{k['id']}", 30, 60_000):
        raise TooManyError('Too many attempts on this kiosk. Wait a minute.')
    b = parse(IDENTIFY, body)
    via_qr = False
    if 'qr' in b:
        p = tokens.parse_qr(b['qr'])
        cred = one('SELECT qr_version, company_id FROM kiosk_credential WHERE employee_id = %s', [p['employeeId']]) if p else None
        version = cred['qr_version'] if cred and str(cred['company_id']) == k['company_id'] else 1
        if not p or not tokens.verify_qr(p, version):
            _kiosk_failure(k)
            raise BadRequestError('This QR code is not valid or has expired. Show the code in the app again.')
        used = execute('INSERT INTO kiosk_qr_use (employee_id, time_window, kiosk_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
                       [p['employeeId'], p['window'], k['id']])
        if not used:
            raise BadRequestError('This QR code was already used. Wait for the next code in the app.')
        employee_id = p['employeeId']
        via_qr = True
    else:
        row = one("""SELECT e.id, c.pin_hash, c.locked_until FROM employee e LEFT JOIN kiosk_credential c ON c.employee_id = e.id
                      WHERE e.company_id = %s AND upper(e.emp_code) = upper(%s) AND e.status = 'active'""", [k['company_id'], b['empCode']])
        if row and row['locked_until'] and row['locked_until'] > _now():
            import math
            mins = max(1, math.ceil((row['locked_until'] - _now()).total_seconds() / 60))
            raise TooManyError(f'Your kiosk PIN is locked after too many wrong tries. Try again in {mins} min or ask HR to reset it.')
        if not row or not row['pin_hash']:
            _kiosk_failure(k)
            # Same answer whether the code is unknown or no PIN is set, so codes cannot be probed.
            raise BadRequestError('Employee code or PIN is wrong. Set your kiosk PIN in the app under My attendance.')
        eid = str(row['id'])
        if not verify_password(b['pin'], row['pin_hash']):
            _kiosk_failure(k)
            u = one('UPDATE kiosk_credential SET failed_count = failed_count + 1 WHERE employee_id = %s RETURNING failed_count', [eid])
            n = u['failed_count'] if u else 0
            if n >= PIN_MAX_FAILS:
                execute('UPDATE kiosk_credential SET failed_count = 0, locked_until = now() + make_interval(mins => %s) WHERE employee_id = %s',
                        [PIN_LOCK_MIN, eid])
                audit('kiosk_pin_locked', 'kiosk_credential', row_id=eid, after={'kioskId': k['id']})
                raise TooManyError(f'Too many wrong PINs. Your kiosk PIN is locked for {PIN_LOCK_MIN} min.')
            left = PIN_MAX_FAILS - n
            raise BadRequestError(f"Employee code or PIN is wrong. {left} {'try' if left == 1 else 'tries'} left.")
        execute('UPDATE kiosk_credential SET failed_count = 0, locked_until = NULL WHERE employee_id = %s', [eid])
        employee_id = eid

    emp = one("SELECT id, emp_code, name_en, designation FROM employee WHERE id = %s AND company_id = %s AND status = 'active'",
              [employee_id, k['company_id']])
    if not emp:
        _kiosk_failure(k)
        raise BadRequestError('This QR code is not valid here.')
    execute('UPDATE kiosk SET failed_attempts = 0 WHERE id = %s', [k['id']])
    direction = next_direction(employee_id)
    open_break = one('SELECT kind, started_at FROM work_break WHERE employee_id = %s AND ended_at IS NULL', [employee_id])
    return {
        'ticket': tokens.issue_ticket(k['id'], employee_id, via_qr=via_qr),
        'employee': {'name': emp['name_en'], 'empCode': emp['emp_code'], 'designation': emp['designation']},
        'nextDirection': direction, 'selfieRequired': bool(k['selfie_required'] or via_qr), 'openBreak': open_break,
    }


def punch(k: dict, body: dict, ip: str | None = None) -> tuple[int, dict]:
    from hajiri.attendance.repo import to_ms
    from hajiri.attendance_tools.services import assert_clock_in_allowed
    from hajiri.field.services import close_open_break, insert_app_punch, next_direction
    from hajiri.punch.services import PHOTO_FIELD_MAX, npt_local_time, parse_photo
    if not tokens.allow(f"punch|{k['id']}", 60, 60_000):
        raise TooManyError('Too many punches on this kiosk. Wait a minute.')
    b = parse(Obj({'ticket': Str(min=20, max=200), 'selfie': Str(max=PHOTO_FIELD_MAX, optional=True)}), body)
    t = tokens.verify_ticket(b['ticket'], k['id'])
    if not t:
        raise BadRequestError('This sign-in has expired. Enter your code and PIN again.')
    photo = parse_photo(b['selfie']) if b.get('selfie') else None
    if (k['selfie_required'] or t['viaQr']) and not photo:
        raise BadRequestError('Take a quick photo to punch at this kiosk.', {'code': 'photo_required'})
    emp = one("SELECT emp_code, name_en FROM employee WHERE id = %s AND company_id = %s AND status = 'active'", [t['employeeId'], k['company_id']])
    if not emp:
        raise ConflictError('This employee can no longer punch here.')
    direction = next_direction(t['employeeId'])
    assert_clock_in_allowed(company_id=k['company_id'], employee_id=t['employeeId'], at_ms=to_ms(_now()), direction=direction)
    p = insert_app_punch(company_id=k['company_id'], employee_id=t['employeeId'], emp_code=emp['emp_code'], direction=direction, kind='kiosk',
                         idempotency=t['nonce'], photo=photo,
                         raw={'kioskId': k['id'], 'kioskName': k['name'], 'locationId': k['location_id'], 'remote': False,
                              'flags': ['Kiosk'], 'ip': ip})
    if not p['duplicate'] and direction == 'out':
        close_open_break(t['employeeId'], p['punchedAt'])
    time = npt_local_time(to_ms(p['punchedAt']))[11:16]
    first = emp['name_en'].split()[0] if emp['name_en'].split() else emp['name_en']
    msg = (f'Already recorded at {time}.' if p['duplicate'] else
           f"{'Clocked in' if direction == 'in' else 'Clocked out'} at {time}. "
           f"{'Have a good day' if direction == 'in' else 'See you tomorrow'}, {first}!")
    return (200 if p['duplicate'] else 201), {'duplicate': p['duplicate'], 'direction': direction, 'punchedAt': p['punchedAt'],
                                              'name': emp['name_en'], 'message': msg}
