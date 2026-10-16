import type { NextFunction, Request, Response } from 'express';
import type { Queryable } from '../../db/pool.js';
import { nptToday } from '@hajiri/attendance-engine';
import { ForbiddenError, NotFoundError, UnauthorizedError } from '../../errors.js';
import { resolveSession, type AuthUser, type Role } from './service.js';

export const SESSION_COOKIE = 'hajiri_session';
export const TWO_FACTOR_SETUP_MESSAGE = 'two-step sign-in setup required';

declare module 'express-serve-static-core' {
  interface Request { user?: AuthUser; sessionToken?: string; authMethod?: 'cookie' | 'bearer' }
}

function readCookie(req: Request, name: string): string | undefined {
  const raw = req.headers.cookie;
  if (!raw) return undefined;
  for (const part of raw.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === name) return decodeURIComponent(v.join('='));
  }
  return undefined;
}

/** Attaches req.user from the session cookie or a Bearer token; does not reject. */
export function sessionLoader(db: Queryable) {
  return async (req: Request, _res: Response, next: NextFunction) => {
    const bearer = req.headers.authorization?.startsWith('Bearer ') ? req.headers.authorization.slice(7) : undefined;
    const token = bearer ?? readCookie(req, SESSION_COOKIE);
    if (token) {
      const user = await resolveSession(db, token);
      if (user) { req.user = user; req.sessionToken = token; req.authMethod = bearer ? 'bearer' : 'cookie'; }
    }
    next();
  };
}

/**
 * CSRF defence for cookie sessions: a state-changing request carried by the session cookie must come from this
 * site's own origin (or a configured CORS origin). SameSite=Lax already stops other sites; this also stops other
 * apps on the same host (e.g. another localhost port), which count as same-site. Bearer-token clients are unaffected.
 */
export function sameOriginGuard(allowedOrigins: string[]) {
  const allowed = new Set(allowedOrigins);
  return (req: Request, _res: Response, next: NextFunction) => {
    if (!req.user || req.authMethod !== 'cookie' || ['GET', 'HEAD', 'OPTIONS'].includes(req.method)) return next();
    let origin = req.get('origin');
    if (!origin || origin === 'null') {
      const ref = req.get('referer');
      if (!ref) return origin === 'null' ? next(new ForbiddenError('cross-site request refused')) : next();
      try { origin = new URL(ref).origin; } catch { return next(new ForbiddenError('cross-site request refused')); }
    }
    const self = `${req.protocol}://${req.get('host')}`;
    if (origin === self || allowed.has(origin)) return next();
    return next(new ForbiddenError('cross-site request refused'));
  };
}

export function requireAuth(req: Request, _res: Response, next: NextFunction): void {
  if (!req.user) return next(new UnauthorizedError());
  next();
}

/** Users created with a temporary password must change it before using anything but /auth. */
export function requirePasswordChanged(req: Request, _res: Response, next: NextFunction): void {
  if (req.user?.mustChangePassword) return next(new ForbiddenError('password change required'));
  // The company requires two-step sign-in for this role: nothing but /auth works until it is set up.
  if (req.user?.mustSetUpTwoFactor) return next(new ForbiddenError(TWO_FACTOR_SETUP_MESSAGE));
  next();
}

/** Deny by default: the user must hold at least one of the roles. Admin always passes. */
export function requireRole(...roles: Role[]) {
  return (req: Request, _res: Response, next: NextFunction) => {
    if (!req.user) return next(new UnauthorizedError());
    if (req.user.roles.includes('admin') || roles.some((r) => req.user!.roles.includes(r))) return next();
    next(new ForbiddenError(`requires role ${roles.join(' or ')}`));
  };
}

export function hasRole(user: AuthUser, ...roles: Role[]): boolean {
  return user.roles.includes('admin') || roles.some((r) => user.roles.includes(r));
}

/**
 * Manager relationship on the given Nepal date (default today): the line manager, the head of the employee's
 * department (or of a department above it) or the head of their branch. Defined once in SQL as hj_manages().
 */
export async function isManagerOf(db: Queryable, managerEmployeeId: string, employeeId: string, onDate = nptToday()): Promise<boolean> {
  const r = await db.query<{ ok: boolean }>('SELECT hj_manages($1::uuid, $2::uuid, $3::date) AS ok', [managerEmployeeId, employeeId, onDate]);
  return r.rows[0]?.ok === true;
}

/**
 * SQL fragment for "the viewer manages this employee" (same rule as isManagerOf), for list queries.
 * `viewer` and `date` are SQL expressions (usually placeholders), `target` the employee id column.
 */
export const managesSql = (viewer: string, target: string, date = 'current_date') => `hj_manages(${viewer}::uuid, ${target}, ${date})`;

/**
 * Who may work in each HR area besides hr and admin. Specialist roles never see other areas; executive is read-only.
 * Keep this the single place that decides; routes call requireModule()/hasModule().
 */
export const MODULE_ROLES = {
  payroll: ['hr', 'payroll_admin'],
  attendance: ['hr', 'attendance_admin'],
  hiring: ['hr', 'recruiter'],
  insights: ['hr', 'executive'],
} as const satisfies Record<string, readonly Role[]>;
export type HrModule = keyof typeof MODULE_ROLES;

export const requireModule = (m: HrModule) => requireRole(...MODULE_ROLES[m]);
export const hasModule = (user: AuthUser, m: HrModule): boolean => hasRole(user, ...MODULE_ROLES[m]);

/**
 * Who may see or act for an employee: HR and admin company-wide; a manager only for direct reports; everyone
 * for themselves; with `area`, that area's specialist role (e.g. attendance_admin) as well. An employee of another company is reported as not found so ids cannot be enumerated across tenants.
 */
export async function assertManages(db: Queryable, user: AuthUser, employeeId: string, area?: HrModule): Promise<void> {
  const inCompany = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, user.companyId]);
  if (inCompany.rows.length === 0) throw new NotFoundError('employee not found');
  if (hasRole(user, 'hr')) return;
  if (area && hasModule(user, area)) return;
  if (user.employeeId === employeeId) return;
  if (hasRole(user, 'manager') && user.employeeId && (await isManagerOf(db, user.employeeId, employeeId))) return;
  throw new ForbiddenError('not allowed to access this employee');
}

/** Same as assertManages (kept for callers written against the earlier name). */
export const assertEmployeeAccess = assertManages;

/** The signed-in user's company; accounts without one (none exist outside tests) cannot use company data. */
export function companyIdOf(req: Request): string {
  const id = req.user?.companyId;
  if (!id) throw new ForbiddenError('this account is not linked to a company');
  return id;
}
