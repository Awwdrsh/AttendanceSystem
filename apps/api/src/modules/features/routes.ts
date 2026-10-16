import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { companyIdOf, requireRole } from '../auth/middleware.js';
import { disabledFeatures, FEATURE_KEYS, FEATURE_SETTING, FEATURES, invalidateFeatures, type FeatureKey } from './service.js';

const view = (off: Set<FeatureKey>) => ({
  features: FEATURES.map((f) => ({ ...f, enabled: !off.has(f.key) })),
  enabled: Object.fromEntries(FEATURE_KEYS.map((k) => [k, !off.has(k)])) as Record<FeatureKey, boolean>,
});

/** GET /settings/features for every signed-in user (it drives the menu); PUT for HR and admin. */
export function featuresRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/', async (req, res) => {
    res.json(view(await disabledFeatures(db, companyIdOf(req))));
  });

  r.put('/', requireRole('hr', 'admin'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ enabled: z.partialRecord(z.enum(FEATURE_KEYS as [FeatureKey, ...FeatureKey[]]), z.boolean()) }).strict().parse(req.body);
    const before = await disabledFeatures(db, companyId);
    const off = new Set(before);
    for (const [k, on] of Object.entries(b.enabled) as Array<[FeatureKey, boolean]>) { if (on) off.delete(k); else off.add(k); }
    const value = { off: FEATURE_KEYS.filter((k) => off.has(k)) };
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [companyId, FEATURE_SETTING, JSON.stringify(value), req.user!.id]);
    invalidateFeatures(companyId);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: FEATURE_SETTING, before: { off: [...before] }, after: value });
    res.json(view(off));
  });

  return r;
}
