import { z } from 'zod';
import type { Queryable } from '../../db/pool.js';

export const BREAK_KINDS = ['lunch', 'tea', 'prayer', 'personal'] as const;
export type BreakKind = typeof BREAK_KINDS[number];

const breakRule = (paid: boolean, maxMin: number) => z.object({
  enabled: z.boolean().default(true),
  paid: z.boolean().default(paid),
  maxMin: z.number().int().min(5).max(180).default(maxMin),
}).default({ enabled: true, paid, maxMin });

/**
 * Field, kiosk and break rules (company_setting key `field.policy`). Kept apart from attendance.policy so changing
 * them never triggers an attendance recompute.
 * - geofenceMode: off = locations are ignored; flag = a punch outside needs a reason and is flagged; block = refused.
 */
export const fieldPolicySchema = z.object({
  geofenceMode: z.enum(['off', 'flag', 'block']).default('flag'),
  breaks: z.object({
    lunch: breakRule(false, 60),
    tea: breakRule(true, 15),
    prayer: breakRule(true, 15),
    personal: breakRule(false, 15),
  }).default({ lunch: { enabled: true, paid: false, maxMin: 60 }, tea: { enabled: true, paid: true, maxMin: 15 }, prayer: { enabled: true, paid: true, maxMin: 15 }, personal: { enabled: true, paid: false, maxMin: 15 } }),
  fieldPhotoRequired: z.boolean().default(false),
  fieldVisitClockIn: z.boolean().default(true),
});
export type FieldPolicy = z.infer<typeof fieldPolicySchema>;

export const FIELD_POLICY_KEY = 'field.policy';

export async function fieldPolicy(db: Queryable, companyId: string): Promise<FieldPolicy> {
  const r = await db.query<{ value: unknown }>('SELECT value FROM company_setting WHERE company_id = $1 AND key = $2', [companyId, FIELD_POLICY_KEY]);
  const parsed = fieldPolicySchema.safeParse(r.rows[0]?.value ?? {});
  return parsed.success ? parsed.data : fieldPolicySchema.parse({});
}
