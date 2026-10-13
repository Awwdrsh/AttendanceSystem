import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** ot module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function otRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'ot module not implemented yet' }); });
  return r;
}
