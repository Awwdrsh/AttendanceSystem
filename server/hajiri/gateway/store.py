"""Gateway storage: the Store contract, an in-memory store (tests, simulator, no-database demo) and the PostgreSQL store
that shares the API's schema through the Django ORM. Port of apps/gateway/src/store/{types,memory,postgres}.ts.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Case, F, TextField, Value, When
from django.db.models.functions import Now
from psycopg import errors as psycopg_errors

from hajiri.db.models import BiometricTemplate, ConsentRecord, Device, DeviceCommand, DeviceUserMap, PunchEvent, RecomputeQueue

from .jsutil import dt_from_ms, ms_from_dt
from .punch import NormalizedPunch

NEAR_DUPLICATE_WINDOW_MS = 5_000
DEFAULT_MAX_PENDING = 50
COMMAND_ACK_TIMEOUT_MS = 10 * 60_000
COMMAND_MAX_ATTEMPTS = 2


class CommandRefused(ValueError):
    """A device command that may not be queued (Error in the TS)."""


@dataclass
class DeviceRecord:
    id: str
    serial_no: str
    vendor: str = 'zkteco'
    protocol: str = 'adms'
    status: str = 'active'            # pending | active | disabled
    tz_offset_min: int = 345
    attlog_stamp: str = '0'
    oplog_stamp: str = '0'
    #: sha256 hex of a per-device shared secret; when set, non-ADMS devices must present it (plan §12.5).
    comm_key_hash: str | None = None
    # DeviceTouch fields (only the in-memory store keeps them on the record)
    last_seen_at: datetime | None = None
    last_handshake_at: datetime | None = None
    push_version: str | None = None
    fw_version: str | None = None
    clock_drift_sec: int | None = None
    #: The company that owns the terminal (None until HR approves it).
    company_id: str | None = None


TOUCH_FIELDS = ('last_seen_at', 'last_handshake_at', 'push_version', 'fw_version', 'attlog_stamp', 'oplog_stamp',
                'clock_drift_sec')


@dataclass
class TemplateRecord:
    device_id: str
    device_user_pin: str
    employee_id: str | None
    modality: str
    vendor_format: str
    ciphertext: bytes
    key_version: int
    aad_hash: str
    #: The employee's active biometric consent the template was stored under.
    consent_id: str | None = None


# Characters a terminal might ignore but a regex would not see: controls, zero-width and BOM.
_INVISIBLE = re.compile('[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F​-‏⁠﻿]')
_DANGEROUS = re.compile(r'^\s*(CLEAR\s+DATA|CLEAR\s+ALL|UPGRADE|UPDATE\s+FIRMWARE|PUTFILE|GETFILE|RELOAD\s+OPTIONS|'
                        r'SET\s+OPTIONS?\s+WebServerURL|CONTROL\s+DEVICE)', re.IGNORECASE)


def assert_allowed_command(command: str, allow_dangerous: bool = False) -> None:
    """Command allow-list (plan Appendix B, §12.5): SHELL is remote code execution and is never emitted. CLEAR DATA and
    firmware/file transfer verbs wipe or reprogram the terminal and need an explicit opt-in. A command may never contain
    a line break: the poll response is newline-delimited, so an embedded newline would smuggle a second command past
    every check (including the database CHECK constraint, which only sees the first line)."""
    if '\r' in command or '\n' in command:
        raise CommandRefused('commands must be a single line')
    if len(command.encode('utf-16-le')) // 2 > 4096:
        raise CommandRefused('command too long')
    c = _INVISIBLE.sub('', command)
    if not c.strip():
        raise CommandRefused('empty command')
    if re.match(r'\s*SHELL', c, re.IGNORECASE):
        raise CommandRefused('SHELL commands are forbidden')
    if not allow_dangerous and _DANGEROUS.match(c):
        raise CommandRefused('dangerous command refused (set GATEWAY_ALLOW_DANGEROUS_COMMANDS=true to allow)')


@dataclass
class MemoryCommand:
    id: int
    device_id: str
    command: str
    status: str = 'queued'           # queued | sent | acked | failed
    return_code: int | None = None
    attempts: int = 0
    sent_at_ms: int | None = None


def _now_dt() -> datetime:
    from hajiri.core.clock import now
    return now()


class MemoryStore:
    """In-memory store for tests and simulator runs."""

    def __init__(self, max_pending_devices: int | None = None, allow_dangerous_commands: bool = False):
        self.max_pending_devices = max_pending_devices
        self.allow_dangerous_commands = allow_dangerous_commands
        self.devices: dict[str, DeviceRecord] = {}
        self.punches: list[NormalizedPunch] = []
        self.templates: list[TemplateRecord] = []
        self.recompute_queue: list[dict] = []
        self.mappings: dict[str, str] = {}      # f'{device_id}:{pin}' -> employee_id
        self.commands: list[MemoryCommand] = []
        self.consents: dict[str, str] = {}      # f'{employee_id}:{modality}' -> consent id
        self._next_cmd = 1

    def add_device(self, serial_no: str, **kw) -> DeviceRecord:
        rec = DeviceRecord(id=kw.pop('id', None) or str(uuid.uuid4()), serial_no=serial_no, **kw)
        self.devices[serial_no] = rec
        return rec

    def get_device(self, serial_no: str) -> DeviceRecord | None:
        return self.devices.get(serial_no)

    def register_pending(self, serial_no: str, *, vendor: str, protocol: str, push_version: str | None = None):
        existing = self.devices.get(serial_no)
        if existing:
            return existing
        pending = sum(1 for d in self.devices.values() if d.status == 'pending')
        if pending >= (self.max_pending_devices or DEFAULT_MAX_PENDING):
            return None
        return self.add_device(serial_no, vendor=vendor, protocol=protocol, status='pending', push_version=push_version)

    def touch(self, serial_no: str, **patch) -> None:
        d = self.devices.get(serial_no)
        if d:
            for k, v in patch.items():
                if v is not None:
                    setattr(d, k, v)

    def resolve_employee(self, device_id: str, pin: str, local_date: str | None = None) -> str | None:
        return self.mappings.get(f'{device_id}:{pin}')

    def insert_punch(self, p: NormalizedPunch) -> str:
        if any(q.idempotency_key == p.idempotency_key for q in self.punches):
            return 'duplicate'
        if any(q.device_id == p.device_id and q.device_user_pin == p.device_user_pin and q.verify_mode == p.verify_mode
               and abs(q.punched_at_ms - p.punched_at_ms) <= NEAR_DUPLICATE_WINDOW_MS for q in self.punches):
            return 'near_duplicate'
        self.punches.append(p)
        return 'inserted'

    def enqueue_recompute(self, employee_id: str, local_date: str, reason: str) -> None:
        if not any(q['employeeId'] == employee_id and q['localDate'] == local_date for q in self.recompute_queue):
            self.recompute_queue.append({'employeeId': employee_id, 'localDate': local_date, 'reason': reason})

    def insert_template(self, t: TemplateRecord) -> None:
        self.templates.append(t)

    def grant_consent(self, employee_id: str, modality: str) -> str:
        cid = str(uuid.uuid4())
        self.consents[f'{employee_id}:{modality}'] = cid
        return cid

    def revoke_consent(self, employee_id: str, modality: str) -> None:
        self.consents.pop(f'{employee_id}:{modality}', None)

    def active_consent_id(self, employee_id: str, modality: str) -> str | None:
        return self.consents.get(f'{employee_id}:{modality}')

    def enqueue_command(self, device_id: str, command: str) -> int:
        assert_allowed_command(command, self.allow_dangerous_commands)
        cid = self._next_cmd
        self._next_cmd += 1
        self.commands.append(MemoryCommand(id=cid, device_id=device_id, command=command))
        return cid

    def pop_queued_commands(self, device_id: str, now: datetime | None = None) -> list[dict]:
        now_ms = ms_from_dt(now or _now_dt())
        for c in self.commands:
            if (c.device_id == device_id and c.status == 'sent' and c.sent_at_ms is not None
                    and now_ms - c.sent_at_ms > COMMAND_ACK_TIMEOUT_MS):
                c.status = 'queued' if c.attempts < COMMAND_MAX_ATTEMPTS else 'failed'
        q = [c for c in self.commands if c.device_id == device_id and c.status == 'queued']
        for c in q:
            c.status, c.sent_at_ms, c.attempts = 'sent', now_ms, c.attempts + 1
        return [{'id': c.id, 'command': c.command} for c in q]

    def ack_command(self, device_id: str, id: int, return_code: int) -> None:
        c = next((x for x in self.commands if x.id == id and x.device_id == device_id), None)
        if c and c.status == 'sent':
            c.status = 'acked' if return_code >= 0 else 'failed'
            c.return_code = return_code

    def list_devices(self, status: str | None = None, protocol: str | None = None,
                     company_id: str | None = None) -> list[DeviceRecord]:
        return [d for d in self.devices.values() if (not status or d.status == status)
                and (not protocol or d.protocol == protocol) and (not company_id or d.company_id == company_id)]

    def count_punches(self, device_id: str, local_date: str) -> int:
        return sum(1 for p in self.punches if p.device_id == device_id and p.local_date == local_date)


_COLS = ('id', 'company_id', 'serial_no', 'vendor', 'protocol', 'status', 'tz_offset_min', 'attlog_stamp', 'oplog_stamp',
         'comm_key_hash')


def _rec(r: dict) -> DeviceRecord:
    return DeviceRecord(id=str(r['id']), serial_no=r['serial_no'], vendor=r['vendor'], protocol=r['protocol'],
                        status=r['status'], tz_offset_min=r['tz_offset_min'], attlog_stamp=r['attlog_stamp'],
                        oplog_stamp=r['oplog_stamp'], comm_key_hash=r['comm_key_hash'],
                        company_id=str(r['company_id']) if r['company_id'] else None)


def _is_unique_violation(e: IntegrityError) -> bool:
    return isinstance(e.__cause__, psycopg_errors.UniqueViolation)


class PostgresStore:
    """Shares the schema owned by the API, through the Django ORM (one connection per server thread).

    Devices belong to a company (device.company_id, set when HR approves the terminal). A punch or template is mapped
    only to an employee of the device's own company: a PIN mapping that points at another company's employee is
    ignored, so a terminal of one company of a group can never record attendance for a sister company."""

    def __init__(self, max_pending_devices: int | None = None, allow_dangerous_commands: bool = False, using: str = 'default'):
        self.max_pending_devices = max_pending_devices
        self.allow_dangerous_commands = allow_dangerous_commands
        self.using = using

    def _qs(self, model):
        return model.objects.db_manager(self.using).all()

    def get_device(self, serial_no: str) -> DeviceRecord | None:
        r = self._qs(Device).filter(serial_no=serial_no).values(*_COLS).first()
        return _rec(r) if r else None

    def register_pending(self, serial_no: str, *, vendor: str, protocol: str, push_version: str | None = None):
        existing = self.get_device(serial_no)
        if existing:
            return existing
        n = self._qs(Device).filter(status=Device.Status.PENDING).count()
        if n >= (self.max_pending_devices or DEFAULT_MAX_PENDING):
            return None
        try:
            with transaction.atomic(using=self.using):
                self._qs(Device).create(serial_no=serial_no, vendor=vendor, protocol=protocol, status=Device.Status.PENDING,
                                        push_version=push_version, last_seen_at=Now())
        except IntegrityError as e:
            if not _is_unique_violation(e):
                raise
            # Registered by a concurrent request from the same terminal: only mark it seen.
            self._qs(Device).filter(serial_no=serial_no).update(last_seen_at=Now())
        return self.get_device(serial_no)

    def touch(self, serial_no: str, *, last_seen_at=None, last_handshake_at=None, push_version=None, fw_version=None,
              attlog_stamp=None, oplog_stamp=None, clock_drift_sec=None) -> None:
        patch = {k: v for k, v in dict(
            last_seen_at=last_seen_at, last_handshake_at=last_handshake_at, push_version=push_version,
            fw_version=fw_version, attlog_stamp=attlog_stamp, oplog_stamp=oplog_stamp,
            clock_drift_sec=clock_drift_sec).items() if v is not None}
        if patch:
            self._qs(Device).filter(serial_no=serial_no).update(**patch)

    def resolve_employee(self, device_id: str, pin: str, local_date: str) -> str | None:
        # Scoped to the device's company: the mapped employee must belong to the company that owns the terminal.
        emp = (self._qs(DeviceUserMap)
               .filter(device_id=device_id, device_user_pin=pin, valid__contains=date.fromisoformat(local_date),
                       employee__company_id=F('device__company_id'))
               .values_list('employee_id', flat=True).first())
        return str(emp) if emp else None

    def insert_punch(self, p: NormalizedPunch) -> str:
        at = dt_from_ms(p.punched_at_ms)
        window = timedelta(milliseconds=NEAR_DUPLICATE_WINDOW_MS)
        near = (self._qs(PunchEvent)
                .filter(device_id=p.device_id, device_user_pin=p.device_user_pin, verify_mode=p.verify_mode,
                        punched_at__range=(at - window, at + window))
                .exclude(idempotency_key=p.idempotency_key).exists())
        if near:
            return 'near_duplicate'
        try:
            with transaction.atomic(using=self.using):
                self._qs(PunchEvent).create(
                    device_id=p.device_id, device_sn=p.device_sn, vendor=p.vendor, device_user_pin=p.device_user_pin,
                    employee_id=p.employee_id, punched_at=at, device_local_time=p.device_local_time,
                    local_date=date.fromisoformat(p.local_date), direction=p.direction, verify_mode=p.verify_mode,
                    source=p.source, raw=p.raw, gateway_signature=p.gateway_signature, idempotency_key=p.idempotency_key)
        except IntegrityError as e:
            # The (idempotency_key, punched_at) unique index: the same record was already stored.
            if not _is_unique_violation(e):
                raise
            return 'duplicate'
        return 'inserted'

    def enqueue_recompute(self, employee_id: str, local_date: str, reason: str) -> None:
        self._qs(RecomputeQueue).bulk_create(
            [RecomputeQueue(employee_id=employee_id, local_date=date.fromisoformat(local_date), reason=reason,
                            enqueued_at=Now())],
            update_conflicts=True, unique_fields=['employee', 'local_date'], update_fields=['enqueued_at', 'reason'])

    def insert_template(self, t: TemplateRecord) -> None:
        self._qs(BiometricTemplate).create(
            employee_id=t.employee_id, device_user_pin=t.device_user_pin, modality=t.modality,
            vendor_format=t.vendor_format, ciphertext=t.ciphertext, key_version=t.key_version, aad_hash=t.aad_hash,
            enrolled_on_device_id=t.device_id, consent_id=t.consent_id)

    def active_consent_id(self, employee_id: str, modality: str) -> str | None:
        cid = (self._qs(ConsentRecord).filter(employee_id=employee_id, modality=modality, revoked_at__isnull=True)
               .order_by('-granted_at').values_list('id', flat=True).first())
        return str(cid) if cid else None

    def enqueue_command(self, device_id: str, command: str) -> int:
        assert_allowed_command(command, self.allow_dangerous_commands)
        return int(self._qs(DeviceCommand).create(device_id=device_id, command=command).id)

    def pop_queued_commands(self, device_id: str, now: datetime | None = None) -> list[dict]:
        now = now or _now_dt()
        S = DeviceCommand.Status
        commands = self._qs(DeviceCommand).filter(device_id=device_id)
        with transaction.atomic(using=self.using):
            # Resend policy: sent-but-unacknowledged commands older than the ack timeout go back to the queue once, then fail.
            commands.filter(status=S.SENT, sent_at__lt=now - timedelta(milliseconds=COMMAND_ACK_TIMEOUT_MS)).update(
                status=Case(When(attempts__lt=COMMAND_MAX_ATTEMPTS, then=Value(S.QUEUED)), default=Value(S.FAILED),
                            output_field=TextField()))
            # Two pollers of the same terminal never get the same command twice.
            queued = list(commands.filter(status=S.QUEUED).order_by('id').select_for_update(skip_locked=True)
                          .values_list('id', 'command'))
            if queued:
                commands.filter(id__in=[i for i, _ in queued]).update(status=S.SENT, sent_at=now,
                                                                       attempts=F('attempts') + 1)
        return [{'id': int(i), 'command': c} for i, c in queued]

    def ack_command(self, device_id: str, id: int, return_code: int) -> None:
        S = DeviceCommand.Status
        self._qs(DeviceCommand).filter(id=id, device_id=device_id, status=S.SENT).update(
            status=S.ACKED if return_code >= 0 else S.FAILED, return_code=return_code, acked_at=Now())

    def list_devices(self, status: str | None = None, protocol: str | None = None,
                     company_id: str | None = None) -> list[DeviceRecord]:
        """Every device of the deployment, or with `company_id` only that company's (Device.objects.for_company)."""
        qs = self._qs(Device).for_company(company_id) if company_id else self._qs(Device)
        if status:
            qs = qs.filter(status=status)
        if protocol:
            qs = qs.filter(protocol=protocol)
        return [_rec(r) for r in qs.order_by('serial_no').values(*_COLS)]

    def count_punches(self, device_id: str, local_date: str) -> int:
        return self._qs(PunchEvent).filter(device_id=device_id, local_date=date.fromisoformat(local_date)).count()
