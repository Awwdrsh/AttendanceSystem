"""
Attendance actions with their access rules (port of apps/api/src/modules/attendance/routes.ts). Pages call these
with the signed-in user; each checks roles, the ACTIVE company and manager scope exactly as the TS route did.
"""
from __future__ import annotations

from django.db.models import BooleanField, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Extract, Floor, Now
from rest_framework import serializers

from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role, manages
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.core.validation import IsoDateField, plain, validate
from hajiri.db.models import AttendanceDay, Employee, FieldVisit, PunchEvent, PunchPhoto, WorkBreak

from .repo import latest_days
from .service import INLINE_RECOMPUTE_MAX, date_range, enqueue_recompute, list_days, recompute_day


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _serializer(name: str, fields: dict) -> type[serializers.Serializer]:
    """A serializer whose field names include `from` (a Python keyword, so it cannot be a class attribute)."""
    return type(name, (serializers.Serializer,), fields)


RecomputeIn = _serializer('RecomputeIn', {'employeeId': serializers.UUIDField(), 'localDate': IsoDateField()})
RecomputeRangeIn = _serializer('RecomputeRangeIn', {
    'employeeIds': serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=5000),
    'from': IsoDateField(), 'to': IsoDateField()})
EmployeeRangeIn = _serializer('EmployeeRangeIn', {'employeeId': serializers.UUIDField(), 'from': IsoDateField(),
                                                  'to': IsoDateField()})
ExceptionsIn = _serializer('ExceptionsIn', {'from': IsoDateField(), 'to': IsoDateField(),
                                            'status': serializers.CharField(required=False, trim_whitespace=False)})
TodayIn = _serializer('TodayIn', {'date': IsoDateField()})

# Flags that make a day an exception besides being Incomplete or Absent.
EXCEPTION_FLAGS = ('OTCapExceeded', 'OTPreapprovalMissing', 'UnpairedPunch', 'AutoClockOut')


def recompute(user: AuthUser, body: dict) -> dict:
    """Recompute one day now (attendance team or a manager for someone they manage)."""
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    b = plain(validate(RecomputeIn, body))
    assert_manages(user, b['employeeId'], 'attendance')
    return recompute_day(b['employeeId'], b['localDate']).to_dict()


def recompute_range(user: AuthUser, body: dict) -> dict:
    """Bulk: small ranges are recomputed at once so the result is visible on return; larger ones go to the worker."""
    _require(user, *MODULE_ROLES['attendance'])
    b = plain(validate(RecomputeRangeIn, body))
    dates = date_range(b['from'], b['to'])
    if not dates or len(dates) > 62:
        raise BadRequestError('range must be 1 to 62 days')
    ids = list(dict.fromkeys(b['employeeIds']))
    if Employee.objects.for_company(company_id_of(user)).filter(pk__in=ids).count() != len(ids):
        raise ForbiddenError('some employees are not in your company')
    if len(ids) * len(dates) <= INLINE_RECOMPUTE_MAX:
        for e in ids:
            for d in dates:
                recompute_day(e, d)
        return {'queued': 0, 'recomputed': len(ids) * len(dates)}
    enqueue_recompute([(e, d) for e in ids for d in dates], 'manual')
    return {'queued': len(ids) * len(dates), 'recomputed': 0}


def days(user: AuthUser, query: dict) -> list[dict]:
    q = plain(validate(EmployeeRangeIn, query))
    assert_manages(user, q['employeeId'], 'attendance')
    return list_days(q['employeeId'], q['from'], q['to'])


def superseded():
    """`superseded`: a later correction replaced this punch."""
    return Exists(PunchEvent.objects.filter(supersedes_id=OuterRef('id')))


def punches(user: AuthUser, query: dict) -> list[dict]:
    """Raw punches someone may see (their own, their team's, or anyone's for the attendance team)."""
    q = plain(validate(EmployeeRangeIn, query))
    assert_manages(user, q['employeeId'], 'attendance')
    return list(PunchEvent.objects.filter(employee_id=q['employeeId'], local_date__range=(q['from'], q['to']))
                .annotate(superseded=superseded()).order_by('punched_at')
                .values('id', 'punched_at', 'local_date', 'direction', 'verify_mode', 'source', 'device_sn', 'supersedes_id',
                        'superseded'))


def _team(user: AuthUser, company_wide: bool, on_date, employee='employee_id') -> Q:
    """Everyone for the company-wide roles; otherwise the viewer and the people they manage on that date."""
    if company_wide:
        return Q()
    return Q(**{employee: user.employee_id}) | Q(manages(user.employee_id, F(employee), on_date))


def exceptions(user: AuthUser, query: dict) -> list[dict]:
    """Latest days with anomalies in a range; the attendance team company-wide, a manager for the people they manage."""
    _require(user, *MODULE_ROLES['attendance'], 'manager')
    q = validate(ExceptionsIn, query)
    flagged = Q(status__in=(AttendanceDay.Status.INCOMPLETE, AttendanceDay.Status.ABSENT))
    for kind in EXCEPTION_FLAGS:
        flagged |= Q(flags__contains=[{'kind': kind}])
    qs = (latest_days().for_company(company_id_of(user))
          .filter(flagged, local_date__range=(q['from'], q['to']), locked_at__isnull=True)
          .filter(_team(user, has_module(user, 'attendance'), F('local_date'))))
    if q.get('status') is not None:
        qs = qs.filter(status=q['status'])
    return list(qs.annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), name_ne=F('employee__name_ne'))
                .order_by('-local_date', 'employee__emp_code')
                .values('id', 'employee_id', 'emp_code', 'name_en', 'name_ne', 'local_date', 'bs_date', 'status',
                        'worked_min', 'late_min', 'early_min', 'ot_min', 'flags')[:1000])


DAY_KEYS = ('status', 'first_in', 'last_out', 'late_min', 'worked_min', 'flags')


def today(user: AuthUser, query: dict) -> dict:
    """Team Today: everyone's latest status for one date. The attendance team and executives see the company; a
    manager sees the people they manage and themselves."""
    _require(user, *MODULE_ROLES['attendance'], 'manager', 'executive')
    q = validate(TodayIn, query)
    d = q['date']
    cid = company_id_of(user)
    company_wide = has_module(user, 'attendance') or has_role(user, 'executive')
    day = {r['employee_id']: r for r in latest_days().for_company(cid).filter(local_date=d).values('employee_id', *DAY_KEYS)}
    here = {'employee': OuterRef('pk'), 'local_date': d}
    breaks = WorkBreak.objects.filter(**here)
    open_minutes = Cast(Floor(Extract(Now() - F('started_at'), 'epoch') / Value(60)), IntegerField())
    people = (Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE)
              .filter(_team(user, company_wide, d, employee='pk'))
              .annotate(
                  remote_punch=Exists(PunchEvent.objects.filter(**here).annotate(remote=KT('raw__remote')).filter(remote='true')),
                  photo_id=Subquery(PunchPhoto.objects.filter(**here, image_enc__isnull=False).order_by('punched_at')
                                    .values('id')[:1]),
                  on_break=Subquery(breaks.filter(ended_at__isnull=True).values('kind')[:1]),
                  break_min=Coalesce(Subquery(breaks.values('employee').annotate(n=Sum(Coalesce('minutes', open_minutes)))
                                              .values('n')[:1], output_field=IntegerField()), 0),
                  field_place=Subquery(FieldVisit.objects.filter(**here)
                                       .order_by(ExpressionWrapper(Q(ended_at__isnull=True), output_field=BooleanField()).desc(),
                                                 '-started_at').values('place')[:1]))
              .order_by('emp_code')
              .values('id', 'emp_code', 'name_en', 'name_ne', 'remote_punch', 'photo_id', 'on_break', 'break_min', 'field_place'))
    rows = []
    for p in people:
        v = day.get(p['id']) or {}
        rows.append({'employee_id': p['id'], 'emp_code': p['emp_code'], 'name_en': p['name_en'], 'name_ne': p['name_ne'],
                     **{k: v.get(k) for k in DAY_KEYS},
                     'remote': v.get('status') == AttendanceDay.Status.WFH or p['remote_punch'],
                     'photo_id': p['photo_id'], 'on_break': p['on_break'], 'break_min': p['break_min'],
                     'field_place': p['field_place']})
    return {'date': d, 'rows': rows}


def today_npt() -> str:
    return npt_today().isoformat()
