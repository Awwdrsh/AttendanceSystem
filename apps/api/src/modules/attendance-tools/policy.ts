import { z } from 'zod';
import type { Queryable } from '../../db/pool.js';

/**
 * Attendance extras: blocking clock-ins after the grace time, the late-arrival penalty and the scope of the "Who's out"
 * card. Stored as the company_setting `attendance.extras`.
 */
export const latePenaltySchema = z.object({
  enabled: z.boolean().default(false),
  /** Every `lateCount` late arrivals in a BS month cost `penaltyDays`. */
  lateCount: z.number().int().min(1).max(31).default(3),
  /** A day counts as late when the arrival is at least this many minutes after the grace time. */
  minLateMinutes: z.number().int().min(1).max(240).default(1),
  penaltyDays: z.number().min(0.25).max(5).multipleOf(0.25).default(0.5),
  /** Leave types the penalty is taken from, in order; whatever they cannot cover is unpaid. */
  leaveTypeIds: z.array(z.uuid()).max(5).default([]),
});

export const extrasPolicySchema = z.object({
  blockLateClockIn: z.boolean().default(false),
  /** Extra minutes after the grace time before a clock-in is refused. */
  blockAfterMin: z.number().int().min(0).max(240).default(0),
  whosOutScope: z.enum(['team', 'department', 'company']).default('team'),
  latePenalty: latePenaltySchema.default(latePenaltySchema.parse({})),
});
export type ExtrasPolicy = z.infer<typeof extrasPolicySchema>;
export type LatePenaltyRule = z.infer<typeof latePenaltySchema>;

export const EXTRAS_KEY = 'attendance.extras';

export async function extrasPolicy(db: Queryable, companyId: string): Promise<ExtrasPolicy> {
  const r = await db.query<{ value: unknown }>('SELECT value FROM company_setting WHERE company_id = $1 AND key = $2', [companyId, EXTRAS_KEY]);
  const parsed = extrasPolicySchema.safeParse(r.rows[0]?.value ?? {});
  return parsed.success ? parsed.data : extrasPolicySchema.parse({});
}

export async function saveExtrasPolicy(db: Queryable, companyId: string, value: ExtrasPolicy, userId: string): Promise<void> {
  await db.query(
    `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, $2, $3, $4)
     ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()`,
    [companyId, EXTRAS_KEY, JSON.stringify(value), userId]);
}
