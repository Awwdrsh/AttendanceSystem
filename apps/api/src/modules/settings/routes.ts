import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** settings module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function settingsRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'settings module not implemented yet' }); });
  return r;
}
