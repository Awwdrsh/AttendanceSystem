import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** punch module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function punchRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'punch module not implemented yet' }); });
  return r;
}
