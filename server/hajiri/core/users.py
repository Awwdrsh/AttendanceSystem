"""
Account administration (port of the /auth/users handlers in apps/api/src/modules/auth/routes.ts). The pages that call
these check the role first (list: hr; create and update: admin), as the routes did; the rules here repeat the checks
so a caller cannot skip them.
"""
from __future__ import annotations

from django.db import transaction

from .audit import audit
from .auth import ROLES, AuthUser, create_user, has_role
from .crypto import password_policy_error
from .db import execute, one, rows
from .errors import BadRequestError, ForbiddenError, NotFoundError
from .schema import Arr, Enum, Obj, Str, Uuid, parse

CREATE_USER = Obj({
    'email': Str(email=True, max=200), 'name': Str(trim=True, min=1, max=120), 'password': Str(max=256),
    'roles': Arr(Enum(ROLES), min=1), 'employeeId': Uuid(optional=True),
})
UPDATE_USER = Obj({
    'roles': Arr(Enum(ROLES), min=1, optional=True), 'status': Enum(('active', 'disabled'), optional=True),
    'employeeId': Uuid(nullish=True),
})


def _require(user: AuthUser, role: str) -> None:
    if user.must_change_password:
        raise ForbiddenError('password change required')
    if not has_role(user, role):
        raise ForbiddenError(f'requires role {role}')


def assert_employee_in_company(employee_id: str | None, company_id: str | None) -> None:
    """A user can only be linked to an employee of the same company."""
    if employee_id and not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id]):
        raise NotFoundError('employee not found')


def list_users(actor: AuthUser) -> list[dict]:
    _require(actor, 'hr')
    return rows("""SELECT id, employee_id, email, name, roles, status, last_login_at, created_at,
                          totp_enabled_at IS NOT NULL AS two_factor
                     FROM app_user WHERE company_id = %s ORDER BY created_at""", [actor.company_id])


@transaction.atomic
def admin_create_user(actor: AuthUser, body: dict, pepper: str | None = None) -> AuthUser:
    """New account with a temporary password the person must change at first sign-in."""
    _require(actor, 'admin')
    b = parse(CREATE_USER, body)
    assert_employee_in_company(b.get('employeeId'), actor.company_id)
    err = password_policy_error(b['password'])
    if err:
        raise BadRequestError(err)
    return create_user(company_id=actor.company_id, employee_id=b.get('employeeId'), email=b['email'], name=b['name'],
                       password=b['password'], roles=b['roles'], must_change_password=True, actor_id=actor.id,
                       pepper=pepper)


@transaction.atomic
def admin_update_user(actor: AuthUser, user_id: str, body: dict) -> dict:
    """Roles, status and employee link. Nobody disables or demotes themselves, and the last active admin stays."""
    _require(actor, 'admin')
    b = parse(UPDATE_USER, body)
    if str(user_id) == actor.id and (b.get('status') == 'disabled' or ('roles' in b and 'admin' not in b['roles'])):
        raise BadRequestError('you cannot disable your own account or remove your own admin role')
    assert_employee_in_company(b.get('employeeId'), actor.company_id)
    target = one('SELECT roles, status FROM app_user WHERE id = %s AND company_id = %s', [user_id, actor.company_id])
    if not target:
        raise NotFoundError('user not found')
    loses_admin = ('admin' in target['roles'] and target['status'] == 'active'
                   and (('roles' in b and 'admin' not in b['roles']) or b.get('status') == 'disabled'))
    if loses_admin:
        others = one("""SELECT count(*)::int AS n FROM app_user WHERE company_id = %s AND status = 'active'
                          AND 'admin' = ANY(roles) AND id <> %s""", [actor.company_id, user_id])
        if others['n'] == 0:
            raise BadRequestError('this is the last active admin; add another admin first')
    r = one("""UPDATE app_user SET roles = coalesce(%s, roles), status = coalesce(%s, status),
                      employee_id = CASE WHEN %s THEN %s::uuid ELSE employee_id END
                WHERE id = %s AND company_id = %s RETURNING id, email, name, roles, status, employee_id""",
            [b.get('roles'), b.get('status'), 'employeeId' in b, b.get('employeeId'), user_id, actor.company_id])
    if not r:
        raise NotFoundError('user not found')
    if b.get('status') == 'disabled':
        execute('UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL', [user_id])
    audit('update', 'app_user', actor_id=actor.id, row_id=str(user_id), before=target, after=b)
    return r


def reset_password(email: str, password: str, pepper: str | None = None) -> str:
    """CLI reset: new password, lockout cleared, every session revoked. Returns the user id."""
    from .crypto import hash_password
    err = password_policy_error(password)
    if err:
        raise BadRequestError(err)
    r = one('UPDATE app_user SET password_hash = %s, failed_logins = 0, locked_until = NULL WHERE email = lower(%s) RETURNING id',
            [hash_password(password, pepper), email])
    if not r:
        raise NotFoundError('user not found')
    execute('UPDATE session SET revoked_at = now() WHERE user_id = %s', [r['id']])
    return str(r['id'])
