"""Querysets the payroll services share: the newest attendance version of each day, locked payslips, the department
an employee is in on a date, and sums over related rows as subqueries."""
from __future__ import annotations

from datetime import date

from django.db.models import DecimalField, Exists, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from hajiri.db.models import AttendanceDay, EmployeeAssignment, Payslip

MONEY = DecimalField(max_digits=14, decimal_places=2)


def latest_days():
    """attendance_day rows that are the newest computed version of their day (what the v_attendance_latest view shows)."""
    newer = AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                         version__gt=OuterRef('version'))
    return AttendanceDay.objects.exclude(Exists(newer))


def locked_payslips():
    """Payslips of locked runs (what counts as paid)."""
    return Payslip.objects.filter(run__locked_at__isnull=False)


def in_fiscal_window(qs, start_ad, last_start_ad, *, inclusive: bool = True):
    """Payslips whose period starts between the fiscal year start and `last_start_ad` (inclusive or not)."""
    qs = qs.filter(run__period__start_ad__gte=start_ad)
    return qs.filter(run__period__start_ad__lte=last_start_ad) if inclusive else qs.filter(run__period__start_ad__lt=last_start_ad)


def department_on(on_date=None, employee_ref='pk'):
    """The org unit name of the employee's assignment covering `on_date` (or, with no date, the newest assignment)."""
    qs = EmployeeAssignment.objects.filter(employee_id=OuterRef(employee_ref))
    if isinstance(on_date, str):
        on_date = date.fromisoformat(on_date)
    qs = qs.filter(valid__contains=on_date) if on_date is not None else qs.order_by('-valid__startswith')
    return Subquery(qs.values('org_unit__name_en')[:1])


def sum_of(qs, field: str, *, group: str, output_field=MONEY):
    """coalesce((SELECT sum(field) FROM qs), 0) as a correlated subquery; `qs` is already filtered by OuterRef and
    `group` is the column it is filtered on."""
    from django.db.models import Sum
    total = qs.order_by().values(group).annotate(total=Sum(field)).values('total')[:1]
    return Coalesce(Subquery(total, output_field=output_field), Value(0), output_field=output_field)


def count_of(qs, *, group: str):
    from django.db.models import Count
    n = qs.order_by().values(group).annotate(n=Count('pk')).values('n')[:1]
    return Coalesce(Subquery(n, output_field=IntegerField()), Value(0), output_field=IntegerField())


def tuple_before(prefix: str, year: int, month: int) -> Q:
    """(bs_year, bs_month) < (year, month) for the fields under `prefix`."""
    return Q(**{f'{prefix}bs_year__lt': year}) | Q(**{f'{prefix}bs_year': year, f'{prefix}bs_month__lt': month})


def tuple_at_most(prefix: str, year: int, month: int) -> Q:
    """(bs_year, bs_month) <= (year, month)."""
    return Q(**{f'{prefix}bs_year__lt': year}) | Q(**{f'{prefix}bs_year': year, f'{prefix}bs_month__lte': month})
