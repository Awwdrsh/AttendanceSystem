"""
Mobile, web and manual punches (port of apps/api/src/modules/punch: routes.ts, devices.ts, geo.ts, photo.ts).

Every punch answers with the recomputed day, never a silent success. The punch time is always the server's clock.
Every query is scoped to the caller's ACTIVE company: an employee, photo or location of another company is not found.

Public interface (other slices import from here):
- ensure_pseudo_device(company_id, 'MOBILE'|'WEB'|'MANUAL') -> {'id', 'serialNo', 'vendor'}; device_for(...)
- npt_local_time(ms) -> 'YYYY-MM-DD HH:MM:SS' (Nepal), npt_hm(instant), distance_m(...), geofence(lat, lng, accuracy_m, locations)
- parse_photo(data_url) -> ParsedPhoto(mime, data); photo_aad(employee_id, punch_id); PHOTO_MAX_BYTES, PHOTO_FIELD_MAX
- store_punch_photo(...), insert_punch(**fields) -> PunchEvent | None (None: the idempotency key is already used)
- visible_to(user, employee_field, date_field) -> Q: HR everyone, a manager their people on that day, else oneself
- punch(user, body, *, device_token=None, ip=None) -> (status, body)      POST /punch
- today(user) -> {'date', 'day', 'punches'}                                GET /punch/today
- remote(user, query) -> {'punches'}                                       GET /punch/remote
- photo(user, photo_id) -> {'mime', 'data'}                                GET /punch/photos/:id (audited)
- manual(user, body) -> (status, body)                                     POST /punch/manual
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from datetime import date, datetime, timedelta, timezone

from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Exists, ExpressionWrapper, F, FloatField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.fields.json import KT, KeyTransform
from django.db.models.functions import Cast, Coalesce

from engines.attendance import local_date_of, local_time_ms

from hajiri.attendance.repo import attendance_policy, from_ms, live_punches, to_ms
from hajiri.attendance.service import is_attendance_locked, list_days, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role, manages
from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher, sha256_hex
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    Company, Device, Employee, EmployeeAssignment, Location, LocationAssignment, PunchEvent, PunchPhoto,
    RegularisationRequest,
)
from hajiri.files.store import has_signature

from .serializers import PHOTO_FIELD_MAX, PHOTO_MAX_BYTES, ManualIn, PunchIn, RangeIn

__all__ = ['PHOTO_FIELD_MAX', 'PHOTO_MAX_BYTES', 'ParsedPhoto', 'ensure_pseudo_device', 'npt_local_time', 'parse_photo',
           'punch', 'today', 'remote', 'photo', 'manual']

CLOCK_TOLERANCE_MS = 10 * 60_000
UNIQUE_VIOLATION = '23505'
EXCLUSION_VIOLATION = '23P01'
NPT = timezone(timedelta(minutes=345))

# ---------------------------------------------------------------- devices.ts

_PSEUDO = {'MOBILE': (Device.Vendor.MOBILE, Device.Protocol.APP, 'Mobile app punches'),
           'WEB': (Device.Vendor.KIOSK, Device.Protocol.APP, 'Web punches'),
           'MANUAL': (Device.Vendor.MANUAL, Device.Protocol.MANUAL, 'Manual entries (HR, regularisation, auto clock-out)')}


def device_for(company_id: str, serial: str, vendor: str, protocol: str, name: str) -> dict:
    """The company's pseudo device with this serial, created on first use (INSERT ... ON CONFLICT DO NOTHING)."""
    d, _ = Device.objects.get_or_create(serial_no=serial, defaults={
        'company_id': company_id, 'vendor': vendor, 'protocol': protocol, 'name': name, 'status': Device.Status.ACTIVE})
    return {'id': str(d.id), 'serialNo': serial, 'vendor': d.vendor}


def ensure_pseudo_device(company_id: str, kind: str) -> dict:
    """Per-company pseudo device for non-terminal punches, created on first use (serial MOBILE-<code>, WEB-<code>,
    MANUAL-<code>)."""
    code = Company.objects.filter(pk=company_id).values_list('code', flat=True).first()
    if code is None:
        raise RuntimeError('company not found')
    return device_for(str(company_id), f'{kind}-{code}', *_PSEUDO[kind])


def npt_local_time(ms: int | float) -> str:
    """Local NPT 'YYYY-MM-DD HH:MM:SS' for the device_local_time column."""
    return from_ms(ms).astimezone(NPT).strftime('%Y-%m-%d %H:%M:%S')


def npt_hm(d) -> str:
    """'14:05' in Nepal time."""
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace('Z', '+00:00'))
    return d.astimezone(NPT).strftime('%H:%M')


def round_half_up(x: float) -> int:
    """Whole metres the way the TS code rounded distances and accuracies (halves up; the values are never negative)."""
    return int(Decimal(repr(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- geo.ts

def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


NO_GEO = {'inside': None, 'locationId': None, 'distanceM': None, 'locationName': None}


def geofence(lat: float, lng: float, accuracy_m: float, locations: list[dict]) -> dict:
    """Nearest configured location and whether the point is inside its radius (accuracy widens the fence a little)."""
    if not locations:
        return dict(NO_GEO)
    loc, d = min(((x, distance_m(lat, lng, float(x['lat']), float(x['lng']))) for x in locations), key=lambda p: p[1])
    tolerance = min(accuracy_m, 100)
    return {'inside': d <= loc['radius_m'] + tolerance, 'locationId': str(loc['id']), 'distanceM': round_half_up(d),
            'locationName': loc['name']}


def locations_for(company_id: str, employee_id: str, branch_id) -> QuerySet:
    """Active locations that apply to the employee: the ones tied to them, else their branch's and company-wide ones."""
    qs = Location.objects.for_company(company_id).filter(active=True)
    tied = LocationAssignment.objects.for_company(company_id).filter(employee_id=employee_id)
    if tied.exists():
        return qs.filter(id__in=tied.values('location_id'))
    return qs.filter(Q(branch__isnull=True) | Q(branch_id=branch_id))


# ---------------------------------------------------------------- photo.ts

@dataclass(frozen=True)
class ParsedPhoto:
    mime: str
    data: bytes


_DATA_URL = re.compile(r'^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$')


def parse_photo(data_url: str) -> ParsedPhoto:
    """Decode a `data:image/...;base64,` photo from the clock-in screen, checking type, signature and size."""
    m = _DATA_URL.match(data_url.strip())
    if not m:
        raise BadRequestError('The photo must be a JPEG, PNG or WebP image.')
    mime, raw = m.group(1), m.group(2)
    try:
        data = base64.b64decode(raw + '=' * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        data = b''
    if len(data) == 0:
        raise BadRequestError('The photo is empty. Take it again.')
    if len(data) > PHOTO_MAX_BYTES:
        raise BadRequestError('The photo is too large (max 300 KB).')
    if not has_signature(mime, data):
        raise BadRequestError('The photo could not be read. Take it again.')
    return ParsedPhoto(mime, data)


def photo_aad(employee_id: str, punch_id: str) -> str:
    return f'punch_photo:{employee_id}:{punch_id}'


def store_punch_photo(*, photo_id: str, company_id: str, employee_id: str, punch: PunchEvent, photo: ParsedPhoto) -> None:
    """The encrypted photo row for a punch (same table and retention for home, kiosk and field punches)."""
    PunchPhoto.objects.create(
        id=photo_id, company_id=company_id, employee_id=employee_id, punch_id=punch.id, punched_at=punch.punched_at,
        local_date=punch.local_date, direction=punch.direction, mime=photo.mime, bytes=len(photo.data),
        image_enc=field_cipher().encrypt(base64.b64encode(photo.data).decode(), photo_aad(employee_id, str(punch.id))))


# ---------------------------------------------------------------- shared helpers

def is_duplicate(e: IntegrityError) -> bool:
    """A unique (or exclusion) violation: the row is already there, which ON CONFLICT DO NOTHING ignored in the TS code.
    Foreign-key and check violations are real errors."""
    return getattr(e.__cause__, 'sqlstate', None) in (UNIQUE_VIOLATION, EXCLUSION_VIOLATION)


def insert_punch(**fields) -> PunchEvent | None:
    """Insert a punch_event, or None when its idempotency key (with punched_at) is already stored (INSERT ... ON
    CONFLICT DO NOTHING). The savepoint keeps the caller's transaction usable after a duplicate."""
    try:
        with transaction.atomic():
            return PunchEvent.objects.create(**fields)
    except IntegrityError as e:
        if is_duplicate(e):
            return None
        raise


def visible_to(user: AuthUser, employee_field: str = 'employee_id', date_field: str = 'local_date') -> Q:
    """Rows a person may see: HR (attendance) everyone, a manager the people they manage on that day and themselves,
    everyone else only themselves."""
    if has_module(user, 'attendance'):
        return Q()
    q = Q(**{employee_field: user.employee_id}) if user.employee_id else Q(**{f'{employee_field}__in': []})
    if has_role(user, 'manager') and user.employee_id:
        q |= Q(manages(user.employee_id, F(employee_field), F(date_field)))
    return q


def current_branch(on_date=None, employee_ref=OuterRef('pk')) -> Subquery:
    """The outer employee's branch on a Nepal date (today by default)."""
    return Subquery(EmployeeAssignment.objects.filter(employee_id=employee_ref, valid__contains=on_date or npt_today())
                    .values('branch_id')[:1])


def _not_linked(full: bool = True) -> BadRequestError:
    return BadRequestError('Your account is not linked to an employee record. Ask HR to link it.' if full
                           else 'Your account is not linked to an employee record.')


def _parse_instant_ms(s: str) -> int:
    return to_ms(datetime.fromisoformat(s.replace('Z', '+00:00')))


def _punch_row(p: PunchEvent) -> dict:
    return {'id': str(p.id), 'punched_at': p.punched_at, 'local_date': str(p.local_date), 'direction': p.direction}


# ---------------------------------------------------------------- POST /punch

def punch(user: AuthUser, body: dict, *, device_token: str | None = None, ip: str | None = None) -> tuple[int, dict]:
    """Browser or phone clock-in/out with location, work-from-home photo and the company's clock-in rules."""
    from hajiri.attendance_tools.services import assert_clock_in_allowed
    from hajiri.field.services import field_policy
    from hajiri.relations.services import assert_registered_device

    employee_id = user.employee_id
    if not employee_id:
        raise _not_linked()
    b = validate(PunchIn, body)
    if b.get('mocked'):
        raise BadRequestError('Punch not saved. Your phone reported a fake location. Turn off mock location and try again.')
    photo = parse_photo(b['selfie']) if b.get('selfie') else None

    server_now = to_ms(_now())
    clock_skew_ms = _parse_instant_ms(b['clientTime']) - server_now
    flags: list[str] = []
    at = server_now
    if abs(clock_skew_ms) > CLOCK_TOLERANCE_MS:
        flags.append('ClockSkew')

    company_id = company_id_of(user)
    e = (Employee.objects.for_company(company_id).filter(pk=employee_id, status=Employee.Status.ACTIVE)
         .annotate(branch_id=current_branch()).values('emp_code', 'branch_id', 'work_from_home', 'field_work').first())
    if not e:
        raise NotFoundError('employee not found')
    assert_registered_device(company_id, employee_id, device_token)

    geo = dict(NO_GEO)
    geofence_mode = field_policy(company_id)['geofenceMode']
    if geofence_mode == 'off':
        pass
    elif b.get('lat') is not None and b.get('lng') is not None:
        locs = list(locations_for(company_id, employee_id, e['branch_id']).values('id', 'name', 'lat', 'lng', 'radius_m'))
        geo = geofence(b['lat'], b['lng'], b.get('accuracyM', 50), locs)
    elif locations_for(company_id, employee_id, e['branch_id']).exists():
        geo = {**NO_GEO, 'inside': False}
        flags.append('NoLocation')

    remote = False
    if geo['inside'] is not True:
        remote = bool(e['work_from_home']) or RegularisationRequest.objects.filter(
            employee_id=employee_id, kind=RegularisationRequest.Kind.WFH, status=RegularisationRequest.Status.APPROVED,
            local_date=local_date_of(at)).exists()
    direction = b['direction']
    if remote:
        flags.append('Remote')
        policy = attendance_policy(company_id)
        if not photo and direction == PunchEvent.Direction.IN and policy.get('wfhPhotoRequired') is not False:
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
    existing = PunchEvent.objects.filter(idempotency_key=key).values('id', 'punched_at', 'local_date').first()
    if existing:
        d = existing['local_date'].isoformat()
        day = list_days(employee_id, d, d)
        return 200, {'duplicate': True, 'punch': {'id': str(existing['id']), 'punched_at': existing['punched_at'], 'local_date': d},
                     'day': day[0] if day else None}
    assert_clock_in_allowed(company_id=company_id, employee_id=employee_id, at_ms=at, direction=direction)
    photo_id = str(uuid.uuid4()) if remote and photo else None
    raw = {'lat': b.get('lat'), 'lng': b.get('lng'), 'accuracyM': b.get('accuracyM'), 'inside': geo['inside'], 'remote': remote,
           'locationId': geo['locationId'], 'distanceM': geo['distanceM'], 'clientTime': b['clientTime'],
           'clockSkewMs': clock_skew_ms, 'reason': b.get('reason'), 'flags': flags,
           'selfie': 'stored' if photo_id else ('discarded' if photo else None), 'photoId': photo_id, 'ip': ip}
    with transaction.atomic():
        ins = PunchEvent.objects.create(
            device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'], device_user_pin=e['emp_code'],
            employee_id=employee_id, punched_at=from_ms(at), device_local_time=npt_local_time(at), local_date=local_date_of(at),
            direction=direction, verify_mode=b['source'], source=b['source'], raw=raw, idempotency_key=key)
        if photo_id and photo:
            store_punch_photo(photo_id=photo_id, company_id=company_id, employee_id=employee_id, punch=ins, photo=photo)
    result = recompute_for_punch(employee_id, at, str(ins.id))
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
    stored = PunchPhoto.objects.filter(punch_id=OuterRef('id'), image_enc__isnull=False)
    punches = list(live_punches().for_company(user.company_id).filter(employee_id=employee_id, local_date=d)
                   .annotate(remote=Coalesce(Cast(KT('raw__remote'), BooleanField()), Value(False)),
                             photo_id=Subquery(stored.values('id')[:1]))
                   .order_by('punched_at')
                   .values('id', 'punched_at', 'direction', 'verify_mode', 'source', 'device_sn', 'remote', 'photo_id'))
    return {'date': d, 'day': days[0] if days else None, 'punches': plain(punches)}


# ---------------------------------------------------------------- GET /punch/remote

def check_range(from_: str, to: str, max_days: int, too_long: str) -> None:
    if to < from_:
        raise BadRequestError('"to" is before "from"')
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > max_days:
        raise BadRequestError(too_long)


def remote(user: AuthUser, query: dict) -> dict:
    """Clock-ins from home with their photos. HR sees the company, a manager their reports and themselves, everyone else
    only their own. Exact coordinates are never returned."""
    q = plain(validate(RangeIn, query))
    check_range(q['from'], q['to'], 92, 'pick a range of 3 months or less')
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    photos = PunchPhoto.objects.filter(punch_id=OuterRef('id'))
    qs = (live_punches().for_company(company_id_of(user))
          .filter(local_date__range=(q['from'], q['to']))
          .annotate(remote_text=KT('raw__remote'), lat_text=KT('raw__lat'))
          .filter(visible_to(user), remote_text='true'))
    if q.get('employeeId'):
        qs = qs.filter(employee_id=q['employeeId'])
    out = list(qs.annotate(
        punch_id=F('id'), emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
        designation=F('employee__designation'), photo_id=Subquery(photos.values('id')[:1]),
        photo_purged=Exists(photos.filter(purged_at__isnull=False)),
        location_shared=ExpressionWrapper(Q(lat_text__isnull=False), output_field=BooleanField()),
        accuracy_m=Cast(KT('raw__accuracyM'), FloatField()), reason=KT('raw__reason'), flags=KeyTransform('flags', 'raw'),
    ).order_by('-punched_at').values(
        'punch_id', 'employee_id', 'emp_code', 'name_en', 'designation', 'punched_at', 'local_date', 'direction', 'photo_id',
        'photo_purged', 'location_shared', 'accuracy_m', 'reason', 'flags')[:1000])
    for r in out:
        r['local_date'] = r['local_date'].isoformat()
    return {'punches': plain(out)}


# ---------------------------------------------------------------- GET /punch/photos/:id

def photo(user: AuthUser, photo_id: str) -> dict:
    """The photo itself, for the employee, their manager and HR. Views by anyone other than the employee are audited."""
    pid = parse_uuid(photo_id)
    row = (PunchPhoto.objects.for_company(user.company_id).filter(pk=pid)
           .values('employee_id', 'punch_id', 'mime', 'image_enc').first())
    if not row:
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

def require_module(user: AuthUser, module: str) -> None:
    if not has_module(user, module):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES[module])}")


def manual(user: AuthUser, body: dict) -> tuple[int, dict]:
    """HR manual entry with a reason; audited; day recomputed."""
    require_module(user, 'attendance')
    b = plain(validate(ManualIn, body))
    if b['employeeId'] == user.employee_id and not has_role(user, 'admin'):
        raise ForbiddenError('someone else must add punches to your own attendance')
    company_id = company_id_of(user)
    emp_code = Employee.objects.for_company(company_id).filter(pk=b['employeeId']).values_list('emp_code', flat=True).first()
    if emp_code is None:
        raise NotFoundError('employee not found')
    if is_attendance_locked(b['employeeId'], b['localDate']):
        raise ConflictError('this day is locked by payroll; raise a supplementary correction instead')
    at = local_time_ms(b['localDate'], b['time'])
    with transaction.atomic():
        device = ensure_pseudo_device(company_id, 'MANUAL')
        ins = insert_punch(
            device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'], device_user_pin=emp_code,
            employee_id=b['employeeId'], punched_at=from_ms(at), device_local_time=npt_local_time(at), local_date=b['localDate'],
            direction=b['direction'], verify_mode='manual', source='manual', raw={'reason': b['reason'], 'enteredBy': user.id},
            idempotency_key=sha256_hex(f"manual|{b['employeeId']}|{b['localDate']}|{b['time']}|{b['direction']}"))
        if ins is None:
            return 200, {'duplicate': True}
        audit('manual_punch', 'punch_event', actor_id=user.id, row_id=str(ins.id), after=b)
    result = recompute_for_punch(b['employeeId'], at, str(ins.id))
    return 201, {'duplicate': False, 'punchId': str(ins.id), 'day': result.day.to_dict()}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
