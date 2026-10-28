"""Everything a section needs (port of apps/api/src/db/demo-suite/context.ts)."""
from __future__ import annotations

import re
import secrets
from datetime import date
from dataclasses import dataclass, field
from typing import Callable

from django.db.models import BooleanField, ExpressionWrapper, OuterRef, Q, QuerySet, Subquery, UUIDField
from django.db.models.functions import Coalesce

from hajiri.core.auth import create_user
from hajiri.core.clock import npt_today
from hajiri.db.models import AppUser, Company, CompanyMembership, Employee, EmployeeAssignment

from .kit import Actor, Kit


@dataclass
class DemoEmployee:
    id: str
    code: str
    name: str
    first: str
    last: str
    gender: str | None
    designation: str | None
    join_date: str | None
    org_unit_id: str | None
    org_code: str | None
    manager_id: str | None
    branch_id: str | None
    user_id: str | None


@dataclass
class DemoContext:
    """The company, today, the employees in code order, and sessions for HR and any employee."""
    kit: Kit
    company_id: str
    company_name: str
    today: str
    domain: str
    hr: Actor
    employees: list[DemoEmployee]
    log: Callable[[str], None]
    _sessions: dict[str, Actor] = field(default_factory=dict)

    def by_code(self, code: str) -> DemoEmployee:
        for e in self.employees:
            if e.code == code:
                return e
        raise RuntimeError(f'no employee {code}')

    def as_(self, e: DemoEmployee) -> Actor:
        if not e.user_id:
            raise RuntimeError(f'{e.code} has no account')
        hit = self._sessions.get(e.user_id)
        if hit:
            return hit
        a = self.kit.actor(e.user_id, self.company_id)
        self._sessions[e.user_id] = a
        return a

    def manager_of(self, e: DemoEmployee) -> Actor:
        """The manager's session for an employee (falls back to HR when there is no line manager with an account)."""
        m = next((x for x in self.employees if x.id == e.manager_id), None) if e.manager_id else None
        return self.as_(m) if m and m.user_id else self.hr


def already_seeded(ctx: DemoContext, section: str, done: QuerySet, started: QuerySet) -> bool:
    """True when a section's data is already there (its last record exists), so the section is skipped. When only its
    first records exist, an earlier run stopped half-way: refuse rather than add duplicates. Both querysets are
    scoped to the context's company."""
    if done.for_company(ctx.company_id).exists():
        return True
    if started.for_company(ctx.company_id).exists():
        raise RuntimeError(f'the {section} data is only partly there from an earlier run; restore the demo database '
                           '(or remove that data) and run again')
    return False


def _s(v) -> str | None:
    return None if v is None else str(v)


def _iso(v) -> str | None:
    return None if v is None else (v if isinstance(v, str) else v.isoformat())


def _employees(company_id: str, today: str) -> list[Employee]:
    """Active employees in code order, with their assignment on `today` and the account that signs in as them in
    this company (a home account, else a group member given access with this employee record)."""
    current = EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=date.fromisoformat(today))
    home_account = (AppUser.objects.filter(employee=OuterRef('pk'), status=AppUser.Status.ACTIVE)
                    .order_by('created_at').values('id')[:1])
    member = CompanyMembership.objects.filter(employee=OuterRef('pk'), status=CompanyMembership.Status.ACTIVE,
                                              user__status=AppUser.Status.ACTIVE).values('user_id')[:1]
    return list(Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
                .annotate(org_unit_id=Subquery(current.values('org_unit_id')[:1]),
                          org_code=Subquery(current.values('org_unit__code')[:1]),
                          manager_id=Subquery(current.values('manager_id')[:1]),
                          branch_id=Subquery(current.values('branch_id')[:1]),
                          user_id=Coalesce(Subquery(home_account), Subquery(member), output_field=UUIDField()))
                .order_by('emp_code'))


def demo_hr(company_id: str) -> AppUser | None:
    """The account the seeder works as: an active HR (preferred) or admin of the company, home account, who has set
    their password."""
    return (AppUser.objects.home_of(company_id)
            .filter(status=AppUser.Status.ACTIVE, roles__overlap=[AppUser.Role.HR, AppUser.Role.ADMIN], must_change_password=False)
            .order_by(ExpressionWrapper(Q(roles__contains=[AppUser.Role.HR]), output_field=BooleanField()).desc(), 'created_at')
            .first())


def _add_manager_role(company_id: str, user_id) -> None:
    """Give an account the manager role in this company (home account roles, or the membership's roles)."""
    u = AppUser.objects.filter(pk=user_id, company_id=company_id).first()
    target = u or CompanyMembership.objects.for_company(company_id).filter(user_id=user_id).first()
    if target is not None and AppUser.Role.MANAGER not in target.roles:
        target.roles = [*target.roles, AppUser.Role.MANAGER]
        if u is not None:
            target.save(update_fields=['roles'])
        else:
            CompanyMembership.objects.filter(user_id=user_id, company_id=company_id).update(roles=target.roles)


def employee_email(first_name: str, code: str, domain: str) -> str:
    """first@domain, or first.code@domain when that address is taken."""
    first = re.sub(r'[^a-z]', '', re.split(r'\s+', first_name)[0].lower()) or 'user'
    email = f'{first}@{domain}'
    if AppUser.objects.filter(email=email).exists():
        email = f'{first}.{code.lower()}@{domain}'
    return email


def build_context(kit: Kit, company_code: str, log: Callable[[str], None]) -> DemoContext:
    c = Company.objects.filter(code=company_code).values('id', 'name_en').first()
    if not c:
        raise RuntimeError(f'company {company_code} not found')
    company_id = str(c['id'])
    today = npt_today().isoformat()
    hr_row = demo_hr(company_id)
    if not hr_row:
        raise RuntimeError('the company needs an active HR or admin user (who has set their password) to run the demo suite')
    parts = hr_row.email.split('@')
    domain = parts[1] if len(parts) > 1 else 'example.com'

    found = _employees(company_id, today)
    if len(found) < 8:
        raise RuntimeError(f'the demo suite needs at least 8 active employees (found {len(found)})')

    # Every employee gets an account so they can raise requests; the password is random (HR can reset it to hand it over).
    managers = {str(r.manager_id) for r in found if r.manager_id}
    created = 0
    for r in found:
        rid = str(r.id)
        if r.user_id:
            if rid in managers:
                _add_manager_role(company_id, r.user_id)
            continue
        want_roles = [AppUser.Role.EMPLOYEE, AppUser.Role.MANAGER] if rid in managers else [AppUser.Role.EMPLOYEE]
        create_user(company_id=company_id, employee_id=rid, email=employee_email(r.name_en, r.emp_code, domain), name=r.name_en,
                    password=secrets.token_urlsafe(18), roles=want_roles, must_change_password=False)
        created += 1
    if created:
        log(f'created {created} employee accounts (random passwords; reset one with the CLI to sign in as that person)')
        found = _employees(company_id, today)

    employees = []
    for r in found:
        names = re.split(r'\s+', r.name_en)
        employees.append(DemoEmployee(
            id=str(r.id), code=r.emp_code, name=r.name_en, first=names[0], last=' '.join(names[1:]) or names[0],
            gender=r.gender, designation=r.designation, join_date=_iso(r.join_date), org_unit_id=_s(r.org_unit_id),
            org_code=r.org_code, manager_id=_s(r.manager_id), branch_id=_s(r.branch_id), user_id=_s(r.user_id)))
    hr = kit.actor(str(hr_row.id), company_id)
    return DemoContext(kit=kit, company_id=company_id, company_name=c['name_en'], today=today, domain=domain, hr=hr,
                       employees=employees, log=log)


def instance_of(ctx: DemoContext, object_type: str, object_id: str) -> str | None:
    """The approval workflow of a record in the context's company (None when it needed no approval)."""
    from hajiri.db.models import WorkflowInstance
    found = (WorkflowInstance.objects.for_company(ctx.company_id).filter(object_type=object_type, object_id=object_id)
             .values_list('id', flat=True).first())
    return str(found) if found else None
