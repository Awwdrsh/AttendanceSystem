"""
Group (holding company) views: one dashboard, one directory and consolidated reports over the companies of a group.

These are the only services that read across companies, and only across `group_scope(user)`: the companies of the
person's ACTIVE group where they are admin, HR or executive (directly, by membership or through a group role). Every
query takes that list of ids (`Model.objects.for_companies(ids)`) and groups by company in SQL, so the number of
queries does not grow with the number of companies.

Pay figures: HR, payroll admins and admins of a company always see its pay; a person who is only an executive there
sees it unless the company hides pay from executives (company setting 'group.policy' {hidePayFromExecutives}).

Nothing here writes, except `set_pay_policy`, which changes the ACTIVE company's own setting (its admin only).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import (
    BooleanField, Case, Count, DateField, DecimalField, Exists, ExpressionWrapper, F, Func, IntegerField, OuterRef, Q,
    Subquery, Sum, TextField, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate

from engines.bs_calendar import (
    BS_MONTH_NAMES_EN, BsRangeError, MAX_YEAR, MIN_YEAR, ad_to_bs, bs_month_bounds, fiscal_year,
)
from engines.bs_calendar.data import FISCAL_YEAR_START_MONTH

from hajiri.attendance.repo import latest_days
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.collation import collate_key
from hajiri.core.companies import CompanyAccess, my_companies
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.core.settings_store import get_setting, put_setting, setting_subquery
from hajiri.db.models import (
    AttendanceDay, Company, Device, Employee, EmployeeAssignment, EmployeeDocument, LeaveLedger, LeaveRequest,
    OtSlip, PayrollRun, Payslip, WorkflowInstance,
)
from hajiri.devices.constants import PSEUDO_VENDORS
from hajiri.devices.services import offline_q
from hajiri.reports.csv import Column, Table

NPT = ZoneInfo('Asia/Kathmandu')

# Roles in a company that let a person see its company-wide figures in group views (admin always may).
VIEW_ROLES = ('hr', 'executive')
# Roles that always see a company's pay figures (admin always does). Executives see them unless hidden.
PAY_ROLES = ('hr', 'payroll_admin')
POLICY_KEY = 'group.policy'
HIDE_PAY = 'hidePayFromExecutives'
FORBIDDEN = 'requires role group_admin, group_hr or group_executive'

A = AttendanceDay.Status
ATTENDED = (A.PRESENT, A.HALF_DAY, A.HALF_DAY_LEAVE, A.WFH, A.INCOMPLETE)     # "in" (the Overview's present tile)
WORKED = (A.PRESENT, A.HALF_DAY, A.WFH, A.INCOMPLETE)
SCHEDULED = (A.PRESENT, A.HALF_DAY, A.ABSENT, A.WFH, A.INCOMPLETE, A.ON_LEAVE, A.HALF_DAY_LEAVE)
ON_LEAVE = (A.ON_LEAVE, A.HALF_DAY_LEAVE)
OT_COUNTED = (OtSlip.Status.APPROVED, OtSlip.Status.POSTED)
LEAVE_TAKEN = (LeaveLedger.Reason.USAGE, LeaveLedger.Reason.CANCELLATION)
# Pay comes from locked (or later) runs only: drafts can still change.
PAY_FINAL = (PayrollRun.Status.LOCKED, PayrollRun.Status.DISBURSED, PayrollRun.Status.FILED)
EXPIRY_DAYS = 30
PROBATION_DAYS = 30
SHORT_MONTHS = ['Bai', 'Jes', 'Asr', 'Shr', 'Bha', 'Asw', 'Kar', 'Man', 'Pou', 'Mag', 'Fal', 'Cha']
DEC = DecimalField(max_digits=16, decimal_places=2)


# ================================================================ scope

@dataclass(frozen=True)
class GroupScope:
    """The companies a person may see in group views, and which of them show pay figures to them."""
    group_id: str
    group_name: str | None
    companies: tuple[CompanyAccess, ...]
    active_company_id: str | None = None
    _hidden: frozenset[str] = field(default=frozenset())

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.companies]

    def company(self, company_id: str) -> CompanyAccess | None:
        return next((c for c in self.companies if c.id == str(company_id)), None)

    def sees_pay(self, company_id: str) -> bool:
        c = self.company(company_id)
        return bool(c) and (c.has_role(*PAY_ROLES) or str(company_id) not in self._hidden)

    @property
    def pay_ids(self) -> list[str]:
        return [c.id for c in self.companies if self.sees_pay(c.id)]


def _visible(user: AuthUser) -> list[CompanyAccess]:
    if not user.group_id:
        return []
    cs = [c for c in my_companies(user) if c.group_id == user.group_id and c.has_role(*VIEW_ROLES)]
    return sorted(cs, key=lambda c: collate_key(c.name))


def can_view(user: AuthUser) -> bool:
    """Group views are for people with a group role, or who may see figures of at least two companies of the group."""
    cs = _visible(user)
    return bool(cs) and (bool(user.group_roles) or len(cs) >= 2)


def group_scope(user: AuthUser) -> GroupScope:
    """ForbiddenError unless the person may use group views (can_view)."""
    cs = _visible(user)
    if not cs or not (user.group_roles or len(cs) >= 2):
        raise ForbiddenError(FORBIDDEN)
    hidden = frozenset(str(c) for c in Company.objects.filter(pk__in=[c.id for c in cs])
                       .annotate(h=setting_subquery(POLICY_KEY, HIDE_PAY, company_ref=OuterRef('pk')))
                       .filter(h='true').values_list('pk', flat=True))
    return GroupScope(user.group_id, cs[0].group_name, tuple(cs), user.company_id, hidden)


def narrow(scope: GroupScope, company_id: str | None) -> list[str]:
    """One company of the scope (a filter), or all of them. A company outside the scope is 'company not found'."""
    if not company_id:
        return scope.ids
    if not scope.company(company_id):
        from hajiri.core.errors import NotFoundError
        raise NotFoundError('company not found')
    return [str(company_id)]


# ---------------------------------------------------------------- pay policy (the active company's own setting)

def pay_policy(company_id: str) -> dict:
    v = get_setting(company_id, POLICY_KEY, {}) or {}
    return {HIDE_PAY: bool(v.get(HIDE_PAY))}


def set_pay_policy(user: AuthUser, hide: bool) -> dict:
    """Admins of a company decide whether its executives see pay figures in group views."""
    if not has_role(user, 'admin'):
        raise ForbiddenError('requires role admin')
    cid = company_id_of(user)
    before = pay_policy(cid)
    after = {**(get_setting(cid, POLICY_KEY, {}) or {}), HIDE_PAY: bool(hide)}
    with transaction.atomic():
        put_setting(cid, POLICY_KEY, after, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=cid, before={POLICY_KEY: before},
              after={POLICY_KEY: {HIDE_PAY: bool(hide)}})
    return {HIDE_PAY: bool(hide)}


# ================================================================ BS months

@dataclass(frozen=True)
class Month:
    year: int
    month: int
    start: date
    end: date

    @property
    def key(self) -> str:
        return f'{self.year}-{self.month:02d}'

    @property
    def label(self) -> str:
        return f'{BS_MONTH_NAMES_EN[self.month - 1]} {self.year}'

    @property
    def short(self) -> str:
        return f'{SHORT_MONTHS[self.month - 1]} {str(self.year)[-2:]}'


def month(y: int, m: int) -> Month:
    try:
        b = bs_month_bounds(y, m)
    except (BsRangeError, ValueError, KeyError):
        raise BadRequestError('that month is outside the supported calendar')
    return Month(y, m, date.fromisoformat(str(b['startAd'])), date.fromisoformat(str(b['endAd'])))


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _next(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def current_month(today: date) -> Month:
    bs = ad_to_bs(today)
    return month(bs.year, bs.month)


def last_months(today: date, n: int = 12) -> list[Month]:
    """The n BS months up to and including this one, oldest first."""
    bs = ad_to_bs(today)
    ym, out = (bs.year, bs.month), []
    for _ in range(n):
        out.append(month(*ym))
        ym = _prev(*ym)
    return out[::-1]


def fiscal_months(start_year: int, today: date | None = None) -> list[Month]:
    """Shrawan to Ashadh of a fiscal year; a running year stops at this month."""
    fiscal_year(start_year)                     # raises for a year outside the table
    ym, out = (start_year, FISCAL_YEAR_START_MONTH), []
    for _ in range(12):
        m = month(*ym)
        if today and m.start > today:
            break
        out.append(m)
        ym = _next(*ym)
    return out


def fiscal_label(start_year: int) -> str:
    return f'{start_year}/{str(start_year + 1)[-2:]}'


def current_fiscal_start(today: date) -> int:
    bs = ad_to_bs(today)
    return bs.year if bs.month >= FISCAL_YEAR_START_MONTH else bs.year - 1


def _month_of(field_name: str, months: list[Month]) -> Case:
    """The BS month key a date column falls in, for grouping by month in SQL."""
    return Case(*[When(**{f'{field_name}__range': (m.start, m.end)}, then=Value(m.key)) for m in months],
                default=Value(None), output_field=TextField())


# ================================================================ shared expressions

def _joined():
    return Coalesce('join_date', TruncDate('created_at', tzinfo=NPT))


def _exited():
    return Case(When(exit_date__isnull=False, then=F('exit_date')),
                When(status=Employee.Status.EXITED, then=TruncDate('updated_at', tzinfo=NPT)),
                default=None, output_field=DateField())


def employees(ids: list[str]):
    """Employees of these companies with `joined` and `exited` dates (the Insights definitions)."""
    return Employee.objects.for_companies(ids).annotate(joined=_joined(), exited=_exited())


def _active_at(d: date) -> Q:
    """Employed on day d."""
    return Q(joined__lte=d) & (Q(exited__isnull=True) | Q(exited__gt=d))


def _placement(today: date):
    """The assignment valid today, else the latest one (correlated to the outer employee)."""
    return (EmployeeAssignment.objects.filter(employee=OuterRef('pk'))
            .order_by(ExpressionWrapper(Q(valid__contains=today), output_field=BooleanField()).desc(),
                      Func(F('valid'), function='lower', output_field=DateField()).desc()))


def placed_employees(ids: list[str], today: date):
    p = _placement(today)
    return (employees(ids)
            .annotate(company_name=F('company__name_en'), company_code=F('company__code'),
                      branch_name=Subquery(p.values('branch__name_en')[:1]),
                      department=Subquery(p.values('org_unit__name_en')[:1]),
                      employment_type=Subquery(p.values('employment_type')[:1])))


def _pct(part, whole, places: int = 1) -> float | None:
    if not whole:
        return None
    q = Decimal(1).scaleb(-places)
    return float((Decimal(part) * 100 / Decimal(whole)).quantize(q, rounding='ROUND_HALF_UP'))


def _attrition(exits: int, start: int, end: int) -> float:
    avg = (start + end) / 2
    return (_pct(exits, avg, 2) or 0.0) if avg > 0 else 0.0


def _money(v) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal('0.01')))


# ================================================================ dashboard

def _cache_seconds() -> int:
    return int(getattr(settings, 'GROUP_DASHBOARD_CACHE_SECONDS', 60))


def _cache_key(ids: list[str], today: date) -> str:
    h = hashlib.sha256(','.join(sorted(ids)).encode()).hexdigest()[:32]
    return f'hajiri:group:dashboard:{today.isoformat()}:{h}'


def figures(ids: list[str], today: date) -> dict:
    """Every company's figures for the dashboard, in a fixed number of queries (one per kind of figure, each grouped
    by company). Pay figures are all here; `dashboard` removes the ones the viewer may not see."""
    this = current_month(today)
    prev = month(*_prev(this.year, this.month))
    trend = last_months(today, 12)
    soon = today + timedelta(days=EXPIRY_DAYS)

    # 1. People: headcount, joiners and leavers this month, probation reviews due, and the attrition trend.
    emp_aggs: dict[str, Any] = {
        'headcount': Count('id', filter=_active_at(today)),      # employed today, as the reports and Insights count
        'joiners': Count('id', filter=Q(join_date__range=(this.start, this.end))),
        'leavers': Count('id', filter=Q(exited__range=(this.start, this.end))),
        'probation_due': Count('id', filter=Q(status=Employee.Status.ACTIVE, probation_end__isnull=False,
                                              confirmed_on__isnull=True,
                                              probation_end__lte=today + timedelta(days=PROBATION_DAYS))
                               & (Q(exit_date__isnull=True) | Q(exit_date__gte=today))),
    }
    for i, m in enumerate(trend):
        emp_aggs[f's{i}'] = Count('id', filter=_active_at(m.start - timedelta(days=1)))
        emp_aggs[f'e{i}'] = Count('id', filter=_active_at(m.end))
        emp_aggs[f'x{i}'] = Count('id', filter=Q(exited__range=(m.start, m.end)))
    people = {str(r['company_id']): r for r in employees(ids).order_by().values('company_id').annotate(**emp_aggs)}

    # 2. Attendance: today's in / late / absent / on leave, and this month's open exceptions (latest day versions).
    on_today = Q(local_date=today)
    att = {str(r['employee__company_id']): r for r in (
        latest_days().for_companies(ids).filter(local_date__range=(this.start, today))
        .order_by().values('employee__company_id')
        .annotate(present=Count('id', filter=on_today & Q(status__in=ATTENDED)),
                  late=Count('id', filter=on_today & Q(late_min__gt=0)),
                  absent=Count('id', filter=on_today & Q(status=A.ABSENT)),
                  exceptions=Count('id', filter=Q(locked_at__isnull=True) & (
                      Q(status__in=(A.INCOMPLETE, A.ABSENT)) | Q(flags__contains=[{'kind': 'OTPreapprovalMissing'}])
                      | Q(flags__contains=[{'kind': 'OTCapExceeded'}])))))}

    # 3. Approvals waiting.
    approvals = dict((str(c), n) for c, n in (
        WorkflowInstance.objects.for_companies(ids).filter(state=WorkflowInstance.State.PENDING)
        .order_by().values('company_id').annotate(n=Count('id')).values_list('company_id', 'n')))

    # 4. People on approved leave today.
    leave = dict((str(c), n) for c, n in (
        LeaveRequest.objects.for_companies(ids)
        .filter(status=LeaveRequest.Status.APPROVED, from_date__lte=today, to_date__gte=today)
        .order_by().values('employee__company_id').annotate(n=Count('employee', distinct=True))
        .values_list('employee__company_id', 'n')))

    # 5. Overtime this month (slips not rejected; approved minutes where reviewed).
    ot = {str(r['employee__company_id']): r for r in (
        OtSlip.objects.for_companies(ids).filter(local_date__range=(this.start, today))
        .exclude(status=OtSlip.Status.REJECTED).order_by().values('employee__company_id')
        .annotate(minutes=Coalesce(Sum(Coalesce('approved_min', 'detected_min')), 0),
                  awaiting=Count('id', filter=Q(status=OtSlip.Status.DETECTED))))}

    # 6. Offline clock-in devices.
    offline = dict((str(c), n) for c, n in (
        Device.objects.for_companies(ids).filter(status=Device.Status.ACTIVE).exclude(vendor__in=PSEUDO_VENDORS)
        .filter(offline_q()).order_by().values('company_id').annotate(n=Count('id')).values_list('company_id', 'n')))

    # 7. Employee documents expired or expiring within 30 days.
    docs = dict((str(c), n) for c, n in (
        EmployeeDocument.objects.for_companies(ids)
        .filter(deleted_at__isnull=True, is_current=True, employee__status=Employee.Status.ACTIVE,
                expiry_date__isnull=False, expiry_date__lte=soon)
        .exclude(status=EmployeeDocument.Status.REJECTED)
        .order_by().values('company_id').annotate(n=Count('id')).values_list('company_id', 'n')))

    # 8. Companies with their latest locked regular run (and its payslip totals) and last month's run.
    final = (PayrollRun.objects.filter(company=OuterRef('pk'), run_type=PayrollRun.RunType.REGULAR, status__in=PAY_FINAL)
             .order_by('-period__bs_year', '-period__bs_month', '-created_at'))
    slips = Payslip.objects.filter(run=OuterRef('run_id')).order_by().values('run')

    def slip_sum(f):
        return Subquery(slips.annotate(s=Sum(f)).values('s'), output_field=DEC)
    last_month_run = PayrollRun.objects.filter(company=OuterRef('pk'), run_type=PayrollRun.RunType.REGULAR,
                                               period__bs_year=prev.year, period__bs_month=prev.month)
    companies = (Company.objects.filter(pk__in=ids)
                 .annotate(run_id=Subquery(final.values('id')[:1]))
                 .annotate(run_year=Subquery(final.values('period__bs_year')[:1]),
                           run_month=Subquery(final.values('period__bs_month')[:1]),
                           run_status=Subquery(final.values('status')[:1]),
                           gross=slip_sum('gross'), net=slip_sum('net'), employer=slip_sum('employer_statutory'),
                           slips=Subquery(slips.annotate(n=Count('id')).values('n'), output_field=IntegerField()),
                           prev_run=Exists(last_month_run))
                 .values('id', 'code', 'name_en', 'run_id', 'run_year', 'run_month', 'run_status', 'gross', 'net',
                         'employer', 'slips', 'prev_run'))

    rows = []
    for c in companies:
        cid = str(c['id'])
        p, a, o = people.get(cid, {}), att.get(cid, {}), ot.get(cid, {})
        payroll = None
        if c['run_id']:
            gross, employer = c['gross'] or Decimal(0), c['employer'] or Decimal(0)
            payroll = {'runId': str(c['run_id']), 'bsYear': c['run_year'], 'bsMonth': c['run_month'],
                       'status': c['run_status'], 'payslips': c['slips'] or 0, 'gross': _money(gross),
                       'net': _money(c['net'] or 0), 'employer': _money(employer), 'cost': _money(gross + employer)}
        rows.append({
            'id': cid, 'code': c['code'], 'name': c['name_en'],
            'headcount': p.get('headcount', 0), 'joiners': p.get('joiners', 0), 'leavers': p.get('leavers', 0),
            'present': a.get('present', 0), 'late': a.get('late', 0), 'absent': a.get('absent', 0),
            'onLeave': leave.get(cid, 0), 'exceptions': a.get('exceptions', 0),
            'pendingApprovals': approvals.get(cid, 0), 'otMinutes': o.get('minutes', 0),
            'otAwaiting': o.get('awaiting', 0), 'payroll': payroll,
            'attention': {'devicesOffline': offline.get(cid, 0), 'documentsExpiring': docs.get(cid, 0),
                          'probationDue': p.get('probation_due', 0),
                          'payrollNotRun': bool(p.get('headcount')) and not c['prev_run']},
            'trend': [{'start': p.get(f's{i}', 0), 'end': p.get(f'e{i}', 0), 'exits': p.get(f'x{i}', 0)}
                      for i in range(len(trend))],
        })
    rows.sort(key=lambda r: collate_key(r['name']))
    return {'date': today.isoformat(), 'month': {'key': this.key, 'label': this.label},
            'previousMonth': {'key': prev.key, 'label': prev.label},
            'months': [{'key': m.key, 'label': m.label, 'short': m.short} for m in trend], 'companies': rows}


def _cached_figures(ids: list[str], today: date, fresh: bool) -> dict:
    seconds = _cache_seconds()
    if seconds <= 0:
        return figures(ids, today)
    key = _cache_key(ids, today)
    if not fresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    data = figures(ids, today)
    cache.set(key, data, seconds)
    return data


SUM_KEYS = ('headcount', 'joiners', 'leavers', 'present', 'late', 'absent', 'onLeave', 'exceptions',
            'pendingApprovals', 'otMinutes', 'otAwaiting')


def _attention(rows: list[dict], prev_label: str) -> list[dict]:
    """The needs-attention list: each item names its company and the page to open there (after switching to it)."""
    out = []

    def plural(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"
    for r in rows:
        a = r['attention']
        items = []
        if a['devicesOffline']:
            items.append({'kind': 'devices_offline', 'icon': 'device-desktop-off', 'tone': 'bad', 'count': a['devicesOffline'],
                          'label': f"{plural(a['devicesOffline'], 'clock-in device')} offline", 'next': '/settings/devices'})
        if a['payrollNotRun']:
            items.append({'kind': 'payroll_not_run', 'icon': 'cash-off', 'tone': 'warn', 'count': None,
                          'label': f'Payroll for {prev_label} not run', 'next': '/payroll'})
        if a['documentsExpiring']:
            items.append({'kind': 'documents_expiring', 'icon': 'file-alert', 'tone': 'warn', 'count': a['documentsExpiring'],
                          'label': f"{plural(a['documentsExpiring'], 'employee document')} expired or expiring within "
                                   f'{EXPIRY_DAYS} days', 'next': '/employees/documents'})
        if a['probationDue']:
            items.append({'kind': 'probation_due', 'icon': 'user-check', 'tone': 'info', 'count': a['probationDue'],
                          'label': f"{plural(a['probationDue'], 'probation review')} due within {PROBATION_DAYS} days",
                          'next': '/employees/onboarding?tab=probation'})
        if r['pendingApprovals']:
            items.append({'kind': 'approvals', 'icon': 'list-check', 'tone': 'info', 'count': r['pendingApprovals'],
                          'label': f"{plural(r['pendingApprovals'], 'approval')} waiting", 'next': '/approvals'})
        out.extend({**i, 'companyId': r['id'], 'companyName': r['name'], 'companyCode': r['code'],
                    'active': r['active']} for i in items)
    return out


def dashboard(user: AuthUser, today: date | None = None, *, fresh: bool = False) -> dict:
    """The /group dashboard for this person: per company and totals, pay hidden where they may not see it."""
    scope = group_scope(user)
    today = today or npt_today()
    data = _cached_figures(scope.ids, today, fresh)
    rows = []
    for r in data['companies']:
        r = {**r, 'active': r['id'] == scope.active_company_id, 'paySeen': scope.sees_pay(r['id'])}
        if not r['paySeen']:
            r['payroll'] = None
        rows.append(r)
    totals = {k: sum(r[k] for r in rows) for k in SUM_KEYS}
    paid = [r['payroll'] for r in rows if r['payroll']]
    totals['payrollCost'] = _money(sum(Decimal(p['cost']) for p in paid)) if paid else None
    totals['payrollGross'] = _money(sum(Decimal(p['gross']) for p in paid)) if paid else None
    totals['payrollNet'] = _money(sum(Decimal(p['net']) for p in paid)) if paid else None
    totals['payHidden'] = sum(1 for r in rows if not r['paySeen'])
    trend = []
    for i, m in enumerate(data['months']):
        start = sum(r['trend'][i]['start'] for r in rows)
        end = sum(r['trend'][i]['end'] for r in rows)
        exits = sum(r['trend'][i]['exits'] for r in rows)
        trend.append({**m, 'start': start, 'end': end, 'exits': exits, 'attritionPct': _attrition(exits, start, end),
                      'byCompany': {r['id']: _attrition(r['trend'][i]['exits'], r['trend'][i]['start'], r['trend'][i]['end'])
                                    for r in rows}})
    return {'date': data['date'], 'month': data['month'], 'previousMonth': data['previousMonth'],
            'groupId': scope.group_id, 'groupName': scope.group_name, 'companies': rows, 'totals': totals,
            'attrition': trend, 'needsAttention': _attention(rows, data['previousMonth']['label'])}


# ================================================================ directory

DIMENSIONS = {
    'company': ('Company', 'company_name'),
    'branch': ('Branch', 'branch_name'),
    'department': ('Department', 'department'),
    'gender': ('Gender', 'gender'),
    'employment_type': ('Employment type', 'employment_type'),
}
GENDER_LABEL = dict(Employee.Gender.choices)
EMPLOYMENT_LABEL = dict(EmployeeAssignment.EmploymentType.choices)


def directory(user: AuthUser, today: date | None = None):
    """Every employee of the companies in scope, with company, branch, department and employment type (a QuerySet,
    narrowed by the directory filters and paged by the table)."""
    scope = group_scope(user)
    return placed_employees(scope.ids, today or npt_today())


def _label(dim: str, v) -> str:
    if v is None or v == '':
        return {'branch': 'No branch', 'department': 'No department', 'gender': 'Not recorded',
                'employment_type': 'Not recorded'}.get(dim, 'Unknown')
    if dim == 'gender':
        return GENDER_LABEL.get(v, v)
    if dim == 'employment_type':
        return EMPLOYMENT_LABEL.get(v, v)
    return v


def breakdowns(qs) -> dict[str, list[dict]]:
    """Headcount by company, branch, department, gender and employment type of a (filtered) directory queryset: one
    grouped query per dimension; branch, department and the others are split by company."""
    out: dict[str, list[dict]] = {}
    for dim, (_, col) in DIMENSIONS.items():
        if dim == 'company':
            q = qs.order_by().values('company_id', 'company_name').annotate(n=Count('id'))
            out[dim] = sorted(({'label': r['company_name'], 'company': r['company_name'], 'value': r['n']} for r in q),
                              key=lambda x: (-x['value'], collate_key(x['label'])))
            continue
        q = qs.order_by().values('company_name', col).annotate(n=Count('id'))
        out[dim] = sorted(({'label': _label(dim, r[col]), 'company': r['company_name'], 'value': r['n']} for r in q),
                          key=lambda x: (-x['value'], collate_key(x['label']), collate_key(x['company'])))
    return out


def breakdown_totals(rows: list[dict]) -> list[dict]:
    """Rows of one dimension summed over companies, for a chart of the whole group."""
    acc: dict[str, int] = {}
    for r in rows:
        acc[r['label']] = acc.get(r['label'], 0) + r['value']
    return sorted(({'label': k, 'value': v} for k, v in acc.items()), key=lambda x: (-x['value'], collate_key(x['label'])))


# ================================================================ reports

REPORTS = {
    'headcount': ('Headcount', 'People at the start and end of the period, joiners and leavers, per company.'),
    'attendance': ('Attendance summary', 'Working days, present, absent, on leave and late, per company and month.'),
    'leave': ('Leave balances and usage', 'Days taken in the period and balances at its end, per company and leave type.'),
    'overtime': ('Overtime', 'Overtime slips, detected and approved hours, per company and month.'),
    'payroll': ('Payroll summary', 'Gross, SSF and PF, TDS and net of locked pay runs, per company and BS month.'),
    'attrition': ('Attrition', 'Leavers and attrition rate, per company and month.'),
}


@dataclass(frozen=True)
class Period:
    months: tuple[Month, ...]
    label: str

    @property
    def start(self) -> date:
        return self.months[0].start

    @property
    def end(self) -> date:
        return self.months[-1].end


def period_of(kind: str, *, bs_year: int | None = None, bs_month: int | None = None, fy: int | None = None,
              today: date | None = None) -> Period:
    today = today or npt_today()
    if kind == 'fy':
        start = fy if fy is not None else current_fiscal_start(today)
        if not (MIN_YEAR <= start <= MAX_YEAR):
            raise BadRequestError('that fiscal year is outside the supported calendar')
        ms = fiscal_months(start, today) or fiscal_months(start)[:1]
        return Period(tuple(ms), f'Fiscal year {fiscal_label(start)}')
    if bs_year is None or bs_month is None:
        m = current_month(today)
    else:
        m = month(bs_year, bs_month)
    return Period((m,), m.label)


def _names(ids: list[str]) -> dict[str, str]:
    return {str(i): n for i, n in Company.objects.filter(pk__in=ids).values_list('id', 'name_en')}


def _ordered(ids: list[str], names: dict[str, str]) -> list[str]:
    return sorted(ids, key=lambda c: collate_key(names.get(c, '')))


def _hours(minutes) -> float:
    return round((minutes or 0) / 60, 2)


def report(user: AuthUser, kind: str, period: Period, company_id: str | None = None) -> Table:
    """One consolidated report as a reports.csv Table (company is the first column; a 'Group total' row closes each
    month when several companies are shown)."""
    if kind not in REPORTS:
        raise BadRequestError('unknown report')
    scope = group_scope(user)
    ids = narrow(scope, company_id)
    t = _BUILDERS[kind](scope, ids, period)
    t.extra.update({'title': REPORTS[kind][0], 'period': period.label})
    return t


def _total_row(rows: list[dict], keys: tuple[str, ...], **fixed) -> dict:
    out = {k: sum((r.get(k) or 0) for r in rows) for k in keys}
    return {'company': 'Group total', 'code': '', **fixed, **out}


def _headcount(scope: GroupScope, ids: list[str], p: Period) -> Table:
    before = p.start - timedelta(days=1)
    names = _names(ids)
    q = {str(r['company_id']): r for r in employees(ids).order_by().values('company_id').annotate(
        start=Count('id', filter=_active_at(before)), end=Count('id', filter=_active_at(p.end)),
        joiners=Count('id', filter=Q(joined__range=(p.start, p.end))),
        leavers=Count('id', filter=Q(exited__range=(p.start, p.end))))}
    rows = []
    for cid in _ordered(ids, names):
        r = q.get(cid, {})
        row = {'company': names[cid], 'code': scope.company(cid).code, 'start': r.get('start', 0), 'joiners': r.get('joiners', 0),
               'leavers': r.get('leavers', 0), 'end': r.get('end', 0)}
        row['net'] = row['joiners'] - row['leavers']
        rows.append(row)
    if len(rows) > 1:
        rows.append(_total_row(rows, ('start', 'joiners', 'leavers', 'end', 'net')))
    cols = [Column('company', 'Company'), Column('code', 'Code'), Column('start', 'At start', 'number'),
            Column('joiners', 'Joiners', 'number'), Column('leavers', 'Leavers', 'number'),
            Column('end', 'At end', 'number'), Column('net', 'Net change', 'number')]
    return Table(cols, rows, 'group-headcount')


def _per_month(scope: GroupScope, ids: list[str], p: Period, data: dict[tuple[str, str], dict], keys: tuple[str, ...],
               finish) -> list[dict]:
    names = _names(ids)
    rows = []
    for m in p.months:
        block = []
        for cid in _ordered(ids, names):
            r = data.get((cid, m.key), {})
            block.append({'month': m.label, 'company': names[cid], 'code': scope.company(cid).code,
                          **{k: r.get(k) or 0 for k in keys}})
        if len(block) > 1:
            block.append(_total_row(block, keys, month=m.label))      # summed before `finish` formats the rows
        rows.extend(finish(row) for row in block)
    return rows


def _attendance(scope: GroupScope, ids: list[str], p: Period) -> Table:
    months = list(p.months)
    keys = ('scheduled', 'present', 'absent', 'leave', 'late', 'late_min')
    q = (latest_days().for_companies(ids).filter(local_date__range=(p.start, p.end))
         .annotate(m=_month_of('local_date', months)).order_by().values('employee__company_id', 'm')
         .annotate(scheduled=Count('id', filter=Q(status__in=SCHEDULED)),
                   present=Count('id', filter=Q(status__in=WORKED)),
                   absent=Count('id', filter=Q(status=A.ABSENT)),
                   leave=Count('id', filter=Q(status__in=ON_LEAVE)),
                   late=Count('id', filter=Q(late_min__gt=0)),
                   late_min=Coalesce(Sum('late_min'), 0)))
    data = {(str(r['employee__company_id']), r['m']): r for r in q}

    def finish(row):
        row['rate'] = _pct(row['present'], row['scheduled'])
        row['late_hours'] = _hours(row.pop('late_min'))
        return row
    rows = _per_month(scope, ids, p, data, keys, finish)
    cols = [Column('month', 'BS month'), Column('company', 'Company'), Column('code', 'Code'),
            Column('scheduled', 'Working days', 'number'), Column('present', 'Present', 'number'),
            Column('absent', 'Absent', 'number'), Column('leave', 'On leave', 'number'),
            Column('late', 'Late arrivals', 'number'), Column('late_hours', 'Late hours', 'number'),
            Column('rate', 'Attendance %', 'number')]
    return Table(cols, rows, 'group-attendance')


def _leave(scope: GroupScope, ids: list[str], p: Period) -> Table:
    names = _names(ids)
    q = (LeaveLedger.objects.for_companies(ids).filter(effective_date__lte=p.end)
         .order_by().values('employee__company_id', 'leave_type__code', 'leave_type__name_en')
         .annotate(taken=Coalesce(-Sum('delta_days', filter=Q(reason__in=LEAVE_TAKEN, effective_date__gte=p.start)),
                                  Decimal(0), output_field=DEC),
                   balance=Coalesce(Sum('delta_days'), Decimal(0), output_field=DEC),
                   people=Count('employee', distinct=True,
                                filter=Q(reason__in=LEAVE_TAKEN, effective_date__gte=p.start))))
    rows = []
    for r in q:
        cid = str(r['employee__company_id'])
        rows.append({'company': names[cid], 'code': scope.company(cid).code, 'leave_type': r['leave_type__name_en'],
                     'leave_code': r['leave_type__code'], 'people': r['people'],
                     'taken': float(r['taken']), 'balance': float(r['balance'])})
    rows.sort(key=lambda x: (collate_key(x['company']), collate_key(x['leave_type'])))
    cols = [Column('company', 'Company'), Column('code', 'Code'), Column('leave_type', 'Leave type'),
            Column('leave_code', 'Type code'), Column('people', 'People who took leave', 'number'),
            Column('taken', 'Days taken', 'number'), Column('balance', 'Balance at end (days)', 'number')]
    return Table(cols, rows, 'group-leave')


def _overtime(scope: GroupScope, ids: list[str], p: Period) -> Table:
    months = list(p.months)
    keys = ('slips', 'people', 'detected', 'approved')
    q = (OtSlip.objects.for_companies(ids).filter(local_date__range=(p.start, p.end))
         .exclude(status=OtSlip.Status.REJECTED)
         .annotate(m=_month_of('local_date', months)).order_by().values('employee__company_id', 'm')
         .annotate(slips=Count('id'), people=Count('employee', distinct=True),
                   detected=Coalesce(Sum('detected_min'), 0),
                   approved=Coalesce(Sum('approved_min', filter=Q(status__in=OT_COUNTED)), 0)))
    data = {(str(r['employee__company_id']), r['m']): r for r in q}

    def finish(row):
        row['detected_hours'] = _hours(row.pop('detected'))
        row['approved_hours'] = _hours(row.pop('approved'))
        return row
    rows = _per_month(scope, ids, p, data, keys, finish)
    cols = [Column('month', 'BS month'), Column('company', 'Company'), Column('code', 'Code'),
            Column('slips', 'Slips', 'number'), Column('people', 'People', 'number'),
            Column('detected_hours', 'Detected hours', 'number'), Column('approved_hours', 'Approved hours', 'number')]
    return Table(cols, rows, 'group-overtime')


def _payroll(scope: GroupScope, ids: list[str], p: Period) -> Table:
    shown = [c for c in ids if scope.sees_pay(c)]
    in_period = Q()
    for m in p.months:
        in_period |= Q(run__period__bs_year=m.year, run__period__bs_month=m.month)
    keys = ('payslips', 'gross', 'ssf_employee', 'ssf_employer', 'tds', 'net')
    q = (Payslip.objects.filter(run__company_id__in=shown, run__status__in=PAY_FINAL).filter(in_period)
         .order_by().values('run__company_id', 'run__period__bs_year', 'run__period__bs_month')
         .annotate(payslips=Count('id'), gross=Sum('gross'), ssf_employee=Sum('employee_statutory'),
                   ssf_employer=Sum('employer_statutory'), tds=Sum('tds'), net=Sum('net'))) if shown else []
    data = {(str(r['run__company_id']), f"{r['run__period__bs_year']}-{r['run__period__bs_month']:02d}"): r for r in q}

    def finish(row):
        for k in keys[1:]:
            row[k] = _money(row[k] or 0)
        row['cost'] = _money(Decimal(row['gross']) + Decimal(row['ssf_employer']))
        return row
    rows = _per_month(scope, shown, p, data, keys, finish) if shown else []
    cols = [Column('month', 'BS month'), Column('company', 'Company'), Column('code', 'Code'),
            Column('payslips', 'Payslips', 'number'), Column('gross', 'Gross', 'number'),
            Column('ssf_employee', 'SSF / PF (employee)', 'number'), Column('ssf_employer', 'SSF / PF (employer)', 'number'),
            Column('tds', 'TDS', 'number'), Column('net', 'Net', 'number'), Column('cost', 'Cost to company', 'number')]
    hidden = [scope.company(c).name for c in ids if not scope.sees_pay(c)]
    return Table(cols, rows, 'group-payroll', {'hidden': hidden})


def _attrition_report(scope: GroupScope, ids: list[str], p: Period) -> Table:
    aggs = {}
    for i, m in enumerate(p.months):
        aggs[f's{i}'] = Count('id', filter=_active_at(m.start - timedelta(days=1)))
        aggs[f'e{i}'] = Count('id', filter=_active_at(m.end))
        aggs[f'x{i}'] = Count('id', filter=Q(exited__range=(m.start, m.end)))
    q = {str(r['company_id']): r for r in employees(ids).order_by().values('company_id').annotate(**aggs)}
    data = {(cid, m.key): {'start': r[f's{i}'], 'end': r[f'e{i}'], 'leavers': r[f'x{i}']}
            for cid, r in q.items() for i, m in enumerate(p.months)}

    def finish(row):
        row['rate'] = _attrition(row['leavers'], row['start'], row['end'])
        return row
    rows = _per_month(scope, ids, p, data, ('start', 'leavers', 'end'), finish)
    cols = [Column('month', 'BS month'), Column('company', 'Company'), Column('code', 'Code'),
            Column('start', 'At start', 'number'), Column('leavers', 'Leavers', 'number'),
            Column('end', 'At end', 'number'), Column('rate', 'Attrition %', 'number')]
    return Table(cols, rows, 'group-attrition')


_BUILDERS = {'headcount': _headcount, 'attendance': _attendance, 'leave': _leave, 'overtime': _overtime,
             'payroll': _payroll, 'attrition': _attrition_report}
