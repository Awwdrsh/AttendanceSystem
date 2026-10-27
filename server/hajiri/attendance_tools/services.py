"""
Leave and attendance extras (port of apps/api/src/modules/attendance-tools: policy.ts, block.ts, penalties.ts,
issues.ts, upload.ts, whosout.ts and routes.ts): blocking late clock-ins, the late-arrival penalty, attendance issues and
nudges, the manual CSV upload and who's out.

Public interface:
- extras_policy(company_id) -> dict (blockLateClockIn, blockAfterMin, whosOutScope, latePenalty{enabled, lateCount,
  minLateMinutes, penaltyDays, leaveTypeIds}); save_extras_policy(company_id, value, user_id); EXTRAS_KEY.
- assert_clock_in_allowed(*, company_id, employee_id, at_ms, direction) raises ConflictError 409 {'code': 'late_blocked'}.
- penalty_for(late_days, rule), allocate_penalty(days, available), month_locked(company_id, y, m),
  penalty_preview(company_id, y, m, rule, *, employee_id=None, manager_employee_id=None) -> [PenaltyLine dict],
  apply_penalties(...), reverse_penalty(...), penalty_unpaid_days(employee_id, y, m) -> float (payroll's loss of pay).
- attendance_issues(company_id, from, to, manager_employee_id) -> [IssueRow], summarise(rows).
- check_upload(company_id, csv, self_employee_id=None) -> {'rows', 'errors'}, import_upload(...), UPLOAD_TEMPLATE.
- scope_employee_ids(user, scope), whos_out(company_id, ids, from, to).
- One function per TS route taking (user, body_or_query) in the TS camelCase shape: get_policy, put_policy, penalties,
  my_penalties, apply, reverse, issues, issue_summary, nudge, upload_template, upload_check, upload_import, uploads,
  whos_out_for.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone

from django.db import transaction

from engines.attendance import MIN_MS, local_date_of, local_time_ms
from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds, bs_to_ad, parse_bs, weekday_of

from hajiri.attendance.repo import resolve_shift
from hajiri.attendance.service import date_range, is_attendance_locked, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role
from hajiri.core.bsdate import bs_month_name, bs_range
from hajiri.core.clock import now as _now
from hajiri.core.crypto import sha256_hex
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, Union, Uuid, parse, parse_uuid, safe_parse)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import approvers_for, assert_manages as wf_assert_manages, get_instance, npt_today

NIL_UUID = '00000000-0000-0000-0000-000000000000'

# ================================================================ policy.ts

EXTRAS_KEY = 'attendance.extras'


def _multiple_of_quarter(v) -> bool:
    return abs(v * 4 - round(v * 4)) < 1e-9


LATE_PENALTY = Obj({
    'enabled': Bool(default=False),
    'lateCount': Int(min=1, max=31, default=3),
    'minLateMinutes': Int(min=1, max=240, default=1),
    'penaltyDays': Num(min=0.25, max=5, default=0.5, refine=(_multiple_of_quarter, 'Invalid number: must be a multiple of 0.25')),
    'leaveTypeIds': Arr(Uuid(), max=5, default=list),
})
EXTRAS_POLICY = Obj({
    'blockLateClockIn': Bool(default=False),
    'blockAfterMin': Int(min=0, max=240, default=0),
    'whosOutScope': Enum(('team', 'department', 'company'), default='team'),
    'latePenalty': Obj(LATE_PENALTY.fields, default=lambda: parse(LATE_PENALTY, {})),
})


def default_extras() -> dict:
    return parse(EXTRAS_POLICY, {})


def extras_policy(company_id: str) -> dict:
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [str(company_id), EXTRAS_KEY])
    ok, v = safe_parse(EXTRAS_POLICY, (r or {}).get('value') or {})
    return v if ok else default_extras()


def save_extras_policy(company_id: str, value: dict, user_id: str) -> None:
    execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            [str(company_id), EXTRAS_KEY, json.dumps(value), user_id])


# ================================================================ block.ts

def _npt_local_time(ms: int) -> str:
    from hajiri.punch.services import npt_local_time
    return npt_local_time(ms)


def assert_clock_in_allowed(*, company_id: str, employee_id: str, at_ms: int, direction: str) -> None:
    """
    Optional rule (off by default): the first clock-in of a working day later than shift start + grace (+ the extra
    minutes HR allows) is refused. Clock-outs, second clock-ins, holidays, weekly offs and days with approved first-half
    or hourly leave at the start are never blocked.
    """
    if direction != 'in':
        return
    policy = extras_policy(company_id)
    if not policy['blockLateClockIn']:
        return
    local_date = local_date_of(at_ms)
    if rows("SELECT 1 AS x FROM punch_event WHERE employee_id = %s AND local_date = %s AND direction = 'in' LIMIT 1", [employee_id, local_date]):
        return
    e = one("""SELECT e.weekly_off_days, e.gender, a.branch_id FROM employee e
                 LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %s::date WHERE e.id = %s""",
            [local_date, employee_id])
    if not e:
        return
    if weekday_of(local_date) in (e['weekly_off_days'] or []):
        return
    if rows("""SELECT 1 AS x FROM holiday WHERE company_id = %s AND local_date = %s AND (branch_id IS NULL OR branch_id = %s) AND NOT optional
                AND (gender_scope = 'all' OR gender_scope = %s) LIMIT 1""", [company_id, local_date, e['branch_id'], e['gender']]):
        return
    shift = resolve_shift(employee_id, local_date)
    if not shift:
        return
    if rows("""SELECT 1 AS x FROM leave_request WHERE employee_id = %s AND status = 'approved' AND %s::date BETWEEN from_date AND to_date
                AND ((half_day IS NULL AND from_time IS NULL) OR half_day = 'first_half'
                     OR (from_time IS NOT NULL AND from_time <= %s::time + make_interval(mins => %s::int))) LIMIT 1""",
            [employee_id, local_date, shift.start, shift.grace_in_min]):
        return
    limit = local_time_ms(local_date, shift.start) + (shift.grace_in_min + policy['blockAfterMin']) * MIN_MS
    if at_ms <= limit:
        return
    closed_at = _npt_local_time(limit)[11:16]
    raise ConflictError(
        f'Clock-in closed at {closed_at} (your shift starts at {shift.start}). Raise a time request with the time you arrived '
        'and your manager will approve it.',
        {'code': 'late_blocked', 'shiftStart': shift.start, 'closedAt': closed_at, 'localDate': local_date})


# ================================================================ penalties.ts

def penalty_for(late_days: int, rule: dict) -> float:
    """Penalty for a number of late days under a rule."""
    if late_days < rule['lateCount']:
        return 0
    return (late_days // rule['lateCount']) * rule['penaltyDays']


def _round2(x: float) -> float:
    return math.floor(x * 100 + 0.5) / 100


def allocate_penalty(days: float, available: list[dict]) -> dict:
    """Split penalty days over the available balances of the leave types in order; the rest is unpaid."""
    left = days
    allocations = []
    for a in available:
        if left <= 0:
            break
        can = math.floor(max(0, a['available']) * 4) / 4   # quarter days, so balances never end up with odd fractions
        take = min(left, can)
        if take > 0:
            allocations.append({'leaveTypeId': a['leaveTypeId'], 'code': a['code'], 'days': take})
            left = _round2(left - take)
    return {'allocations': allocations, 'unpaid': max(0, _round2(left))}


def month_locked(company_id: str, bs_year: int, bs_month: int) -> bool:
    return bool(rows("""SELECT 1 AS x FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
                         WHERE p.company_id = %s AND p.bs_year = %s AND p.bs_month = %s AND r.run_type = 'regular'
                           AND r.locked_at IS NOT NULL LIMIT 1""", [str(company_id), bs_year, bs_month]))


def _iso_ms(d: datetime) -> str:
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def _num(v) -> float:
    f = float(v)
    return int(f) if f.is_integer() else f


def penalty_preview(company_id: str, bs_year: int, bs_month: int, rule: dict, *, employee_id: str | None = None,
                    manager_employee_id: str | None = None) -> list[dict]:
    """Late days per employee in the month (one employee, a manager's team or the whole company)."""
    from hajiri.leave.services import ledger_balance, pending_days
    b = bs_month_bounds(bs_year, bs_month)
    found = rows("""
        SELECT e.id AS employee_id, e.emp_code, e.name_en,
               count(v.local_date) FILTER (WHERE v.late_min >= %(min)s)::int AS late_days,
               coalesce(sum(v.late_min) FILTER (WHERE v.late_min >= %(min)s), 0)::int AS late_minutes,
               coalesce(array_agg(v.local_date::text ORDER BY v.local_date) FILTER (WHERE v.late_min >= %(min)s), '{}') AS late_dates
          FROM employee e
          JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date BETWEEN %(s)s AND %(e)s
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
         WHERE e.company_id = %(c)s AND (%(emp)s::uuid IS NULL OR e.id = %(emp)s::uuid)
           AND (%(mgr)s::uuid IS NULL OR hj_manages(%(mgr)s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))
         GROUP BY e.id, e.emp_code, e.name_en
        HAVING count(v.local_date) FILTER (WHERE v.late_min >= %(min)s) > 0
         ORDER BY late_days DESC, e.emp_code""",
                 {'c': str(company_id), 's': b['startAd'], 'e': b['endAd'], 'min': rule['minLateMinutes'], 'emp': employee_id,
                  'mgr': manager_employee_id})
    applied = rows("""SELECT id, employee_id, penalty_days, leave_days, unpaid_days, applied_at FROM late_penalty
                       WHERE company_id = %s AND bs_year = %s AND bs_month = %s AND status = 'applied'""",
                   [str(company_id), bs_year, bs_month])
    types = rows('SELECT id, code FROM leave_type WHERE company_id = %s AND id = ANY(%s::uuid[])',
                 [str(company_id), rule['leaveTypeIds']]) if rule['leaveTypeIds'] else []
    by_id = {str(t['id']): t for t in types}
    ordered = [by_id[i] for i in rule['leaveTypeIds'] if i in by_id]
    out = []
    for r in found:
        emp = str(r['employee_id'])
        penalty_days = penalty_for(r['late_days'], rule)
        avail = [{'leaveTypeId': str(t['id']), 'code': t['code'],
                  'available': ledger_balance(emp, str(t['id'])) - pending_days(emp, str(t['id']))} for t in ordered]
        a = allocate_penalty(penalty_days, avail)
        ap = next((x for x in applied if str(x['employee_id']) == emp), None)
        out.append({
            'employeeId': emp, 'empCode': r['emp_code'], 'name': r['name_en'], 'lateDays': r['late_days'],
            'lateMinutes': r['late_minutes'], 'lateDates': list(r['late_dates'] or []), 'penaltyDays': _num(penalty_days),
            'allocations': a['allocations'], 'unpaidDays': _num(a['unpaid']),
            'applied': {'id': str(ap['id']), 'penaltyDays': _num(ap['penalty_days']), 'leaveDays': _num(ap['leave_days']),
                        'unpaidDays': _num(ap['unpaid_days']), 'appliedAt': _iso_ms(ap['applied_at'])} if ap else None,
        })
    return out


def apply_penalties(company_id: str, bs_year: int, bs_month: int, rule: dict, employee_ids: list[str] | None, actor_id: str) -> dict:
    """Apply the month's penalties for the listed employees (or everyone with one); already-applied ones are skipped."""
    if month_locked(company_id, bs_year, bs_month):
        raise ConflictError(f'{bs_month_name(bs_year, bs_month)} payroll is locked; penalties can no longer change')
    end_ad = bs_month_bounds(bs_year, bs_month)['endAd']
    label = bs_month_name(bs_year, bs_month)
    lines = penalty_preview(company_id, bs_year, bs_month, rule)
    applied = skipped = 0
    for ln in lines:
        if employee_ids is not None and ln['employeeId'] not in employee_ids:
            continue
        if ln['applied'] or ln['penaltyDays'] <= 0:
            skipped += 1
            continue
        leave_days = _num(sum(a['days'] for a in ln['allocations']))
        ins = one("""INSERT INTO late_penalty (company_id, employee_id, bs_year, bs_month, late_days, late_minutes, penalty_days, leave_days,
                       leave_type_id, unpaid_days, rule, applied_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                  [str(company_id), ln['employeeId'], bs_year, bs_month, ln['lateDays'], ln['lateMinutes'], ln['penaltyDays'], leave_days,
                   ln['allocations'][0]['leaveTypeId'] if ln['allocations'] else None, ln['unpaidDays'],
                   json.dumps({**rule, 'allocations': ln['allocations'], 'lateDates': ln['lateDates']}), actor_id])
        if not ins:
            skipped += 1
            continue
        pid = str(ins['id'])
        for a in ln['allocations']:
            execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note, posted_by)
                       VALUES (%s, %s, %s, %s, 'adjustment', 'late_penalty', %s, %s, %s)""",
                    [ln['employeeId'], a['leaveTypeId'], end_ad, -a['days'], pid,
                     f"Late-arrival penalty, {label} ({ln['lateDays']} late days)", actor_id])
        audit('apply_late_penalty', 'late_penalty', actor_id=actor_id, row_id=pid,
              after={'employeeId': ln['employeeId'], 'bsYear': bs_year, 'bsMonth': bs_month, 'lateDays': ln['lateDays'],
                     'penaltyDays': ln['penaltyDays'], 'leaveDays': leave_days, 'unpaidDays': ln['unpaidDays']})
        user_id = user_id_for_employee(ln['employeeId'])
        parts = ' and '.join(x for x in [f'{leave_days} day(s) from leave' if leave_days else '',
                                         f"{ln['unpaidDays']} unpaid day(s)" if ln['unpaidDays'] else ''] if x)
        notify(company_id=str(company_id), user_id=user_id, employee_id=ln['employeeId'], template='attendance.late_penalty',
               title='Late-arrival penalty',
               body=f"You arrived late on {ln['lateDays']} day(s) in {label}. Under the company rule this counts as "
                    f"{ln['penaltyDays']} day(s): {parts}.", payload={'penaltyId': pid})
        applied += 1
    return {'applied': applied, 'skipped': skipped}


def reverse_penalty(company_id: str, penalty_id: str, reason: str, actor_id: str, actor_employee_id: str | None = None) -> None:
    p = one('SELECT employee_id, bs_year, bs_month, status, rule FROM late_penalty WHERE id = %s AND company_id = %s FOR UPDATE',
            [penalty_id, str(company_id)])
    if not p:
        raise NotFoundError('penalty not found')
    if actor_employee_id and str(p['employee_id']) == str(actor_employee_id):
        raise ForbiddenError('someone else must reverse your own late penalty')
    if p['status'] != 'applied':
        raise ConflictError('this penalty is already reversed')
    if month_locked(company_id, p['bs_year'], p['bs_month']):
        raise ConflictError('the payroll for this month is locked; the penalty can no longer be reversed')
    end_ad = bs_month_bounds(p['bs_year'], p['bs_month'])['endAd']
    for a in (p['rule'] or {}).get('allocations') or []:
        execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note, posted_by)
                   VALUES (%s, %s, %s, %s, 'adjustment', 'late_penalty_reversed', %s, %s, %s)""",
                [str(p['employee_id']), a['leaveTypeId'], end_ad, a['days'], penalty_id, f'Late-arrival penalty reversed: {reason}', actor_id])
    execute("UPDATE late_penalty SET status = 'reversed', reversed_by = %s, reversed_at = now(), reverse_reason = %s WHERE id = %s",
            [actor_id, reason, penalty_id])
    audit('reverse_late_penalty', 'late_penalty', actor_id=actor_id, row_id=penalty_id, after={'reason': reason})


def penalty_unpaid_days(employee_id: str, bs_year: int, bs_month: int) -> float:
    """Unpaid penalty days for payroll's loss of pay in a BS month (applied penalties only)."""
    r = one("""SELECT coalesce(sum(unpaid_days), 0)::text AS d FROM late_penalty WHERE employee_id = %s AND bs_year = %s AND bs_month = %s
                AND status = 'applied'""", [str(employee_id), bs_year, bs_month])
    return _num(r['d'])


# ================================================================ issues.ts

def _sort_key_code(s: str):
    return (s.casefold(), s)


def attendance_issues(company_id: str, from_: str, to: str, manager_employee_id: str | None) -> list[dict]:
    """Missing punches, absences nobody asked to cover, and leave and time requests still waiting (with who they wait on).
    HR sees the company, a manager their team (manager_employee_id)."""
    p = {'c': str(company_id), 'f': from_, 't': to, 'm': manager_employee_id}
    team = '(%(m)s::uuid IS NULL OR hj_manages(%(m)s::uuid, e.id, (now() AT TIME ZONE \'Asia/Kathmandu\')::date))'
    days = rows(f"""
        SELECT e.id AS employee_id, e.emp_code, e.name_en, v.local_date::text AS local_date, v.status,
               CASE WHEN v.status = 'Incomplete' OR v.flags @> '[{{"kind":"MissingOut"}}]' OR v.flags @> '[{{"kind":"UnpairedPunch"}}]'
                    THEN 'missing' ELSE 'absent' END AS issue
          FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
         WHERE e.company_id = %(c)s AND e.status = 'active' AND v.local_date BETWEEN %(f)s AND %(t)s AND {team}
           AND v.local_date < (now() AT TIME ZONE 'Asia/Kathmandu')::date
           AND (v.status = 'Incomplete' OR v.flags @> '[{{"kind":"MissingOut"}}]' OR v.flags @> '[{{"kind":"UnpairedPunch"}}]'
                OR (v.status = 'Absent' AND NOT v.flags @> '[{{"kind":"HalfDayLeave"}}]'))
           AND NOT EXISTS (SELECT 1 FROM leave_request lr WHERE lr.employee_id = e.id AND lr.status = 'pending'
                            AND v.local_date BETWEEN lr.from_date AND lr.to_date)
           AND NOT EXISTS (SELECT 1 FROM regularisation_request rr WHERE rr.employee_id = e.id AND rr.status = 'pending'
                            AND rr.local_date = v.local_date)
         ORDER BY e.emp_code, v.local_date""", p)
    pend = rows(f"""
        SELECT wi.id AS instance_id, wi.object_type, wi.object_id, e.id AS employee_id, e.emp_code, e.name_en, wi.steps, wi.current_step,
               wi.created_at, coalesce(lr.from_date, rr.local_date)::text AS from_date, coalesce(lr.to_date, rr.local_date)::text AS to_date,
               CASE WHEN lr.id IS NOT NULL THEN lt.name_en ELSE replace(initcap(rr.kind), '_', ' ') END AS label
          FROM workflow_instance wi JOIN employee e ON e.id = wi.employee_id
          LEFT JOIN leave_request lr ON wi.object_type = 'leave_request' AND lr.id = wi.object_id
          LEFT JOIN leave_type lt ON lt.id = lr.leave_type_id
          LEFT JOIN regularisation_request rr ON wi.object_type = 'regularisation_request' AND rr.id = wi.object_id
         WHERE wi.company_id = %(c)s AND wi.state = 'pending' AND wi.object_type IN ('leave_request', 'regularisation_request') AND {team}
           AND coalesce(lr.to_date, rr.local_date) >= %(f)s AND coalesce(lr.from_date, rr.local_date) <= %(t)s
         ORDER BY wi.created_at""", p)
    cache: dict = {}
    names: dict[str, str] = {}
    by_emp: dict[str, dict] = {}

    def row(i, code, name):
        r = by_emp.get(i)
        if not r:
            r = {'employeeId': i, 'empCode': code, 'name': name, 'missing': [], 'absent': [], 'pending': []}
            by_emp[i] = r
        return r

    for d in days:
        row(str(d['employee_id']), d['emp_code'], d['name_en'])[d['issue']].append(d['local_date'])
    now = _now()
    for x in pend:
        steps = x['steps'] or []
        step = steps[x['current_step']] if 0 <= x['current_step'] < len(steps) else None
        ids = approvers_for(str(company_id), str(x['employee_id']), step.get('role') if isinstance(step, dict) and step.get('role') else step,
                            cache) if step else []
        missing = [i for i in ids if i not in names]
        if missing:
            for u in rows('SELECT id, name FROM app_user WHERE id = ANY(%s::uuid[])', [missing]):
                names[str(u['id'])] = u['name']
        row(str(x['employee_id']), x['emp_code'], x['name_en'])['pending'].append({
            'instanceId': str(x['instance_id']), 'type': 'leave' if x['object_type'] == 'leave_request' else 'time',
            'requestId': str(x['object_id']), 'from': x['from_date'], 'to': x['to_date'], 'label': x['label'],
            'createdAt': _iso_ms(x['created_at']), 'ageDays': math.floor((now - x['created_at']).total_seconds() / 86400),
            'waitingOn': [{'userId': i, 'name': names.get(i, 'Approver')} for i in ids],
        })
    return sorted(by_emp.values(), key=lambda r: _sort_key_code(r['empCode']))


def summarise(issue_rows: list[dict]) -> dict:
    pending = [p for r in issue_rows for p in r['pending']]
    return {
        'employees': len(issue_rows),
        'missing': sum(len(r['missing']) for r in issue_rows),
        'absent': sum(len(r['absent']) for r in issue_rows),
        'pendingLeave': sum(1 for p in pending if p['type'] == 'leave'),
        'pendingTime': sum(1 for p in pending if p['type'] == 'time'),
        'oldestPendingDays': max([0] + [p['ageDays'] for p in pending]),
    }


# ================================================================ upload.ts

HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
UPLOAD_TEMPLATE = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-29,09:05,17:10,Device was offline\n'
MAX_UPLOAD_ROWS = 2000


def _norm(t: str) -> str | None:
    m = HHMM.match(t.strip())
    return f'{m.group(1).zfill(2)}:{m.group(2)}' if m else None


def _valid_ad(s: str) -> bool:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return False
    try:
        y, m, d = (int(x) for x in s.split('-'))
        # Date.parse rolls invalid days within 1..31 over; it only fails for month > 12 or day > 31.
        return 1 <= m <= 12 and 1 <= d <= 31
    except ValueError:
        return False


def _ad_value(s: str) -> str:
    """The ISO date Date.parse would read (2026-02-30 -> 2026-03-02), as the TS kept the cell text when it parsed."""
    return s


def check_upload(company_id: str, csv: str, self_employee_id: str | None = None) -> dict:
    """Read and check every row: the file's columns first, then each row's employee, date, times and lock."""
    from hajiri.employees.services import parse_csv
    table = parse_csv(re.sub(r'^﻿', '', csv))
    if not table:
        return {'rows': [], 'errors': ['The file is empty.']}
    header = [re.sub(r'\s+', '_', h.strip().lower()) for h in table[0]]

    def col(name):
        return header.index(name) if name in header else -1
    i_code = col('emp_code')
    i_bs = col('date_bs') if col('date_bs') >= 0 else col('date')
    i_ad = col('date_ad')
    i_in, i_out, i_note = col('in_time'), col('out_time'), col('note')
    errors = []
    if i_code < 0:
        errors.append('The first row must name the columns: emp_code, date_bs (or date_ad), in_time, out_time, note.')
    if i_bs < 0 and i_ad < 0:
        errors.append('Add a date_bs (BS, YYYY-MM-DD) or date_ad column.')
    if i_in < 0 and i_out < 0:
        errors.append('Add in_time and/or out_time columns (HH:MM, 24-hour).')
    if errors:
        return {'rows': [], 'errors': errors}
    body = table[1:]
    if len(body) > MAX_UPLOAD_ROWS:
        return {'rows': [], 'errors': [f'Upload at most {MAX_UPLOAD_ROWS} rows at a time.']}

    def cell_of(r, k):
        return (r[k] if k < len(r) and r[k] is not None else '').strip() if k >= 0 else ''
    codes = list(dict.fromkeys(c for c in (cell_of(r, i_code) for r in body) if c))
    emps = rows("""SELECT id, emp_code, name_en, status, join_date::text AS join_date, exit_date::text AS exit_date FROM employee
                    WHERE company_id = %s AND emp_code = ANY(%s::text[])""", [str(company_id), codes])
    by_code = {e['emp_code']: e for e in emps}
    today = npt_today()
    seen: set[str] = set()
    out = []
    for i, r in enumerate(body):
        def cell(k):
            return cell_of(r, k)
        row = {'line': i + 2, 'empCode': cell(i_code), 'date': None, 'dateBs': None, 'inTime': None, 'outTime': None,
               'note': cell(i_note) or None, 'employeeId': None, 'name': None, 'overnight': False, 'errors': []}
        e = by_code.get(row['empCode'])
        if not row['empCode']:
            row['errors'].append('Employee code is missing.')
        elif not e:
            row['errors'].append(f"No employee with code {row['empCode']}.")
        elif e['status'] != 'active':
            row['errors'].append(f"{e['name_en']} is not active.")
        elif self_employee_id and str(e['id']) == str(self_employee_id):
            row['errors'].append('You cannot upload your own attendance; ask another HR person.')
        else:
            row['employeeId'] = str(e['id'])
            row['name'] = e['name_en']
        try:
            if i_bs >= 0 and cell(i_bs):
                row['dateBs'] = cell(i_bs)
                row['date'] = bs_to_ad(parse_bs(row['dateBs']))
            elif i_ad >= 0 and cell(i_ad) and _valid_ad(cell(i_ad)):
                row['date'] = cell(i_ad)
            else:
                row['errors'].append('The date is missing or not YYYY-MM-DD.')
        except (BsRangeError, ValueError, KeyError):
            row['errors'].append(f'{cell(i_bs)} is not a valid BS date.')
        if row['date'] and row['date'] > today:
            row['errors'].append('The date is in the future.')
        if row['date'] and e and e['join_date'] and row['date'] < e['join_date']:
            row['errors'].append('The date is before the employee joined.')
        if row['date'] and e and e['exit_date'] and row['date'] > e['exit_date']:
            row['errors'].append('The date is after the employee left.')
        raw_in, raw_out = cell(i_in), cell(i_out)
        row['inTime'] = _norm(raw_in) if raw_in else None
        row['outTime'] = _norm(raw_out) if raw_out else None
        if raw_in and not row['inTime']:
            row['errors'].append(f'In time {raw_in} is not HH:MM.')
        if raw_out and not row['outTime']:
            row['errors'].append(f'Out time {raw_out} is not HH:MM.')
        if not raw_in and not raw_out:
            row['errors'].append('Give an in time, an out time or both.')
        if row['inTime'] and row['outTime'] and row['outTime'] <= row['inTime']:
            row['overnight'] = True
        if row['note'] and len(row['note']) > 200:
            row['errors'].append('The note is longer than 200 characters.')
        if row['employeeId'] and row['date']:
            k = f"{row['employeeId']}|{row['date']}"
            if k in seen:
                row['errors'].append('This employee and date appear more than once in the file.')
            seen.add(k)
            if not row['errors'] and is_attendance_locked(row['employeeId'], row['date']):
                row['errors'].append('This day is locked by payroll.')
        out.append(row)
    return {'rows': out, 'errors': []}


def _upload_punch(*, company_id, employee_id, emp_code, local_date, time, day_offset, direction, reason, actor_id) -> dict | None:
    """One manual punch with the same idempotency key as POST /punch/manual; None for a duplicate."""
    from hajiri.attendance.repo import from_ms
    from hajiri.punch.services import ensure_pseudo_device, npt_local_time
    device = ensure_pseudo_device(company_id, 'MANUAL')
    at = local_time_ms(local_date, time, day_offset)
    punch_date = local_date_of(at)
    key = sha256_hex(f'manual|{employee_id}|{punch_date}|{time}|{direction}')
    ins = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                   local_date, direction, verify_mode, source, raw, idempotency_key)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', 'manual', %s, %s)
                 ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id""",
              [device['id'], device['serialNo'], device['vendor'], emp_code, employee_id, from_ms(at), npt_local_time(at), punch_date,
               direction, json.dumps({'reason': reason, 'enteredBy': actor_id, 'upload': True}), key])
    return {'id': str(ins['id']), 'at': at} if ins else None


def import_upload(company_id: str, upload_rows: list[dict], reason: str, actor_id: str) -> dict:
    punches = duplicates = days = 0
    for r in upload_rows:
        if r['errors'] or not r['employeeId'] or not r['date']:
            continue
        note = f" ({r['note']})" if r['note'] else ''
        why = f'CSV upload: {reason}{note}'
        last = None
        for direction, time, offset in (('in', r['inTime'], 0), ('out', r['outTime'], 1 if r['overnight'] else 0)):
            if not time:
                continue
            p = _upload_punch(company_id=str(company_id), employee_id=r['employeeId'], emp_code=r['empCode'], local_date=r['date'],
                              time=time, day_offset=offset, direction=direction, reason=why, actor_id=actor_id)
            if p:
                punches += 1
                last = p
            else:
                duplicates += 1
        if last:
            recompute_for_punch(r['employeeId'], last['at'], last['id'])
            days += 1
    return {'punches': punches, 'duplicates': duplicates, 'days': days}


# ================================================================ whosout.ts

OUT_LABEL = {'leave': 'On leave', 'travel': 'Travelling', 'wfh': 'From home', 'field': 'In the field'}
OUT_TONE = {'leave': 'info', 'travel': 'accent', 'wfh': 'ok', 'field': 'warn'}


def scope_employee_ids(user: AuthUser, scope: str) -> list[str] | None:
    if scope == 'company':
        return None
    if not user.employee_id:
        return None if has_role(user, 'hr') else []
    if scope == 'department':
        r = rows("""SELECT a.employee_id AS id FROM employee_assignment a
                      JOIN employee_assignment me ON me.employee_id = %s AND me.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                     WHERE a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND me.org_unit_id IS NOT NULL AND a.org_unit_id = me.org_unit_id""", [user.employee_id])
        return [str(x['id']) for x in r] if r else [user.employee_id]
    r = rows("""SELECT a.employee_id AS id FROM employee_assignment a WHERE a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND a.manager_id = %(e)s
                UNION SELECT a.employee_id FROM employee_assignment a JOIN employee_assignment me ON me.employee_id = %(e)s AND me.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                 WHERE a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND me.manager_id IS NOT NULL AND (a.manager_id = me.manager_id OR a.employee_id = me.manager_id)
                UNION SELECT %(e)s::uuid""", {'e': user.employee_id})
    return [str(x['id']) for x in r]


def whos_out(company_id: str, ids: list[str] | None, from_: str, to: str) -> list[dict]:
    p = {'c': str(company_id), 'f': from_, 't': to, 'ids': ids}
    in_scope = "e.company_id = %(c)s AND e.status = 'active' AND (%(ids)s::uuid[] IS NULL OR e.id = ANY(%(ids)s::uuid[]))"
    leave = rows(f"""SELECT e.id AS employee_id, e.name_en, e.designation, lr.from_date::text AS from_date, lr.to_date::text AS to_date,
                            lr.half_day, lr.hours::text AS hours
                       FROM leave_request lr JOIN employee e ON e.id = lr.employee_id
                      WHERE {in_scope} AND lr.status = 'approved' AND lr.to_date >= %(f)s AND lr.from_date <= %(t)s""", p)
    travel = rows(f"""SELECT e.id AS employee_id, e.name_en, e.designation, t.from_date::text AS from_date, t.to_date::text AS to_date, t.destination
                        FROM travel_request t JOIN employee e ON e.id = t.employee_id
                       WHERE {in_scope} AND t.status IN ('approved', 'settling', 'settled') AND t.to_date >= %(f)s AND t.from_date <= %(t)s""", p)
    wfh = rows(f"""SELECT DISTINCT e.id AS employee_id, e.name_en, e.designation, x.local_date::text AS local_date FROM employee e
                     JOIN (SELECT employee_id, local_date FROM regularisation_request WHERE kind = 'wfh' AND status = 'approved'
                             AND local_date BETWEEN %(f)s AND %(t)s
                           UNION SELECT employee_id, local_date FROM punch_event WHERE local_date BETWEEN %(f)s AND %(t)s
                             AND raw->'flags' ? 'Remote') x ON x.employee_id = e.id
                    WHERE {in_scope}""", p)
    field = rows(f"""SELECT DISTINCT ON (e.id, v.local_date) e.id AS employee_id, e.name_en, e.designation, v.local_date::text AS local_date, v.place
                       FROM field_visit v JOIN employee e ON e.id = v.employee_id
                      WHERE {in_scope} AND v.local_date BETWEEN %(f)s AND %(t)s ORDER BY e.id, v.local_date, v.started_at DESC""", p)
    days = [{'date': d, 'people': []} for d in date_range(from_, to)]
    by_date = {d['date']: d for d in days}

    def add(d, x):
        day = by_date.get(d)
        if day and not any(q['employeeId'] == x['employeeId'] for q in day['people']):
            day['people'].append(x)

    def person(r, kind, detail):
        return {'employeeId': str(r['employee_id']), 'name': r['name_en'], 'designation': r['designation'], 'kind': kind, 'detail': detail}
    for lv in leave:
        detail = (f"{_num(lv['hours'])} h" if lv['hours'] else
                  ('First half' if lv['half_day'] == 'first_half' else 'Second half') if lv['half_day'] else None)
        for d in date_range(max(lv['from_date'], from_), min(lv['to_date'], to)):
            add(d, person(lv, 'leave', detail))
    for t in travel:
        for d in date_range(max(t['from_date'], from_), min(t['to_date'], to)):
            add(d, person(t, 'travel', t['destination']))
    for w in wfh:
        add(w['local_date'], person(w, 'wfh', None))
    for f in field:
        add(f['local_date'], person(f, 'field', f['place']))
    for d in days:
        d['people'].sort(key=lambda x: (x['name'].casefold(), x['name']))
    return days


# ================================================================ routes.ts

MONTH = Obj({'bsYear': Int(min=2000, max=2100, coerce=True), 'bsMonth': Int(min=1, max=12, coerce=True)})
MONTH_PARTIAL = Obj({'bsYear': Int(min=2000, max=2100, coerce=True, optional=True),
                     'bsMonth': Int(min=1, max=12, coerce=True, optional=True)})


def _range(query: dict) -> dict:
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate()}), query)
    if q['from'] > q['to']:
        from hajiri.core.errors import ValidationError
        raise ValidationError([{'path': '', 'message': 'from must be on or before to'}])
    if (date.fromisoformat(q['to']) - date.fromisoformat(q['from'])).days > 92:
        from hajiri.core.errors import ValidationError
        raise ValidationError([{'path': '', 'message': 'pick at most 92 days'}])
    return q


def self_of(user: AuthUser) -> str | None:
    """The acting person's own employee id, so nobody but an administrator edits their own attendance or penalties."""
    return user.employee_id if not has_role(user, 'admin') else None


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _require_module(user: AuthUser, module: str) -> None:
    _require(user, *MODULE_ROLES[module])


def _staff(user: AuthUser) -> None:
    _require(user, *MODULE_ROLES['attendance'], 'manager')


def get_policy(user: AuthUser) -> dict:
    return {'policy': extras_policy(company_id_of(user))}


def put_policy(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(EXTRAS_POLICY, body)
    ids = b['latePenalty']['leaveTypeIds']
    if ids:
        t = rows('SELECT id, paid, event_days FROM leave_type WHERE company_id = %s AND id = ANY(%s::uuid[])', [company_id, ids])
        if len(t) != len(set(ids)):
            raise BadRequestError('a chosen leave type does not exist')
        if any(not x['paid'] or x['event_days'] is not None for x in t):
            raise BadRequestError('penalties can only be taken from paid leave with a balance')
    with transaction.atomic():
        before = extras_policy(company_id)
        save_extras_policy(company_id, b, user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=company_id, before={EXTRAS_KEY: before}, after={EXTRAS_KEY: b})
    return {'policy': b}


def penalties(user: AuthUser, query: dict) -> dict:
    _staff(user)
    company_id = company_id_of(user)
    q = parse(MONTH, query)
    policy = extras_policy(company_id)
    mgr = None if has_module(user, 'attendance') else (user.employee_id or NIL_UUID)
    lines = penalty_preview(company_id, q['bsYear'], q['bsMonth'], policy['latePenalty'], manager_employee_id=mgr)
    return {'rule': policy['latePenalty'], 'locked': month_locked(company_id, q['bsYear'], q['bsMonth']), 'lines': lines}


def my_penalties(user: AuthUser, query: dict) -> dict:
    company_id = company_id_of(user)
    if not user.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    t = ad_to_bs(npt_today())
    q = parse(MONTH_PARTIAL, query)
    y, m = q.get('bsYear') or t.year, q.get('bsMonth') or t.month
    policy = extras_policy(company_id)
    lp = policy['latePenalty']
    lines = penalty_preview(company_id, y, m, lp, employee_id=user.employee_id)
    line = lines[0] if lines else None
    return {'bsYear': y, 'bsMonth': m, 'enabled': lp['enabled'],
            'rule': {'lateCount': lp['lateCount'], 'penaltyDays': lp['penaltyDays'], 'minLateMinutes': lp['minLateMinutes']},
            'lateDays': line['lateDays'] if line else 0, 'lateDates': line['lateDates'] if line else [],
            'projectedDays': (line['penaltyDays'] if line else 0) if lp['enabled'] else 0, 'applied': line['applied'] if line else None}


def apply(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(MONTH.extend({'employeeIds': Arr(Uuid(), max=2000, optional=True)}), body)
    policy = extras_policy(company_id)
    if not policy['latePenalty']['enabled']:
        raise ConflictError('turn the late-arrival penalty on first')
    me = self_of(user)
    if b.get('employeeIds') is not None and me and me in b['employeeIds']:
        raise ForbiddenError('someone else must apply your own late penalty')
    with transaction.atomic():
        return apply_penalties(company_id, b['bsYear'], b['bsMonth'], policy['latePenalty'], b.get('employeeIds'), user.id)


def reverse(user: AuthUser, penalty_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    pid = parse_uuid(penalty_id)
    b = parse(Obj({'reason': Str(trim=True, min=3, max=300)}), body)
    with transaction.atomic():
        reverse_penalty(company_id_of(user), pid, b['reason'], user.id, self_of(user))
    return {'ok': True}


def issues(user: AuthUser, query: dict) -> dict:
    _staff(user)
    q = _range(query)
    mgr = None if has_module(user, 'attendance') else (user.employee_id or NIL_UUID)
    found = attendance_issues(company_id_of(user), q['from'], q['to'], mgr)
    return {'summary': summarise(found), 'rows': found}


def issue_summary(user: AuthUser, query: dict) -> dict:
    """Pre-payroll check: the open issues of a BS month in numbers (the payroll pre-flight links here)."""
    _require_module(user, 'attendance')
    q = parse(MONTH, query)
    b = bs_month_bounds(q['bsYear'], q['bsMonth'])
    found = attendance_issues(company_id_of(user), b['startAd'], b['endAd'], None)
    return {'bsYear': q['bsYear'], 'bsMonth': q['bsMonth'], 'from': b['startAd'], 'to': b['endAd'], **summarise(found)}


NUDGE = Union(
    Obj({'target': Enum(('employee',)), 'employeeId': Uuid(), 'from': IsoDate(), 'to': IsoDate()}),
    Obj({'target': Enum(('approver',)), 'instanceId': Uuid()}),
)


def nudge(user: AuthUser, body: dict) -> dict:
    _staff(user)
    company_id = company_id_of(user)
    b = parse(NUDGE, body)
    today = npt_today()
    if b['target'] == 'employee':
        if not one('SELECT id FROM employee WHERE id = %s AND company_id = %s', [b['employeeId'], company_id]):
            raise NotFoundError('employee not found')
        wf_assert_manages(user, b['employeeId'])
        if b['employeeId'] == user.employee_id:
            raise BadRequestError('you cannot nudge yourself')
        r = next((x for x in attendance_issues(company_id, b['from'], b['to'], None) if x['employeeId'] == b['employeeId']), None)
        if not r or (not r['missing'] and not r['absent']):
            raise ConflictError('there is nothing for this employee to fix in this period')
        user_id = user_id_for_employee(b['employeeId'])
        if not user_id:
            raise ConflictError('this employee has no sign-in to notify')
        with transaction.atomic():
            ins = one("""INSERT INTO attendance_nudge (company_id, target_user_id, kind, ref, local_date, nudged_by)
                         VALUES (%s, %s, 'employee', %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                      [company_id, user_id, f"{b['from']}:{b['to']}", today, user.id])
            if not ins:
                return {'sent': 0, 'already': True}
            parts = ' and '.join(x for x in [f"{len(r['missing'])} day(s) with a missing punch" if r['missing'] else '',
                                             f"{len(r['absent'])} absent day(s) with no leave or time request" if r['absent'] else ''] if x)
            notify(company_id=company_id, user_id=user_id, employee_id=b['employeeId'], template='attendance.nudge',
                   title='Please fix your attendance',
                   body=f"You have {parts} between {bs_range(b['from'], b['to'])}. Apply for leave or raise a time request before payroll.",
                   payload={'from': b['from'], 'to': b['to']}, channels=['push'])
        return {'sent': 1, 'already': False}
    inst = get_instance(b['instanceId'])
    if not inst or inst['company_id'] != company_id or inst['object_type'] not in ('leave_request', 'regularisation_request'):
        raise NotFoundError('request not found')
    if inst['state'] != 'pending':
        raise ConflictError('this request is no longer waiting')
    if inst['employee_id']:
        wf_assert_manages(user, inst['employee_id'])
    steps = inst['steps']
    step = steps[inst['current_step']] if 0 <= inst['current_step'] < len(steps) else None
    ids = []
    if step:
        role = step.get('role') if isinstance(step, dict) and step.get('role') else step
        ids = [i for i in dict.fromkeys(approvers_for(company_id, inst['employee_id'], role)) if i != user.id]
    if not ids:
        raise ConflictError('there is nobody else to remind about this request')
    emp = one('SELECT name_en FROM employee WHERE id = %s', [inst['employee_id']]) if inst['employee_id'] else None
    created = inst['created_at'].astimezone(timezone.utc).date().isoformat()
    sent = 0
    with transaction.atomic():
        for i in ids:
            ins = one("""INSERT INTO attendance_nudge (company_id, target_user_id, kind, ref, local_date, nudged_by)
                         VALUES (%s, %s, 'approver', %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                      [company_id, i, inst['id'], today, user.id])
            if not ins:
                continue
            kind = 'leave' if inst['object_type'] == 'leave_request' else 'time'
            notify(company_id=company_id, user_id=i, template='attendance.nudge_approver', title='A request is waiting for you',
                   body=f"{emp['name_en'] if emp else 'An employee'}'s {kind} request has been waiting since {bs_range(created, created)}. "
                        'Please decide it before payroll.',
                   payload={'instanceId': inst['id']}, channels=['push'])
            sent += 1
    return {'sent': sent, 'already': sent == 0}


UPLOAD_BODY = Obj({'csv': Str(min=1, max=1_000_000), 'filename': Str(max=200, optional=True)})


def upload_check(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = parse(UPLOAD_BODY, body)
    out = check_upload(company_id_of(user), b['csv'], self_of(user))
    return {'errors': out['errors'], 'rows': out['rows'], 'valid': sum(1 for x in out['rows'] if not x['errors']),
            'invalid': sum(1 for x in out['rows'] if x['errors'])}


def upload_import(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(UPLOAD_BODY.extend({'reason': Str(trim=True, min=5, max=300)}), body)
    checked = check_upload(company_id, b['csv'], self_of(user))
    if checked['errors']:
        raise BadRequestError(' '.join(checked['errors']))
    bad = [x for x in checked['rows'] if x['errors']]
    if bad:
        raise BadRequestError(f'{len(bad)} row(s) have errors; fix them or remove them and upload again',
                              {'rows': [{'line': x['line'], 'errors': x['errors']} for x in bad]})
    if not checked['rows']:
        raise BadRequestError('there are no rows to import')
    out = import_upload(company_id, checked['rows'], b['reason'], user.id)
    with transaction.atomic():
        ins = one("""INSERT INTO attendance_upload (company_id, filename, reason, rows_total, punches, duplicates, uploaded_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [company_id, b.get('filename'), b['reason'], len(checked['rows']), out['punches'], out['duplicates'], user.id])
        audit('attendance_upload', 'attendance_upload', actor_id=user.id, row_id=str(ins['id']),
              after={'rows': len(checked['rows']), **out, 'reason': b['reason']})
    return {'id': str(ins['id']), 'rows': len(checked['rows']), **out}


def uploads(user: AuthUser) -> dict:
    _require_module(user, 'attendance')
    found = rows("""SELECT u.id, u.filename, u.reason, u.rows_total, u.punches, u.duplicates, u.created_at, a.name AS uploaded_by_name
                      FROM attendance_upload u LEFT JOIN app_user a ON a.id = u.uploaded_by WHERE u.company_id = %s
                     ORDER BY u.created_at DESC LIMIT 50""", [company_id_of(user)])
    for r in found:
        r['id'] = str(r['id'])
    return {'uploads': found}


def whos_out_for(user: AuthUser, query: dict) -> dict:
    company_id = company_id_of(user)
    q = parse(Obj({'days': Int(min=1, max=14, coerce=True, default=7)}), query)
    policy = extras_policy(company_id)
    ids = scope_employee_ids(user, policy['whosOutScope'])
    if ids is not None and not ids:
        return {'scope': policy['whosOutScope'], 'days': []}
    from_ = npt_today()
    to = (date.fromisoformat(from_) + timedelta(days=q['days'] - 1)).isoformat()
    return {'scope': policy['whosOutScope'], 'days': whos_out(company_id, ids, from_, to)}
