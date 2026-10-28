"""
Attendance reports, the ad-hoc report builder and saved reports (port of apps/api/src/modules/reports/routes.ts and
the builder half of catalog.ts). HR sees the active company, a manager the people they manage and themselves;
salary-based and device reports, and the builder, are HR only. Each report returns a Table (send_report / send_table
turn it into CSV for `?format=csv`).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import (
    BooleanField, Count, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Sum, TextField, Value,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Now
from rest_framework import serializers

from engines.bs_calendar import bs_month_bounds

from hajiri.attendance.repo import latest_days
from hajiri.company_settings.fields import JsonBool, JsonString, StrictSerializer
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, assert_manages, company_id_of, has_role, manages
from hajiri.core.clock import npt_today
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.validation import IsoDateField, parse_uuid, plain, validate
from hajiri.db.models import (
    AttendanceDay, BsCalendar, Device, Employee, EmployeeAssignment, LeaveLedger, LeaveRequest, LeaveType, OtSlip,
    PunchEvent, SalaryAssignment, SavedReport,
)

from .adhoc import AdhocDefinitionIn, build_adhoc_query, datasets as adhoc_datasets, parse_definition, plain_dict
from .csv import Table, to_fixed, cols, hours, npt_hhmm, table_response


def query_serializer(name: str, fields: dict) -> type[serializers.Serializer]:
    """A query-string serializer with `format` (json|csv); field names may be Python keywords such as `from`."""
    return type(name, (serializers.Serializer,), {'format': serializers.ChoiceField(('json', 'csv'), required=False), **fields})


FormatQuery = query_serializer('FormatQuery', {})
RangeQuery = query_serializer('RangeQuery', {'from': IsoDateField(), 'to': IsoDateField()})
DateQuery = query_serializer('DateQuery', {'date': IsoDateField()})
RegisterQuery = query_serializer('RegisterQuery', {'bsYear': serializers.IntegerField(),
                                                   'bsMonth': serializers.IntegerField(min_value=1, max_value=12),
                                                   'employeeId': serializers.UUIDField(required=False)})
PunchAuditQuery = query_serializer('PunchAuditQuery', {'from': IsoDateField(), 'to': IsoDateField(),
                                                       'employeeId': serializers.UUIDField()})


def _q(serializer, query) -> dict:
    return plain(validate(serializer, query))


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def team(user: AuthUser, employee: str = 'employee_id', on_date=None) -> Q:
    """HR sees everyone; otherwise the viewer and the people they manage (today, Nepal date)."""
    if has_role(user, 'hr'):
        return Q()
    return Q(**{employee: user.employee_id}) | Q(manages(user.employee_id, F(employee), on_date or npt_today()))


def placement(on_date, path: str, ref: str = 'pk') -> Subquery:
    """A value of the employee's placement on a date (branch, department, manager, employment type)."""
    on = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
    return Subquery(EmployeeAssignment.objects.filter(employee=OuterRef(ref), valid__contains=on).values(path)[:1])


DAY_FIELDS = ('status', 'first_in', 'last_out', 'worked_min', 'late_min', 'early_min', 'ot_min', 'flags')


def _flag(kind: str) -> Q:
    return Q(flags__contains=[{'kind': kind}])


def attendance_report(user: AuthUser, name: str, query: dict) -> Table:
    _require(user, 'hr', 'manager')
    cid = company_id_of(user)
    today = npt_today()
    days = latest_days().for_company(cid)

    if name == 'daily':
        q = _q(DateQuery, query)
        d = q['date']
        people = (Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE).filter(team(user, 'id'))
                  .annotate(branch_name=placement(d, 'branch__name_en')).order_by('emp_code')
                  .values('id', 'emp_code', 'name_en', 'name_ne', 'branch_name'))
        on_day = {r.pop('employee_id'): r for r in days.filter(local_date=d).values('employee_id', *DAY_FIELDS)}
        out = []
        for e in people:
            v = on_day.get(e['id']) or dict.fromkeys(DAY_FIELDS)
            out.append({'emp_code': e['emp_code'], 'name_en': e['name_en'], 'name_ne': e['name_ne'], 'branch': e['branch_name'],
                        **v, 'status': v['status'] or 'NotComputed', 'arrival': npt_hhmm(v['first_in']),
                        'departure': npt_hhmm(v['last_out']), 'worked_hours': hours(v['worked_min']),
                        'ot_hours': hours(v['ot_min']),
                        'flags': json.dumps(v['flags'] if v['flags'] is not None else [], separators=(',', ':'), ensure_ascii=False)})
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('branch', 'Branch'), ('status', 'Status'),
                          ('arrival', 'Arrival'), ('departure', 'Departure'), ('worked_hours', 'Worked (h)'),
                          ('late_min', 'Late (min)'), ('early_min', 'Early out (min)'), ('ot_hours', 'OT (h)'), ('flags', 'Flags')),
                     out, f"daily-{q['date']}")

    if name == 'register':
        # Monthly attendance register, inspector-ready: one row per employee per calendar day of the BS month.
        q = _q(RegisterQuery, query)
        if q.get('employeeId'):
            assert_manages(user, q['employeeId'])
        bounds = bs_month_bounds(q['bsYear'], q['bsMonth'])
        start, end = date.fromisoformat(bounds['startAd']), date.fromisoformat(bounds['endAd'])
        people = (Employee.objects.for_company(cid).filter(Q(join_date__isnull=True) | Q(join_date__lte=end))
                  .filter(Q(exit_date__isnull=True) | Q(exit_date__gte=start)).filter(team(user, 'id')))
        if q.get('employeeId'):
            people = people.filter(pk=q['employeeId'])
        people = list(people.order_by('emp_code').values('id', 'emp_code', 'name_en', 'name_ne'))
        ids = [e['id'] for e in people]
        calendar = dict(BsCalendar.objects.filter(ad_date__range=(start, end)).values_list('ad_date', 'bs_date'))
        stored = {(r['employee_id'], r['local_date']): r for r in days.filter(employee_id__in=ids, local_date__range=(start, end))
                  .values('employee_id', 'local_date', 'status', 'first_in', 'last_out', 'worked_min', 'break_min', 'ot_min',
                          'late_min')}
        leave = list(LeaveRequest.objects.filter(employee_id__in=ids, status=LeaveRequest.Status.APPROVED, from_date__lte=end,
                                                 to_date__gte=start).values('employee_id', 'from_date', 'to_date', 'leave_type__code'))
        out = []
        for e in people:
            mine = [lv for lv in leave if lv['employee_id'] == e['id']]
            d = start
            while d <= end:
                v = stored.get((e['id'], d)) or {}
                code = next((lv['leave_type__code'] for lv in mine if lv['from_date'] <= d <= lv['to_date']), None)
                out.append({'emp_code': e['emp_code'], 'name_en': e['name_en'], 'name_ne': e['name_ne'], 'bs_date': calendar.get(d),
                            'ad_date': d.isoformat(), 'arrival': npt_hhmm(v.get('first_in')), 'departure': npt_hhmm(v.get('last_out')),
                            'worked_hours': hours(v.get('worked_min')), 'ot_hours': hours(v.get('ot_min')),
                            'break_min': v.get('break_min') or 0, 'late_min': v.get('late_min') or 0, 'leave': code or '',
                            'status': v.get('status') or ''})
                d += timedelta(days=1)
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('bs_date', 'Date (BS)'), ('ad_date', 'Date (AD)'),
                          ('arrival', 'Arrival'), ('departure', 'Departure'), ('worked_hours', 'Worked (h)'),
                          ('break_min', 'Break (min)'), ('ot_hours', 'OT (h)'), ('late_min', 'Late (min)'), ('leave', 'Leave'),
                          ('status', 'Status')), out, f"register-{q['bsYear']}-{q['bsMonth']:02d}")

    if name == 'ot':
        q = _q(RangeQuery, query)
        found = (OtSlip.objects.for_company(cid).filter(local_date__range=(q['from'], q['to'])).filter(team(user))
                 .annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))
                 .order_by('local_date', 'employee__emp_code')
                 .values('emp_code', 'name_en', 'local_date', 'day_type', 'detected_min', 'approved_min', 'multiplier', 'status'))
        out = [{**x, 'detected_hours': hours(x['detected_min']), 'approved_hours': hours(x['approved_min'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('local_date', 'Date'), ('day_type', 'Day type'),
                          ('detected_hours', 'Detected (h)'), ('approved_hours', 'Approved (h)'), ('multiplier', 'Rate'),
                          ('status', 'Status')), out, f"ot-{q['from']}_{q['to']}")

    if name == 'late-early':
        q = _q(RangeQuery, query)
        found = list(days.filter(local_date__range=(q['from'], q['to'])).filter(team(user))
                     .values('employee_id').annotate(
                         emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                         late_days=Count('id', filter=Q(late_min__gt=0)), late_min=Coalesce(Sum('late_min'), 0),
                         early_days=Count('id', filter=Q(early_min__gt=0)), early_min=Coalesce(Sum('early_min'), 0),
                         absent_days=Count('id', filter=Q(status=AttendanceDay.Status.ABSENT)),
                         half_days=Count('id', filter=Q(status=AttendanceDay.Status.HALF_DAY)))
                     .order_by('-late_days', 'employee__emp_code'))
        out = [{k: x[k] for k in ('emp_code', 'name_en', 'late_days', 'late_min', 'early_days', 'early_min', 'absent_days',
                                  'half_days')} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('late_days', 'Late days'), ('late_min', 'Late (min)'),
                          ('early_days', 'Early-out days'), ('early_min', 'Early (min)'), ('absent_days', 'Absent'),
                          ('half_days', 'Half days')), out, f"late-early-{q['from']}_{q['to']}")

    if name == 'leave-balances':
        # Balance per employee and type; liability = balance x daily basic for encashable types.
        _require(user, 'hr')
        _q(FormatQuery, query)
        people = list(Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE).order_by('emp_code')
                      .values('id', 'emp_code', 'name_en'))
        types = list(LeaveType.objects.for_company(cid).exclude(accrual_basis=LeaveType.AccrualBasis.NONE)
                     .order_by('sort_order', 'code').values('id', 'code', 'encashable'))
        balance = {(r['employee_id'], r['leave_type_id']): r['n'] for r in LeaveLedger.objects.for_company(cid)
                   .values('employee_id', 'leave_type_id').annotate(n=Sum('delta_days'))}
        basic = dict(SalaryAssignment.objects.for_company(cid).filter(valid__contains=today)
                     .annotate(basic=KT('base_amounts__BASIC')).values_list('employee_id', 'basic'))
        month = BsCalendar.objects.filter(ad_date=today).values('bs_year', 'bs_month').first()
        n = BsCalendar.objects.filter(**month).count() if month else 0
        days_in_month = n or 30
        out = []
        for e in people:
            pay = float(basic[e['id']]) if basic.get(e['id']) else None
            for t in types:
                b = Decimal(balance.get((e['id'], t['id'])) or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                liability = to_fixed((pay / days_in_month) * float(b), 2) if t['encashable'] and pay is not None else ''
                out.append({'emp_code': e['emp_code'], 'name_en': e['name_en'], 'leave_type': t['code'], 'balance': str(b),
                            'encashable': t['encashable'], 'liability_npr': liability})
        return Table(cols(('emp_code', 'Emp code'), ('name_en', 'Name'), ('leave_type', 'Leave type'), ('balance', 'Balance (days)'),
                          ('encashable', 'Encashable'), ('liability_npr', 'Liability (NPR)')), out, 'leave-balances')

    if name == 'headcount':
        _q(FormatQuery, query)
        found = list(Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE)
                     .annotate(branch_name=Coalesce(placement(today, 'branch__name_en'), Value('(no branch)'), output_field=TextField()),
                               unit_name=Coalesce(placement(today, 'org_unit__name_en'), Value('(no unit)'), output_field=TextField()),
                               employment=placement(today, 'employment_type'))
                     .values('branch_name', 'unit_name', 'employment')
                     .annotate(headcount=Count('id'), female=Count('id', filter=Q(gender=Employee.Gender.FEMALE)),
                               male=Count('id', filter=Q(gender=Employee.Gender.MALE)))
                     .order_by('branch_name', 'unit_name', 'employment'))
        out = [{'branch': x['branch_name'], 'org_unit': x['unit_name'], 'employment_type': x['employment'],
                'headcount': x['headcount'], 'female': x['female'], 'male': x['male']} for x in found]
        return Table(cols(('branch', 'Branch'), ('org_unit', 'Unit'), ('employment_type', 'Type'), ('headcount', 'Headcount'),
                          ('female', 'Female'), ('male', 'Male')), out, 'headcount')

    if name == 'exceptions-summary':
        q = _q(RangeQuery, query)
        found = list(days.filter(local_date__range=(q['from'], q['to'])).filter(team(user))
                     .values('local_date').annotate(
                         incomplete=Count('id', filter=Q(status=AttendanceDay.Status.INCOMPLETE)),
                         absent=Count('id', filter=Q(status=AttendanceDay.Status.ABSENT)),
                         ot_unapproved=Count('id', filter=_flag('OTPreapprovalMissing')),
                         ot_cap=Count('id', filter=_flag('OTCapExceeded')),
                         late=Count('id', filter=Q(late_min__gt=0)))
                     .order_by('local_date'))
        return Table(cols(('local_date', 'Date'), ('incomplete', 'Incomplete'), ('absent', 'Absent'), ('late', 'Late'),
                          ('ot_unapproved', 'OT without approval'), ('ot_cap', 'OT cap exceeded')),
                     found, f"exceptions-{q['from']}_{q['to']}")

    if name == 'devices':
        _require(user, 'hr')
        _q(FormatQuery, query)
        punches_today = (PunchEvent.objects.filter(device=OuterRef('pk'), local_date=today).values('device')
                         .annotate(n=Count('*')).values('n')[:1])
        unattended = (Device.Vendor.MOBILE, Device.Vendor.KIOSK, Device.Vendor.MANUAL)
        found = list(Device.objects.for_company(cid)
                     .annotate(offline=ExpressionWrapper(
                                   Q(status=Device.Status.ACTIVE) & ~Q(vendor__in=unattended)
                                   & (Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=Now() - timedelta(seconds=120))),
                                   output_field=BooleanField()),
                               punches_today=Coalesce(Subquery(punches_today, output_field=IntegerField()), 0))
                     .order_by('status', 'serial_no')
                     .values('serial_no', 'name', 'vendor', 'status', 'fw_version', 'last_handshake_at', 'last_seen_at', 'offline',
                             'punches_today'))
        return Table(cols(('serial_no', 'Serial'), ('name', 'Name'), ('vendor', 'Vendor'), ('status', 'Status'),
                          ('fw_version', 'Firmware'), ('last_seen_at', 'Last seen'), ('offline', 'Offline'),
                          ('punches_today', 'Punches today')), found, 'device-health')

    if name == 'punch-audit':
        q = _q(PunchAuditQuery, query)
        assert_manages(user, q['employeeId'])
        found = (PunchEvent.objects.for_company(cid).filter(employee_id=q['employeeId'], local_date__range=(q['from'], q['to']))
                 .annotate(superseded=Exists(PunchEvent.objects.filter(supersedes_id=OuterRef('id'))),
                           reason=KT('raw__reason'))
                 .order_by('punched_at')
                 .values('id', 'local_date', 'punched_at', 'direction', 'verify_mode', 'source', 'device_sn', 'ingested_at',
                         'supersedes_id', 'superseded', 'reason'))
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
    found = built.fetch()
    if csv:
        audit('export', 'adhoc_report', actor_id=user.id, after={'dataset': d['dataset'], 'columns': d['columns'], 'rows': len(found)})
        return table_response(Table(built.columns, found, name))
    total = built.count.count()
    return {'columns': [c.to_dict() for c in built.columns], 'rows': found, 'total': total, 'limited': total > len(found)}


class SavedReportIn(StrictSerializer):
    name = JsonString(min_length=1, max_length=120, allow_blank=False, trim_whitespace=True)
    description = JsonString(max_length=500, trim_whitespace=True, required=False, allow_null=True)
    shared = JsonBool(default=False)
    definition = AdhocDefinitionIn()


SAVED_FIELDS = ('id', 'name', 'description', 'shared', 'definition', 'updated_at')


def _saved_body(body) -> dict:
    b = validate(SavedReportIn, body)
    return {**b, 'definition': plain_dict(b['definition'])}


def list_saved(user: AuthUser) -> list[dict]:
    _require(user, 'hr')
    return list(SavedReport.objects.for_company(company_id_of(user)).filter(Q(created_by_id=user.id) | Q(shared=True))
                .annotate(mine=ExpressionWrapper(Q(created_by_id=user.id), output_field=BooleanField()),
                          owner_name=F('created_by__name'))
                .order_by('name')
                .values('id', 'name', 'description', 'shared', 'definition', 'mine', 'owner_name', 'updated_at'))


def _saved_row(report_id) -> dict:
    r = SavedReport.objects.filter(pk=report_id).values(*SAVED_FIELDS).get()
    return {**r, 'mine': True}


@transaction.atomic
def create_saved(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    b = _saved_body(body)
    r = SavedReport.objects.create(company_id=company_id_of(user), name=b['name'], description=b.get('description'),
                                   shared=b['shared'], definition=b['definition'], created_by_id=user.id)
    audit('create', 'saved_report', actor_id=user.id, row_id=str(r.pk), after={'name': b['name'], 'dataset': b['definition']['dataset']})
    return _saved_row(r.pk)


def _own_saved(user: AuthUser, report_id: str) -> dict:
    parse_uuid(report_id)
    r = (SavedReport.objects.for_company(company_id_of(user)).filter(pk=report_id)
         .values('id', 'created_by_id', 'shared', 'definition', 'name').first())
    if not r or (str(r['created_by_id']) != user.id and not r['shared']):
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
    if str(r['created_by_id']) != user.id:
        raise ForbiddenError('only the person who saved this report can change it')
    b = _saved_body(body)
    SavedReport.objects.filter(pk=r['id']).update(name=b['name'], description=b.get('description'), shared=b['shared'],
                                                  definition=b['definition'], updated_at=Now())
    return _saved_row(r['id'])


@transaction.atomic
def delete_saved(user: AuthUser, report_id: str) -> None:
    _require(user, 'hr')
    r = _own_saved(user, report_id)
    if str(r['created_by_id']) != user.id:
        raise ForbiddenError('only the person who saved this report can delete it')
    SavedReport.objects.filter(pk=r['id']).delete()
    audit('delete', 'saved_report', actor_id=user.id, row_id=str(r['id']), before={'name': r['name']})
