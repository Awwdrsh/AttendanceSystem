import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** notifications module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function notificationsRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'notifications module not implemented yet' }); });
  return r;
}
