"""
Biometric terminals: activation, keys, PIN mappings, unmapped punches and the command queue (port of
apps/api/src/modules/devices/routes.ts). HR and admin only; every query is scoped to the user's active company.
A terminal that has not been claimed yet (company_id NULL, status pending) is visible to every company's HR so it
can be activated; once claimed it belongs to that company only.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, timedelta

from django.db import connection, transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.contrib.postgres.fields import DateRangeField
from django.db.models import (BooleanField, Count, Exists, ExpressionWrapper, F, Func, IntegerField, OuterRef, Q,
                              QuerySet, Subquery, TextField, Value)
from django.db.models.functions import Cast, Coalesce, Now

from hajiri.attendance.service import recompute_day
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Branch, Device, DeviceCommand, DeviceUserMap, Employee, PunchEvent

from .constants import (COMMANDS, OFFLINE_AFTER_SECONDS, PAGE_COMMANDS, PIN_MESSAGE, PIN_RE, PROTOCOL,  # noqa: F401
                        PSEUDO_VENDORS, ROLES, VENDOR)
from .serializers import ActivateIn, CommandIn, MapIn

DEVICE_FIELDS = ('id', 'company_id', 'branch_id', 'serial_no', 'vendor', 'protocol', 'name', 'status', 'fw_version',
                 'push_version', 'tz_offset_min', 'last_handshake_at', 'last_seen_at', 'created_at')


def _require(user: AuthUser) -> str:
    if not has_role(user, *ROLES):
        raise ForbiddenError(f'requires role {" or ".join(ROLES)}')
    return company_id_of(user)


def _id(v) -> str:
    return parse_uuid(str(v))


def device_name(name: str) -> str:
    """Terminal user names travel in a tab-separated, newline-delimited command: strip every control character."""
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', name or '')
    return re.sub(r'\s+', ' ', s).strip()[:24]


def userinfo_command(pin: str, name: str) -> str:
    return f'DATA UPDATE USERINFO PIN={pin}\tName={device_name(name)}\tPri=0'


def live_status(d: dict) -> str:
    """Active devices are shown as online/offline; pending and disabled keep their lifecycle status."""
    if d.get('status') != Device.Status.ACTIVE:
        return d.get('status') or ''
    return 'offline' if d.get('offline') else 'online'


# ---------------------------------------------------------------- querysets

def count_of(qs: QuerySet, group: str) -> Coalesce:
    """A correlated `(SELECT count(*) ...)` for an annotation; `group` is the column the subquery is tied to."""
    sub = qs.order_by().values(group).annotate(n=Count('*')).values('n')
    return Coalesce(Subquery(sub, output_field=IntegerField()), 0)


def offline_q() -> Q:
    return Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=Now() - timedelta(seconds=OFFLINE_AFTER_SECONDS))


def terminals(cid: str) -> QuerySet:
    """Devices of the company plus terminals nobody has claimed yet."""
    return Device.objects.filter(Q(company_id=cid) | Q(company__isnull=True))


def superseded() -> Exists:
    """punch_event is append-only: a punch that a later row supersedes is no longer current."""
    return Exists(PunchEvent.objects.filter(supersedes_id=OuterRef('id')))


def unmapped_punch_queryset(cid: str) -> QuerySet:
    """Current punches from the company's devices whose PIN is not mapped to an employee."""
    return PunchEvent.objects.filter(employee__isnull=True, device__company_id=cid).exclude(superseded())


# ---------------------------------------------------------------- reads

def _devices(cid: str) -> QuerySet:
    today = npt_today()
    mapped = DeviceUserMap.objects.filter(device=OuterRef('pk'), valid__contains=today)
    queued = DeviceCommand.objects.filter(device=OuterRef('pk'), status=DeviceCommand.Status.QUEUED)
    return (terminals(cid)
            .annotate(offline=ExpressionWrapper(offline_q(), output_field=BooleanField()),
                      mapped_users=count_of(mapped, 'device'), queued_commands=count_of(queued, 'device'))
            .values(*DEVICE_FIELDS, 'offline', 'mapped_users', 'queued_commands')
            .order_by('status', '-created_at'))


def list_devices(user: AuthUser) -> list[dict]:
    return list(_devices(_require(user)))


def get_device(user: AuthUser, device_id) -> dict:
    """One device as list_devices shows it (pending devices with no company included), or 404."""
    cid = _require(user)
    d = _devices(cid).filter(pk=_id(device_id)).first()
    if d is None:
        raise NotFoundError('device not found')
    return d


def mappings(user: AuthUser, device_id) -> list[dict]:
    cid = _require(user)
    qs = (DeviceUserMap.objects.filter(device_id=_id(device_id), device__company_id=cid)
          .order_by('device_user_pin', 'valid__startswith')
          .values('device_user_pin', 'employee_id', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                  valid_text=Cast('valid', TextField())))
    return [{'device_user_pin': m['device_user_pin'], 'employee_id': m['employee_id'], 'emp_code': m['emp_code'],
             'name_en': m['name_en'], 'valid': m['valid_text']} for m in qs]


def commands(user: AuthUser, device_id) -> list[dict]:
    cid = _require(user)
    return list(DeviceCommand.objects.filter(device_id=_id(device_id), device__company_id=cid).order_by('-id')
                .values('id', 'command', 'status', 'return_code', 'queued_at', 'sent_at', 'acked_at')[:100])


def unmapped_punches(user: AuthUser) -> list[dict]:
    """Punches from active devices whose PIN is not mapped to an employee (HR queue, plan §5.2)."""
    cid = _require(user)
    same_pin = PunchEvent.objects.filter(device_id=OuterRef('device_id'), device_user_pin=OuterRef('device_user_pin'),
                                         employee__isnull=True)
    return list(unmapped_punch_queryset(cid)
                .annotate(total_for_pin=count_of(same_pin, 'device_id'))
                .order_by('-ingested_at')
                .values('id', 'device_id', 'device_sn', 'device_user_pin', 'punched_at', 'local_date', 'verify_mode',
                        'total_for_pin')[:500])


def branches(user: AuthUser) -> list[dict]:
    cid = _require(user)
    return list(Branch.objects.for_company(cid).order_by('code').values('id', 'code', 'name_en'))


# ---------------------------------------------------------------- lifecycle

def activate(user: AuthUser, device_id, body: dict) -> dict:
    """Approve a pending device for this company (only active devices may push punches)."""
    cid = _require(user)
    did = _id(device_id)
    b = validate(ActivateIn, body)
    if b.get('branchId') and not Branch.objects.for_company(cid).filter(pk=b['branchId']).exists():
        raise NotFoundError('branch not found')
    with transaction.atomic():
        d = terminals(cid).select_for_update().filter(pk=did).first()
        if d is None:
            raise NotFoundError('device not found')
        d.status, d.company_id = Device.Status.ACTIVE, cid
        if b.get('branchId'):
            d.branch_id = b['branchId']
        if b.get('name') is not None:
            d.name = b['name']
        d.save(update_fields=['status', 'company', 'branch', 'name'])
        r = {'id': str(d.id), 'serial_no': d.serial_no, 'status': d.status}
        audit('activate', 'device', actor_id=user.id, row_id=did, after=r)
    return r


def issue_key(user: AuthUser, device_id) -> dict:
    """
    Issue (or rotate) the shared key for a device that can send one, such as a Hikvision terminal. Only the SHA-256
    is stored; the key is shown once and goes into the device's listening-host URL (?key=...). ZKTeco ADMS terminals
    cannot send a key.
    """
    cid = _require(user)
    did = _id(device_id)
    protocol = Device.objects.for_company(cid).filter(pk=did).values_list('protocol', flat=True).first()
    if protocol is None:
        raise NotFoundError('device not found')
    if protocol == Device.Protocol.ADMS:
        raise BadRequestError('ZKTeco ADMS terminals cannot send a device key; keep them on the office network')
    key = secrets.token_urlsafe(24)
    with transaction.atomic():
        Device.objects.filter(pk=did).update(comm_key_hash=hashlib.sha256(key.encode()).hexdigest())
        audit('rotate_key', 'device', actor_id=user.id, row_id=did)
    return {'key': key}


def disable(user: AuthUser, device_id) -> dict:
    cid = _require(user)
    did = _id(device_id)
    with transaction.atomic():
        d = (Device.objects.for_company(cid).exclude(vendor__in=PSEUDO_VENDORS).select_for_update()
             .filter(pk=did).first())
        if d is None:
            raise NotFoundError('device not found')
        d.status = Device.Status.DISABLED
        d.save(update_fields=['status'])
        audit('disable', 'device', actor_id=user.id, row_id=did)
    return {'id': str(d.id), 'serial_no': d.serial_no, 'status': d.status}


# ---------------------------------------------------------------- PIN mapping

def _day(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def _reattributed_key(p: PunchEvent) -> str:
    """The superseding row's idempotency key: unique per original punch, the same for the same punch."""
    return hashlib.sha256(f'{p.idempotency_key}:reattributed:{p.id}'.encode()).hexdigest()


def map_pin(user: AuthUser, device_id, body: dict) -> dict:
    """
    Map a device PIN to an employee from `from` onwards. If the PIN is already mapped to someone else in that range
    the request is refused (409) unless `replace` is set, so a typo cannot silently take over a colleague's punches.
    Every punch for that PIN since `from` (unmapped or attributed to another employee) is re-attributed by inserting
    superseding rows (punch_event is append-only), and the affected days are recomputed for everyone involved.
    """
    cid = _require(user)
    did = _id(device_id)
    b = plain(validate(MapIn, body))
    dev = Device.objects.for_company(cid).filter(pk=did).values('serial_no', 'vendor').first()
    if not dev:
        raise NotFoundError('device not found')
    emp = Employee.objects.for_company(cid).filter(pk=b['employeeId']).values('emp_code', 'name_en').first()
    if not emp:
        raise NotFoundError('employee not found')
    pin, eid, start = b['pin'], b['employeeId'], date.fromisoformat(b['from'])
    pin_maps = DeviceUserMap.objects.filter(device_id=did, device_user_pin=pin)
    with transaction.atomic():
        with connection.cursor() as cur:
            # Raw SQL: a transaction-scoped advisory lock serialises two mappings of the same PIN (no ORM API).
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f'pinmap:{did}:{pin}'])
        holders = list(pin_maps.filter(valid__overlap=DateRange(start, None)).exclude(employee_id=eid)
                       .values('employee_id', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))
                       .order_by('employee_id').distinct())
        if holders and not b['replace']:
            who = ', '.join(f"{h['name_en']} ({h['emp_code']})" for h in holders)
            raise ConflictError(f"PIN {pin} on this device is already mapped to {who}. Confirm the replacement to move "
                                f"their punches since {b['from']} to {emp['name_en']}.")
        # Close any open mapping for this PIN, then open the new one.
        pin_maps.filter(valid__upper_inf=True, valid__startswith__lt=start).update(
            valid=Func(Func(F('valid'), function='lower'), Value(start), function='daterange',
                       output_field=DateRangeField()))
        pin_maps.filter(valid__startswith__gte=start).delete()
        DeviceUserMap.objects.create(device_id=did, device_user_pin=pin, employee_id=eid, valid=DateRange(start, None))
        # Current (not yet superseded) punches for this PIN since `from` that are not already this employee's.
        moving = list(PunchEvent.objects.filter(device_id=did, device_user_pin=pin, local_date__gte=start)
                      .exclude(employee_id=eid).exclude(superseded()).order_by('punched_at', 'id'))
        previous = list(dict.fromkeys((str(p.employee_id), p.local_date.isoformat()) for p in moving if p.employee_id))
        PunchEvent.objects.bulk_create([PunchEvent(
            device_id=p.device_id, device_sn=p.device_sn, vendor=p.vendor, device_user_pin=p.device_user_pin, employee_id=eid,
            punched_at=p.punched_at, device_local_time=p.device_local_time, local_date=p.local_date, direction=p.direction,
            verify_mode=p.verify_mode, source=p.source, raw=p.raw, gateway_signature=p.gateway_signature,
            idempotency_key=_reattributed_key(p), supersedes_id=p.id) for p in moving])
        dates = list(dict.fromkeys(p.local_date.isoformat() for p in moving))
        if b['syncToDevice'] and dev['vendor'] == Device.Vendor.ZKTECO:
            DeviceCommand.objects.create(device_id=did, command=userinfo_command(pin, emp['name_en']))
        audit('map_pin', 'device_user_map', actor_id=user.id, row_id=did,
              after={'pin': pin, 'employeeId': eid, 'from': b['from'], 'reattributed': len(moving),
                     'replaced': [str(h['employee_id']) for h in holders]})

    # Recompute both sides; a morning punch can belong to the previous day's shift.
    work: dict[tuple[str, str], None] = {}
    for d in dates:
        work[(eid, _day(d, -1))] = None
        work[(eid, d)] = None
    for e, d in previous:
        work[(e, _day(d, -1))] = None
        work[(e, d)] = None
    for e, d in work:
        recompute_day(e, d)
    moved_from = list(dict.fromkeys(e for e, _ in previous))
    return {'ok': True, 'reattributed': len(dates), 'dates': dates, 'movedFrom': moved_from}


# ---------------------------------------------------------------- commands

def queue_command(user: AuthUser, device_id, body: dict) -> dict:
    """Queue a command for the terminal (allow-listed verbs only; SHELL is impossible by DB constraint)."""
    cid = _require(user)
    did = _id(device_id)
    b = validate(CommandIn, body)
    if not Device.objects.for_company(cid).filter(pk=did).exists():
        raise NotFoundError('device not found')
    with transaction.atomic():
        c = DeviceCommand.objects.create(device_id=did, command=b['command'])
        audit('device_command', 'device_command', actor_id=user.id, row_id=str(c.id), after=b)
    return {'id': int(c.id)}


def sync_users(user: AuthUser, device_id) -> dict:
    """Push all current PIN mappings to the device as USERINFO updates."""
    cid = _require(user)
    did = _id(device_id)
    found = list(DeviceUserMap.objects.filter(device_id=did, device__company_id=cid, valid__contains=npt_today(),
                                              employee__status=Employee.Status.ACTIVE)
                 .values_list('device_user_pin', 'employee__name_en'))
    with transaction.atomic():
        DeviceCommand.objects.bulk_create([DeviceCommand(device_id=did, command=userinfo_command(pin, name))
                                           for pin, name in found if PIN_RE.match(pin)])
    return {'queued': len(found)}
