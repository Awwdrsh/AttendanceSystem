"""
Attendance reports, the ad-hoc report builder and saved reports (port of apps/api/src/modules/reports/routes.ts and
the builder half of catalog.ts). HR sees the company, a manager the people they manage and themselves; salary-based
and device reports, and the builder, are HR only. Each report returns a Table (send_report / send_table turn it into
CSV for `?format=csv`).
"""
from __future__ import annotations

import json

from django.db import transaction

from engines.bs_calendar import bs_month_bounds

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, assert_manages, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.schema import Bool, Enum, Int, IsoDate, Obj, Str, Uuid, parse

from .adhoc import ADHOC_DEFINITION, build_adhoc_query, datasets as adhoc_datasets, parse_definition
from .csv import Table, _to_fixed, cols, hours, npt_hhmm, table_response

FMT = Obj({'format': Enum(('json', 'csv'), optional=True)})
RANGE = FMT.extend({'from': IsoDate(), 'to': IsoDate()})


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _scope(user: AuthUser) -> dict:
    """%(all)s: HR sees everyone; otherwise %(me)s and the people they manage today (Nepal date)."""
    return {'all': has_role(user, 'hr'), 'me': user.employee_id, 'today': npt_today().isoformat(), 'company': user.company_id}


TEAM = "(%(all)s OR e.id = %(me)s::uuid OR hj_manages(%(me)s::uuid, e.id, %(today)s::date))"


def attendance_report(user: AuthUser, name: str, query: dict) -> Table:
    _require(user, 'hr', 'manager')
    p = _scope(user)
    if name == 'daily':
        q = parse(FMT.extend({'date': IsoDate()}), query)
        p['d'] = q['date']
        found = rows(f"""
            SELECT e.emp_code, e.name_en, e.name_ne, b.name_en AS branch, v.status, v.first_in, v.last_out, v.worked_min,
                   v.late_min, v.early_min, v.ot_min, v.flags
              FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(d)s::date
              LEFT JOIN branch b ON b.id = a.branch_id
              LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = %(d)s
             WHERE e.company_id = %(company)s AND e.status = 'active' AND {TEAM} ORDER BY e.emp_code""", p)
        out = [{**x, 'status': x['status'] or 'NotComputed', 'arrival': npt_hhmm(x['first_in']), 'departure': npt_hhmm(x['last_out']),
                'worked_hours': hours(x['worked_min']), 'ot_hours': hours(x['ot_min']),
                'flags': json.dumps(x['flags'] if x['flags'] is not None else [], separators=(',', ':'), ensure_ascii=False)}
               for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('branch', 'Branch'), ('status', 'Status'),
                          ('arrival', 'Arrival'), ('departure', 'Departure'), ('worked_hours', 'Worked (h)'),
                          ('late_min', 'Late (min)'), ('early_min', 'Early out (min)'), ('ot_hours', 'OT (h)'), ('flags', 'Flags')),
                     out, f"daily-{q['date']}")

    if name == 'register':
        # Monthly attendance register, inspector-ready: one row per employee per calendar day of the BS month.
        q = parse(FMT.extend({'bsYear': Int(coerce=True), 'bsMonth': Int(min=1, max=12, coerce=True),
                              'employeeId': Uuid(optional=True)}), query)
        if q.get('employeeId'):
            assert_manages(user, q['employeeId'])
        bounds = bs_month_bounds(q['bsYear'], q['bsMonth'])
        p.update({'s': bounds['startAd'], 'e': bounds['endAd'], 'emp': q.get('employeeId')})
        found = rows(f"""
            SELECT e.emp_code, e.name_en, e.name_ne, d.day::date AS ad_date, c.bs_date, v.status, v.first_in, v.last_out,
                   v.worked_min, v.break_min, v.ot_min, v.late_min,
                   (SELECT lt.code FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id
                     WHERE lr.employee_id = e.id AND lr.status = 'approved' AND d.day BETWEEN lr.from_date AND lr.to_date LIMIT 1) AS leave_code
              FROM employee e
              CROSS JOIN generate_series(%(s)s::date, %(e)s::date, interval '1 day') AS d(day)
              LEFT JOIN bs_calendar c ON c.ad_date = d.day::date
              LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = d.day::date
             WHERE e.company_id = %(company)s AND (%(emp)s::uuid IS NULL OR e.id = %(emp)s::uuid)
               AND (e.join_date IS NULL OR e.join_date <= %(e)s::date) AND (e.exit_date IS NULL OR e.exit_date >= %(s)s::date)
               AND {TEAM}
             ORDER BY e.emp_code, d.day""", p)
        out = [{'emp_code': x['emp_code'], 'name_en': x['name_en'], 'name_ne': x['name_ne'], 'bs_date': x['bs_date'],
                'ad_date': str(x['ad_date'])[:10], 'arrival': npt_hhmm(x['first_in']), 'departure': npt_hhmm(x['last_out']),
                'worked_hours': hours(x['worked_min']), 'ot_hours': hours(x['ot_min']), 'break_min': x['break_min'] or 0,
                'late_min': x['late_min'] or 0, 'leave': x['leave_code'] or '', 'status': x['status'] or ''} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('bs_date', 'Date (BS)'), ('ad_date', 'Date (AD)'),
                          ('arrival', 'Arrival'), ('departure', 'Departure'), ('worked_hours', 'Worked (h)'),
                          ('break_min', 'Break (min)'), ('ot_hours', 'OT (h)'), ('late_min', 'Late (min)'), ('leave', 'Leave'),
                          ('status', 'Status')), out, f"register-{q['bsYear']}-{q['bsMonth']:02d}")

    if name == 'ot':
        q = parse(RANGE, query)
        p.update({'f': q['from'], 't': q['to']})
        found = rows(f"""
            SELECT e.emp_code, e.name_en, s.local_date, s.day_type, s.detected_min, s.approved_min, s.multiplier, s.status
              FROM ot_slip s JOIN employee e ON e.id = s.employee_id
             WHERE e.company_id = %(company)s AND s.local_date BETWEEN %(f)s AND %(t)s AND {TEAM} ORDER BY s.local_date, e.emp_code""", p)
        out = [{**x, 'detected_hours': hours(x['detected_min']), 'approved_hours': hours(x['approved_min'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('local_date', 'Date'), ('day_type', 'Day type'),
                          ('detected_hours', 'Detected (h)'), ('approved_hours', 'Approved (h)'), ('multiplier', 'Rate'),
                          ('status', 'Status')), out, f"ot-{q['from']}_{q['to']}")

    if name == 'late-early':
        q = parse(RANGE, query)
        p.update({'f': q['from'], 't': q['to']})
        found = rows(f"""
            SELECT e.emp_code, e.name_en, count(*) FILTER (WHERE v.late_min > 0)::int AS late_days, coalesce(sum(v.late_min), 0)::int AS late_min,
                   count(*) FILTER (WHERE v.early_min > 0)::int AS early_days, coalesce(sum(v.early_min), 0)::int AS early_min,
                   count(*) FILTER (WHERE v.status = 'Absent')::int AS absent_days, count(*) FILTER (WHERE v.status = 'HalfDay')::int AS half_days
              FROM employee e JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date BETWEEN %(f)s AND %(t)s
             WHERE e.company_id = %(company)s AND {TEAM} GROUP BY e.id, e.emp_code, e.name_en ORDER BY late_days DESC, e.emp_code""", p)
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('late_days', 'Late days'), ('late_min', 'Late (min)'),
                          ('early_days', 'Early-out days'), ('early_min', 'Early (min)'), ('absent_days', 'Absent'),
                          ('half_days', 'Half days')), found, f"late-early-{q['from']}_{q['to']}")

    if name == 'leave-balances':
        # Balance per employee and type; liability = balance x daily basic for encashable types.
        _require(user, 'hr')
        parse(FMT, query)
        found = rows("""
            SELECT e.emp_code, e.name_en, lt.code AS leave_type, lt.encashable, coalesce(sum(l.delta_days), 0)::numeric(8,2)::text AS balance,
                   (SELECT (sa.base_amounts->>'BASIC') FROM salary_assignment sa WHERE sa.employee_id = e.id AND sa.valid @> %(today)s::date) AS basic
              FROM employee e JOIN leave_type lt ON lt.company_id = e.company_id
              LEFT JOIN leave_ledger l ON l.employee_id = e.id AND l.leave_type_id = lt.id
             WHERE e.company_id = %(company)s AND e.status = 'active' AND lt.accrual_basis <> 'none'
             GROUP BY e.id, e.emp_code, e.name_en, lt.code, lt.encashable, lt.sort_order ORDER BY e.emp_code, lt.sort_order""", p)
        n = one("""SELECT count(*)::int AS n FROM bs_calendar c
                    WHERE (c.bs_year, c.bs_month) = (SELECT bs_year, bs_month FROM bs_calendar WHERE ad_date = %(today)s::date)""", p)
        days = (n and n['n']) or 30
        out = []
        for x in found:
            basic = float(x['basic']) if x['basic'] else None
            liability = _to_fixed((basic / days) * float(x['balance']), 2) if x['encashable'] and basic is not None else ''
            out.append({'emp_code': x['emp_code'], 'name_en': x['name_en'], 'leave_type': x['leave_type'], 'balance': x['balance'],
                        'encashable': x['encashable'], 'liability_npr': liability})
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('leave_type', 'Leave type'), ('balance', 'Balance (days)'),
                          ('encashable', 'Encashable'), ('liability_npr', 'Liability (NPR)')), out, 'leave-balances')

    if name == 'headcount':
        parse(FMT, query)
        found = rows("""
            SELECT coalesce(b.name_en, '(no branch)') AS branch, coalesce(o.name_en, '(no unit)') AS org_unit, a.employment_type,
                   count(*)::int AS headcount, count(*) FILTER (WHERE e.gender = 'female')::int AS female,
                   count(*) FILTER (WHERE e.gender = 'male')::int AS male
              FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(today)s::date
              LEFT JOIN branch b ON b.id = a.branch_id LEFT JOIN org_unit o ON o.id = a.org_unit_id
             WHERE e.company_id = %(company)s AND e.status = 'active' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", p)
        return Table(cols(('branch', 'Branch'), ('org_unit', 'Unit'), ('employment_type', 'Type'), ('headcount', 'Headcount'),
                          ('female', 'Female'), ('male', 'Male')), found, 'headcount')

    if name == 'exceptions-summary':
        q = parse(RANGE, query)
        p.update({'f': q['from'], 't': q['to']})
        found = rows(f"""
            SELECT v.local_date, count(*) FILTER (WHERE v.status = 'Incomplete')::int AS incomplete,
                   count(*) FILTER (WHERE v.status = 'Absent')::int AS absent,
                   count(*) FILTER (WHERE v.flags @> '[{{"kind":"OTPreapprovalMissing"}}]')::int AS ot_unapproved,
                   count(*) FILTER (WHERE v.flags @> '[{{"kind":"OTCapExceeded"}}]')::int AS ot_cap,
                   count(*) FILTER (WHERE v.late_min > 0)::int AS late
              FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
             WHERE e.company_id = %(company)s AND v.local_date BETWEEN %(f)s AND %(t)s AND {TEAM}
             GROUP BY v.local_date ORDER BY v.local_date""", p)
        return Table(cols(('local_date', 'Date'), ('incomplete', 'Incomplete'), ('absent', 'Absent'), ('late', 'Late'),
                          ('ot_unapproved', 'OT without approval'), ('ot_cap', 'OT cap exceeded')),
                     found, f"exceptions-{q['from']}_{q['to']}")

    if name == 'devices':
        _require(user, 'hr')
        parse(FMT, query)
        found = rows("""
            SELECT d.serial_no, d.name, d.vendor, d.status, d.fw_version, d.last_handshake_at, d.last_seen_at,
                   (d.status = 'active' AND d.vendor NOT IN ('mobile', 'kiosk', 'manual')
                    AND (d.last_seen_at IS NULL OR d.last_seen_at < now() - interval '120 seconds')) AS offline,
                   (SELECT count(*) FROM punch_event p WHERE p.device_id = d.id AND p.local_date = %(today)s::date)::int AS punches_today
              FROM device d WHERE d.company_id = %(company)s ORDER BY d.status, d.serial_no""", p)
        return Table(cols(('serial_no', 'Serial'), ('name', 'Name'), ('vendor', 'Vendor'), ('status', 'Status'),
                          ('fw_version', 'Firmware'), ('last_seen_at', 'Last seen'), ('offline', 'Offline'),
                          ('punches_today', 'Punches today')), found, 'device-health')

    if name == 'punch-audit':
        q = parse(RANGE.extend({'employeeId': Uuid()}), query)
        assert_manages(user, q['employeeId'])
        found = rows("""
            SELECT p.id, p.local_date, p.punched_at, p.direction, p.verify_mode, p.source, p.device_sn, p.ingested_at, p.supersedes_id,
                   (SELECT count(*) FROM punch_event s WHERE s.supersedes_id = p.id) > 0 AS superseded, p.raw->>'reason' AS reason
              FROM punch_event p WHERE p.employee_id = %s AND p.local_date BETWEEN %s AND %s ORDER BY p.punched_at""",
                     [q['employeeId'], q['from'], q['to']])
        out = [{**x, 'time': npt_hhmm(x['punched_at'])} for x in found]
        return Table(cols(('local_date', 'Date'), ('time', 'Time'), ('direction', 'Direction'), ('verify_mode', 'Verify'),
                          ('source', 'Source'), ('device_sn', 'Device'), ('superseded', 'Superseded'), ('reason', 'Reason')),
                     out, f"punches-{q['employeeId']}")

    raise NotFoundError('not found')


ATTENDANCE_REPORTS = ('daily', 'register', 'ot', 'late-early', 'leave-balances', 'headcount', 'exceptions-summary',
                      'devices', 'punch-audit')


# ---------------------------------------------------------------- ad-hoc report builder (HR)

def builder_datasets(user: AuthUser) -> dict:
    _require(user, 'hr')
    return adhoc_datasets()


def run_adhoc(user: AuthUser, definition, *, csv: bool = False, name: str = 'report'):
    """Preview (at most 100 rows, with the total) or, with csv=True, a CSV download of up to 10,000 rows (audited)."""
    _require(user, 'hr')
    d = parse_definition(definition)
    built = build_adhoc_query(d, company_id_of(user), npt_today().isoformat(), 10_000 if csv else 100)
    found = rows(built.sql, built.params)
    if csv:
        audit('export', 'adhoc_report', actor_id=user.id, after={'dataset': d['dataset'], 'columns': d['columns'], 'rows': len(found)})
        return table_response(Table(built.columns, found, name))
    n = one(built.count_sql, built.params)
    total = n['n'] if n else len(found)
    return {'columns': [c.to_dict() for c in built.columns], 'rows': found, 'total': total, 'limited': (n['n'] if n else 0) > len(found)}


SAVED_BODY = Obj({'name': Str(trim=True, min=1, max=120), 'description': Str(trim=True, max=500, nullish=True),
                  'shared': Bool(default=False), 'definition': ADHOC_DEFINITION}, strict=True)
_SAVED_COLS = 'id, name, description, shared, definition, true AS mine, updated_at'


def list_saved(user: AuthUser) -> list[dict]:
    _require(user, 'hr')
    return rows("""SELECT s.id, s.name, s.description, s.shared, s.definition, s.created_by = %(me)s AS mine, u.name AS owner_name, s.updated_at
                     FROM saved_report s LEFT JOIN app_user u ON u.id = s.created_by
                    WHERE s.company_id = %(c)s AND (s.created_by = %(me)s OR s.shared) ORDER BY s.name""",
                {'c': company_id_of(user), 'me': user.id})


@transaction.atomic
def create_saved(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    b = parse(SAVED_BODY, body)
    row = one(f"""INSERT INTO saved_report (company_id, name, description, shared, definition, created_by)
                  VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_SAVED_COLS}""",
              [company_id_of(user), b['name'], b.get('description'), b['shared'], json.dumps(b['definition']), user.id])
    audit('create', 'saved_report', actor_id=user.id, row_id=str(row['id']), after={'name': b['name'], 'dataset': b['definition']['dataset']})
    return row


def _own_saved(user: AuthUser, report_id: str) -> dict:
    parse(Uuid(), report_id)
    r = one('SELECT id, created_by, shared, definition, name FROM saved_report WHERE id = %s AND company_id = %s',
            [report_id, company_id_of(user)])
    if not r or (str(r['created_by']) != user.id and not r['shared']):
        raise NotFoundError('report not found')
    return r


def run_saved(user: AuthUser, report_id: str, *, csv: bool = False):
    """Saved definitions are validated again when run."""
    _require(user, 'hr')
    r = _own_saved(user, report_id)
    return run_adhoc(user, r['definition'], csv=csv, name=r['name'])


@transaction.atomic
def update_saved(user: AuthUser, report_id: str, body: dict) -> dict:
    _require(user, 'hr')
    r = _own_saved(user, report_id)
    if str(r['created_by']) != user.id:
        raise ForbiddenError('only the person who saved this report can change it')
    b = parse(SAVED_BODY, body)
    return one(f"""UPDATE saved_report SET name = %s, description = %s, shared = %s, definition = %s, updated_at = now()
                   WHERE id = %s RETURNING {_SAVED_COLS}""",
               [b['name'], b.get('description'), b['shared'], json.dumps(b['definition']), r['id']])


@transaction.atomic
def delete_saved(user: AuthUser, report_id: str) -> None:
    _require(user, 'hr')
    r = _own_saved(user, report_id)
    if str(r['created_by']) != user.id:
        raise ForbiddenError('only the person who saved this report can delete it')
    execute('DELETE FROM saved_report WHERE id = %s', [r['id']])
    audit('delete', 'saved_report', actor_id=user.id, row_id=str(r['id']), before={'name': r['name']})
