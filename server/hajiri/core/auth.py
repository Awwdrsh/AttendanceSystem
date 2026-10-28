"""
Sign-in, sessions, two-step sign-in, the active company and access rules. Port of apps/api/src/modules/auth (service,
two-factor and the access helpers in middleware). Sessions live in the `session` table, so a session cookie works on
either stack.

Groups of companies: an account has a home company (app_user.company_id, with app_user.roles and employee_id), may
hold memberships in other companies (company_membership, with their own roles and employee) and may hold group roles
(group_member: group_admin, group_hr, group_executive), each standing for admin, hr or executive in EVERY company of
that group. `company_access` merges the three into the companies a person can work in. Each session works in one
company at a time, `session.active_company_id` (None: home). AuthUser.company_id, roles and employee_id are always
those of the ACTIVE company, so every company-scoped query keeps using `company_id_of(request.hajiri_user)`;
`switch_company` changes it.

Leavers lose access per company: a membership whose employee has left no longer counts, and when the home employee
has left the home company and the group roles stop counting; the person can still sign in while another company is
left (a single-company leaver cannot sign in at all, as before).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.db import models, transaction
from django.db.models import Case, Count, Exists, ExpressionWrapper, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Lower, Now

from .audit import audit
from .clock import now, npt_today
from .crypto import field_cipher, hash_password, random_token, sha256_hex, verify_password
from .scoping import as_uuid
from .errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from .totp import match_totp, new_recovery_codes, new_totp_secret, normalise_recovery_code, otpauth_uri

Role = Literal['admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive']
# admin and hr see everything; the specialist roles split HR work by module (payroll, attendance and leave, hiring,
# read-only executive dashboards); manager covers the people a user manages (line reports, department, branch).
ROLES: tuple[str, ...] = ('admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive')

SESSION_COOKIE = 'hajiri_session'
TWO_FACTOR_SETUP_MESSAGE = 'two-step sign-in setup required'
SESSION_TTL = timedelta(hours=12)      # overall (plan §12.4 AAL3 ceiling)
SESSION_IDLE = timedelta(hours=1)
MAX_FAILED = 10
LOCK = timedelta(minutes=15)
# The second sign-in step must follow the password within 5 minutes and allows 5 tries.
CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_MAX_ATTEMPTS = 5
_DUMMY_HASH = ('scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$'
               'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
_EXPIRED = 'This sign-in has expired. Enter your password again.'
_LOCKED = 'account temporarily locked; try again later'

# Who may work in each HR area besides hr and admin. Specialist roles never see other areas; executive is read-only.
MODULE_ROLES: dict[str, tuple[str, ...]] = {
    'payroll': ('hr', 'payroll_admin'),
    'attendance': ('hr', 'attendance_admin'),
    'hiring': ('hr', 'recruiter'),
    'insights': ('hr', 'executive'),
}


# Group roles (group_member.roles) and the company role each one gives in every company of the group.
GROUP_ROLES: tuple[str, ...] = ('group_admin', 'group_hr', 'group_executive')
GROUP_ROLE_GRANTS: dict[str, str] = {'group_admin': 'admin', 'group_hr': 'hr', 'group_executive': 'executive'}


def _models():
    from hajiri.db import models as m
    return m


@dataclass(frozen=True)
class AuthUser:
    id: str
    company_id: str | None            # the ACTIVE company of this session
    employee_id: str | None           # the person's employee record in the active company
    email: str
    name: str
    roles: tuple[str, ...]            # roles in the active company
    must_change_password: bool
    two_factor_enabled: bool
    # The company requires two-step sign-in for one of the user's roles and it is not set up yet.
    must_set_up_two_factor: bool
    # The account's own company (app_user.company_id); the active company differs after switch_company.
    home_company_id: str | None = None
    # Every company this person can work in (home first), for the company switcher and group-wide views.
    company_ids: tuple[str, ...] = ()
    # The same companies with name, code and the person's roles there.
    companies: tuple['CompanyRef', ...] = ()
    group_id: str | None = None             # the group of the active company (None: standalone)
    group_roles: tuple[str, ...] = ()       # the person's group roles in that group

    @property
    def home(self) -> str | None:
        return self.home_company_id or self.company_id

    @property
    def multi_company(self) -> bool:
        return len(self.company_ids) > 1

    @property
    def active_company(self) -> 'CompanyRef | None':
        return self.company(self.company_id)

    def company(self, company_id) -> 'CompanyRef | None':
        """What the person is in one of their companies (None: no access there)."""
        cid = str(company_id) if company_id else None
        return next((c for c in self.companies if c.id == cid), None)

    def has_group_role(self, *roles: str) -> bool:
        """group_admin always counts, like admin for company roles."""
        return 'group_admin' in self.group_roles or any(r in self.group_roles for r in roles)


@dataclass(frozen=True)
class CompanyRef:
    """One company a person can work in, and what they are there."""
    id: str
    code: str
    name: str
    roles: tuple[str, ...]                  # own roles there plus those given by group roles
    employee_id: str | None = None
    home: bool = False
    group_id: str | None = None
    group_name: str | None = None
    via_group: bool = False                 # access only through a group role (no own account or membership there)
    is_default: bool = False                # a fresh sign-in starts here

    def has_role(self, *roles: str) -> bool:
        return 'admin' in self.roles or any(r in self.roles for r in roles)


@dataclass(frozen=True)
class LoginResult:
    user: AuthUser | None = None
    token: str | None = None
    challenge: str | None = None
    method: str | None = None

    @property
    def two_factor_required(self) -> bool:
        return self.challenge is not None


@dataclass(frozen=True)
class SessionMeta:
    ip: str | None = None
    user_agent: str | None = None


def _effective_roles(roles, is_head: bool) -> tuple[str, ...]:
    """A department or branch head manages people even when the account was not given the manager role."""
    roles = tuple(roles)
    return roles + ('manager',) if is_head and 'manager' not in roles else roles


def _policy_roles(value) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _member_companies(user_ref, today):
    """Active memberships whose employee (if any) has not left."""
    m = _models()
    return (m.CompanyMembership.objects.filter(user_id=user_ref, status=m.CompanyMembership.Status.ACTIVE)
            .exclude(employee__exit_date__lt=today))


def _merge_roles(own, granted) -> list[str]:
    out = list(own)
    out += [r for r in ROLES if r in granted and r not in out]
    return out


def _home_left(u, today) -> bool:
    return bool(u.employee_id) and u.employee.exit_date is not None and u.employee.exit_date < today


def company_access(u, today=None, *, has_memberships: bool | None = None,
                   has_group_roles: bool | None = None) -> tuple[list[CompanyRef], dict[str, tuple[str, ...]]]:
    """
    The companies an app_user row can work in (home first, then by name) and its group roles by group id:
    the home company (unless the home employee has left), active memberships whose employee has not left, and every
    company of a group the person holds a group role in (merged into the roles of a company they already have).
    has_memberships / has_group_roles=False skip the queries when the caller already knows there are none.
    """
    from .collation import collate_key
    m = _models()
    today = today or npt_today()
    home_left = _home_left(u, today)
    refs: dict[str, dict] = {}

    def add(c, **kw):
        refs[str(c.id)] = {'id': str(c.id), 'code': c.code, 'name': c.name_en, 'group_id': c.group_id and str(c.group_id),
                           'group_name': c.group.name_en if c.group_id else None, **kw}

    if u.company_id and not home_left:
        add(u.company, roles=list(u.roles), employee_id=u.employee_id and str(u.employee_id), home=True)
    members = _member_companies(u.pk, today).select_related('company__group') if has_memberships is not False else ()
    for ms in members:
        if str(ms.company_id) not in refs:
            add(ms.company, roles=list(ms.roles), employee_id=ms.employee_id and str(ms.employee_id),
                is_default=ms.is_default)
    groups: dict[str, tuple[str, ...]] = {}
    if not home_left and has_group_roles is not False:
        groups = {str(g): tuple(r) for g, r in m.GroupMember.objects.filter(user_id=u.pk).values_list('group_id', 'roles')}
    if groups:
        for c in m.Company.objects.filter(group_id__in=list(groups)).select_related('group'):
            granted = {GROUP_ROLE_GRANTS[r] for r in groups[str(c.group_id)] if r in GROUP_ROLE_GRANTS}
            if str(c.id) in refs:
                refs[str(c.id)]['roles'] = _merge_roles(refs[str(c.id)]['roles'], granted)
            else:
                add(c, roles=_merge_roles([], granted), employee_id=None, via_group=True)
    out = [CompanyRef(**{**r, 'roles': tuple(r['roles'])}) for r in refs.values()]
    out.sort(key=lambda r: (not r.home, collate_key(r.name)))
    return out, groups


def _pick_active(refs: list[CompanyRef], requested) -> CompanyRef | None:
    """The session's company if the person may still work there, else home, else the default membership, else the
    first company left."""
    want = str(requested) if requested else None
    return (next((r for r in refs if r.id == want), None) or next((r for r in refs if r.home), None)
            or next((r for r in refs if r.is_default), None) or (refs[0] if refs else None))


def _auth_user(u, *, is_head: bool, required: list[str], company_id=None, roles=None, employee_id=None,
               company_ids: tuple[str, ...] = (), also_required: bool = False, companies: tuple = (),
               group_id=None, group_roles: tuple[str, ...] = ()) -> AuthUser:
    """AuthUser for an app_user row, in its home company unless company_id/roles/employee_id say otherwise."""
    home = u.company_id and str(u.company_id)
    active = str(company_id) if company_id else home
    raw_roles = list(roles if roles is not None else u.roles)
    emp = employee_id if roles is not None else u.employee_id
    return AuthUser(
        id=str(u.id), company_id=active, employee_id=emp and str(emp), email=u.email, name=u.name,
        roles=_effective_roles(raw_roles, is_head), must_change_password=u.must_change_password,
        two_factor_enabled=u.totp_enabled_at is not None,
        must_set_up_two_factor=u.totp_enabled_at is None and (is_two_factor_required(raw_roles, required) or also_required),
        home_company_id=home, company_ids=company_ids or ((home,) if home else ()), companies=tuple(companies),
        group_id=group_id, group_roles=tuple(group_roles))


def _user_in(u, requested_company, today=None, hints: dict | None = None) -> AuthUser | None:
    """The AuthUser of an account working in `requested_company` (or its fallback, see _pick_active). None when
    the person has left every company they had. `hints` (from resolve_session's single query) saves the queries
    for the common case: no memberships, no group roles, working at home."""
    m = _models()
    today = today or npt_today()
    hints = hints or {}
    refs, groups = company_access(u, today, has_memberships=hints.get('memberships'), has_group_roles=hints.get('group'))
    if not refs and _home_left(u, today):
        return None
    active = _pick_active(refs, requested_company)
    home = str(u.company_id) if u.company_id else None
    company_ids = tuple(r.id for r in refs)
    if active is None:          # an account without any company (only in tests and setup)
        return _auth_user(u, is_head=False, required=required_two_factor_roles(home), company_ids=company_ids)
    home_ref = next((r for r in refs if r.home), None)
    if active.home and 'home_policy' in hints:
        policy = {active.id: _policy_roles(hints['home_policy'])}
        head = bool(hints.get('home_head'))
    else:
        wanted = [active.id] + ([home] if home and home != active.id else [])
        policy = {str(c): _policy_roles(v) for c, v in m.CompanySetting.objects
                  .filter(company_id__in=wanted, key='security.policy').values_list('company_id', 'value__require2faRoles')}
        head = bool(active.employee_id) and m.Employee.objects.filter(pk=active.employee_id).filter(IsHeadExpr(F('id'))).exists()
    also = bool(home_ref and home_ref.id != active.id and is_two_factor_required(home_ref.roles, policy.get(home_ref.id, [])))
    return _auth_user(u, is_head=head, required=policy.get(active.id, []), company_id=active.id, roles=active.roles,
                      employee_id=active.employee_id, company_ids=company_ids, also_required=also, companies=refs,
                      group_id=active.group_id, group_roles=groups.get(active.group_id, ()) if active.group_id else ())


def has_role(user: AuthUser, *roles: str) -> bool:
    return 'admin' in user.roles or any(r in user.roles for r in roles)


def has_module(user: AuthUser, module: str) -> bool:
    return has_role(user, *MODULE_ROLES[module])


# ---------------------------------------------------------------- sign-in

def _start_session(u, meta: SessionMeta, two_factor: bool) -> LoginResult:
    """A new session in the person's sign-in company: the default membership if one is set, else home (or, for
    someone who has left the home company, the first company left)."""
    m = _models()
    today = npt_today()
    refs, _ = company_access(u, today)
    start = next((r for r in refs if r.is_default), None) or next((r for r in refs if r.home), None) or (refs[0] if refs else None)
    active = start.id if start and not start.home else None
    token = random_token()
    m.AppUser.objects.filter(pk=u.pk).update(failed_logins=0, locked_until=None, last_login_at=Now())
    m.Session.objects.create(token_hash=sha256_hex(token), user_id=u.pk, expires_at=now() + SESSION_TTL, ip=meta.ip,
                             user_agent=(meta.user_agent or '')[:200] or None, two_factor=two_factor,
                             active_company_id=active)
    audit('login', 'app_user', actor_id=str(u.pk), row_id=str(u.pk), **({'after': {'twoFactor': True}} if two_factor else {}))
    return LoginResult(user=_user_in(u, active, today), token=token)


def _record_failed_login(user_id: str, action: str) -> None:
    # One UPDATE with F() so parallel guesses cannot undercount and slip past the lockout.
    _models().AppUser.objects.filter(pk=user_id).update(
        failed_logins=F('failed_logins') + 1,
        locked_until=Case(When(failed_logins__gte=MAX_FAILED - 1, then=ExpressionWrapper(Now() + LOCK, output_field=models.DateTimeField())),
                          default=F('locked_until')))
    audit(action, 'app_user', actor_id=str(user_id), row_id=str(user_id))


def _is_locked(u) -> bool:
    return u.locked_until is not None and u.locked_until > now()


def create_user(*, company_id: str | None, email: str, name: str, password: str, roles: list[str],
                employee_id: str | None = None, must_change_password: bool = False, pepper: str | None = None,
                actor_id: str | None = None) -> AuthUser:
    u = _models().AppUser.objects.create(company_id=company_id, employee_id=employee_id, email=Lower(Value(email)),
                                         name=name, password_hash=hash_password(password, pepper), roles=list(roles),
                                         must_change_password=must_change_password)
    u.refresh_from_db(fields=['email'])
    user = _auth_user(u, is_head=False, required=[])
    audit('create', 'app_user', actor_id=actor_id, row_id=user.id, after={'email': user.email, 'roles': list(user.roles)})
    return user


# Not atomic: failed attempts must stay counted when the error is raised.
def login(email: str, password: str, meta: SessionMeta = SessionMeta(), pepper: str | None = None) -> LoginResult:
    """
    Password step. With two-step sign-in on, no session is created: the caller gets a single-use challenge for the
    second step, and failed logins are only cleared once the second step succeeds.
    """
    m = _models()
    u = m.AppUser.objects.select_related('company__group', 'employee').filter(email=Lower(Value(email))).first()
    # Constant-ish time: verify against a dummy hash when the user does not exist.
    ok = verify_password(password, u.password_hash if u else _DUMMY_HASH, pepper) and u is not None
    if not u or u.status != m.AppUser.Status.ACTIVE:
        raise UnauthorizedError('invalid email or password')
    if _is_locked(u):
        raise UnauthorizedError(_LOCKED)
    if not ok:
        _record_failed_login(u.pk, 'login_failed')
        raise UnauthorizedError('invalid email or password')
    # Someone who has left (exit date before today, Nepal time) can no longer sign in; HR clears the exit date to
    # restore access. A person of a group keeps signing in while they still work in another company.
    if _home_left(u, npt_today()) and not company_access(u)[0]:
        raise UnauthorizedError('this account was closed when you left the company; contact HR if you need your records')
    if u.totp_enabled_at:
        challenge = random_token()
        m.LoginChallenge.objects.create(token_hash=sha256_hex(challenge), user_id=u.pk, expires_at=now() + CHALLENGE_TTL,
                                        ip=meta.ip, user_agent=(meta.user_agent or '')[:200] or None)
        audit('login_password_ok', 'app_user', actor_id=str(u.pk), row_id=str(u.pk))
        return LoginResult(challenge=challenge)
    return _start_session(u, meta, False)


# Not atomic: failed attempts must stay counted when the error is raised.
def complete_two_factor_login(challenge: str, code: str, meta: SessionMeta = SessionMeta()) -> LoginResult:
    """Second step: a code from the authenticator app (or a recovery code) turns the challenge into a session."""
    m = _models()
    h = sha256_hex(challenge)
    # Count the attempt first, in one conditional UPDATE, so parallel guesses share the five tries.
    live = m.LoginChallenge.objects.filter(token_hash=h, used_at__isnull=True, expires_at__gt=Now(),
                                           attempts__lt=CHALLENGE_MAX_ATTEMPTS)
    if live.update(attempts=F('attempts') + 1) == 0:
        raise UnauthorizedError(_EXPIRED)
    user_id = m.LoginChallenge.objects.filter(token_hash=h).values_list('user_id', flat=True).first()
    u = m.AppUser.objects.select_related('company__group', 'employee').filter(pk=user_id).first()
    if not u or u.status != m.AppUser.Status.ACTIVE or not u.totp_enabled_at:
        raise UnauthorizedError(_EXPIRED)
    if _is_locked(u):
        raise UnauthorizedError(_LOCKED)
    method = consume_code(u.pk, code)
    if not method:
        _record_failed_login(u.pk, 'login_2fa_failed')
        raise UnauthorizedError('That code is not correct. Use the newest code from your authenticator app, or a recovery code.')
    if m.LoginChallenge.objects.filter(token_hash=h, used_at__isnull=True).update(used_at=Now()) == 0:
        raise UnauthorizedError(_EXPIRED)
    s = _start_session(u, meta, True)
    return LoginResult(user=s.user, token=s.token, method=method)


def resolve_session(token: str) -> AuthUser | None:
    """The signed-in user of a session token, in the session's active company; None when the session is missing,
    expired, idle for more than an hour, revoked, or the person is disabled or has left every company they had."""
    m = _models()
    h = sha256_hex(token)
    today = npt_today()
    s = (m.Session.objects.select_related('user__company__group', 'user__employee')
         .filter(token_hash=h, revoked_at__isnull=True, expires_at__gt=Now(), user__status=m.AppUser.Status.ACTIVE)
         .annotate(has_memberships=Exists(_member_companies(OuterRef('user_id'), today)),
                   has_group_roles=Exists(m.GroupMember.objects.filter(user_id=OuterRef('user_id'))),
                   home_policy=Subquery(m.CompanySetting.objects.filter(company_id=OuterRef('user__company_id'),
                                                                         key='security.policy')
                                        .values('value__require2faRoles')[:1], output_field=models.JSONField()),
                   home_head=IsHeadExpr(F('user__employee_id')))
         .first())
    if s is None:
        return None
    if now() - s.last_seen_at > SESSION_IDLE:
        m.Session.objects.filter(token_hash=h).update(revoked_at=Now())
        return None
    user = _user_in(s.user, s.active_company_id, today, hints={
        'memberships': s.has_memberships, 'group': s.has_group_roles, 'home_policy': s.home_policy,
        'home_head': s.home_head})
    if user is None:
        return None
    m.Session.objects.filter(token_hash=h).update(last_seen_at=Now())
    return user


def logout(token: str) -> None:
    _models().Session.objects.filter(token_hash=sha256_hex(token)).update(revoked_at=Now())


@transaction.atomic
def change_password(user_id: str, current: str, new: str, pepper: str | None = None) -> None:
    m = _models()
    u = m.AppUser.objects.filter(pk=user_id).first()
    if not u or not verify_password(current, u.password_hash, pepper):
        raise UnauthorizedError('current password is incorrect')
    if current == new:
        raise BadRequestError('the new password must be different from the current one')
    m.AppUser.objects.filter(pk=user_id).update(password_hash=hash_password(new, pepper), must_change_password=False)
    m.Session.objects.filter(user_id=user_id, revoked_at__isnull=True).update(revoked_at=Now())
    audit('password_change', 'app_user', actor_id=str(user_id), row_id=str(user_id))


# ---------------------------------------------------------------- the active company

@transaction.atomic
def switch_company(user: AuthUser, token: str, company_id: str) -> AuthUser:
    """Work in another of the person's companies from now on (this session only). A company the person has no access
    to is 'company not found', so ids of other tenants cannot be probed."""
    cid = str(company_id)
    if cid not in user.company_ids:
        raise NotFoundError('company not found')
    if cid == user.company_id:
        return user
    m = _models()
    m.Session.objects.filter(token_hash=sha256_hex(token), user_id=user.id).update(
        active_company_id=None if cid == user.home_company_id else cid)
    audit('company_switch', 'session', actor_id=user.id, row_id=user.id,
          before={'companyId': user.company_id}, after={'companyId': cid})
    switched = resolve_session(token)
    if switched is None:
        raise UnauthorizedError()
    return switched


@transaction.atomic
def set_default_company(user: AuthUser, company_id: str) -> None:
    """The company a fresh sign-in starts in: the home company, or one held by membership. Access through a group
    role alone cannot be the default (there is no access row to mark)."""
    m = _models()
    ref = user.company(company_id)
    if ref is None:
        raise NotFoundError('company not found')
    if not ref.home and ref.via_group:
        raise BadRequestError('only a company you were given access to can be where you start')
    before = next((c.id for c in user.companies if c.is_default), user.home_company_id)
    m.CompanyMembership.objects.filter(user_id=user.id, is_default=True).exclude(company_id=ref.id).update(is_default=False)
    if not ref.home:
        m.CompanyMembership.objects.filter(user_id=user.id, company_id=ref.id).update(is_default=True)
    audit('update', 'app_user', actor_id=user.id, row_id=user.id, before={'defaultCompanyId': before},
          after={'defaultCompanyId': ref.id})


# ---------------------------------------------------------------- active sessions

@dataclass(frozen=True)
class SessionInfo:
    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    device: str
    two_factor: bool
    current: bool


def describe_user_agent(ua: str | None) -> str:
    """A short, human description of a user agent ("Chrome on Android"); nothing beyond the original header is stored."""
    if not ua:
        return 'Unknown device'
    s = lambda p: re.search(p, ua) is not None
    browser = ('Edge' if s(r'Edg/') else 'Opera' if s(r'OPR/|Opera') else 'Firefox' if s(r'Firefox/')
               else 'Chrome' if s(r'Chrome/') else 'Safari' if s(r'Safari/') else 'Browser')
    os_ = ('Android' if s('Android') else 'iOS' if s('iPhone|iPad|iPod') else 'Windows' if s('Windows')
           else 'macOS' if s('Mac OS X|Macintosh') else 'ChromeOS' if s('CrOS') else 'Linux' if s('Linux') else None)
    return f'{browser} on {os_}' if os_ else browser


def list_sessions(user_id: str, current_token: str | None) -> list[SessionInfo]:
    cur = sha256_hex(current_token) if current_token else ''
    found = (_models().Session.objects
             .filter(user_id=user_id, revoked_at__isnull=True, expires_at__gt=Now(), last_seen_at__gt=Now() - SESSION_IDLE)
             .annotate(current=ExpressionWrapper(Q(token_hash=cur), output_field=models.BooleanField()))
             .order_by(F('current').desc(), '-last_seen_at'))
    return [SessionInfo(str(x.id), x.created_at, x.last_seen_at, x.expires_at, x.ip, describe_user_agent(x.user_agent),
                        x.two_factor, x.current) for x in found]


@transaction.atomic
def revoke_session(user_id: str, session_id: str, current_token: str | None) -> None:
    """Sign out one of my other sessions (the current one signs out with logout)."""
    m = _models()
    s = m.Session.objects.filter(id=as_uuid(session_id), user_id=user_id, revoked_at__isnull=True).first()
    if not s:
        raise NotFoundError('session not found')
    if current_token and s.token_hash == sha256_hex(current_token):
        raise BadRequestError('this is the session you are using; sign out instead')
    m.Session.objects.filter(id=session_id, user_id=user_id).update(revoked_at=Now())
    audit('session_revoke', 'session', actor_id=str(user_id), row_id=str(session_id))


@transaction.atomic
def revoke_other_sessions(user_id: str, current_token: str | None) -> int:
    n = (_models().Session.objects.filter(user_id=user_id, revoked_at__isnull=True)
         .exclude(token_hash=sha256_hex(current_token) if current_token else '').update(revoked_at=Now()))
    audit('session_revoke_others', 'session', actor_id=str(user_id), row_id=str(user_id), after={'count': n})
    return n


# ---------------------------------------------------------------- two-step sign-in

def _secret_aad(user_id: str) -> str:
    return f'totp:{user_id}'


def _recovery_hash(user_id: str, code: str) -> str:
    return sha256_hex(f'recovery:{user_id}:{normalise_recovery_code(code)}')


def required_two_factor_roles(company_id: str | None) -> list[str]:
    """Roles the company requires two-step sign-in for (Settings → security.policy)."""
    if not company_id:
        return []
    v = (_models().CompanySetting.objects.filter(company_id=company_id, key='security.policy')
         .values_list('value__require2faRoles', flat=True).first())
    return _policy_roles(v)


def is_two_factor_required(roles, required) -> bool:
    return any(r in required for r in roles)


@dataclass(frozen=True)
class TwoFactorStatus:
    enabled: bool
    enabled_at: datetime | None
    recovery_codes_left: int
    required: bool
    setup_pending: bool


def two_factor_status(user: AuthUser) -> TwoFactorStatus:
    u = (_models().AppUser.objects.filter(pk=user.id)
         .annotate(left=Count('user_recovery_codes', filter=Q(user_recovery_codes__used_at__isnull=True)))
         .only('totp_enabled_at', 'totp_pending_enc').first())
    if not u:
        raise NotFoundError('user not found')
    enabled = u.totp_enabled_at is not None
    return TwoFactorStatus(enabled, u.totp_enabled_at, u.left if enabled else 0,
                           is_two_factor_required(user.roles, required_two_factor_roles(user.company_id)),
                           u.totp_pending_enc is not None)


def begin_setup(user: AuthUser) -> dict[str, str]:
    """Start enrolment: a fresh secret kept as pending until a code from the app confirms it."""
    m = _models()
    if m.AppUser.objects.filter(pk=user.id, totp_enabled_at__isnull=False).exists():
        raise ConflictError('two-step sign-in is already on; turn it off first to move to a new phone')
    secret = new_totp_secret()
    m.AppUser.objects.filter(pk=user.id).update(totp_pending_enc=field_cipher().encrypt(secret, _secret_aad(user.id)))
    return {'secret': secret, 'otpauth_uri': otpauth_uri(secret, user.email)}


def _store_recovery_codes(user_id: str) -> list[str]:
    m = _models()
    codes = new_recovery_codes()
    m.UserRecoveryCode.objects.filter(user_id=user_id).delete()
    m.UserRecoveryCode.objects.bulk_create(
        [m.UserRecoveryCode(user_id=user_id, code_hash=_recovery_hash(user_id, c)) for c in codes])
    return codes


@transaction.atomic
def confirm_setup(user: AuthUser, code: str) -> list[str]:
    """Confirm enrolment with the first code from the app; returns the recovery codes (shown once)."""
    m = _models()
    u = m.AppUser.objects.filter(pk=user.id).only('totp_pending_enc', 'totp_enabled_at').first()
    if u and u.totp_enabled_at:
        raise ConflictError('two-step sign-in is already on')
    if not u or not u.totp_pending_enc:
        raise BadRequestError('start the setup first')
    step = match_totp(field_cipher().decrypt(bytes(u.totp_pending_enc), _secret_aad(user.id)), code)
    if step is None:
        raise BadRequestError('That code did not match. Check the time on your phone is automatic and try the newest code.')
    m.AppUser.objects.filter(pk=user.id).update(totp_secret_enc=F('totp_pending_enc'), totp_pending_enc=None,
                                                totp_enabled_at=Now(), totp_last_step=step)
    codes = _store_recovery_codes(user.id)
    audit('two_factor_enable', 'app_user', actor_id=user.id, row_id=user.id)
    return codes


def consume_code(user_id: str, code: str) -> Literal['totp', 'recovery'] | None:
    """
    Check a 6-digit code or a recovery code and consume it. A code step can be used once (totp_last_step only moves
    forward, in one conditional UPDATE), and a recovery code once. Returns how the user proved it, or None.
    """
    m = _models()
    c = code.strip()
    if re.fullmatch(r'\d{3}\s?\d{3}', c):
        secret = (m.AppUser.objects.filter(pk=user_id, totp_enabled_at__isnull=False)
                  .values_list('totp_secret_enc', flat=True).first())
        if not secret:
            return None
        step = match_totp(field_cipher().decrypt(bytes(secret), _secret_aad(str(user_id))), c)
        if step is None:
            return None
        n = (m.AppUser.objects.filter(pk=user_id).filter(Q(totp_last_step__isnull=True) | Q(totp_last_step__lt=step))
             .update(totp_last_step=step))
        return 'totp' if n > 0 else None
    if len(normalise_recovery_code(c)) != 12:
        return None
    n = (m.UserRecoveryCode.objects.filter(user_id=user_id, code_hash=_recovery_hash(str(user_id), c), used_at__isnull=True)
         .update(used_at=Now()))
    return 'recovery' if n > 0 else None


@transaction.atomic
def regenerate_recovery_codes(user: AuthUser, code: str) -> list[str]:
    if not two_factor_status(user).enabled:
        raise BadRequestError('two-step sign-in is off')
    if consume_code(user.id, code) is None:
        raise BadRequestError('That code did not match.')
    codes = _store_recovery_codes(user.id)
    audit('two_factor_recovery_codes', 'app_user', actor_id=user.id, row_id=user.id)
    return codes


def _clear_two_factor(user_id: str) -> None:
    m = _models()
    m.AppUser.objects.filter(pk=user_id).update(totp_secret_enc=None, totp_pending_enc=None, totp_enabled_at=None,
                                                totp_last_step=None)
    m.UserRecoveryCode.objects.filter(user_id=user_id).delete()
    m.LoginChallenge.objects.filter(user_id=user_id, used_at__isnull=True).update(used_at=Now())


@transaction.atomic
def disable_two_factor(user: AuthUser, password: str, code: str, pepper: str | None = None) -> None:
    """Turning it off needs the password and a current code; not allowed while the company requires it for the user's role."""
    st = two_factor_status(user)
    if not st.enabled:
        raise BadRequestError('two-step sign-in is already off')
    if st.required:
        raise ForbiddenError('your company requires two-step sign-in for your role; it cannot be turned off')
    pw = _models().AppUser.objects.filter(pk=user.id).values_list('password_hash', flat=True).first()
    if not pw or not verify_password(password, pw, pepper):
        raise UnauthorizedError('current password is incorrect')
    if consume_code(user.id, code) is None:
        raise BadRequestError('That code did not match.')
    _clear_two_factor(user.id)
    audit('two_factor_disable', 'app_user', actor_id=user.id, row_id=user.id)


@transaction.atomic
def resettable_users(actor: AuthUser):
    """Whose two-step sign-in the active company's admin may reset: its own accounts and members of the group with
    access here. Someone holding a group role works in every company of the group, so only a group admin resets them."""
    from .companies import is_group_admin
    m = _models()
    cid = actor.company_id
    qs = m.AppUser.objects.for_company(cid)
    if is_group_admin(actor):
        return qs
    member = m.CompanyMembership.objects.filter(user=OuterRef('pk'), company_id=cid,
                                                status=m.CompanyMembership.Status.ACTIVE)
    group_role = m.GroupMember.objects.filter(user=OuterRef('pk'), group__companies=cid)
    return qs.filter(Q(company_id=cid) | (Exists(member) & ~Exists(group_role)))


def admin_reset_two_factor(actor: AuthUser, target_id: str, reason: str) -> None:
    """Admin reset for a lost phone: two-step sign-in is cleared and the person is signed out everywhere."""
    m = _models()
    t = resettable_users(actor).filter(pk=target_id).only('id', 'totp_enabled_at').first()
    if not t:
        raise NotFoundError('user not found')
    if str(target_id) == actor.id:
        raise BadRequestError('use your own sign-in security page to change your two-step sign-in')
    _clear_two_factor(target_id)
    m.Session.objects.filter(user_id=target_id, revoked_at__isnull=True).update(revoked_at=Now())
    audit('two_factor_reset', 'app_user', actor_id=actor.id, row_id=str(target_id),
          before={'enabled': t.totp_enabled_at is not None}, after={'reason': reason})


# ---------------------------------------------------------------- access to employees

def IsHeadExpr(employee):
    from hajiri.db.models.base import IsHead
    return IsHead(employee)


def manages(viewer_employee_id, target=F('id'), on_date=None):
    """ORM filter expression for "the viewer manages this employee" (hj_manages, the rule of is_manager_of):

        Employee.objects.for_company(cid).filter(manages(user.employee_id))            # target: the row's id
        AttendanceDay.objects.filter(manages(user.employee_id, F('employee_id'), F('local_date')))
    """
    from hajiri.db.models.base import Manages
    return Manages(viewer_employee_id, target, on_date if on_date is not None else npt_today())


def is_manager_of(manager_employee_id: str, employee_id: str, on_date=None) -> bool:
    """
    Manager relationship on the given Nepal date (default today): the line manager, the head of the employee's
    department (or of a department above it) or the head of their branch. Defined once in SQL as hj_manages().
    """
    return _models().Employee.objects.filter(pk=employee_id).filter(
        manages(manager_employee_id, F('id'), on_date)).exists()


def manages_sql(viewer: str, target: str, date: str = '(now() AT TIME ZONE \'Asia/Kathmandu\')::date') -> str:
    """SQL fragment for "the viewer manages this employee", for code that still builds raw SQL. ORM code uses
    `manages()` instead."""
    return f'hj_manages({viewer}::uuid, {target}, {date})'


def assert_manages(user: AuthUser, employee_id: str, area: str | None = None) -> None:
    """
    Who may see or act for an employee: HR and admin company-wide; a manager for the people they manage; everyone for
    themselves; with `area`, that area's specialist role as well. An employee of another company is reported as not
    found so ids cannot be enumerated across tenants.
    """
    if not _models().Employee.objects.for_company(user.company_id).filter(pk=as_uuid(employee_id)).exists():
        raise NotFoundError('employee not found')
    if has_role(user, 'hr') or (area and has_module(user, area)) or user.employee_id == str(employee_id):
        return
    if has_role(user, 'manager') and user.employee_id and is_manager_of(user.employee_id, employee_id):
        return
    raise ForbiddenError('not allowed to access this employee')


def company_id_of(user: AuthUser | None) -> str:
    """The signed-in user's ACTIVE company; accounts without one (none exist outside tests) cannot use company data."""
    if not user or not user.company_id:
        raise ForbiddenError('this account is not linked to a company')
    return user.company_id
