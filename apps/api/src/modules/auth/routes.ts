import { Router } from 'express';
import { z } from 'zod';
import type { Db } from '../../db/pool.js';
import { passwordPolicyError } from '../../crypto.js';
import { BadRequestError } from '../../errors.js';
import { audit } from '../../audit.js';
import { requireAuth, requireRole, SESSION_COOKIE } from './middleware.js';
import { changePassword, createUser, login, logout, ROLES, SESSION_TTL_MS } from './service.js';

export interface AuthOptions { pepper?: string; secureCookie: boolean }

// Simple per-IP login throttle (plan §12.6 per-identity rate limiting; a shared store comes with multi-replica deploys).
const attempts = new Map<string, { n: number; reset: number }>();
function throttled(ip: string): boolean {
  const now = Date.now();
  const a = attempts.get(ip);
  if (!a || a.reset < now) { attempts.set(ip, { n: 1, reset: now + 15 * 60_000 }); return false; }
  a.n++;
  return a.n > 30;
}

export function authRoutes(db: Db, opts: AuthOptions): Router {
  const r = Router();

  r.post('/login', async (req, res) => {
    const b = z.object({ email: z.string().email().max(200), password: z.string().min(1).max(128) }).parse(req.body);
    if (throttled(req.ip ?? 'unknown')) return res.status(429).json({ error: 'too many login attempts; try again in 15 minutes' });
    const { user, token } = await login(db, b.email, b.password, { ip: req.ip ?? '', userAgent: req.get('user-agent') ?? '', ...(opts.pepper ? { pepper: opts.pepper } : {}) });
    res.cookie(SESSION_COOKIE, token, { httpOnly: true, sameSite: 'lax', secure: opts.secureCookie, maxAge: SESSION_TTL_MS, path: '/' });
    return res.json({ user, token });
  });

  r.post('/logout', requireAuth, async (req, res) => {
    if (req.sessionToken) await logout(db, req.sessionToken);
    res.clearCookie(SESSION_COOKIE, { path: '/' });
    res.json({ ok: true });
  });

  r.get('/me', requireAuth, (req, res) => { res.json({ user: req.user }); });

  r.post('/change-password', requireAuth, async (req, res) => {
    const b = z.object({ currentPassword: z.string(), newPassword: z.string() }).parse(req.body);
    const err = passwordPolicyError(b.newPassword);
    if (err) throw new BadRequestError(err);
    await changePassword(db, req.user!.id, b.currentPassword, b.newPassword, opts.pepper);
    res.clearCookie(SESSION_COOKIE, { path: '/' });
    res.json({ ok: true });
  });

  r.get('/users', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT id, employee_id, email, name, roles, status, last_login_at, created_at FROM app_user WHERE company_id = $1 ORDER BY created_at`,
      [req.user!.companyId]);
    res.json({ users: rows.rows });
  });

  r.post('/users', requireRole('admin'), async (req, res) => {
    const b = z.object({
      email: z.string().email(), name: z.string().min(1), password: z.string(), roles: z.array(z.enum(ROLES)).min(1),
      employeeId: z.uuid().optional(),
    }).parse(req.body);
    const err = passwordPolicyError(b.password);
    if (err) throw new BadRequestError(err);
    const user = await createUser(db, {
      companyId: req.user!.companyId, employeeId: b.employeeId ?? null, email: b.email, name: b.name, password: b.password,
      roles: b.roles, mustChangePassword: true, actorId: req.user!.id, ...(opts.pepper ? { pepper: opts.pepper } : {}),
    });
    res.status(201).json({ user });
  });

  r.patch('/users/:id', requireRole('admin'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ roles: z.array(z.enum(ROLES)).min(1).optional(), status: z.enum(['active', 'disabled']).optional(), employeeId: z.uuid().nullable().optional() }).parse(req.body);
    if (id === req.user!.id && (b.status === 'disabled' || (b.roles && !b.roles.includes('admin')))) {
      throw new BadRequestError('you cannot disable your own account or remove your own admin role');
    }
    const target = await db.query<{ roles: string[]; status: string }>('SELECT roles, status FROM app_user WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!target.rows[0]) return res.status(404).json({ error: 'user not found' });
    const losesAdmin = target.rows[0].roles.includes('admin') && target.rows[0].status === 'active'
      && ((b.roles && !b.roles.includes('admin')) || b.status === 'disabled');
    if (losesAdmin) {
      const others = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM app_user WHERE company_id = $1 AND status = 'active' AND 'admin' = ANY(roles) AND id <> $2`, [req.user!.companyId, id]);
      if (others.rows[0]!.n === 0) throw new BadRequestError('this is the last active admin; add another admin first');
    }
    const rows = await db.query(
      `UPDATE app_user SET roles = coalesce($2, roles), status = coalesce($3, status), employee_id = CASE WHEN $5 THEN $4 ELSE employee_id END
        WHERE id = $1 AND company_id = $6 RETURNING id, email, name, roles, status, employee_id`,
      [id, b.roles ?? null, b.status ?? null, b.employeeId ?? null, b.employeeId !== undefined, req.user!.companyId]);
    if (!rows.rows[0]) return res.status(404).json({ error: 'user not found' });
    if (b.status === 'disabled') await db.query('UPDATE session SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL', [id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'app_user', rowId: id, before: target.rows[0], after: b });
    return res.json({ user: rows.rows[0] });
  });

  return r;
}
