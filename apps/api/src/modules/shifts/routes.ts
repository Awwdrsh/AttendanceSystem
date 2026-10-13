import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** shifts module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function shiftsRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'shifts module not implemented yet' }); });
  return r;
}
