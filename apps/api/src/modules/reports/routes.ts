import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** reports module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function reportsRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'reports module not implemented yet' }); });
  return r;
}
