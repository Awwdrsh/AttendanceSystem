import type { NextFunction, Request, Response } from 'express';
import type { Queryable } from '../../db/pool.js';

/**
 * Software feature control: each company can switch whole modules off. A switched-off module disappears from the menu
 * and its API answers 404 "feature off". Everything is on until HR turns it off, so the setting stores only the keys
 * that are off (a module added later starts switched on).
 */
export const FEATURES = [
  { key: 'time', label: 'Time tracking & timesheets', description: 'Project timer, weekly timesheets, projects and clients.', group: 'Time & attendance' },
  { key: 'field', label: 'Field visits, breaks & kiosks', description: 'Field visits with GPS, break tracking and shared clock-in tablets.', group: 'Time & attendance' },
  { key: 'expenses', label: 'Expense claims & travel', description: 'Expense claims, travel requests, allowances and settlements.', group: 'Pay & money' },
  { key: 'performance', label: 'Goals & reviews', description: 'Goals and KPIs, review cycles, feedback, 1-on-1s and the manager journal.', group: 'Talent' },
  { key: 'learning', label: 'Learning & training', description: 'Course catalogue, sessions, training requests, quizzes and certificates.', group: 'Talent' },
  { key: 'hiring', label: 'Hiring & careers page', description: 'Requisitions, jobs, the public careers page, candidates, interviews and offers.', group: 'Talent' },
  { key: 'lifecycle', label: 'Onboarding, exits & assets', description: 'Joining checklists, probation reviews, resignations, clearance and company assets.', group: 'People' },
  { key: 'documents', label: 'Documents & letters', description: 'Employee documents, letter templates and requests, company policies.', group: 'People' },
  { key: 'helpdesk', label: 'Help desk', description: 'Employee questions and requests to HR, with response targets.', group: 'People' },
  { key: 'tasks', label: 'Tasks', description: 'Personal and team task boards.', group: 'People' },
  { key: 'kudos', label: 'Kudos', description: 'Peer appreciation with company values and a leaderboard.', group: 'Engagement' },
  { key: 'surveys', label: 'Surveys & polls', description: 'Pulse surveys, eNPS and quick polls.', group: 'Engagement' },
  { key: 'suggestions', label: 'Suggestion box', description: 'Anonymous suggestions to HR.', group: 'Engagement' },
] as const;

export type FeatureKey = typeof FEATURES[number]['key'];
export const FEATURE_KEYS = FEATURES.map((f) => f.key) as FeatureKey[];
export const FEATURE_SETTING = 'features';

const TTL_MS = 30_000;
const cache = new Map<string, { at: number; off: Set<FeatureKey> }>();

export function invalidateFeatures(companyId?: string): void {
  if (companyId) cache.delete(companyId); else cache.clear();
}

/** The switched-off modules of a company (cached for 30 s; a change through the API clears the cache at once). */
export async function disabledFeatures(db: Queryable, companyId: string): Promise<Set<FeatureKey>> {
  const hit = cache.get(companyId);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.off;
  const r = await db.query<{ value: { off?: unknown } }>('SELECT value FROM company_setting WHERE company_id = $1 AND key = $2', [companyId, FEATURE_SETTING]);
  const raw = Array.isArray(r.rows[0]?.value?.off) ? (r.rows[0]!.value.off as unknown[]) : [];
  const off = new Set(raw.filter((k): k is FeatureKey => typeof k === 'string' && (FEATURE_KEYS as string[]).includes(k)));
  cache.set(companyId, { at: Date.now(), off });
  return off;
}

export async function featureOn(db: Queryable, companyId: string, key: FeatureKey): Promise<boolean> {
  return !(await disabledFeatures(db, companyId)).has(key);
}

/**
 * Middleware for a module mount (`api.use('/time', featureGate(db, 'time'), ...)`). With `paths`, only those sub-paths
 * are gated, for features that are one part of a router (e.g. /engage/kudos).
 */
export function featureGate(db: Queryable, key: FeatureKey, paths?: string[]) {
  return async (req: Request, res: Response, next: NextFunction) => {
    const companyId = req.user?.companyId;
    if (!companyId) return next();
    if (paths && !paths.some((p) => req.path === p || req.path.startsWith(`${p}/`) || req.path.startsWith(`${p}.`))) return next();
    try {
      if (await featureOn(db, companyId, key)) return next();
      res.status(404).json({ error: 'feature off', feature: key });
    } catch (e) { next(e); }
  };
}
