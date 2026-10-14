import { nptToday } from '@hajiri/attendance-engine';
import type { Queryable } from '../../db/pool.js';
import { hashPassword, randomToken, sha256Hex, verifyPassword } from '../../crypto.js';
import { audit } from '../../audit.js';
import { BadRequestError, UnauthorizedError } from '../../errors.js';

export type Role = 'admin' | 'hr' | 'manager' | 'employee';
export const ROLES: Role[] = ['admin', 'hr', 'manager', 'employee'];

export interface AuthUser {
  id: string;
  companyId: string | null;
  employeeId: string | null;
  email: string;
  name: string;
  roles: Role[];
  mustChangePassword: boolean;
}

interface UserRow {
  id: string; company_id: string | null; employee_id: string | null; email: string; name: string; password_hash: string;
  roles: Role[]; status: string; must_change_password: boolean; failed_logins: number; locked_until: Date | null;
}

const toUser = (r: UserRow): AuthUser => ({
  id: r.id, companyId: r.company_id, employeeId: r.employee_id, email: r.email, name: r.name, roles: r.roles, mustChangePassword: r.must_change_password,
});

export const SESSION_TTL_MS = 12 * 60 * 60 * 1000;      // 12 h overall (plan §12.4 AAL3 ceiling)
export const SESSION_IDLE_MS = 60 * 60 * 1000;          // 1 h idle
const MAX_FAILED = 10;
const LOCK_MS = 15 * 60 * 1000;

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

export async function login(db: Queryable, email: string, password: string, meta: { ip?: string; userAgent?: string; pepper?: string }):
  Promise<{ user: AuthUser; token: string }> {
  const r = await db.query<UserRow>('SELECT * FROM app_user WHERE email = lower($1)', [email]);
  const row = r.rows[0];
  // Constant-ish time: verify against a dummy hash when the user does not exist.
  const ok = row ? await verifyPassword(password, row.password_hash, meta.pepper) : (await verifyPassword(password, DUMMY_HASH, meta.pepper), false);
  if (!row || row.status !== 'active') throw new UnauthorizedError('invalid email or password');
  if (row.locked_until && row.locked_until.getTime() > Date.now()) throw new UnauthorizedError('account temporarily locked; try again later');
  if (!ok) {
    // Increment in SQL so parallel guesses cannot undercount and slip past the lockout.
    await db.query(
      `UPDATE app_user SET failed_logins = failed_logins + 1,
              locked_until = CASE WHEN failed_logins + 1 >= $2 THEN now() + make_interval(secs => $3) ELSE locked_until END
        WHERE id = $1`, [row.id, MAX_FAILED, LOCK_MS / 1000]);
    await audit(db, { actorId: row.id, action: 'login_failed', table: 'app_user', rowId: row.id });
    throw new UnauthorizedError('invalid email or password');
  }
  // Someone who has left (exit date before today, Nepal time) can no longer sign in; HR clears the exit date to restore access.
  if (row.employee_id) {
    const ex = await db.query<{ left: boolean }>('SELECT exit_date < $2::date AS left FROM employee WHERE id = $1', [row.employee_id, nptToday()]);
    if (ex.rows[0]?.left) throw new UnauthorizedError('this account was closed when you left the company; contact HR if you need your records');
  }
  const token = randomToken();
  await db.query('UPDATE app_user SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = $1', [row.id]);
  await db.query('INSERT INTO session (token_hash, user_id, expires_at, ip, user_agent) VALUES ($1, $2, $3::timestamptz, $4, $5)',
    [sha256Hex(token), row.id, new Date(Date.now() + SESSION_TTL_MS), meta.ip ?? null, meta.userAgent?.slice(0, 200) ?? null]);
  await audit(db, { actorId: row.id, action: 'login', table: 'app_user', rowId: row.id });
  return { user: toUser(row), token };
}

const DUMMY_HASH = 'scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

export async function resolveSession(db: Queryable, token: string): Promise<AuthUser | null> {
  const r = await db.query<UserRow & { last_seen_at: Date }>(
    `SELECT u.*, s.last_seen_at FROM session s JOIN app_user u ON u.id = s.user_id
      WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now() AND u.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM employee e WHERE e.id = u.employee_id AND e.exit_date < $2::date)`, [sha256Hex(token), nptToday()]);
  const row = r.rows[0];
  if (!row) return null;
  if (Date.now() - row.last_seen_at.getTime() > SESSION_IDLE_MS) {
    await db.query('UPDATE session SET revoked_at = now() WHERE token_hash = $1', [sha256Hex(token)]);
    return null;
  }
  await db.query('UPDATE session SET last_seen_at = now() WHERE token_hash = $1', [sha256Hex(token)]);
  return toUser(row);
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
