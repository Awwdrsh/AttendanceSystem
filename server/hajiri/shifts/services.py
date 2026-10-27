"""
Shifts, holidays, geofence locations, the UI calendar, rosters and shift assignments (port of
apps/api/src/modules/shifts/routes.ts). Every function takes the signed-in user and a body or query in the TS (camelCase)
shape, checks roles, company and manager scope as the TS route did, and returns what the route answered.

Pages and HTMX views call these; other slices may too:

- list_shifts(company_id) / shifts(user)       every signed-in user (GET /shifts)
- holidays(user, query)                        GET /shifts/holidays
- calendar(user, {from, to, employeeId?})      GET /shifts/calendar: days with BS date, weekly off and holidays
- roster(user, {from, to, branchId?})          GET /shifts/roster
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db import transaction

from engines.bs_calendar import weekday_of
from hajiri.attendance.service import date_range, enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, has_module, has_role
from hajiri.core.bsdate import bs_range
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Hhmm, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, user_id_for_employee

# ---------------------------------------------------------------- access helpers


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _require_module(user: AuthUser, module: str) -> None:
    _require(user, *MODULE_ROLES[module])


def _company_of(user: AuthUser) -> str:
    if not user or not user.company_id:
        raise ForbiddenError('user is not attached to a company')
    return str(user.company_id)


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


def _today() -> str:
    return npt_today().isoformat()


def _assert_branch(company_id: str | None, branch_id: str | None) -> None:
    """A branch id from the body must belong to the caller's company."""
    if not branch_id:
        return
    if not one('SELECT 1 AS x FROM branch WHERE id = %s AND company_id = %s', [branch_id, company_id]):
        raise NotFoundError('branch not found')


def _active_employee_ids(company_id: str) -> list[str]:
    return [str(r['id']) for r in rows("SELECT id FROM employee WHERE company_id = %s AND status = 'active'", [company_id])]


def _provided(parsed: dict, body: dict) -> dict:
    """Keys actually present in the request body: PATCH must not apply schema defaults to untouched fields."""
    keys = set(body.keys()) if isinstance(body, dict) else set()
    return {k: v for k, v in parsed.items() if k in keys}


def _partial(schema: Obj) -> Obj:
    """zod .partial(): every field optional, defaults not applied."""
    out = {}
    for k, s in schema.fields.items():
        import copy
        c = copy.copy(s)
        c.optional = True
        c.default = _MISSING
        out[k] = c
    return Obj(out)


from hajiri.core.schema import MISSING as _MISSING  # noqa: E402

# ---------------------------------------------------------------- schemas

SHIFT_BODY = Obj({
    'code': Str(min=1, max=20),
    'nameEn': Str(min=1, max=80),
    'nameNe': Str(max=80, optional=True),
    'start': Hhmm(),
    'end': Hhmm(),
    'graceInMin': Int(min=0, max=240, default=0),
    'graceOutMin': Int(min=0, max=240, default=0),
    'earlyInWindowMin': Int(min=0, max=720, default=60),
    'lateOutWindowMin': Int(min=0, max=720, default=360),
    'breakMinutes': Int(min=0, max=240, default=0),
    'halfDayThresholdMin': Int(min=0, max=720, default=240),
    'absentThresholdMin': Int(min=0, max=720, default=120),
    'pairingMode': Enum(['alternating', 'strict_log_type', 'first_last'], default='alternating'),
    'roundingMin': Int(min=0, max=60, default=0),
    'roundingDirection': Enum(['nearest', 'down', 'up'], default='nearest'),
})
SHIFT_PATCH = _partial(SHIFT_BODY)

HOLIDAY_BODY = Obj({
    'localDate': IsoDate(),
    'nameEn': Str(min=1, max=120),
    'nameNe': Str(max=120, optional=True),
    'branchId': Uuid(nullable=True, optional=True),
    'genderScope': Enum(['all', 'female'], default='all'),
    'optional': Bool(default=False),
})
HOLIDAY_PATCH = _partial(HOLIDAY_BODY)
HOLIDAY_QUERY = Obj({'from': IsoDate(optional=True), 'to': IsoDate(optional=True), 'branchId': Uuid(optional=True)})

LOCATION_BODY = Obj({
    'name': Str(min=1, max=80),
    'branchId': Uuid(nullable=True, optional=True),
    'lat': Num(min=-90, max=90),
    'lng': Num(min=-180, max=180),
    'radiusM': Int(min=20, max=5000, default=150),
})
LOCATION_PATCH = _partial(LOCATION_BODY)

CALENDAR_QUERY = Obj({'from': IsoDate(), 'to': IsoDate(), 'employeeId': Uuid(optional=True)})
ROSTER_QUERY = Obj({'from': IsoDate(), 'to': IsoDate(), 'branchId': Uuid(optional=True)})
ROSTER_PUT = Obj({'rows': Arr(Obj({'employeeId': Uuid(), 'localDate': IsoDate(), 'shiftId': Uuid(nullable=True)}), min=1, max=5000)})
ROSTER_DELETE = Obj({'employeeId': Uuid(), 'localDate': IsoDate()})
COPY_WEEK = Obj({'weekStart': IsoDate()})
PUBLISH = Obj({'from': IsoDate(), 'to': IsoDate()})
ASSIGN = Obj({'employeeId': Uuid(), 'shiftId': Uuid(), 'from': IsoDate()})
IMPORT_DEFAULT = Obj({'bsYear': Int()})

# Public holidays for BS 2083 (14 Apr 2026 to 13 Apr 2027), checked in October 2026 against the published lists on
# hamropatro.com and katigate.com. Community or region-specific days (Eid, Lhosars, Indra Jatra, Holi by region) are
# imported as optional so HR can decide which to observe. The employer still picks the paid days (Sec 41).
HOLIDAYS_2083: list[tuple[str, str, str, bool]] = [
    ('2026-04-14', 'Nepali New Year', 'all', False),
    ('2026-05-01', 'Buddha Jayanti and Labour Day', 'all', False),
    ('2026-05-28', 'Bakar Eid', 'all', True),
    ('2026-05-29', 'Republic Day', 'all', False),
    ('2026-08-26', "Prophet Muhammad's Birthday", 'all', True),
    ('2026-08-28', 'Janai Purnima', 'all', False),
    ('2026-09-04', 'Krishna Janmashtami', 'all', False),
    ('2026-09-08', "Gen Z Martyrs' Day", 'all', False),
    ('2026-09-14', 'Haritalika Teej', 'female', False),
    ('2026-09-19', 'Constitution Day', 'all', False),
    ('2026-09-25', 'Indra Jatra', 'all', True),
    ('2026-10-04', 'Jitiya', 'female', True),
    ('2026-10-11', 'Ghatasthapana', 'all', False),
    ('2026-10-17', 'Dashain (Phulpati)', 'all', False),
    ('2026-10-18', 'Dashain (Maha Ashtami)', 'all', False),
    ('2026-10-19', 'Dashain holiday', 'all', False),
    ('2026-10-20', 'Dashain (Maha Navami)', 'all', False),
    ('2026-10-21', 'Dashain (Vijaya Dashami)', 'all', False),
    ('2026-10-22', 'Dashain (Ekadashi)', 'all', False),
    ('2026-10-23', 'Dashain holiday', 'all', False),
    ('2026-11-08', 'Tihar (Laxmi Puja)', 'all', False),
    ('2026-11-09', 'Tihar (Gai Puja)', 'all', False),
    ('2026-11-10', 'Tihar (Govardhan Puja and Mha Puja)', 'all', False),
    ('2026-11-11', 'Tihar (Bhai Tika)', 'all', False),
    ('2026-11-12', 'Tihar holiday', 'all', False),
    ('2026-11-15', 'Chhath', 'all', False),
    ('2026-11-24', 'Guru Nanak Jayanti', 'all', True),
    ('2026-12-03', 'International Day of Persons with Disabilities', 'all', True),
    ('2026-12-24', 'Udhauli Parva', 'all', True),
    ('2026-12-25', 'Christmas', 'all', False),
    ('2026-12-30', 'Tamu Lhosar', 'all', True),
    ('2027-01-11', 'Prithvi Jayanti', 'all', False),
    ('2027-01-15', 'Maghe Sankranti', 'all', False),
    ('2027-02-07', 'Sonam Lhosar', 'all', True),
    ('2027-02-11', 'Basanta Panchami', 'all', True),
    ('2027-02-19', 'Democracy Day', 'all', False),
    ('2027-03-06', 'Maha Shivaratri', 'all', False),
    ('2027-03-08', "International Women's Day", 'female', False),
    ('2027-03-09', 'Gyalpo Lhosar', 'all', True),
    ('2027-03-21', 'Holi (hill districts)', 'all', True),
    ('2027-03-22', 'Holi (Terai districts)', 'all', True),
    ('2027-04-06', 'Ghode Jatra', 'all', True),
]


def _bs_of(ad: str) -> str | None:
    r = one('SELECT bs_date FROM bs_calendar WHERE ad_date = %s', [ad])
    return r['bs_date'] if r else None


# ---------------------------------------------------------------- holidays (plan §5.7)

def holidays(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(HOLIDAY_QUERY, query or {})
    return rows("""SELECT h.id, h.branch_id, h.local_date, h.bs_date, h.name_en, h.name_ne, h.gender_scope, h.optional FROM holiday h
                    WHERE h.company_id = %s AND (%s::date IS NULL OR h.local_date >= %s::date) AND (%s::date IS NULL OR h.local_date <= %s::date)
                      AND (%s::uuid IS NULL OR h.branch_id IS NULL OR h.branch_id = %s::uuid) ORDER BY h.local_date""",
                [user.company_id, q.get('from'), q.get('from'), q.get('to'), q.get('to'), q.get('branchId'), q.get('branchId')])


def get_holiday(user: AuthUser, holiday_id: str) -> dict:
    hid = parse_uuid(holiday_id)
    r = one("""SELECT id, branch_id, local_date, bs_date, name_en, name_ne, gender_scope, optional FROM holiday
                WHERE id = %s AND company_id = %s""", [hid, user.company_id])
    if not r:
        raise NotFoundError('holiday not found')
    return r


def create_holiday(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = parse(HOLIDAY_BODY, body)
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        # The UNIQUE constraint treats NULL branch_id as distinct, so company-wide duplicates must be refused explicitly.
        if one("""SELECT 1 AS x FROM holiday WHERE company_id = %s AND branch_id IS NOT DISTINCT FROM %s::uuid AND local_date = %s
                  AND name_en = %s""", [user.company_id, b.get('branchId'), b['localDate'], b['nameEn']]):
            raise ConflictError('this holiday already exists for that date')
        ins = one("""INSERT INTO holiday (company_id, branch_id, local_date, bs_date, name_en, name_ne, gender_scope, optional)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [user.company_id, b.get('branchId'), b['localDate'], _bs_of(b['localDate']), b['nameEn'], b.get('nameNe'),
                   b['genderScope'], b['optional']])
        ids = _active_employee_ids(_company_of(user))
        enqueue_recompute([(e, b['localDate']) for e in ids], 'holiday_added')
        audit('create', 'holiday', actor_id=user.id, row_id=ins['id'], after=b)
    return {'id': str(ins['id']), 'recomputeQueued': len(ids)}


def update_holiday(user: AuthUser, holiday_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    hid = parse_uuid(holiday_id)
    b = _provided(parse(HOLIDAY_PATCH, body), body)
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        before = one('SELECT local_date FROM holiday WHERE id = %s AND company_id = %s', [hid, user.company_id])
        if not before:
            raise NotFoundError('holiday not found')
        bs = _bs_of(b['localDate']) if b.get('localDate') else None
        execute("""UPDATE holiday SET local_date = coalesce(%s::date, local_date), bs_date = coalesce(%s, bs_date),
                          name_en = coalesce(%s, name_en), name_ne = coalesce(%s, name_ne),
                          branch_id = CASE WHEN %s THEN %s::uuid ELSE branch_id END, gender_scope = coalesce(%s, gender_scope),
                          optional = coalesce(%s, optional) WHERE id = %s""",
                [b.get('localDate'), bs, b.get('nameEn'), b.get('nameNe'), 'branchId' in b, b.get('branchId'),
                 b.get('genderScope'), b.get('optional'), hid])
        ids = _active_employee_ids(_company_of(user))
        dates = list(dict.fromkeys([_iso(before['local_date'])] + ([b['localDate']] if b.get('localDate') else [])))
        enqueue_recompute([(e, d) for e in ids for d in dates], 'holiday_changed')
        audit('update', 'holiday', actor_id=user.id, row_id=hid, before={'local_date': _iso(before['local_date'])}, after=b)
    return {'ok': True}


def delete_holiday(user: AuthUser, holiday_id: str) -> dict:
    _require_module(user, 'attendance')
    hid = parse_uuid(holiday_id)
    with transaction.atomic():
        d = one('DELETE FROM holiday WHERE id = %s AND company_id = %s RETURNING local_date', [hid, user.company_id])
        if not d:
            raise NotFoundError('holiday not found')
        ids = _active_employee_ids(_company_of(user))
        enqueue_recompute([(e, _iso(d['local_date'])) for e in ids], 'holiday_removed')
        audit('delete', 'holiday', actor_id=user.id, row_id=hid, before={'local_date': _iso(d['local_date'])})
    return {'ok': True}


def import_default_holidays(user: AuthUser, body: dict) -> dict:
    """Load the default MoHA-style list for a BS year (only 2083 shipped; plan §16.7 SOP updates it each Chaitra)."""
    _require_module(user, 'attendance')
    b = parse(IMPORT_DEFAULT, body)
    if b['bsYear'] != 2083:
        raise BadRequestError('default list available for BS 2083 only; add others via POST /shifts/holidays')
    inserted_dates: list[str] = []
    with transaction.atomic():
        for ad, en, scope, optional in HOLIDAYS_2083:
            # UNIQUE with a NULL branch_id never conflicts in SQL, so idempotency is an explicit existence check.
            n = execute("""INSERT INTO holiday (company_id, branch_id, local_date, bs_date, name_en, gender_scope, optional)
                           SELECT %s, NULL, %s::date, (SELECT bs_date FROM bs_calendar WHERE ad_date = %s::date), %s, %s, %s
                            WHERE NOT EXISTS (SELECT 1 FROM holiday WHERE company_id = %s AND branch_id IS NULL AND local_date = %s::date
                                              AND name_en = %s)""",
                        [user.company_id, ad, ad, en, scope, optional, user.company_id, ad, en])
            if n > 0:
                inserted_dates.append(ad)
        if inserted_dates:
            ids = _active_employee_ids(_company_of(user))
            enqueue_recompute([(e, ad) for e in ids for ad in inserted_dates], 'holiday_import')
        audit('import', 'holiday', actor_id=user.id, after={'bsYear': b['bsYear'], 'inserted': len(inserted_dates)})
    return {'inserted': len(inserted_dates), 'total': len(HOLIDAYS_2083)}


# ---------------------------------------------------------------- locations (mobile geofence, plan §5.2)

def locations(user: AuthUser) -> list[dict]:
    return rows('SELECT id, branch_id, name, lat, lng, radius_m FROM location WHERE company_id = %s ORDER BY name', [user.company_id])


def create_location(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = parse(LOCATION_BODY, body)
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        ins = one('INSERT INTO location (company_id, branch_id, name, lat, lng, radius_m) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                  [user.company_id, b.get('branchId'), b['name'], b['lat'], b['lng'], b['radiusM']])
        audit('create', 'location', actor_id=user.id, row_id=ins['id'], after=b)
    return {'id': str(ins['id'])}


def update_location(user: AuthUser, location_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    b = _provided(parse(LOCATION_PATCH, body), body)
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        n = execute("""UPDATE location SET name = coalesce(%s, name), lat = coalesce(%s, lat), lng = coalesce(%s, lng),
                              radius_m = coalesce(%s, radius_m), branch_id = CASE WHEN %s THEN %s::uuid ELSE branch_id END
                        WHERE id = %s AND company_id = %s""",
                    [b.get('name'), b.get('lat'), b.get('lng'), b.get('radiusM'), 'branchId' in b, b.get('branchId'), lid, user.company_id])
        if not n:
            raise NotFoundError('location not found')
        audit('update', 'location', actor_id=user.id, row_id=lid, after=b)
    return {'ok': True}


def delete_location(user: AuthUser, location_id: str) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    with transaction.atomic():
        if not execute('DELETE FROM location WHERE id = %s AND company_id = %s', [lid, user.company_id]):
            raise NotFoundError('location not found')
        audit('delete', 'location', actor_id=user.id, row_id=lid)
    return {'ok': True}


# ---------------------------------------------------------------- calendar for the UI (holidays + weekly offs)

def calendar(user: AuthUser, query: dict) -> dict:
    """{'days': [{date, bsDate, weekday, weeklyOff, holidays: [{local_date, name_en, name_ne, gender_scope, optional, branch_id}]}]}"""
    q = parse(CALENDAR_QUERY, query)
    if q['to'] < q['from'] or (date.fromisoformat(q['to']) - date.fromisoformat(q['from'])).days >= 400:
        raise BadRequestError('range must be 1 to 400 days')
    dates = date_range(q['from'], q['to'])
    weekly_off = [6]
    if q.get('employeeId'):
        emp = one('SELECT weekly_off_days FROM employee WHERE id = %s AND company_id = %s', [q['employeeId'], user.company_id])
        if not emp:
            raise NotFoundError('employee not found')
        assert_manages(user, q['employeeId'], 'attendance')  # self, their manager or HR only
        weekly_off = list(emp['weekly_off_days'] or [])
    hol = rows("""SELECT local_date, name_en, name_ne, gender_scope, optional, branch_id FROM holiday
                   WHERE company_id = %s AND local_date BETWEEN %s AND %s ORDER BY local_date""", [user.company_id, q['from'], q['to']])
    bs = {_iso(r['ad_date']): r['bs_date'] for r in rows('SELECT ad_date, bs_date FROM bs_calendar WHERE ad_date BETWEEN %s AND %s',
                                                          [q['from'], q['to']])}
    by_day: dict[str, list[dict]] = {}
    for h in hol:
        h = {**h, 'local_date': _iso(h['local_date'])}
        by_day.setdefault(h['local_date'], []).append(h)
    return {'days': [{'date': d, 'bsDate': bs.get(d), 'weekday': weekday_of(d), 'weeklyOff': weekday_of(d) in weekly_off,
                      'holidays': by_day.get(d, [])} for d in dates]}


# ---------------------------------------------------------------- roster (plan §5.3)

def _roster_roles(user: AuthUser) -> None:
    _require(user, *MODULE_ROLES['attendance'], 'manager')


def roster(user: AuthUser, query: dict) -> dict:
    """Line managers roster their own reports only (and themselves); HR sees the company."""
    _roster_roles(user)
    q = parse(ROSTER_QUERY, query)
    team_only = not has_module(user, 'attendance')
    emps = rows("""SELECT e.id, e.emp_code, e.name_en, e.name_ne, e.weekly_off_days,
                          (SELECT s.shift_id FROM shift_assignment s WHERE s.employee_id = e.id AND s.valid @> %s::date) AS default_shift_id
                     FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                    WHERE e.company_id = %s AND e.status = 'active' AND (%s::uuid IS NULL OR a.branch_id = %s::uuid)
                      AND (NOT %s OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR e.id = %s::uuid) ORDER BY e.emp_code""",
                [q['from'], user.company_id, q.get('branchId'), q.get('branchId'), team_only, user.employee_id, user.employee_id])
    cells = rows("""SELECT r.employee_id, r.local_date, r.shift_id FROM roster r JOIN employee e ON e.id = r.employee_id
                     LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                     WHERE e.company_id = %s AND r.local_date BETWEEN %s AND %s
                       AND (NOT %s OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date) OR e.id = %s::uuid)""",
                 [user.company_id, q['from'], q['to'], team_only, user.employee_id, user.employee_id])
    for e in emps:
        e['id'] = str(e['id'])
        e['default_shift_id'] = str(e['default_shift_id']) if e['default_shift_id'] else None
        e['weekly_off_days'] = list(e['weekly_off_days'] or [])
    for c in cells:
        c['employee_id'] = str(c['employee_id'])
        c['local_date'] = _iso(c['local_date'])
        c['shift_id'] = str(c['shift_id']) if c['shift_id'] else None
    return {'employees': emps, 'roster': cells}


def _locked(employee_id: str, local_date: str) -> bool:
    return bool(one("""SELECT 1 AS x FROM v_attendance_latest WHERE employee_id = %s AND local_date = %s AND locked_at IS NOT NULL""",
                    [employee_id, local_date]))


def save_roster(user: AuthUser, body: dict) -> dict:
    """PUT /shifts/roster: upsert cells (shiftId null = rostered off). All or nothing."""
    _roster_roles(user)
    b = parse(ROSTER_PUT, body)
    with transaction.atomic():
        checked_emps: set[str] = set()
        checked_shifts: set[str] = set()
        for row in b['rows']:
            if row['employeeId'] not in checked_emps:
                if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [row['employeeId'], user.company_id]):
                    raise NotFoundError(f"employee {row['employeeId']} not found")
                assert_manages(user, row['employeeId'])
                checked_emps.add(row['employeeId'])
            if row['shiftId'] and row['shiftId'] not in checked_shifts:
                if not one('SELECT 1 AS x FROM shift WHERE id = %s AND company_id = %s', [row['shiftId'], user.company_id]):
                    raise NotFoundError(f"shift {row['shiftId']} not found")
                checked_shifts.add(row['shiftId'])
            if _locked(row['employeeId'], row['localDate']):
                raise ConflictError(f"attendance for {row['localDate']} is locked by payroll")
            execute("""INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s)
                       ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id""",
                    [row['employeeId'], row['localDate'], row['shiftId']])
        enqueue_recompute([(x['employeeId'], x['localDate']) for x in b['rows']], 'roster_changed')
        audit('update', 'roster', actor_id=user.id, after={'rows': len(b['rows'])})
    return {'upserted': len(b['rows'])}


def clear_roster(user: AuthUser, body: dict) -> dict:
    """DELETE /shifts/roster: the cell goes back to the person's normal shift."""
    _roster_roles(user)
    b = parse(ROSTER_DELETE, body)
    assert_manages(user, b['employeeId'], 'attendance')
    with transaction.atomic():
        n = execute("""DELETE FROM roster r USING employee e WHERE e.id = r.employee_id AND r.employee_id = %s AND r.local_date = %s
                       AND e.company_id = %s""", [b['employeeId'], b['localDate'], user.company_id])
        if n:
            enqueue_recompute([(b['employeeId'], b['localDate'])], 'roster_removed')
    return {'deleted': n or 0}


def copy_week(user: AuthUser, body: dict) -> dict:
    """Copy last week's roster rows onto the week starting weekStart (plan §5.3 "copy last week")."""
    _roster_roles(user)
    b = parse(COPY_WEEK, body)
    team_only = not has_module(user, 'attendance')
    with transaction.atomic():
        src = rows("""SELECT r.employee_id, r.local_date, r.shift_id FROM roster r JOIN employee e ON e.id = r.employee_id
                       LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                       WHERE e.company_id = %s AND r.local_date >= %s::date - 7 AND r.local_date < %s::date
                         AND (NOT %s OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))""",
                   [user.company_id, b['weekStart'], b['weekStart'], team_only, user.employee_id])
        items: list[tuple[str, str]] = []
        skipped = 0
        for row in src:
            target = (date.fromisoformat(_iso(row['local_date'])) + timedelta(days=7)).isoformat()
            if _locked(row['employee_id'], target):
                skipped += 1
                continue
            execute("""INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s)
                       ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id""",
                    [row['employee_id'], target, row['shift_id']])
            items.append((str(row['employee_id']), target))
        enqueue_recompute(items, 'roster_copied')
        audit('copy_week', 'roster', actor_id=user.id, after={'weekStart': b['weekStart'], 'copied': len(items), 'skippedLocked': skipped})
    return {'copied': len(items), 'skippedLocked': skipped}


def publish_roster(user: AuthUser, body: dict) -> dict:
    """Publish: tell each rostered employee their shifts for the range. A line manager publishes only their own team."""
    _roster_roles(user)
    b = parse(PUBLISH, body)
    team_only = not has_module(user, 'attendance')
    with transaction.atomic():
        per = rows("""SELECT r.employee_id, count(*)::text AS n FROM roster r JOIN employee e ON e.id = r.employee_id
                       LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                       WHERE e.company_id = %s AND r.local_date BETWEEN %s AND %s AND (NOT %s OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))
                       GROUP BY r.employee_id""",
                   [user.company_id, b['from'], b['to'], team_only, user.employee_id])
        for row in per:
            emp = str(row['employee_id'])
            notify(company_id=user.company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='roster.published',
                   title='Roster published', body=f"Your shifts for {bs_range(b['from'], b['to'])} have been published ({row['n']} days).",
                   payload={'from': b['from'], 'to': b['to']}, channels=['push'])
        audit('publish', 'roster', actor_id=user.id, after=b)
    return {'notified': len(per)}


# ---------------------------------------------------------------- assignments

def assign_shift(user: AuthUser, body: dict) -> dict:
    """Effective-dated: the person's shift from `from` onwards; later assignments are replaced."""
    _require_module(user, 'attendance')
    b = parse(ASSIGN, body)
    if not one("""SELECT 1 AS x FROM employee e JOIN shift s ON s.company_id = e.company_id WHERE e.id = %s AND s.id = %s
                  AND e.company_id = %s""", [b['employeeId'], b['shiftId'], user.company_id]):
        raise NotFoundError('employee or shift not found')
    with transaction.atomic():
        execute('DELETE FROM shift_assignment WHERE employee_id = %s AND lower(valid) >= %s::date', [b['employeeId'], b['from']])
        execute("""UPDATE shift_assignment SET valid = daterange(lower(valid), %s::date)
                    WHERE employee_id = %s AND valid @> %s::date AND lower(valid) < %s::date""",
                [b['from'], b['employeeId'], b['from'], b['from']])
        execute('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange(%s::date, NULL))',
                [b['employeeId'], b['shiftId'], b['from']])
        audit('assign', 'shift_assignment', actor_id=user.id, row_id=b['employeeId'], after=b)
    today = _today()
    if b['from'] <= today:
        start = today if b['from'] < today else b['from']
        enqueue_recompute([(b['employeeId'], d) for d in date_range(start, today)], 'shift_assigned')
    return {'ok': True}


# ---------------------------------------------------------------- shift CRUD

SHIFT_COLS = """s.id, s.code, s.name_en, s.name_ne, s.start_time::text AS start, s.end_time::text AS "end", s.grace_in_min,
  s.grace_out_min, s.early_in_window_min, s.late_out_window_min, s.break_minutes, s.half_day_threshold_min, s.absent_threshold_min,
  s.pairing_mode, s.rounding_min, s.rounding_direction, (s.end_time <= s.start_time) AS crosses_midnight,
  (SELECT count(*) FROM shift_assignment a WHERE a.shift_id = s.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date)::int AS assigned_employees"""

_COLUMN_OF = [('code', 'code'), ('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('start', 'start_time'), ('end', 'end_time'),
              ('graceInMin', 'grace_in_min'), ('graceOutMin', 'grace_out_min'), ('earlyInWindowMin', 'early_in_window_min'),
              ('lateOutWindowMin', 'late_out_window_min'), ('breakMinutes', 'break_minutes'),
              ('halfDayThresholdMin', 'half_day_threshold_min'), ('absentThresholdMin', 'absent_threshold_min'),
              ('pairingMode', 'pairing_mode'), ('roundingMin', 'rounding_min'), ('roundingDirection', 'rounding_direction')]


def _shift_row(r: dict) -> dict:
    r['id'] = str(r['id'])
    return r


def list_shifts(company_id: str | None) -> list[dict]:
    return [_shift_row(r) for r in rows(f'SELECT {SHIFT_COLS} FROM shift s WHERE s.company_id = %s ORDER BY s.code', [company_id])]


def shifts(user: AuthUser) -> list[dict]:
    """GET /shifts: every signed-in user of the company (home page, correction forms)."""
    return list_shifts(user.company_id)


def get_shift(user: AuthUser, shift_id: str) -> dict:
    sid = parse_uuid(shift_id)
    r = one(f'SELECT {SHIFT_COLS} FROM shift s WHERE s.id = %s AND s.company_id = %s', [sid, user.company_id])
    if not r:
        raise NotFoundError('shift not found')
    return _shift_row(r)


def create_shift(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = parse(SHIFT_BODY, body)
    with transaction.atomic():
        ins = one("""INSERT INTO shift (company_id, code, name_en, name_ne, start_time, end_time, grace_in_min, grace_out_min,
                       early_in_window_min, late_out_window_min, break_minutes, half_day_threshold_min, absent_threshold_min,
                       pairing_mode, rounding_min, rounding_direction)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [user.company_id, b['code'], b['nameEn'], b.get('nameNe'), b['start'], b['end'], b['graceInMin'], b['graceOutMin'],
                   b['earlyInWindowMin'], b['lateOutWindowMin'], b['breakMinutes'], b['halfDayThresholdMin'], b['absentThresholdMin'],
                   b['pairingMode'], b['roundingMin'], b['roundingDirection']])
        audit('create', 'shift', actor_id=user.id, row_id=ins['id'], after=b)
    return {'id': str(ins['id'])}


def update_shift(user: AuthUser, shift_id: str, body: dict) -> dict:
    """Rule changes apply going forward: unlocked days of this month are recomputed for the people on the shift."""
    _require_module(user, 'attendance')
    sid = parse_uuid(shift_id)
    b = _provided(parse(SHIFT_PATCH, body), body)
    with transaction.atomic():
        before = one(f'SELECT {SHIFT_COLS} FROM shift s WHERE s.id = %s AND s.company_id = %s', [sid, user.company_id])
        if not before:
            raise NotFoundError('shift not found')
        sets, vals = [], []
        for k, col in _COLUMN_OF:
            if b.get(k) is not None:
                sets.append(f'{col} = %s')
                vals.append(b[k])
        if sets:
            execute(f"UPDATE shift SET {', '.join(sets)} WHERE id = %s", [*vals, sid])
        emps = rows('SELECT employee_id FROM shift_assignment WHERE shift_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date', [sid])
        today = _today()
        month_start = f'{today[:8]}01'
        enqueue_recompute([(e['employee_id'], d) for e in emps for d in date_range(month_start, today)], 'shift_rules_changed')
        audit('update', 'shift', actor_id=user.id, row_id=sid, before=_shift_row(before), after=b)
    return {'ok': True, 'recomputeQueued': len(emps)}


def delete_shift(user: AuthUser, shift_id: str) -> dict:
    """Refuse to delete a shift that employees still use; the UI reassigns first (plan §13.8 destructive-action rule)."""
    _require_module(user, 'attendance')
    sid = parse_uuid(shift_id)
    with transaction.atomic():
        if not one('SELECT 1 AS x FROM shift WHERE id = %s AND company_id = %s', [sid, user.company_id]):
            raise NotFoundError('shift not found')
        u = one("""SELECT (SELECT count(*) FROM shift_assignment WHERE shift_id = %s AND upper_inf(valid))::text AS assignments,
                          (SELECT count(*) FROM roster WHERE shift_id = %s AND local_date >= (now() AT TIME ZONE 'Asia/Kathmandu')::date)::text AS roster""", [sid, sid])
        if int(u['assignments']) > 0 or int(u['roster']) > 0:
            raise ConflictError(f"shift is assigned to {u['assignments']} employee(s) and {u['roster']} future roster day(s); reassign first")
        n = execute("""DELETE FROM shift WHERE id = %s AND company_id = %s AND NOT EXISTS (SELECT 1 FROM shift_assignment WHERE shift_id = %s)
                       AND NOT EXISTS (SELECT 1 FROM roster WHERE shift_id = %s) AND NOT EXISTS (SELECT 1 FROM attendance_day WHERE shift_id = %s)""",
                    [sid, user.company_id, sid, sid, sid])
        if not n:
            if not one('SELECT 1 AS x FROM shift WHERE id = %s AND company_id = %s', [sid, user.company_id]):
                raise NotFoundError('shift not found')
            raise ConflictError('shift has historical assignments or attendance; it cannot be deleted')
        audit('delete', 'shift', actor_id=user.id, row_id=sid)
    return {'ok': True}


# ---------------------------------------------------------------- shift swaps shown on the roster page (read only)

SWAP_SELECT = """SELECT s.id, s.local_date, s.status, s.reason, s.colleague_note, s.created_at, s.decided_at,
           s.requester_id, r.name_en AS requester_name, r.emp_code AS requester_code, s.colleague_id, c.name_en AS colleague_name,
           c.emp_code AS colleague_code, rs.code AS requester_shift_code, rs.name_en AS requester_shift_name,
           cs.code AS colleague_shift_code, cs.name_en AS colleague_shift_name
      FROM shift_swap s JOIN employee r ON r.id = s.requester_id JOIN employee c ON c.id = s.colleague_id
      LEFT JOIN shift rs ON rs.id = s.requester_shift_id LEFT JOIN shift cs ON cs.id = s.colleague_shift_id"""


def team_swaps(user: AuthUser, from_: str | None, to: str | None) -> list[dict]:
    """The field module's GET /field/swaps?scope=team, for the roster page: HR company-wide, managers their people."""
    if not has_role(user, *MODULE_ROLES['attendance'], 'manager'):
        raise ForbiddenError('only managers and HR see team swaps')
    return rows(f"""{SWAP_SELECT} WHERE s.company_id = %s AND (%s::date IS NULL OR s.local_date >= %s::date)
                     AND (%s::date IS NULL OR s.local_date <= %s::date)
                     AND (%s OR (hj_manages(%s::uuid, s.requester_id, s.local_date) OR hj_manages(%s::uuid, s.colleague_id, s.local_date)))
                   ORDER BY s.local_date DESC, s.created_at DESC LIMIT 500""",
                [_company_of(user), from_, from_, to, to, has_module(user, 'attendance'), user.employee_id, user.employee_id])
