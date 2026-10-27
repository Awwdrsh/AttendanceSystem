"""
Overtime (port of apps/api/src/modules/ot/routes.ts): pre-approval requests that go through the approval workflow, and
the OT slips the attendance engine detects, which HR or the line manager approves, rejects or converts to substitute
leave. Only approved minutes reach payroll. Labour Act 2074 Sec 30(1) caps overtime at 4 h a day and 24 h a week;
settings never relax it.

Route-level functions take the signed-in user and the TS request body or query (camelCase keys) and return the TS
response shape, so the JSON API and the pages share every rule and message.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from django.db import transaction

from hajiri.attendance.service import enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, has_module, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Enum, Int, IsoDate, Obj, Str, Uuid, parse, parse_uuid
from hajiri.leave.services import credit_comp_off
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import (DecisionContext, assert_manages, cancel_workflow, npt_today, register_handler,
                                     start_workflow)

# Labour Act 2074 Sec 30(1): 4 h/day, 24 h/week. Never relaxed by settings.
CAP_DAY_MIN = 240
CAP_WEEK_MIN = 1440

REQUEST_STATUSES = ('pending', 'approved', 'rejected', 'cancelled')
SLIP_STATUSES = ('detected', 'approved', 'rejected', 'posted', 'comp_off')
SLIP_REVIEWERS = (*MODULE_ROLES['attendance'], 'manager')
REGISTER_COLUMNS = ('emp_code', 'name_en', 'local_date', 'bs_date', 'day_type', 'detected_min', 'approved_min',
                    'multiplier', 'status')

REQUEST_BODY = Obj({'employeeId': Uuid(optional=True), 'localDate': IsoDate(), 'requestedMin': Int(min=1),
                    'reason': Str(max=500, optional=True)})
REQUESTS_QUERY = Obj({'employeeId': Uuid(optional=True), 'status': Enum(REQUEST_STATUSES, optional=True),
                      'from': IsoDate(optional=True), 'to': IsoDate(optional=True),
                      'scope': Enum(('own', 'team', 'all'), optional=True)})
SLIPS_QUERY = Obj({'employeeId': Uuid(optional=True), 'from': IsoDate(optional=True), 'to': IsoDate(optional=True),
                   'status': Enum(SLIP_STATUSES, optional=True)})
APPROVE_BODY = Obj({'approvedMin': Int(min=0, max=CAP_DAY_MIN, optional=True)})
REJECT_BODY = Obj({'reason': Str(min=3, max=500)})
REGISTER_QUERY = Obj({'from': IsoDate(), 'to': IsoDate(), 'format': Enum(('json', 'csv'), default='json')})


def _require_role(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _js_num(x: float):
    """A number as JSON.stringify writes it: 1 rather than 1.0."""
    return int(x) if float(x).is_integer() else x


def week_start(d: str) -> str:
    """The Sunday that starts the week of an ISO date (getUTCDay() == 0 is Sunday)."""
    day = date.fromisoformat(d)
    return (day - timedelta(days=(day.weekday() + 1) % 7)).isoformat()


# ---------------------------------------------------------------- decision handler (workflow)

def apply_ot_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """An approved pre-approval is capped at the request and at 4 h; the day is recomputed and the employee told."""
    o = one("""SELECT o.employee_id, o.local_date, o.requested_min, o.status, e.company_id
                 FROM ot_request o JOIN employee e ON e.id = o.employee_id WHERE o.id = %s FOR UPDATE""", [request_id])
    if not o:
        raise NotFoundError('OT request not found')
    if o['status'] != 'pending':
        raise ConflictError(f"request already {o['status']}")
    approved = None
    if state == 'approved':
        raw = (ctx.payload or {}).get('approvedMin')
        asked = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else o['requested_min']
        approved = min(o['requested_min'], max(0, asked), CAP_DAY_MIN)
        approved = int(approved) if float(approved).is_integer() else approved
    execute('UPDATE ot_request SET status = %s, approved_min = %s, decided_at = now() WHERE id = %s',
            [state, approved, request_id])
    if state == 'approved':
        enqueue_recompute([(str(o['employee_id']), str(o['local_date']))], 'ot_preapproved')
    employee_id = str(o['employee_id'])
    outcome = f'{approved} minutes approved' if state == 'approved' else 'rejected'
    notify(company_id=str(o['company_id']), user_id=user_id_for_employee(employee_id), employee_id=employee_id,
           template=f'ot.{state}', title='Overtime approved' if state == 'approved' else 'Overtime rejected',
           body=f"Overtime on {bs_label(o['local_date'])}: {outcome}{f' ({ctx.reason})' if ctx.reason else ''}.",
           payload={'requestId': str(request_id)}, channels=['push'])


register_handler('ot_request', apply_ot_decision)


# ---------------------------------------------------------------- pre-approval requests

def past_window_days(company_id: str) -> int | float:
    pol = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'attendance.policy'", [company_id])
    v = (pol or {}).get('value') or {}
    w = v.get('otPastDatedDays') if isinstance(v, dict) else None
    return w if isinstance(w, (int, float)) and not isinstance(w, bool) else 7


def create_request(user: AuthUser, body: dict) -> dict:
    """POST /ot/requests -> {'id'}. The employee themself, their manager, or HR / attendance admin for anyone."""
    b = parse(REQUEST_BODY, body)
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    emp = one('SELECT company_id, ot_eligible FROM employee WHERE id = %s', [employee_id])
    if not emp or str(emp['company_id']) != str(user.company_id):
        raise NotFoundError('employee not found')
    if employee_id != user.employee_id:
        assert_manages(user, employee_id, 'attendance')
    if not emp['ot_eligible']:
        raise BadRequestError('employee grade is not overtime-eligible (Sec 31(2) basis must be documented)')
    if b['requestedMin'] > CAP_DAY_MIN:
        raise BadRequestError(f'overtime may not exceed {CAP_DAY_MIN} minutes (4 hours) per day (Sec 30(1))')
    ws = week_start(b['localDate'])
    wk = one("""SELECT (SELECT coalesce(sum(ot_min), 0) FROM v_attendance_latest WHERE employee_id = %(e)s AND local_date >= %(w)s::date
                               AND local_date < %(w)s::date + 7 AND local_date <> %(d)s::date)::text AS done,
                       (SELECT coalesce(sum(coalesce(approved_min, requested_min)), 0) FROM ot_request WHERE employee_id = %(e)s
                               AND status IN ('pending', 'approved') AND local_date >= %(w)s::date AND local_date < %(w)s::date + 7
                               AND local_date <> %(d)s::date)::text AS pending""",
             {'e': employee_id, 'w': ws, 'd': b['localDate']})
    used = max(int(wk['done']), int(wk['pending']))
    if used + b['requestedMin'] > CAP_WEEK_MIN:
        raise BadRequestError(f'weekly overtime cap of 24 hours would be exceeded ({used} minutes already this week)')
    window = past_window_days(user.company_id)
    age = (date.fromisoformat(npt_today()) - date.fromisoformat(b['localDate'])).days
    if age > window and not has_module(user, 'attendance'):
        raise BadRequestError(f'past-dated overtime requests are limited to {_js_num(window)} days')
    if one("SELECT 1 AS x FROM ot_request WHERE employee_id = %s AND local_date = %s AND status IN ('pending', 'approved')",
           [employee_id, b['localDate']]):
        raise ConflictError('an overtime request already exists for that day')
    with transaction.atomic():
        ins = one("""INSERT INTO ot_request (employee_id, local_date, requested_min, reason, created_by)
                     VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                  [employee_id, b['localDate'], b['requestedMin'], b.get('reason'), user.id])
        rid = str(ins['id'])
        reason = b.get('reason')
        inst = start_workflow(company_id=user.company_id, object_type='ot_request', object_id=rid, employee_id=employee_id,
                              title='Overtime pre-approval',
                              body=f"{b['requestedMin']} minutes on {b['localDate']}{f': {reason}' if reason else ''}")
        execute('UPDATE ot_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], rid])
        audit('create', 'ot_request', actor_id=user.id, row_id=rid, after={**b, 'employeeId': employee_id})
    return {'id': rid}


def list_requests(user: AuthUser, query: dict) -> list[dict]:
    """GET /ot/requests -> the rows of {'requests'}. HR sees everyone, a manager their team, others their own."""
    q = parse(REQUESTS_QUERY, query)
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    return rows("""SELECT o.id, o.employee_id, e.emp_code, e.name_en, o.local_date, o.requested_min, o.approved_min, o.reason, o.status,
                          o.workflow_instance_id, o.created_at, o.decided_at
                     FROM ot_request o JOIN employee e ON e.id = o.employee_id
                     LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                    WHERE e.company_id = %(c)s AND (%(e)s::uuid IS NULL OR o.employee_id = %(e)s) AND (%(s)s::text IS NULL OR o.status = %(s)s)
                      AND (%(f)s::date IS NULL OR o.local_date >= %(f)s) AND (%(t)s::date IS NULL OR o.local_date <= %(t)s)
                      AND CASE %(scope)s WHEN 'own' THEN o.employee_id = %(me)s::uuid
                                         WHEN 'team' THEN (hj_manages(%(me)s::uuid, o.employee_id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR o.employee_id = %(me)s::uuid)
                                         ELSE true END
                    ORDER BY o.local_date DESC LIMIT 500""",
                {'c': user.company_id, 'e': q.get('employeeId'), 's': q.get('status'), 'f': q.get('from'), 't': q.get('to'),
                 'scope': scope, 'me': user.employee_id})


def cancel_request(user: AuthUser, request_id: str) -> dict:
    """POST /ot/requests/:id/cancel: a pending request is withdrawn and its approval ends."""
    rid = parse_uuid(request_id)
    o = one("""SELECT o.employee_id, o.status, e.company_id FROM ot_request o JOIN employee e ON e.id = o.employee_id
                WHERE o.id = %s""", [rid])
    if not o or str(o['company_id']) != str(user.company_id):
        raise NotFoundError('request not found')
    assert_manages(user, str(o['employee_id']), 'attendance')
    if o['status'] != 'pending':
        raise ConflictError(f"only pending requests can be cancelled (status {o['status']})")
    with transaction.atomic():
        if not execute("UPDATE ot_request SET status = 'cancelled', decided_at = now() WHERE id = %s AND status = 'pending'", [rid]):
            raise ConflictError('request was decided in the meantime')
        cancel_workflow('ot_request', rid)
    return {'ok': True}


# ---------------------------------------------------------------- slips (detected by the attendance engine)

def list_slips(user: AuthUser, query: dict) -> list[dict]:
    """GET /ot/slips -> the rows of {'slips'}. HR all, a manager their team and themselves, an employee their own."""
    q = parse(SLIPS_QUERY, query)
    reviewer = has_role(user, *SLIP_REVIEWERS)
    employee_id = q.get('employeeId') if reviewer else user.employee_id
    if not reviewer and not employee_id:
        raise ForbiddenError('no employee linked to this user')
    return rows("""SELECT s.id, s.employee_id, e.emp_code, e.name_en, s.local_date, s.detected_min, s.approved_min, s.multiplier, s.day_type,
                          s.status, s.posted_payslip_id, s.reviewed_by, s.reviewed_at, s.created_at
                     FROM ot_slip s JOIN employee e ON e.id = s.employee_id
                     LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                    WHERE e.company_id = %(c)s AND (%(e)s::uuid IS NULL OR s.employee_id = %(e)s)
                      AND (%(f)s::date IS NULL OR s.local_date >= %(f)s) AND (%(t)s::date IS NULL OR s.local_date <= %(t)s)
                      AND (%(s)s::text IS NULL OR s.status = %(s)s)
                      AND (%(all)s OR hj_manages(%(me)s::uuid, s.employee_id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR s.employee_id = %(me)s::uuid)
                    ORDER BY s.local_date DESC LIMIT 1000""",
                {'c': user.company_id, 'e': employee_id, 'f': q.get('from'), 't': q.get('to'), 's': q.get('status'),
                 'all': has_module(user, 'attendance'), 'me': user.employee_id})


def slip_for(user: AuthUser, slip_id: str) -> dict:
    """A slip this user may decide: same company, not their own, someone they manage, not posted or converted."""
    s = one('SELECT s.*, e.company_id FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE s.id = %s', [slip_id])
    if not s or str(s['company_id']) != str(user.company_id):
        raise NotFoundError('OT slip not found')
    if user.employee_id == str(s['employee_id']):
        raise ForbiddenError('cannot decide your own overtime')
    assert_manages(user, str(s['employee_id']), 'attendance')
    if s['status'] == 'posted':
        raise ConflictError('slip already posted to payroll')
    if s['status'] == 'comp_off':
        raise ConflictError('slip already converted to substitute leave; the credit cannot be reversed here')
    return s


def approve_slip(user: AuthUser, slip_id: str, body: dict | None = None) -> dict:
    """POST /ot/slips/:id/approve -> {'ok', 'approvedMin'}, capped at the detected minutes and 4 h."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    b = parse(APPROVE_BODY, body or {})
    s = slip_for(user, sid)
    asked = b.get('approvedMin')
    approved = min(s['detected_min'] if asked is None else asked, s['detected_min'], CAP_DAY_MIN)
    with transaction.atomic():
        # Conditional update: a second approval (double click, two approvers at once) is a conflict, not a silent re-approval.
        if not execute("""UPDATE ot_slip SET status = 'approved', approved_min = %s, reviewed_by = %s, reviewed_at = now()
                           WHERE id = %s AND status IN ('detected', 'rejected')""", [approved, user.id, sid]):
            raise ConflictError(f"slip is already {s['status']}")
        audit('approve', 'ot_slip', actor_id=user.id, row_id=sid, after={'approvedMin': approved})
    return {'ok': True, 'approvedMin': approved}


def reject_slip(user: AuthUser, slip_id: str, body: dict | None) -> dict:
    """POST /ot/slips/:id/reject {reason} -> {'ok'}."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    b = parse(REJECT_BODY, body)
    s = slip_for(user, sid)
    with transaction.atomic():
        if not execute("""UPDATE ot_slip SET status = 'rejected', approved_min = 0, reviewed_by = %s, reviewed_at = now()
                           WHERE id = %s AND status IN ('detected', 'approved')""", [user.id, sid]):
            raise ConflictError(f"slip is already {s['status']}")
        audit('reject', 'ot_slip', actor_id=user.id, row_id=sid, after={'reason': b['reason'], 'employeeId': str(s['employee_id'])})
    return {'ok': True}


def comp_off_days(minutes: int) -> float:
    """Math.round(minutes / 480 * 100) / 100: a working day is 8 h."""
    return math.floor(minutes / 480 * 100 + 0.5) / 100


def comp_off_slip(user: AuthUser, slip_id: str) -> dict:
    """POST /ot/slips/:id/comp-off -> {'ok', 'days', 'expiresOn'}: substitute leave instead of pay (Sec 42: used within 21 days)."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    s = slip_for(user, sid)
    minutes = s['detected_min'] if s['approved_min'] is None else s['approved_min']
    days = comp_off_days(minutes)
    local = s['local_date'] if isinstance(s['local_date'], date) else date.fromisoformat(str(s['local_date']))
    expires = (local + timedelta(days=21)).isoformat()
    with transaction.atomic():
        # Claim the slip first so a double click cannot credit substitute leave twice.
        if not one("""UPDATE ot_slip SET status = 'comp_off' WHERE id = %s AND status NOT IN ('comp_off', 'posted', 'rejected')
                      RETURNING id""", [sid]):
            raise ConflictError('slip was already converted, posted or rejected')
        ledger_id = credit_comp_off(str(s['employee_id']), days, expires, ref_type='ot_slip', ref_id=sid,
                                    note=f'comp-off for {minutes} min OT on {local.isoformat()}', posted_by=user.id)
        execute("""UPDATE ot_slip SET status = 'comp_off', approved_min = %s, comp_off_ledger_id = %s, reviewed_by = %s,
                          reviewed_at = now() WHERE id = %s""", [minutes, ledger_id, user.id, sid])
        audit('comp_off', 'ot_slip', actor_id=user.id, row_id=sid, after={'days': _js_num(days), 'expires': expires})
    return {'ok': True, 'days': _js_num(days), 'expiresOn': expires}


# ---------------------------------------------------------------- inspector-ready register (plan §5.10)

def register(user: AuthUser, query: dict) -> tuple[str, list[dict]]:
    """GET /ot/register -> (format, rows). Every slip in the range with its BS date."""
    _require_role(user, *MODULE_ROLES['attendance'])
    q = parse(REGISTER_QUERY, query)
    data = rows("""SELECT e.emp_code, e.name_en, s.local_date, v.bs_date, s.day_type, s.detected_min, s.approved_min, s.multiplier, s.status
                     FROM ot_slip s JOIN employee e ON e.id = s.employee_id
                     LEFT JOIN v_attendance_latest v ON v.employee_id = s.employee_id AND v.local_date = s.local_date
                    WHERE e.company_id = %s AND s.local_date BETWEEN %s AND %s ORDER BY s.local_date, e.emp_code""",
                [user.company_id, q['from'], q['to']])
    return q['format'], data


def register_csv(data: list[dict]) -> str:
    """The register as the TS wrote it: BOM, header row, CRLF line ends, formula-safe cells."""
    from hajiri.web.csv import csv_cell
    lines = [','.join(REGISTER_COLUMNS)] + [','.join(csv_cell(r.get(c)) for c in REGISTER_COLUMNS) for r in data]
    return '﻿' + '\r\n'.join(lines) + '\r\n'
