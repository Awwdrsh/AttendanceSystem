"""
Sign-in, sessions, two-step sign-in and access rules. Port of apps/api/src/modules/auth (service, two-factor and the
access helpers in middleware). Sessions live in the `session` table, so a session cookie works on either stack.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from django.db import transaction

from .audit import audit
from .clock import now, npt_today
from .crypto import field_cipher, hash_password, random_token, sha256_hex, verify_password
from .db import execute, one, rows
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


@dataclass(frozen=True)
class AuthUser:
    id: str
    company_id: str | None
    employee_id: str | None
    email: str
    name: str
    roles: tuple[str, ...]
    must_change_password: bool
    two_factor_enabled: bool
    # The company requires two-step sign-in for one of the user's roles and it is not set up yet.
    must_set_up_two_factor: bool


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


def _effective_roles(r: dict) -> tuple[str, ...]:
    """A department or branch head manages people even when the account was not given the manager role."""
    roles = tuple(r['roles'])
    return roles + ('manager',) if r.get('is_head') and 'manager' not in roles else roles


def _to_user(r: dict, required_roles: list[str] | tuple[str, ...] = ()) -> AuthUser:
    return AuthUser(
        id=str(r['id']), company_id=r['company_id'] and str(r['company_id']),
        employee_id=r['employee_id'] and str(r['employee_id']), email=r['email'], name=r['name'],
        roles=_effective_roles(r), must_change_password=r['must_change_password'],
        two_factor_enabled=r['totp_enabled_at'] is not None,
        must_set_up_two_factor=r['totp_enabled_at'] is None and is_two_factor_required(r['roles'], required_roles))


def has_role(user: AuthUser, *roles: str) -> bool:
    return 'admin' in user.roles or any(r in user.roles for r in roles)


def has_module(user: AuthUser, module: str) -> bool:
    return has_role(user, *MODULE_ROLES[module])


# ---------------------------------------------------------------- sign-in

def _start_session(row: dict, meta: SessionMeta, two_factor: bool) -> LoginResult:
    token = random_token()
    execute('UPDATE app_user SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = %s', [row['id']])
    execute('INSERT INTO session (token_hash, user_id, expires_at, ip, user_agent, two_factor) VALUES (%s, %s, %s, %s, %s, %s)',
            [sha256_hex(token), row['id'], now() + SESSION_TTL, meta.ip, (meta.user_agent or '')[:200] or None, two_factor])
    audit('login', 'app_user', actor_id=row['id'], row_id=row['id'], **({'after': {'twoFactor': True}} if two_factor else {}))
    head = one('SELECT hj_is_head(%s) AS h', [row['employee_id']])
    user = _to_user({**row, 'is_head': bool(head and head['h'])}, required_two_factor_roles(row['company_id']))
    return LoginResult(user=user, token=token)


def _record_failed_login(user_id: str, action: str) -> None:
    # Increment in SQL so parallel guesses cannot undercount and slip past the lockout.
    execute("""UPDATE app_user SET failed_logins = failed_logins + 1,
                      locked_until = CASE WHEN failed_logins + 1 >= %s THEN now() + make_interval(secs => %s) ELSE locked_until END
                WHERE id = %s""", [MAX_FAILED, LOCK.total_seconds(), user_id])
    audit(action, 'app_user', actor_id=user_id, row_id=user_id)


def _is_locked(row: dict) -> bool:
    return row['locked_until'] is not None and row['locked_until'] > now()


def create_user(*, company_id: str | None, email: str, name: str, password: str, roles: list[str],
                employee_id: str | None = None, must_change_password: bool = False, pepper: str | None = None,
                actor_id: str | None = None) -> AuthUser:
    r = one("""INSERT INTO app_user (company_id, employee_id, email, name, password_hash, roles, must_change_password)
               VALUES (%s, %s, lower(%s), %s, %s, %s, %s) RETURNING *""",
            [company_id, employee_id, email, name, hash_password(password, pepper), roles, must_change_password])
    user = _to_user(r)
    audit('create', 'app_user', actor_id=actor_id, row_id=user.id, after={'email': user.email, 'roles': list(user.roles)})
    return user


# Not atomic: failed attempts must stay counted when the error is raised.
def login(email: str, password: str, meta: SessionMeta = SessionMeta(), pepper: str | None = None) -> LoginResult:
    """
    Password step. With two-step sign-in on, no session is created: the caller gets a single-use challenge for the
    second step, and failed logins are only cleared once the second step succeeds.
    """
    row = one('SELECT * FROM app_user WHERE email = lower(%s)', [email])
    # Constant-ish time: verify against a dummy hash when the user does not exist.
    ok = verify_password(password, row['password_hash'] if row else _DUMMY_HASH, pepper) and row is not None
    if not row or row['status'] != 'active':
        raise UnauthorizedError('invalid email or password')
    if _is_locked(row):
        raise UnauthorizedError(_LOCKED)
    if not ok:
        _record_failed_login(row['id'], 'login_failed')
        raise UnauthorizedError('invalid email or password')
    # Someone who has left (exit date before today, Nepal time) can no longer sign in; HR clears the exit date to restore access.
    if row['employee_id']:
        ex = one('SELECT exit_date < %s::date AS left FROM employee WHERE id = %s', [npt_today(), row['employee_id']])
        if ex and ex['left']:
            raise UnauthorizedError('this account was closed when you left the company; contact HR if you need your records')
    if row['totp_enabled_at']:
        challenge = random_token()
        execute('INSERT INTO login_challenge (token_hash, user_id, expires_at, ip, user_agent) VALUES (%s, %s, %s, %s, %s)',
                [sha256_hex(challenge), row['id'], now() + CHALLENGE_TTL, meta.ip, (meta.user_agent or '')[:200] or None])
        audit('login_password_ok', 'app_user', actor_id=row['id'], row_id=row['id'])
        return LoginResult(challenge=challenge)
    return _start_session(row, meta, False)


# Not atomic: failed attempts must stay counted when the error is raised.
def complete_two_factor_login(challenge: str, code: str, meta: SessionMeta = SessionMeta()) -> LoginResult:
    """Second step: a code from the authenticator app (or a recovery code) turns the challenge into a session."""
    # Count the attempt first (atomically), so parallel guesses share the five tries.
    ch = one("""UPDATE login_challenge SET attempts = attempts + 1
                 WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() AND attempts < %s RETURNING user_id""",
             [sha256_hex(challenge), CHALLENGE_MAX_ATTEMPTS])
    if not ch:
        raise UnauthorizedError(_EXPIRED)
    row = one('SELECT * FROM app_user WHERE id = %s', [ch['user_id']])
    if not row or row['status'] != 'active' or not row['totp_enabled_at']:
        raise UnauthorizedError(_EXPIRED)
    if _is_locked(row):
        raise UnauthorizedError(_LOCKED)
    method = consume_code(row['id'], code)
    if not method:
        _record_failed_login(row['id'], 'login_2fa_failed')
        raise UnauthorizedError('That code is not correct. Use the newest code from your authenticator app, or a recovery code.')
    if execute('UPDATE login_challenge SET used_at = now() WHERE token_hash = %s AND used_at IS NULL', [sha256_hex(challenge)]) == 0:
        raise UnauthorizedError(_EXPIRED)
    s = _start_session(row, meta, True)
    return LoginResult(user=s.user, token=s.token, method=method)


def resolve_session(token: str) -> AuthUser | None:
    row = one("""
        SELECT u.*, s.last_seen_at, hj_is_head(u.employee_id) AS is_head,
               (SELECT cs.value->'require2faRoles' FROM company_setting cs
                 WHERE cs.company_id = u.company_id AND cs.key = 'security.policy') AS required_roles
          FROM session s JOIN app_user u ON u.id = s.user_id
         WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() AND u.status = 'active'
           AND NOT EXISTS (SELECT 1 FROM employee e WHERE e.id = u.employee_id AND e.exit_date < %s::date)""",
              [sha256_hex(token), npt_today()])
    if not row:
        return None
    if now() - row['last_seen_at'] > SESSION_IDLE:
        execute('UPDATE session SET revoked_at = now() WHERE token_hash = %s', [sha256_hex(token)])
        return None
    execute('UPDATE session SET last_seen_at = now() WHERE token_hash = %s', [sha256_hex(token)])
    req = row['required_roles']
    return _to_user(row, [x for x in req if isinstance(x, str)] if isinstance(req, list) else [])


def logout(token: str) -> None:
    execute('UPDATE session SET revoked_at = now() WHERE token_hash = %s', [sha256_hex(token)])


@transaction.atomic
def change_password(user_id: str, current: str, new: str, pepper: str | None = None) -> None:
    row = one('SELECT * FROM app_user WHERE id = %s', [user_id])
    if not row or not verify_password(current, row['password_hash'], pepper):
        raise UnauthorizedError('current password is incorrect')
    if current == new:
        raise BadRequestError('the new password must be different from the current one')
    execute('UPDATE app_user SET password_hash = %s, must_change_password = false WHERE id = %s', [hash_password(new, pepper), user_id])
    execute('UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL', [user_id])
    audit('password_change', 'app_user', actor_id=user_id, row_id=user_id)


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
    found = rows("""
        SELECT id, created_at, last_seen_at, expires_at, ip, user_agent, two_factor, token_hash = %s AS current FROM session
         WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now() AND last_seen_at > now() - make_interval(secs => %s)
         ORDER BY (token_hash = %s) DESC, last_seen_at DESC""", [cur, user_id, SESSION_IDLE.total_seconds(), cur])
    return [SessionInfo(str(x['id']), x['created_at'], x['last_seen_at'], x['expires_at'], x['ip'],
                        describe_user_agent(x['user_agent']), x['two_factor'], x['current']) for x in found]


@transaction.atomic
def revoke_session(user_id: str, session_id: str, current_token: str | None) -> None:
    """Sign out one of my other sessions (the current one signs out with logout)."""
    r = one('SELECT token_hash = %s AS current FROM session WHERE id = %s AND user_id = %s AND revoked_at IS NULL',
            [sha256_hex(current_token) if current_token else '', session_id, user_id])
    if not r:
        raise NotFoundError('session not found')
    if r['current']:
        raise BadRequestError('this is the session you are using; sign out instead')
    execute('UPDATE session SET revoked_at = now() WHERE id = %s AND user_id = %s', [session_id, user_id])
    audit('session_revoke', 'session', actor_id=user_id, row_id=session_id)


@transaction.atomic
def revoke_other_sessions(user_id: str, current_token: str | None) -> int:
    n = execute('UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL AND token_hash <> %s',
                [user_id, sha256_hex(current_token) if current_token else ''])
    audit('session_revoke_others', 'session', actor_id=user_id, row_id=user_id, after={'count': n})
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
    r = one("SELECT value->'require2faRoles' AS roles FROM company_setting WHERE company_id = %s AND key = 'security.policy'", [company_id])
    v = r and r['roles']
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


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
    row = one("""SELECT totp_enabled_at, totp_pending_enc IS NOT NULL AS pending,
                        (SELECT count(*)::int FROM user_recovery_code c WHERE c.user_id = u.id AND c.used_at IS NULL) AS left
                   FROM app_user u WHERE id = %s""", [user.id])
    if not row:
        raise NotFoundError('user not found')
    enabled = row['totp_enabled_at'] is not None
    return TwoFactorStatus(enabled, row['totp_enabled_at'], row['left'] if enabled else 0,
                           is_two_factor_required(user.roles, required_two_factor_roles(user.company_id)), row['pending'])


def begin_setup(user: AuthUser) -> dict[str, str]:
    """Start enrolment: a fresh secret kept as pending until a code from the app confirms it."""
    cur = one('SELECT totp_enabled_at FROM app_user WHERE id = %s', [user.id])
    if cur and cur['totp_enabled_at']:
        raise ConflictError('two-step sign-in is already on; turn it off first to move to a new phone')
    secret = new_totp_secret()
    execute('UPDATE app_user SET totp_pending_enc = %s WHERE id = %s', [field_cipher().encrypt(secret, _secret_aad(user.id)), user.id])
    return {'secret': secret, 'otpauth_uri': otpauth_uri(secret, user.email)}


def _store_recovery_codes(user_id: str) -> list[str]:
    codes = new_recovery_codes()
    execute('DELETE FROM user_recovery_code WHERE user_id = %s', [user_id])
    for c in codes:
        execute('INSERT INTO user_recovery_code (user_id, code_hash) VALUES (%s, %s)', [user_id, _recovery_hash(user_id, c)])
    return codes


@transaction.atomic
def confirm_setup(user: AuthUser, code: str) -> list[str]:
    """Confirm enrolment with the first code from the app; returns the recovery codes (shown once)."""
    row = one('SELECT totp_pending_enc, totp_enabled_at FROM app_user WHERE id = %s', [user.id])
    if row and row['totp_enabled_at']:
        raise ConflictError('two-step sign-in is already on')
    if not row or not row['totp_pending_enc']:
        raise BadRequestError('start the setup first')
    step = match_totp(field_cipher().decrypt(row['totp_pending_enc'], _secret_aad(user.id)), code)
    if step is None:
        raise BadRequestError('That code did not match. Check the time on your phone is automatic and try the newest code.')
    execute("""UPDATE app_user SET totp_secret_enc = totp_pending_enc, totp_pending_enc = NULL, totp_enabled_at = now(),
                      totp_last_step = %s WHERE id = %s""", [step, user.id])
    codes = _store_recovery_codes(user.id)
    audit('two_factor_enable', 'app_user', actor_id=user.id, row_id=user.id)
    return codes


def consume_code(user_id: str, code: str) -> Literal['totp', 'recovery'] | None:
    """
    Check a 6-digit code or a recovery code and consume it. A code step can be used once (totp_last_step only moves
    forward, atomically), and a recovery code once. Returns how the user proved it, or None.
    """
    c = code.strip()
    if re.fullmatch(r'\d{3}\s?\d{3}', c):
        r = one('SELECT totp_secret_enc FROM app_user WHERE id = %s AND totp_enabled_at IS NOT NULL', [user_id])
        if not r or not r['totp_secret_enc']:
            return None
        step = match_totp(field_cipher().decrypt(r['totp_secret_enc'], _secret_aad(str(user_id))), c)
        if step is None:
            return None
        n = execute('UPDATE app_user SET totp_last_step = %s WHERE id = %s AND (totp_last_step IS NULL OR totp_last_step < %s)',
                    [step, user_id, step])
        return 'totp' if n > 0 else None
    if len(normalise_recovery_code(c)) != 12:
        return None
    n = execute('UPDATE user_recovery_code SET used_at = now() WHERE user_id = %s AND code_hash = %s AND used_at IS NULL',
                [user_id, _recovery_hash(str(user_id), c)])
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
    execute('UPDATE app_user SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL, totp_last_step = NULL WHERE id = %s', [user_id])
    execute('DELETE FROM user_recovery_code WHERE user_id = %s', [user_id])
    execute('UPDATE login_challenge SET used_at = now() WHERE user_id = %s AND used_at IS NULL', [user_id])


@transaction.atomic
def disable_two_factor(user: AuthUser, password: str, code: str, pepper: str | None = None) -> None:
    """Turning it off needs the password and a current code; not allowed while the company requires it for the user's role."""
    st = two_factor_status(user)
    if not st.enabled:
        raise BadRequestError('two-step sign-in is already off')
    if st.required:
        raise ForbiddenError('your company requires two-step sign-in for your role; it cannot be turned off')
    r = one('SELECT password_hash FROM app_user WHERE id = %s', [user.id])
    if not r or not verify_password(password, r['password_hash'], pepper):
        raise UnauthorizedError('current password is incorrect')
    if consume_code(user.id, code) is None:
        raise BadRequestError('That code did not match.')
    _clear_two_factor(user.id)
    audit('two_factor_disable', 'app_user', actor_id=user.id, row_id=user.id)


@transaction.atomic
def admin_reset_two_factor(actor: AuthUser, target_id: str, reason: str) -> None:
    """Admin reset for a lost phone: two-step sign-in is cleared and the person is signed out everywhere."""
    t = one('SELECT id, totp_enabled_at FROM app_user WHERE id = %s AND company_id = %s', [target_id, actor.company_id])
    if not t:
        raise NotFoundError('user not found')
    if str(target_id) == actor.id:
        raise BadRequestError('use your own sign-in security page to change your two-step sign-in')
    _clear_two_factor(target_id)
    execute('UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL', [target_id])
    audit('two_factor_reset', 'app_user', actor_id=actor.id, row_id=target_id,
          before={'enabled': t['totp_enabled_at'] is not None}, after={'reason': reason})


# ---------------------------------------------------------------- access to employees

def is_manager_of(manager_employee_id: str, employee_id: str, on_date=None) -> bool:
    """
    Manager relationship on the given Nepal date (default today): the line manager, the head of the employee's
    department (or of a department above it) or the head of their branch. Defined once in SQL as hj_manages().
    """
    r = one('SELECT hj_manages(%s::uuid, %s::uuid, %s::date) AS ok', [manager_employee_id, employee_id, on_date or npt_today()])
    return bool(r and r['ok'])


def manages_sql(viewer: str, target: str, date: str = '(now() AT TIME ZONE \'Asia/Kathmandu\')::date') -> str:
    """SQL fragment for "the viewer manages this employee" (same rule as is_manager_of), for list queries."""
    return f'hj_manages({viewer}::uuid, {target}, {date})'


def assert_manages(user: AuthUser, employee_id: str, area: str | None = None) -> None:
    """
    Who may see or act for an employee: HR and admin company-wide; a manager for the people they manage; everyone for
    themselves; with `area`, that area's specialist role as well. An employee of another company is reported as not
    found so ids cannot be enumerated across tenants.
    """
    if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, user.company_id]):
        raise NotFoundError('employee not found')
    if has_role(user, 'hr') or (area and has_module(user, area)) or user.employee_id == str(employee_id):
        return
    if has_role(user, 'manager') and user.employee_id and is_manager_of(user.employee_id, employee_id):
        return
    raise ForbiddenError('not allowed to access this employee')


def company_id_of(user: AuthUser | None) -> str:
    """The signed-in user's company; accounts without one (none exist outside tests) cannot use company data."""
    if not user or not user.company_id:
        raise ForbiddenError('this account is not linked to a company')
    return user.company_id
