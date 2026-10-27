"""
Mobile, web and manual punches (port of apps/api/src/modules/punch: routes.ts, devices.ts, geo.ts, photo.ts).

Every punch answers with the recomputed day, never a silent success. The punch time is always the server's clock.

Public interface (other slices import from here):
- ensure_pseudo_device(company_id, 'MOBILE'|'WEB'|'MANUAL') -> {'id', 'serialNo', 'vendor'}
- npt_local_time(ms) -> 'YYYY-MM-DD HH:MM:SS' (Nepal), distance_m(...), geofence(lat, lng, accuracy_m, locations)
- parse_photo(data_url) -> ParsedPhoto(mime, data); photo_aad(employee_id, punch_id); PHOTO_MAX_BYTES
- punch(user, body, *, device_token=None, ip=None) -> (status, body)      POST /punch
- today(user) -> {'date', 'day', 'punches'}                                GET /punch/today
- remote(user, query) -> {'punches'}                                       GET /punch/remote
- photo(user, photo_id) -> {'mime', 'data'}                                GET /punch/photos/:id (audited)
- manual(user, body) -> (status, body)                                     POST /punch/manual
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from django.db import transaction

from engines.attendance import local_date_of, local_time_ms

from hajiri.attendance.repo import attendance_policy, from_ms, to_ms
from hajiri.attendance.service import is_attendance_locked, list_days, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, assert_manages, has_module, has_role
from hajiri.core.clock import now as _now
from hajiri.core.crypto import field_cipher, sha256_hex
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Bool, DateTimeStr, Enum, Hhmm, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid

CLOCK_TOLERANCE_MS = 10 * 60_000
PHOTO_MAX_BYTES = 300 * 1024
PHOTO_FIELD_MAX = math.ceil(PHOTO_MAX_BYTES * 4 / 3) + 64

# ---------------------------------------------------------------- devices.ts

_PSEUDO = {'MOBILE': ('mobile', 'app', 'Mobile app punches'), 'WEB': ('kiosk', 'app', 'Web punches'),
           'MANUAL': ('manual', 'manual', 'Manual entries (HR, regularisation, auto clock-out)')}


def ensure_pseudo_device(company_id: str, kind: str) -> dict:
    """Per-company pseudo device for non-terminal punches, created on first use (serial MOBILE-<code>, WEB-<code>,
    MANUAL-<code>)."""
    c = one('SELECT code FROM company WHERE id = %s', [str(company_id)])
    if not c:
        raise RuntimeError('company not found')
    serial = f"{kind}-{c['code']}"
    vendor, protocol, name = _PSEUDO[kind]
    execute("""INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES (%s, %s, %s, %s, %s, 'active')
               ON CONFLICT (serial_no) DO NOTHING""", [str(company_id), serial, vendor, protocol, name])
    d = one('SELECT id, vendor FROM device WHERE serial_no = %s', [serial])
    return {'id': str(d['id']), 'serialNo': serial, 'vendor': d['vendor']}


def npt_local_time(ms: int | float) -> str:
    """Local NPT 'YYYY-MM-DD HH:MM:SS' for the device_local_time column."""
    return (from_ms(ms).astimezone(timezone.utc) + timedelta(minutes=345)).strftime('%Y-%m-%d %H:%M:%S')


def npt_hm(d) -> str:
    """'14:05' in Nepal time."""
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace('Z', '+00:00'))
    return (d.astimezone(timezone.utc) + timedelta(minutes=345)).strftime('%H:%M')


# ---------------------------------------------------------------- geo.ts

def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000
    to_rad = math.radians
    d_lat = to_rad(lat2 - lat1)
    d_lng = to_rad(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def geofence(lat: float, lng: float, accuracy_m: float, locations: list[dict]) -> dict:
    """Nearest configured location and whether the point is inside its radius (accuracy widens the fence a little)."""
    if not locations:
        return {'inside': None, 'locationId': None, 'distanceM': None, 'locationName': None}
    best = None
    for loc in locations:
        d = distance_m(lat, lng, float(loc['lat']), float(loc['lng']))
        if best is None or d < best[1]:
            best = (loc, d)
    tolerance = min(accuracy_m, 100)
    loc, d = best
    return {'inside': d <= loc['radius_m'] + tolerance, 'locationId': str(loc['id']), 'distanceM': _js_round(d),
            'locationName': loc['name']}


# ---------------------------------------------------------------- photo.ts

@dataclass(frozen=True)
class ParsedPhoto:
    mime: str
    data: bytes


_SIGNATURES = {
    'image/jpeg': lambda b: len(b) > 3 and b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF,
    'image/png': lambda b: len(b) > 8 and b[:8] == b'\x89PNG\r\n\x1a\n',
    'image/webp': lambda b: len(b) > 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP',
}
_DATA_URL = re.compile(r'^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$')


def parse_photo(data_url: str) -> ParsedPhoto:
    """Decode a `data:image/...;base64,` photo from the clock-in screen, checking type, signature and size."""
    m = _DATA_URL.match(data_url.strip())
    if not m:
        raise BadRequestError('The photo must be a JPEG, PNG or WebP image.')
    mime = m.group(1)
    raw = m.group(2)
    try:
        data = base64.b64decode(raw + '=' * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        data = b''
    if len(data) == 0:
        raise BadRequestError('The photo is empty. Take it again.')
    if len(data) > PHOTO_MAX_BYTES:
        raise BadRequestError('The photo is too large (max 300 KB).')
    if not _SIGNATURES[mime](data):
        raise BadRequestError('The photo could not be read. Take it again.')
    return ParsedPhoto(mime, data)


def photo_aad(employee_id: str, punch_id: str) -> str:
    return f'punch_photo:{employee_id}:{punch_id}'


def store_punch_photo(*, photo_id: str, company_id: str, employee_id: str, punch: dict, photo: ParsedPhoto) -> None:
    """The encrypted photo row for a punch (same table and retention for home, kiosk and field punches)."""
    execute("""INSERT INTO punch_photo (id, company_id, employee_id, punch_id, punched_at, local_date, direction, mime, bytes, image_enc)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [photo_id, company_id, employee_id, str(punch['id']), punch['punched_at'], punch['local_date'], punch['direction'],
             photo.mime, len(photo.data),
             field_cipher().encrypt(base64.b64encode(photo.data).decode(), photo_aad(employee_id, str(punch['id'])))])


# ---------------------------------------------------------------- POST /punch

PUNCH_BODY = Obj({
    'direction': Enum(('in', 'out', 'unknown'), default='unknown'),
    'clientTime': DateTimeStr(),
    'idempotencyKey': Str(min=8, max=64),
    'lat': Num(min=-90, max=90, optional=True),
    'lng': Num(min=-180, max=180, optional=True),
    'accuracyM': Num(min=0, max=10_000, optional=True),
    'mocked': Bool(optional=True),
    'selfie': Str(max=PHOTO_FIELD_MAX, optional=True),
    'source': Enum(('mobile', 'web')),
    'reason': Str(max=300, optional=True),
})

_LOC_SCOPE = """company_id = %(c)s AND active AND CASE WHEN EXISTS (SELECT 1 FROM location_assignment la WHERE la.employee_id = %(e)s)
      THEN id IN (SELECT location_id FROM location_assignment la WHERE la.employee_id = %(e)s) ELSE (branch_id IS NULL OR branch_id = %(b)s) END"""


def _not_linked(full: bool = True) -> BadRequestError:
    return BadRequestError('Your account is not linked to an employee record. Ask HR to link it.' if full
                           else 'Your account is not linked to an employee record.')


def _parse_instant_ms(s: str) -> int:
    return to_ms(datetime.fromisoformat(s.replace('Z', '+00:00')))


def _punch_row(r: dict) -> dict:
    return {'id': str(r['id']), 'punched_at': r['punched_at'], 'local_date': str(r['local_date']), 'direction': r['direction']}


def punch(user: AuthUser, body: dict, *, device_token: str | None = None, ip: str | None = None) -> tuple[int, dict]:
    """Browser or phone clock-in/out with location, work-from-home photo and the company's clock-in rules."""
    from hajiri.attendance_tools.services import assert_clock_in_allowed
    from hajiri.field.services import field_policy
    from hajiri.relations.services import assert_registered_device

    employee_id = user.employee_id
    if not employee_id:
        raise _not_linked()
    b = parse(PUNCH_BODY, body)
    if b.get('mocked'):
        raise BadRequestError('Punch not saved. Your phone reported a fake location. Turn off mock location and try again.')
    photo = parse_photo(b['selfie']) if b.get('selfie') else None

    server_now = to_ms(_now())
    client_ms = _parse_instant_ms(b['clientTime'])
    clock_skew_ms = client_ms - server_now
    flags: list[str] = []
    at = server_now
    if abs(clock_skew_ms) > CLOCK_TOLERANCE_MS:
        flags.append('ClockSkew')

    e = one("""SELECT e.company_id, e.emp_code, a.branch_id, e.work_from_home, e.field_work FROM employee e
                 LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                WHERE e.id = %s AND e.status = 'active'""", [employee_id])
    if not e:
        raise NotFoundError('employee not found')
    company_id = str(e['company_id'])
    assert_registered_device(company_id, employee_id, device_token)

    geo = {'inside': None, 'locationId': None, 'distanceM': None, 'locationName': None}
    geofence_mode = field_policy(company_id)['geofenceMode']
    scope = {'c': company_id, 'b': e['branch_id'], 'e': employee_id}
    if geofence_mode == 'off':
        pass
    elif b.get('lat') is not None and b.get('lng') is not None:
        locs = rows(f'SELECT id, name, lat::float8 AS lat, lng::float8 AS lng, radius_m FROM location WHERE {_LOC_SCOPE}', scope)
        geo = geofence(b['lat'], b['lng'], b.get('accuracyM', 50), locs)
    else:
        if rows(f'SELECT 1 AS x FROM location WHERE {_LOC_SCOPE} LIMIT 1', scope):
            geo = {'inside': False, 'locationId': None, 'distanceM': None, 'locationName': None}
            flags.append('NoLocation')

    remote = False
    if geo['inside'] is not True:
        remote = bool(e['work_from_home']) or bool(rows(
            """SELECT 1 AS x FROM regularisation_request WHERE employee_id = %s AND kind = 'wfh' AND status = 'approved'
                AND local_date = %s LIMIT 1""", [employee_id, local_date_of(at)]))
    direction = b['direction']
    if remote:
        flags.append('Remote')
        policy = attendance_policy(company_id)
        if not photo and direction == 'in' and policy.get('wfhPhotoRequired') is not False:
            raise BadRequestError('Take a quick photo to clock in from home.', {'code': 'photo_required'})
    elif geo['inside'] is False and e['field_work']:
        flags.append('Field')
    elif geo['inside'] is False and geofence_mode == 'block':
        raise BadRequestError(
            f"You are {geo['distanceM']} m from {geo['locationName']}. Clock in when you reach your work location."
            if geo['distanceM'] is not None else 'Your location is needed to clock in. Allow location access and try again.',
            {'code': 'outside_location'})
    elif geo['inside'] is False:
        flags.append('OutsideLocation')
        if not (b.get('reason') or '').strip():
            raise BadRequestError(
                f"You are {geo['distanceM']} m from {geo['locationName']}. Add a short reason to punch anyway."
                if geo['distanceM'] is not None else 'Your location was not available. Add a short reason to punch anyway.')

    device = ensure_pseudo_device(company_id, 'MOBILE' if b['source'] == 'mobile' else 'WEB')
    key = sha256_hex(f"app|{employee_id}|{b['idempotencyKey']}")
    existing = one('SELECT id, punched_at, local_date FROM punch_event WHERE idempotency_key = %s', [key])
    if existing:
        day = list_days(employee_id, str(existing['local_date']), str(existing['local_date']))
        return 200, {'duplicate': True, 'punch': {'id': str(existing['id']), 'punched_at': existing['punched_at'],
                                                  'local_date': str(existing['local_date'])},
                     'day': day[0] if day else None}
    assert_clock_in_allowed(company_id=company_id, employee_id=employee_id, at_ms=at, direction=direction)
    local_date = local_date_of(at)
    photo_id = str(uuid.uuid4()) if remote and photo else None
    raw = {'lat': b.get('lat'), 'lng': b.get('lng'), 'accuracyM': b.get('accuracyM'), 'inside': geo['inside'], 'remote': remote,
           'locationId': geo['locationId'], 'distanceM': geo['distanceM'], 'clientTime': b['clientTime'],
           'clockSkewMs': clock_skew_ms, 'reason': b.get('reason'), 'flags': flags,
           'selfie': 'stored' if photo_id else ('discarded' if photo else None), 'photoId': photo_id, 'ip': ip}
    with transaction.atomic():
        ins = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                       local_date, direction, verify_mode, source, raw, idempotency_key)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, punched_at, local_date, direction""",
                  [device['id'], device['serialNo'], device['vendor'], e['emp_code'], employee_id, from_ms(at), npt_local_time(at),
                   local_date, direction, b['source'], b['source'], json.dumps(raw), key])
        if photo_id and photo:
            store_punch_photo(photo_id=photo_id, company_id=company_id, employee_id=employee_id, punch=ins, photo=photo)
    result = recompute_for_punch(employee_id, at, str(ins['id']))
    suffix = ' from home' if remote else (' (outside location, flagged)' if geo['inside'] is False else '')
    return 201, {
        'duplicate': False, 'punch': _punch_row(ins), 'flags': flags,
        'location': {'inside': geo['inside'], 'name': geo['locationName'], 'distanceM': geo['distanceM']},
        'day': result.day.to_dict(), 'remote': remote, 'photoId': photo_id,
        'message': f"Clocked {'out' if direction == 'out' else 'in'} at {npt_local_time(at)[11:16]}{suffix}",
    }


# ---------------------------------------------------------------- GET /punch/today

def today(user: AuthUser) -> dict:
    """ESS "Today" card: latest computed day plus raw punches for the caller."""
    employee_id = user.employee_id
    if not employee_id:
        raise _not_linked(full=False)
    d = local_date_of(to_ms(_now()))
    days = list_days(employee_id, d, d)
    punches = rows("""SELECT id, punched_at, direction, verify_mode, source, device_sn, coalesce((raw->>'remote')::boolean, false) AS remote,
                             (SELECT ph.id FROM punch_photo ph WHERE ph.punch_id = punch_event.id AND ph.image_enc IS NOT NULL) AS photo_id
                        FROM punch_event WHERE employee_id = %s AND local_date = %s
                         AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = punch_event.id) ORDER BY punched_at""",
                   [employee_id, d])
    for p in punches:
        p['id'] = str(p['id'])
        p['photo_id'] = str(p['photo_id']) if p['photo_id'] else None
    return {'date': d, 'day': days[0] if days else None, 'punches': punches}


# ---------------------------------------------------------------- GET /punch/remote

REMOTE_QUERY = Obj({'from': IsoDate(), 'to': IsoDate(), 'employeeId': Uuid(optional=True)})


def _days_between(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def remote(user: AuthUser, query: dict) -> dict:
    """Clock-ins from home with their photos. HR sees the company, a manager their reports and themselves, everyone else
    only their own. Exact coordinates are never returned."""
    q = parse(REMOTE_QUERY, query)
    if q['to'] < q['from']:
        raise BadRequestError('"to" is before "from"')
    if _days_between(q['from'], q['to']) > 92:
        raise BadRequestError('pick a range of 3 months or less')
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    out = rows("""
        SELECT p.id AS punch_id, p.employee_id, e.emp_code, e.name_en, e.designation, p.punched_at, p.local_date::text AS local_date, p.direction,
               ph.id AS photo_id, ph.purged_at IS NOT NULL AS photo_purged,
               (p.raw->>'lat') IS NOT NULL AS location_shared, (p.raw->>'accuracyM')::float8 AS accuracy_m, p.raw->>'reason' AS reason,
               p.raw->'flags' AS flags
          FROM punch_event p JOIN employee e ON e.id = p.employee_id
          LEFT JOIN punch_photo ph ON ph.punch_id = p.id
         WHERE e.company_id = %(c)s AND p.local_date BETWEEN %(f)s AND %(t)s AND p.raw->>'remote' = 'true'
           AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
           AND (%(emp)s::uuid IS NULL OR p.employee_id = %(emp)s::uuid)
           AND (%(all)s OR p.employee_id = %(me)s::uuid OR (%(mgr)s AND hj_manages(%(me)s::uuid, e.id, p.local_date)))
         ORDER BY p.punched_at DESC LIMIT 1000""",
               {'c': user.company_id, 'f': q['from'], 't': q['to'], 'emp': q.get('employeeId'), 'all': has_module(user, 'attendance'),
                'me': user.employee_id, 'mgr': has_role(user, 'manager')})
    for r in out:
        for k in ('punch_id', 'employee_id', 'photo_id'):
            r[k] = str(r[k]) if r[k] else None
    return {'punches': out}


# ---------------------------------------------------------------- GET /punch/photos/:id

def photo(user: AuthUser, photo_id: str) -> dict:
    """The photo itself, for the employee, their manager and HR. Views by anyone other than the employee are audited."""
    pid = parse_uuid(photo_id)
    row = one('SELECT employee_id, punch_id, mime, image_enc, company_id FROM punch_photo WHERE id = %s', [pid])
    if not row or str(row['company_id']) != str(user.company_id):
        raise NotFoundError('photo not found')
    emp = str(row['employee_id'])
    assert_manages(user, emp, 'attendance')
    if row['image_enc'] is None:
        raise NotFoundError('this photo was deleted after the retention period')
    if user.employee_id != emp:
        audit('view_photo', 'punch_photo', actor_id=user.id, row_id=pid)
    data = base64.b64decode(field_cipher().decrypt(bytes(row['image_enc']), photo_aad(emp, str(row['punch_id']))))
    return {'mime': row['mime'], 'data': data}


# ---------------------------------------------------------------- POST /punch/manual

MANUAL_BODY = Obj({'employeeId': Uuid(), 'localDate': IsoDate(), 'time': Hhmm(), 'direction': Enum(('in', 'out')),
                   'reason': Str(min=3, max=300)})


def _require_module(user: AuthUser, module: str) -> None:
    from hajiri.core.auth import MODULE_ROLES
    if not has_module(user, module):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES[module])}")


def manual(user: AuthUser, body: dict) -> tuple[int, dict]:
    """HR manual entry with a reason; audited; day recomputed."""
    _require_module(user, 'attendance')
    b = parse(MANUAL_BODY, body)
    if b['employeeId'] == user.employee_id and not has_role(user, 'admin'):
        raise ForbiddenError('someone else must add punches to your own attendance')
    emp = one('SELECT emp_code FROM employee WHERE id = %s AND company_id = %s', [b['employeeId'], user.company_id])
    if not emp:
        raise NotFoundError('employee not found')
    if is_attendance_locked(b['employeeId'], b['localDate']):
        raise ConflictError('this day is locked by payroll; raise a supplementary correction instead')
    with transaction.atomic():
        device = ensure_pseudo_device(str(user.company_id), 'MANUAL')
        at = local_time_ms(b['localDate'], b['time'])
        key = sha256_hex(f"manual|{b['employeeId']}|{b['localDate']}|{b['time']}|{b['direction']}")
        ins = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                       local_date, direction, verify_mode, source, raw, idempotency_key)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', 'manual', %s, %s)
                     ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id""",
                  [device['id'], device['serialNo'], device['vendor'], emp['emp_code'], b['employeeId'], from_ms(at), npt_local_time(at),
                   b['localDate'], b['direction'], json.dumps({'reason': b['reason'], 'enteredBy': user.id}), key])
        if not ins:
            return 200, {'duplicate': True}
        audit('manual_punch', 'punch_event', actor_id=user.id, row_id=str(ins['id']), after=b)
    result = recompute_for_punch(b['employeeId'], at, str(ins['id']))
    return 201, {'duplicate': False, 'punchId': str(ins['id']), 'day': result.day.to_dict()}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
