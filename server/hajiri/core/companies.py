"""
Groups of companies: one Hajiri install, one sign-in, several companies (a business group that owns several firms).

- An account's home company is app_user.company_id; access to the group's other companies is a company_membership
  row with its own roles and employee record. The session's active company decides which applies
  (hajiri.core.auth.switch_company); every page and service keeps scoping by `company_id_of(user)`.
- Group-wide views are the only code that reads across companies, and only across `group_company_ids(user)`: the
  companies where the person holds a role that may see company-wide figures (admin, hr, executive by default).
- Group roles (group_member) reach every company of a group: group_admin is admin everywhere, group_hr is HR
  everywhere, group_executive has the read-only executive view everywhere. Group admins grant and remove group roles.
- The admin of a standalone company can turn it into a group by adding a sister company (seeded with the standard
  defaults); they become the group's first group admin. Once a group exists only its group admins add companies.
  Any admin can give people of the group access to the company they administer (company_membership).

Every mutation is audited ('create'/'update'/'delete' on company, company_group, company_membership, group_member).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery

from .audit import audit
from .auth import GROUP_ROLES, ROLES, AuthUser, company_access, has_role
from .clock import npt_today
from .collation import collate_key
from .errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from .scoping import as_uuid

# Roles that may see a company's figures in group-wide views (admin always may).
GROUP_VIEW_ROLES = ('hr', 'executive')
ACTIVE_TICKET = ('open', 'in_progress', 'waiting')


def _m():
    from hajiri.db import models
    return models


@dataclass(frozen=True)
class CompanyAccess:
    """One company the person can work in, with what they are there."""
    id: str
    code: str
    name: str
    group_id: str | None
    roles: tuple[str, ...]
    employee_id: str | None
    home: bool
    active: bool
    group_name: str | None = None

    def has_role(self, *roles: str) -> bool:
        return 'admin' in self.roles or any(r in self.roles for r in roles)


def my_companies(user: AuthUser) -> list[CompanyAccess]:
    """The companies this person can switch to (home first, then by name), with their roles in each (own roles and
    those given by group roles)."""
    refs = user.companies
    if not refs:        # an AuthUser built by hand (tests, scripts): read the access rows
        m = _m()
        u = m.AppUser.objects.select_related('company__group', 'employee').filter(pk=user.id).first()
        refs = company_access(u)[0] if u else []
    return [CompanyAccess(r.id, r.code, r.name, r.group_id, r.roles, r.employee_id, r.home, r.id == user.company_id,
                          r.group_name) for r in refs]


def group_company_ids(user: AuthUser, *roles: str) -> list[str]:
    """Companies whose company-wide figures this person may see in a group view (roles default GROUP_VIEW_ROLES)."""
    need = roles or GROUP_VIEW_ROLES
    return [a.id for a in my_companies(user) if a.has_role(*need)]


def can_view_group(user: AuthUser) -> bool:
    """The group overview is offered to people who may see figures of at least two companies, and to anyone with a
    group role."""
    return bool(user.group_roles) or (user.multi_company and len(group_company_ids(user)) >= 2)


def can_open_group(user: AuthUser) -> bool:
    """The Group hub: the overview (can_view_group) or, for an admin, the page that adds a sister company."""
    return can_view_group(user) or has_role(user, 'admin')


def is_group_admin(user: AuthUser) -> bool:
    return 'group_admin' in user.group_roles


# ---------------------------------------------------------------- group overview

def group_overview(user: AuthUser) -> dict:
    """One row of headline figures per company the person may see, plus group totals: people, today's attendance
    (latest computed version of each day), pending approvals, open help desk tickets and the latest regular payroll."""
    m = _m()
    ids = group_company_ids(user)
    if not ids:
        raise ForbiddenError('requires role hr or executive')
    today = npt_today()
    A = m.AttendanceDay.Status
    # Employed today, as the group dashboard and Insights count (an accepted exit past its last day no longer counts).
    headcount = dict(m.Employee.objects.for_companies(ids).filter(status=m.Employee.Status.ACTIVE)
                     .exclude(exit_date__lt=today).exclude(join_date__gt=today)
                     .values('company_id').annotate(n=Count('id')).values_list('company_id', 'n'))
    newer = m.AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                           version__gt=OuterRef('version'))
    attendance = {r['employee__company_id']: r for r in (
        m.AttendanceDay.objects.for_companies(ids).filter(local_date=today).exclude(Exists(newer))
        .values('employee__company_id')
        .annotate(present=Count('id', filter=Q(status__in=[A.PRESENT, A.HALF_DAY, A.WFH, A.INCOMPLETE])),
                  on_leave=Count('id', filter=Q(status__in=[A.ON_LEAVE, A.HALF_DAY_LEAVE])),
                  absent=Count('id', filter=Q(status=A.ABSENT))))}
    approvals = dict(m.WorkflowInstance.objects.for_companies(ids).filter(state=m.WorkflowInstance.State.PENDING)
                     .values('company_id').annotate(n=Count('id')).values_list('company_id', 'n'))
    tickets = dict(m.HelpdeskTicket.objects.for_companies(ids).filter(status__in=ACTIVE_TICKET)
                   .values('company_id').annotate(n=Count('id')).values_list('company_id', 'n'))
    latest_run = (m.PayrollRun.objects.filter(company=OuterRef('pk'), run_type=m.PayrollRun.RunType.REGULAR)
                  .order_by('-period__bs_year', '-period__bs_month', '-created_at'))
    companies = (m.Company.objects.filter(pk__in=ids)
                 .annotate(payroll_status=Subquery(latest_run.values('status')[:1]),
                           payroll_year=Subquery(latest_run.values('period__bs_year')[:1]),
                           payroll_month=Subquery(latest_run.values('period__bs_month')[:1])))
    rows = []
    for c in companies:
        a = attendance.get(c.id, {})
        rows.append({
            'id': str(c.id), 'code': c.code, 'name': c.name_en, 'active': str(c.id) == user.company_id,
            'headcount': headcount.get(c.id, 0), 'present': a.get('present', 0), 'onLeave': a.get('on_leave', 0),
            'absent': a.get('absent', 0), 'pendingApprovals': approvals.get(c.id, 0), 'openTickets': tickets.get(c.id, 0),
            'payroll': ({'status': c.payroll_status, 'bsYear': c.payroll_year, 'bsMonth': c.payroll_month}
                        if c.payroll_status else None),
        })
    rows.sort(key=lambda r: collate_key(r['name']))
    keys = ('headcount', 'present', 'onLeave', 'absent', 'pendingApprovals', 'openTickets')
    totals = {k: sum(r[k] for r in rows) for k in keys}
    return {'date': today.isoformat(), 'companies': rows, 'totals': totals}


# ---------------------------------------------------------------- group administration

def _require_admin(user: AuthUser) -> None:
    if user.must_change_password:
        raise ForbiddenError('password change required')
    if not has_role(user, 'admin'):
        raise ForbiddenError('requires role admin')


def _group_code(company_code: str) -> str:
    base = re.sub(r'[^A-Z0-9_-]', '', company_code.upper()).lstrip('_-')[:15] or 'GROUP'
    code, n = f'{base}-GRP', 1
    while _m().CompanyGroup.objects.filter(code=code).exists():
        n += 1
        code = f'{base}-GRP{n}'
    return code


def _ensure_group(actor: AuthUser):
    """The active company's group, created (named after the company) the first time it gets a sister company; its
    creator becomes the first group admin. A company already in a group gets sisters only from a group admin."""
    m = _m()
    company = m.Company.objects.select_for_update().get(pk=actor.company_id)
    if company.group_id:
        if not is_group_admin(actor):
            raise ForbiddenError('only a group admin can add companies to the group')
        return company.group
    group = m.CompanyGroup.objects.create(code=_group_code(company.code), name_en=f'{company.name_en} group')
    company.group = group
    company.save(update_fields=['group'])
    m.GroupMember.objects.create(user_id=actor.id, group=group, roles=['group_admin'], created_by_id=actor.id)
    audit('create', 'company_group', actor_id=actor.id, row_id=str(group.id),
          after={'code': group.code, 'name': group.name_en, 'companies': [company.code]})
    audit('create', 'group_member', actor_id=actor.id, row_id=f'{actor.id}:{group.id}', after={'roles': ['group_admin']})
    return group


@transaction.atomic
def create_group_company(actor: AuthUser, data: dict) -> dict:
    """Add a company to the active company's group (data: code, name_en, name_ne?). It gets the standard defaults
    (leave types, approval flows, settings, every module's seeds) and the actor becomes its admin."""
    _require_admin(actor)
    from hajiri.company_settings.seed import create_company
    m = _m()
    group = _ensure_group(actor)
    code = data['code'].strip().upper()
    if m.Company.objects.filter(code__iexact=code).exists():
        raise ConflictError('a company with this code already exists')
    c = create_company(code, data['name_en'], data.get('name_ne') or None)
    m.Company.objects.filter(pk=c['id']).update(group=group)
    m.CompanyMembership.objects.create(user_id=actor.id, company_id=c['id'], roles=['admin'], created_by_id=actor.id)
    audit('create', 'company', actor_id=actor.id, row_id=c['id'],
          after={'code': code, 'name': data['name_en'], 'groupId': str(group.id)})
    return {'id': c['id'], 'code': code, 'name': data['name_en'], 'groupId': str(group.id)}


def group_users(actor: AuthUser):
    """Accounts anywhere in the actor's group (home company, membership in one of its companies, or a group role)."""
    m = _m()
    group_id = m.Company.objects.filter(pk=actor.company_id).values_list('group_id', flat=True).first()
    if not group_id:
        return m.AppUser.objects.none()
    in_group = m.CompanyMembership.objects.filter(user=OuterRef('pk'), company__group_id=group_id)
    group_role = m.GroupMember.objects.filter(user=OuterRef('pk'), group_id=group_id)
    return m.AppUser.objects.filter(Q(company__group_id=group_id) | Exists(in_group) | Exists(group_role))


def list_memberships(actor: AuthUser) -> list[dict]:
    """People of other companies of the group who can work in the actor's (active) company."""
    _require_admin(actor)
    m = _m()
    rows = (m.CompanyMembership.objects.for_company(actor.company_id).select_related('user__company', 'employee')
            .order_by('created_at'))
    out = [{'userId': str(r.user_id), 'name': r.user.name, 'email': r.user.email, 'roles': list(r.roles), 'status': r.status,
            'homeCompany': r.user.company.name_en if r.user.company_id else None,
            'employeeId': r.employee_id and str(r.employee_id),
            'employeeName': r.employee.name_en if r.employee_id else None, 'createdAt': r.created_at} for r in rows]
    out.sort(key=lambda r: collate_key(r['name']))
    return out


def _check_roles(roles) -> list[str]:
    roles = [r for r in roles if r]
    if not roles or any(r not in ROLES for r in roles):
        raise BadRequestError('choose at least one valid role')
    return list(dict.fromkeys(roles))


@transaction.atomic
def grant_access(actor: AuthUser, data: dict) -> dict:
    """Give a person of the group access to the actor's company (data: email, roles, employee_id?). Granting again
    updates the roles and employee link and re-activates a disabled access."""
    _require_admin(actor)
    m = _m()
    roles = _check_roles(data.get('roles') or [])
    target = group_users(actor).filter(email__iexact=(data.get('email') or '').strip()).first()
    if target is None:
        raise NotFoundError('no account with this email in your group')
    if target.company_id and str(target.company_id) == actor.company_id:
        raise BadRequestError('this account already belongs to this company; change its roles under Users')
    emp = data.get('employee_id')
    if emp:
        if not m.Employee.objects.for_company(actor.company_id).filter(pk=as_uuid(emp)).exists():
            raise NotFoundError('employee not found')
        if (m.AppUser.objects.filter(employee_id=emp).exists()
                or m.CompanyMembership.objects.filter(employee_id=emp).exclude(user_id=target.pk).exists()):
            raise ConflictError('this employee is already linked to another account')
    before = m.CompanyMembership.objects.filter(user_id=target.pk, company_id=actor.company_id).values('roles', 'status').first()
    try:
        with transaction.atomic():
            m.CompanyMembership.objects.update_or_create(
                user_id=target.pk, company_id=actor.company_id,
                defaults={'roles': roles, 'status': m.CompanyMembership.Status.ACTIVE, 'employee_id': emp or None},
                create_defaults={'roles': roles, 'employee_id': emp or None, 'created_by_id': actor.id})
    except IntegrityError as e:
        raise ConflictError('this employee is already linked to another account') from e
    audit('update' if before else 'create', 'company_membership', actor_id=actor.id, row_id=f'{target.pk}:{actor.company_id}',
          **({'before': before} if before else {}), after={'roles': roles, 'employeeId': emp or None})
    return {'userId': str(target.pk), 'roles': roles}


@transaction.atomic
def revoke_access(actor: AuthUser, user_id: str) -> None:
    """Take away a person's access to the actor's company; their sessions working in it go back to their home."""
    _require_admin(actor)
    m = _m()
    uid = as_uuid(user_id)
    if str(uid) == actor.id:
        raise BadRequestError('you cannot remove your own access')
    ms = m.CompanyMembership.objects.for_company(actor.company_id).filter(user_id=uid).first()
    if ms is None:
        raise NotFoundError('access not found')
    if 'admin' in ms.roles and not _other_active_admins(actor.company_id, exclude_user=uid):
        raise BadRequestError('this is the last active admin; add another admin first')
    before = {'roles': list(ms.roles), 'status': ms.status}
    m.CompanyMembership.objects.filter(user_id=uid, company_id=actor.company_id).delete()
    m.Session.objects.filter(user_id=uid, active_company_id=actor.company_id).update(active_company=None)
    audit('delete', 'company_membership', actor_id=actor.id, row_id=f'{uid}:{actor.company_id}', before=before)


def _other_active_admins(company_id: str, exclude_user) -> int:
    from .users import _active_admins_other_than
    return _active_admins_other_than(company_id, exclude_user)


# ---------------------------------------------------------------- group roles

def _require_group_admin(user: AuthUser) -> str:
    if user.must_change_password:
        raise ForbiddenError('password change required')
    if not user.group_id or not is_group_admin(user):
        raise ForbiddenError('requires role group_admin')
    return user.group_id


def _check_group_roles(roles) -> list[str]:
    roles = [r for r in roles if r]
    if not roles or any(r not in GROUP_ROLES for r in roles):
        raise BadRequestError('choose at least one valid group role')
    return [r for r in GROUP_ROLES if r in roles]


def list_group_roles(actor: AuthUser) -> list[dict]:
    """People with a group role in the actor's group (group admins only)."""
    group_id = _require_group_admin(actor)
    m = _m()
    rows = m.GroupMember.objects.filter(group_id=group_id).select_related('user__company')
    out = [{'userId': str(r.user_id), 'name': r.user.name, 'email': r.user.email, 'roles': list(r.roles),
            'homeCompany': r.user.company.name_en if r.user.company_id else None, 'status': r.user.status,
            'createdAt': r.created_at} for r in rows]
    out.sort(key=lambda r: collate_key(r['name']))
    return out


@transaction.atomic
def grant_group_role(actor: AuthUser, data: dict) -> dict:
    """Give a person of the group group roles (data: email, roles). Granting again replaces their group roles."""
    group_id = _require_group_admin(actor)
    m = _m()
    roles = _check_group_roles(data.get('roles') or [])
    target = group_users(actor).filter(email__iexact=(data.get('email') or '').strip()).first()
    if target is None:
        raise NotFoundError('no account with this email in your group')
    if str(target.pk) == actor.id and 'group_admin' not in roles:
        raise BadRequestError('you cannot remove your own group admin role')
    before = m.GroupMember.objects.filter(user_id=target.pk, group_id=group_id).values_list('roles', flat=True).first()
    m.GroupMember.objects.update_or_create(user_id=target.pk, group_id=group_id, defaults={'roles': roles},
                                           create_defaults={'roles': roles, 'created_by_id': actor.id})
    audit('update' if before else 'create', 'group_member', actor_id=actor.id, row_id=f'{target.pk}:{group_id}',
          **({'before': {'roles': list(before)}} if before else {}), after={'roles': roles})
    return {'userId': str(target.pk), 'roles': roles}


@transaction.atomic
def revoke_group_role(actor: AuthUser, user_id: str) -> None:
    """Take away all of a person's group roles; sessions working in a company they reached only through them go
    back to their home company on the next request."""
    group_id = _require_group_admin(actor)
    m = _m()
    uid = as_uuid(user_id)
    if str(uid) == actor.id:
        raise BadRequestError('you cannot remove your own group roles')
    gm = m.GroupMember.objects.select_for_update().filter(user_id=uid, group_id=group_id).first()
    if gm is None:
        raise NotFoundError('group role not found')
    gm_roles = list(gm.roles)
    m.GroupMember.objects.filter(user_id=uid, group_id=group_id).delete()
    audit('delete', 'group_member', actor_id=actor.id, row_id=f'{uid}:{group_id}', before={'roles': gm_roles})


@dataclass
class GroupSummary:
    group_id: str | None
    group_name: str | None
    companies: list[CompanyAccess] = field(default_factory=list)


def group_summary(user: AuthUser) -> GroupSummary:
    companies = my_companies(user)
    active = next((c for c in companies if c.active), None)
    return GroupSummary(active.group_id if active else None, active.group_name if active else None, companies)
