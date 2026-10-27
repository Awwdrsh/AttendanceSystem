"""
Inputs of the attendance engine, read from the database (port of apps/api/src/modules/attendance/repo.ts). Local dates
are 'YYYY-MM-DD' strings; instants are epoch milliseconds, as the engine expects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from engines.attendance import DAY_MIN, MIN_MS, Punch, Shift, ShortLeaveWindow, local_midnight_ms

from hajiri.core.db import one, rows

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_ms(dt: datetime) -> int:
    """Epoch ms of an instant, truncated like JS Date.getTime()."""
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int | float) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def iso(v) -> str:
    """'YYYY-MM-DD' for a date, a datetime or a string."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def get_employee(employee_id: str) -> dict | None:
    r = one('SELECT id, company_id, gender, ot_eligible, weekly_off_days, join_date, exit_date FROM employee WHERE id = %s',
            [employee_id])
    if r:
        r['id'], r['company_id'] = str(r['id']), str(r['company_id'])
    return r


def get_branch_id(employee_id: str, local_date: str) -> str | None:
    r = one('SELECT branch_id FROM employee_assignment WHERE employee_id = %s AND valid @> %s::date', [employee_id, local_date])
    return str(r['branch_id']) if r and r['branch_id'] else None


SHIFT_COLS = """id, code, start_time::text, end_time::text, grace_in_min, grace_out_min, early_in_window_min, late_out_window_min,
  break_minutes, half_day_threshold_min, absent_threshold_min, pairing_mode, rounding_min, rounding_direction"""


def to_shift(r: dict) -> Shift:
    """The engine's Shift, carrying the row id (it is part of every stored inputs_hash)."""
    return Shift(
        id=str(r['id']), code=r['code'], start=r['start_time'][:5], end=r['end_time'][:5], grace_in_min=r['grace_in_min'],
        grace_out_min=r['grace_out_min'], early_in_window_min=r['early_in_window_min'],
        late_out_window_min=r['late_out_window_min'], break_minutes=r['break_minutes'],
        half_day_threshold_min=r['half_day_threshold_min'], absent_threshold_min=r['absent_threshold_min'],
        pairing_mode=r['pairing_mode'], rounding_min=r['rounding_min'], rounding_direction=r['rounding_direction'])


def get_shift(shift_id: str) -> Shift | None:
    s = one(f'SELECT {SHIFT_COLS} FROM shift WHERE id = %s', [shift_id])
    return to_shift(s) if s else None


def resolve_shift(employee_id: str, local_date: str) -> Shift | None:
    """approved shift_change regularisation > roster row (even if its shift is NULL) > shift_assignment > none."""
    reg = rows("""SELECT shift_id FROM regularisation_request WHERE employee_id = %s AND local_date = %s AND kind = 'shift_change'
                    AND status = 'approved' ORDER BY decided_at DESC LIMIT 1""", [employee_id, local_date])
    if reg:
        shift_id = reg[0]['shift_id']
    else:
        roster = rows('SELECT shift_id FROM roster WHERE employee_id = %s AND local_date = %s', [employee_id, local_date])
        if roster:
            shift_id = roster[0]['shift_id']
        else:
            sa = one('SELECT shift_id FROM shift_assignment WHERE employee_id = %s AND valid @> %s::date', [employee_id, local_date])
            shift_id = sa['shift_id'] if sa else None
    return get_shift(str(shift_id)) if shift_id else None


def _punch(p: dict) -> Punch:
    return Punch(id=str(p['id']), at=to_ms(p['punched_at']), direction=p['direction'], verify_mode=p['verify_mode'],
                 source=p['source'])


def get_punches_around(employee_id: str, local_date: str) -> list[Punch]:
    """Punches that could fall in any window of this date's shift: from the previous local day to two days after."""
    midnight = local_midnight_ms(local_date)
    found = rows("""
        SELECT p.id, p.punched_at, p.direction, p.verify_mode, p.source FROM punch_event p
         WHERE p.employee_id = %s AND p.punched_at >= %s AND p.punched_at < %s
           AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
         ORDER BY p.punched_at, p.id""",
        [employee_id, from_ms(midnight - DAY_MIN * MIN_MS), from_ms(midnight + 3 * DAY_MIN * MIN_MS)])
    return [_punch(p) for p in found]


def is_holiday(company_id: str, branch_id: str | None, local_date: str, gender: str | None) -> bool:
    return one("""SELECT 1 AS x FROM holiday
                   WHERE company_id = %s AND (branch_id IS NULL OR branch_id = %s) AND local_date = %s AND NOT optional
                     AND (gender_scope = 'all' OR gender_scope = %s) LIMIT 1""",
               [company_id, branch_id, local_date, gender]) is not None


@dataclass(frozen=True)
class LeaveOnDay:
    cover: str
    paid: bool
    leave_type_id: str | None


def approved_leave(employee_id: str, local_date: str) -> LeaveOnDay:
    """Approved leave covering the date: full day, or a half."""
    r = one("""SELECT lr.half_day, lt.paid, lr.leave_type_id FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id
                WHERE lr.employee_id = %s AND lr.status = 'approved' AND %s::date BETWEEN lr.from_date AND lr.to_date
                  AND lr.from_time IS NULL ORDER BY lr.decided_at DESC LIMIT 1""", [employee_id, local_date])
    if r:
        return LeaveOnDay(r['half_day'] or 'full', r['paid'], str(r['leave_type_id']))
    # An approved half-day regularisation is a paid half day; the request does not say which half, so the afternoon
    # is assumed (the employee worked the morning).
    if one("""SELECT 1 AS x FROM regularisation_request WHERE employee_id = %s AND local_date = %s AND kind = 'half_day'
               AND status = 'approved' LIMIT 1""", [employee_id, local_date]):
        return LeaveOnDay('second_half', True, None)
    return LeaveOnDay('none', True, None)


def approved_short_leave(employee_id: str, local_date: str) -> list[ShortLeaveWindow]:
    """Approved hourly leave windows on the date ('HH:MM'); empty when there is none."""
    found = rows("""SELECT to_char(from_time, 'HH24:MI') AS f, to_char(to_time, 'HH24:MI') AS t FROM leave_request
                     WHERE employee_id = %s AND status = 'approved' AND from_date = %s::date AND from_time IS NOT NULL
                     ORDER BY from_time""", [employee_id, local_date])
    return [ShortLeaveWindow(from_=r['f'], to=r['t']) for r in found]


def approved_wfh(employee_id: str, local_date: str) -> bool:
    return one("""SELECT 1 AS x FROM regularisation_request WHERE employee_id = %s AND local_date = %s
                    AND kind IN ('wfh', 'on_duty') AND status = 'approved' LIMIT 1""", [employee_id, local_date]) is not None


def approved_ot_minutes(employee_id: str, local_date: str) -> int | None:
    r = one("""SELECT approved_min FROM ot_request WHERE employee_id = %s AND local_date = %s AND status = 'approved'
                ORDER BY decided_at DESC LIMIT 1""", [employee_id, local_date])
    return r['approved_min'] if r else None


def ot_minutes_this_week(employee_id: str, local_date: str) -> int:
    """OT minutes already recorded this week (Sunday..Saturday) on other days, from the latest version of each day."""
    r = one("""
        WITH wk AS (SELECT (%(d)s::date - extract(dow from %(d)s::date)::int) AS start)
        SELECT coalesce(sum(ot_min), 0)::bigint AS total FROM (
          SELECT DISTINCT ON (local_date) ot_min FROM attendance_day, wk
           WHERE employee_id = %(e)s AND local_date <> %(d)s::date AND local_date >= wk.start AND local_date < wk.start + 7
           ORDER BY local_date, version DESC) d""", {'e': employee_id, 'd': local_date})
    return int(r['total']) if r else 0


def latest_version(employee_id: str, local_date: str) -> dict | None:
    return one("""SELECT id, version, inputs_hash, locked_at FROM attendance_day WHERE employee_id = %s AND local_date = %s
                   ORDER BY version DESC LIMIT 1""", [employee_id, local_date])


def bs_date_for(local_date: str) -> str | None:
    r = one('SELECT bs_date FROM bs_calendar WHERE ad_date = %s', [local_date])
    return r['bs_date'] if r else None


def attendance_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'attendance.policy'", [company_id])
    return (r or {}).get('value') or {}


# ---------------------------------------------------------------- bulk inputs for previews (same rules as above)

@dataclass
class DayInputs:
    shift: Shift | None
    punches: list[Punch]
    holiday: bool
    leave: str
    wfh: bool
    ot_preapproved_min: int | None
    ot_minutes_this_week: int
    stored: dict | None
    short_leave: list[ShortLeaveWindow]


def _in_range(rng, d: str) -> bool:
    """A daterange value (psycopg Range or Postgres text '[2026-01-01,2026-02-01)') contains d."""
    if not isinstance(rng, str):
        lo = iso(rng.lower) if rng.lower is not None else None
        hi = iso(rng.upper) if rng.upper is not None else None
        if rng.isempty:
            return False
        lo_ok = lo is None or (d >= lo if rng.lower_inc else d > lo)
        hi_ok = hi is None or (d <= hi if rng.upper_inc else d < hi)
        return lo_ok and hi_ok
    m = re.fullmatch(r'[\[(]([^,]*),([^)\]]*)[)\]]', str(rng))
    if not m:
        return False
    return (not m[1] or d >= m[1]) and (not m[2] or d < m[2])


def load_range_inputs(company_id: str, employees: list[dict], dates: list[str]) -> dict[str, DayInputs]:
    """Everything compute_day needs for every (employee, date) in a range, in a fixed number of queries."""
    ids = [str(e['id']) for e in employees]
    lo, hi = dates[0], dates[-1]
    week_start = add_days(lo, -((date.fromisoformat(lo).weekday() + 1) % 7))
    assign = rows("SELECT employee_id, branch_id, valid::text AS valid FROM employee_assignment WHERE employee_id = ANY(%s::uuid[]) AND valid && daterange(%s::date, %s::date, '[]')", [ids, lo, hi])
    regs = rows("""SELECT employee_id, local_date, kind, shift_id, decided_at FROM regularisation_request
                    WHERE employee_id = ANY(%s::uuid[]) AND status = 'approved' AND local_date BETWEEN %s AND %s
                      AND kind IN ('shift_change', 'half_day', 'wfh', 'on_duty') ORDER BY decided_at DESC NULLS LAST""", [ids, lo, hi])
    roster = rows('SELECT employee_id, local_date, shift_id FROM roster WHERE employee_id = ANY(%s::uuid[]) AND local_date BETWEEN %s AND %s', [ids, lo, hi])
    sa = rows("SELECT employee_id, shift_id, valid::text AS valid FROM shift_assignment WHERE employee_id = ANY(%s::uuid[]) AND valid && daterange(%s::date, %s::date, '[]')", [ids, lo, hi])
    punches = rows("""SELECT p.id, p.employee_id, p.punched_at, p.direction, p.verify_mode, p.source FROM punch_event p
                       WHERE p.employee_id = ANY(%s::uuid[]) AND p.punched_at >= %s AND p.punched_at < %s
                         AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)
                       ORDER BY p.punched_at, p.id""",
                   [ids, from_ms(local_midnight_ms(lo) - DAY_MIN * MIN_MS), from_ms(local_midnight_ms(hi) + 3 * DAY_MIN * MIN_MS)])
    leave = rows("""SELECT lr.employee_id, lr.from_date, lr.to_date, lr.half_day, to_char(lr.from_time, 'HH24:MI') AS from_hm,
                           to_char(lr.to_time, 'HH24:MI') AS to_hm
                      FROM leave_request lr WHERE lr.employee_id = ANY(%s::uuid[]) AND lr.status = 'approved'
                       AND lr.to_date >= %s AND lr.from_date <= %s ORDER BY lr.decided_at DESC NULLS LAST""", [ids, lo, hi])
    ot = rows("""SELECT employee_id, local_date, approved_min FROM ot_request WHERE employee_id = ANY(%s::uuid[]) AND status = 'approved'
                  AND local_date BETWEEN %s AND %s ORDER BY decided_at DESC NULLS LAST""", [ids, lo, hi])
    stored = rows("""SELECT DISTINCT ON (employee_id, local_date) employee_id, local_date, status, late_min, ot_min FROM attendance_day
                      WHERE employee_id = ANY(%s::uuid[]) AND local_date BETWEEN %s AND %s
                      ORDER BY employee_id, local_date, version DESC""", [ids, week_start, add_days(hi, 6)])
    holidays = rows("""SELECT local_date, branch_id, gender_scope FROM holiday WHERE company_id = %s AND NOT optional
                        AND local_date BETWEEN %s AND %s""", [company_id, lo, hi])
    shift_ids = {str(x['shift_id']) for x in regs + roster + sa if x.get('shift_id')}
    shifts = {str(r['id']): to_shift(r) for r in rows(f'SELECT {SHIFT_COLS} FROM shift WHERE id = ANY(%s::uuid[])', [list(shift_ids)])}

    def key(e, d):
        return f'{e}|{d}'

    def first(items, k):
        out = {}
        for r in items:
            out.setdefault(k(r), r)
        return out

    shift_reg = first([r for r in regs if r['kind'] == 'shift_change'], lambda r: key(r['employee_id'], iso(r['local_date'])))
    half_reg = {key(r['employee_id'], iso(r['local_date'])) for r in regs if r['kind'] == 'half_day'}
    wfh_reg = {key(r['employee_id'], iso(r['local_date'])) for r in regs if r['kind'] in ('wfh', 'on_duty')}
    roster_by = first(roster, lambda r: key(r['employee_id'], iso(r['local_date'])))
    ot_by = first(ot, lambda r: key(r['employee_id'], iso(r['local_date'])))
    stored_by = {key(r['employee_id'], iso(r['local_date'])): r for r in stored}
    punches_by: dict[str, list[Punch]] = {}
    for p in punches:
        punches_by.setdefault(str(p['employee_id']), []).append(_punch(p))

    out: dict[str, DayInputs] = {}
    for e in employees:
        eid = str(e['id'])
        allp = punches_by.get(eid, [])
        for d in dates:
            k = key(eid, d)
            if k in shift_reg:
                shift_id = shift_reg[k]['shift_id']
            elif k in roster_by:
                shift_id = roster_by[k]['shift_id']
            else:
                shift_id = next((r['shift_id'] for r in sa if str(r['employee_id']) == eid and _in_range(r['valid'], d)), None)
            branch_id = next((r['branch_id'] for r in assign if str(r['employee_id']) == eid and _in_range(r['valid'], d)), None)
            lv = next((r for r in leave if str(r['employee_id']) == eid and not r['from_hm'] and iso(r['from_date']) <= d <= iso(r['to_date'])), None)
            short = sorted((ShortLeaveWindow(from_=r['from_hm'], to=r['to_hm']) for r in leave
                            if str(r['employee_id']) == eid and r['from_hm'] and r['to_hm'] and iso(r['from_date']) == d),
                           key=lambda w: w.from_)
            cover = (lv['half_day'] or 'full') if lv else ('second_half' if k in half_reg else 'none')
            ws = add_days(d, -((date.fromisoformat(d).weekday() + 1) % 7))
            week = 0
            for i in range(7):
                wd = add_days(ws, i)
                if wd != d:
                    week += (stored_by.get(key(eid, wd)) or {}).get('ot_min') or 0
            lo_ms = local_midnight_ms(d) - DAY_MIN * MIN_MS
            hi_ms = local_midnight_ms(d) + 3 * DAY_MIN * MIN_MS
            out[k] = DayInputs(
                shift=shifts.get(str(shift_id)) if shift_id else None,
                punches=[p for p in allp if lo_ms <= p.at < hi_ms],
                holiday=any(iso(h['local_date']) == d and (h['branch_id'] is None or h['branch_id'] == branch_id)
                            and (h['gender_scope'] == 'all' or h['gender_scope'] == e.get('gender')) for h in holidays),
                leave=cover, wfh=k in wfh_reg, ot_preapproved_min=(ot_by.get(k) or {}).get('approved_min'),
                ot_minutes_this_week=week, stored=stored_by.get(k), short_leave=short)
    return out
