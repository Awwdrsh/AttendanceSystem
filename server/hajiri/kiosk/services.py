"""
Shared clock-in tablets (port of apps/api/src/modules/kiosk/routes.ts). A tablet pairs once with a one-time code from
HR and then sends its kiosk key in the X-Kiosk-Key header. Employees identify with their code and PIN (or a rotating QR
badge), get a two-minute ticket, take a selfie and punch at server time.

A kiosk belongs to exactly one company (the one whose HR paired it): every employee, credential and punch it touches
is looked up in that company only, so a code, PIN or badge of another company of the group does not work there.

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

import math
from datetime import timedelta

from django.db.models import F
from django.db.models.functions import Now

from hajiri.core.audit import audit
from hajiri.core.clock import now as _now
from hajiri.core.crypto import random_token, sha256_hex, verify_password
from hajiri.core.errors import BadRequestError, ConflictError, HttpError, NotFoundError, UnauthorizedError
from hajiri.core.scoping import is_uuid
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import validate
from hajiri.db.models import Company, Employee, Kiosk, KioskCredential, KioskQrUse, WorkBreak

from . import tokens
from .serializers import KioskPunchIn, PairIn, identify_body

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
    b = validate(PairIn, body)
    h = sha256_hex(f'kiosk-pair|{tokens.normalise_pairing_code(b["code"])}')
    k = (Kiosk.objects.filter(pairing_code_hash=h, pairing_expires_at__gt=Now()).exclude(status=Kiosk.Status.REVOKED)
         .values('id', 'name').first())
    if not k:
        raise BadRequestError('That pairing code is wrong or has expired. Ask HR for a new one.')
    key = random_token(32)
    Kiosk.objects.filter(pk=k['id']).update(
        key_hash=sha256_hex(f'kiosk-key|{key}'), status=Kiosk.Status.ACTIVE, pairing_code_hash=None, pairing_expires_at=None,
        paired_at=Now(), last_seen_at=Now(), failed_attempts=0, locked_until=None)
    audit('kiosk_paired', 'kiosk', row_id=str(k['id']), after={'ip': ip})
    return {'key': key, 'kiosk': {'id': str(k['id']), 'name': k['name']}}


# ---------------------------------------------------------------- the paired-kiosk gate

def authenticate(key: str | None) -> dict:
    from hajiri.features.service import feature_on
    if not key or len(key) < 20 or len(key) > 100:
        raise UnauthorizedError('This tablet is not paired. Enter a pairing code from HR.')
    k = (Kiosk.objects.filter(key_hash=sha256_hex(f'kiosk-key|{key}'), status=Kiosk.Status.ACTIVE)
         .values('id', 'company_id', 'name', 'branch_id', 'location_id', 'selfie_required', 'locked_until', 'last_seen_at')
         .first())
    if not k:
        raise UnauthorizedError('This tablet is no longer paired. Ask HR for a new pairing code.')
    k = {**k, 'id': str(k['id']), 'company_id': str(k['company_id']),
         'location_id': str(k['location_id']) if k['location_id'] else None}
    if not feature_on(k['company_id'], 'field'):
        raise NotFoundError('feature off')
    if not k['last_seen_at'] or (_now() - k['last_seen_at']).total_seconds() > 60:
        Kiosk.objects.filter(pk=k['id']).update(last_seen_at=Now())
    return k


def session(k: dict) -> dict:
    company_name = Company.objects.filter(pk=k['company_id']).values_list('name_en', flat=True).first()
    profile = get_setting(k['company_id'], 'company.profile') if company_name is not None else None
    name = profile.get('nameEn') if isinstance(profile, dict) else None
    return {
        'kiosk': {'id': k['id'], 'name': k['name'], 'selfieRequired': k['selfie_required'],
                  'lockedUntil': k['locked_until'] if k['locked_until'] and k['locked_until'] > _now() else None},
        'company': {'name': name if name is not None else (company_name or '')},
        'serverTime': _now(),
    }


def _kiosk_locked(k: dict) -> bool:
    return bool(k['locked_until']) and k['locked_until'] > _now()


def _kiosk_failure(k: dict) -> None:
    """A wrong code or PIN counts against the kiosk too; many failures lock the tablet for a few minutes."""
    counted = Kiosk.objects.filter(pk=k['id'], failed_attempts__lt=KIOSK_MAX_FAILS - 1).update(
        failed_attempts=F('failed_attempts') + 1)
    if not counted:          # this failure is the KIOSK_MAX_FAILS-th
        Kiosk.objects.filter(pk=k['id']).update(failed_attempts=0, locked_until=_now() + timedelta(minutes=KIOSK_LOCK_MIN))
        audit('kiosk_locked', 'kiosk', row_id=k['id'])


def _wrong_pin(k: dict, employee_id: str) -> HttpError:
    """Count a wrong PIN; the PIN_MAX_FAILS-th in a row locks the PIN for PIN_LOCK_MIN minutes."""
    _kiosk_failure(k)
    cred = KioskCredential.objects.filter(employee_id=employee_id)
    if cred.filter(failed_count__lt=PIN_MAX_FAILS - 1).update(failed_count=F('failed_count') + 1):
        left = PIN_MAX_FAILS - (cred.values_list('failed_count', flat=True).first() or 0)
        return BadRequestError(f"Employee code or PIN is wrong. {left} {'try' if left == 1 else 'tries'} left.")
    cred.update(failed_count=0, locked_until=_now() + timedelta(minutes=PIN_LOCK_MIN))
    audit('kiosk_pin_locked', 'kiosk_credential', row_id=employee_id, after={'kioskId': k['id']})
    return TooManyError(f'Too many wrong PINs. Your kiosk PIN is locked for {PIN_LOCK_MIN} min.')


def _by_qr(k: dict, qr: str) -> str:
    """The employee a scanned badge names, once its signature, window and single use check out."""
    p = tokens.parse_qr(qr)
    if p and is_uuid(p['employeeId']):
        cred = (KioskCredential.objects.for_company(k['company_id']).filter(employee_id=p['employeeId'])
                .values_list('qr_version', flat=True).first())
    else:
        p, cred = None, None
    if not p or not tokens.verify_qr(p, cred or 1):
        _kiosk_failure(k)
        raise BadRequestError('This QR code is not valid or has expired. Show the code in the app again.')
    _, first_use = KioskQrUse.objects.get_or_create(employee_id=p['employeeId'], time_window=p['window'],
                                                    defaults={'kiosk_id': k['id']})
    if not first_use:
        raise BadRequestError('This QR code was already used. Wait for the next code in the app.')
    return p['employeeId']


def _by_pin(k: dict, emp_code: str, pin: str) -> str:
    row = (Employee.objects.for_company(k['company_id']).filter(emp_code__iexact=emp_code, status=Employee.Status.ACTIVE)
           .values('id', 'kiosk_credential__pin_hash', 'kiosk_credential__locked_until').first())
    locked_until = row['kiosk_credential__locked_until'] if row else None
    if locked_until and locked_until > _now():
        mins = max(1, math.ceil((locked_until - _now()).total_seconds() / 60))
        raise TooManyError(f'Your kiosk PIN is locked after too many wrong tries. Try again in {mins} min or ask HR to reset it.')
    if not row or not row['kiosk_credential__pin_hash']:
        _kiosk_failure(k)
        # Same answer whether the code is unknown or no PIN is set, so codes cannot be probed.
        raise BadRequestError('Employee code or PIN is wrong. Set your kiosk PIN in the app under My attendance.')
    eid = str(row['id'])
    if not verify_password(pin, row['kiosk_credential__pin_hash']):
        raise _wrong_pin(k, eid)
    KioskCredential.objects.filter(employee_id=eid).update(failed_count=0, locked_until=None)
    return eid


def identify(k: dict, body: dict) -> dict:
    from hajiri.field.services import next_direction
    if _kiosk_locked(k):
        raise TooManyError('This kiosk is paused after too many wrong attempts. Try again in a few minutes.')
    if not tokens.allow(f"identify|{k['id']}", 30, 60_000):
        raise TooManyError('Too many attempts on this kiosk. Wait a minute.')
    b = identify_body(body)
    via_qr = 'qr' in b
    employee_id = _by_qr(k, b['qr']) if via_qr else _by_pin(k, b['empCode'], b['pin'])

    emp = (Employee.objects.for_company(k['company_id']).filter(pk=employee_id, status=Employee.Status.ACTIVE)
           .values('emp_code', 'name_en', 'designation').first())
    if not emp:
        _kiosk_failure(k)
        raise BadRequestError('This QR code is not valid here.')
    Kiosk.objects.filter(pk=k['id']).update(failed_attempts=0)
    open_break = WorkBreak.objects.filter(employee_id=employee_id, ended_at__isnull=True).values('kind', 'started_at').first()
    return {
        'ticket': tokens.issue_ticket(k['id'], employee_id, via_qr=via_qr),
        'employee': {'name': emp['name_en'], 'empCode': emp['emp_code'], 'designation': emp['designation']},
        'nextDirection': next_direction(employee_id), 'selfieRequired': bool(k['selfie_required'] or via_qr),
        'openBreak': open_break,
    }


def punch(k: dict, body: dict, ip: str | None = None) -> tuple[int, dict]:
    from hajiri.attendance.repo import to_ms
    from hajiri.attendance_tools.services import assert_clock_in_allowed
    from hajiri.field.services import close_open_break, insert_app_punch, next_direction
    from hajiri.punch.services import npt_local_time, parse_photo
    if not tokens.allow(f"punch|{k['id']}", 60, 60_000):
        raise TooManyError('Too many punches on this kiosk. Wait a minute.')
    b = validate(KioskPunchIn, body)
    t = tokens.verify_ticket(b['ticket'], k['id'])
    if not t:
        raise BadRequestError('This sign-in has expired. Enter your code and PIN again.')
    photo = parse_photo(b['selfie']) if b.get('selfie') else None
    if (k['selfie_required'] or t['viaQr']) and not photo:
        raise BadRequestError('Take a quick photo to punch at this kiosk.', {'code': 'photo_required'})
    emp = (Employee.objects.for_company(k['company_id']).filter(pk=t['employeeId'], status=Employee.Status.ACTIVE)
           .values('emp_code', 'name_en').first())
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
