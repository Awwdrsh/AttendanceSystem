import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** payroll module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function payrollRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'payroll module not implemented yet' }); });
  return r;
}
