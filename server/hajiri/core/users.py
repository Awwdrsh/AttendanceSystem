"""
Account administration (port of the /auth/users handlers in apps/api/src/modules/auth/routes.ts). The pages that call
these check the role first (list: hr; create and update: admin), as the routes did; the rules here repeat the checks
so a caller cannot skip them.

Users of a company are the accounts whose home company it is plus the people of the group who were given access to
it (company_membership). For those, roles, status and employee link are the membership's, so changing them here never
touches what the person is in their own company. People with a group role (group_member) act in every company of
the group with the roles it gives (group_hr: hr, ...); `company_users`, `company_roles` and `user_ids_with_role`
count them, the Users page does not list them (group roles are managed on the group pages).
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import ExpressionWrapper, Q, Value
from django.db.models.functions import Lower
from django.db import models
from rest_framework import serializers

from .audit import audit
from .collation import collate_key
from .auth import GROUP_ROLE_GRANTS, ROLES, AuthUser, _merge_roles, create_user, has_role
from .crypto import hash_password, password_policy_error
from .errors import BadRequestError, ForbiddenError, NotFoundError
from .scoping import as_uuid
from .validation import validate


class CreateUserIn(serializers.Serializer):
    email = serializers.EmailField(max_length=200, trim_whitespace=False)
    name = serializers.CharField(min_length=1, max_length=120)
    password = serializers.CharField(max_length=256, trim_whitespace=False, allow_blank=True)
    roles = serializers.ListField(child=serializers.ChoiceField(ROLES), min_length=1)
    employeeId = serializers.UUIDField(required=False)


class UpdateUserIn(serializers.Serializer):
    roles = serializers.ListField(child=serializers.ChoiceField(ROLES), min_length=1, required=False)
    status = serializers.ChoiceField(('active', 'disabled'), required=False)
    employeeId = serializers.UUIDField(required=False, allow_null=True)


def _m():
    from hajiri.db import models as m
    return m


def _plain(data: dict) -> dict:
    """Validated data as the TS body had it (UUIDs as strings), for audit entries and callers."""
    return {k: (str(v) if hasattr(v, 'hex') else v) for k, v in data.items()}


def _require(user: AuthUser, role: str) -> None:
    if user.must_change_password:
        raise ForbiddenError('password change required')
    if not has_role(user, role):
        raise ForbiddenError(f'requires role {role}')


def assert_employee_in_company(employee_id: str | None, company_id: str | None) -> None:
    """A user can only be linked to an employee of the same company."""
    if employee_id and not _m().Employee.objects.for_company(company_id).filter(pk=as_uuid(employee_id)).exists():
        raise NotFoundError('employee not found')


def list_users(actor: AuthUser) -> list[dict]:
    """Everyone who can sign in to the actor's company: its own accounts, then people of the group with access
    (`membership: True`, with their roles here and their home company's name)."""
    _require(actor, 'hr')
    m = _m()
    cols = ('id', 'employee_id', 'email', 'name', 'roles', 'status', 'last_login_at', 'created_at', 'two_factor')
    own = list(m.AppUser.objects.home_of(actor.company_id)
               .annotate(two_factor=ExpressionWrapper(Q(totp_enabled_at__isnull=False), output_field=models.BooleanField()))
               .order_by('created_at').values(*cols))
    for r in own:
        r['membership'] = False
    members = (m.CompanyMembership.objects.for_company(actor.company_id).select_related('user__company')
               .order_by('created_at'))
    for ms in members:
        u = ms.user
        own.append({'id': u.id, 'employee_id': ms.employee_id, 'email': u.email, 'name': u.name, 'roles': list(ms.roles),
                    'status': ms.status if u.status == 'active' else u.status, 'last_login_at': u.last_login_at,
                    'created_at': ms.created_at, 'two_factor': u.totp_enabled_at is not None, 'membership': True,
                    'home_company': u.company.name_en if u.company_id else None})
    return own


@transaction.atomic
def admin_create_user(actor: AuthUser, body: dict, pepper: str | None = None) -> AuthUser:
    """New account with a temporary password the person must change at first sign-in."""
    _require(actor, 'admin')
    b = _plain(validate(CreateUserIn, body))
    assert_employee_in_company(b.get('employeeId'), actor.company_id)
    err = password_policy_error(b['password'])
    if err:
        raise BadRequestError(err)
    return create_user(company_id=actor.company_id, employee_id=b.get('employeeId'), email=b['email'], name=b['name'],
                       password=b['password'], roles=b['roles'], must_change_password=True, actor_id=actor.id,
                       pepper=pepper)


def _active_admins_other_than(company_id: str, user_id) -> int:
    m = _m()
    home = (m.AppUser.objects.home_of(company_id).filter(status='active', roles__contains=['admin'])
            .exclude(pk=user_id).count())
    members = (m.CompanyMembership.objects.for_company(company_id)
               .filter(status='active', roles__contains=['admin'], user__status='active').exclude(user_id=user_id).count())
    group_admins = sum(1 for uid, roles, _u in group_role_holders(company_id) if 'admin' in roles and str(uid) != str(user_id))
    return home + members + group_admins


def group_role_holders(company_id) -> list[tuple]:
    """(user_id, company roles given here, AppUser) for active people with a group role in the company's group whose
    home employee (if any) has not left."""
    from .clock import npt_today
    m = _m()
    rows = (m.GroupMember.objects.filter(group__companies=company_id, user__status='active')
            .exclude(user__employee__exit_date__lt=npt_today()).select_related('user'))
    out = []
    for gm in rows:
        granted = _merge_roles([], {GROUP_ROLE_GRANTS[r] for r in gm.roles if r in GROUP_ROLE_GRANTS})
        if granted:
            out.append((gm.user_id, granted, gm.user))
    return out


@transaction.atomic
def admin_update_user(actor: AuthUser, user_id: str, body: dict) -> dict:
    """Roles, status and employee link. Nobody disables or demotes themselves, and the last active admin stays."""
    _require(actor, 'admin')
    m = _m()
    b = _plain(validate(UpdateUserIn, body, partial=True))
    if str(user_id) == actor.id and (b.get('status') == 'disabled' or ('roles' in b and 'admin' not in b['roles'])):
        raise BadRequestError('you cannot disable your own account or remove your own admin role')
    assert_employee_in_company(b.get('employeeId'), actor.company_id)
    uid = as_uuid(user_id)
    target = m.AppUser.objects.home_of(actor.company_id).filter(pk=uid).values('roles', 'status').first()
    membership = None
    if target is None:
        membership = m.CompanyMembership.objects.for_company(actor.company_id).filter(user_id=uid).first()
        if membership is None:
            raise NotFoundError('user not found')
        target = {'roles': list(membership.roles), 'status': membership.status}
    loses_admin = ('admin' in target['roles'] and target['status'] == 'active'
                   and (('roles' in b and 'admin' not in b['roles']) or b.get('status') == 'disabled'))
    if loses_admin and _active_admins_other_than(actor.company_id, uid) == 0:
        raise BadRequestError('this is the last active admin; add another admin first')
    changes = {k: v for k, v in (('roles', b.get('roles')), ('status', b.get('status'))) if v is not None}
    if 'employeeId' in b:
        changes['employee_id'] = b['employeeId']
    if membership is None:
        m.AppUser.objects.filter(pk=uid, company_id=actor.company_id).update(**changes)
        r = m.AppUser.objects.filter(pk=uid).values('id', 'email', 'name', 'roles', 'status', 'employee_id').first()
        if b.get('status') == 'disabled':
            m.Session.objects.filter(user_id=uid, revoked_at__isnull=True).update(revoked_at=models.functions.Now())
    else:
        m.CompanyMembership.objects.filter(user_id=uid, company_id=actor.company_id).update(**changes)
        u = m.AppUser.objects.filter(pk=uid).values('id', 'email', 'name').first()
        ms = m.CompanyMembership.objects.filter(user_id=uid, company_id=actor.company_id).values('roles', 'status', 'employee_id').first()
        r = {**u, **ms}
        if b.get('status') == 'disabled':
            # Access to this company ends; sessions working in it fall back to the person's home company.
            m.Session.objects.filter(user_id=uid, active_company_id=actor.company_id).update(active_company=None)
    audit('update', 'company_membership' if membership else 'app_user', actor_id=actor.id, row_id=str(user_id),
          before=target, after=b)
    return r


def reset_password(email: str, password: str, pepper: str | None = None) -> str:
    """CLI reset: new password, lockout cleared, every session revoked. Returns the user id."""
    err = password_policy_error(password)
    if err:
        raise BadRequestError(err)
    m = _m()
    uid = m.AppUser.objects.filter(email=Lower(Value(email))).values_list('id', flat=True).first()
    if uid is None:
        raise NotFoundError('user not found')
    m.AppUser.objects.filter(pk=uid).update(password_hash=hash_password(password, pepper), failed_logins=0, locked_until=None)
    m.Session.objects.filter(user_id=uid).update(revoked_at=models.functions.Now())
    return str(uid)


# ---------------------------------------------------------------- people of a company (home accounts and members)

def company_users(company_id: str, *, active_only: bool = True) -> list[dict]:
    """Everyone who can act in a company, with their roles THERE: home accounts (app_user.roles) and members of the
    group with access (company_membership.roles). [{id, name, email, roles, status, employee_id, membership}],
    ordered by name (collation order, like the TS lists' ORDER BY name)."""
    m = _m()
    home = m.AppUser.objects.home_of(company_id)
    if active_only:
        home = home.filter(status='active')
    out = [{**r, 'membership': False} for r in home.values('id', 'name', 'email', 'roles', 'status', 'employee_id')]
    members = m.CompanyMembership.objects.for_company(company_id).select_related('user')
    if active_only:
        members = members.filter(status='active', user__status='active')
    for ms in members:
        out.append({'id': ms.user_id, 'name': ms.user.name, 'email': ms.user.email, 'roles': list(ms.roles),
                    'status': ms.status, 'employee_id': ms.employee_id, 'membership': True})
    by_id = {str(r['id']): r for r in out}
    for uid, granted, u in group_role_holders(company_id):
        r = by_id.get(str(uid))
        if r is not None:
            if r['status'] == 'active':
                r['roles'] = _merge_roles(r['roles'], granted)
        else:
            out.append({'id': uid, 'name': u.name, 'email': u.email, 'roles': granted, 'status': 'active',
                        'employee_id': None, 'membership': True, 'via_group': True})
    out.sort(key=lambda r: collate_key(r['name']))
    return out


def company_roles(company_id: str, user_id) -> list[str] | None:
    """The roles an ACTIVE user has in a company (home or membership), or None when they cannot act there."""
    m = _m()
    try:
        uid = as_uuid(user_id)
    except BadRequestError:
        return None
    roles = (m.AppUser.objects.home_of(company_id).filter(pk=uid, status='active').values_list('roles', flat=True).first())
    if roles is None:
        roles = (m.CompanyMembership.objects.for_company(company_id).filter(user_id=uid, status='active', user__status='active')
                 .values_list('roles', flat=True).first())
    granted = next((g for u, g, _ in group_role_holders(company_id) if u == uid), None)
    if granted is not None:
        return _merge_roles(list(roles or []), set(granted))
    return list(roles) if roles is not None else None


def user_ids_with_role(company_id: str, role: str) -> list[str]:
    """Active users holding a role in a company (admins always included), members of the group with access too; the
    multi-company version of notifications.user_ids_with_role."""
    return [str(r['id']) for r in company_users(company_id) if role in r['roles'] or 'admin' in r['roles']]
