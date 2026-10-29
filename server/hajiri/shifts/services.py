"""
Shifts, holidays, geofence locations, the UI calendar, rosters and shift assignments (port of
apps/api/src/modules/shifts/routes.ts). Every function takes the signed-in user and a body or query in the TS (camelCase)
shape, checks roles, company and manager scope as the TS route did, and returns what the route answered.

Every query is scoped to the ACTIVE company (`Model.objects.for_company(user.company_id)`): ids of another company
are simply not found. Bodies are validated with the serializers in serializers.py (the zod schemas).

Pages, HTMX views, the JSON API (api.py) and other slices call these:

- list_shifts(company_id) / shifts(user)       every signed-in user (GET /shifts)
- holidays(user, query)                        GET /shifts/holidays
- calendar(user, {from, to, employeeId?})      GET /shifts/calendar: days with BS date, weekly off and holidays
- roster(user, {from, to, branchId?})          GET /shifts/roster
"""
from __future__ import annotations

from datetime import date, timedelta

from django.contrib.postgres.fields import DateRangeField
from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import BooleanField, Count, Exists, ExpressionWrapper, F, Func, OuterRef, Q, TextField, Value
from django.db.models.functions import Cast

from engines.bs_calendar import weekday_of
from hajiri.attendance.service import date_range, enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, has_module, has_role, manages
from hajiri.core.bsdate import bs_range
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    AttendanceDay, Branch, BsCalendar, Company, Employee, EmployeeAssignment, Holiday, Location, Roster, Shift,
    ShiftAssignment, ShiftSwap,
)
from hajiri.notifications.service import notify, user_id_for_employee
# The TS shifts routes used the workflow helper (assertManages): 'not allowed to act for this employee'.
from hajiri.workflow.service import assert_manages

from .filters import HolidayFilter
from .serializers import (
    AssignIn, CalendarQueryIn, CopyWeekIn, HolidayIn, HolidayQueryIn, ImportDefaultIn, LocationIn, PublishIn,
    RosterDeleteIn, RosterPutIn, RosterQueryIn, ShiftIn,
)

ACTIVE = Employee.Status.ACTIVE

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


def _day(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _today() -> str:
    return npt_today().isoformat()


def _assert_branch(company_id: str | None, branch_id: str | None) -> None:
    """A branch id from the body must belong to the caller's company."""
    if branch_id and not Branch.objects.for_company(company_id).filter(pk=branch_id).exists():
        raise NotFoundError('branch not found')


def _active_employee_ids(company_id: str) -> list[str]:
    return [str(i) for i in Employee.objects.for_company(company_id).filter(status=ACTIVE).values_list('id', flat=True)]


def _lock_company(company_id: str) -> None:
    """Serialise duplicate checks of one company's holidays (a NULL branch_id never conflicts in the UNIQUE
    constraint) without blocking inserts that reference the company row."""
    Company.objects.select_for_update(no_key=True).filter(pk=company_id).first()


def _changes(b: dict, columns: list[tuple[str, str]], nullable: tuple[str, ...] = ()) -> dict:
    """PATCH: the columns of the keys that were sent with a value (null keeps the column, as coalesce() did), plus
    the `nullable` keys, which are written whenever they are present (null clears them)."""
    out = {col: b[k] for k, col in columns if b.get(k) is not None}
    out.update({col: b[k] for k, col in columns if k in nullable and k in b})
    return out


# ---------------------------------------------------------------- the default holiday list

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


def _bs_of(ad) -> str | None:
    return BsCalendar.objects.filter(ad_date=ad).values_list('bs_date', flat=True).first()


# ---------------------------------------------------------------- holidays (plan §5.7)

HOLIDAY_VALUES = ('id', 'branch_id', 'local_date', 'bs_date', 'name_en', 'name_ne', 'gender_scope', 'optional')
_HOLIDAY_COLUMNS = [('localDate', 'local_date'), ('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('branchId', 'branch_id'),
                    ('genderScope', 'gender_scope'), ('optional', 'optional')]


def holidays(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(HolidayQueryIn, query or {}))
    f = HolidayFilter({'date_from': q.get('from'), 'date_to': q.get('to'), 'branch': q.get('branchId')},
                      queryset=Holiday.objects.for_company(user.company_id))
    return list(f.qs.order_by('local_date', 'id').values(*HOLIDAY_VALUES))


def get_holiday(user: AuthUser, holiday_id: str) -> dict:
    hid = parse_uuid(holiday_id)
    r = Holiday.objects.for_company(user.company_id).filter(pk=hid).values(*HOLIDAY_VALUES).first()
    if not r:
        raise NotFoundError('holiday not found')
    return r


def create_holiday(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = plain(validate(HolidayIn, body))
    cid = user.company_id
    with transaction.atomic():
        _assert_branch(cid, b.get('branchId'))
        _lock_company(cid)
        # The UNIQUE constraint treats NULL branch_id as distinct, so company-wide duplicates must be refused explicitly.
        if Holiday.objects.for_company(cid).filter(branch_id=b.get('branchId'), local_date=b['localDate'], name_en=b['nameEn']).exists():
            raise ConflictError('this holiday already exists for that date')
        h = Holiday.objects.create(company_id=cid, branch_id=b.get('branchId'), local_date=b['localDate'], bs_date=_bs_of(b['localDate']),
                                   name_en=b['nameEn'], name_ne=b.get('nameNe'), gender_scope=b['genderScope'], optional=b['optional'])
        ids = _active_employee_ids(_company_of(user))
        enqueue_recompute([(e, b['localDate']) for e in ids], 'holiday_added')
        audit('create', 'holiday', actor_id=user.id, row_id=h.id, after=b)
    return {'id': str(h.id), 'recomputeQueued': len(ids)}


def update_holiday(user: AuthUser, holiday_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    hid = parse_uuid(holiday_id)
    b = plain(validate(HolidayIn, body, partial=True))
    cid = user.company_id
    with transaction.atomic():
        _assert_branch(cid, b.get('branchId'))
        before = Holiday.objects.for_company(cid).select_for_update().filter(pk=hid).values('local_date').first()
        if not before:
            raise NotFoundError('holiday not found')
        changes = _changes(b, _HOLIDAY_COLUMNS, nullable=('branchId',))
        bs = _bs_of(b['localDate']) if b.get('localDate') else None
        if bs is not None:
            changes['bs_date'] = bs
        if changes:
            Holiday.objects.filter(pk=hid).update(**changes)
        ids = _active_employee_ids(_company_of(user))
        dates = list(dict.fromkeys([_iso(before['local_date'])] + ([b['localDate']] if b.get('localDate') else [])))
        enqueue_recompute([(e, d) for e in ids for d in dates], 'holiday_changed')
        audit('update', 'holiday', actor_id=user.id, row_id=hid, before={'local_date': _iso(before['local_date'])}, after=b)
    return {'ok': True}


def delete_holiday(user: AuthUser, holiday_id: str) -> dict:
    _require_module(user, 'attendance')
    hid = parse_uuid(holiday_id)
    with transaction.atomic():
        found = Holiday.objects.for_company(user.company_id).select_for_update().filter(pk=hid).values('local_date').first()
        if not found:
            raise NotFoundError('holiday not found')
        Holiday.objects.filter(pk=hid).delete()
        day = _iso(found['local_date'])
        ids = _active_employee_ids(_company_of(user))
        enqueue_recompute([(e, day) for e in ids], 'holiday_removed')
        audit('delete', 'holiday', actor_id=user.id, row_id=hid, before={'local_date': day})
    return {'ok': True}


def import_default_holidays(user: AuthUser, body: dict) -> dict:
    """Load the default MoHA-style list for a BS year (only 2083 shipped; plan §16.7 SOP updates it each Chaitra)."""
    _require_module(user, 'attendance')
    b = validate(ImportDefaultIn, body)
    if b['bsYear'] != 2083:
        raise BadRequestError('default list available for BS 2083 only; add others via POST /shifts/holidays')
    cid = user.company_id
    days = [date.fromisoformat(ad) for ad, *_ in HOLIDAYS_2083]
    with transaction.atomic():
        _lock_company(cid)
        # UNIQUE with a NULL branch_id never conflicts in SQL, so idempotency is an explicit existence check.
        existing = set(Holiday.objects.for_company(cid).filter(branch__isnull=True, local_date__in=days)
                       .values_list('local_date', 'name_en'))
        bs = dict(BsCalendar.objects.filter(ad_date__in=days).values_list('ad_date', 'bs_date'))
        new = [Holiday(company_id=cid, branch=None, local_date=d, bs_date=bs.get(d), name_en=en, gender_scope=scope, optional=optional)
               for d, (_ad, en, scope, optional) in zip(days, HOLIDAYS_2083) if (d, en) not in existing]
        Holiday.objects.bulk_create(new)
        inserted_dates = [h.local_date.isoformat() for h in new]
        if inserted_dates:
            ids = _active_employee_ids(_company_of(user))
            enqueue_recompute([(e, ad) for e in ids for ad in inserted_dates], 'holiday_import')
        audit('import', 'holiday', actor_id=user.id, after={'bsYear': b['bsYear'], 'inserted': len(inserted_dates)})
    return {'inserted': len(inserted_dates), 'total': len(HOLIDAYS_2083)}


# ---------------------------------------------------------------- locations (mobile geofence, plan §5.2)

_LOCATION_COLUMNS = [('name', 'name'), ('lat', 'lat'), ('lng', 'lng'), ('radiusM', 'radius_m'), ('branchId', 'branch_id')]


def locations(user: AuthUser) -> list[dict]:
    return list(Location.objects.for_company(user.company_id).order_by('name', 'id')
                .values('id', 'branch_id', 'name', 'lat', 'lng', 'radius_m'))


def create_location(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = plain(validate(LocationIn, body))
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        loc = Location.objects.create(company_id=user.company_id, branch_id=b.get('branchId'), name=b['name'], lat=b['lat'],
                                      lng=b['lng'], radius_m=b['radiusM'])
        audit('create', 'location', actor_id=user.id, row_id=loc.id, after=b)
    return {'id': str(loc.id)}


def update_location(user: AuthUser, location_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    b = plain(validate(LocationIn, body, partial=True))
    with transaction.atomic():
        _assert_branch(user.company_id, b.get('branchId'))
        found = Location.objects.for_company(user.company_id).filter(pk=lid)
        changes = _changes(b, _LOCATION_COLUMNS, nullable=('branchId',))
        if not (found.update(**changes) if changes else found.exists()):
            raise NotFoundError('location not found')
        audit('update', 'location', actor_id=user.id, row_id=lid, after=b)
    return {'ok': True}


def delete_location(user: AuthUser, location_id: str) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    with transaction.atomic():
        deleted, _ = Location.objects.for_company(user.company_id).filter(pk=lid).delete()
        if not deleted:
            raise NotFoundError('location not found')
        audit('delete', 'location', actor_id=user.id, row_id=lid)
    return {'ok': True}


# ---------------------------------------------------------------- calendar for the UI (holidays + weekly offs)

def calendar(user: AuthUser, query: dict) -> dict:
    """{'days': [{date, bsDate, weekday, weeklyOff, holidays: [{local_date, name_en, name_ne, gender_scope, optional, branch_id}]}]}"""
    q = plain(validate(CalendarQueryIn, query))
    if q['to'] < q['from'] or (date.fromisoformat(q['to']) - date.fromisoformat(q['from'])).days >= 400:
        raise BadRequestError('range must be 1 to 400 days')
    dates = date_range(q['from'], q['to'])
    weekly_off = [6]
    emp = None
    if q.get('employeeId'):
        emp = Employee.objects.for_company(user.company_id).filter(pk=q['employeeId']).values('weekly_off_days', 'gender').first()
        if not emp:
            raise NotFoundError('employee not found')
        assert_manages(user, q['employeeId'], 'attendance')  # self, their manager or HR only
        weekly_off = list(emp['weekly_off_days'] or [])
    hol = (Holiday.objects.for_company(user.company_id).filter(local_date__range=(q['from'], q['to'])).order_by('local_date', 'id')
           .values('local_date', 'name_en', 'name_ne', 'gender_scope', 'optional', 'branch_id'))
    if emp is not None:
        # One person's calendar shows the holidays that apply to them, by the attendance engine's rule (is_holiday):
        # company-wide or their branch on that day, for everyone or their gender. Optional ones stay, flagged.
        from hajiri.attendance.repo import get_branch_id
        branch_on = {}
        hol = [h for h in hol
               if h['gender_scope'] in (Holiday.GenderScope.ALL, emp['gender'])
               and (h['branch_id'] is None
                    or str(h['branch_id']) == str(branch_on.setdefault(h['local_date'], get_branch_id(q['employeeId'], _iso(h['local_date'])))))]
    bs = {_iso(ad): b for ad, b in BsCalendar.objects.filter(ad_date__range=(q['from'], q['to'])).values_list('ad_date', 'bs_date')}
    by_day: dict[str, list[dict]] = {}
    for h in hol:
        h = {**h, 'local_date': _iso(h['local_date'])}
        by_day.setdefault(h['local_date'], []).append(h)
    return {'days': [{'date': d, 'bsDate': bs.get(d), 'weekday': weekday_of(d), 'weeklyOff': weekday_of(d) in weekly_off,
                      'holidays': by_day.get(d, [])} for d in dates]}


# ---------------------------------------------------------------- roster (plan §5.3)

def _roster_roles(user: AuthUser) -> None:
    _require(user, *MODULE_ROLES['attendance'], 'manager')


def _manageable(user: AuthUser, employee_ids: set[str]) -> set[str]:
    """Which of these employees (already known to be in the active company) the user may roster: assert_manages
    without an area, for many people in one query. HR and admin every one, anyone themselves, a manager the people
    they manage today."""
    if has_role(user, 'hr'):
        return set(employee_ids)
    mine = {user.employee_id} & employee_ids if user.employee_id else set()
    if has_role(user, 'manager') and user.employee_id and employee_ids - mine:
        mine |= {str(i) for i in Employee.objects.filter(pk__in=employee_ids - mine)
                 .filter(manages(user.employee_id)).values_list('id', flat=True)}
    return mine


def _team_rosters(user: AuthUser, *, include_self: bool = False):
    """Roster cells of the active company; for a line manager (no attendance module) only the people they manage
    today (and, with include_self, their own row)."""
    qs = Roster.objects.for_company(user.company_id)
    if has_module(user, 'attendance'):
        return qs
    mine = manages(user.employee_id, F('employee_id'))
    return qs.filter(mine | Q(employee_id=user.employee_id) if include_self else mine)


def _locked_days(company_id: str, pairs) -> set[tuple[str, str]]:
    """The (employee_id, 'YYYY-MM-DD') pairs of the company whose newest attendance version is locked by payroll
    (v_attendance_latest with locked_at set), in one query."""
    pairs = {(str(e), _iso(d)) for e, d in pairs}
    if not pairs:
        return set()
    newer = AttendanceDay.objects.filter(employee_id=OuterRef('employee_id'), local_date=OuterRef('local_date'), version__gt=OuterRef('version'))
    locked = (AttendanceDay.objects.for_company(company_id).filter(employee_id__in={e for e, _ in pairs}, local_date__in={d for _, d in pairs}, locked_at__isnull=False)
              .exclude(Exists(newer)).values_list('employee_id', 'local_date'))
    return {(str(e), _iso(d)) for e, d in locked} & pairs


def _upsert_cells(cells: dict[tuple[str, str], str | None]) -> None:
    """INSERT ... ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id."""
    Roster.objects.bulk_create([Roster(employee_id=e, local_date=d, shift_id=s) for (e, d), s in cells.items()],
                               update_conflicts=True, unique_fields=['employee', 'local_date'], update_fields=['shift'])


def roster(user: AuthUser, query: dict) -> dict:
    """Line managers roster their own reports only (and themselves); HR sees the company."""
    _roster_roles(user)
    q = plain(validate(RosterQueryIn, query))
    today = npt_today()
    emps = Employee.objects.for_company(user.company_id).filter(status=ACTIVE)
    if q.get('branchId'):
        emps = emps.filter(Exists(EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=today, branch_id=q['branchId'])))
    if not has_module(user, 'attendance'):
        emps = emps.filter(manages(user.employee_id) | Q(pk=user.employee_id))
    default_shift = ShiftAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=_day(q['from'])).values('shift_id')[:1]
    employees = list(emps.annotate(default_shift_id=default_shift).order_by('emp_code', 'id')
                     .values('id', 'emp_code', 'name_en', 'name_ne', 'weekly_off_days', 'default_shift_id'))
    cells = list(_team_rosters(user, include_self=True).filter(local_date__range=(q['from'], q['to']))
                 .values('employee_id', 'local_date', 'shift_id'))
    for e in employees:
        e['id'] = str(e['id'])
        e['default_shift_id'] = str(e['default_shift_id']) if e['default_shift_id'] else None
        e['weekly_off_days'] = list(e['weekly_off_days'] or [])
    for c in cells:
        c['employee_id'] = str(c['employee_id'])
        c['local_date'] = _iso(c['local_date'])
        c['shift_id'] = str(c['shift_id']) if c['shift_id'] else None
    return {'employees': employees, 'roster': cells}


def save_roster(user: AuthUser, body: dict) -> dict:
    """PUT /shifts/roster: upsert cells (shiftId null = rostered off). All or nothing."""
    _roster_roles(user)
    b = plain(validate(RosterPutIn, body))
    cid = user.company_id
    rows = b['rows']
    with transaction.atomic():
        # Three queries for the whole body (not three per employee); the checks still run row by row so the first
        # problem in body order is the one reported, as before.
        emp_ids = {r['employeeId'] for r in rows}
        known_emps = {str(i) for i in Employee.objects.for_company(cid).filter(pk__in=emp_ids).values_list('id', flat=True)}
        allowed = _manageable(user, known_emps)
        known_shifts = {str(i) for i in Shift.objects.for_company(cid).filter(pk__in={r['shiftId'] for r in rows if r['shiftId']})
                        .values_list('id', flat=True)}
        locked = _locked_days(cid, ((r['employeeId'], r['localDate']) for r in rows))
        for row in rows:
            if row['employeeId'] not in known_emps:
                raise NotFoundError(f"employee {row['employeeId']} not found")
            if row['employeeId'] not in allowed:
                raise ForbiddenError('not allowed to act for this employee')
            if row['shiftId'] and row['shiftId'] not in known_shifts:
                raise NotFoundError(f"shift {row['shiftId']} not found")
            if (row['employeeId'], row['localDate']) in locked:
                raise ConflictError(f"attendance for {row['localDate']} is locked by payroll")
        _upsert_cells({(r['employeeId'], r['localDate']): r['shiftId'] for r in rows})   # a repeated cell: the last wins
        enqueue_recompute([(x['employeeId'], x['localDate']) for x in rows], 'roster_changed')
        audit('update', 'roster', actor_id=user.id, after={'rows': len(rows)})
    return {'upserted': len(rows)}


def clear_roster(user: AuthUser, body: dict) -> dict:
    """DELETE /shifts/roster: the cell goes back to the person's normal shift."""
    _roster_roles(user)
    b = plain(validate(RosterDeleteIn, body))
    assert_manages(user, b['employeeId'], 'attendance')
    with transaction.atomic():
        n, _ = Roster.objects.for_company(user.company_id).filter(employee_id=b['employeeId'], local_date=b['localDate']).delete()
        if n:
            enqueue_recompute([(b['employeeId'], b['localDate'])], 'roster_removed')
    return {'deleted': n or 0}


def copy_week(user: AuthUser, body: dict) -> dict:
    """Copy last week's roster rows onto the week starting weekStart (plan §5.3 "copy last week")."""
    _roster_roles(user)
    b = plain(validate(CopyWeekIn, body))
    start = date.fromisoformat(b['weekStart'])
    with transaction.atomic():
        src = list(_team_rosters(user).filter(local_date__gte=start - timedelta(days=7), local_date__lt=start)
                   .values_list('employee_id', 'local_date', 'shift_id'))
        targets = [(str(e), (d + timedelta(days=7)).isoformat(), s) for e, d, s in src]
        locked = _locked_days(user.company_id, ((e, d) for e, d, _ in targets))
        copy = {(e, d): s for e, d, s in targets if (e, d) not in locked}
        skipped = len(targets) - len(copy)
        if copy:
            _upsert_cells(copy)
        items = list(copy)
        enqueue_recompute(items, 'roster_copied')
        audit('copy_week', 'roster', actor_id=user.id, after={'weekStart': b['weekStart'], 'copied': len(items), 'skippedLocked': skipped})
    return {'copied': len(items), 'skippedLocked': skipped}


def publish_roster(user: AuthUser, body: dict) -> dict:
    """Publish: tell each rostered employee their shifts for the range. A line manager publishes only their own team."""
    _roster_roles(user)
    b = plain(validate(PublishIn, body))
    with transaction.atomic():
        per = list(_team_rosters(user).filter(local_date__range=(b['from'], b['to']))
                   .values('employee_id').annotate(n=Count('local_date')).order_by('employee_id'))
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
    b = plain(validate(AssignIn, body))
    cid = user.company_id
    if not (Employee.objects.for_company(cid).filter(pk=b['employeeId']).exists()
            and Shift.objects.for_company(cid).filter(pk=b['shiftId']).exists()):
        raise NotFoundError('employee or shift not found')
    frm = date.fromisoformat(b['from'])
    with transaction.atomic():
        mine = ShiftAssignment.objects.for_company(cid).filter(employee_id=b['employeeId'])
        mine.filter(valid__startswith__gte=frm).delete()
        # The assignment running on `from` now ends the day before: daterange(lower(valid), from).
        mine.filter(valid__contains=frm, valid__startswith__lt=frm).update(
            valid=Func(Func(F('valid'), function='lower'), Value(frm), function='daterange', output_field=DateRangeField()))
        ShiftAssignment.objects.create(employee_id=b['employeeId'], shift_id=b['shiftId'], valid=DateRange(frm, None))
        audit('assign', 'shift_assignment', actor_id=user.id, row_id=b['employeeId'], after=b)
    today = _today()
    if b['from'] <= today:
        start = today if b['from'] < today else b['from']
        enqueue_recompute([(b['employeeId'], d) for d in date_range(start, today)], 'shift_assigned')
    return {'ok': True}


# ---------------------------------------------------------------- shift CRUD

SHIFT_VALUES = ('id', 'code', 'name_en', 'name_ne', 'start', 'end', 'grace_in_min', 'grace_out_min', 'early_in_window_min',
                'late_out_window_min', 'break_minutes', 'half_day_threshold_min', 'absent_threshold_min', 'pairing_mode',
                'rounding_min', 'rounding_direction', 'crosses_midnight', 'assigned_employees')

_COLUMN_OF = [('code', 'code'), ('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('start', 'start_time'), ('end', 'end_time'),
              ('graceInMin', 'grace_in_min'), ('graceOutMin', 'grace_out_min'), ('earlyInWindowMin', 'early_in_window_min'),
              ('lateOutWindowMin', 'late_out_window_min'), ('breakMinutes', 'break_minutes'),
              ('halfDayThresholdMin', 'half_day_threshold_min'), ('absentThresholdMin', 'absent_threshold_min'),
              ('pairingMode', 'pairing_mode'), ('roundingMin', 'rounding_min'), ('roundingDirection', 'rounding_direction')]


def _shift_rows(company_id: str | None):
    """The company's shifts in the TS row shape: start/end as 'HH:MM:SS' text, whether the shift runs past midnight
    and how many people are on it today."""
    return (Shift.objects.for_company(company_id)
            .annotate(start=Cast('start_time', TextField()), end=Cast('end_time', TextField()),
                      crosses_midnight=ExpressionWrapper(Q(end_time__lte=F('start_time')), output_field=BooleanField()),
                      assigned_employees=Count('shift_assignments', filter=Q(shift_assignments__valid__contains=npt_today())))
            .values(*SHIFT_VALUES))


def _shift_row(r: dict) -> dict:
    r['id'] = str(r['id'])
    return r


def list_shifts(company_id: str | None) -> list[dict]:
    return [_shift_row(r) for r in _shift_rows(company_id).order_by('code')]


def shifts(user: AuthUser) -> list[dict]:
    """GET /shifts: every signed-in user of the company (home page, correction forms)."""
    return list_shifts(user.company_id)


def get_shift(user: AuthUser, shift_id: str) -> dict:
    sid = parse_uuid(shift_id)
    r = _shift_rows(user.company_id).filter(pk=sid).first()
    if not r:
        raise NotFoundError('shift not found')
    return _shift_row(r)


def create_shift(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = plain(validate(ShiftIn, body))
    with transaction.atomic():
        s = Shift.objects.create(company_id=user.company_id, name_ne=b.get('nameNe'),
                                 **{col: b[k] for k, col in _COLUMN_OF if k != 'nameNe'})
        audit('create', 'shift', actor_id=user.id, row_id=s.id, after=b)
    return {'id': str(s.id)}


def update_shift(user: AuthUser, shift_id: str, body: dict) -> dict:
    """Rule changes apply going forward: unlocked days of this month are recomputed for the people on the shift."""
    _require_module(user, 'attendance')
    sid = parse_uuid(shift_id)
    b = plain(validate(ShiftIn, body, partial=True))
    with transaction.atomic():
        before = _shift_rows(user.company_id).filter(pk=sid).first()
        if not before:
            raise NotFoundError('shift not found')
        changes = _changes(b, _COLUMN_OF)
        if changes:
            Shift.objects.filter(pk=sid).update(**changes)
        emps = list(ShiftAssignment.objects.for_company(user.company_id).filter(shift_id=sid, valid__contains=npt_today()).values_list('employee_id', flat=True))
        today = _today()
        month_start = f'{today[:8]}01'
        enqueue_recompute([(e, d) for e in emps for d in date_range(month_start, today)], 'shift_rules_changed')
        audit('update', 'shift', actor_id=user.id, row_id=sid, before=_shift_row(before), after=b)
    return {'ok': True, 'recomputeQueued': len(emps)}


def delete_shift(user: AuthUser, shift_id: str) -> dict:
    """Refuse to delete a shift that employees still use; the UI reassigns first (plan §13.8 destructive-action rule)."""
    _require_module(user, 'attendance')
    sid = parse_uuid(shift_id)
    mine = Shift.objects.for_company(user.company_id).filter(pk=sid)
    with transaction.atomic():
        if not mine.exists():
            raise NotFoundError('shift not found')
        assignments = ShiftAssignment.objects.for_company(user.company_id).filter(shift_id=sid, valid__upper_inf=True).count()
        future = Roster.objects.for_company(user.company_id).filter(shift_id=sid, local_date__gte=npt_today()).count()
        if assignments > 0 or future > 0:
            raise ConflictError(f'shift is assigned to {assignments} employee(s) and {future} future roster day(s); reassign first')
        used = (Exists(ShiftAssignment.objects.filter(shift=OuterRef('pk'))) | Exists(Roster.objects.filter(shift=OuterRef('pk')))
                | Exists(AttendanceDay.objects.filter(shift=OuterRef('pk'))))
        deleted, _ = mine.exclude(used).delete()
        if not deleted:
            if not mine.exists():
                raise NotFoundError('shift not found')
            raise ConflictError('shift has historical assignments or attendance; it cannot be deleted')
        audit('delete', 'shift', actor_id=user.id, row_id=sid)
    return {'ok': True}


# ---------------------------------------------------------------- shift swaps shown on the roster page (read only)

SWAP_VALUES = dict(
    requester_name=F('requester__name_en'), requester_code=F('requester__emp_code'), colleague_name=F('colleague__name_en'),
    colleague_code=F('colleague__emp_code'), requester_shift_code=F('requester_shift__code'),
    requester_shift_name=F('requester_shift__name_en'), colleague_shift_code=F('colleague_shift__code'),
    colleague_shift_name=F('colleague_shift__name_en'))


def team_swaps(user: AuthUser, from_: str | None, to: str | None) -> list[dict]:
    """The field module's GET /field/swaps?scope=team, for the roster page: HR company-wide, managers their people."""
    if not has_role(user, *MODULE_ROLES['attendance'], 'manager'):
        raise ForbiddenError('only managers and HR see team swaps')
    qs = ShiftSwap.objects.for_company(_company_of(user))
    if from_:
        qs = qs.filter(local_date__gte=from_)
    if to:
        qs = qs.filter(local_date__lte=to)
    if not has_module(user, 'attendance'):
        qs = qs.filter(manages(user.employee_id, F('requester_id'), F('local_date'))
                       | manages(user.employee_id, F('colleague_id'), F('local_date')))
    return list(qs.order_by('-local_date', '-created_at').values(
        'id', 'local_date', 'status', 'reason', 'colleague_note', 'created_at', 'decided_at', 'requester_id', 'colleague_id',
        **SWAP_VALUES)[:500])
