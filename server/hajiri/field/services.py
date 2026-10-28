"""
Field force, breaks, locations, kiosks (HR side), kiosk PIN and QR badge, my roster and shift swaps (port of
apps/api/src/modules/field: routes.ts, policy.ts, punches.ts, swaps.ts, jobs.ts).

Attendance maths is unchanged: breaks are recorded and reported, visits can open the day with a field punch, and approved
swaps become roster rows. Every query is scoped to the caller's ACTIVE company: ids of another company are not found.

Public interface:
- field_policy(company_id) -> {'geofenceMode', 'breaks': {lunch|tea|prayer|personal: {enabled, paid, maxMin}},
  'fieldPhotoRequired', 'fieldVisitClockIn'}; BREAK_KINDS, BREAK_LABEL, FIELD_POLICY_KEY.
- next_direction(employee_id, local_date=None) -> 'in'|'out'; ensure_kiosk_device(company_id);
  insert_app_punch(...) -> {'duplicate', 'punchId', 'punchedAt', 'localDate', 'photoId'}; close_open_break(employee_id, at).
- effective_shift(employee_id, local_date) -> {'shiftId', 'source'}; apply_swap_decision (workflow handler 'shift_swap').
- close_stale_breaks(now=None) -> int (daily job 'staleBreaksClosed').
- One function per TS route taking (user, body_or_query) in the TS camelCase shape: get_policy, put_policy, me,
  start_break, end_break, list_breaks, start_visit, end_visit, list_visits, visit_photo, locations, create_location,
  update_location, delete_location, staff, update_staff, kiosks, kiosk_detail, create_kiosk, update_kiosk,
  kiosk_pairing_code, revoke_kiosk, unlock_kiosk, set_kiosk_pin, reset_kiosk_pin, kiosk_qr, my_roster, swap_colleagues,
  swaps, request_swap, accept_swap, decline_swap, cancel_swap.
  list_breaks / list_visits with format='csv' return {'filename', 'columns', 'rows'} (hajiri.insights CSV sources).
- branch_options(company_id), location_options(company_id): pickers for the page forms.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from django.contrib.postgres.expressions import ArraySubquery
from django.db import IntegrityError, transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import (
    BooleanField, Count, DateTimeField, DurationField, Exists, ExpressionWrapper, F, FloatField, IntegerField, OuterRef, Q, Subquery,
    TextField, Value,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Extract, Floor, Greatest, Least, Now
from django.db.models.lookups import GreaterThan

from engines.attendance import local_date_of
from engines.bs_calendar import weekday_of

from hajiri.attendance.repo import from_ms, live_punches, to_ms
from hajiri.attendance.service import date_range, enqueue_recompute, is_attendance_locked, recompute_for_punch
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role, manages
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today
from hajiri.core.crypto import hash_password, sha256_hex
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, HttpError, NotFoundError, ValidationError
from hajiri.core.scoping import as_uuid, exists_in_company
from hajiri.core.settings_store import get_setting, put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    Branch, Company, Device, Employee, EmployeeAssignment, FieldVisit, Holiday, Kiosk, KioskCredential, Location,
    LocationAssignment, PunchEvent, Roster, Shift, ShiftAssignment, ShiftSwap, WorkBreak,
)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.punch.services import (
    PHOTO_FIELD_MAX, current_branch, device_for, ensure_pseudo_device, is_duplicate, npt_hm, npt_local_time, parse_photo,
    round_half_up, store_punch_photo, visible_to,
)

from .serializers import (
    BREAK_KINDS, BreaksQueryIn, BreakStartIn, DateQueryIn, FieldPolicyIn, KioskIn, FieldLocationIn, SwapNoteIn, PinIn,
    ReportQueryIn, RosterQueryIn, StaffIn, SwapIn, SwapsQueryIn, VisitEndIn, VisitStartIn,
)

__all__ = ['PHOTO_FIELD_MAX', 'BREAK_KINDS']

PAIRING_TTL_MIN = 15
MAX_RANGE_DAYS = 92
Swap = ShiftSwap


class TooManyError(HttpError):
    status, default_message = 429, 'too many requests'


def minutes_between(start, end) -> Cast:
    """floor((end - start) in minutes) as an integer expression."""
    span = ExpressionWrapper(end - start, output_field=DurationField())
    return Cast(Floor(Extract(span, 'epoch') / 60), IntegerField())


def minutes_after(start, minutes) -> ExpressionWrapper:
    """start + `minutes` minutes (make_interval(mins => minutes))."""
    return ExpressionWrapper(start + ExpressionWrapper(minutes * Value(timedelta(minutes=1)), output_field=DurationField()),
                             output_field=DateTimeField())


# ================================================================ policy.ts

BREAK_LABEL = {'lunch': 'Lunch', 'tea': 'Tea', 'prayer': 'Prayer', 'personal': 'Personal'}
FIELD_POLICY_KEY = 'field.policy'


def field_policy(company_id: str) -> dict:
    """The company's field rules; a stored value that no longer validates falls back to the defaults."""
    try:
        return validate(FieldPolicyIn, get_setting(company_id, FIELD_POLICY_KEY) or {})
    except ValidationError:
        return validate(FieldPolicyIn, {})


# ================================================================ punches.ts

def ensure_kiosk_device(company_id: str) -> dict:
    """One device row per company for kiosk punches (vendor 'kiosk', so the offline alarm ignores it)."""
    code = Company.objects.filter(pk=company_id).values_list('code', flat=True).get()
    return device_for(str(company_id), f'KIOSK-{code}', Device.Vendor.KIOSK, Device.Protocol.APP, 'Kiosk tablets')


def next_direction(employee_id: str, local_date: str | None = None) -> str:
    """Direction of the next punch today: in after nothing or an out, otherwise out."""
    last = (live_punches().filter(employee_id=employee_id, local_date=local_date or local_date_of(to_ms(_now())))
            .order_by('-punched_at').values_list('direction', flat=True).first())
    return 'in' if not last or last == PunchEvent.Direction.OUT else 'out'


def insert_app_punch(*, company_id: str, employee_id: str, emp_code: str, direction: str, kind: str, idempotency: str,
                     raw: dict, photo=None) -> dict:
    """Store a kiosk or field punch at server time, with an optional encrypted photo, then recompute the day."""
    key = sha256_hex(f'{kind}|{employee_id}|{idempotency}')
    existing = PunchEvent.objects.filter(idempotency_key=key).values('id', 'punched_at', 'local_date').first()
    if existing:
        return {'duplicate': True, 'punchId': str(existing['id']), 'punchedAt': existing['punched_at'],
                'localDate': existing['local_date'].isoformat(), 'photoId': None}
    at = to_ms(_now())
    device = ensure_kiosk_device(company_id) if kind == 'kiosk' else ensure_pseudo_device(company_id, 'MOBILE')
    photo_id = str(uuid.uuid4()) if photo else None
    flags = raw.get('flags') if isinstance(raw.get('flags'), list) else []
    with transaction.atomic():
        p = PunchEvent.objects.create(
            device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'], device_user_pin=emp_code,
            employee_id=employee_id, punched_at=from_ms(at), device_local_time=npt_local_time(at), local_date=local_date_of(at),
            direction=direction, verify_mode='kiosk' if kind == 'kiosk' else 'field', source=kind,
            raw={**raw, 'flags': flags, 'photoId': photo_id}, idempotency_key=key)
        if photo_id and photo:
            store_punch_photo(photo_id=photo_id, company_id=company_id, employee_id=employee_id, punch=p, photo=photo)
    recompute_for_punch(employee_id, at, str(p.id))
    return {'duplicate': False, 'punchId': str(p.id), 'punchedAt': p.punched_at, 'localDate': str(p.local_date),
            'photoId': photo_id}


def _break_end(end) -> dict:
    """The columns that close a break at `end` (an expression; never before it started)."""
    return {'ended_at': end, 'minutes': minutes_between(F('started_at'), end),
            'over_limit': GreaterThan(minutes_between(F('started_at'), end), F('max_min'))}


def close_open_break(employee_id: str, at) -> None:
    """Close an open break at `at` (used when someone clocks out at a kiosk mid-break)."""
    WorkBreak.objects.filter(employee_id=employee_id, ended_at__isnull=True).update(
        **_break_end(Greatest(Value(at), F('started_at'))))


# ================================================================ jobs.ts

def close_stale_breaks(now: datetime | None = None) -> int:
    """Nightly: close breaks left open from an earlier day. The break ends at the day's first clock-out after it started,
    capped at its limit, so a forgotten break never runs for hours."""
    cap = minutes_after(F('started_at'), F('max_min'))
    first_out = (PunchEvent.objects.filter(employee_id=OuterRef('employee_id'), local_date=OuterRef('local_date'),
                                           direction=PunchEvent.Direction.OUT, punched_at__gt=OuterRef('started_at'))
                 .order_by('punched_at').values('punched_at')[:1])
    end = Least(Coalesce(Subquery(first_out), cap), cap)
    return WorkBreak.objects.filter(ended_at__isnull=True, local_date__lt=npt_today(now)).update(**_break_end(end))


# ================================================================ swaps.ts

def effective_shift(employee_id: str, local_date: str) -> dict:
    """What someone works on a date: a roster row wins (NULL = off), then the weekly off, then the default shift."""
    return effective_shifts([employee_id], [local_date])[(str(employee_id), str(local_date))]


def effective_shifts(employee_ids, dates) -> dict[tuple[str, str], dict]:
    """effective_shift for every (employee, date) pair at once, in three queries whatever the number of people and
    days: {(employee_id, 'YYYY-MM-DD'): {'shiftId', 'source'}}."""
    emps = list(dict.fromkeys(str(e) for e in employee_ids))
    days = sorted({date.fromisoformat(str(d)) for d in dates})
    out: dict[tuple[str, str], dict] = {}
    if not emps or not days:
        return out
    off_days = {str(e['id']): e['weekly_off_days'] or []
                for e in Employee.objects.filter(pk__in=emps).values('id', 'weekly_off_days')}
    rostered = {(str(r['employee_id']), r['local_date']): r['shift_id'] for r in Roster.objects.filter(
        employee_id__in=emps, local_date__range=(days[0], days[-1])).values('employee_id', 'local_date', 'shift_id')}
    defaults: dict[str, list] = {}
    span = DateRange(days[0], days[-1], '[]')
    for a in (ShiftAssignment.objects.filter(employee_id__in=emps, valid__overlap=span)
              .values('employee_id', 'valid', 'shift_id')):
        defaults.setdefault(str(a['employee_id']), []).append((a['valid'], a['shift_id']))
    for e in emps:
        for day in days:
            iso = day.isoformat()
            if e not in off_days:
                eff = {'shiftId': None, 'source': 'none'}
            elif (e, day) in rostered:
                sid = rostered[(e, day)]
                eff = {'shiftId': str(sid), 'source': 'roster'} if sid else {'shiftId': None, 'source': 'roster_off'}
            elif weekday_of(iso) in off_days[e]:
                eff = {'shiftId': None, 'source': 'weekly_off'}
            else:
                sid = next((s for valid, s in defaults.get(e, []) if day in valid), None)
                eff = {'shiftId': str(sid), 'source': 'default'} if sid else {'shiftId': None, 'source': 'none'}
            out[(e, iso)] = eff
    return out


def apply_swap_decision(object_id: str, state: str, ctx=None) -> None:
    """Approved swap: each person takes the other's current shift for that day (roster rows), both are told, and both
    days are recomputed."""
    s = Swap.objects.select_for_update().filter(pk=object_id).first()
    if not s or s.status != Swap.Status.PENDING:
        return
    req, col, day = str(s.requester_id), str(s.colleague_id), s.local_date.isoformat()
    if state == Swap.Status.APPROVED:
        for i in (req, col):
            if is_attendance_locked(i, day):
                raise ConflictError('attendance for that day is locked by payroll')
        a = effective_shift(req, day)
        b = effective_shift(col, day)
        Roster.objects.bulk_create([Roster(employee_id=req, local_date=day, shift_id=b['shiftId']),
                                    Roster(employee_id=col, local_date=day, shift_id=a['shiftId'])],
                                   update_conflicts=True, unique_fields=['employee', 'local_date'], update_fields=['shift'])
        enqueue_recompute([(req, day), (col, day)], 'shift_swap')
    Swap.objects.filter(pk=s.pk).update(status=state, decided_at=Now())
    for emp in (req, col):
        user_id = user_id_for_employee(emp)
        if not user_id:
            continue
        notify(company_id=str(s.company_id), user_id=user_id, employee_id=emp, template=f'shift_swap.{state}',
               title='Shift swap approved' if state == 'approved' else 'Shift swap not approved',
               body=(f'The shift swap on {bs_label(day)} is approved. Check your roster.' if state == 'approved'
                     else f'The shift swap on {bs_label(day)} was not approved. Your shifts stay as they were.'),
               payload={'swapId': str(s.pk), 'localDate': day})


SWAP_SHIFTS = {'requester_shift_code': F('requester_shift__code'), 'requester_shift_name': F('requester_shift__name_en'),
               'colleague_shift_code': F('colleague_shift__code'), 'colleague_shift_name': F('colleague_shift__name_en')}


def swap_summary(inst: dict) -> dict:
    return (Swap.objects.filter(pk=inst['object_id'])
            .annotate(colleague_name=F('colleague__name_en'), **SWAP_SHIFTS)
            .values('local_date', 'reason', 'colleague_note', 'colleague_name', *SWAP_SHIFTS).first()) or {}


# ================================================================ routes.ts helpers

def check_range(from_: str, to: str, max_days: int = MAX_RANGE_DAYS) -> None:
    if to < from_:
        raise BadRequestError('"to" is before "from"')
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > max_days:
        raise BadRequestError(f'pick a range of {max_days} days or less')


def employee_of(user: AuthUser) -> dict:
    """The caller's own active employee record in the active company."""
    eid = user.employee_id
    if not eid:
        raise BadRequestError('Your account is not linked to an employee record. Ask HR to link it.')
    r = (Employee.objects.for_company(user.company_id).filter(pk=eid, status=Employee.Status.ACTIVE)
         .values('id', 'company_id', 'emp_code', 'name_en', 'field_work').first())
    if not r:
        raise NotFoundError('employee not found')
    return plain(r)


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _require_module(user: AuthUser, module: str) -> None:
    _require(user, *MODULE_ROLES[module])


def visit_aad(visit_id: str) -> str:
    return f'field_visit:{visit_id}'


def _today() -> str:
    return local_date_of(to_ms(_now()))


def _iso_dates(rows: list[dict], *keys: str) -> list[dict]:
    for r in rows:
        for k in keys:
            if r.get(k) is not None:
                r[k] = r[k].isoformat()
    return rows


# ================================================================ policy routes

def get_policy(user: AuthUser) -> dict:
    return {'policy': field_policy(company_id_of(user))}


def put_policy(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    value = validate(FieldPolicyIn, body)
    with transaction.atomic():
        before = field_policy(company_id)
        put_setting(company_id, FIELD_POLICY_KEY, value, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=FIELD_POLICY_KEY, before=before, after=value)
    return {'policy': value}


# ================================================================ my day

BREAK_FIELDS = ('id', 'kind', 'paid', 'max_min', 'started_at', 'ended_at', 'minutes', 'over_limit')


def me(user: AuthUser) -> dict:
    e = employee_of(user)
    today = _today()
    policy = field_policy(e['company_id'])
    direction = next_direction(e['id'], today)
    mine_today = Q(local_date=today) | Q(ended_at__isnull=True)
    breaks = plain(list(WorkBreak.objects.for_company(e['company_id']).filter(mine_today, employee_id=e['id'])
                        .order_by('started_at').values(*BREAK_FIELDS)))
    visits = plain(list(FieldVisit.objects.for_company(e['company_id']).filter(mine_today, employee_id=e['id'])
                        .annotate(has_photo=ExpressionWrapper(Q(photo_file__isnull=False), output_field=BooleanField()))
                        .order_by('started_at').values('id', 'place', 'purpose', 'started_at', 'ended_at', 'outcome', 'has_photo')))
    cred = KioskCredential.objects.filter(employee_id=e['id']).values('pin_hash', 'locked_until').first()
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
    b = validate(BreakStartIn, body)
    rule = field_policy(e['company_id'])['breaks'][b['kind']]
    if not rule['enabled']:
        raise BadRequestError('This kind of break is not used here.')
    if next_direction(e['id']) != 'out':
        raise BadRequestError('Clock in first, then start your break.')
    try:
        with transaction.atomic():
            br = WorkBreak.objects.create(company_id=e['company_id'], employee_id=e['id'], local_date=_today(), kind=b['kind'],
                                          paid=rule['paid'], max_min=rule['maxMin'], started_at=Now())
    except IntegrityError as err:
        # the partial unique index allows one open break per person (ON CONFLICT DO NOTHING in the TS code)
        if not is_duplicate(err):
            raise
        raise ConflictError('You are already on a break. End it first.') from None
    return {'break': plain(WorkBreak.objects.filter(pk=br.pk).values('id', 'kind', 'paid', 'max_min', 'started_at').get())}


def end_break(user: AuthUser) -> dict:
    from hajiri.workflow.service import approvers_for
    e = employee_of(user)
    with transaction.atomic():
        open_break = WorkBreak.objects.select_for_update().filter(employee_id=e['id'], ended_at__isnull=True)
        ids = list(open_break.values_list('id', flat=True))
        if not ids:
            raise ConflictError('You are not on a break.')
        WorkBreak.objects.filter(pk__in=ids).update(**_break_end(Now()))
        br = plain(WorkBreak.objects.filter(pk=ids[0])
                   .values('id', 'kind', 'minutes', 'max_min', 'over_limit', 'started_at', 'ended_at').get())
        if br['over_limit']:
            for uid in approvers_for(e['company_id'], e['id'], 'manager'):
                notify(company_id=e['company_id'], user_id=uid, employee_id=e['id'], template='break.over_limit', title='Long break',
                       body=f"{e['name_en']} took a {br['minutes']} min {br['kind']} break (limit {br['max_min']} min).",
                       payload={'breakId': br['id']})
    return {'break': br}


BREAK_CSV = [{'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name_en', 'header': 'Name'}, {'key': 'date_bs', 'header': 'Date (BS)'},
             {'key': 'kind', 'header': 'Break'}, {'key': 'paid', 'header': 'Paid'}, {'key': 'start', 'header': 'Start'},
             {'key': 'end', 'header': 'End'}, {'key': 'minutes', 'header': 'Minutes'}, {'key': 'max_min', 'header': 'Limit (min)'},
             {'key': 'over', 'header': 'Over limit'}]
VISIT_CSV = [{'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name_en', 'header': 'Name'}, {'key': 'date_bs', 'header': 'Date (BS)'},
             {'key': 'place', 'header': 'Place'}, {'key': 'purpose', 'header': 'Purpose'}, {'key': 'start', 'header': 'Arrived'},
             {'key': 'end', 'header': 'Left'}, {'key': 'minutes', 'header': 'Minutes'}, {'key': 'outcome', 'header': 'Outcome'},
             {'key': 'start_lat', 'header': 'Latitude'}, {'key': 'start_lng', 'header': 'Longitude'}]


def _report_scope(user: AuthUser, serializer, query: dict) -> dict:
    q = plain(validate(serializer, query))
    check_range(q['from'], q['to'])
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'], 'attendance')
    return q


def _in_report(qs, user: AuthUser, q: dict):
    qs = qs.for_company(company_id_of(user)).filter(local_date__range=(q['from'], q['to'])).filter(visible_to(user))
    return qs.filter(employee_id=q['employeeId']) if q.get('employeeId') else qs


def list_breaks(user: AuthUser, query: dict):
    q = _report_scope(user, BreaksQueryIn, query)
    qs = _in_report(WorkBreak.objects, user, q)
    if q.get('overOnly'):
        qs = qs.filter(over_limit=True)
    found = _iso_dates(plain(list(
        qs.annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                    minutes_so_far=Coalesce(F('minutes'), minutes_between(F('started_at'), Now())),
                    open=ExpressionWrapper(Q(ended_at__isnull=True), output_field=BooleanField()))
        .order_by('-started_at')
        .values('id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'kind', 'paid', 'max_min', 'started_at', 'ended_at',
                'minutes_so_far', 'over_limit', 'open')[:2000])), 'local_date')
    for r in found:
        r['minutes'] = r.pop('minutes_so_far')
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
    b = validate(VisitStartIn, body)
    policy = field_policy(e['company_id'])
    photo = parse_photo(b['photo']) if b.get('photo') else None
    if policy['fieldPhotoRequired'] and not photo:
        raise BadRequestError('Take a quick photo at the place you are visiting.', {'code': 'photo_required'})
    if FieldVisit.objects.filter(employee_id=e['id'], ended_at__isnull=True).exists():
        raise ConflictError('You have a visit in progress. End it first.')
    today = _today()
    acc = b.get('accuracyM')
    with transaction.atomic():
        v = FieldVisit.objects.create(
            company_id=e['company_id'], employee_id=e['id'], local_date=today, place=b['place'], purpose=b.get('purpose') or None,
            started_at=Now(), start_lat=b.get('lat'), start_lng=b.get('lng'),
            start_accuracy_m=round_half_up(acc) if acc is not None else None)
        vid = str(v.id)
        if photo:
            f = save_file(company_id=e['company_id'], purpose='field_visit_photo', filename=f'visit-{today}.jpg',
                          file=ParsedFile(photo.mime, photo.data), uploaded_by=user.id, attached_to=visit_aad(vid))
            FieldVisit.objects.filter(pk=vid).update(photo_file_id=f['id'])
    clocked_in = False
    if policy['fieldVisitClockIn'] and not PunchEvent.objects.filter(employee_id=e['id'], local_date=today).exists():
        p = insert_app_punch(company_id=e['company_id'], employee_id=e['id'], emp_code=e['emp_code'], direction='in', kind='field',
                             idempotency=f'visit-in|{vid}',
                             raw={'field': True, 'visitId': vid, 'place': b['place'], 'lat': b.get('lat'), 'lng': b.get('lng'),
                                  'accuracyM': b.get('accuracyM'), 'remote': False, 'flags': ['Field'], 'ip': ip})
        FieldVisit.objects.filter(pk=vid).update(punch_id=p['punchId'])
        clocked_in = True
    hm = npt_hm(_now())
    visit = plain(FieldVisit.objects.filter(pk=vid).values('id', 'place', 'purpose', 'started_at').get())
    return {'visit': visit, 'clockedIn': clocked_in,
            'message': f'Visit started and clocked in at {hm}.' if clocked_in else f'Visit started at {hm}.'}


def end_visit(user: AuthUser, visit_id: str, body: dict) -> dict:
    e = employee_of(user)
    vid = parse_uuid(visit_id)
    b = validate(VisitEndIn, body or {})
    acc = b.get('accuracyM')
    mine = FieldVisit.objects.for_company(e['company_id']).filter(pk=vid, employee_id=e['id'], ended_at__isnull=True)
    if not mine.update(ended_at=Now(), outcome=b.get('outcome') or None, end_lat=b.get('lat'), end_lng=b.get('lng'),
                       end_accuracy_m=round_half_up(acc) if acc is not None else None):
        raise NotFoundError('No visit in progress with that id.')
    return {'visit': plain(FieldVisit.objects.filter(pk=vid).values('id', 'place', 'started_at', 'ended_at', 'outcome').get())}


def _float(field: str) -> Cast:
    return Cast(field, FloatField())


def list_visits(user: AuthUser, query: dict):
    q = _report_scope(user, ReportQueryIn, query)
    found = _iso_dates(plain(list(
        _in_report(FieldVisit.objects, user, q)
        .annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), designation=F('employee__designation'),
                  start_lat_f=_float('start_lat'), start_lng_f=_float('start_lng'), end_lat_f=_float('end_lat'),
                  end_lng_f=_float('end_lng'),
                  has_photo=ExpressionWrapper(Q(photo_file__isnull=False), output_field=BooleanField()),
                  opened_day=ExpressionWrapper(Q(punch_id__isnull=False), output_field=BooleanField()),
                  minutes=minutes_between(F('started_at'), Coalesce(F('ended_at'), Now())))
        .order_by('-started_at')
        .values('id', 'employee_id', 'emp_code', 'name_en', 'designation', 'local_date', 'place', 'purpose', 'started_at',
                'ended_at', 'outcome', 'start_lat_f', 'start_lng_f', 'start_accuracy_m', 'end_lat_f', 'end_lng_f',
                'end_accuracy_m', 'has_photo', 'opened_day', 'minutes')[:2000])), 'local_date')
    for r in found:
        for k in ('start_lat', 'start_lng', 'end_lat', 'end_lng'):
            r[k] = r.pop(f'{k}_f')
    if q.get('format') == 'csv':
        return {'filename': f"field-visits-{q['from']}-{q['to']}", 'columns': VISIT_CSV,
                'rows': [{**x, 'date_bs': bs_label(x['local_date']), 'start': npt_hm(x['started_at']),
                          'end': npt_hm(x['ended_at']) if x['ended_at'] else ''} for x in found]}
    return {'visits': found}


def visit_photo(user: AuthUser, visit_id: str) -> dict:
    from hajiri.files.store import read_file
    vid = parse_uuid(visit_id)
    company_id = company_id_of(user)
    row = FieldVisit.objects.for_company(company_id).filter(pk=vid).values('employee_id', 'photo_file_id').first()
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

def branch_options(company_id: str) -> list[dict]:
    return plain(list(Branch.objects.for_company(company_id).order_by('name_en').values('id', 'name_en')))


def location_options(company_id: str) -> list[dict]:
    return plain(list(Location.objects.for_company(company_id).order_by('-active', 'name').values('id', 'name')))


def _assert_branch(company_id: str, branch_id) -> None:
    if branch_id and not exists_in_company(Branch, company_id, pk=branch_id):
        raise NotFoundError('branch not found')


def _assert_location(company_id: str, location_id) -> None:
    if location_id and not exists_in_company(Location, company_id, pk=location_id):
        raise NotFoundError('location not found')


def locations(user: AuthUser) -> dict:
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    found = plain(list(
        Location.objects.for_company(company_id_of(user))
        .annotate(branch_name=F('branch__name_en'), lat_f=_float('lat'), lng_f=_float('lng'),
                  employee_ids=ArraySubquery(LocationAssignment.objects.filter(location_id=OuterRef('pk')).values('employee_id')))
        .order_by('-active', 'name')
        .values('id', 'name', 'address', 'branch_id', 'branch_name', 'lat_f', 'lng_f', 'radius_m', 'active', 'employee_ids')))
    for r in found:
        r['lat'], r['lng'] = r.pop('lat_f'), r.pop('lng_f')
    return {'locations': found}


def _location_fields(b: dict) -> dict:
    return {'name': b['name'], 'address': b.get('address') or None, 'branch_id': b.get('branchId'), 'lat': b['lat'],
            'lng': b['lng'], 'radius_m': b['radiusM'], 'active': b['active']}


def create_location(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = plain(validate(FieldLocationIn, body))
    _assert_branch(company_id, b.get('branchId'))
    with transaction.atomic():
        loc = Location.objects.create(company_id=company_id, **_location_fields(b))
        audit('create', 'location', actor_id=user.id, row_id=str(loc.id), after=b)
    return {'id': str(loc.id)}


def update_location(user: AuthUser, location_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    lid = parse_uuid(location_id)
    b = plain(validate(FieldLocationIn, body))
    _assert_branch(company_id, b.get('branchId'))
    with transaction.atomic():
        if not Location.objects.for_company(company_id).filter(pk=lid).update(**_location_fields(b)):
            raise NotFoundError('location not found')
        audit('update', 'location', actor_id=user.id, row_id=lid, after=b)
    return {'ok': True}


def delete_location(user: AuthUser, location_id: str) -> dict:
    _require_module(user, 'attendance')
    lid = parse_uuid(location_id)
    with transaction.atomic():
        deleted, _ = Location.objects.for_company(company_id_of(user)).filter(pk=lid).delete()
        if not deleted:
            raise NotFoundError('location not found')
        audit('delete', 'location', actor_id=user.id, row_id=lid)
    return {'ok': True}


def staff(user: AuthUser) -> dict:
    """Staff settings for field work: the field-work switch, work from home, and locations tied to a person."""
    _require_module(user, 'attendance')
    cred = KioskCredential.objects.filter(employee_id=OuterRef('pk'))
    found = plain(list(
        Employee.objects.for_company(company_id_of(user)).filter(status=Employee.Status.ACTIVE)
        .annotate(branch_name=Subquery(EmployeeAssignment.objects.filter(employee_id=OuterRef('pk'), valid__contains=npt_today())
                                       .values('branch__name_en')[:1]),
                  location_ids=ArraySubquery(LocationAssignment.objects.filter(employee_id=OuterRef('pk')).values('location_id')),
                  kiosk_pin_set=Exists(cred.filter(pin_hash__isnull=False)),
                  kiosk_pin_locked=Exists(cred.filter(locked_until__gt=Now())))
        .order_by('emp_code')
        .values('id', 'emp_code', 'name_en', 'designation', 'field_work', 'work_from_home', 'branch_name', 'location_ids',
                'kiosk_pin_set', 'kiosk_pin_locked')))
    return {'staff': found}


def staff_name(user: AuthUser, employee_id: str) -> str:
    """An active employee's name in the active company, '' when there is none (for page messages)."""
    return (Employee.objects.for_company(company_id_of(user)).filter(pk=as_uuid(employee_id), status=Employee.Status.ACTIVE)
            .values_list('name_en', flat=True).first()) or ''


def update_staff(user: AuthUser, employee_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    b = plain(validate(StaffIn, body))
    if not exists_in_company(Employee, company_id, pk=eid):
        raise NotFoundError('employee not found')
    with transaction.atomic():
        if 'fieldWork' in b:
            Employee.objects.filter(pk=eid).update(field_work=b['fieldWork'])
        if 'locationIds' in b:
            ids = list(dict.fromkeys(b['locationIds']))
            if Location.objects.for_company(company_id).filter(pk__in=ids).count() != len(ids):
                raise NotFoundError('location not found')
            LocationAssignment.objects.filter(employee_id=eid).delete()
            LocationAssignment.objects.bulk_create(
                [LocationAssignment(location_id=loc, employee_id=eid, company_id=company_id) for loc in ids])
        audit('update', 'employee', actor_id=user.id, row_id=eid, after={'field': b})
    return {'ok': True}


# ================================================================ kiosks (HR)

def _new_pairing(kiosk_id: str) -> dict:
    from hajiri.kiosk.tokens import pairing_code
    code = pairing_code()
    expires = _now() + timedelta(minutes=PAIRING_TTL_MIN)
    Kiosk.objects.filter(pk=kiosk_id).update(pairing_code_hash=sha256_hex(f"kiosk-pair|{code.replace('-', '', 1)}"),
                                              pairing_expires_at=expires)
    return {'pairingCode': code, 'expiresAt': expires}


def _kiosk_punches_today() -> Coalesce:
    """Punches made at the outer kiosk today (punch_event.raw->>'kioskId')."""
    n = (PunchEvent.objects.filter(source='kiosk', local_date=npt_today())
         .annotate(kiosk_id=KT('raw__kioskId')).filter(kiosk_id=Cast(OuterRef('id'), TextField()))
         .order_by().values('source').annotate(n=Count('*')).values('n'))
    return Coalesce(Subquery(n, output_field=IntegerField()), Value(0))


def kiosks(user: AuthUser) -> dict:
    _require_module(user, 'attendance')
    found = plain(list(
        Kiosk.objects.for_company(company_id_of(user))
        .annotate(branch_name=F('branch__name_en'), location_name=F('location__name'),
                  locked=ExpressionWrapper(Q(locked_until__gt=Now()), output_field=BooleanField()),
                  pairing_open=ExpressionWrapper(Q(pairing_expires_at__gt=Now()), output_field=BooleanField()),
                  punches_today=_kiosk_punches_today(),
                  revoked=ExpressionWrapper(Q(status=Kiosk.Status.REVOKED), output_field=BooleanField()))
        .order_by('revoked', '-created_at')
        .values('id', 'name', 'branch_id', 'branch_name', 'location_id', 'location_name', 'selfie_required', 'status',
                'paired_at', 'last_seen_at', 'created_at', 'revoked_at', 'locked', 'pairing_open', 'pairing_expires_at',
                'punches_today')))
    return {'kiosks': found}


def kiosk_detail(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    k = (Kiosk.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(kiosk_id))
         .values('id', 'name', 'branch_id', 'location_id', 'selfie_required', 'status').first())
    if not k:
        raise NotFoundError('kiosk not found')
    return plain(k)


def _kiosk_fields(b: dict) -> dict:
    return {'name': b['name'], 'branch_id': b.get('branchId'), 'location_id': b.get('locationId'),
            'selfie_required': b['selfieRequired']}


def _kiosk_body(user: AuthUser, body: dict) -> tuple[str, dict]:
    company_id = company_id_of(user)
    b = plain(validate(KioskIn, body))
    _assert_branch(company_id, b.get('branchId'))
    _assert_location(company_id, b.get('locationId'))
    return company_id, b


def create_kiosk(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id, b = _kiosk_body(user, body)
    with transaction.atomic():
        k = Kiosk.objects.create(company_id=company_id, created_by_id=user.id, **_kiosk_fields(b))
        kid = str(k.id)
        pairing = _new_pairing(kid)
        audit('create', 'kiosk', actor_id=user.id, row_id=kid, after=b)
    return {'id': kid, **pairing}


def update_kiosk(user: AuthUser, kiosk_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    company_id, b = _kiosk_body(user, body)
    with transaction.atomic():
        if not Kiosk.objects.for_company(company_id).filter(pk=kid).update(**_kiosk_fields(b)):
            raise NotFoundError('kiosk not found')
        audit('update', 'kiosk', actor_id=user.id, row_id=kid, after=b)
    return {'ok': True}


def kiosk_pairing_code(user: AuthUser, kiosk_id: str) -> dict:
    """A fresh one-time code (to pair a replacement tablet); the old tablet keeps working until the new one pairs."""
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    status = Kiosk.objects.for_company(company_id_of(user)).filter(pk=kid).values_list('status', flat=True).first()
    if status is None:
        raise NotFoundError('kiosk not found')
    if status == Kiosk.Status.REVOKED:
        raise ConflictError('This kiosk was revoked. Add a new kiosk instead.')
    with transaction.atomic():
        pairing = _new_pairing(kid)
        audit('kiosk_pairing_code', 'kiosk', actor_id=user.id, row_id=kid)
    return pairing


def revoke_kiosk(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    with transaction.atomic():
        live = Kiosk.objects.for_company(company_id_of(user)).filter(pk=kid).exclude(status=Kiosk.Status.REVOKED)
        if not live.update(status=Kiosk.Status.REVOKED, key_hash=None, pairing_code_hash=None, pairing_expires_at=None,
                           revoked_at=Now()):
            raise NotFoundError('kiosk not found')
        audit('kiosk_revoked', 'kiosk', actor_id=user.id, row_id=kid)
    return {'ok': True}


def unlock_kiosk(user: AuthUser, kiosk_id: str) -> dict:
    _require_module(user, 'attendance')
    kid = parse_uuid(kiosk_id)
    with transaction.atomic():
        if not Kiosk.objects.for_company(company_id_of(user)).filter(pk=kid).update(locked_until=None, failed_attempts=0):
            raise NotFoundError('kiosk not found')
        audit('kiosk_unlocked', 'kiosk', actor_id=user.id, row_id=kid)
    return {'ok': True}


# ================================================================ kiosk PIN and QR badge

def set_kiosk_pin(user: AuthUser, body: dict) -> dict:
    from hajiri.kiosk.tokens import allow, weak_pin
    e = employee_of(user)
    if not allow(f"pin-set|{e['id']}", 10, 60 * 60_000):
        raise TooManyError('Too many PIN changes. Try again later.')
    b = validate(PinIn, body)
    if weak_pin(b['pin']):
        raise BadRequestError('That PIN is too easy to guess. Avoid repeated or consecutive digits.')
    with transaction.atomic():
        KioskCredential.objects.bulk_create(
            [KioskCredential(employee_id=e['id'], company_id=e['company_id'], pin_hash=hash_password(b['pin']), failed_count=0,
                             locked_until=None, updated_at=_now())],
            update_conflicts=True, unique_fields=['employee'], update_fields=['pin_hash', 'failed_count', 'locked_until', 'updated_at'])
        audit('kiosk_pin_set', 'kiosk_credential', actor_id=user.id, row_id=e['id'])
    return {'ok': True}


def reset_kiosk_pin(user: AuthUser, employee_id: str) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    if not exists_in_company(Employee, company_id, pk=eid):
        raise NotFoundError('employee not found')
    with transaction.atomic():
        KioskCredential.objects.update_or_create(
            employee_id=eid, create_defaults={'company_id': company_id},
            defaults={'pin_hash': None, 'failed_count': 0, 'locked_until': None, 'qr_version': F('qr_version') + 1,
                      'updated_at': Now()})
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
    KioskCredential.objects.get_or_create(employee_id=e['id'], defaults={'company_id': e['company_id']})
    version = KioskCredential.objects.filter(employee_id=e['id']).values_list('qr_version', flat=True).get()
    now_ms = to_ms(_now())
    w = qr_window(now_ms)
    return {'token': qr_token(e['id'], version, w), 'refreshInMs': (w + 1) * QR_WINDOW_MS - now_ms, 'empCode': e['emp_code']}


# ================================================================ my roster and shift swaps

def _shift_info(company_id: str) -> dict[str, dict]:
    out = {}
    for s in Shift.objects.for_company(company_id).values('id', 'code', 'name_en', 'start_time', 'end_time'):
        sid = str(s['id'])
        out[sid] = {'id': sid, 'code': s['code'], 'name_en': s['name_en'], 'start': s['start_time'].strftime('%H:%M'),
                    'end': s['end_time'].strftime('%H:%M')}
    return out


def my_roster(user: AuthUser, query: dict) -> dict:
    e = employee_of(user)
    q = validate(RosterQueryIn, query)
    check_range(q['from'], q['to'], 62)
    shifts = _shift_info(e['company_id'])
    branch_that_day = current_branch(on_date=OuterRef('local_date'), employee_ref=e['id'])
    hol = list(Holiday.objects.for_company(e['company_id']).filter(local_date__range=(q['from'], q['to']))
               .filter(Q(branch__isnull=True) | Q(branch_id=branch_that_day)).values('local_date', 'name_en'))
    dates = date_range(q['from'], q['to'])
    effective = effective_shifts([e['id']], dates)
    days = []
    for d in dates:
        eff = effective[(e['id'], d)]
        days.append({'date': d, 'weekday': weekday_of(d), 'source': eff['source'],
                     'shift': shifts.get(eff['shiftId']) if eff['shiftId'] else None,
                     'holidays': [h['name_en'] for h in hol if h['local_date'].isoformat() == d]})
    return {'days': days}


def _same_team(employee_id: str, on_date: str) -> Q | None:
    """Assignments on that day with the same manager or the same branch as the employee (None: no assignment)."""
    me_ = (EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=date.fromisoformat(on_date))
           .values('manager_id', 'branch_id').first())
    if me_ is None:
        return None
    same_manager = Q(manager_id=me_['manager_id']) if me_['manager_id'] else Q(manager__isnull=True)
    same_branch = Q(branch_id=me_['branch_id']) if me_['branch_id'] else Q(branch__isnull=True)
    return same_manager | same_branch


def swap_colleagues(user: AuthUser, query: dict) -> dict:
    """Colleagues one could swap with on a date (same manager or same branch), with what they work that day."""
    e = employee_of(user)
    q = validate(DateQueryIn, query)
    team = _same_team(e['id'], q['date'])
    found = []
    if team is not None:
        peers = EmployeeAssignment.objects.filter(team, valid__contains=date.fromisoformat(q['date'])).values('employee_id')
        found = plain(list(Employee.objects.for_company(e['company_id']).filter(status=Employee.Status.ACTIVE, pk__in=peers)
                           .exclude(pk=e['id']).order_by('name_en').values('id', 'emp_code', 'name_en', 'designation')[:200]))
    shifts = _shift_info(e['company_id'])
    effective = effective_shifts([e['id'], *(c['id'] for c in found)], [q['date']])
    mine = effective[(e['id'], q['date'])]
    out = []
    for c in found:
        eff = effective[(c['id'], q['date'])]
        out.append({**c, 'shift': shifts.get(eff['shiftId']) if eff['shiftId'] else None, 'off': not eff['shiftId'],
                    'sameAsMine': eff['shiftId'] == mine['shiftId']})
    return {'mine': {'shift': shifts.get(mine['shiftId']) if mine['shiftId'] else None, 'off': not mine['shiftId']}, 'colleagues': out}


SWAP_FIELDS = ('id', 'local_date', 'status', 'reason', 'colleague_note', 'created_at', 'decided_at', 'workflow_instance_id',
               'requester_id', 'requester_name', 'requester_code', 'colleague_id', 'colleague_name', 'colleague_code',
               'requester_shift_code', 'requester_shift_name', 'colleague_shift_code', 'colleague_shift_name')


def _swap_rows(qs, limit: int) -> list[dict]:
    return _iso_dates(plain(list(
        qs.annotate(requester_name=F('requester__name_en'), requester_code=F('requester__emp_code'),
                    colleague_name=F('colleague__name_en'), colleague_code=F('colleague__emp_code'), **SWAP_SHIFTS)
        .order_by('-local_date', '-created_at').values(*SWAP_FIELDS)[:limit])), 'local_date')


def swaps(user: AuthUser, query: dict) -> dict:
    q = validate(SwapsQueryIn, query)
    qs = Swap.objects.for_company(company_id_of(user))
    if q['scope'] == 'team':
        if not has_role(user, *MODULE_ROLES['attendance'], 'manager'):
            raise ForbiddenError('only managers and HR see team swaps')
        if q.get('from'):
            qs = qs.filter(local_date__gte=q['from'])
        if q.get('to'):
            qs = qs.filter(local_date__lte=q['to'])
        if not has_module(user, 'attendance'):
            qs = qs.filter(Q(manages(user.employee_id, F('requester_id'), F('local_date')))
                           | Q(manages(user.employee_id, F('colleague_id'), F('local_date'))))
        return {'swaps': _swap_rows(qs, 500)}
    if not user.employee_id:
        return {'swaps': []}
    return {'swaps': _swap_rows(qs.filter(Q(requester_id=user.employee_id) | Q(colleague_id=user.employee_id)), 200)}


def request_swap(user: AuthUser, body: dict) -> dict:
    e = employee_of(user)
    b = plain(validate(SwapIn, body))
    if b['localDate'] < _today():
        raise BadRequestError('Pick today or a later day.')
    if b['colleagueId'] == e['id']:
        raise BadRequestError('Pick a colleague to swap with.')
    if not Employee.objects.for_company(e['company_id']).filter(pk=b['colleagueId'], status=Employee.Status.ACTIVE).exists():
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
            s = Swap.objects.create(company_id=e['company_id'], requester_id=e['id'], colleague_id=b['colleagueId'],
                                    local_date=b['localDate'], requester_shift_id=mine['shiftId'],
                                    colleague_shift_id=theirs['shiftId'], reason=b.get('reason') or None)
    except IntegrityError as err:
        # unique (requester, local_date): one open request per day
        if not is_duplicate(err):
            raise
        raise ConflictError('You already have a swap request open for that day.') from None
    sid = str(s.id)
    uid = user_id_for_employee(b['colleagueId'])
    if uid:
        notify(company_id=e['company_id'], user_id=uid, employee_id=b['colleagueId'], template='shift_swap.asked', title='Shift swap request',
               body=f"{e['name_en']} asks to swap shifts with you on {bs_label(b['localDate'])}.", payload={'swapId': sid}, channels=['push'])
    return {'id': sid}


def _load_swap(swap_id: str, company_id: str) -> dict:
    s = (Swap.objects.for_company(company_id).filter(pk=as_uuid(swap_id))
         .annotate(requester_name=F('requester__name_en'), colleague_name=F('colleague__name_en'))
         .values('id', 'company_id', 'requester_id', 'colleague_id', 'local_date', 'status', 'requester_name', 'colleague_name')
         .first())
    if not s:
        raise NotFoundError('swap request not found')
    return _iso_dates([plain(s)], 'local_date')[0]


def _answerable(user: AuthUser, swap_id: str, body: dict | None) -> tuple[dict, dict]:
    """The swap the caller was asked to take, still waiting for their answer."""
    e = employee_of(user)
    b = validate(SwapNoteIn, body or {})
    s = _load_swap(parse_uuid(swap_id), e['company_id'])
    if s['colleague_id'] != e['id']:
        raise NotFoundError('swap request not found')
    if s['status'] != Swap.Status.AWAITING_COLLEAGUE:
        raise ConflictError('This request was already answered.')
    return s, b


def accept_swap(user: AuthUser, swap_id: str, body: dict | None = None) -> dict:
    from hajiri.workflow.service import start_workflow
    s, b = _answerable(user, swap_id, body)
    with transaction.atomic():
        inst = start_workflow(company_id=s['company_id'], object_type='shift_swap', object_id=s['id'], employee_id=s['requester_id'],
                              title='Shift swap to approve',
                              body=f"{s['requester_name']} and {s['colleague_name']} want to swap shifts on {bs_label(s['local_date'])}.")
        Swap.objects.filter(pk=s['id']).update(status=Swap.Status.PENDING, colleague_note=b.get('note') or None,
                                               workflow_instance_id=inst['id'])
        uid = user_id_for_employee(s['requester_id'])
        if uid:
            notify(company_id=s['company_id'], user_id=uid, employee_id=s['requester_id'], template='shift_swap.accepted', title='Swap accepted',
                   body=f"{s['colleague_name']} agreed to swap on {bs_label(s['local_date'])}. It now needs your manager's approval.")
    return {'status': 'pending'}


def decline_swap(user: AuthUser, swap_id: str, body: dict | None = None) -> dict:
    s, b = _answerable(user, swap_id, body)
    with transaction.atomic():
        Swap.objects.filter(pk=s['id']).update(status=Swap.Status.DECLINED, colleague_note=b.get('note') or None, decided_at=Now())
        uid = user_id_for_employee(s['requester_id'])
        if uid:
            notify(company_id=s['company_id'], user_id=uid, employee_id=s['requester_id'], template='shift_swap.declined', title='Swap declined',
                   body=f"{s['colleague_name']} can't swap on {bs_label(s['local_date'])}.")
    return {'status': 'declined'}


def cancel_swap(user: AuthUser, swap_id: str) -> dict:
    from hajiri.workflow.service import cancel_workflow
    e = employee_of(user)
    s = _load_swap(parse_uuid(swap_id), e['company_id'])
    if s['requester_id'] != e['id']:
        raise NotFoundError('swap request not found')
    if s['status'] not in (Swap.Status.AWAITING_COLLEAGUE, Swap.Status.PENDING):
        raise ConflictError('This request is already closed.')
    with transaction.atomic():
        Swap.objects.filter(pk=s['id']).update(status=Swap.Status.CANCELLED, decided_at=Now())
        cancel_workflow('shift_swap', s['id'])
    return {'status': 'cancelled'}
