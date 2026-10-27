"""
Biometric terminals: activation, keys, PIN mappings, unmapped punches and the command queue (port of
apps/api/src/modules/devices/routes.ts). HR and admin only; every query is scoped to the user's company.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, timedelta

from django.db import transaction

from hajiri.attendance.service import recompute_day
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Bool, Enum, IsoDate, Obj, Str, Uuid, parse

ROLES = ('hr', 'admin')
PSEUDO_VENDORS = ('mobile', 'kiosk', 'manual')
COMMANDS = ('CHECK', 'INFO', 'REBOOT', 'LOG', 'DATA QUERY USERINFO', 'CLEAR LOG')
# The command picker on the Devices page offers these (CLEAR LOG stays API-only, as in the TS page).
PAGE_COMMANDS = ('CHECK', 'INFO', 'LOG', 'REBOOT', 'DATA QUERY USERINFO')
VENDOR = {'zkteco': 'ZKTeco', 'hikvision': 'Hikvision', 'suprema': 'Suprema'}
PROTOCOL = {'adms': 'ADMS push', 'isapi': 'ISAPI'}

# Terminal PINs are plain digits/letters; anything else could smuggle extra fields or commands into the device
# protocol. \Z, not $: Python's $ also matches before a trailing newline.
PIN_RE = re.compile(r'^[A-Za-z0-9]{1,24}\Z')
PIN_MESSAGE = 'PIN must be 1 to 24 letters or digits'

ACTIVATE = Obj({'branchId': Uuid(optional=True), 'name': Str(max=120, optional=True)})
MAP = Obj({'pin': Str(regex=PIN_RE.pattern, regex_message=PIN_MESSAGE), 'employeeId': Uuid(), 'from': IsoDate(),
           'syncToDevice': Bool(default=True), 'replace': Bool(default=False)})
COMMAND = Obj({'command': Enum(COMMANDS)})


def _require(user: AuthUser) -> str:
    if not has_role(user, *ROLES):
        raise ForbiddenError(f'requires role {" or ".join(ROLES)}')
    return company_id_of(user)


def _id(v) -> str:
    return parse(Uuid(), str(v))


def _js_slice(s: str, n: int) -> str:
    """String.prototype.slice(0, n): counts UTF-16 code units."""
    out, units = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > n:
            break
        out.append(ch)
        units += w
    return ''.join(out)


def device_name(name: str) -> str:
    """Terminal user names travel in a tab-separated, newline-delimited command: strip every control character."""
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', name or '')
    s = re.sub(r'\s+', ' ', s).strip()
    return _js_slice(s, 24)


def userinfo_command(pin: str, name: str) -> str:
    return f'DATA UPDATE USERINFO PIN={pin}\tName={device_name(name)}\tPri=0'


def live_status(d: dict) -> str:
    """Active devices are shown as online/offline; pending and disabled keep their lifecycle status."""
    if d.get('status') != 'active':
        return d.get('status') or ''
    return 'offline' if d.get('offline') else 'online'


# ---------------------------------------------------------------- reads

def list_devices(user: AuthUser) -> list[dict]:
    cid = _require(user)
    return rows("""
        SELECT d.id, d.company_id, d.branch_id, d.serial_no, d.vendor, d.protocol, d.name, d.status, d.fw_version, d.push_version,
               d.tz_offset_min, d.last_handshake_at, d.last_seen_at, d.created_at,
               (d.last_seen_at IS NULL OR d.last_seen_at < now() - interval '120 seconds') AS offline,
               (SELECT count(*) FROM device_user_map m WHERE m.device_id = d.id AND m.valid @> %s::date) AS mapped_users,
               (SELECT count(*) FROM device_command c WHERE c.device_id = d.id AND c.status = 'queued') AS queued_commands
          FROM device d WHERE d.company_id = %s OR d.company_id IS NULL ORDER BY d.status, d.created_at DESC""",
        [npt_today(), cid])


def get_device(user: AuthUser, device_id) -> dict:
    """One device as list_devices shows it (pending devices with no company included), or 404."""
    did = _id(device_id)
    for d in list_devices(user):
        if str(d['id']) == did:
            return d
    raise NotFoundError('device not found')


def mappings(user: AuthUser, device_id) -> list[dict]:
    cid = _require(user)
    return rows("""
        SELECT m.device_user_pin, m.employee_id, e.emp_code, e.name_en, m.valid::text AS valid
          FROM device_user_map m JOIN employee e ON e.id = m.employee_id JOIN device d ON d.id = m.device_id
         WHERE m.device_id = %s AND d.company_id = %s ORDER BY m.device_user_pin, lower(m.valid)""", [_id(device_id), cid])


def commands(user: AuthUser, device_id) -> list[dict]:
    cid = _require(user)
    return rows("""
        SELECT c.id, c.command, c.status, c.return_code, c.queued_at, c.sent_at, c.acked_at
          FROM device_command c JOIN device d ON d.id = c.device_id
         WHERE c.device_id = %s AND d.company_id = %s ORDER BY c.id DESC LIMIT 100""", [_id(device_id), cid])


def unmapped_punches(user: AuthUser) -> list[dict]:
    """Punches from active devices whose PIN is not mapped to an employee (HR queue, plan §5.2)."""
    cid = _require(user)
    return rows("""
        SELECT p.id, p.device_id, p.device_sn, p.device_user_pin, p.punched_at, p.local_date, p.verify_mode,
               (SELECT count(*) FROM punch_event q WHERE q.device_id = p.device_id AND q.device_user_pin = p.device_user_pin
                  AND q.employee_id IS NULL) AS total_for_pin
          FROM punch_event p JOIN device d ON d.id = p.device_id
         WHERE p.employee_id IS NULL AND d.company_id = %s AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
         ORDER BY p.ingested_at DESC LIMIT 500""", [cid])


def branches(user: AuthUser) -> list[dict]:
    cid = _require(user)
    return rows('SELECT id, code, name_en FROM branch WHERE company_id = %s ORDER BY code', [cid])


# ---------------------------------------------------------------- lifecycle

def activate(user: AuthUser, device_id, body: dict) -> dict:
    """Approve a pending device for this company (only active devices may push punches)."""
    cid = _require(user)
    did = _id(device_id)
    b = parse(ACTIVATE, body)
    if b.get('branchId') and not one('SELECT 1 AS x FROM branch WHERE id = %s AND company_id = %s', [b['branchId'], cid]):
        raise NotFoundError('branch not found')
    with transaction.atomic():
        r = one("""UPDATE device SET status = 'active', company_id = %s, branch_id = coalesce(%s::uuid, branch_id), name = coalesce(%s, name)
                    WHERE id = %s AND (company_id IS NULL OR company_id = %s) RETURNING id, serial_no, status""",
                [cid, b.get('branchId'), b.get('name'), did, cid])
        if not r:
            raise NotFoundError('device not found')
        r = {**r, 'id': str(r['id'])}
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
    d = one('SELECT protocol FROM device WHERE id = %s AND company_id = %s', [did, cid])
    if not d:
        raise NotFoundError('device not found')
    if d['protocol'] == 'adms':
        raise BadRequestError('ZKTeco ADMS terminals cannot send a device key; keep them on the office network')
    key = secrets.token_urlsafe(24)
    with transaction.atomic():
        execute('UPDATE device SET comm_key_hash = %s WHERE id = %s', [hashlib.sha256(key.encode()).hexdigest(), did])
        audit('rotate_key', 'device', actor_id=user.id, row_id=did)
    return {'key': key}


def disable(user: AuthUser, device_id) -> dict:
    cid = _require(user)
    did = _id(device_id)
    with transaction.atomic():
        r = one("""UPDATE device SET status = 'disabled' WHERE id = %s AND company_id = %s AND vendor NOT IN ('mobile', 'kiosk', 'manual')
                   RETURNING id, serial_no, status""", [did, cid])
        if not r:
            raise NotFoundError('device not found')
        audit('disable', 'device', actor_id=user.id, row_id=did)
    return {**r, 'id': str(r['id'])}


# ---------------------------------------------------------------- PIN mapping

_MOVING = """SELECT p.* FROM punch_event p
              WHERE p.device_id = %(d)s AND p.device_user_pin = %(pin)s AND p.employee_id IS DISTINCT FROM %(e)s::uuid
                AND p.local_date >= %(f)s::date AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)"""


def _day(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def map_pin(user: AuthUser, device_id, body: dict) -> dict:
    """
    Map a device PIN to an employee from `from` onwards. If the PIN is already mapped to someone else in that range
    the request is refused (409) unless `replace` is set, so a typo cannot silently take over a colleague's punches.
    Every punch for that PIN since `from` (unmapped or attributed to another employee) is re-attributed by inserting
    superseding rows (punch_event is append-only), and the affected days are recomputed for everyone involved.
    """
    cid = _require(user)
    did = _id(device_id)
    b = parse(MAP, body)
    dev = one('SELECT serial_no, vendor FROM device WHERE id = %s AND company_id = %s', [did, cid])
    if not dev:
        raise NotFoundError('device not found')
    emp = one('SELECT emp_code, name_en FROM employee WHERE id = %s AND company_id = %s', [b['employeeId'], cid])
    if not emp:
        raise NotFoundError('employee not found')
    p = {'d': did, 'pin': b['pin'], 'e': b['employeeId'], 'f': b['from']}
    with transaction.atomic():
        execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f"pinmap:{did}:{b['pin']}"])
        holders = rows("""
            SELECT DISTINCT m.employee_id, e.emp_code, e.name_en FROM device_user_map m JOIN employee e ON e.id = m.employee_id
             WHERE m.device_id = %(d)s AND m.device_user_pin = %(pin)s AND m.valid && daterange(%(f)s::date, NULL)
               AND m.employee_id <> %(e)s::uuid""", p)
        if holders and not b['replace']:
            who = ', '.join(f"{h['name_en']} ({h['emp_code']})" for h in holders)
            raise ConflictError(f"PIN {b['pin']} on this device is already mapped to {who}. Confirm the replacement to move "
                                f"their punches since {b['from']} to {emp['name_en']}.")
        # Close any open mapping for this PIN, then open the new one.
        execute("""UPDATE device_user_map SET valid = daterange(lower(valid), %(f)s::date)
                    WHERE device_id = %(d)s AND device_user_pin = %(pin)s AND upper_inf(valid) AND lower(valid) < %(f)s::date""", p)
        execute('DELETE FROM device_user_map WHERE device_id = %(d)s AND device_user_pin = %(pin)s AND lower(valid) >= %(f)s::date', p)
        execute("INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES (%(d)s, %(pin)s, %(e)s, daterange(%(f)s::date, NULL))", p)
        # Current (not yet superseded) punches for this PIN since `from` that are not already this employee's.
        previous = rows(f'SELECT DISTINCT employee_id, local_date::text AS local_date FROM ({_MOVING}) m WHERE employee_id IS NOT NULL', p)
        re_rows = rows(f"""
            WITH moving AS ({_MOVING})
            INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
                                     direction, verify_mode, source, raw, gateway_signature, idempotency_key, supersedes_id)
            SELECT device_id, device_sn, vendor, device_user_pin, %(e)s::uuid, punched_at, device_local_time, local_date, direction,
                   verify_mode, source, raw, gateway_signature,
                   encode(sha256(convert_to(idempotency_key || ':reattributed:' || id::text, 'UTF8')), 'hex'), id
              FROM moving RETURNING local_date::text AS local_date""", p)
        dates = list(dict.fromkeys(r['local_date'] for r in re_rows))
        if b['syncToDevice'] and dev['vendor'] == 'zkteco':
            execute('INSERT INTO device_command (device_id, command) VALUES (%s, %s)', [did, userinfo_command(b['pin'], emp['name_en'])])
        audit('map_pin', 'device_user_map', actor_id=user.id, row_id=did,
              after={'pin': b['pin'], 'employeeId': b['employeeId'], 'from': b['from'], 'reattributed': len(re_rows),
                     'replaced': [str(h['employee_id']) for h in holders]})

    # Recompute both sides; a morning punch can belong to the previous day's shift.
    work: dict[tuple[str, str], None] = {}
    for d in dates:
        work[(b['employeeId'], _day(d, -1))] = None
        work[(b['employeeId'], d)] = None
    for pr in previous:
        e = str(pr['employee_id'])
        work[(e, _day(pr['local_date'], -1))] = None
        work[(e, pr['local_date'])] = None
    for e, d in work:
        recompute_day(e, d)
    moved_from = list(dict.fromkeys(str(pr['employee_id']) for pr in previous))
    return {'ok': True, 'reattributed': len(dates), 'dates': dates, 'movedFrom': moved_from}


# ---------------------------------------------------------------- commands

def queue_command(user: AuthUser, device_id, body: dict) -> dict:
    """Queue a command for the terminal (allow-listed verbs only; SHELL is impossible by DB constraint)."""
    cid = _require(user)
    did = _id(device_id)
    b = parse(COMMAND, body)
    if not one('SELECT 1 AS x FROM device WHERE id = %s AND company_id = %s', [did, cid]):
        raise NotFoundError('device not found')
    with transaction.atomic():
        r = one('INSERT INTO device_command (device_id, command) VALUES (%s, %s) RETURNING id', [did, b['command']])
        audit('device_command', 'device_command', actor_id=user.id, row_id=str(r['id']), after=b)
    return {'id': int(r['id'])}


def sync_users(user: AuthUser, device_id) -> dict:
    """Push all current PIN mappings to the device as USERINFO updates."""
    cid = _require(user)
    did = _id(device_id)
    found = rows("""
        SELECT m.device_user_pin, e.name_en FROM device_user_map m JOIN employee e ON e.id = m.employee_id JOIN device d ON d.id = m.device_id
         WHERE m.device_id = %s AND d.company_id = %s AND m.valid @> %s::date AND e.status = 'active'""", [did, cid, npt_today()])
    with transaction.atomic():
        for m in found:
            if not PIN_RE.match(m['device_user_pin']):
                continue
            execute('INSERT INTO device_command (device_id, command) VALUES (%s, %s)', [did, userinfo_command(m['device_user_pin'], m['name_en'])])
    return {'queued': len(found)}
