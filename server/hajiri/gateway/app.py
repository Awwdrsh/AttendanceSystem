"""Device-facing gateway logic (port of apps/gateway/src/app.ts). The device is always the HTTP client (plan §7.3).

  ZKTeco ADMS/PUSH (Appendix B):
    GET  /iclock/cdata?SN=..&options=all    handshake -> options block (approved devices only)
    POST /iclock/cdata?SN=..&table=ATTLOG   attendance records -> "OK: n"
    POST /iclock/cdata?SN=..&table=OPERLOG  operation log incl. FP/FACE/BIODATA template lines -> "OK: 1"
    GET  /iclock/getrequest?SN=..           command poll / heartbeat -> "C:id:CMD" lines or "OK"
    POST /iclock/devicecmd                  command results
    POST /iclock/fdata?SN=..&table=..       templates / photos
  Hikvision ISAPI listening host (Appendix C):
    POST /hikvision/event?sn=..             JSON or multipart AccessControllerEvent / heartbeat

Framework-free: views.py turns a Django request into a GwRequest and the Response back. Deps are injected, so tests
run the same code against the in-memory store.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field

from .adms import (
    build_options, direction_of, drift_seconds, parse_attlog, parse_device_cmd_results, parse_template_upload,
    verify_mode_of,
)
from .archive import RawArchive, safe_serial
from .hikvision import extract_event_json, parse_hik_event
from .jsutil import iso_ms, js_trim, utc_ms, utc_parts
from .punch import DeviceTimeError, NormalizedPunch, Signer, TemplateCipher, aad_hash, build_punch, template_aad
from .store import DeviceRecord, TemplateRecord

log = logging.getLogger('hajiri.gateway')

TEMPLATE_TABLES = frozenset({'FINGERTMP', 'FACE', 'BIODATA', 'FP', 'ATTPHOTO', 'USERPIC', 'BIOPHOTO'})
#: Punches before 2020 or more than a day in the future are refused (device clock reset or badly wrong).
MIN_PUNCH_MS = utc_ms(2020, 1, 1)
MAX_FUTURE_MS = 24 * 3600_000
DEFAULT_SERVER_VERSION = 'hajiri-gw/0.1.0'


def now_ms() -> int:
    """Date.now(); patched in tests that need a fixed clock."""
    return int(time.time() * 1000)


def affected_dates(punched_at_ms: int, local_date: str) -> list[str]:
    """Days a punch can change: its own date and, for a morning punch (before 12:00 Nepal time), the previous date,
    because a night shift or an overtime punch-out after midnight belongs to the day the shift started."""
    _, _, _, npt_hour = utc_parts(punched_at_ms + 345 * 60_000)
    if npt_hour >= 12:
        return [local_date]
    y, m, d = (int(x) for x in local_date.split('-'))
    return [iso_ms(utc_ms(y, m, d) - 86_400_000)[:10], local_date]


def redact_url(url: str) -> str:
    """Drop secrets from a logged URL: the Hikvision listening-host URL may carry ?key=."""
    return re.sub(r'([?&](?:key|token|password|pwd)=)[^&]*', r'\1[redacted]', url, flags=re.IGNORECASE)


_UNAVAILABLE_CODES = frozenset({'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN', '57P01', '57P02', '57P03',
                                '53300', '08000', '08003', '08006', '08001'})
_UNAVAILABLE_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT})


def is_storage_unavailable(e: BaseException) -> bool:
    """Connection and admin-shutdown errors: the terminal keeps the record buffered and retries (plan §7.7)."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, 'code', None) or getattr(cur, 'sqlstate', None) or getattr(cur, 'pgcode', None)
        if isinstance(code, str) and code in _UNAVAILABLE_CODES:
            return True
        if isinstance(cur, OSError) and cur.errno in _UNAVAILABLE_ERRNOS:
            return True
        msg = str(cur)
        if re.search(r'terminat|connection|timeout', msg, re.IGNORECASE) and not re.search(r'invalid', msg, re.IGNORECASE):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


@dataclass
class GwRequest:
    method: str
    path: str
    url: str                                  # path and query as received (for the request log)
    query: dict[str, list[str]]
    headers: dict[str, str]                   # lower-case names
    body: bytes = b''
    text: str = ''                            # decoded body for the /iclock routes

    def q(self, name: str) -> str | list[str] | None:
        v = self.query.get(name)
        if v is None:
            return None
        return v[0] if len(v) == 1 else list(v)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass
class GwResponse:
    status: int
    body: str | bytes
    content_type: str = 'text/plain; charset=utf-8'


def plain(body: str, status: int = 200) -> GwResponse:
    return GwResponse(status, body)


def _qstr(v) -> str:
    """String(v ?? ''): repeated query keys arrive as a list, which JS joins with commas."""
    if v is None:
        return ''
    return ','.join(v) if isinstance(v, list) else str(v)


def stamp_of(v) -> str | None:
    """ADMS cursors are echoed back in the handshake options block, so only plain digits are accepted (no line injection)."""
    return v if isinstance(v, str) and re.fullmatch(r'[0-9]{1,20}', v) else None


def version_of(v) -> str | None:
    """Firmware version strings are stored and shown to admins: printable and short."""
    return v if isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.() -]{1,32}', v) else None


def serial_of(v) -> str | None:
    """Serial numbers: printable, bounded, no path or log tricks. Returns None when unacceptable."""
    sn = js_trim(_qstr(v))
    ok = sn and len(sn) <= 64 and re.fullmatch(r'[A-Za-z0-9_.:-]+', sn) and safe_serial(sn) != 'unknown'
    return sn if ok else None


@dataclass
class Counters:
    inserted: int = 0
    duplicate: int = 0
    near: int = 0
    unmapped: int = 0
    skewed: int = 0
    malformed: int = 0
    rejected: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _log(level: int, fields: dict, msg: str) -> None:
    if log.isEnabledFor(level):
        log.log(level, '%s %s', msg, json.dumps(fields, default=str, ensure_ascii=False))


@dataclass
class Gateway:
    store: object
    signer: Signer
    server_version: str = DEFAULT_SERVER_VERSION
    #: Template encryption; when absent, templates are discarded (never stored in plaintext).
    cipher: TemplateCipher | None = None
    archive: RawArchive = field(default_factory=lambda: RawArchive(None))
    max_clock_skew_min: int = 10
    #: Request lines (method, redacted URL, status) are logged when true, as pino-http did.
    request_log: bool = True

    @property
    def max_skew_ms(self) -> int:
        return self.max_clock_skew_min * 60_000

    def _touch_now(self):
        from hajiri.core.clock import now
        return now()

    def comm_key_ok(self, device: DeviceRecord, req: GwRequest) -> bool:
        """Per-device shared secret (plan §12.5). ADMS terminals cannot send one, so it is enforced only for other
        protocols (Hikvision carries it in the listening-host URL: ?sn=..&key=..). Compared constant-time against
        sha256(key)."""
        if not device.comm_key_hash or device.protocol == 'adms':
            return True
        key = req.q('key')
        presented = _qstr(key) if key is not None else (req.header('x-device-key') or '')
        if not presented:
            return False
        a = hashlib.sha256(presented.encode('utf-8')).hexdigest().encode()
        b = device.comm_key_hash.lower().encode('utf-8')
        return len(a) == len(b) and hmac.compare_digest(a, b)

    # ------------------------------------------------------------------ operational
    def health(self) -> dict:
        return {'ok': True, 'signer': 'ephemeral' if self.signer.ephemeral else 'configured',
                'templates': 'encrypted' if self.cipher else 'discarded'}

    # ------------------------------------------------------------------ shared ingest
    def ingest(self, device: DeviceRecord, base: NormalizedPunch, tz_offset_min: int, c: Counters) -> NormalizedPunch | None:
        """Shared ingest for every vendor: normalise, sign, dedupe, store, queue recompute."""
        try:
            punch = build_punch(base, tz_offset_min, self.signer)
        except DeviceTimeError:
            c.malformed += 1
            return None
        skew = punch.punched_at_ms - now_ms()
        # A reset clock (factory 2000-01-01) or one days ahead cannot be a real punch; storing it would put attendance
        # on the wrong day. Rejected outright (the raw archive keeps the line); smaller future skews are stored but flagged.
        if punch.punched_at_ms < MIN_PUNCH_MS or skew > MAX_FUTURE_MS:
            c.rejected += 1
            return None
        if skew > self.max_skew_ms:
            punch.raw = {**punch.raw, 'clockSkew': True, 'clockSkewMs': skew}
            c.skewed += 1
        punch.employee_id = self.store.resolve_employee(device.id, base.device_user_pin, punch.local_date)
        if not punch.employee_id:
            c.unmapped += 1
        outcome = self.store.insert_punch(punch)
        if outcome == 'inserted':
            c.inserted += 1
            if punch.employee_id:
                for d in affected_dates(punch.punched_at_ms, punch.local_date):
                    self.store.enqueue_recompute(punch.employee_id, d, f'punch:{device.vendor}')
        elif outcome == 'duplicate':
            c.duplicate += 1
        else:
            c.near += 1
        return punch

    def store_templates(self, device: DeviceRecord, table: str, body: str) -> dict:
        """Encrypt and store template uploads; discard photos; never log bodies (plan §9.1 decision 5)."""
        parsed = parse_template_upload(table, body)
        stored = discarded = no_consent = 0
        for t in parsed.templates:
            if not self.cipher or not t.valid:
                discarded += 1
                continue
            # A template is kept only for a mapped employee with an active consent for that modality; after a
            # withdrawal (or before consent is given) the device's upload is dropped. The raw archive is unaffected.
            employee_id = self.store.resolve_employee(device.id, t.pin, iso_ms(now_ms())[:10])
            consent_id = self.store.active_consent_id(employee_id, t.modality) if employee_id else None
            if not employee_id or not consent_id:
                discarded += 1
                no_consent += 1
                continue
            aad = template_aad(employee_id, t.modality)
            self.store.insert_template(TemplateRecord(
                device_id=device.id, device_user_pin=t.pin, employee_id=employee_id, modality=t.modality,
                vendor_format=t.vendor_format, ciphertext=self.cipher.encrypt(t.template_b64, aad),
                key_version=self.cipher.key_version, aad_hash=aad_hash(aad), consent_id=consent_id))
            stored += 1
        if no_consent:
            _log(logging.WARNING, {'sn': device.serial_no, 'count': no_consent},
                 'templates discarded: no active biometric consent for the mapped employee')
        if parsed.templates and not self.cipher:
            _log(logging.WARNING, {'sn': device.serial_no, 'count': len(parsed.templates)},
                 'templates discarded: DATA_ENCRYPTION_KEY not configured')
        _log(logging.INFO, {'sn': device.serial_no, 'table': table, 'stored': stored, 'discarded': discarded,
                            'photos': parsed.photos, 'users': parsed.users, 'unknown': parsed.unknown}, 'template upload')
        return {'stored': stored, 'discarded': discarded, 'templates': parsed.templates, 'photos': parsed.photos,
                'users': parsed.users, 'unknown': parsed.unknown}

    def _record_drift(self, sn: str, newest: str, tz_offset_min: int) -> None:
        drift = drift_seconds(newest, tz_offset_min, now_ms())
        if drift > -600:
            self.store.touch(sn, clock_drift_sec=drift)
        if abs(drift) > 60 and drift > -600:
            _log(logging.WARNING, {'sn': sn, 'driftSec': drift}, 'device clock drift above 60 s')

    # ------------------------------------------------------------------ ZKTeco ADMS
    def cdata_get(self, req: GwRequest) -> GwResponse:
        sn = serial_of(req.q('SN'))
        if not sn:
            return plain('ERROR: SN required', 400)
        push_version = version_of(req.q('pushver'))
        device = self.store.get_device(sn)
        if not device:
            device = self.store.register_pending(sn, vendor='zkteco', protocol='adms', push_version=push_version)
            if not device:
                _log(logging.WARNING, {'sn': sn}, 'pending device cap reached; registration refused')
                return plain('ERROR: too many pending devices', 403)
            _log(logging.WARNING, {'sn': sn}, 'unknown device registered as pending')
        t = self._touch_now()
        self.store.touch(sn, last_seen_at=t, last_handshake_at=t, push_version=push_version)
        # Not approved: acknowledge so the device keeps polling, but send no options and accept no data.
        if device.status != 'active':
            return plain('OK')
        return plain(build_options(sn, attlog_stamp=device.attlog_stamp, oplog_stamp=device.oplog_stamp,
                                   server_version=self.server_version))

    def _approved(self, sn: str | None) -> DeviceRecord | None:
        device = self.store.get_device(sn) if sn else None
        return device if device and device.status == 'active' else None

    def cdata_post(self, req: GwRequest) -> GwResponse:
        sn = serial_of(req.q('SN'))
        table = _qstr(req.q('table')).upper()[:32]
        body = req.text
        device = self._approved(sn)
        if not device:
            _log(logging.WARNING, {'sn': sn, 'table': table}, 'data from unapproved device rejected')
            return plain('ERROR: device not approved', 403)
        self.archive.append(sn, f'cdata:{table or "none"}', body)
        self.store.touch(sn, last_seen_at=self._touch_now())

        if table == 'ATTLOG':
            parsed = parse_attlog(body)
            c = Counters()
            newest = ''
            for r in parsed.records:
                self.ingest(device, NormalizedPunch(
                    device_id=device.id, device_sn=sn, vendor=device.vendor, device_user_pin=r.pin, employee_id=None,
                    device_local_time=r.device_local_time, direction=direction_of(r.status_code),
                    verify_mode=verify_mode_of(r.verify_code), source=device.vendor,
                    raw={'line': r.line, 'statusCode': r.status_code, 'verifyCode': r.verify_code, 'workcode': r.workcode},
                ), device.tz_offset_min, c)
                if r.device_local_time > newest:
                    newest = r.device_local_time
            stamp = stamp_of(req.q('Stamp'))
            if stamp:
                self.store.touch(sn, attlog_stamp=stamp)
            if newest and c.inserted > 0:
                # Only fresh (non-buffered) records say anything about the clock; buffered replays are old by definition.
                self._record_drift(sn, newest, device.tz_offset_min)
            _log(logging.INFO, {'sn': sn, 'records': len(parsed.records), **c.as_dict(),
                                'malformed': len(parsed.malformed) + c.malformed}, 'ATTLOG')
            if parsed.malformed:
                _log(logging.WARNING, {'sn': sn, 'malformed': parsed.malformed[:5]}, 'malformed ATTLOG lines skipped')
            return plain(f'OK: {len(parsed.records)}')

        if table == 'OPERLOG':
            stamp = stamp_of(req.q('OpStamp'))
            if stamp:
                self.store.touch(sn, oplog_stamp=stamp)
            self.store_templates(device, table, body)
            return plain('OK: 1')

        if table in TEMPLATE_TABLES:
            self.store_templates(device, table, body)
            return plain('OK: 1')

        _log(logging.INFO, {'sn': sn, 'table': table, 'bytes': len(body)}, 'unhandled cdata table acknowledged')
        return plain('OK: 1')

    def getrequest(self, req: GwRequest) -> GwResponse:
        sn = serial_of(req.q('SN'))
        device = self.store.get_device(sn) if sn else None
        if not device:
            return plain('ERROR: unknown device', 404)
        self.store.touch(sn, last_seen_at=self._touch_now())
        if device.status != 'active':
            return plain('OK')
        cmds = self.store.pop_queued_commands(device.id)
        if not cmds:
            return plain('OK')
        return plain('\n'.join(f"C:{c['id']}:{c['command']}" for c in cmds) + '\n')

    def devicecmd(self, req: GwRequest) -> GwResponse:
        sn = serial_of(req.q('SN'))
        body = req.text
        # Results are accepted only from an approved device and only for that device's own commands; otherwise anyone
        # on the network could mark another terminal's commands as done or failed.
        device = self._approved(sn)
        if not device:
            return plain('ERROR: device not approved', 403)
        self.archive.append(sn, 'devicecmd', body)
        self.store.touch(sn, last_seen_at=self._touch_now())
        for r in parse_device_cmd_results(body):
            self.store.ack_command(device.id, r['id'], r['returnCode'])
            _log(logging.INFO, {'sn': sn, **r}, 'device command result')
        return plain('OK')

    def fdata(self, req: GwRequest) -> GwResponse:
        sn = serial_of(req.q('SN'))
        table = _qstr(req.q('table')).upper()[:32]
        body = req.text
        device = self._approved(sn)
        if not device:
            return plain('ERROR: device not approved', 403)
        self.archive.append(sn, f'fdata:{table or "none"}', body)
        self.store.touch(sn, last_seen_at=self._touch_now())
        self.store_templates(device, table, body)
        return plain('OK')

    # ------------------------------------------------------------------ Hikvision ISAPI listening host
    def hikvision_event(self, req: GwRequest) -> GwResponse:
        body = req.body
        content_type = req.header('content-type') or ''
        payload = extract_event_json(body, content_type)
        ev = parse_hik_event(payload if payload is not None else {})
        sn = serial_of(req.q('sn')) or (serial_of(ev.serial_no) if ev.serial_no else None)
        if not sn:
            return plain('ERROR: serial number required (use ?sn=)', 400)
        device = self.store.get_device(sn)
        if not device:
            device = self.store.register_pending(sn, vendor='hikvision', protocol='isapi')
            if not device:
                _log(logging.WARNING, {'sn': sn}, 'pending device cap reached; registration refused')
                return plain('ERROR: too many pending devices', 403)
            _log(logging.WARNING, {'sn': sn}, 'unknown hikvision device registered as pending')
        if device.status != 'active':
            self.store.touch(sn, last_seen_at=self._touch_now())
            return plain('ERROR: device not approved', 403)
        if not self.comm_key_ok(device, req):
            _log(logging.WARNING, {'sn': sn}, 'device key missing or wrong')
            return plain('ERROR: device key rejected', 403)
        # Archived and marked seen only once authenticated (unknown or unauthenticated senders could otherwise fill the disk).
        self.archive.append(sn, 'hikvision', body)
        self.store.touch(sn, last_seen_at=self._touch_now())
        if ev.kind == 'heartbeat':
            return plain('OK')
        if ev.kind != 'attendance' or not ev.pin or not ev.device_local_time:
            _log(logging.DEBUG, {'sn': sn, 'major': ev.major, 'minor': ev.minor, 'xml': ev.raw.get('fromXml') is True},
                 'non-attendance event ignored')
            return plain('OK')
        c = Counters()
        tz = ev.tz_offset_min if ev.tz_offset_min is not None else device.tz_offset_min
        self.ingest(device, NormalizedPunch(
            device_id=device.id, device_sn=sn, vendor=device.vendor, device_user_pin=ev.pin, employee_id=None,
            device_local_time=ev.device_local_time, direction=ev.direction, verify_mode=ev.verify_mode,
            source=device.vendor, raw=ev.raw), tz, c)
        if c.inserted > 0:
            self._record_drift(sn, ev.device_local_time, tz)
        _log(logging.INFO, {'sn': sn, **c.as_dict()}, 'hikvision event')
        return plain('OK')

    # ------------------------------------------------------------------ errors
    def error_response(self, exc: BaseException, path: str) -> GwResponse:
        """Devices must get plain text, never an HTML error page."""
        if isinstance(exc, BodyTooLarge):
            return plain('ERROR: body too large', 413)
        if is_storage_unavailable(exc):
            _log(logging.ERROR, {'code': getattr(exc, 'code', None) or getattr(exc, 'sqlstate', None), 'path': path},
                 'storage unavailable')
            return plain('ERROR: storage unavailable, retry later', 503)
        log.error('gateway error %s', json.dumps({'path': path}), exc_info=exc)
        return plain('ERROR', 500)

    def log_request(self, req: GwRequest, status: int) -> None:
        """Request logs carry method and path only: the Hikvision device key travels in the query string or the
        x-device-key header, so the URL is redacted and headers are never logged."""
        if not self.request_log or req.url.startswith('/iclock/getrequest') or req.url == '/health':
            return
        _log(logging.INFO, {'req': {'method': req.method, 'url': redact_url(req.url)}, 'res': {'statusCode': status}},
             'request completed')


class BodyTooLarge(Exception):
    """entity.too.large in the TS body parsers."""


class BodyUnreadable(Exception):
    """Unsupported charset or content encoding (body-parser's 415s, which the TS error handler answers with 500)."""


