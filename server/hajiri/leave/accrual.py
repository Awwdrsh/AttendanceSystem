"""
Leave accrual (port of apps/api/src/modules/leave/accrual.ts, plan §10.6, Labour Act Sec 42-44, 49):

- worked_days basis (home leave): worked days / N, fractions carried in the numeric ledger;
- monthly basis (sick leave): accrual_days per month; nothing for a month the employee joined part-way (Sec 44 proviso);
- annual basis (casual): allocated once per fiscal year, pro-rated for joiners (allocate_annual_leave);
- caps: balance above max_balance is expired (and HR is told, so it can be paid out under Sec 49(3) if encashable);
- substitute leave past expires_on lapses.
Every posting is idempotent.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Case, DecimalField, Exists, OuterRef, Q, Sum, Value, When

from engines.bs_calendar import ad_to_bs, bs_month_bounds, fiscal_year, fiscal_year_of

from hajiri.core.users import user_ids_with_role
from hajiri.db.models import AttendanceDay, Employee, LeaveLedger, LeaveType
from hajiri.notifications.service import notify

from .service import ZERO, Reason, day, dec, fixed, iso, latest_days, num, round2, type_rows

Basis = LeaveType.AccrualBasis
WORKED = (AttendanceDay.Status.PRESENT, AttendanceDay.Status.WFH)
HALF_WORKED = (AttendanceDay.Status.HALF_DAY, AttendanceDay.Status.HALF_DAY_LEAVE)

FY_START_MONTH = 4  # Shrawan


def _balance(employee_id, leave_type_id) -> float:
    return num(LeaveLedger.objects.filter(employee_id=employee_id, leave_type_id=leave_type_id)
               .aggregate(b=Sum('delta_days'))['b'])


def _applies(t: dict, gender) -> bool:
    return t['gender_scope'] == LeaveType.GenderScope.ALL or t['gender_scope'] == gender


def run_monthly_accrual(company_id: str, bs_year: int, bs_month: int) -> dict:
    """Accrue one BS month for a company: {'employees', 'postings', 'expired', 'bsYear', 'bsMonth'}."""
    company_id = str(company_id)
    bounds = bs_month_bounds(bs_year, bs_month)
    start_ad, end_ad = bounds['startAd'], bounds['endAd']
    types = type_rows(LeaveType.objects.for_company(company_id).exclude(accrual_basis=Basis.NONE))
    emps = list(Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
                .filter(Q(join_date__isnull=True) | Q(join_date__lte=day(end_ad))).order_by('id')
                .values('id', 'gender', 'join_date'))
    # Worked days of the month per employee: a full day for Present/WFH, half for a half day.
    worked_by = dict(latest_days().filter(employee__in=[e['id'] for e in emps],
                                          local_date__range=(day(start_ad), day(end_ad)))
                     .values('employee_id')
                     .annotate(w=Sum(Case(When(status__in=WORKED, then=Value(Decimal(1))),
                                          When(status__in=HALF_WORKED, then=Value(Decimal('0.5'))),
                                          default=ZERO, output_field=DecimalField())))
                     .order_by().values_list('employee_id', 'w')) if emps and types else {}
    postings = 0

    def key(emp_id: str, type_id: str) -> str:
        return f'accrual:{emp_id}:{type_id}:{bs_year}-{bs_month}'

    for emp in emps:
        emp_id = str(emp['id'])
        join = iso(emp['join_date'])
        worked_days = num(worked_by.get(emp['id']))
        for t in types:
            if not _applies(t, emp['gender']):
                continue
            if LeaveLedger.objects.filter(employee_id=emp_id, leave_type_id=t['id'], reason__in=(Reason.ACCRUAL, Reason.ALLOCATION),
                                          note=key(emp_id, t['id'])).exists():
                continue
            delta = 0.0
            if t['accrual_basis'] == Basis.WORKED_DAYS:
                per = t['accrual_per_worked_days'] if t['accrual_per_worked_days'] is not None else 20
                delta = round2((worked_days / per) * num(t['accrual_days']))
            elif t['accrual_basis'] == Basis.MONTHLY:
                delta = num(t['accrual_days'])
                # First year of service: a month counts once the employee was there for all of it.
                if join and join > start_ad:
                    delta = 0.0
            elif t['accrual_basis'] == Basis.ANNUAL:
                continue  # allocated per fiscal year by allocate_annual_leave below
            if delta <= 0:
                continue
            LeaveLedger.objects.create(employee_id=emp_id, leave_type_id=t['id'], effective_date=day(end_ad), delta_days=dec(delta),
                                       reason=Reason.ACCRUAL, ref_type='accrual', note=key(emp_id, t['id']))
            postings += 1
            # Cap (Sec 43: home <= 90, Sec 44: sick <= 45). Excess is expired; encashable excess is flagged to HR.
            if t['max_balance'] is not None:
                excess = _balance(emp_id, t['id']) - num(t['max_balance'])
                if excess > 0:
                    LeaveLedger.objects.create(employee_id=emp_id, leave_type_id=t['id'], effective_date=day(end_ad),
                                               delta_days=dec(-excess), reason=Reason.EXPIRY, ref_type='accrual_cap',
                                               note=f"{key(emp_id, t['id'])}:cap")
                    if t['encashable']:
                        for user_id in user_ids_with_role(company_id, 'hr'):
                            notify(company_id=company_id, user_id=user_id, employee_id=emp_id, template='leave.cap_exceeded',
                                   title='Leave balance above cap',
                                   body=f"{t['name_en']} balance exceeded {t['max_balance']} days by {fixed(excess, 2)}; "
                                        'excess should be paid out (Sec 49(3)).',
                                   payload={'leaveTypeId': t['id'], 'excess': excess})
    postings += allocate_annual_leave(company_id, end_ad)
    expired = expire_substitute_leave(company_id, end_ad)
    return {'employees': len(emps), 'postings': postings, 'expired': expired, 'bsYear': bs_year, 'bsMonth': bs_month}


def _months_since(from_ad: str, to_ad: str) -> int:
    a, b = date.fromisoformat(from_ad), date.fromisoformat(to_ad)
    return (b.year - a.year) * 12 + (b.month - a.month) + (1 if b.day > a.day else 0)


def allocate_annual_leave(company_id: str, as_of_ad: str, employee_id: str | None = None) -> int:
    """
    Annual-basis leave (casual): one allocation per employee per fiscal year, granted as soon as the employee is on the
    books. Joiners during the year get a share pro-rated by the months left. Any existing allocation of the type in the
    fiscal year (automatic or an HR opening allocation) counts, so this never grants twice. Returns postings made.
    """
    company_id = str(company_id)
    fy = fiscal_year_of(ad_to_bs(as_of_ad))
    fy_start, next_fy_start = day(fy.start_ad), day(fiscal_year(fy.start_year + 1).start_ad)
    types = type_rows(LeaveType.objects.for_company(company_id).filter(accrual_basis=Basis.ANNUAL, accrual_days__gt=0))
    if not types:
        return 0
    emps = (Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
            .filter(Q(join_date__isnull=True) | Q(join_date__lte=day(as_of_ad)))
            .filter(Q(exit_date__isnull=True) | Q(exit_date__gte=fy_start)))
    if employee_id:
        emps = emps.filter(pk=employee_id)
    emps = list(emps.order_by('id').values('id', 'gender', 'join_date'))
    if not emps:
        return 0
    done = LeaveLedger.objects.filter(employee__in=[e['id'] for e in emps], leave_type_id__in=[t['id'] for t in types],
                                      reason=Reason.ALLOCATION, effective_date__gte=fy_start, effective_date__lt=next_fy_start)
    allocated = {(str(e), str(lt)) for e, lt in done.values_list('employee_id', 'leave_type_id').distinct()}
    n = 0
    for emp in emps:
        emp_id = str(emp['id'])
        for t in types:
            if not _applies(t, emp['gender']) or (emp_id, t['id']) in allocated:
                continue
            join = iso(emp['join_date'])
            joined_in_year = join is not None and join > fy.start_ad
            months = max(0, 12 - _months_since(fy.start_ad, join)) if joined_in_year else 12
            delta = round2(num(t['accrual_days']) * months / 12)
            if delta <= 0:
                continue
            with transaction.atomic():
                # Concurrent callers (worker, HR catch-up, balance views) serialise on the employee row, and the one that
                # gets there second sees the allocation and skips.
                list(Employee.objects.select_for_update(no_key=True).filter(pk=emp_id).values_list('pk', flat=True))
                if LeaveLedger.objects.filter(employee_id=emp_id, leave_type_id=t['id'], reason=Reason.ALLOCATION,
                                              effective_date__gte=fy_start, effective_date__lt=next_fy_start).exists():
                    continue
                LeaveLedger.objects.create(employee_id=emp_id, leave_type_id=t['id'],
                                           effective_date=day(join) if joined_in_year else fy_start, delta_days=dec(delta),
                                           reason=Reason.ALLOCATION, ref_type='accrual',
                                           note=f"allocation:{emp_id}:{t['id']}:FY{fy.start_year}")
                n += 1
    return n


def run_leave_catch_up(company_id: str, as_of_ad: str) -> dict:
    """
    Run every completed BS month of the current fiscal year that has not been accrued yet (each month is idempotent),
    then grant any missing annual allocation as of today: {'months': [summary], 'annualPostings', 'asOf'}.
    """
    bs = ad_to_bs(as_of_ad)
    fy = fiscal_year_of(bs)
    months = []
    y, m = fy.start_year, FY_START_MONTH
    while y < bs.year or (y == bs.year and m < bs.month):
        months.append(run_monthly_accrual(company_id, y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    annual = allocate_annual_leave(company_id, as_of_ad)
    return {'months': months, 'annualPostings': annual, 'asOf': as_of_ad}


def expire_substitute_leave(company_id: str, as_of: str) -> int:
    """Substitute leave not used within its window lapses (Sec 42: 21 days). Returns rows expired."""
    lapsed = LeaveLedger.objects.filter(reason=Reason.EXPIRY, ref_type='comp_off', ref_id=OuterRef('pk'))
    due = (LeaveLedger.objects.filter(leave_type__company_id=company_id, reason=Reason.COMP_OFF, expires_on__isnull=False,
                                      expires_on__lt=day(as_of))
           .exclude(Exists(lapsed)).values('id', 'employee_id', 'leave_type_id', 'delta_days', 'expires_on'))
    n = 0
    for row in due:
        # Only the unused part of the credit lapses: the balance cannot go below zero because of expiry.
        lapse = min(num(row['delta_days']), max(0.0, _balance(row['employee_id'], row['leave_type_id'])))
        LeaveLedger.objects.create(employee_id=row['employee_id'], leave_type_id=row['leave_type_id'],
                                   effective_date=row['expires_on'], delta_days=dec(-lapse), reason=Reason.EXPIRY,
                                   ref_type='comp_off', ref_id=row['id'],
                                   note=f"substitute leave expired after {iso(row['expires_on'])}")
        n += 1
    return n
