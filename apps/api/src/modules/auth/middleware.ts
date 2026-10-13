import type { NextFunction, Request, Response } from 'express';
import type { Db } from '../../db/pool.js';
import { ForbiddenError, UnauthorizedError } from '../../errors.js';
import { resolveSession, type AuthUser, type Role } from './service.js';

export const SESSION_COOKIE = 'hajiri_session';

declare module 'express-serve-static-core' {
  interface Request { user?: AuthUser; sessionToken?: string }
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
export function sessionLoader(db: Db) {
  return async (req: Request, _res: Response, next: NextFunction) => {
    const bearer = req.headers.authorization?.startsWith('Bearer ') ? req.headers.authorization.slice(7) : undefined;
    const token = bearer ?? readCookie(req, SESSION_COOKIE);
    if (token) {
      const user = await resolveSession(db, token);
      if (user) { req.user = user; req.sessionToken = token; }
    }
    next();
  };
}

export function requireAuth(req: Request, _res: Response, next: NextFunction): void {
  if (!req.user) return next(new UnauthorizedError());
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

/** Employees may only read their own records; HR/admin/finance may read anyone in their company. */
export function canAccessEmployee(user: AuthUser, employeeId: string): boolean {
  if (hasRole(user, 'hr', 'finance', 'manager')) return true;
  return user.employeeId === employeeId;
}

export function assertAccessEmployee(user: AuthUser, employeeId: string): void {
  if (!canAccessEmployee(user, employeeId)) throw new ForbiddenError('not allowed to access this employee');
}
