import { nptToday } from '@hajiri/attendance-engine';
import type { Queryable } from '../../db/pool.js';
import { hashPassword, randomToken, sha256Hex, verifyPassword } from '../../crypto.js';
import { audit } from '../../audit.js';
import { BadRequestError, NotFoundError, UnauthorizedError } from '../../errors.js';
import type { FieldCipher } from '../../crypto.js';
import { consumeCode, isTwoFactorRequired, requiredTwoFactorRoles } from './two-factor.js';

export type Role = 'admin' | 'hr' | 'manager' | 'employee' | 'payroll_admin' | 'attendance_admin' | 'recruiter' | 'executive';
/**
 * admin and hr see everything; the specialist roles split HR work by module (payroll, attendance and leave, hiring,
 * read-only executive dashboards); manager covers the people a user manages (line reports, department, branch).
 */
export const ROLES: Role[] = ['admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive'];

export interface AuthUser {
  id: string;
  companyId: string | null;
  employeeId: string | null;
  email: string;
  name: string;
  roles: Role[];
  mustChangePassword: boolean;
  /** Two-step sign-in is on for this account. */
  twoFactorEnabled: boolean;
  /** The company requires two-step sign-in for one of the user's roles and it is not set up yet. */
  mustSetUpTwoFactor: boolean;
}

interface UserRow {
  id: string; company_id: string | null; employee_id: string | null; email: string; name: string; password_hash: string;
  roles: Role[]; status: string; must_change_password: boolean; failed_logins: number; locked_until: Date | null; totp_enabled_at: Date | null;
  is_head?: boolean;
}

/** A department or branch head manages people even when the account was not given the manager role. */
const effectiveRoles = (r: UserRow): Role[] => (r.is_head && !r.roles.includes('manager') ? [...r.roles, 'manager'] : r.roles);

const toUser = (r: UserRow, requiredRoles: Role[] = []): AuthUser => ({
  id: r.id, companyId: r.company_id, employeeId: r.employee_id, email: r.email, name: r.name, roles: effectiveRoles(r), mustChangePassword: r.must_change_password,
  twoFactorEnabled: r.totp_enabled_at != null,
  mustSetUpTwoFactor: r.totp_enabled_at == null && isTwoFactorRequired(r.roles, requiredRoles),
});

export const SESSION_TTL_MS = 12 * 60 * 60 * 1000;      // 12 h overall (plan §12.4 AAL3 ceiling)
export const SESSION_IDLE_MS = 60 * 60 * 1000;          // 1 h idle
const MAX_FAILED = 10;
const LOCK_MS = 15 * 60 * 1000;
/** The second sign-in step must follow the password within 5 minutes and allows 5 tries. */
export const CHALLENGE_TTL_MS = 5 * 60 * 1000;
const CHALLENGE_MAX_ATTEMPTS = 5;

export type LoginResult = { user: AuthUser; token: string } | { twoFactorRequired: true; challenge: string };

interface SessionMeta { ip?: string; userAgent?: string }

async function startSession(db: Queryable, row: UserRow, meta: SessionMeta, twoFactor: boolean): Promise<{ user: AuthUser; token: string }> {
  const token = randomToken();
  await db.query('UPDATE app_user SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = $1', [row.id]);
  await db.query('INSERT INTO session (token_hash, user_id, expires_at, ip, user_agent, two_factor) VALUES ($1, $2, $3::timestamptz, $4, $5, $6)',
    [sha256Hex(token), row.id, new Date(Date.now() + SESSION_TTL_MS), meta.ip ?? null, meta.userAgent?.slice(0, 200) ?? null, twoFactor]);
  await audit(db, { actorId: row.id, action: 'login', table: 'app_user', rowId: row.id, ...(twoFactor ? { after: { twoFactor: true } } : {}) });
  const head = await db.query<{ h: boolean }>('SELECT hj_is_head($1) AS h', [row.employee_id]);
  return { user: toUser({ ...row, is_head: head.rows[0]?.h ?? false }, await requiredTwoFactorRoles(db, row.company_id)), token };
}

async function recordFailedLogin(db: Queryable, userId: string, action: string): Promise<void> {
  // Increment in SQL so parallel guesses cannot undercount and slip past the lockout.
  await db.query(
    `UPDATE app_user SET failed_logins = failed_logins + 1,
            locked_until = CASE WHEN failed_logins + 1 >= $2 THEN now() + make_interval(secs => $3) ELSE locked_until END
      WHERE id = $1`, [userId, MAX_FAILED, LOCK_MS / 1000]);
  await audit(db, { actorId: userId, action, table: 'app_user', rowId: userId });
}

export async function createUser(db: Queryable, u: {
  companyId: string | null; employeeId?: string | null; email: string; name: string; password: string; roles: Role[];
  mustChangePassword?: boolean; pepper?: string; actorId?: string | null;
}): Promise<AuthUser> {
  const r = await db.query<UserRow>(
    `INSERT INTO app_user (company_id, employee_id, email, name, password_hash, roles, must_change_password)
     VALUES ($1, $2, lower($3), $4, $5, $6, $7) RETURNING *`,
    [u.companyId, u.employeeId ?? null, u.email, u.name, await hashPassword(u.password, u.pepper), u.roles, u.mustChangePassword ?? false]);
  const user = toUser(r.rows[0]!);
  await audit(db, { actorId: u.actorId ?? null, action: 'create', table: 'app_user', rowId: user.id, after: { email: user.email, roles: user.roles } });
  return user;
}

/**
 * Password step. With two-step sign-in on, no session is created: the caller gets a single-use challenge for
 * POST /auth/login/2fa, and failed logins are only cleared once the second step succeeds.
 */
export async function login(db: Queryable, email: string, password: string, meta: SessionMeta & { pepper?: string }): Promise<LoginResult> {
  const r = await db.query<UserRow>('SELECT * FROM app_user WHERE email = lower($1)', [email]);
  const row = r.rows[0];
  // Constant-ish time: verify against a dummy hash when the user does not exist.
  const ok = row ? await verifyPassword(password, row.password_hash, meta.pepper) : (await verifyPassword(password, DUMMY_HASH, meta.pepper), false);
  if (!row || row.status !== 'active') throw new UnauthorizedError('invalid email or password');
  if (row.locked_until && row.locked_until.getTime() > Date.now()) throw new UnauthorizedError('account temporarily locked; try again later');
  if (!ok) {
    await recordFailedLogin(db, row.id, 'login_failed');
    throw new UnauthorizedError('invalid email or password');
  }
  // Someone who has left (exit date before today, Nepal time) can no longer sign in; HR clears the exit date to restore access.
  if (row.employee_id) {
    const ex = await db.query<{ left: boolean }>('SELECT exit_date < $2::date AS left FROM employee WHERE id = $1', [row.employee_id, nptToday()]);
    if (ex.rows[0]?.left) throw new UnauthorizedError('this account was closed when you left the company; contact HR if you need your records');
  }
  if (row.totp_enabled_at) {
    const challenge = randomToken();
    await db.query('INSERT INTO login_challenge (token_hash, user_id, expires_at, ip, user_agent) VALUES ($1, $2, $3::timestamptz, $4, $5)',
      [sha256Hex(challenge), row.id, new Date(Date.now() + CHALLENGE_TTL_MS), meta.ip ?? null, meta.userAgent?.slice(0, 200) ?? null]);
    await audit(db, { actorId: row.id, action: 'login_password_ok', table: 'app_user', rowId: row.id });
    return { twoFactorRequired: true, challenge };
  }
  return startSession(db, row, meta, false);
}

/** Second step: a code from the authenticator app (or a recovery code) turns the challenge into a session. */
export async function completeTwoFactorLogin(db: Queryable, cipher: FieldCipher, challenge: string, code: string, meta: SessionMeta): Promise<{ user: AuthUser; token: string; method: 'totp' | 'recovery' }> {
  // Count the attempt first (atomically), so parallel guesses share the five tries.
  const ch = await db.query<{ user_id: string }>(
    `UPDATE login_challenge SET attempts = attempts + 1
      WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now() AND attempts < $2 RETURNING user_id`, [sha256Hex(challenge), CHALLENGE_MAX_ATTEMPTS]);
  const userId = ch.rows[0]?.user_id;
  if (!userId) throw new UnauthorizedError('This sign-in has expired. Enter your password again.');
  const r = await db.query<UserRow>('SELECT * FROM app_user WHERE id = $1', [userId]);
  const row = r.rows[0];
  if (!row || row.status !== 'active' || !row.totp_enabled_at) throw new UnauthorizedError('This sign-in has expired. Enter your password again.');
  if (row.locked_until && row.locked_until.getTime() > Date.now()) throw new UnauthorizedError('account temporarily locked; try again later');
  const method = await consumeCode(db, cipher, row.id, code);
  if (!method) {
    await recordFailedLogin(db, row.id, 'login_2fa_failed');
    throw new UnauthorizedError('That code is not correct. Use the newest code from your authenticator app, or a recovery code.');
  }
  const used = await db.query('UPDATE login_challenge SET used_at = now() WHERE token_hash = $1 AND used_at IS NULL', [sha256Hex(challenge)]);
  if ((used.rowCount ?? 0) === 0) throw new UnauthorizedError('This sign-in has expired. Enter your password again.');
  const s = await startSession(db, row, meta, true);
  return { ...s, method };
}

const DUMMY_HASH = 'scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

export async function resolveSession(db: Queryable, token: string): Promise<AuthUser | null> {
  const r = await db.query<UserRow & { last_seen_at: Date; required_roles: Role[] | null }>(
    `SELECT u.*, s.last_seen_at, hj_is_head(u.employee_id) AS is_head,
            (SELECT cs.value->'require2faRoles' FROM company_setting cs WHERE cs.company_id = u.company_id AND cs.key = 'security.policy') AS required_roles
       FROM session s JOIN app_user u ON u.id = s.user_id
      WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now() AND u.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM employee e WHERE e.id = u.employee_id AND e.exit_date < $2::date)`, [sha256Hex(token), nptToday()]);
  const row = r.rows[0];
  if (!row) return null;
  if (Date.now() - row.last_seen_at.getTime() > SESSION_IDLE_MS) {
    await db.query('UPDATE session SET revoked_at = now() WHERE token_hash = $1', [sha256Hex(token)]);
    return null;
  }
  await db.query('UPDATE session SET last_seen_at = now() WHERE token_hash = $1', [sha256Hex(token)]);
  return toUser(row, Array.isArray(row.required_roles) ? row.required_roles : []);
}

export async function logout(db: Queryable, token: string): Promise<void> {
  await db.query('UPDATE session SET revoked_at = now() WHERE token_hash = $1', [sha256Hex(token)]);
}

export async function changePassword(db: Queryable, userId: string, current: string, next: string, pepper?: string): Promise<void> {
  const r = await db.query<UserRow>('SELECT * FROM app_user WHERE id = $1', [userId]);
  const row = r.rows[0];
  if (!row || !(await verifyPassword(current, row.password_hash, pepper))) throw new UnauthorizedError('current password is incorrect');
  if (current === next) throw new BadRequestError('the new password must be different from the current one');
  await db.query('UPDATE app_user SET password_hash = $2, must_change_password = false WHERE id = $1', [userId, await hashPassword(next, pepper)]);
  await db.query('UPDATE session SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL', [userId]);
  await audit(db, { actorId: userId, action: 'password_change', table: 'app_user', rowId: userId });
}

// ---------------------------------------------------------------- active sessions

export interface SessionInfo { id: string; createdAt: Date; lastSeenAt: Date; expiresAt: Date; ip: string | null; device: string; twoFactor: boolean; current: boolean }

/** A short, human description of a user agent ("Chrome on Android"); nothing beyond the original header is stored. */
export function describeUserAgent(ua: string | null): string {
  if (!ua) return 'Unknown device';
  const browser = /Edg\//.test(ua) ? 'Edge' : /OPR\/|Opera/.test(ua) ? 'Opera' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome'
    : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  const os = /Android/.test(ua) ? 'Android' : /iPhone|iPad|iPod/.test(ua) ? 'iOS' : /Windows/.test(ua) ? 'Windows' : /Mac OS X|Macintosh/.test(ua) ? 'macOS'
    : /CrOS/.test(ua) ? 'ChromeOS' : /Linux/.test(ua) ? 'Linux' : null;
  return os ? `${browser} on ${os}` : browser;
}

export async function listSessions(db: Queryable, userId: string, currentToken: string | undefined): Promise<SessionInfo[]> {
  const cur = currentToken ? sha256Hex(currentToken) : '';
  const r = await db.query<{ id: string; created_at: Date; last_seen_at: Date; expires_at: Date; ip: string | null; user_agent: string | null; two_factor: boolean; current: boolean }>(
    `SELECT id, created_at, last_seen_at, expires_at, ip, user_agent, two_factor, token_hash = $2 AS current FROM session
      WHERE user_id = $1 AND revoked_at IS NULL AND expires_at > now() AND last_seen_at > now() - make_interval(secs => $3)
      ORDER BY (token_hash = $2) DESC, last_seen_at DESC`, [userId, cur, SESSION_IDLE_MS / 1000]);
  return r.rows.map((x) => ({ id: x.id, createdAt: x.created_at, lastSeenAt: x.last_seen_at, expiresAt: x.expires_at, ip: x.ip, device: describeUserAgent(x.user_agent), twoFactor: x.two_factor, current: x.current }));
}

/** Sign out one of my other sessions (the current one signs out with POST /auth/logout). */
export async function revokeSession(db: Queryable, userId: string, sessionId: string, currentToken: string | undefined): Promise<void> {
  const r = await db.query<{ current: boolean }>('SELECT token_hash = $3 AS current FROM session WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL',
    [sessionId, userId, currentToken ? sha256Hex(currentToken) : '']);
  if (!r.rows[0]) throw new NotFoundError('session not found');
  if (r.rows[0].current) throw new BadRequestError('this is the session you are using; sign out instead');
  await db.query('UPDATE session SET revoked_at = now() WHERE id = $1 AND user_id = $2', [sessionId, userId]);
  await audit(db, { actorId: userId, action: 'session_revoke', table: 'session', rowId: sessionId });
}

export async function revokeOtherSessions(db: Queryable, userId: string, currentToken: string | undefined): Promise<number> {
  const r = await db.query('UPDATE session SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL AND token_hash <> $2', [userId, currentToken ? sha256Hex(currentToken) : '']);
  await audit(db, { actorId: userId, action: 'session_revoke_others', table: 'session', rowId: userId, after: { count: r.rowCount ?? 0 } });
  return r.rowCount ?? 0;
}
