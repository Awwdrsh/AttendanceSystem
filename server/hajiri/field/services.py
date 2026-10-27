"""
Field force, breaks, locations, kiosks (HR side), kiosk PIN and QR badge, my roster and shift swaps (port of
apps/api/src/modules/field: routes.ts, policy.ts, punches.ts, swaps.ts, jobs.ts).

Attendance maths is unchanged: breaks are recorded and reported, visits can open the day with a field punch, and approved
swaps become roster rows.

Public interface:
- field_policy(company_id) -> {'geofenceMode', 'breaks': {lunch|tea|prayer|personal: {enabled, paid, maxMin}},
  'fieldPhotoRequired', 'fieldVisitClockIn'}; BREAK_KINDS, BREAK_LABEL, FIELD_POLICY_KEY.
- next_direction(employee_id, local_date=None) -> 'in'|'out'; ensure_kiosk_device(company_id);
  insert_app_punch(...) -> {'duplicate', 'punchId', 'punchedAt', 'localDate', 'photoId'}; close_open_break(employee_id, at).
- effective_shift(employee_id, local_date) -> {'shiftId', 'source'}; apply_swap_decision (workflow handler 'shift_swap').
- close_stale_breaks(now=None) -> int (daily job 'staleBreaksClosed').
- One function per TS route taking (user, body_or_query) in the TS camelCase shape: get_policy, put_policy, me,
  start_break, end_break, list_breaks, start_visit, end_visit, list_visits, visit_photo, locations, create_location,
  update_location, delete_location, staff, update_staff, kiosks, create_kiosk, update_kiosk, kiosk_pairing_code,
  revoke_kiosk, unlock_kiosk, set_kiosk_pin, reset_kiosk_pin, kiosk_qr, my_roster, swap_colleagues, swaps,
  request_swap, accept_swap, decline_swap, cancel_swap.
  list_breaks / list_visits with format='csv' return {'filename', 'columns', 'rows'} (hajiri.insights CSV sources).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

from django.db import transaction

from engines.attendance import local_date_of
from engines.bs_calendar import weekday_of

from hajiri.attendance.repo import from_ms, to_ms
from hajiri.attendance.service import date_range, enqueue_recompute, is_attendance_locked, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as _now
from hajiri.core.crypto import hash_password, sha256_hex
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, HttpError, NotFoundError
from hajiri.core.schema import (Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid, safe_parse)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.punch.services import (PHOTO_FIELD_MAX, ensure_pseudo_device, npt_hm, npt_local_time, parse_photo,
                                   store_punch_photo)

PAIRING_TTL_MIN = 15
MAX_RANGE_DAYS = 92


class TooManyError(HttpError):
    status, default_message = 429, 'too many requests'


# ================================================================ policy.ts

BREAK_KINDS = ('lunch', 'tea', 'prayer', 'personal')
BREAK_LABEL = {'lunch': 'Lunch', 'tea': 'Tea', 'prayer': 'Prayer', 'personal': 'Personal'}
FIELD_POLICY_KEY = 'field.policy'
_BREAK_DEFAULTS = {'lunch': (False, 60), 'tea': (True, 15), 'prayer': (True, 15), 'personal': (False, 15)}


def _break_rule(paid: bool, max_min: int) -> Obj:
    return Obj({'enabled': Bool(default=True), 'paid': Bool(default=paid), 'maxMin': Int(min=5, max=180, default=max_min)},
               default=lambda: {'enabled': True, 'paid': paid, 'maxMin': max_min})


FIELD_POLICY = Obj({
    'geofenceMode': Enum(('off', 'flag', 'block'), default='flag'),
    'breaks': Obj({k: _break_rule(*v) for k, v in _BREAK_DEFAULTS.items()},
                  default=lambda: {k: {'enabled': True, 'paid': p, 'maxMin': m} for k, (p, m) in _BREAK_DEFAULTS.items()}),
    'fieldPhotoRequired': Bool(default=False),
    'fieldVisitClockIn': Bool(default=True),
})


def field_policy(company_id: str) -> dict:
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [str(company_id), FIELD_POLICY_KEY])
    ok, v = safe_parse(FIELD_POLICY, (r or {}).get('value') or {})
    return v if ok else parse(FIELD_POLICY, {})


# ================================================================ punches.ts

def ensure_kiosk_device(company_id: str) -> dict:
    """One device row per company for kiosk punches (vendor 'kiosk', so the offline alarm ignores it)."""
    c = one('SELECT code FROM company WHERE id = %s', [str(company_id)])
    serial = f"KIOSK-{c['code']}"
    execute("""INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES (%s, %s, 'kiosk', 'app', 'Kiosk tablets', 'active')
               ON CONFLICT (serial_no) DO NOTHING""", [str(company_id), serial])
    d = one('SELECT id FROM device WHERE serial_no = %s', [serial])
    return {'id': str(d['id']), 'serialNo': serial, 'vendor': 'kiosk'}


def next_direction(employee_id: str, local_date: str | None = None) -> str:
    """Direction of the next punch today: in after nothing or an out, otherwise out."""
    local_date = local_date or local_date_of(to_ms(_now()))
    r = one("""SELECT direction FROM punch_event p WHERE employee_id = %s AND local_date = %s
                 AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id) ORDER BY punched_at DESC LIMIT 1""",
            [str(employee_id), local_date])
    last = r['direction'] if r else None
    return 'in' if not last or last == 'out' else 'out'


def insert_app_punch(*, company_id: str, employee_id: str, emp_code: str, direction: str, kind: str, idempotency: str,
                     raw: dict, photo=None) -> dict:
    """Store a kiosk or field punch at server time, with an optional encrypted photo, then recompute the day."""
    key = sha256_hex(f'{kind}|{employee_id}|{idempotency}')
    existing = one('SELECT id, punched_at, local_date FROM punch_event WHERE idempotency_key = %s', [key])
    if existing:
        return {'duplicate': True, 'punchId': str(existing['id']), 'punchedAt': existing['punched_at'],
                'localDate': str(existing['local_date']), 'photoId': None}
    at = to_ms(_now())
    local_date = local_date_of(at)
    device = ensure_kiosk_device(company_id) if kind == 'kiosk' else ensure_pseudo_device(company_id, 'MOBILE')
    photo_id = str(uuid.uuid4()) if photo else None
    flags = raw.get('flags') if isinstance(raw.get('flags'), list) else []
    with transaction.atomic():
        row = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                       local_date, direction, verify_mode, source, raw, idempotency_key)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, punched_at, local_date, direction""",
                  [device['id'], device['serialNo'], device['vendor'], emp_code, employee_id, from_ms(at), npt_local_time(at), local_date,
                   direction, 'kiosk' if kind == 'kiosk' else 'field', kind, json.dumps({**raw, 'flags': flags, 'photoId': photo_id}), key])
        if photo_id and photo:
            store_punch_photo(photo_id=photo_id, company_id=company_id, employee_id=employee_id, punch=row, photo=photo)
    recompute_for_punch(employee_id, at, str(row['id']))
    return {'duplicate': False, 'punchId': str(row['id']), 'punchedAt': row['punched_at'], 'localDate': str(row['local_date']),
            'photoId': photo_id}


def close_open_break(employee_id: str, at) -> None:
    """Close an open break at `at` (used when someone clocks out at a kiosk mid-break)."""
    execute("""UPDATE work_break SET ended_at = greatest(%(at)s::timestamptz, started_at),
                      minutes = floor(extract(epoch FROM greatest(%(at)s::timestamptz, started_at) - started_at) / 60)::int,
                      over_limit = floor(extract(epoch FROM greatest(%(at)s::timestamptz, started_at) - started_at) / 60) > max_min
                WHERE employee_id = %(e)s AND ended_at IS NULL""", {'at': at, 'e': str(employee_id)})


# ================================================================ jobs.ts

def close_stale_breaks(now: datetime | None = None) -> int:
    """Nightly: close breaks left open from an earlier day. The break ends at the day's first clock-out after it started,
    capped at its limit, so a forgotten break never runs for hours."""
    return execute("""
        UPDATE work_break b SET ended_at = x.ended_at, minutes = floor(extract(epoch FROM x.ended_at - b.started_at) / 60)::int,
               over_limit = floor(extract(epoch FROM x.ended_at - b.started_at) / 60) > b.max_min
          FROM (SELECT wb.id, least(coalesce((SELECT min(p.punched_at) FROM punch_event p WHERE p.employee_id = wb.employee_id
                        AND p.local_date = wb.local_date AND p.direction = 'out' AND p.punched_at > wb.started_at),
                        wb.started_at + make_interval(mins => wb.max_min)),
                        wb.started_at + make_interval(mins => wb.max_min)) AS ended_at
                  FROM work_break wb WHERE wb.ended_at IS NULL AND wb.local_date < (%s::timestamptz AT TIME ZONE 'Asia/Kathmandu')::date) x
         WHERE b.id = x.id""", [now or _now()])


# ================================================================ swaps.ts

def effective_shift(employee_id: str, local_date: str) -> dict:
    """What someone works on a date: a roster row wins (NULL = off), then the weekly off, then the default shift."""
    r = one("""SELECT EXISTS (SELECT 1 FROM roster WHERE employee_id = e.id AND local_date = %(d)s) AS has_roster,
                      (SELECT shift_id FROM roster WHERE employee_id = e.id AND local_date = %(d)s) AS roster_shift,
                      (SELECT shift_id FROM shift_assignment WHERE employee_id = e.id AND valid @> %(d)s::date) AS default_shift,
                      e.weekly_off_days
                 FROM employee e WHERE e.id = %(e)s""", {'e': str(employee_id), 'd': local_date})
    if not r:
        return {'shiftId': None, 'source': 'none'}
    if r['has_roster']:
        return {'shiftId': str(r['roster_shift']), 'source': 'roster'} if r['roster_shift'] else {'shiftId': None, 'source': 'roster_off'}
    if weekday_of(local_date) in (r['weekly_off_days'] or []):
        return {'shiftId': None, 'source': 'weekly_off'}
    return {'shiftId': str(r['default_shift']), 'source': 'default'} if r['default_shift'] else {'shiftId': None, 'source': 'none'}


def apply_swap_decision(object_id: str, state: str, ctx=None) -> None:
    """Approved swap: each person takes the other's current shift for that day (roster rows), both are told, and both
    days are recomputed."""
    s = one('SELECT id, company_id, requester_id, colleague_id, local_date::text AS local_date, status FROM shift_swap WHERE id = %s FOR UPDATE',
            [str(object_id)])
    if not s or s['status'] != 'pending':
        return
    req, col = str(s['requester_id']), str(s['colleague_id'])
    if state == 'approved':
        for i in (req, col):
            if is_attendance_locked(i, s['local_date']):
                raise ConflictError('attendance for that day is locked by payroll')
        a = effective_shift(req, s['local_date'])
        b = effective_shift(col, s['local_date'])
        upsert = """INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s)
                    ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id"""
        execute(upsert, [req, s['local_date'], b['shiftId']])
        execute(upsert, [col, s['local_date'], a['shiftId']])
        enqueue_recompute([(req, s['local_date']), (col, s['local_date'])], 'shift_swap')
    execute('UPDATE shift_swap SET status = %s, decided_at = now() WHERE id = %s', [state, str(s['id'])])
    for emp in (req, col):
        user_id = user_id_for_employee(emp)
        if not user_id:
            continue
        notify(company_id=str(s['company_id']), user_id=user_id, employee_id=emp, template=f'shift_swap.{state}',
               title='Shift swap approved' if state == 'approved' else 'Shift swap not approved',
               body=(f"The shift swap on {bs_label(s['local_date'])} is approved. Check your roster." if state == 'approved'
                     else f"The shift swap on {bs_label(s['local_date'])} was not approved. Your shifts stay as they were."),
               payload={'swapId': str(s['id']), 'localDate': s['local_date']})


def swap_summary(inst: dict) -> dict:
    r = one("""SELECT s.local_date, s.reason, s.colleague_note, c.name_en AS colleague_name,
                      rs.code AS requester_shift_code, rs.name_en AS requester_shift_name, cs.code AS colleague_shift_code,
                      cs.name_en AS colleague_shift_name
                 FROM shift_swap s JOIN employee c ON c.id = s.colleague_id
                 LEFT JOIN shift rs ON rs.id = s.requester_shift_id LEFT JOIN shift cs ON cs.id = s.colleague_shift_id
                WHERE s.id = %s""", [str(inst['object_id'])])
    return r or {}


# ================================================================ routes.ts helpers

GEO = {'lat': Num(min=-90, max=90, optional=True), 'lng': Num(min=-180, max=180, optional=True),
       'accuracyM': Num(min=0, max=10_000, optional=True)}
RANGE_QUERY = Obj({'from': IsoDate(), 'to': IsoDate(), 'employeeId': Uuid(optional=True)})


def _js_round(x: float) -> int:
    import math
    return math.floor(x + 0.5)


def check_range(from_: str, to: str, max_days: int = MAX_RANGE_DAYS) -> None:
    if to < from_:
        raise BadRequestError('"to" is before "from"')
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > max_days:
        raise BadRequestError(f'pick a range of {max_days} days or less')


def _scope_sql(alias: str, date_expr: str) -> str:
    """HR sees everyone, a manager their direct reports (on that day) and themselves, everyone else only themselves."""
    return f'(%(all)s OR {alias}.employee_id = %(me)s::uuid OR (%(mgr)s AND hj_manages(%(me)s::uuid, {alias}.employee_id, {date_expr})))'


def employee_of(user: AuthUser) -> dict:
    eid = user.employee_id
    if not eid:
        raise BadRequestError('Your account is not linked to an employee record. Ask HR to link it.')
    r = one("SELECT id, company_id, emp_code, name_en, field_work FROM employee WHERE id = %s AND status = 'active'", [eid])
    if not r:
        raise NotFoundError('employee not found')
    return {**r, 'id': str(r['id']), 'company_id': str(r['company_id'])}


def _assert_employee_in_company(company_id: str, employee_id: str) -> None:
    if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id]):
        raise NotFoundError('employee not found')


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _require_module(user: AuthUser, module: str) -> None:
    _require(user, *MODULE_ROLES[module])


def visit_aad(visit_id: str) -> str:
    return f'field_visit:{visit_id}'


def _ids(r: dict, *keys: str) -> dict:
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


# ================================================================ policy routes

def get_policy(user: AuthUser) -> dict:
    return {'policy': field_policy(company_id_of(user))}


def put_policy(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    value = parse(FIELD_POLICY, body)
    with transaction.atomic():
        before = field_policy(company_id)
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, FIELD_POLICY_KEY, json.dumps(value), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id=FIELD_POLICY_KEY, before=before, after=value)
    return {'policy': value}


# ================================================================ my day

def me(user: AuthUser) -> dict:
    e = employee_of(user)
    today = local_date_of(to_ms(_now()))
    policy = field_policy(e['company_id'])
    direction = next_direction(e['id'], today)
    breaks = [_ids(b, 'id') for b in rows("""SELECT id, kind, paid, max_min, started_at, ended_at, minutes, over_limit FROM work_break
                                              WHERE employee_id = %s AND (local_date = %s OR ended_at IS NULL) ORDER BY started_at""",
                                           [e['id'], today])]
    visits = [_ids(v, 'id') for v in rows("""SELECT id, place, purpose, started_at, ended_at, outcome, photo_file_id IS NOT NULL AS has_photo
                                              FROM field_visit WHERE employee_id = %s AND (local_date = %s OR ended_at IS NULL) ORDER BY started_at""",
                                           [e['id'], today])]
    cred = one('SELECT pin_hash, locked_until FROM kiosk_credential WHERE employee_id = %s', [e['id']])
    break_types = [{'kind': k, 'paid': policy['breaks'][k]['paid'], 'maxMin': policy['breaks'][k]['maxMin']}
                   for k in BREAK_KINDS if policy['breaks'][k]['enabled']]
    locked = cred['locked_until'] if cred and cred['locked_until'] and cred['locked_until'] > _now() else None
    return {
        'date': today, 'clockedIn': direction == 'out', 'fieldWork': bool(e['field_work']),
        'fieldPhotoRequired': policy['fieldPhotoRequired'], 'breakTypes': break_types, 'breaks': breaks,
        'openBreak': next((b for b in breaks if not b['ended_at']), None), 'visits': visits,
        'openVisit': next((v for v in visits if not v['ended_at']), None),
        'kioskPin': {'set': bool(cred and cred['pin_hash']), 'lockedUntil': locked},
    }


# ================================================================ breaks

def start_break(user: AuthUser, body: dict) -> dict:
    e = employee_of(user)
    b = parse(Obj({'kind': Enum(BREAK_KINDS)}), body)
    rule = field_policy(e['company_id'])['breaks'][b['kind']]
    if not rule['enabled']:
        raise BadRequestError('This kind of break is not used here.')
    if next_direction(e['id']) != 'out':
        raise BadRequestError('Clock in first, then start your break.')
    ins = one("""INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at) VALUES (%s, %s, %s, %s, %s, %s, now())
                 ON CONFLICT DO NOTHING RETURNING id, kind, paid, max_min, started_at""",
              [e['company_id'], e['id'], local_date_of(to_ms(_now())), b['kind'], rule['paid'], rule['maxMin']])
    if not ins:
        raise ConflictError('You are already on a break. End it first.')
    return {'break': _ids(ins, 'id')}


def end_break(user: AuthUser) -> dict:
    from hajiri.workflow.service import approvers_for
    e = employee_of(user)
    with transaction.atomic():
        br = one("""UPDATE work_break SET ended_at = now(), minutes = floor(extract(epoch FROM now() - started_at) / 60)::int,
                           over_limit = floor(extract(epoch FROM now() - started_at) / 60) > max_min
                     WHERE employee_id = %s AND ended_at IS NULL RETURNING id, kind, minutes, max_min, over_limit, started_at, ended_at""",
                 [e['id']])
        if not br:
            raise ConflictError('You are not on a break.')
        if br['over_limit']:
            for uid in approvers_for(e['company_id'], e['id'], 'manager'):
                notify(company_id=e['company_id'], user_id=uid, employee_id=e['id'], template='break.over_limit', title='Long break',
                       body=f"{e['name_en']} took a {br['minutes']} min {br['kind']} break (limit {br['max_min']} min).",
                       payload={'breakId': str(br['id'])})
    return {'break': _ids(br, 'id')}


BREAK_CSV = [{'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name_en', 'header': 'Name'}, {'key': 'date_bs', 'header': 'Date (BS)'},
             {'key': 'kind', 'header': 'Break'}, {'key': 'paid', 'header': 'Paid'}, {'key': 'start', 'header': 'Start'},
             {'key': 'end', 'header': 'End'}, {'key': 'minutes', 'header': 'Minutes'}, {'key': 'max_min', 'header': 'Limit (min)'},
             {'key': 'over', 'header': 'Over limit'}]
VISIT_CSV = [{'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name_en', 'header': 'Name'}, {'key': 'date_bs', 'header': 'Date (BS)'},
             {'key': 'place', 'header': 'Place'}, {'key': 'purpose', 'header': 'Purpose'}, {'key': 'start', 'header': 'Arrived'},
             {'key': 'end', 'header': 'Left'}, {'key': 'minutes', 'header': 'Minutes'}, {'key': 'outcome', 'header': 'Outcome'},
             {'key': 'start_lat', 'header': 'Latitude'}, {'key': 'start_lng', 'header': 'Longitude'}]


def _scope_params(user: AuthUser) -> dict:
    return {'all': has_module(user, 'attendance'), 'me': user.employee_id, 'mgr': has_role(user, 'manager')}


def list_breaks(user: AuthUser, query: dict):
    q = parse(RANGE_QUERY.extend({'overOnly': Enum(('1', 'true'), optional=True), 'format': Enum(('json', 'csv'), optional=True)}), query)
    check_range(q['from'], q['to'])
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    found = rows(f"""
        SELECT b.id, b.employee_id, e.emp_code, e.name_en, b.local_date::text AS local_date, b.kind, b.paid, b.max_min, b.started_at, b.ended_at,
               coalesce(b.minutes, floor(extract(epoch FROM now() - b.started_at) / 60)::int) AS minutes, b.over_limit, b.ended_at IS NULL AS open
          FROM work_break b JOIN employee e ON e.id = b.employee_id
         WHERE b.company_id = %(c)s AND b.local_date BETWEEN %(f)s AND %(t)s AND (%(emp)s::uuid IS NULL OR b.employee_id = %(emp)s::uuid)
           AND (NOT %(over)s OR b.over_limit) AND {_scope_sql('b', 'b.local_date')}
         ORDER BY b.started_at DESC LIMIT 2000""",
                 {'c': company_id_of(user), 'f': q['from'], 't': q['to'], 'emp': q.get('employeeId'), 'over': bool(q.get('overOnly')),
                  **_scope_params(user)})
    for r in found:
        _ids(r, 'id', 'employee_id')
    if q.get('format') == 'csv':
        return {'filename': f"breaks-{q['from']}-{q['to']}", 'columns': BREAK_CSV,
                'rows': [{**x, 'date_bs': bs_label(x['local_date']), 'paid': 'Yes' if x['paid'] else 'No',
                          'over': 'Yes' if x['over_limit'] else 'No', 'start': npt_hm(x['started_at']),
                          'end': npt_hm(x['ended_at']) if x['ended_at'] else ''} for x in found]}
    return {'breaks': found}


# ================================================================ field visits

def start_visit(user: AuthUser, body: dict, ip: str | None = None) -> dict:
    from hajiri.files.store import ParsedFile, save_file
    e = employee_of(user)
    if not e['field_work']:
        raise ForbiddenError('Field visits are not turned on for you. Ask HR.')
    b = parse(Obj({'place': Str(trim=True, min=1, max=160), 'purpose': Str(trim=True, max=500, optional=True),
                   'photo': Str(max=PHOTO_FIELD_MAX, optional=True), **GEO}), body)
    policy = field_policy(e['company_id'])
    photo = parse_photo(b['photo']) if b.get('photo') else None
    if policy['fieldPhotoRequired'] and not photo:
        raise BadRequestError('Take a quick photo at the place you are visiting.', {'code': 'photo_required'})
    if one('SELECT 1 AS x FROM field_visit WHERE employee_id = %s AND ended_at IS NULL', [e['id']]):
        raise ConflictError('You have a visit in progress. End it first.')
    today = local_date_of(to_ms(_now()))
    acc = b.get('accuracyM')
    with transaction.atomic():
        visit = one("""INSERT INTO field_visit (company_id, employee_id, local_date, place, purpose, started_at, start_lat, start_lng, start_accuracy_m)
                       VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s) RETURNING id, place, purpose, started_at""",
                    [e['company_id'], e['id'], today, b['place'], b.get('purpose') or None, b.get('lat'), b.get('lng'),
                     _js_round(acc) if acc is not None else None])
        vid = str(visit['id'])
        if photo:
            f = save_file(company_id=e['company_id'], purpose='field_visit_photo', filename=f'visit-{today}.jpg',
                          file=ParsedFile(photo.mime, photo.data), uploaded_by=user.id, attached_to=visit_aad(vid))
            execute('UPDATE field_visit SET photo_file_id = %s WHERE id = %s', [str(f['id']), vid])
    clocked_in = False
    if policy['fieldVisitClockIn']:
        if not one('SELECT 1 AS x FROM punch_event WHERE employee_id = %s AND local_date = %s LIMIT 1', [e['id'], today]):
            p = insert_app_punch(company_id=e['company_id'], employee_id=e['id'], emp_code=e['emp_code'], direction='in', kind='field',
                                 idempotency=f'visit-in|{vid}',
                                 raw={'field': True, 'visitId': vid, 'place': b['place'], 'lat': b.get('lat'), 'lng': b.get('lng'),
                                      'accuracyM': b.get('accuracyM'), 'remote': False, 'flags': ['Field'], 'ip': ip})
            execute('UPDATE field_visit SET punch_id = %s WHERE id = %s', [p['punchId'], vid])
            clocked_in = True
    hm = npt_hm(_now())
    return {'visit': _ids(visit, 'id'), 'clockedIn': clocked_in,
            'message': f'Visit started and clocked in at {hm}.' if clocked_in else f'Visit started at {hm}.'}


def end_visit(user: AuthUser, visit_id: str, body: dict) -> dict:
    e = employee_of(user)
    vid = parse_uuid(visit_id)
    b = parse(Obj({'outcome': Str(trim=True, max=1000, optional=True), **GEO}), body or {})
    acc = b.get('accuracyM')
    u = one("""UPDATE field_visit SET ended_at = now(), outcome = %s, end_lat = %s, end_lng = %s, end_accuracy_m = %s
                WHERE id = %s AND employee_id = %s AND ended_at IS NULL RETURNING id, place, started_at, ended_at, outcome""",
            [b.get('outcome') or None, b.get('lat'), b.get('lng'), _js_round(acc) if acc is not None else None, vid, e['id']])
    if not u:
        raise NotFoundError('No visit in progress with that id.')
    return {'visit': _ids(u, 'id')}


def list_visits(user: AuthUser, query: dict):
    q = parse(RANGE_QUERY.extend({'format': Enum(('json', 'csv'), optional=True)}), query)
    check_range(q['from'], q['to'])
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    found = rows(f"""
        SELECT v.id, v.employee_id, e.emp_code, e.name_en, e.designation, v.local_date::text AS local_date, v.place, v.purpose, v.started_at,
               v.ended_at, v.outcome, v.start_lat::float8 AS start_lat, v.start_lng::float8 AS start_lng, v.start_accuracy_m,
               v.end_lat::float8 AS end_lat, v.end_lng::float8 AS end_lng, v.end_accuracy_m,
               v.photo_file_id IS NOT NULL AS has_photo, v.punch_id IS NOT NULL AS opened_day,
               floor(extract(epoch FROM coalesce(v.ended_at, now()) - v.started_at) / 60)::int AS minutes
          FROM field_visit v JOIN employee e ON e.id = v.employee_id
         WHERE v.company_id = %(c)s AND v.local_date BETWEEN %(f)s AND %(t)s AND (%(emp)s::uuid IS NULL OR v.employee_id = %(emp)s::uuid)
           AND {_scope_sql('v', 'v.local_date')}
         ORDER BY v.started_at DESC LIMIT 2000""",
                 {'c': company_id_of(user), 'f': q['from'], 't': q['to'], 'emp': q.get('employeeId'), **_scope_params(user)})
    for r in found:
        _ids(r, 'id', 'employee_id')
    if q.get('format') == 'csv':
        return {'filename': f"field-visits-{q['from']}-{q['to']}", 'columns': VISIT_CSV,
                'rows': [{**x, 'date_bs': bs_label(x['local_date']), 'start': npt_hm(x['started_at']),
                          'end': npt_hm(x['ended_at']) if x['ended_at'] else ''} for x in found]}
    return {'visits': found}


def visit_photo(user: AuthUser, visit_id: str) -> dict:
    from hajiri.files.store import read_file
    vid = parse_uuid(visit_id)
    company_id = company_id_of(user)
    row = one('SELECT employee_id, photo_file_id FROM field_visit WHERE id = %s AND company_id = %s', [vid, company_id])
    if not row:
        raise NotFoundError('visit not found')
    assert_manages(user, str(row['employee_id']), 'attendance')
    if not row['photo_file_id']:
        raise NotFoundError('this visit has no photo')
    f = read_file(company_id, str(row['photo_file_id']))
    if user.employee_id != str(row['employee_id']):
        audit('view_photo', 'field_visit', actor_id=user.id, row_id=vid)
    return {'mime': f['mime'], 'data': f['data']}


# ================================================================ locations and staff

LOCATION_BODY = Obj({
    'name': Str(trim=True, min=1, max=80), 'address': Str(trim=True, max=300, nullish=True), 'branchId': Uuid(nullish=True),
    'lat': Num(min=-90, max=90), 'lng': Num(min=-180, max=180), 'radiusM': Int(min=20, max=5000, default=150),
    'active': Bool(default=True),
})


def _assert_branch(company_id: str, branch_id) -> None:
    if branch_id and not one('SELECT 1 AS x FROM branch WHERE id = %s AND company_id = %s', [branch_id, company_id]):
        raise NotFoundError('branch not found')


def _assert_location(company_id: str, location_id) -> None:
    if location_id and not one('SELECT 1 AS x FROM location WHERE id = %s AND company_id = %s', [location_id, company_id]):
        raise NotFoundError('location not found')


def locations(user: AuthUser) -> dict:
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    found = rows("""SELECT l.id, l.name, l.address, l.branch_id, b.name_en AS branch_name, l.lat::float8 AS lat, l.lng::float8 AS lng,
                           l.radius_m, l.active,
                           coalesce((SELECT array_agg(employee_id) FROM location_assignment a WHERE a.location_id = l.id), '{}') AS employee_ids
                      FROM location l LEFT JOIN branch b ON b.id = l.branch_id WHERE l.company_id = %s ORDER BY l.active DESC, l.name""",
                 [company_id_of(user)])
    for r in found:
        _ids(r, 'id', 'branch_id')
        r['employee_ids'] = [str(x) for x in r['employee_ids'] or []]
    return {'locations': found}


def _loc_audit(b: dict) -> dict:
    return {k: v for k, v in b.items()}


def create_location(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(LOCATION_BODY, body)
    _assert_branch(company_id, b.get('branchId'))
    with transaction.atomic():
        ins = one("""INSERT INTO location (company_id, branch_id, name, address, lat, lng, radius_m, active)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [company_id, b.get('branchId'), b['name'], b.get('address') or None, b['lat'], b['lng'], b['radiusM'], b['active']])
        audit('create', 'location', actor_id=user.id, row_id=str(ins['id']), after=_loc_audit(b))
    return {'id': str(ins['id'])}


def update_location(user: AuthUser, location_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    lid = parse_uuid(location_id)
    b = parse(LOCATION_BODY, body)
    _assert_branch(company_id, b.get('branchId'))
    with transaction.atomic():
        n = execute("""UPDATE location SET name = %s, address = %s, branch_id = %s, lat = %s, lng = %s, radius_m = %s, active = %s
                        WHERE id = %s AND company_id = %s""",
                    [b['name'], b.get('address') or None, b.get('branchId'), b['lat'], b['lng'], b['radiusM'], b['active'], lid, company_id])
        if not n:
            raise NotFoundError('location not found')
        audit('update', 'location', actor_id=user.id, row_id=lid, after=_loc_audit(b))
    return {'ok': True}


def delete_location(user: AuthUser, location_id: str) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    with transaction.atomic():
        if not execute('DELETE FROM location WHERE id = %s AND company_id = %s', [lid, company_id_of(user)]):
            raise NotFoundError('location not found')
        audit('delete', 'location', actor_id=user.id, row_id=lid)
    return {'ok': True}


def staff(user: AuthUser) -> dict:
    """Staff settings for field work: the field-work switch, work from home, and locations tied to a person."""
    _require_module(user, 'attendance')
    found = rows("""SELECT e.id, e.emp_code, e.name_en, e.designation, e.field_work, e.work_from_home, b.name_en AS branch_name,
                           coalesce((SELECT array_agg(location_id) FROM location_assignment la WHERE la.employee_id = e.id), '{}') AS location_ids,
                           (c.pin_hash IS NOT NULL) AS kiosk_pin_set, (c.locked_until IS NOT NULL AND c.locked_until > now()) AS kiosk_pin_locked
                      FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                      LEFT JOIN branch b ON b.id = a.branch_id
                      LEFT JOIN kiosk_credential c ON c.employee_id = e.id
                     WHERE e.company_id = %s AND e.status = 'active' ORDER BY e.emp_code""", [company_id_of(user)])
    for r in found:
        _ids(r, 'id')
        r['location_ids'] = [str(x) for x in r['location_ids'] or []]
    return {'staff': found}


def update_staff(user: AuthUser, employee_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    b = parse(Obj({'fieldWork': Bool(optional=True), 'locationIds': Arr(Uuid(), max=50, optional=True)}), body)
    _assert_employee_in_company(company_id, eid)
    with transaction.atomic():
        if 'fieldWork' in b:
            execute('UPDATE employee SET field_work = %s WHERE id = %s', [b['fieldWork'], eid])
        if 'locationIds' in b:
            ids = list(dict.fromkeys(b['locationIds']))
            own = one('SELECT count(*)::int AS n FROM location WHERE company_id = %s AND id = ANY(%s::uuid[])', [company_id, ids])
            if own['n'] != len(ids):
                raise NotFoundError('location not found')
            execute('DELETE FROM location_assignment WHERE employee_id = %s', [eid])
            for loc in ids:
                execute('INSERT INTO location_assignment (location_id, employee_id, company_id) VALUES (%s, %s, %s)', [loc, eid, company_id])
        audit('update', 'employee', actor_id=user.id, row_id=eid, after={'field': b})
    return {'ok': True}


# ================================================================ kiosks (HR)

KIOSK_BODY = Obj({'name': Str(trim=True, min=1, max=80), 'branchId': Uuid(nullish=True), 'locationId': Uuid(nullish=True),
                  'selfieRequired': Bool(default=True)})


def _new_pairing(kiosk_id: str) -> dict:
    from hajiri.kiosk.tokens import pairing_code
    code = pairing_code()
    expires = _now() + timedelta(minutes=PAIRING_TTL_MIN)
    execute('UPDATE kiosk SET pairing_code_hash = %s, pairing_expires_at = %s WHERE id = %s',
            [sha256_hex(f"kiosk-pair|{code.replace('-', '', 1)}"), expires, kiosk_id])
    return {'pairingCode': code, 'expiresAt': expires}


def kiosks(user: AuthUser) -> dict:
    _require_module(user, 'attendance')
    found = rows("""SELECT k.id, k.name, k.branch_id, b.name_en AS branch_name, k.location_id, l.name AS location_name, k.selfie_required, k.status,
                           k.paired_at, k.last_seen_at, k.created_at, k.revoked_at, (k.locked_until IS NOT NULL AND k.locked_until > now()) AS locked,
                           (k.pairing_expires_at IS NOT NULL AND k.pairing_expires_at > now()) AS pairing_open, k.pairing_expires_at,
                           (SELECT count(*)::int FROM punch_event p WHERE p.source = 'kiosk' AND p.raw->>'kioskId' = k.id::text
                              AND p.local_date = (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS punches_today
                      FROM kiosk k LEFT JOIN branch b ON b.id = k.branch_id LEFT JOIN location l ON l.id = k.location_id
                     WHERE k.company_id = %s ORDER BY k.status = 'revoked', k.created_at DESC""", [company_id_of(user)])
    for r in found:
        _ids(r, 'id', 'branch_id', 'location_id')
    return {'kiosks': found}


def kiosk_detail(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    k = one('SELECT id, name, branch_id, location_id, selfie_required, status FROM kiosk WHERE id = %s AND company_id = %s',
            [parse_uuid(kiosk_id), company_id_of(user)])
    if not k:
        raise NotFoundError('kiosk not found')
    return _ids(k, 'id', 'branch_id', 'location_id')


def create_kiosk(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(KIOSK_BODY, body)
    _assert_branch(company_id, b.get('branchId'))
    _assert_location(company_id, b.get('locationId'))
    with transaction.atomic():
        ins = one("""INSERT INTO kiosk (company_id, name, branch_id, location_id, selfie_required, created_by)
                     VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                  [company_id, b['name'], b.get('branchId'), b.get('locationId'), b['selfieRequired'], user.id])
        kid = str(ins['id'])
        pairing = _new_pairing(kid)
        audit('create', 'kiosk', actor_id=user.id, row_id=kid, after=b)
    return {'id': kid, **pairing}


def update_kiosk(user: AuthUser, kiosk_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    kid = parse_uuid(kiosk_id)
    b = parse(KIOSK_BODY, body)
    _assert_branch(company_id, b.get('branchId'))
    _assert_location(company_id, b.get('locationId'))
    with transaction.atomic():
        if not execute('UPDATE kiosk SET name = %s, branch_id = %s, location_id = %s, selfie_required = %s WHERE id = %s AND company_id = %s',
                       [b['name'], b.get('branchId'), b.get('locationId'), b['selfieRequired'], kid, company_id]):
            raise NotFoundError('kiosk not found')
        audit('update', 'kiosk', actor_id=user.id, row_id=kid, after=b)
    return {'ok': True}


def kiosk_pairing_code(user: AuthUser, kiosk_id: str) -> dict:
    """A fresh one-time code (to pair a replacement tablet); the old tablet keeps working until the new one pairs."""
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    k = one('SELECT status FROM kiosk WHERE id = %s AND company_id = %s', [kid, company_id_of(user)])
    if not k:
        raise NotFoundError('kiosk not found')
    if k['status'] == 'revoked':
        raise ConflictError('This kiosk was revoked. Add a new kiosk instead.')
    with transaction.atomic():
        pairing = _new_pairing(kid)
        audit('kiosk_pairing_code', 'kiosk', actor_id=user.id, row_id=kid)
    return pairing


def revoke_kiosk(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    with transaction.atomic():
        if not execute("""UPDATE kiosk SET status = 'revoked', key_hash = NULL, pairing_code_hash = NULL, pairing_expires_at = NULL, revoked_at = now()
                           WHERE id = %s AND company_id = %s AND status <> 'revoked'""", [kid, company_id_of(user)]):
            raise NotFoundError('kiosk not found')
        audit('kiosk_revoked', 'kiosk', actor_id=user.id, row_id=kid)
    return {'ok': True}


def unlock_kiosk(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    with transaction.atomic():
        if not execute('UPDATE kiosk SET locked_until = NULL, failed_attempts = 0 WHERE id = %s AND company_id = %s', [kid, company_id_of(user)]):
            raise NotFoundError('kiosk not found')
        audit('kiosk_unlocked', 'kiosk', actor_id=user.id, row_id=kid)
    return {'ok': True}


# ================================================================ kiosk PIN and QR badge

PIN = Str(regex=r'^\d{4,6}$', regex_message='PIN is 4 to 6 digits')


def set_kiosk_pin(user: AuthUser, body: dict) -> dict:
    from hajiri.kiosk.tokens import allow, weak_pin
    e = employee_of(user)
    if not allow(f"pin-set|{e['id']}", 10, 60 * 60_000):
        raise TooManyError('Too many PIN changes. Try again later.')
    b = parse(Obj({'pin': PIN}), body)
    if weak_pin(b['pin']):
        raise BadRequestError('That PIN is too easy to guess. Avoid repeated or consecutive digits.')
    with transaction.atomic():
        execute("""INSERT INTO kiosk_credential (employee_id, company_id, pin_hash) VALUES (%s, %s, %s)
                   ON CONFLICT (employee_id) DO UPDATE SET pin_hash = EXCLUDED.pin_hash, failed_count = 0, locked_until = NULL, updated_at = now()""",
                [e['id'], e['company_id'], hash_password(b['pin'])])
        audit('kiosk_pin_set', 'kiosk_credential', actor_id=user.id, row_id=e['id'])
    return {'ok': True}


def reset_kiosk_pin(user: AuthUser, employee_id: str) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    _assert_employee_in_company(company_id, eid)
    with transaction.atomic():
        execute("""INSERT INTO kiosk_credential (employee_id, company_id) VALUES (%s, %s)
                   ON CONFLICT (employee_id) DO UPDATE SET pin_hash = NULL, failed_count = 0, locked_until = NULL,
                       qr_version = kiosk_credential.qr_version + 1, updated_at = now()""", [eid, company_id])
        audit('kiosk_pin_reset', 'kiosk_credential', actor_id=user.id, row_id=eid)
        uid = user_id_for_employee(eid)
        if uid:
            notify(company_id=company_id, user_id=uid, employee_id=eid, template='kiosk.pin_reset', title='Kiosk PIN reset',
                   body='HR reset your kiosk PIN. Set a new one under My attendance, Kiosk badge.')
    return {'ok': True}


def kiosk_qr(user: AuthUser) -> dict:
    """The rotating QR badge: a fresh signed code every 30 seconds, accepted once by a kiosk."""
    from hajiri.kiosk.tokens import QR_WINDOW_MS, qr_token, qr_window
    e = employee_of(user)
    c = one("""INSERT INTO kiosk_credential (employee_id, company_id) VALUES (%s, %s)
               ON CONFLICT (employee_id) DO UPDATE SET employee_id = EXCLUDED.employee_id RETURNING qr_version""",
            [e['id'], e['company_id']])
    now_ms = to_ms(_now())
    w = qr_window(now_ms)
    return {'token': qr_token(e['id'], c['qr_version'], w), 'refreshInMs': (w + 1) * QR_WINDOW_MS - now_ms, 'empCode': e['emp_code']}


# ================================================================ my roster and shift swaps

def _shift_info(company_id: str) -> dict[str, dict]:
    s = rows("""SELECT id, code, name_en, to_char(start_time, 'HH24:MI') AS start, to_char(end_time, 'HH24:MI') AS "end"
                  FROM shift WHERE company_id = %s""", [company_id])
    return {str(x['id']): {**x, 'id': str(x['id'])} for x in s}


def my_roster(user: AuthUser, query: dict) -> dict:
    e = employee_of(user)
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate()}), query)
    check_range(q['from'], q['to'], 62)
    shifts = _shift_info(e['company_id'])
    hol = rows("""SELECT h.local_date::text AS local_date, h.name_en FROM holiday h WHERE h.company_id = %(c)s AND h.local_date BETWEEN %(f)s AND %(t)s
                    AND (h.branch_id IS NULL OR h.branch_id = (SELECT branch_id FROM employee_assignment WHERE employee_id = %(e)s
                                                               AND valid @> h.local_date))""",
               {'c': e['company_id'], 'f': q['from'], 't': q['to'], 'e': e['id']})
    days = []
    for d in date_range(q['from'], q['to']):
        eff = effective_shift(e['id'], d)
        days.append({'date': d, 'weekday': weekday_of(d), 'source': eff['source'],
                     'shift': shifts.get(eff['shiftId']) if eff['shiftId'] else None,
                     'holidays': [h['name_en'] for h in hol if h['local_date'] == d]})
    return {'days': days}


def swap_colleagues(user: AuthUser, query: dict) -> dict:
    """Colleagues one could swap with on a date (same manager or same branch), with what they work that day."""
    e = employee_of(user)
    q = parse(Obj({'date': IsoDate()}), query)
    found = rows("""SELECT e.id, e.emp_code, e.name_en, e.designation FROM employee e
                      JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(d)s::date
                      JOIN employee_assignment me ON me.employee_id = %(e)s AND me.valid @> %(d)s::date
                     WHERE e.company_id = %(c)s AND e.status = 'active' AND e.id <> %(e)s
                       AND (a.manager_id IS NOT DISTINCT FROM me.manager_id OR a.branch_id IS NOT DISTINCT FROM me.branch_id)
                     ORDER BY e.name_en LIMIT 200""", {'c': e['company_id'], 'e': e['id'], 'd': q['date']})
    shifts = _shift_info(e['company_id'])
    mine = effective_shift(e['id'], q['date'])
    out = []
    for c in found:
        eff = effective_shift(str(c['id']), q['date'])
        out.append({**_ids(c, 'id'), 'shift': shifts.get(eff['shiftId']) if eff['shiftId'] else None, 'off': not eff['shiftId'],
                    'sameAsMine': eff['shiftId'] == mine['shiftId']})
    return {'mine': {'shift': shifts.get(mine['shiftId']) if mine['shiftId'] else None, 'off': not mine['shiftId']}, 'colleagues': out}


SWAP_SELECT = """SELECT s.id, s.local_date::text AS local_date, s.status, s.reason, s.colleague_note, s.created_at, s.decided_at, s.workflow_instance_id,
           s.requester_id, r.name_en AS requester_name, r.emp_code AS requester_code, s.colleague_id, c.name_en AS colleague_name,
           c.emp_code AS colleague_code, rs.code AS requester_shift_code, rs.name_en AS requester_shift_name, cs.code AS colleague_shift_code,
           cs.name_en AS colleague_shift_name
      FROM shift_swap s JOIN employee r ON r.id = s.requester_id JOIN employee c ON c.id = s.colleague_id
      LEFT JOIN shift rs ON rs.id = s.requester_shift_id LEFT JOIN shift cs ON cs.id = s.colleague_shift_id"""


def swaps(user: AuthUser, query: dict) -> dict:
    q = parse(Obj({'scope': Enum(('mine', 'team'), default='mine'), 'from': IsoDate(optional=True), 'to': IsoDate(optional=True)}), query)
    company_id = company_id_of(user)
    if q['scope'] == 'team':
        if not has_role(user, *MODULE_ROLES['attendance'], 'manager'):
            raise ForbiddenError('only managers and HR see team swaps')
        found = rows(f"""{SWAP_SELECT} WHERE s.company_id = %(c)s AND (%(f)s::date IS NULL OR s.local_date >= %(f)s::date)
                          AND (%(t)s::date IS NULL OR s.local_date <= %(t)s::date)
                          AND (%(all)s OR (hj_manages(%(me)s::uuid, s.requester_id, s.local_date) OR hj_manages(%(me)s::uuid, s.colleague_id, s.local_date)))
                        ORDER BY s.local_date DESC, s.created_at DESC LIMIT 500""",
                     {'c': company_id, 'f': q.get('from'), 't': q.get('to'), 'all': has_module(user, 'attendance'), 'me': user.employee_id})
    elif not user.employee_id:
        return {'swaps': []}
    else:
        found = rows(f"""{SWAP_SELECT} WHERE s.company_id = %s AND (s.requester_id = %s OR s.colleague_id = %s)
                         ORDER BY s.local_date DESC, s.created_at DESC LIMIT 200""", [company_id, user.employee_id, user.employee_id])
    for r in found:
        _ids(r, 'id', 'workflow_instance_id', 'requester_id', 'colleague_id')
    return {'swaps': found}


def request_swap(user: AuthUser, body: dict) -> dict:
    from django.db import IntegrityError
    e = employee_of(user)
    b = parse(Obj({'colleagueId': Uuid(), 'localDate': IsoDate(), 'reason': Str(trim=True, max=500, optional=True)}), body)
    if b['localDate'] < local_date_of(to_ms(_now())):
        raise BadRequestError('Pick today or a later day.')
    if b['colleagueId'] == e['id']:
        raise BadRequestError('Pick a colleague to swap with.')
    if not one("SELECT id, name_en FROM employee WHERE id = %s AND company_id = %s AND status = 'active'", [b['colleagueId'], e['company_id']]):
        raise NotFoundError('colleague not found')
    for i in (e['id'], b['colleagueId']):
        if is_attendance_locked(i, b['localDate']):
            raise ConflictError('attendance for that day is locked by payroll')
    mine = effective_shift(e['id'], b['localDate'])
    theirs = effective_shift(b['colleagueId'], b['localDate'])
    if mine['shiftId'] == theirs['shiftId']:
        raise BadRequestError('You are both on the same shift that day.' if mine['shiftId'] else 'You are both off that day.')
    try:
        with transaction.atomic():
            ins = one("""INSERT INTO shift_swap (company_id, requester_id, colleague_id, local_date, requester_shift_id, colleague_shift_id, reason)
                         VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                      [e['company_id'], e['id'], b['colleagueId'], b['localDate'], mine['shiftId'], theirs['shiftId'], b.get('reason') or None])
    except IntegrityError as err:
        if getattr(getattr(err, '__cause__', None), 'sqlstate', None) == '23505' or 'unique' in str(err).lower() or 'duplicate' in str(err).lower():
            raise ConflictError('You already have a swap request open for that day.') from None
        raise
    sid = str(ins['id'])
    uid = user_id_for_employee(b['colleagueId'])
    if uid:
        notify(company_id=e['company_id'], user_id=uid, employee_id=b['colleagueId'], template='shift_swap.asked', title='Shift swap request',
               body=f"{e['name_en']} asks to swap shifts with you on {bs_label(b['localDate'])}.", payload={'swapId': sid}, channels=['push'])
    return {'id': sid}


def _load_swap(swap_id: str, company_id: str) -> dict:
    s = one("""SELECT s.id, s.company_id, s.requester_id, s.colleague_id, s.local_date::text AS local_date, s.status,
                      r.name_en AS requester_name, c.name_en AS colleague_name
                 FROM shift_swap s JOIN employee r ON r.id = s.requester_id JOIN employee c ON c.id = s.colleague_id
                WHERE s.id = %s AND s.company_id = %s""", [swap_id, company_id])
    if not s:
        raise NotFoundError('swap request not found')
    return _ids(s, 'id', 'company_id', 'requester_id', 'colleague_id')


NOTE = Obj({'note': Str(trim=True, max=500, optional=True)})


def accept_swap(user: AuthUser, swap_id: str, body: dict | None = None) -> dict:
    from hajiri.workflow.service import start_workflow
    e = employee_of(user)
    sid = parse_uuid(swap_id)
    b = parse(NOTE, body or {})
    s = _load_swap(sid, e['company_id'])
    if s['colleague_id'] != e['id']:
        raise NotFoundError('swap request not found')
    if s['status'] != 'awaiting_colleague':
        raise ConflictError('This request was already answered.')
    with transaction.atomic():
        inst = start_workflow(company_id=s['company_id'], object_type='shift_swap', object_id=s['id'], employee_id=s['requester_id'],
                              title='Shift swap to approve',
                              body=f"{s['requester_name']} and {s['colleague_name']} want to swap shifts on {bs_label(s['local_date'])}.")
        execute("UPDATE shift_swap SET status = 'pending', colleague_note = %s, workflow_instance_id = %s WHERE id = %s",
                [b.get('note') or None, inst['id'], s['id']])
        uid = user_id_for_employee(s['requester_id'])
        if uid:
            notify(company_id=s['company_id'], user_id=uid, employee_id=s['requester_id'], template='shift_swap.accepted', title='Swap accepted',
                   body=f"{s['colleague_name']} agreed to swap on {bs_label(s['local_date'])}. It now needs your manager's approval.")
    return {'status': 'pending'}


def decline_swap(user: AuthUser, swap_id: str, body: dict | None = None) -> dict:
    e = employee_of(user)
    sid = parse_uuid(swap_id)
    b = parse(NOTE, body or {})
    s = _load_swap(sid, e['company_id'])
    if s['colleague_id'] != e['id']:
        raise NotFoundError('swap request not found')
    if s['status'] != 'awaiting_colleague':
        raise ConflictError('This request was already answered.')
    with transaction.atomic():
        execute("UPDATE shift_swap SET status = 'declined', colleague_note = %s, decided_at = now() WHERE id = %s", [b.get('note') or None, s['id']])
        uid = user_id_for_employee(s['requester_id'])
        if uid:
            notify(company_id=s['company_id'], user_id=uid, employee_id=s['requester_id'], template='shift_swap.declined', title='Swap declined',
                   body=f"{s['colleague_name']} can't swap on {bs_label(s['local_date'])}.")
    return {'status': 'declined'}


def cancel_swap(user: AuthUser, swap_id: str) -> dict:
    from hajiri.workflow.service import cancel_workflow
    e = employee_of(user)
    sid = parse_uuid(swap_id)
    s = _load_swap(sid, e['company_id'])
    if s['requester_id'] != e['id']:
        raise NotFoundError('swap request not found')
    if s['status'] not in ('awaiting_colleague', 'pending'):
        raise ConflictError('This request is already closed.')
    with transaction.atomic():
        execute("UPDATE shift_swap SET status = 'cancelled', decided_at = now() WHERE id = %s", [s['id']])
        cancel_workflow('shift_swap', s['id'])
    return {'status': 'cancelled'}
