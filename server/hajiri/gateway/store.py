"""Gateway storage: the Store contract, an in-memory store (tests, simulator, no-database demo) and the PostgreSQL store
that shares the API's schema. Port of apps/gateway/src/store/{types,memory,postgres}.ts.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

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

    def list_devices(self, status: str | None = None, protocol: str | None = None) -> list[DeviceRecord]:
        return [d for d in self.devices.values() if (not status or d.status == status) and (not protocol or d.protocol == protocol)]

    def count_punches(self, device_id: str, local_date: str) -> int:
        return sum(1 for p in self.punches if p.device_id == device_id and p.local_date == local_date)


_COLS = 'id, serial_no, vendor, protocol, status, tz_offset_min, attlog_stamp, oplog_stamp, comm_key_hash'


def _rec(r: dict) -> DeviceRecord:
    return DeviceRecord(id=str(r['id']), serial_no=r['serial_no'], vendor=r['vendor'], protocol=r['protocol'],
                        status=r['status'], tz_offset_min=r['tz_offset_min'], attlog_stamp=r['attlog_stamp'],
                        oplog_stamp=r['oplog_stamp'], comm_key_hash=r['comm_key_hash'])


class PostgresStore:
    """Shares the schema owned by the API, through Django's database connection (one per server thread)."""

    def __init__(self, max_pending_devices: int | None = None, allow_dangerous_commands: bool = False, using: str = 'default'):
        self.max_pending_devices = max_pending_devices
        self.allow_dangerous_commands = allow_dangerous_commands
        self.using = using

    # Raw SQL with json/jsonb decoding, on the chosen connection.
    def _rows(self, sql: str, params=()) -> list[dict]:
        from django.db import connections
        with connections[self.using].cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            names = [c.name for c in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def _execute(self, sql: str, params=()) -> int:
        from django.db import connections
        with connections[self.using].cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def get_device(self, serial_no: str) -> DeviceRecord | None:
        r = self._rows(f'SELECT {_COLS} FROM device WHERE serial_no = %s', [serial_no])
        return _rec(r[0]) if r else None

    def register_pending(self, serial_no: str, *, vendor: str, protocol: str, push_version: str | None = None):
        existing = self.get_device(serial_no)
        if existing:
            return existing
        n = self._rows("SELECT count(*)::int AS n FROM device WHERE status = 'pending'")[0]['n']
        if n >= (self.max_pending_devices or DEFAULT_MAX_PENDING):
            return None
        r = self._rows(
            f"""INSERT INTO device (serial_no, vendor, protocol, status, push_version, last_seen_at)
                VALUES (%s, %s, %s, 'pending', %s, now())
                ON CONFLICT (serial_no) DO UPDATE SET last_seen_at = now() RETURNING {_COLS}""",
            [serial_no, vendor, protocol, push_version])
        return _rec(r[0])

    def touch(self, serial_no: str, *, last_seen_at=None, last_handshake_at=None, push_version=None, fw_version=None,
              attlog_stamp=None, oplog_stamp=None, clock_drift_sec=None) -> None:
        self._execute(
            """UPDATE device SET last_seen_at = coalesce(%s::timestamptz, last_seen_at),
                 last_handshake_at = coalesce(%s::timestamptz, last_handshake_at),
                 push_version = coalesce(%s::text, push_version), fw_version = coalesce(%s::text, fw_version),
                 attlog_stamp = coalesce(%s::text, attlog_stamp), oplog_stamp = coalesce(%s::text, oplog_stamp),
                 clock_drift_sec = coalesce(%s::int, clock_drift_sec)
               WHERE serial_no = %s""",
            [last_seen_at, last_handshake_at, push_version, fw_version, attlog_stamp, oplog_stamp, clock_drift_sec, serial_no])

    def resolve_employee(self, device_id: str, pin: str, local_date: str) -> str | None:
        r = self._rows('SELECT employee_id FROM device_user_map WHERE device_id = %s AND device_user_pin = %s AND valid @> %s::date',
                       [device_id, pin, local_date])
        return str(r[0]['employee_id']) if r else None

    def insert_punch(self, p: NormalizedPunch) -> str:
        at = dt_from_ms(p.punched_at_ms)
        near = self._rows(
            """SELECT 1 FROM punch_event WHERE device_id = %s AND device_user_pin = %s AND verify_mode = %s
                 AND punched_at BETWEEN %s::timestamptz - %s::interval AND %s::timestamptz + %s::interval
                 AND idempotency_key <> %s LIMIT 1""",
            [p.device_id, p.device_user_pin, p.verify_mode, at, f'{NEAR_DUPLICATE_WINDOW_MS} milliseconds', at,
             f'{NEAR_DUPLICATE_WINDOW_MS} milliseconds', p.idempotency_key])
        if near:
            return 'near_duplicate'
        n = self._execute(
            """INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                 local_date, direction, verify_mode, source, raw, gateway_signature, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (idempotency_key, punched_at) DO NOTHING""",
            [p.device_id, p.device_sn, p.vendor, p.device_user_pin, p.employee_id, at, p.device_local_time, p.local_date,
             p.direction, p.verify_mode, p.source, json.dumps(p.raw, ensure_ascii=False), p.gateway_signature, p.idempotency_key])
        return 'inserted' if n > 0 else 'duplicate'

    def enqueue_recompute(self, employee_id: str, local_date: str, reason: str) -> None:
        self._execute(
            """INSERT INTO recompute_queue (employee_id, local_date, reason) VALUES (%s, %s::date, %s)
               ON CONFLICT (employee_id, local_date) DO UPDATE SET enqueued_at = now(), reason = EXCLUDED.reason""",
            [employee_id, local_date, reason])

    def insert_template(self, t: TemplateRecord) -> None:
        self._execute(
            """INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version,
                 aad_hash, enrolled_on_device_id, consent_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [t.employee_id, t.device_user_pin, t.modality, t.vendor_format, t.ciphertext, t.key_version, t.aad_hash,
             t.device_id, t.consent_id])

    def active_consent_id(self, employee_id: str, modality: str) -> str | None:
        r = self._rows("""SELECT id FROM consent_record WHERE employee_id = %s AND modality = %s AND revoked_at IS NULL
                          ORDER BY granted_at DESC LIMIT 1""", [employee_id, modality])
        return str(r[0]['id']) if r else None

    def enqueue_command(self, device_id: str, command: str) -> int:
        assert_allowed_command(command, self.allow_dangerous_commands)
        return int(self._rows('INSERT INTO device_command (device_id, command) VALUES (%s, %s) RETURNING id', [device_id, command])[0]['id'])

    def pop_queued_commands(self, device_id: str, now: datetime | None = None) -> list[dict]:
        now = now or _now_dt()
        # Resend policy: sent-but-unacknowledged commands older than the ack timeout go back to the queue once, then fail.
        self._execute(
            """UPDATE device_command SET status = CASE WHEN attempts < %s::int THEN 'queued' ELSE 'failed' END
                WHERE device_id = %s AND status = 'sent' AND sent_at < %s::timestamptz - make_interval(secs => %s::int)""",
            [COMMAND_MAX_ATTEMPTS, device_id, now, COMMAND_ACK_TIMEOUT_MS // 1000])
        r = self._rows(
            """UPDATE device_command SET status = 'sent', sent_at = %s::timestamptz, attempts = attempts + 1
                WHERE id IN (SELECT id FROM device_command WHERE device_id = %s AND status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED)
                RETURNING id, command""", [now, device_id])
        return [{'id': int(c['id']), 'command': c['command']} for c in sorted(r, key=lambda c: c['id'])]

    def ack_command(self, device_id: str, id: int, return_code: int) -> None:
        self._execute(
            """UPDATE device_command SET status = CASE WHEN %s::int >= 0 THEN 'acked' ELSE 'failed' END, return_code = %s::int,
                 acked_at = now() WHERE id = %s AND device_id = %s AND status = 'sent'""",
            [return_code, return_code, id, device_id])

    def list_devices(self, status: str | None = None, protocol: str | None = None) -> list[DeviceRecord]:
        r = self._rows(f"""SELECT {_COLS} FROM device WHERE (%s::text IS NULL OR status = %s) AND (%s::text IS NULL OR protocol = %s)
                           ORDER BY serial_no""", [status, status, protocol, protocol])
        return [_rec(x) for x in r]

    def count_punches(self, device_id: str, local_date: str) -> int:
        return self._rows('SELECT count(*)::int AS n FROM punch_event WHERE device_id = %s AND local_date = %s::date',
                          [device_id, local_date])[0]['n']
