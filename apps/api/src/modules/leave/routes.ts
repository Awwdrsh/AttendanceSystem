import { Router } from 'express';
import type { AppDeps } from '../../deps.js';

/** leave module. TODO: implement (see plan.md and docs/DEVELOPMENT.md). */
export function leaveRoutes(_deps: AppDeps): Router {
  const r = Router();
  r.get('/', (_req, res) => { res.status(501).json({ error: 'leave module not implemented yet' }); });
  return r;
}
