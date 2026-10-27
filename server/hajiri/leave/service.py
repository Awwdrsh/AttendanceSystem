"""
Leave rules (port of apps/api/src/modules/leave/service.ts): day counting with the sandwich rule, hourly leave,
event-based statutory leave, the ledger and balances, substitute (comp-off) credits and encashment at separation.

Numbers follow the TS code: ledger sums are read as text and turned into floats (JavaScript Number), and results are
rounded with Math.round semantics, so explanations and JSON match the TS API digit for digit.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from engines.bs_calendar import ad_to_bs, fiscal_year_of, weekday_of

from hajiri.attendance.repo import resolve_shift
from hajiri.attendance.service import date_range
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.web.format import to_fixed


# ---------------------------------------------------------------- number helpers (JavaScript semantics)

def num(v) -> float:
    """Number(v) of a numeric column or text; None is 0."""
    return 0.0 if v is None else float(v)


def js_round(x: float) -> float:
    """Math.round: halves go up (towards +infinity)."""
    return float(math.floor(x + 0.5))


def round2(x: float) -> float:
    """Math.round(x * 100) / 100."""
    return js_round(x * 100) / 100


def js(x) -> str:
    """How a JavaScript template literal prints a number: 2 not 2.0, 0.5, 1.1."""
    if isinstance(x, bool):
        return 'true' if x else 'false'
    if isinstance(x, (int, float)):
        f = float(x)
        if f.is_integer():
            return str(int(f))
        return repr(f)
    return str(x)


def out(x):
    """A number for JSON: integral values as int (JSON.stringify prints 10, not 10.0)."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def fixed(x: float, digits: int) -> str:
    return to_fixed(x, digits)


def iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def add_iso(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- rows

def _type(r: dict | None) -> dict | None:
    if r is None:
        return None
    t = dict(r)
    for k in ('id', 'company_id', 'club_with_id'):
        if t.get(k) is not None:
            t[k] = str(t[k])
    return t


def get_leave_type(type_id: str) -> dict | None:
    return _type(one('SELECT * FROM leave_type WHERE id = %s', [str(type_id)]))


def leave_type_by_code(company_id: str, code: str) -> dict | None:
    return _type(one('SELECT * FROM leave_type WHERE company_id = %s AND code = %s', [str(company_id), code]))


def get_employee_lite(employee_id: str) -> dict | None:
    r = one("""SELECT id, company_id, gender, weekly_off_days, join_date, probation_end, exit_date, status
                 FROM employee WHERE id = %s""", [str(employee_id)])
    if not r:
        return None
    return {'id': str(r['id']), 'company_id': str(r['company_id']), 'gender': r['gender'],
            'weekly_off_days': list(r['weekly_off_days'] or []), 'join_date': iso(r['join_date']),
            'probation_end': iso(r['probation_end']), 'exit_date': iso(r['exit_date']), 'status': r['status']}


# ---------------------------------------------------------------- day counting

def working_days_between(emp: dict, from_: str, to: str) -> tuple[list[str], list[str]]:
    """Dates in [from, to] that are neither weekly offs nor (gender-aware, branch-aware) holidays: (working, non_working)."""
    branch = one('SELECT branch_id FROM employee_assignment WHERE employee_id = %s AND valid @> %s::date', [emp['id'], from_])
    hol = rows("""SELECT local_date FROM holiday WHERE company_id = %s AND (branch_id IS NULL OR branch_id = %s)
                    AND local_date BETWEEN %s AND %s AND NOT optional AND (gender_scope = 'all' OR gender_scope = %s)""",
               [emp['company_id'], branch['branch_id'] if branch else None, from_, to, emp['gender']])
    holidays = {iso(h['local_date']) for h in hol}
    working: list[str] = []
    non_working: list[str] = []
    offs = emp['weekly_off_days']
    for d in date_range(from_, to):
        (non_working if d in holidays or weekday_of(d) in offs else working).append(d)
    return working, non_working


def group_head(t: dict) -> dict:
    """The type whose order and sandwich rule a type follows: the type it is clubbed with, or itself."""
    if not t.get('club_with_id'):
        return t
    return get_leave_type(t['club_with_id']) or t


def compute_leave_days(emp: dict, t: dict, from_: str, to: str, half_day: str | None, sandwich_policy: bool) -> dict:
    """
    Days deducted for a request (plan §10.6): working days only, unless the sandwich rule bridges the range with leave on
    both ends and only holidays/weekly-offs between. Half-day requests are a single date worth 0.5.
    """
    if half_day:
        if from_ != to:
            raise BadRequestError('half-day leave must be a single date')
        if not t['allow_half_day']:
            raise BadRequestError(f"{t['name_en']} cannot be taken as a half day")
        working, _ = working_days_between(emp, from_, to)
        if not working:
            raise BadRequestError(f'{from_} is not a working day')
        return {'days': 0.5, 'workingDays': working, 'excludedDays': [], 'sandwichApplied': False,
                'explanation': 'Half day will be deducted'}
    if t['event_days'] is not None:
        # Event-based statutory leave (maternity, maternity-care, mourning) runs in calendar days.
        all_ = date_range(from_, to)
        return {'days': len(all_), 'workingDays': all_, 'excludedDays': [], 'sandwichApplied': False,
                'explanation': f"{len(all_)} calendar day(s) of {t['name_en']}"}
    working, non_working = working_days_between(emp, from_, to)
    sandwich = ((group_head(t)['sandwich_rule'] or sandwich_policy) and len(working) >= 2 and len(non_working) > 0
                and working[0] == from_ and working[-1] == to)
    if sandwich:
        all_ = date_range(from_, to)
        return {'days': len(all_), 'workingDays': all_, 'excludedDays': [], 'sandwichApplied': True,
                'explanation': f'{len(all_)} calendar days will be deducted (sandwich rule: {len(non_working)} non-working '
                               'day(s) between leave days count)'}
    if not working:
        raise BadRequestError('the selected range contains no working days')
    explanation = (f'{len(working)} working day(s) will be deducted; {len(non_working)} weekly-off/holiday day(s) excluded'
                   if non_working else f'{len(working)} working day(s) will be deducted')
    return {'days': len(working), 'workingDays': working, 'excludedDays': non_working, 'sandwichApplied': False,
            'explanation': explanation}


HOURLY_MIN_MINUTES = 30


def _to_min(s: str) -> int:
    return int(s[0:2]) * 60 + int(s[3:5])


def compute_hourly_leave(emp: dict, t: dict, day: str, from_time: str, to_time: str) -> dict:
    """
    Hourly (short) leave on one working day: the window must sit inside the day's (daytime) shift, and is deducted as
    minutes / scheduled working minutes of that shift (span minus the fixed break), to 2 decimals.
    """
    if not t['allow_hourly']:
        raise BadRequestError(f"{t['name_en']} cannot be taken by the hour")
    if not t['paid'] or t['event_days'] is not None:
        raise BadRequestError('only paid leave with a balance can be taken by the hour')
    working, _ = working_days_between(emp, day, day)
    if not working:
        raise BadRequestError('hourly leave must be on a working day')
    shift = resolve_shift(emp['id'], day)
    if not shift:
        raise BadRequestError('there is no shift on this day to take hourly leave from')
    start, end = _to_min(shift.start), _to_min(shift.end)
    if end <= start:
        raise BadRequestError('hourly leave is not available on shifts that cross midnight; apply for a half or full day')
    f, tt = _to_min(from_time), _to_min(to_time)
    if tt <= f:
        raise BadRequestError('the end time must be after the start time')
    if f < start or tt > end:
        raise BadRequestError(f'hourly leave must be within your shift ({shift.start}–{shift.end})')
    minutes = tt - f
    if minutes < HOURLY_MIN_MINUTES:
        raise BadRequestError(f'hourly leave must be at least {HOURLY_MIN_MINUTES} minutes')
    scheduled = end - start - shift.break_minutes
    if scheduled <= 0 or minutes >= scheduled:
        raise BadRequestError('this covers the whole working day; apply for a full day instead')
    days = max(0.01, round2(minutes / scheduled))
    hours = round2(minutes / 60)
    return {'days': out(days), 'hours': out(hours), 'minutes': minutes, 'workingDays': [day], 'excludedDays': [],
            'sandwichApplied': False,
            'explanation': f'{js(hours)} hour(s) of a {js(round2(scheduled / 60))}-hour working day = {js(days)} day(s) will be deducted'}


EVENT_GAP_DAYS = 30


def event_days_used(employee_id: str, leave_type_id: str, from_: str, to: str, exclude_id: str | None = None) -> float:
    """Days already requested (pending or approved) for the same event: requests of the type within EVENT_GAP_DAYS."""
    r = one("""SELECT coalesce(sum(days_requested), 0)::text AS d FROM leave_request
                WHERE employee_id = %s AND leave_type_id = %s AND status IN ('pending', 'approved') AND (%s::uuid IS NULL OR id <> %s::uuid)
                  AND to_date >= %s::date - %s::int AND from_date <= %s::date + %s::int""",
            [str(employee_id), str(leave_type_id), exclude_id, exclude_id, from_, EVENT_GAP_DAYS, to, EVENT_GAP_DAYS])
    return num(r['d'])


def ledger_balance(employee_id: str, leave_type_id: str, as_of: str | None = None) -> float:
    r = one("""SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s
                 AND (%s::date IS NULL OR effective_date <= %s::date)""", [str(employee_id), str(leave_type_id), as_of, as_of])
    return num(r['b'])


def pending_days(employee_id: str, leave_type_id: str) -> float:
    """Days held by pending requests, including the non-working days a request bridges on the type that pays for them."""
    lt = str(leave_type_id)
    r = one("""SELECT coalesce(sum(CASE WHEN leave_type_id = %s THEN days_requested ELSE 0 END
                                 + CASE WHEN bridge_type_id = %s THEN bridge_days ELSE 0 END), 0)::text AS d
                 FROM leave_request WHERE employee_id = %s AND status = 'pending' AND (leave_type_id = %s OR bridge_type_id = %s)""",
            [lt, lt, str(employee_id), lt, lt])
    return num(r['d'])


def bridge_across(emp: dict, t: dict, from_: str, to: str, sandwich_policy: bool, exclude_id: str | None = None) -> dict | None:
    """
    Sandwich across requests: when this request and another pending or approved full-day leave are separated only by
    weekly offs or holidays, those days are charged to whichever of the two types comes first in the leave order (a
    clubbed type takes the order of the type it is clubbed with; on a tie, this request's type), if that type or the
    company applies the sandwich rule. Unpaid and event-based types never absorb bridge days, and a gap is charged once.
    """
    if t['event_days'] is not None:
        return None
    adj = rows("""SELECT id, leave_type_id, from_date::text, to_date::text FROM leave_request
                   WHERE employee_id = %s AND status IN ('pending', 'approved') AND half_day IS NULL AND from_time IS NULL
                     AND (%s::uuid IS NULL OR id <> %s::uuid)
                     AND (to_date BETWEEN %s::date - 15 AND %s::date - 2 OR from_date BETWEEN %s::date + 2 AND %s::date + 15)
                   ORDER BY (to_date < %s::date) DESC, abs(from_date - %s::date)""",
               [emp['id'], exclude_id, exclude_id, from_, from_, to, to, from_, from_])
    my_head = group_head(t)
    for a in adj:
        before = a['to_date'] < from_
        gap_from = add_iso(a['to_date'], 1) if before else add_iso(to, 1)
        gap_to = add_iso(from_, -1) if before else add_iso(a['from_date'], -1)
        if gap_from > gap_to:
            continue
        working, non_working = working_days_between(emp, gap_from, gap_to)
        if working or not non_working:
            continue
        taken = one("""SELECT 1 AS x FROM leave_request WHERE employee_id = %s AND status IN ('pending', 'approved')
                         AND (%s::uuid IS NULL OR id <> %s::uuid)
                         AND ((bridge_days > 0 AND bridge_from <= %s::date AND bridge_to >= %s::date)
                              OR (from_date <= %s::date AND to_date >= %s::date)) LIMIT 1""",
                    [emp['id'], exclude_id, exclude_id, gap_to, gap_from, gap_to, gap_from])
        if taken:
            continue
        other = get_leave_type(str(a['leave_type_id']))
        if not other:
            continue
        other_head = group_head(other)
        mine_first = my_head['id'] == other_head['id'] or my_head['sort_order'] <= other_head['sort_order']
        owner = t if mine_first else other
        owner_head = my_head if mine_first else other_head
        if not (sandwich_policy or owner_head['sandwich_rule']):
            continue
        if not owner['paid'] or owner['event_days'] is not None:
            continue
        days = len(non_working)
        return {
            'days': days, 'typeId': owner['id'], 'typeName': owner['name_en'], 'from': gap_from, 'to': gap_to,
            'adjacentRequestId': str(a['id']),
            'explanation': f"{days} weekly-off/holiday day(s) between this and your "
                           f"{'other' if other['id'] == t['id'] else other['name_en']} leave will be charged to "
                           f"{owner['name_en']} (sandwich rule)",
        }
    return None


# ---------------------------------------------------------------- balances

def balances(emp: dict, as_of_ad: str) -> list[dict]:
    """Balance explanation per plan §13.5: Opening (1 Shrawan) + accruals - taken - lapsed + carried forward."""
    fy = fiscal_year_of(ad_to_bs(as_of_ad))
    types = rows("""SELECT * FROM leave_type WHERE company_id = %s AND (gender_scope = 'all' OR gender_scope = %s)
                     ORDER BY sort_order, code""", [emp['company_id'], emp['gender']])
    result = []
    for t in map(_type, types):
        r = rows("""SELECT reason, (effective_date >= %s::date) AS in_fy, coalesce(sum(delta_days), 0)::text AS total FROM leave_ledger
                     WHERE employee_id = %s AND leave_type_id = %s GROUP BY reason, (effective_date >= %s::date)""",
                 [fy.start_ad, emp['id'], t['id'], fy.start_ad])

        def s(reason: str, in_fy: bool) -> float:
            return sum((num(x['total']) for x in r if x['reason'] == reason and x['in_fy'] == in_fy), 0.0)

        opening = sum((num(x['total']) for x in r if not x['in_fy']), 0.0)
        accrued = s('accrual', True) + s('allocation', True)
        taken = -s('usage', True)
        cancelled = s('cancellation', True)
        lapsed = -s('expiry', True)
        carried = s('carry_forward', True)
        adjustments = s('adjustment', True)
        comp_off = s('comp_off', True)
        encashed = -s('encashment', True)
        balance = sum((num(x['total']) for x in r), 0.0)
        pending = pending_days(emp['id'], t['id'])
        base = {'leaveTypeId': t['id'], 'code': t['code'], 'nameEn': t['name_en'], 'nameNe': t['name_ne'], 'paid': t['paid'],
                'encashable': t['encashable']}
        if t['event_days'] is not None:
            per_event = num(t['event_days'])
            ev = one("""SELECT from_date::text, to_date::text FROM leave_request WHERE employee_id = %s AND leave_type_id = %s
                          AND status IN ('pending', 'approved') AND to_date >= %s::date - %s::int ORDER BY from_date LIMIT 1""",
                     [emp['id'], t['id'], as_of_ad, EVENT_GAP_DAYS])
            used = event_days_used(emp['id'], t['id'], ev['from_date'], ev['to_date']) if ev else 0.0
            left = max(0.0, per_event - used)
            extra = f'; {js(used)} day(s) already requested for the current event, {js(left)} left' if used else ''
            result.append({**base, 'opening': 0, 'accrued': 0, 'taken': out(taken), 'cancelled': out(cancelled), 'lapsed': 0,
                           'carried': 0, 'adjustments': 0, 'compOff': 0, 'encashed': 0, 'pending': out(pending),
                           'balance': out(left), 'available': out(left), 'fiscalYear': fy.label,
                           'explanation': f'Up to {js(per_event)} paid day(s) per event (statutory){extra}; '
                                          f'{fixed(taken, 2)} day(s) taken this fiscal year'})
            continue
        result.append({**base, 'opening': out(opening), 'accrued': out(accrued), 'taken': out(taken), 'cancelled': out(cancelled),
                       'lapsed': out(lapsed), 'carried': out(carried), 'adjustments': out(adjustments), 'compOff': out(comp_off),
                       'encashed': out(encashed), 'pending': out(pending), 'balance': out(balance),
                       'available': out(balance - pending), 'fiscalYear': fy.label,
                       'explanation': f'Opening {fixed(opening, 2)} on 1 Shrawan {fy.start_year} + accrued {fixed(accrued, 2)} '
                                      f'+ comp-off {fixed(comp_off, 2)} + adjustments {fixed(adjustments, 2)} − taken {fixed(taken, 2)} '
                                      f'+ cancelled {fixed(cancelled, 2)} − lapsed {fixed(lapsed, 2)} − encashed {fixed(encashed, 2)} '
                                      f'= {fixed(balance, 2)} days' + (f' ({js(pending)} pending)' if pending else '')})
    return result


# ---------------------------------------------------------------- used by other modules

def credit_comp_off(employee_id: str, days: float, expires_on: str, *, ref_type: str, ref_id: str, note: str | None = None,
                    posted_by: str | None = None) -> str:
    """Credit substitute leave for holiday/weekly-off work or comp-off (Sec 42: expires 21 days after). Returns the ledger id."""
    from hajiri.core.clock import npt_today
    emp = get_employee_lite(employee_id)
    if not emp:
        raise NotFoundError('employee not found')
    t = leave_type_by_code(emp['company_id'], 'SUBSTITUTE')
    if not t:
        raise NotFoundError('SUBSTITUTE leave type is not configured')
    r = one("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, expires_on, note, posted_by)
               VALUES (%s, %s, %s::date, %s, 'comp_off', %s, %s, %s, %s, %s) RETURNING id""",
            [str(employee_id), t['id'], npt_today().isoformat(), days, ref_type, str(ref_id), expires_on, note, posted_by])
    return str(r['id'])


def encashable_balances(employee_id: str) -> list[dict]:
    """Encashable balances at separation (Sec 49(2)): [{'leaveTypeId', 'code', 'days'}] with days > 0."""
    emp = get_employee_lite(employee_id)
    if not emp:
        raise NotFoundError('employee not found')
    result = []
    for t in rows('SELECT id, code FROM leave_type WHERE company_id = %s AND encashable ORDER BY sort_order', [emp['company_id']]):
        b = ledger_balance(employee_id, str(t['id']))
        if b > 0:
            result.append({'leaveTypeId': str(t['id']), 'code': t['code'], 'days': out(b)})
    return result


def post_encashment(employee_id: str, leave_type_id: str, days: float, *, ref_type: str, ref_id: str,
                    posted_by: str | None = None) -> None:
    """Post the encashment row (payroll final settlement calls this once the run is locked)."""
    from hajiri.core.clock import npt_today
    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by)
               VALUES (%s, %s, %s::date, %s, 'encashment', %s, %s, %s)""",
            [str(employee_id), str(leave_type_id), npt_today().isoformat(), -days, ref_type, str(ref_id), posted_by])


def assert_no_overlap(employee_id: str, from_: str, to: str, exclude_id: str | None = None) -> None:
    r = one("""SELECT 1 AS x FROM leave_request WHERE employee_id = %s AND status IN ('pending', 'approved')
                 AND from_date <= %s::date AND to_date >= %s::date AND (%s::uuid IS NULL OR id <> %s::uuid) LIMIT 1""",
            [str(employee_id), to, from_, exclude_id, exclude_id])
    if r:
        raise ConflictError('an existing leave request already covers part of this range')


def has_locked_day(employee_id: str, from_: str, to: str) -> bool:
    """Any latest attendance day in [from, to] already locked by payroll? Leave decisions never change such days."""
    return one("""SELECT 1 AS x FROM v_attendance_latest WHERE employee_id = %s AND local_date BETWEEN %s AND %s
                    AND locked_at IS NOT NULL LIMIT 1""", [str(employee_id), from_, to]) is not None
