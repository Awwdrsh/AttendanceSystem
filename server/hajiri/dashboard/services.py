"""
Executive and employee home dashboards (port of apps/api/src/modules/dashboard/routes.ts; plan §3, §13.2: at most
6 KPI tiles, one trend, one "needs attention" list). The Overview page renders `executive`; the Home page (home
slice) uses `me`.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import (BooleanField, Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef,
                              Q, Subquery, Sum, TextField, Value)
from django.db.models.functions import Cast, Coalesce, Concat, Now

from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds

from hajiri.attendance.repo import latest_days  # noqa: F401  (re-exported)
from hajiri.attendance.service import list_days
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.db.models import (AttendanceDay, Device, Employee, LeaveLedger, LeaveRequest, LeaveType, OtRequest,
                              OtSlip, PayrollRun, Payslip, RegularisationRequest, Shift, WorkflowInstance)
from hajiri.devices.constants import PSEUDO_VENDORS
from hajiri.devices.services import count_of, offline_q, unmapped_punch_queryset

EXECUTIVE_ROLES = ('hr', 'manager', 'executive')

S = AttendanceDay.Status
ATTENDED = (S.PRESENT, S.HALF_DAY, S.HALF_DAY_LEAVE, S.WFH, S.INCOMPLETE)      # "present" tile
PRESENT_FOR_RATE = (S.PRESENT, S.HALF_DAY, S.HALF_DAY_LEAVE, S.WFH)             # trend numerator
NOT_SCHEDULED = (S.HOLIDAY, S.WEEKLY_OFF, S.OFF_SHIFT)                           # left out of the trend


def _month(today: str) -> tuple[str, str, str | None]:
    """(start, end, 'YYYY-MM' BS label) of the BS month containing today; today itself outside the calendar table."""
    try:
        bs = ad_to_bs(today)
        b = bs_month_bounds(bs.year, bs.month)
        return str(b['startAd']), str(b['endAd']), f'{bs.year}-{bs.month:02d}'
    except (BsRangeError, ValueError):
        return today, today, None


def _pct(part: int, whole: int) -> float | None:
    """round(100.0 * part / whole, 1) as PostgreSQL rounds numerics (half away from zero); None for no working days."""
    if not whole:
        return None
    return float((Decimal(100 * part) / Decimal(whole)).quantize(Decimal('0.1'), ROUND_HALF_UP))


def _last_run(cid: str):
    """The company's newest payroll run with its payslip count and totals (totals as text, like numeric::text)."""
    slips = Payslip.objects.filter(run=OuterRef('pk'))

    def total(field):
        sub = slips.order_by().values('run').annotate(s=Sum(field)).values('s')
        return Cast(Coalesce(Subquery(sub), 0, output_field=DecimalField()), TextField())
    return (PayrollRun.objects.for_company(cid).order_by('-created_at')
            .values('id', 'status', 'run_type', bs_year=F('period__bs_year'), bs_month=F('period__bs_month'),
                    headcount=count_of(slips, 'run'), net=total('net'), gross=total('gross'))
            .first())


def executive(user: AuthUser, today: str | None = None) -> dict:
    if not has_role(user, *EXECUTIVE_ROLES):
        raise ForbiddenError(f'requires role {" or ".join(EXECUTIVE_ROLES)}')
    cid = company_id_of(user)
    today = today or npt_today().isoformat()
    try:
        bs = ad_to_bs(today)
        month_start = str(bs_month_bounds(bs.year, bs.month)['startAd'])
    except (BsRangeError, ValueError):
        month_start = today[:8] + '01'
    day = date.fromisoformat(today)
    days = latest_days().for_company(cid)
    on_today = Q(local_date=day)
    k = days.filter(local_date__range=(month_start, day)).aggregate(
        present=Count('id', filter=on_today & Q(status__in=ATTENDED)),
        late=Count('id', filter=on_today & Q(late_min__gt=0)),
        absent=Count('id', filter=on_today & Q(status=S.ABSENT)),
        exceptions=Count('id', filter=Q(locked_at__isnull=True) & (
            Q(status__in=(S.INCOMPLETE, S.ABSENT)) | Q(flags__contains=[{'kind': 'OTPreapprovalMissing'}])
            | Q(flags__contains=[{'kind': 'OTCapExceeded'}]))))
    headcount = Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE).count()
    pending = WorkflowInstance.objects.for_company(cid).filter(state=WorkflowInstance.State.PENDING).count()
    trend = (days.filter(local_date__gt=day - timedelta(days=30), local_date__lte=day)
             .values('local_date')
             .annotate(present=Count('id', filter=Q(status__in=PRESENT_FOR_RATE)),
                       scheduled=Count('id', filter=~Q(status__in=NOT_SCHEDULED)))
             .order_by('local_date'))
    devices = (Device.objects.for_company(cid).filter(status=Device.Status.ACTIVE).exclude(vendor__in=PSEUDO_VENDORS)
               .filter(offline_q()).order_by(F('last_seen_at').asc(nulls_first=True))
               .values('serial_no', 'name', 'last_seen_at'))
    unmapped = unmapped_punch_queryset(cid).count()
    ot = OtSlip.objects.for_company(cid).aggregate(
        n=Count('id', filter=Q(status=OtSlip.Status.DETECTED)),
        minutes=Coalesce(Sum('detected_min', filter=Q(local_date__gte=month_start)), 0))
    leave_old = (LeaveRequest.objects.for_company(cid)
                 .filter(status=LeaveRequest.Status.PENDING, created_at__lt=Now() - timedelta(hours=24)).count())
    last_run = _last_run(cid)
    attention = [{'kind': 'device_offline', 'label': f"{d['name'] or d['serial_no']} is offline", 'since': d['last_seen_at']}
                 for d in devices]
    if unmapped > 0:
        attention.append({'kind': 'unmapped_punches', 'label': f'{unmapped} punches from unknown PINs', 'count': unmapped})
    if ot['n'] > 0:
        attention.append({'kind': 'ot_unapproved', 'label': f"{ot['n']} overtime slips awaiting review", 'count': ot['n']})
    if leave_old > 0:
        attention.append({'kind': 'leave_overdue', 'label': f'{leave_old} leave requests waiting over 24 h', 'count': leave_old})
    if last_run:
        last_run = {**last_run, 'id': str(last_run['id'])}
    return {
        'date': today,
        'tiles': [
            {'key': 'headcount', 'label': 'Active employees', 'value': headcount},
            {'key': 'present', 'label': 'Present today', 'value': k['present']},
            {'key': 'late', 'label': 'Late today', 'value': k['late']},
            {'key': 'absent', 'label': 'Absent today', 'value': k['absent']},
            {'key': 'pendingApprovals', 'label': 'Pending approvals', 'value': pending},
            {'key': 'exceptions', 'label': 'Open exceptions this month', 'value': k['exceptions']},
        ],
        'trend': [{'date': t['local_date'].isoformat(), 'presentPct': _pct(t['present'], t['scheduled'])} for t in trend],
        'needsAttention': attention,
        'otMinutesThisMonth': ot['minutes'],
        # Payroll totals are HR-only; managers get the attendance tiles.
        'lastPayrollRun': last_run if has_role(user, 'hr', 'payroll_admin', 'executive') else None,
    }


def _pending_requests(employee_id: str) -> list[dict]:
    """The employee's pending leave, correction and overtime requests, newest first (at most 20): one UNION ALL query.
    Every part selects the same columns in the same order (id, created_at, then the annotations kind_t and detail_t;
    regularisation_request has its own `kind` column)."""
    def text(field):
        return Cast(field, TextField())

    def part(qs, kind, *detail):
        return qs.filter(employee_id=employee_id).values('id', 'created_at', kind_t=Value(kind, output_field=TextField()),
                                                         detail_t=Concat(*detail, output_field=TextField()))
    leave = part(LeaveRequest.objects.filter(status=LeaveRequest.Status.PENDING), 'leave',
                 text('from_date'), Value(' to '), text('to_date'))
    reg = part(RegularisationRequest.objects.filter(status=RegularisationRequest.Status.PENDING), 'regularisation',
               text('local_date'), Value(' '), text('kind'))
    ot = part(OtRequest.objects.filter(status=OtRequest.Status.PENDING), 'ot',
              text('local_date'), Value(' '), text('requested_min'), Value(' min'))
    return [{'kind': r['kind_t'], 'id': r['id'], 'created_at': r['created_at'], 'detail': r['detail_t']}
            for r in leave.union(reg, ot, all=True).order_by('-created_at')[:20]]


def me(user: AuthUser, today: str | None = None) -> dict:
    """The employee home: today, this BS month's summary, leave balances, unread notifications and pending requests."""
    employee_id = user.employee_id
    if not employee_id:
        raise BadRequestError('Your account is not linked to an employee record.')
    today = today or npt_today().isoformat()
    month_start, month_end, bs_label = _month(today)
    days = list_days(employee_id, today, today)
    month = (latest_days().filter(employee_id=employee_id, local_date__range=(month_start, month_end))
             .values('status').annotate(n=Count('id'), late=Coalesce(Sum('late_min'), 0), ot=Coalesce(Sum('ot_min'), 0))
             .order_by())
    # The employee record belongs to one company: its leave types are the ones with a balance.
    company_of_employee = Employee.objects.filter(pk=employee_id).values('company_id')
    ledger = (LeaveLedger.objects.filter(employee_id=employee_id, leave_type=OuterRef('pk')).order_by()
              .values('leave_type').annotate(s=Sum('delta_days')).values('s'))
    balance = Coalesce(Subquery(ledger), 0, output_field=DecimalField())
    balances = list(LeaveType.objects.filter(company_id__in=Subquery(company_of_employee))
                    .filter(~Q(accrual_basis=LeaveType.AccrualBasis.NONE) | Q(code='SUBSTITUTE'))
                    .annotate(balance=Cast(Cast(balance, DecimalField(max_digits=8, decimal_places=2)), TextField()))
                    .order_by('sort_order')
                    .values('code', 'name_en', 'name_ne', 'balance'))
    # Notifications are addressed to the person (user_id); count them as the bell does, for the active company.
    from hajiri.notifications.service import unread_count
    unread = unread_count(user.id, company_id=company_id_of(user))
    wfh_today = RegularisationRequest.objects.filter(
        employee=OuterRef('pk'), kind=RegularisationRequest.Kind.WFH, status=RegularisationRequest.Status.APPROVED,
        local_date=today)
    wfh = (Employee.objects.filter(pk=employee_id)
           .values('work_from_home', approved_today=ExpressionWrapper(Exists(wfh_today), output_field=BooleanField()))
           .first())
    summary: dict[str, int] = {}
    late_min = ot_min = 0
    for m in month:
        summary[m['status']] = m['n']
        late_min += m['late']
        ot_min += m['ot']
    return {
        'date': today, 'today': days[0] if days else None,
        'month': {'bs': bs_label, 'from': month_start, 'to': month_end, 'byStatus': summary, 'lateMin': late_min, 'otMin': ot_min},
        'leaveBalances': balances, 'unreadNotifications': unread, 'pendingRequests': _pending_requests(employee_id),
        'workFromHome': {'allowed': bool(wfh and wfh['work_from_home']), 'approvedToday': bool(wfh and wfh['approved_today'])},
    }


# ---------------------------------------------------------------- Overview page helpers

def team_today(user: AuthUser, today: str) -> list[dict]:
    """The Team Today rows the Overview page draws (attendance.today); empty for a role that may not see them."""
    from hajiri.attendance import api
    try:
        return api.today(user, {'date': today})['rows']
    except ForbiddenError:
        return []


def first_shift(company_id: str) -> dict | None:
    """The company's first shift by code (shifts list order), for the arrivals ribbon's start line and grace."""
    return Shift.objects.for_company(company_id).order_by('code').values('start_time', 'grace_in_min').first()


def _minute_of_day(at) -> int:
    from hajiri.core.clock import NPT
    t = at.astimezone(NPT)
    return t.hour * 60 + t.minute


def arrivals_ribbon(team: list[dict], shift_start: str = '10:00', grace: int = 15) -> dict:
    """ArrivalsRibbon.tsx geometry: one mark per first clock-in on a time axis, stacked within a few minutes."""
    sh, sm = (int(x) for x in shift_start.split(':')[:2])
    start = sh * 60 + sm
    arrivals = [r for r in team if r.get('first_in')]
    times = [_minute_of_day(r['first_in']) for r in arrivals]
    lo = math.floor((min([start - 60, *times]) - 10) / 30) * 30
    hi = math.ceil((max([start + 60, *times]) + 10) / 30) * 30
    span = hi - lo

    def pos(m: int) -> float:
        return round((min(max(m, lo), hi) - lo) / span * 100, 3)

    # Marks within `bucket` minutes stack: span / 60 rounded half up (the span is a whole number of minutes).
    bucket = max(2, (span + 30) // 60)
    stacks: dict[int, int] = {}
    marks = []
    from hajiri.web.format import fmt_time
    for r, m in zip(arrivals, times):
        b = m // bucket
        level = stacks.get(b, 0)
        stacks[b] = level + 1
        late = (r.get('late_min') or 0) > 0
        marks.append({'left': pos(b * bucket), 'bottom': 4 + level * 11, 'level': level, 'late': late,
                      'title': f"{r['name_en']}, {fmt_time(r['first_in'])}{' (late)' if late else ''}"})
    hours = [{'left': pos(t), 'label': f'{t // 60:02d}:{t % 60:02d}'} for t in range(lo, hi + 1, 30)]
    max_level = max([3, *[mk['level'] + 1 for mk in marks]])
    return {'marks': marks, 'hours': hours, 'height': max_level * 11 + 8, 'shift_left': pos(start),
            'grace_width': round(grace / span * 100, 3), 'shift_start': shift_start}


def overview(user: AuthUser) -> dict:
    """Everything the Overview page (DashboardPage.tsx) shows."""
    from hajiri.core.clock import now
    from hajiri.web.format import fmt_date_long, greeting
    today = npt_today().isoformat()
    d = executive(user, today)
    tile = {t['key']: t['value'] for t in d['tiles']}
    team = team_today(user, today)
    shift = first_shift(user.company_id)
    start = shift['start_time'].strftime('%H:%M') if shift else '10:00'
    grace = shift['grace_in_min'] if shift and shift['grace_in_min'] is not None else 15
    clocked = [r for r in team if r.get('first_in')]
    recent = sorted(clocked, key=lambda r: r['first_in'], reverse=True)[:6]
    from hajiri.web.templatetags.hj import live_status
    recent = [{**r, 'live_status': live_status(r['status'], on_date=today, today=today, last_out=r.get('last_out'),
                                               first_in=r.get('first_in')) or 'NotComputed'} for r in recent]
    not_in = len([r for r in team if not r.get('first_in') and r.get('status') not in ('WeeklyOff', 'Holiday', 'OnLeave')])
    first_name = (user.name or '').split()[0] if (user.name or '').split() else ''
    stats = [
        {'label': 'In today', 'value': tile['present'], 'hint': f"of {tile['headcount']} active employees"},
        {'label': 'Late', 'value': tile['late'], 'hint': 'after shift grace', **({'tone': 'warn'} if tile['late'] > 0 else {})},
        {'label': 'Absent', 'value': tile['absent'], 'hint': f'{not_in} not clocked in yet'},
        {'label': 'Pending approvals', 'value': tile['pendingApprovals'], 'hint': 'leave, corrections, overtime'},
        {'label': 'Open exceptions', 'value': tile['exceptions'], 'hint': 'this payroll month'},
    ]
    attention_links = {
        'device_offline': ('/settings/devices', 'fingerprint', 'Check device'),
        'unmapped_punches': ('/settings/devices', 'fingerprint', 'Map PINs'),
        'ot_unapproved': ('/attendance/overtime', 'clock-hour-4', 'Review'),
        'leave_overdue': ('/approvals', 'inbox', 'Open approvals'),
    }
    attention = []
    for n in d['needsAttention']:
        to, ico, action = attention_links.get(n['kind'], ('/overview', 'alert-triangle', 'Open'))
        attention.append({**n, 'to': to, 'icon': ico, 'action': action})
    return {
        'title': f"{greeting(now())}{', ' + first_name if first_name else ''}",
        'subtitle': fmt_date_long(today),
        'today': today,
        'data': d,
        'stats': stats,
        'ribbon': arrivals_ribbon(team, start, grace),
        'trend': [{'date': p['date'], 'value': p['presentPct']} for p in d['trend'] if p['date'] != today],
        'attention': attention,
        'recent': recent,
        'clocked_count': len(clocked),
    }
