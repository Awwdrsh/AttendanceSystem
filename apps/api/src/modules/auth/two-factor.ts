import type { FieldCipher } from '../../crypto.js';
import { sha256Hex, verifyPassword } from '../../crypto.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError } from '../../errors.js';
import { matchTotp, newRecoveryCodes, newTotpSecret, normaliseRecoveryCode, otpauthUri } from './totp.js';
import type { AuthUser, Role } from './service.js';

const secretAad = (userId: string) => `totp:${userId}`;
const recoveryHash = (userId: string, code: string) => sha256Hex(`recovery:${userId}:${normaliseRecoveryCode(code)}`);

/** Roles the company requires two-step sign-in for (Settings → security.policy). */
export async function requiredTwoFactorRoles(db: Queryable, companyId: string | null): Promise<Role[]> {
  if (!companyId) return [];
  const r = await db.query<{ roles: unknown }>(`SELECT value->'require2faRoles' AS roles FROM company_setting WHERE company_id = $1 AND key = 'security.policy'`, [companyId]);
  const v = r.rows[0]?.roles;
  return Array.isArray(v) ? (v.filter((x) => typeof x === 'string') as Role[]) : [];
}

export function isTwoFactorRequired(roles: Role[], required: Role[]): boolean {
  return roles.some((r) => required.includes(r));
}

export interface TwoFactorStatus { enabled: boolean; enabledAt: Date | null; recoveryCodesLeft: number; required: boolean; setupPending: boolean }

export async function twoFactorStatus(db: Queryable, user: AuthUser): Promise<TwoFactorStatus> {
  const r = await db.query<{ totp_enabled_at: Date | null; pending: boolean; left: number }>(
    `SELECT totp_enabled_at, totp_pending_enc IS NOT NULL AS pending,
            (SELECT count(*)::int FROM user_recovery_code c WHERE c.user_id = u.id AND c.used_at IS NULL) AS left
       FROM app_user u WHERE id = $1`, [user.id]);
  const row = r.rows[0];
  if (!row) throw new NotFoundError('user not found');
  const required = isTwoFactorRequired(user.roles, await requiredTwoFactorRoles(db, user.companyId));
  return { enabled: row.totp_enabled_at !== null, enabledAt: row.totp_enabled_at, recoveryCodesLeft: row.totp_enabled_at ? row.left : 0, required, setupPending: row.pending };
}

/** Start enrolment: a fresh secret kept as pending until a code from the app confirms it. */
export async function beginSetup(db: Queryable, cipher: FieldCipher, user: AuthUser): Promise<{ secret: string; otpauthUri: string }> {
  const cur = await db.query<{ totp_enabled_at: Date | null }>('SELECT totp_enabled_at FROM app_user WHERE id = $1', [user.id]);
  if (cur.rows[0]?.totp_enabled_at) throw new ConflictError('two-step sign-in is already on; turn it off first to move to a new phone');
  const secret = newTotpSecret();
  await db.query('UPDATE app_user SET totp_pending_enc = $2 WHERE id = $1', [user.id, cipher.encrypt(secret, secretAad(user.id))]);
  return { secret, otpauthUri: otpauthUri(secret, user.email) };
}

async function storeRecoveryCodes(db: Queryable, userId: string): Promise<string[]> {
  const codes = newRecoveryCodes();
  await db.query('DELETE FROM user_recovery_code WHERE user_id = $1', [userId]);
  for (const c of codes) await db.query('INSERT INTO user_recovery_code (user_id, code_hash) VALUES ($1, $2)', [userId, recoveryHash(userId, c)]);
  return codes;
}

/** Confirm enrolment with the first code from the app; returns the recovery codes (shown once). */
export async function confirmSetup(db: Queryable, cipher: FieldCipher, user: AuthUser, code: string): Promise<string[]> {
  const r = await db.query<{ totp_pending_enc: Buffer | null; totp_enabled_at: Date | null }>('SELECT totp_pending_enc, totp_enabled_at FROM app_user WHERE id = $1', [user.id]);
  const row = r.rows[0];
  if (row?.totp_enabled_at) throw new ConflictError('two-step sign-in is already on');
  if (!row?.totp_pending_enc) throw new BadRequestError('start the setup first');
  const secret = cipher.decrypt(row.totp_pending_enc, secretAad(user.id));
  const step = matchTotp(secret, code);
  if (step === null) throw new BadRequestError('That code did not match. Check the time on your phone is automatic and try the newest code.');
  await db.query(
    'UPDATE app_user SET totp_secret_enc = totp_pending_enc, totp_pending_enc = NULL, totp_enabled_at = now(), totp_last_step = $2 WHERE id = $1', [user.id, step]);
  const codes = await storeRecoveryCodes(db, user.id);
  await audit(db, { actorId: user.id, action: 'two_factor_enable', table: 'app_user', rowId: user.id });
  return codes;
}

/**
 * Check a 6-digit code or a recovery code and consume it. A code step can be used once (totp_last_step only moves
 * forward, atomically), and a recovery code once. Returns how the user proved it, or null.
 */
export async function consumeCode(db: Queryable, cipher: FieldCipher, userId: string, code: string): Promise<'totp' | 'recovery' | null> {
  const c = code.trim();
  if (/^\d{3}\s?\d{3}$/.test(c)) {
    const r = await db.query<{ totp_secret_enc: Buffer | null }>('SELECT totp_secret_enc FROM app_user WHERE id = $1 AND totp_enabled_at IS NOT NULL', [userId]);
    const enc = r.rows[0]?.totp_secret_enc;
    if (!enc) return null;
    const step = matchTotp(cipher.decrypt(enc, secretAad(userId)), c);
    if (step === null) return null;
    const upd = await db.query('UPDATE app_user SET totp_last_step = $2 WHERE id = $1 AND (totp_last_step IS NULL OR totp_last_step < $2)', [userId, step]);
    return (upd.rowCount ?? 0) > 0 ? 'totp' : null;
  }
  if (normaliseRecoveryCode(c).length !== 12) return null;
  const upd = await db.query('UPDATE user_recovery_code SET used_at = now() WHERE user_id = $1 AND code_hash = $2 AND used_at IS NULL', [userId, recoveryHash(userId, c)]);
  return (upd.rowCount ?? 0) > 0 ? 'recovery' : null;
}

export async function regenerateRecoveryCodes(db: Queryable, cipher: FieldCipher, user: AuthUser, code: string): Promise<string[]> {
  const st = await twoFactorStatus(db, user);
  if (!st.enabled) throw new BadRequestError('two-step sign-in is off');
  if ((await consumeCode(db, cipher, user.id, code)) === null) throw new BadRequestError('That code did not match.');
  const codes = await storeRecoveryCodes(db, user.id);
  await audit(db, { actorId: user.id, action: 'two_factor_recovery_codes', table: 'app_user', rowId: user.id });
  return codes;
}

async function clearTwoFactor(db: Queryable, userId: string): Promise<void> {
  await db.query('UPDATE app_user SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL, totp_last_step = NULL WHERE id = $1', [userId]);
  await db.query('DELETE FROM user_recovery_code WHERE user_id = $1', [userId]);
  await db.query('UPDATE login_challenge SET used_at = now() WHERE user_id = $1 AND used_at IS NULL', [userId]);
}

/** Turning it off needs the password and a current code; not allowed while the company requires it for the user's role. */
export async function disableTwoFactor(db: Queryable, cipher: FieldCipher, user: AuthUser, password: string, code: string, pepper?: string): Promise<void> {
  const st = await twoFactorStatus(db, user);
  if (!st.enabled) throw new BadRequestError('two-step sign-in is already off');
  if (st.required) throw new ForbiddenError('your company requires two-step sign-in for your role; it cannot be turned off');
  const r = await db.query<{ password_hash: string }>('SELECT password_hash FROM app_user WHERE id = $1', [user.id]);
  if (!r.rows[0] || !(await verifyPassword(password, r.rows[0].password_hash, pepper))) throw new UnauthorizedError('current password is incorrect');
  if ((await consumeCode(db, cipher, user.id, code)) === null) throw new BadRequestError('That code did not match.');
  await clearTwoFactor(db, user.id);
  await audit(db, { actorId: user.id, action: 'two_factor_disable', table: 'app_user', rowId: user.id });
}

/** Admin reset for a lost phone: two-step sign-in is cleared and the person is signed out everywhere. */
export async function adminResetTwoFactor(db: Queryable, actor: AuthUser, targetId: string, reason: string): Promise<void> {
  const t = await db.query<{ id: string; totp_enabled_at: Date | null }>('SELECT id, totp_enabled_at FROM app_user WHERE id = $1 AND company_id = $2', [targetId, actor.companyId]);
  if (!t.rows[0]) throw new NotFoundError('user not found');
  if (targetId === actor.id) throw new BadRequestError('use your own sign-in security page to change your two-step sign-in');
  await clearTwoFactor(db, targetId);
  await db.query('UPDATE session SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL', [targetId]);
  await audit(db, { actorId: actor.id, action: 'two_factor_reset', table: 'app_user', rowId: targetId, before: { enabled: t.rows[0].totp_enabled_at !== null }, after: { reason } });
}
