"""
Shared model plumbing: company-scoped querysets and ORM expressions for the schema's SQL functions.

Every model that holds company data declares how it reaches its company and uses CompanyQuerySet as its manager:

    class HelpdeskTicket(models.Model):
        company = models.ForeignKey('hajiri.Company', ...)
        company_scope = 'company'                       # a direct company_id column
        objects = CompanyQuerySet.as_manager()

    class AttendanceDay(models.Model):
        employee = models.ForeignKey('hajiri.Employee', ...)
        company_scope = 'employee__company'             # scoped through its parent
        objects = CompanyQuerySet.as_manager()

    HelpdeskTicket.objects.for_company(company_id_of(request.hajiri_user)).filter(status=HelpdeskTicket.Status.OPEN)
    AttendanceDay.objects.for_company(cid).filter(local_date=day)        # joins employee for the company test

`company_scope` is a lookup path ending in a company foreign key (or a plain `company_id` UUID column); the
queryset filters `{company_scope: company_id}`. A row of another company is simply not in the queryset, so `.get()`
raises DoesNotExist: map that to NotFoundError with hajiri.db.scoping.get_or_404 (ids from another company behave as
not found, never 403).
"""
from __future__ import annotations

from django.db import models
from django.db.models import Exists, OuterRef, Q


class CompanyQuerySet(models.QuerySet):
    """QuerySet for tables that belong to one company (directly or through a parent row)."""

    def for_company(self, company_id):
        """Rows of this company only. `company_id` is a UUID or its string; None matches nothing (an account with no
        company sees no company data)."""
        if company_id is None:
            return self.none()
        scope = getattr(self.model, 'company_scope', None)
        if not scope:
            raise TypeError(f'{self.model.__name__} has no company_scope; it is not company data')
        return self.filter(**{scope: company_id})

    def for_companies(self, company_ids):
        """Rows of any of these companies (group-wide views; pass only companies the viewer may see)."""
        ids = list(company_ids)
        if not ids:
            return self.none()
        scope = self.model.company_scope
        return self.filter(**{f'{scope}__in': ids})


class AppUserQuerySet(CompanyQuerySet):
    """Accounts. A person can work in several companies of a group: their home company is app_user.company_id and
    every other company is a company_membership row; a group role (group_member) reaches every company of the group.
    `for_company` returns everyone who can act in that company."""

    def for_company(self, company_id):
        if company_id is None:
            return self.none()
        from .platform import CompanyMembership, GroupMember
        member = CompanyMembership.objects.filter(user=OuterRef('pk'), company_id=company_id,
                                                  status=CompanyMembership.Status.ACTIVE)
        group_role = GroupMember.objects.filter(user=OuterRef('pk'), group__companies=company_id)
        return self.filter(Q(company_id=company_id) | Exists(member) | Exists(group_role))

    def home_of(self, company_id):
        """Only the accounts whose home company this is (what app_user.company_id alone meant before groups)."""
        return self.filter(company_id=company_id)


class CompanyQuerySetForCompany(models.QuerySet):
    """QuerySet for the company table itself."""

    def for_company(self, company_id):
        return self.filter(pk=company_id) if company_id else self.none()

    def in_group(self, group_id):
        return self.filter(group_id=group_id) if group_id else self.none()


CompanyManager = models.Manager.from_queryset(CompanyQuerySetForCompany)


# ---------------------------------------------------------------- SQL functions as ORM expressions

class Manages(models.Func):
    """`hj_manages(viewer, target, on_date)`: the viewer is the target's line manager, the head of their department (or
    of one above it) or of their branch on that Nepal date. Usable in filter() and annotate():

        Employee.objects.for_company(cid).filter(Manages(Value(user.employee_id), F('id'), Value(npt_today())))
        .annotate(mine=Manages(Value(viewer_id), F('employee_id'), F('local_date')))

    Plain Python values are wrapped in Value() automatically. Always pass a Nepal date (hajiri.core.clock.npt_today()).
    """
    function = 'hj_manages'
    output_field = models.BooleanField()
    arity = 3

    def __init__(self, viewer, target, on_date, **extra):
        super().__init__(_uuid(viewer), _uuid(target), _date(on_date), **extra)


class IsHead(models.Func):
    """`hj_is_head(employee)`: the employee heads a department or a branch (managers by position)."""
    function = 'hj_is_head'
    output_field = models.BooleanField()
    arity = 1

    def __init__(self, employee, **extra):
        super().__init__(_uuid(employee), **extra)


def _uuid(v):
    if hasattr(v, 'resolve_expression'):
        return v
    return models.functions.Cast(models.Value(None if v is None else str(v)), models.UUIDField())


def _date(v):
    if hasattr(v, 'resolve_expression'):
        return v
    return models.functions.Cast(models.Value(v if v is None or isinstance(v, str) else v.isoformat()), models.DateField())
