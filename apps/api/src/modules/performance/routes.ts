import { Router } from 'express';
import type { AppDeps } from '../../deps.js';
import { conversationRoutes } from './conversations.js';
import { goalRoutes } from './goals.js';
import { reviewRoutes } from './reviews.js';

/**
 * Performance: goals and key results with check-ins and a KPI library; the competency framework; review cycles (self,
 * manager, peer, calibration, release, acknowledgement); the manager's journal; continuous feedback; 1-on-1s.
 */
export function performanceRoutes(deps: AppDeps): Router {
  const r = Router();
  goalRoutes(r, deps);
  reviewRoutes(r, deps);
  conversationRoutes(r, deps);
  return r;
}
