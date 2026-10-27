"""
Attendance corrections (port of apps/api/src/modules/regularisation/routes.ts, plan §5.4): missed punches, work from
home, on duty, half day and shift change requests, approved through the workflow engine. An approved missed punch
becomes a real, attributable punch_event row; every approval queues a recompute of the day.

Also the HR "Fix a punch" entry (POST /punch/manual in the TS API), which the Fix a punch page uses for HR, and the
offline clock-in flush (lib/offlinePunch.ts), which files a missed-punch correction for a clock-in made without a
connection.

Bodies use the TS camelCase shape, so the JSON endpoints, the pages and the tests share one code path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from django.db import transaction

from engines.attendance import MIN_MS, NPT_OFFSET_MIN, local_date_of, local_time_ms

from hajiri.core import clock
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_module, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import DateTimeStr, Enum, Hhmm, IsoDate, Obj, Str, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import (DecisionContext, act, assert_manages, cancel_workflow, register_handler,
                                     start_workflow)

KINDS = ('missed_punch', 'wfh', 'on_duty', 'half_day', 'shift_change')
STATUSES = ('pending', 'approved', 'rejected', 'cancelled')
OBJECT_TYPE = 'regularisation_request'

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_ms(dt: datetime) -> int:
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _days_between(a: str, b: str) -> int:
    """b - a in whole days for two ISO dates."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def kind_label(kind: str) -> str:
    """'Work from home' or the kind with its first underscore as a space ('missed punch'), as the TS text."""
    return 'Work from home' if kind == 'wfh' else str(kind).replace('_', ' ', 1)


def npt_today() -> str:
    return clock.npt_today().isoformat()


def _parse_instant(s: str) -> datetime:
    v = s[:-1] + '+00:00' if s.endswith('Z') else s
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise BadRequestError('punchTime must be an ISO date-time')
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- devices

def manual_device_id(company_id: str) -> str:
    """Device row used for HR-entered and regularised punches (one per company; vendor/protocol 'manual')."""
    c = one('SELECT code FROM company WHERE id = %s', [company_id])
    sn = f"MANUAL-{c['code'] if c else str(company_id)[:8]}"
    r = one("""INSERT INTO device (company_id, serial_no, vendor, protocol, name, status)
               VALUES (%s, %s, 'manual', 'manual', 'Manual entries', 'active')
               ON CONFLICT (serial_no) DO UPDATE SET status = 'active' RETURNING id""", [company_id, sn])
    return str(r['id'])


def _device_local_time(at: datetime) -> str:
    d = _from_ms(_to_ms(at) + NPT_OFFSET_MIN * MIN_MS)
    return d.strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- decision handler

def apply_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """Applies an approved regularisation (plan §5.4): a missed punch becomes a real, attributable punch_event row."""
    from hajiri.attendance.service import enqueue_recompute, is_attendance_locked

    rr = one("""SELECT rr.employee_id, rr.local_date, rr.kind, rr.punch_time, rr.punch_direction, rr.status, e.company_id, e.emp_code
                  FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id WHERE rr.id = %s FOR UPDATE""",
             [str(request_id)])
    if not rr:
        raise NotFoundError('regularisation request not found')
    if rr['status'] != 'pending':
        raise ConflictError(f"request already {rr['status']}")
    employee_id, company_id = str(rr['employee_id']), str(rr['company_id'])
    local_date = rr['local_date'].isoformat() if isinstance(rr['local_date'], date) else str(rr['local_date'])[:10]
    if state == 'approved':
        # Payroll may have locked the day after the request was filed; a correction must not slip into a locked period.
        if is_attendance_locked(employee_id, local_date):
            raise ConflictError('this day is now locked by payroll; reject the request and let HR raise a supplementary correction')
    execute('UPDATE regularisation_request SET status = %s, decided_at = now() WHERE id = %s', [state, str(request_id)])
    if state == 'approved':
        if rr['kind'] == 'missed_punch' and rr['punch_time']:
            device_id = manual_device_id(company_id)
            key = hashlib.sha256(f'regularisation|{request_id}'.encode()).hexdigest()
            at: datetime = rr['punch_time']
            p = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                                                local_date, direction, verify_mode, source, raw, idempotency_key)
                       SELECT %s, d.serial_no, 'manual', %s, %s, %s, %s, %s, %s, 'manual', 'regularisation', %s, %s FROM device d WHERE d.id = %s
                       ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id""",
                    [device_id, rr['emp_code'], employee_id, at, _device_local_time(at), local_date_of(_to_ms(at)),
                     rr['punch_direction'] or 'unknown', json.dumps({'requestId': str(request_id), 'approvedBy': ctx.actor_id}),
                     key, device_id])
            if p:
                execute('UPDATE regularisation_request SET resulting_punch_id = %s WHERE id = %s', [p['id'], str(request_id)])
        enqueue_recompute([(employee_id, local_date)], 'regularisation_approved')
    notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id,
           template=f'regularisation.{state}',
           title='Attendance correction approved' if state == 'approved' else 'Attendance correction rejected',
           body=f"{kind_label(rr['kind'])} for {bs_label(local_date)} was {state}{': ' + ctx.reason if ctx.reason else ''}.",
           payload={'requestId': str(request_id)}, channels=('push',))


register_handler(OBJECT_TYPE, apply_decision)


# ---------------------------------------------------------------- policy

def policy(company_id: str) -> dict:
    """{'backdateDays', 'monthlyCap'} from the company's attendance.policy (defaults 30 and 5)."""
    s = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'attendance.policy'", [company_id])
    v = (s or {}).get('value') or {}
    if not isinstance(v, dict):
        v = {}

    def num(k, d):
        x = v.get(k)
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else d
    return {'backdateDays': num('regularisationBackdateDays', 30), 'monthlyCap': num('regularisationMonthlyCap', 5)}


# ---------------------------------------------------------------- create

CREATE = Obj({
    'employeeId': Uuid(optional=True), 'localDate': IsoDate(), 'kind': Enum(KINDS),
    'punchTime': DateTimeStr(optional=True), 'punchDirection': Enum(('in', 'out'), optional=True),
    'shiftId': Uuid(optional=True), 'reason': Str(min=3, max=1000), 'attachmentKey': Str(max=200, optional=True),
})


def create_request(user: AuthUser, body: dict) -> dict:
    """POST /regularisation: {'id', 'status': 'pending' | 'approved'}."""
    from hajiri.attendance.service import is_attendance_locked, recompute_queued_for

    b = parse(CREATE, body)
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    employee_id = str(employee_id)
    on_behalf = employee_id != (str(user.employee_id) if user.employee_id else None)
    emp = one('SELECT company_id, status FROM employee WHERE id = %s', [employee_id])
    if not emp or str(emp['company_id']) != str(user.company_id):
        raise NotFoundError('employee not found')
    company_id = str(emp['company_id'])
    if on_behalf:
        assert_manages(user, employee_id, 'attendance')
    if b['kind'] == 'missed_punch' and (not b.get('punchTime') or not b.get('punchDirection')):
        raise BadRequestError('punchTime and punchDirection are required for a missed punch')
    if b['kind'] == 'shift_change':
        if not b.get('shiftId'):
            raise BadRequestError('shiftId is required for a shift change')
        if not one('SELECT 1 AS x FROM shift WHERE id = %s AND company_id = %s', [b['shiftId'], user.company_id]):
            raise NotFoundError('shift not found')
    punch_at = None
    if b.get('punchTime'):
        # Night shifts: the OUT punch may fall on the next calendar day (plan §10.1 attribution to the start date); never earlier.
        punch_at = _parse_instant(b['punchTime'])
        diff = _days_between(b['localDate'], local_date_of(_to_ms(punch_at)))
        if diff < 0 or diff > 1:
            raise BadRequestError('punchTime must be on the attendance date or the following day')
    today = npt_today()
    days_ahead = _days_between(today, b['localDate'])
    # Work-from-home days are planned ahead (up to 60 days); every other correction is for a day that has happened.
    if (days_ahead > 60) if b['kind'] == 'wfh' else (days_ahead > 0):
        raise BadRequestError('work from home can be requested up to 60 days ahead' if b['kind'] == 'wfh'
                              else 'cannot regularise a future date')
    pol = policy(company_id)
    is_hr = has_module(user, 'attendance')
    if not is_hr:
        age_days = _days_between(b['localDate'], today)
        if age_days > pol['backdateDays']:
            raise BadRequestError(f"corrections can only be requested within {pol['backdateDays']} days")
        month = b['localDate'][:7]
        if b['kind'] != 'wfh':
            # The cap limits what an employee files for themself; HR/manager corrections (device outages) do not count against it.
            cnt = one("""SELECT count(*)::int AS n FROM regularisation_request WHERE employee_id = %s AND to_char(local_date, 'YYYY-MM') = %s
                          AND status IN ('pending', 'approved') AND created_by = %s AND kind <> 'wfh'""", [employee_id, month, user.id])
            if cnt['n'] >= pol['monthlyCap']:
                raise BadRequestError(f"monthly limit of {pol['monthlyCap']} corrections reached; contact HR")
    if is_attendance_locked(employee_id, b['localDate']):
        raise ConflictError('this day is locked by payroll; HR must raise a supplementary correction')
    if one('SELECT 1 AS x FROM regularisation_request WHERE employee_id = %s AND local_date = %s AND kind = %s AND status = %s',
           [employee_id, b['localDate'], b['kind'], 'pending']):
        raise ConflictError('a pending request of this kind already exists for that day')

    state = 'pending'
    with transaction.atomic():
        ins = one("""INSERT INTO regularisation_request (employee_id, local_date, kind, punch_time, punch_direction, shift_id, reason,
                                                         attachment_key, created_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [employee_id, b['localDate'], b['kind'], punch_at, b.get('punchDirection'), b.get('shiftId'), b['reason'],
                   b.get('attachmentKey'), user.id])
        rid = str(ins['id'])
        inst = start_workflow(company_id=company_id, object_type=OBJECT_TYPE, object_id=rid, employee_id=employee_id,
                              title='Attendance correction to approve',
                              body=f"{kind_label(b['kind'])} on {bs_label(b['localDate'])}: {b['reason']}")
        execute('UPDATE regularisation_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], rid])
        audit('create', 'regularisation_request', actor_id=user.id, row_id=rid,
              after={**b, 'employeeId': employee_id, 'onBehalf': on_behalf})
        # HR fixing a punch on behalf of a worker is approved immediately but still leaves a signed action (plan §3 "Fix punch (audited)").
        if is_hr and on_behalf and b['kind'] == 'missed_punch':
            state = act(instance_id=inst['id'], user=user, decision='approve', reason='fixed by HR on behalf of the employee')
    if state == 'approved':
        recompute_queued_for(employee_id)
    return {'id': rid, 'status': 'approved' if state == 'approved' else 'pending'}


# ---------------------------------------------------------------- list

LIST = Obj({'employeeId': Uuid(optional=True), 'status': Enum(STATUSES, optional=True), 'from': IsoDate(optional=True),
            'to': IsoDate(optional=True), 'scope': Enum(('own', 'team', 'all'), optional=True)})


def list_requests(user: AuthUser, query: dict) -> list[dict]:
    """GET /regularisation: own, team or (attendance area) all requests, newest first, at most 500."""
    q = parse(LIST, {k: v for k, v in query.items() if v not in (None, '')})
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    return rows("""
        SELECT rr.id, rr.employee_id, e.emp_code, e.name_en, rr.local_date, rr.kind, rr.punch_time, rr.punch_direction, rr.shift_id,
               rr.reason, rr.attachment_key, rr.status, rr.workflow_instance_id, rr.resulting_punch_id, rr.created_by, rr.created_at,
               rr.decided_at
          FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
         WHERE e.company_id = %(c)s AND (%(e)s::uuid IS NULL OR rr.employee_id = %(e)s::uuid) AND (%(s)s::text IS NULL OR rr.status = %(s)s)
           AND (%(f)s::date IS NULL OR rr.local_date >= %(f)s::date) AND (%(t)s::date IS NULL OR rr.local_date <= %(t)s::date)
           AND CASE %(scope)s WHEN 'own' THEN rr.employee_id = %(me)s::uuid
                              WHEN 'team' THEN (hj_manages(%(me)s::uuid, rr.employee_id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR rr.employee_id = %(me)s::uuid)
                              ELSE true END
         ORDER BY rr.created_at DESC LIMIT 500""",
                {'c': user.company_id, 'e': q.get('employeeId'), 's': q.get('status'), 'f': q.get('from'), 't': q.get('to'),
                 'scope': scope, 'me': user.employee_id})


# ---------------------------------------------------------------- cancel

def cancel_request(user: AuthUser, request_id: str) -> dict:
    """POST /regularisation/:id/cancel: only while pending."""
    rid = parse_uuid(str(request_id))
    rr = one("""SELECT rr.employee_id, rr.status, e.company_id FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id
                 WHERE rr.id = %s""", [rid])
    if not rr or str(rr['company_id']) != str(user.company_id):
        raise NotFoundError('request not found')
    assert_manages(user, str(rr['employee_id']), 'attendance')
    # Approved corrections already produced a punch/decision; they cannot be undone here (a new request or HR adjustment supersedes).
    if rr['status'] != 'pending':
        raise ConflictError(f"only pending requests can be cancelled (status {rr['status']})")
    with transaction.atomic():
        # Conditional update: an approval landing at the same moment wins, and the cancel reports the conflict.
        n = execute("UPDATE regularisation_request SET status = 'cancelled', decided_at = now() WHERE id = %s AND status = 'pending'", [rid])
        if not n:
            raise ConflictError('request was decided in the meantime')
        cancel_workflow(OBJECT_TYPE, rid)
        audit('cancel', 'regularisation_request', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- HR manual punch (TS POST /punch/manual)

MANUAL = Obj({'employeeId': Uuid(), 'localDate': IsoDate(), 'time': Hhmm(), 'direction': Enum(('in', 'out')),
              'reason': Str(min=3, max=300)})

_MANUAL_DEVICE = ('manual', 'manual', 'Manual entries (HR, regularisation, auto clock-out)')


def _pseudo_manual_device(company_id: str) -> dict:
    """ensurePseudoDevice(db, companyId, 'MANUAL') of punch/devices.ts."""
    c = one('SELECT code FROM company WHERE id = %s', [company_id])
    if not c:
        raise RuntimeError('company not found')
    serial = f"MANUAL-{c['code']}"
    vendor, protocol, name = _MANUAL_DEVICE
    execute("""INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES (%s, %s, %s, %s, %s, 'active')
               ON CONFLICT (serial_no) DO NOTHING""", [company_id, serial, vendor, protocol, name])
    d = one('SELECT id, vendor FROM device WHERE serial_no = %s', [serial])
    return {'id': str(d['id']), 'serialNo': serial, 'vendor': d['vendor']}


def manual_punch(user: AuthUser, body: dict) -> dict:
    """HR manual entry with reason (plan §5.2); audited; day recomputed. {'duplicate', 'punchId'?, 'day'?}."""
    from hajiri.attendance.service import is_attendance_locked, recompute_for_punch

    if not has_module(user, 'attendance'):
        from hajiri.core.auth import MODULE_ROLES
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES['attendance'])}")
    b = parse(MANUAL, body)
    if b['employeeId'] == (str(user.employee_id) if user.employee_id else None) and not has_role(user, 'admin'):
        raise ForbiddenError('someone else must add punches to your own attendance')
    emp = one('SELECT emp_code FROM employee WHERE id = %s AND company_id = %s', [b['employeeId'], user.company_id])
    if not emp:
        raise NotFoundError('employee not found')
    if is_attendance_locked(b['employeeId'], b['localDate']):
        raise ConflictError('this day is locked by payroll; raise a supplementary correction instead')
    with transaction.atomic():
        device = _pseudo_manual_device(str(user.company_id))
        at = local_time_ms(b['localDate'], b['time'])
        key = hashlib.sha256(f"manual|{b['employeeId']}|{b['localDate']}|{b['time']}|{b['direction']}".encode()).hexdigest()
        ins = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                                              local_date, direction, verify_mode, source, raw, idempotency_key)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', 'manual', %s, %s)
                     ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id""",
                  [device['id'], device['serialNo'], device['vendor'], emp['emp_code'], b['employeeId'], _from_ms(at),
                   _device_local_time(_from_ms(at)), b['localDate'], b['direction'],
                   json.dumps({'reason': b['reason'], 'enteredBy': user.id}), key])
        if not ins:
            return {'duplicate': True}
        punch_id = str(ins['id'])
        audit('manual_punch', 'punch_event', actor_id=user.id, row_id=punch_id, after=b)
    result = recompute_for_punch(b['employeeId'], at, punch_id)
    return {'duplicate': False, 'punchId': punch_id, 'day': result.day}


# ---------------------------------------------------------------- offline clock-ins (lib/offlinePunch.ts)

OFFLINE = Obj({'id': Str(optional=True), 'direction': Enum(('in', 'out')), 'at': DateTimeStr(),
               'localDate': IsoDate(), 'userId': Str(optional=True)})


def offline_reason(direction: str, at: str | datetime) -> str:
    from hajiri.web.format import fmt_time
    return (f'Clocked {direction} at {fmt_time(at)} while offline (time from my phone); '
            'sent automatically when the connection came back.')


def flush_offline_punch(user: AuthUser, body: dict) -> dict:
    """One queued offline clock-in becomes a missed-punch correction for the signed-in user, with the phone's time."""
    p = parse(OFFLINE, body)
    if p.get('userId') and p['userId'] != user.id:
        # The queue is per account on a shared phone; another account's clock-in is never filed under this one.
        raise ForbiddenError('this clock-in was saved for another account')
    return create_request(user, {'localDate': p['localDate'], 'kind': 'missed_punch', 'punchTime': p['at'],
                                 'punchDirection': p['direction'], 'reason': offline_reason(p['direction'], p['at'])})


# ---------------------------------------------------------------- helpers for the pages

def shift_options(company_id: str) -> list[dict]:
    """Shifts of the company for the 'Shift worked' select: '<name or code>, HH:MM to HH:MM'."""
    return [{'id': str(r['id']), 'label': f"{r['name_en'] or r['code']}, {r['s']} to {r['e']}"} for r in rows(
        """SELECT id, code, name_en, to_char(start_time, 'HH24:MI') AS s, to_char(end_time, 'HH24:MI') AS e
             FROM shift WHERE company_id = %s ORDER BY code""", [company_id])]
