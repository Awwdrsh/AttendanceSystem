"""
Attendance actions with their access rules (port of apps/api/src/modules/attendance/routes.ts). Pages call these
with the signed-in user; each checks roles, company and manager scope exactly as the TS route did.
"""
from __future__ import annotations

from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, has_module, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.core.schema import Arr, IsoDate, Obj, Str, Uuid, parse

from .service import INLINE_RECOMPUTE_MAX, date_range, enqueue_recompute, list_days, recompute_day


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


RECOMPUTE = Obj({'employeeId': Uuid(), 'localDate': IsoDate()})
RECOMPUTE_RANGE = Obj({'employeeIds': Arr(Uuid(), min=1, max=5000), 'from': IsoDate(), 'to': IsoDate()})
EMP_RANGE = Obj({'employeeId': Uuid(), 'from': IsoDate(), 'to': IsoDate()})
EXCEPTIONS = Obj({'from': IsoDate(), 'to': IsoDate(), 'status': Str(optional=True)})
TODAY = Obj({'date': IsoDate()})


def recompute(user: AuthUser, body: dict) -> dict:
    """Recompute one day now (attendance team or a manager for someone they manage)."""
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    b = parse(RECOMPUTE, body)
    assert_manages(user, b['employeeId'], 'attendance')
    return recompute_day(b['employeeId'], b['localDate']).to_dict()


def recompute_range(user: AuthUser, body: dict) -> dict:
    """Bulk: small ranges are recomputed at once so the result is visible on return; larger ones go to the worker."""
    _require(user, *MODULE_ROLES['attendance'])
    b = parse(RECOMPUTE_RANGE, body)
    dates = date_range(b['from'], b['to'])
    if not dates or len(dates) > 62:
        raise BadRequestError('range must be 1 to 62 days')
    ids = list(dict.fromkeys(b['employeeIds']))
    owned = one('SELECT count(*)::int AS n FROM employee WHERE company_id = %s AND id = ANY(%s::uuid[])', [user.company_id, ids])
    if owned['n'] != len(ids):
        raise ForbiddenError('some employees are not in your company')
    if len(ids) * len(dates) <= INLINE_RECOMPUTE_MAX:
        for e in ids:
            for d in dates:
                recompute_day(e, d)
        return {'queued': 0, 'recomputed': len(ids) * len(dates)}
    enqueue_recompute([(e, d) for e in ids for d in dates], 'manual')
    return {'queued': len(ids) * len(dates), 'recomputed': 0}


def days(user: AuthUser, query: dict) -> list[dict]:
    q = parse(EMP_RANGE, query)
    assert_manages(user, q['employeeId'], 'attendance')
    return list_days(q['employeeId'], q['from'], q['to'])


def punches(user: AuthUser, query: dict) -> list[dict]:
    """Raw punches someone may see (their own, their team's, or anyone's for the attendance team)."""
    q = parse(EMP_RANGE, query)
    assert_manages(user, q['employeeId'], 'attendance')
    return rows("""SELECT p.id, p.punched_at, p.local_date, p.direction, p.verify_mode, p.source, p.device_sn, p.supersedes_id,
                          (SELECT count(*) FROM punch_event s WHERE s.supersedes_id = p.id) > 0 AS superseded
                     FROM punch_event p WHERE p.employee_id = %s AND p.local_date BETWEEN %s AND %s ORDER BY p.punched_at""",
                [q['employeeId'], q['from'], q['to']])


def exceptions(user: AuthUser, query: dict) -> list[dict]:
    """Latest days with anomalies in a range; the attendance team company-wide, a manager for the people they manage."""
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    q = parse(EXCEPTIONS, query)
    return rows("""
        SELECT v.id, v.employee_id, e.emp_code, e.name_en, e.name_ne, v.local_date, v.bs_date, v.status, v.worked_min,
               v.late_min, v.early_min, v.ot_min, v.flags
          FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
         WHERE e.company_id = %(c)s AND v.local_date BETWEEN %(f)s AND %(t)s AND v.locked_at IS NULL
           AND (v.status IN ('Incomplete', 'Absent') OR v.flags @> '[{"kind":"OTCapExceeded"}]'
                OR v.flags @> '[{"kind":"OTPreapprovalMissing"}]' OR v.flags @> '[{"kind":"UnpairedPunch"}]'
                OR v.flags @> '[{"kind":"AutoClockOut"}]')
           AND (%(s)s::text IS NULL OR v.status = %(s)s)
           AND (%(all)s OR e.id = %(me)s::uuid OR hj_manages(%(me)s::uuid, e.id, v.local_date))
         ORDER BY v.local_date DESC, e.emp_code LIMIT 1000""",
        {'c': user.company_id, 'f': q['from'], 't': q['to'], 's': q.get('status'), 'all': has_module(user, 'attendance'),
         'me': user.employee_id})


def today(user: AuthUser, query: dict) -> dict:
    """Team Today: everyone's latest status for one date. The attendance team and executives see the company; a
    manager sees the people they manage and themselves."""
    _require(user, *MODULE_ROLES['attendance'], 'manager', 'executive')
    q = parse(TODAY, query)
    company_wide = has_module(user, 'attendance') or has_role(user, 'executive')
    found = rows("""
        SELECT e.id AS employee_id, e.emp_code, e.name_en, e.name_ne, v.status, v.first_in, v.last_out, v.late_min,
               v.worked_min, v.flags,
               coalesce(v.status = 'WFH', false) OR EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = e.id
                 AND p.local_date = %(d)s AND p.raw->>'remote' = 'true') AS remote,
               (SELECT ph.id FROM punch_photo ph WHERE ph.employee_id = e.id AND ph.local_date = %(d)s AND ph.image_enc IS NOT NULL
                 ORDER BY ph.punched_at LIMIT 1) AS photo_id,
               (SELECT wb.kind FROM work_break wb WHERE wb.employee_id = e.id AND wb.ended_at IS NULL AND wb.local_date = %(d)s) AS on_break,
               (SELECT coalesce(sum(coalesce(wb.minutes, floor(extract(epoch FROM now() - wb.started_at) / 60)::int)), 0)::int
                  FROM work_break wb WHERE wb.employee_id = e.id AND wb.local_date = %(d)s) AS break_min,
               (SELECT fv.place FROM field_visit fv WHERE fv.employee_id = e.id AND fv.local_date = %(d)s
                 ORDER BY fv.ended_at IS NULL DESC, fv.started_at DESC LIMIT 1) AS field_place
          FROM employee e LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = %(d)s
         WHERE e.company_id = %(c)s AND e.status = 'active'
           AND (%(all)s OR e.id = %(me)s::uuid OR hj_manages(%(me)s::uuid, e.id, %(d)s::date))
         ORDER BY e.emp_code""",
        {'c': user.company_id, 'd': q['date'], 'all': company_wide, 'me': user.employee_id})
    return {'date': q['date'], 'rows': found}


def today_npt() -> str:
    return npt_today().isoformat()
