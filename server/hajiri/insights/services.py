"""
Workforce insights for HR and executives (port of apps/api/src/modules/insights/service.ts and routes.ts): headcount,
joiners and leavers, attrition, demographics, people cost, overtime, leave and attendance trends by Bikram Sambat
month, filtered by branch and department, and every dataset as CSV.

Everything is read through the ORM and scoped to the ACTIVE company (`company_id_of(user)`): the employees of the
company (narrowed by branch and department), and their attendance, overtime, leave and locked pay. A branch or
department of another company is 'unknown', like the TS route.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import (
    BooleanField, Case, Count, DateField, Exists, ExpressionWrapper, F, Func, OuterRef, Q, Subquery, Sum, TextField,
    Value, When, Avg,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce, TruncDate

from engines.bs_calendar import BS_MONTH_NAMES_EN, ad_to_bs, bs_month_bounds

from hajiri.core.numbers import round_half_up
from hajiri.core.auth import AuthUser, company_id_of, has_role, MODULE_ROLES
from hajiri.core.clock import npt_today
from hajiri.core.collation import collate_key
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.core.scoping import exists_in_company
from hajiri.core.validation import plain, validate
from hajiri.db.models import (
    AttendanceDay, Branch, Employee, EmployeeAssignment, EmployeeExit, LeaveLedger, OrgUnit, OtSlip, PayrollRun, Payslip,
)

from .serializers import InsightsFilterIn

NPT = ZoneInfo('Asia/Kathmandu')

# Axis abbreviations that stay distinct (Ashadh and Ashwin both start "Ash").
SHORT_MONTHS = ['Bai', 'Jes', 'Asr', 'Shr', 'Bha', 'Asw', 'Kar', 'Man', 'Pou', 'Mag', 'Fal', 'Cha']

A = AttendanceDay.Status
SCHEDULED = (A.PRESENT, A.HALF_DAY, A.ABSENT, A.WFH, A.INCOMPLETE, A.ON_LEAVE, A.HALF_DAY_LEAVE)
WORKED = (A.PRESENT, A.HALF_DAY, A.WFH, A.INCOMPLETE)
OT_COUNTED = (OtSlip.Status.APPROVED, OtSlip.Status.POSTED)
LEAVE_TAKEN = (LeaveLedger.Reason.USAGE, LeaveLedger.Reason.CANCELLATION)
# People cost comes from locked (or later) pay runs only: drafts can still change.
PAY_FINAL = (PayrollRun.Status.LOCKED, PayrollRun.Status.DISBURSED, PayrollRun.Status.FILED)
# Optional columns some installs add to employee for the exit reason, in order of preference.
EXIT_COLUMNS = ('separation_type', 'exit_type', 'exit_reason', 'separation_reason')




def _require(user: AuthUser) -> None:
    roles = MODULE_ROLES['insights']
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


# ---------------------------------------------------------------- months

def _ym(s: str) -> tuple[int, int]:
    m = re.fullmatch(r'(\d{4})-(\d{1,2})', s)
    if not m:
        raise BadRequestError('months are written as YYYY-MM in the Bikram Sambat calendar')
    y, mo = int(m[1]), int(m[2])
    if mo < 1 or mo > 12:
        raise BadRequestError('month must be 1 to 12')
    return y, mo


def _key(y: int, m: int) -> str:
    return f'{y}-{m:02d}'


def _next(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def months_between(frm: str | None, to: str | None, today: str) -> list[dict]:
    """BS months from `frm` to `to` inclusive (default: the last 12 months up to this one), at most 36."""
    now = ad_to_bs(today)
    end = _ym(to) if to else (now.year, now.month)
    start = _ym(frm) if frm else end
    if not frm:
        for _ in range(11):
            start = _prev(*start)
    out: list[dict] = []
    cur = start
    while cur[0] < end[0] or (cur[0] == end[0] and cur[1] <= end[1]):
        try:
            b = bs_month_bounds(cur[0], cur[1])
        except Exception:
            raise BadRequestError('that range is outside the supported calendar')
        name = BS_MONTH_NAMES_EN[cur[1] - 1]
        out.append({'key': _key(*cur), 'label': f'{name} {cur[0]}', 'short': f'{SHORT_MONTHS[cur[1] - 1]} {str(cur[0])[-2:]}',
                    'startAd': b['startAd'], 'endAd': b['endAd']})
        if len(out) > 36:
            raise BadRequestError('pick at most 36 months')
        cur = _next(*cur)
    if not out:
        raise BadRequestError('the start month is after the end month')
    return out


# ---------------------------------------------------------------- exit reasons

def _employee_exit_column() -> str | None:
    """An exit-reason column added to employee by the install, if any (Django's schema introspection)."""
    # Schema introspection, not a query of company data: the column is not part of the models.
    with connection.cursor() as cur:
        cols = {c.name for c in connection.introspection.get_table_description(cur, Employee._meta.db_table)}
    return next((c for c in EXIT_COLUMNS if c in cols), None)


def exit_reason_source():
    """
    The exit kind of each employee as an expression on Employee: an exit-reason column on employee when the install
    added one, otherwise the kind of the employee's exit record (resignation, termination, ...).
    """
    col = _employee_exit_column()
    if col:
        qn = connection.ops.quote_name
        # Raw SQL: the column is optional and added outside the models, so the ORM has no field for it.
        return RawSQL(f'{qn(Employee._meta.db_table)}.{qn(col)}::text', (), output_field=TextField())
    return Subquery(EmployeeExit.objects.filter(employee=OuterRef('pk')).values('kind')[:1], output_field=TextField())


def classify_exit(kind: str | None) -> str | None:
    if not kind:
        return None
    k = kind.lower()
    if re.search(r'involuntary|terminat|dismiss|layoff|lay_off|redundan|fired|misconduct', k):
        return 'involuntary'
    if re.search(r'resign|voluntary|quit|transfer', k):  # a transfer to a sister company is the person's move
        return 'voluntary'
    return 'other'


# ---------------------------------------------------------------- helpers

def _years_between(a: str, b: str) -> float:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days / 365.25


def _bands(items: list, labels: list[str], pick) -> list[dict]:
    m = {label: 0 for label in labels}
    for r in items:
        k = pick(r)
        m[k] = m.get(k, 0) + 1
    return [{'label': k, 'count': v} for k, v in m.items()]


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def _month_of(field: str, months: list[dict]) -> Case:
    """The BS month key ('2083-04') a date column falls in, for grouping by month."""
    return Case(*[When(**{f'{field}__range': (m['startAd'], m['endAd'])}, then=Value(m['key'])) for m in months],
                default=Value(None), output_field=TextField())


def _units_under(company_id: str, unit_id: str) -> set[str]:
    """A department and every unit below it (the org tree is small: one query, walked here)."""
    children: dict[str, list[str]] = {}
    for uid, parent in OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id'):
        children.setdefault(str(parent), []).append(str(uid))
    out, todo = set(), [str(unit_id)]
    while todo:
        u = todo.pop()
        if u not in out:
            out.add(u)
            todo.extend(children.get(u, []))
    return out


# ---------------------------------------------------------------- filters

def filter_options(user: AuthUser) -> dict:
    """Branches and departments for the filter row."""
    _require(user)
    c = company_id_of(user)
    return {
        'branches': [{'id': str(i), 'name': n} for i, n in
                     Branch.objects.for_company(c).order_by('name_en').values_list('id', 'name_en')],
        'orgUnits': [{'id': str(i), 'name': n, 'kind': k, 'parentId': str(p) if p else None} for i, n, k, p in
                     OrgUnit.objects.for_company(c).order_by('name_en').values_list('id', 'name_en', 'kind', 'parent_id')],
    }


def parse_filters(user: AuthUser, query: dict) -> dict:
    """The TS route's `filters`: query shape, then the branch and department must belong to the company."""
    _require(user)
    q = plain(validate(InsightsFilterIn, {k: v for k, v in query.items()
                                          if k in ('from', 'to', 'branchId', 'orgUnitId') and v not in (None, '')}))
    c = company_id_of(user)
    if q.get('branchId') and not exists_in_company(Branch, c, pk=q['branchId']):
        raise BadRequestError('unknown branch')
    if q.get('orgUnitId') and not exists_in_company(OrgUnit, c, pk=q['orgUnitId']):
        raise BadRequestError('unknown department')
    return {'companyId': c, 'from': q.get('from'), 'to': q.get('to'), 'branchId': q.get('branchId'),
            'orgUnitId': q.get('orgUnitId'), 'today': npt_today().isoformat()}


# ---------------------------------------------------------------- the queries

def _employees(f: dict):
    """The company's employees with their current placement (the assignment valid today, else the latest one),
    narrowed by branch and department (with the units below it). Also the subquery of the other figures, so it carries
    no exit-reason expression (an install's raw column would not survive Django's subquery aliasing)."""
    c = f['companyId']
    placement = (EmployeeAssignment.objects.filter(employee=OuterRef('pk'))
                 .order_by(ExpressionWrapper(Q(valid__contains=date.fromisoformat(f['today'])), output_field=BooleanField()).desc(),
                           Func(F('valid'), function='lower', output_field=DateField()).desc()))
    qs = (Employee.objects.for_company(c)
          .annotate(placed_branch=Subquery(placement.values('branch_id')[:1]),
                    placed_unit=Subquery(placement.values('org_unit_id')[:1]),
                    unit_name=Subquery(placement.values('org_unit__name_en')[:1]),
                    branch_name=Subquery(placement.values('branch__name_en')[:1]),
                    joined=Coalesce('join_date', TruncDate('created_at', tzinfo=NPT)),
                    exited=Case(When(exit_date__isnull=False, then=F('exit_date')),
                                When(status=Employee.Status.EXITED, then=TruncDate('updated_at', tzinfo=NPT)),
                                default=None, output_field=DateField())))
    if f.get('branchId'):
        qs = qs.filter(placed_branch=f['branchId'])
    if f.get('orgUnitId'):
        qs = qs.filter(placed_unit__in=_units_under(c, f['orgUnitId']))
    return qs


def _attendance_by_month(c: str, people, months: list[dict]) -> dict[str, dict]:
    newer = AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                         version__gt=OuterRef('version'))
    qs = (AttendanceDay.objects.for_company(c)
          .filter(employee__in=people, local_date__range=(months[0]['startAd'], months[-1]['endAd']))
          .exclude(Exists(newer))
          .annotate(month=_month_of('local_date', months)).order_by().values('month')
          .annotate(scheduled=Count('id', filter=Q(status__in=SCHEDULED)),
                    present=Count('id', filter=Q(status__in=WORKED)),
                    absent=Count('id', filter=Q(status=A.ABSENT)),
                    late=Count('id', filter=Q(late_min__gt=0)),
                    ot_min=Sum('ot_min'),
                    avg_worked=Avg('worked_min', filter=Q(worked_min__gt=0))))
    return {r['month']: r for r in qs}


def _approved_ot_by_month(c: str, people, months: list[dict]) -> dict[str, int]:
    qs = (OtSlip.objects.for_company(c)
          .filter(employee__in=people, status__in=OT_COUNTED,
                  local_date__range=(months[0]['startAd'], months[-1]['endAd']))
          .annotate(month=_month_of('local_date', months)).order_by().values('month')
          .annotate(minutes=Sum('approved_min')))
    return {r['month']: r['minutes'] or 0 for r in qs}


def _leave_taken(c: str, people, months: list[dict]):
    return (LeaveLedger.objects.for_company(c)
            .filter(employee__in=people, reason__in=LEAVE_TAKEN,
                    effective_date__range=(months[0]['startAd'], months[-1]['endAd'])))


def _final_payslips(c: str, people, months: list[dict]):
    in_range = Q()
    for m in months:
        y, mo = m['key'].split('-')
        in_range |= Q(run__period__bs_year=int(y), run__period__bs_month=int(mo))
    return (Payslip.objects.for_company(c)
            .filter(in_range, run__in=PayrollRun.objects.for_company(c).filter(status__in=PAY_FINAL), employee__in=people))


# ---------------------------------------------------------------- the computation

def compute_insights(f: dict) -> dict:
    """Everything the Insights page shows, for one company, filter and BS month range."""
    months = months_between(f.get('from'), f.get('to'), f['today'])
    first, last = months[0], months[-1]
    c = f['companyId']
    people_qs = _employees(f)
    people = people_qs.values('pk')
    emps = []
    for e in people_qs.annotate(exit_kind=exit_reason_source()).values('id', 'gender', 'dob', 'joined', 'exited', 'status', 'unit_name', 'branch_name', 'exit_kind'):
        emps.append({'id': str(e['id']), 'gender': e['gender'], 'dob': _iso(e['dob']), 'joined': _iso(e['joined']),
                     'exited': _iso(e['exited']), 'status': e['status'], 'unit': e['unit_name'],
                     'branch': e['branch_name'], 'exit_kind': e['exit_kind']})

    def active_at(ad: str) -> list[dict]:
        return [e for e in emps if e['joined'] <= ad and (not e['exited'] or e['exited'] > ad)]

    def before(ad: str) -> str:
        return (date.fromisoformat(ad) - timedelta(days=1)).isoformat()

    headcount = []
    for m in months:
        start = len(active_at(before(m['startAd'])))
        end = len(active_at(m['endAd']))
        joins = sum(1 for e in emps if m['startAd'] <= e['joined'] <= m['endAd'])
        exit_rows = [e for e in emps if e['exited'] and m['startAd'] <= e['exited'] <= m['endAd']]
        avg_hc = (start + end) / 2
        headcount.append({
            'month': m['key'], 'label': m['label'], 'short': m['short'], 'headcount': end, 'joins': joins,
            'exits': len(exit_rows), 'net': joins - len(exit_rows),
            'attritionPct': round_half_up(len(exit_rows) / avg_hc * 100, 2) if avg_hc > 0 else 0,
            'voluntary': sum(1 for e in exit_rows if classify_exit(e['exit_kind']) == 'voluntary'),
            'involuntary': sum(1 for e in exit_rows if classify_exit(e['exit_kind']) == 'involuntary'),
        })
    total_exits = sum(h['exits'] for h in headcount)
    avg_monthly = sum(h['attritionPct'] for h in headcount) / len(headcount)
    with_reason = [e for e in emps if e['exited'] and first['startAd'] <= e['exited'] <= last['endAd'] and classify_exit(e['exit_kind'])]
    attrition = {
        'exits': total_exits, 'monthlyAvgPct': round_half_up(avg_monthly, 2), 'annualisedPct': round_half_up(avg_monthly * 12, 1),
        'split': {
            'voluntary': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'voluntary'),
            'involuntary': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'involuntary'),
            'other': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'other'),
        } if with_reason else None,
    }

    as_of = last['endAd'] if last['endAd'] < f['today'] else f['today']
    current = active_at(as_of)

    def tenure_band(e):
        y = _years_between(e['joined'], as_of)
        return 'Under 1 year' if y < 1 else '1–2 years' if y < 2 else '2–5 years' if y < 5 else '5–10 years' if y < 10 else '10+ years'

    def age_band(e):
        if not e['dob']:
            return 'Not recorded'
        y = _years_between(e['dob'], as_of)
        return 'Under 25' if y < 25 else '25–34' if y < 35 else '35–44' if y < 45 else '45–54' if y < 55 else '55 and over'

    G = Employee.Gender
    tenure = _bands(current, ['Under 1 year', '1–2 years', '2–5 years', '5–10 years', '10+ years'], tenure_band)
    age = _bands(current, ['Under 25', '25–34', '35–44', '45–54', '55 and over', 'Not recorded'], age_band)
    gender = [g for g in _bands(current, ['Female', 'Male', 'Other', 'Not recorded'],
                                lambda e: {G.FEMALE: 'Female', G.MALE: 'Male', G.OTHER: 'Other'}.get(e['gender'], 'Not recorded'))
              if g['count'] > 0 or g['label'] != 'Other']

    def group_count(pick, none: str) -> list[dict]:
        m: dict[str, int] = {}
        for e in current:
            k = pick(e) or none
            m[k] = m.get(k, 0) + 1
        # Most people first, then by name in the browser's collation order.
        return sorted(({'label': k, 'count': v} for k, v in m.items()), key=lambda x: (-x['count'], collate_key(x['label'])))

    departments = group_count(lambda e: e['unit'], 'No department')
    branches = group_count(lambda e: e['branch'], 'No branch')

    att_by = _attendance_by_month(c, people, months)
    ot_by = _approved_ot_by_month(c, people, months)
    attendance = []
    for m in months:
        a = att_by.get(m['key'])
        scheduled = a['scheduled'] if a else 0
        present = a['present'] if a else 0
        absent = a['absent'] if a else 0
        late = a['late'] if a else 0
        attendance.append({
            'month': m['key'], 'label': m['label'], 'short': m['short'], 'scheduledDays': scheduled, 'presentDays': present,
            'absentDays': absent, 'lateDays': late,
            'absenteeismPct': round_half_up(absent / scheduled * 100, 1) if scheduled else None,
            'latePct': round_half_up(late / present * 100, 1) if present else None,
            'avgWorkedHours': round_half_up(float(a['avg_worked']) / 60, 2) if a and a['avg_worked'] else None,
        })
    overtime = [{'month': m['key'], 'label': m['label'], 'short': m['short'],
                 'detectedHours': round_half_up(((att_by.get(m['key']) or {}).get('ot_min') or 0) / 60, 1),
                 'approvedHours': round_half_up((ot_by.get(m['key']) or 0) / 60, 1)} for m in months]

    taken = _leave_taken(c, people, months)
    leave_q = (taken.filter(leave_type__company_id=c).order_by()
               .values('leave_type__name_en', 'leave_type__code', 'leave_type__sort_order')
               .annotate(days=-Sum('delta_days')).order_by('leave_type__sort_order'))
    avg_headcount = sum(h['headcount'] for h in headcount) / len(headcount)
    leave = [x for x in ({'type': r['leave_type__name_en'], 'code': r['leave_type__code'], 'days': round_half_up(_f(r['days']), 1),
                          'perEmployee': round_half_up(_f(r['days']) / avg_headcount, 2) if avg_headcount else 0} for r in leave_q)
             if x['days'] > 0]
    leave_by = {r['month']: -_f(r['days']) for r in
                taken.annotate(month=_month_of('effective_date', months)).order_by().values('month')
                .annotate(days=Sum('delta_days'))}

    slips = _final_payslips(c, people, months)
    cost_by = {f"{r['run__period__bs_year']}-{r['run__period__bs_month']:02d}": r for r in
               slips.order_by().values('run__period__bs_year', 'run__period__bs_month')
               .annotate(gross=Sum('gross'), employer=Sum('employer_statutory'), net=Sum('net'), tds=Sum('tds'),
                         people=Count('employee_id', distinct=True))}
    cost = []
    for m in months:
        r = cost_by.get(m['key'])
        gross = _f(r['gross']) if r else 0.0
        employer = _f(r['employer']) if r else 0.0
        cost.append({'month': m['key'], 'label': m['label'], 'short': m['short'], 'gross': round_half_up(gross, 2),
                     'employerContributions': round_half_up(employer, 2), 'totalCost': round_half_up(gross + employer, 2),
                     'net': round_half_up(_f(r['net']) if r else 0, 2), 'tds': round_half_up(_f(r['tds']) if r else 0, 2),
                     'people': r['people'] if r else 0, 'locked': bool(r)})
    unit_of = {e['id']: e['unit'] or 'No department' for e in emps}
    dept_cost: dict[str, float] = {}
    for r in slips.order_by().values('employee_id').annotate(gross=Sum('gross'), employer=Sum('employer_statutory')):
        k = unit_of.get(str(r['employee_id']), 'No department')
        dept_cost[k] = dept_cost.get(k, 0) + _f(r['gross']) + _f(r['employer'])
    cost_by_department = sorted(({'label': k, 'totalCost': round_half_up(v, 2)} for k, v in dept_cost.items()),
                                key=lambda x: -x['totalCost'])

    # Headline tiles: the last month of the range against the one before it.
    last_h = headcount[-1]
    prev_h = headcount[-2] if len(headcount) > 1 else None
    # Pay is only final once a run is locked: compare the latest locked month with the locked month before it.
    locked_idx = [i for i, x in enumerate(cost) if x['locked']]
    last_c = cost[locked_idx[-1]] if locked_idx else None
    prev_c = cost[locked_idx[-2]] if len(locked_idx) > 1 else None
    last_a = attendance[-1]
    prev_a = attendance[-2] if len(attendance) > 1 else None

    def delta(a, b):
        return None if a is None or b is None else round_half_up(a - b, 2)

    leave_days = leave_by.get(last['key'], 0)
    prev_leave = leave_by.get(months[-2]['key'], 0) if prev_h else None
    g = lambda d, k: d[k] if d else None  # noqa: E731
    cost_kpi = {'key': 'cost', 'label': f"People cost, {last_c['label'].split(' ')[0]}" if last_c else 'People cost',
                'value': last_c['totalCost'] if last_c else None,
                'delta': delta(last_c['totalCost'], prev_c['totalCost']) if last_c and prev_c else None,
                'unit': 'NPR', 'goodWhen': 'neutral'}
    if prev_c:
        cost_kpi['vs'] = prev_c['label'].split(' ')[0]
    kpis = [
        {'key': 'headcount', 'label': 'Headcount', 'value': last_h['headcount'], 'delta': delta(last_h['headcount'], g(prev_h, 'headcount')), 'unit': '', 'goodWhen': 'neutral'},
        {'key': 'joins', 'label': 'Joined', 'value': last_h['joins'], 'delta': delta(last_h['joins'], g(prev_h, 'joins')), 'unit': '', 'goodWhen': 'neutral'},
        {'key': 'exits', 'label': 'Left', 'value': last_h['exits'], 'delta': delta(last_h['exits'], g(prev_h, 'exits')), 'unit': '', 'goodWhen': 'down'},
        {'key': 'attrition', 'label': 'Attrition a year', 'value': attrition['annualisedPct'], 'delta': None, 'unit': '%', 'goodWhen': 'down'},
        cost_kpi,
        {'key': 'absenteeism', 'label': 'Absenteeism', 'value': last_a['absenteeismPct'], 'delta': delta(last_a['absenteeismPct'], g(prev_a, 'absenteeismPct')), 'unit': '%', 'goodWhen': 'down'},
        {'key': 'late', 'label': 'Late arrivals', 'value': last_a['latePct'], 'delta': delta(last_a['latePct'], g(prev_a, 'latePct')), 'unit': '%', 'goodWhen': 'down'},
        {'key': 'hours', 'label': 'Average day', 'value': last_a['avgWorkedHours'], 'delta': delta(last_a['avgWorkedHours'], g(prev_a, 'avgWorkedHours')), 'unit': 'h', 'goodWhen': 'neutral'},
        {'key': 'leave', 'label': 'Leave taken', 'value': round_half_up(leave_days, 1), 'delta': delta(leave_days, prev_leave), 'unit': 'days', 'goodWhen': 'neutral'},
    ]

    return {
        'months': months, 'asOf': as_of, 'kpis': kpis, 'headcount': headcount, 'attrition': attrition, 'tenure': tenure,
        'age': age, 'gender': gender, 'departments': departments, 'branches': branches, 'cost': cost,
        'costByDepartment': cost_by_department, 'overtime': overtime, 'leave': leave,
        'leaveMonthly': [{'month': m['key'], 'label': m['label'], 'short': m['short'], 'days': round_half_up(leave_by.get(m['key'], 0), 1)}
                         for m in months],
        # Exit reasons always have a source now: the employee exit records (or an install's own column).
        'attendance': attendance, 'exitReasonsAvailable': True,
    }


def insights(user: AuthUser, query: dict) -> dict:
    """GET /insights: the filters checked against the company, then the computation."""
    return compute_insights(parse_filters(user, query))


# ---------------------------------------------------------------- CSV datasets

def _c(*pairs) -> list[dict]:
    return [{'key': k, 'header': h} for k, h in pairs]


DATASETS: dict[str, dict] = {
    'headcount': {'file': 'headcount', 'rows': lambda i: i['headcount'], 'columns': _c(
        ('label', 'Month'), ('headcount', 'Headcount at month end'), ('joins', 'Joined'), ('exits', 'Left'), ('net', 'Net change'),
        ('attritionPct', 'Attrition %'), ('voluntary', 'Voluntary exits'), ('involuntary', 'Involuntary exits'))},
    'tenure': {'file': 'tenure', 'rows': lambda i: i['tenure'], 'columns': _c(('label', 'Tenure'), ('count', 'Employees'))},
    'age': {'file': 'age', 'rows': lambda i: i['age'], 'columns': _c(('label', 'Age'), ('count', 'Employees'))},
    'gender': {'file': 'gender', 'rows': lambda i: i['gender'], 'columns': _c(('label', 'Gender'), ('count', 'Employees'))},
    'departments': {'file': 'departments', 'rows': lambda i: i['departments'], 'columns': _c(('label', 'Department'), ('count', 'Employees'))},
    'branches': {'file': 'branches', 'rows': lambda i: i['branches'], 'columns': _c(('label', 'Branch'), ('count', 'Employees'))},
    'cost': {'file': 'people-cost', 'rows': lambda i: i['cost'], 'columns': _c(
        ('label', 'Month'), ('gross', 'Gross pay'), ('employerContributions', 'Employer SSF/PF/gratuity'), ('totalCost', 'Total cost'),
        ('net', 'Net pay'), ('tds', 'TDS'), ('people', 'People paid'))},
    'costByDepartment': {'file': 'people-cost-by-department', 'rows': lambda i: i['costByDepartment'],
                         'columns': _c(('label', 'Department'), ('totalCost', 'Total cost'))},
    'overtime': {'file': 'overtime', 'rows': lambda i: i['overtime'], 'columns': _c(
        ('label', 'Month'), ('detectedHours', 'Overtime detected (h)'), ('approvedHours', 'Overtime approved (h)'))},
    'leave': {'file': 'leave-by-type', 'rows': lambda i: i['leave'], 'columns': _c(
        ('type', 'Leave type'), ('days', 'Days taken'), ('perEmployee', 'Days per employee'))},
    'attendance': {'file': 'attendance', 'rows': lambda i: i['attendance'], 'columns': _c(
        ('label', 'Month'), ('scheduledDays', 'Scheduled days'), ('presentDays', 'Days worked'), ('absentDays', 'Absent days'),
        ('absenteeismPct', 'Absenteeism %'), ('lateDays', 'Late days'), ('latePct', 'Late %'), ('avgWorkedHours', 'Average hours per day'))},
}


def export_dataset(user: AuthUser, name: str, query: dict) -> tuple[str, list[dict], list[dict]]:
    """GET /insights/export/:dataset -> (filename, columns, rows). The dataset name is checked before the filters."""
    _require(user)
    ds = DATASETS.get(name) if name in DATASETS else None
    if not ds:
        raise BadRequestError(f'unknown dataset; use one of {", ".join(DATASETS)}')
    data = insights(user, query)
    return f'insights-{ds["file"]}', ds['columns'], ds['rows'](data)
