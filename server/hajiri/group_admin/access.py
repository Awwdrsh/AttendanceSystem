"""
Access of people to the companies of the group (/group/access), for group admins:

- every account that can work in a company of the group: its home accounts and the people given access to it
  (company_membership), with their roles and employee record there;
- give or change a person's roles and employee record in any company of the group (the home company's roles live
  on app_user, any other company's on company_membership);
- invite a new person (an account whose home is a company of the group, with a temporary password);
- remove a person's access to a company other than their home.

People with a group role reach every company through it; those roles are managed in the foundation's group-roles
section (hajiri.core.companies). Every change is audited.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q

from hajiri.core import companies as core_companies
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, create_user
from hajiri.core.collation import collate_key
from hajiri.core.crypto import password_policy_error
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.users import _active_admins_other_than
from hajiri.core.validation import plain, validate
from hajiri.db.models import AppUser, CompanyMembership, Employee, Session

from .common import GroupScope, group_scope
from .serializers import AccessIn, InviteIn


def _row(user: AppUser, company, roles, employee, status: str, source: str, created_at, is_default: bool = False) -> dict:
    return {'userId': str(user.pk), 'name': user.name, 'email': user.email, 'companyId': str(company.pk),
            'companyName': company.name_en, 'companyCode': company.code, 'roles': list(roles),
            'employeeId': employee and str(employee.pk), 'employeeName': employee.name_en if employee else None,
            'employeeCode': employee.emp_code if employee else None,
            'status': status, 'source': source, 'isDefault': is_default, 'createdAt': created_at}


def list_access(actor: AuthUser, company_id: str | None = None) -> list[dict]:
    """One row per person and company of the group (or of one company): home accounts first, then members."""
    scope = group_scope(actor)
    ids = [str(scope.company(company_id).pk)] if company_id else scope.ids
    out = []
    other_default = CompanyMembership.objects.filter(user=OuterRef('pk'), is_default=True)
    homes = (AppUser.objects.filter(company_id__in=ids).annotate(starts_elsewhere=Exists(other_default))
             .select_related('company', 'employee').order_by('created_at'))
    for u in homes:
        out.append(_row(u, u.company, u.roles, u.employee, u.status, 'home', u.created_at, not u.starts_elsewhere))
    members = (CompanyMembership.objects.for_companies(ids).select_related('user', 'company', 'employee')
               .order_by('created_at'))
    for ms in members:
        status = ms.status if ms.user.status == AppUser.Status.ACTIVE else ms.user.status
        out.append(_row(ms.user, ms.company, ms.roles, ms.employee, status, 'membership', ms.created_at, ms.is_default))
    out.sort(key=lambda r: (collate_key(r['name']), r['email'], r['source'] != 'home', collate_key(r['companyName'])))
    return out


def unlinked_employees(actor: AuthUser, company_id, keep: str | None = None) -> list[dict]:
    """Active employees of a group company that no account works as yet (for the employee select), plus `keep`."""
    scope = group_scope(actor)
    c = scope.company(company_id)
    free = Q(app_user__isnull=True, membership__isnull=True)
    if keep:
        free |= Q(pk=as_uuid(keep))
    qs = (Employee.objects.for_company(c.pk).filter(free, status=Employee.Status.ACTIVE)
          .order_by('name_en').values('id', 'name_en', 'emp_code')[:1000])
    rows = [{'id': str(r['id']), 'name': r['name_en'], 'code': r['emp_code']} for r in qs]
    rows.sort(key=lambda r: collate_key(r['name']))
    return rows


def _check_employee(company_id: str, employee_id: str | None, user_id) -> None:
    """The employee record must belong to that company and not be linked to another account."""
    if not employee_id:
        return
    if not Employee.objects.for_company(company_id).filter(pk=as_uuid(employee_id)).exists():
        raise NotFoundError('employee not found')
    taken = (AppUser.objects.filter(employee_id=employee_id).exclude(pk=user_id).exists()
             or CompanyMembership.objects.filter(employee_id=employee_id).exclude(user_id=user_id).exists())
    if taken:
        raise ConflictError('this employee is already linked to another account')


def _target(scope: GroupScope, actor: AuthUser, email: str) -> AppUser:
    target = (core_companies.group_users(actor).filter(email__iexact=(email or '').strip())
              .select_related('company').first())
    if target is None:
        raise NotFoundError('no account with this email in your group')
    return target


@transaction.atomic
def grant_access(actor: AuthUser, body: dict) -> dict:
    """Give a person of the group access to one company with these roles (and optionally their employee record
    there), or change what they already have there. Granting again re-activates a disabled access."""
    scope = group_scope(actor)
    b = plain(validate(AccessIn, body))
    company = scope.company(b['companyId'])
    cid = str(company.pk)
    roles = list(dict.fromkeys(b['roles']))
    emp = b.get('employeeId')
    target = _target(scope, actor, b['email'])
    _check_employee(cid, emp, target.pk)
    if target.company_id and str(target.company_id) == cid:
        return _change_home(actor, target, cid, roles, emp)
    before = (CompanyMembership.objects.select_for_update().filter(user_id=target.pk, company_id=cid)
              .values('roles', 'status', 'employee_id').first())
    if before and 'admin' in before['roles'] and 'admin' not in roles:
        _keep_an_admin(actor, cid, target.pk)
    try:
        with transaction.atomic():
            CompanyMembership.objects.update_or_create(
                user_id=target.pk, company_id=cid,
                defaults={'roles': roles, 'status': CompanyMembership.Status.ACTIVE, 'employee_id': emp},
                create_defaults={'roles': roles, 'employee_id': emp, 'created_by_id': actor.id})
    except IntegrityError as e:
        raise ConflictError('this employee is already linked to another account') from e
    after = {'roles': roles, 'employeeId': emp, 'status': CompanyMembership.Status.ACTIVE}
    if before:
        before = {'roles': list(before['roles']), 'status': before['status'],
                  'employeeId': before['employee_id'] and str(before['employee_id'])}
    audit('update' if before else 'create', 'company_membership', actor_id=actor.id, row_id=f'{target.pk}:{cid}',
          **({'before': before} if before else {}), after=after)
    return {'userId': str(target.pk), 'companyId': cid, 'roles': roles, 'employeeId': emp, 'source': 'membership'}


def _keep_an_admin(actor: AuthUser, company_id: str, user_id) -> None:
    if str(user_id) == actor.id:
        raise BadRequestError('you cannot remove your own admin role')
    if not _active_admins_other_than(company_id, user_id):
        raise BadRequestError('this is the last active admin; add another admin first')


def _change_home(actor: AuthUser, target: AppUser, company_id: str, roles: list[str], emp: str | None) -> dict:
    """Roles and employee record in the person's home company (app_user)."""
    u = AppUser.objects.select_for_update().get(pk=target.pk)
    before = {'roles': list(u.roles), 'employeeId': u.employee_id and str(u.employee_id)}
    if 'admin' in u.roles and 'admin' not in roles:
        _keep_an_admin(actor, company_id, u.pk)
    after = {'roles': roles, 'employeeId': emp if emp else before['employeeId']}
    if after != before:
        AppUser.objects.filter(pk=u.pk).update(roles=roles, employee_id=after['employeeId'])
        audit('update', 'app_user', actor_id=actor.id, row_id=str(u.pk), before=before, after=after)
    return {'userId': str(u.pk), 'companyId': company_id, 'roles': roles, 'employeeId': after['employeeId'], 'source': 'home'}


@transaction.atomic
def invite_user(actor: AuthUser, body: dict, pepper: str | None = None) -> dict:
    """A new account whose home is a company of the group, with a temporary password changed at first sign-in."""
    scope = group_scope(actor)
    b = plain(validate(InviteIn, body))
    company = scope.company(b['companyId'])
    cid = str(company.pk)
    email = b['email'].strip()
    if AppUser.objects.filter(email__iexact=email).exists():
        raise ConflictError('an account with this email already exists; give it access instead')
    err = password_policy_error(b['password'])
    if err:
        raise BadRequestError(err)
    _check_employee(cid, b.get('employeeId'), None)
    user = create_user(company_id=cid, employee_id=b.get('employeeId'), email=email, name=b['name'], password=b['password'],
                       roles=list(dict.fromkeys(b['roles'])), must_change_password=True, pepper=pepper, actor_id=actor.id)
    return {'userId': user.id, 'companyId': cid, 'email': user.email, 'roles': list(user.roles)}


@transaction.atomic
def revoke_access(actor: AuthUser, user_id, company_id) -> None:
    """Take away a person's access to a company of the group other than their home; their sessions working there
    go back to their home company on the next request."""
    scope = group_scope(actor)
    company = scope.company(company_id)
    cid = str(company.pk)
    uid = as_uuid(user_id)
    if str(uid) == actor.id:
        raise BadRequestError('you cannot remove your own access')
    ms = CompanyMembership.objects.select_for_update().for_company(cid).filter(user_id=uid).first()
    if ms is None:
        if AppUser.objects.filter(pk=uid, company_id=cid).exists():
            raise BadRequestError("this is the person's home company; disable the account under Users there instead")
        raise NotFoundError('access not found')
    if 'admin' in ms.roles and ms.status == CompanyMembership.Status.ACTIVE:
        _keep_an_admin(actor, cid, uid)
    before = {'roles': list(ms.roles), 'status': ms.status, 'employeeId': ms.employee_id and str(ms.employee_id)}
    CompanyMembership.objects.filter(user_id=uid, company_id=cid).delete()
    Session.objects.filter(user_id=uid, active_company_id=cid).update(active_company=None)
    audit('delete', 'company_membership', actor_id=actor.id, row_id=f'{uid}:{cid}', before=before)
