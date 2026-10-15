import type { Db, Queryable } from '../../db/pool.js';
import { NotFoundError } from '../../errors.js';
import { hasRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';

/** Run fn inside one transaction on a pooled client. */
export async function tx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    const out = await fn(client);
    await client.query('COMMIT');
    return out;
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();
  }
}

/** The employee must belong to the company; another company's employee is "not found". */
export async function employeeInCompany(db: Queryable, companyId: string, employeeId: string): Promise<{ id: string; name_en: string; designation: string | null; grade: string | null }> {
  const r = await db.query<{ id: string; name_en: string; designation: string | null; grade: string | null }>(
    'SELECT id, name_en, designation, grade FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

/** Line managers above an employee today (direct manager first), up to five levels, cycle-safe. */
export async function managerChain(db: Queryable, employeeId: string): Promise<string[]> {
  const r = await db.query<{ manager_id: string }>(
    `WITH RECURSIVE chain(emp, manager_id, depth, path) AS (
       SELECT a.employee_id, a.manager_id, 1, ARRAY[a.employee_id] FROM employee_assignment a
        WHERE a.employee_id = $1 AND a.valid @> current_date AND a.manager_id IS NOT NULL
       UNION ALL
       SELECT a.employee_id, a.manager_id, c.depth + 1, c.path || a.employee_id FROM chain c
         JOIN employee_assignment a ON a.employee_id = c.manager_id AND a.valid @> current_date
        WHERE a.manager_id IS NOT NULL AND c.depth < 5 AND NOT a.manager_id = ANY(c.path)
     ) SELECT manager_id FROM chain ORDER BY depth`, [employeeId]);
  return r.rows.map((x) => x.manager_id);
}

/** Direct manager today, if any. */
export async function directManager(db: Queryable, employeeId: string): Promise<string | null> {
  const r = await db.query<{ manager_id: string | null }>('SELECT manager_id FROM employee_assignment WHERE employee_id = $1 AND valid @> current_date', [employeeId]);
  return r.rows[0]?.manager_id ?? null;
}

/** HR, the employee, or anyone in their manager chain. */
export async function canSeeEmployee(db: Queryable, user: AuthUser, employeeId: string): Promise<boolean> {
  if (hasRole(user, 'hr')) return true;
  if (!user.employeeId) return false;
  if (user.employeeId === employeeId) return true;
  return (await managerChain(db, employeeId)).includes(user.employeeId);
}

/** HR, or someone in the employee's manager chain (not the employee). */
export async function isAboveInChain(db: Queryable, user: AuthUser, employeeId: string): Promise<boolean> {
  if (hasRole(user, 'hr')) return true;
  if (!user.employeeId || user.employeeId === employeeId) return false;
  return (await managerChain(db, employeeId)).includes(user.employeeId);
}

// ---------------------------------------------------------------- progress and scores

export interface KeyResultValues { measure: string; start_value: string | number; target_value: string | number; current_value: string | number; weight: string | number }

/** Progress of one key result in percent, clamped to 0..100. Works for decreasing targets (e.g. cut defects 20 → 5). */
export function keyResultProgress(k: KeyResultValues): number {
  const cur = Number(k.current_value);
  if (k.measure === 'boolean') return cur >= 1 ? 100 : 0;
  const start = Number(k.start_value); const target = Number(k.target_value);
  if (target === start) return cur === target ? 100 : (target > start ? (cur >= target ? 100 : 0) : (cur <= target ? 100 : 0));
  const p = ((cur - start) / (target - start)) * 100;
  return Math.round(Math.min(100, Math.max(0, p)) * 100) / 100;
}

/** Weighted average of the key results; null when the goal has none (progress is then set by check-ins). */
export function goalProgress(krs: KeyResultValues[]): number | null {
  if (!krs.length) return null;
  const total = krs.reduce((s, k) => s + Number(k.weight), 0);
  if (total <= 0) return null;
  const p = krs.reduce((s, k) => s + keyResultProgress(k) * Number(k.weight), 0) / total;
  return Math.round(p * 100) / 100;
}

export async function recomputeGoalProgress(db: Queryable, goalId: string): Promise<void> {
  const krs = await db.query<KeyResultValues>('SELECT measure, start_value, target_value, current_value, weight FROM perf_key_result WHERE goal_id = $1', [goalId]);
  const p = goalProgress(krs.rows);
  if (p !== null) await db.query('UPDATE perf_goal SET progress = $2, updated_at = now() WHERE id = $1', [goalId, p]);
}

export interface ScoredItem { item_type: 'goal' | 'competency'; weight: string | number; rating: number | null }

/**
 * Overall score on the 1–5 scale: the weighted goal average and the plain competency average, blended with the
 * cycle's weights. A part with no ratings is left out and the other part carries the full weight.
 */
export function overallScore(items: ScoredItem[], goalsWeight: number, competencyWeight: number): number | null {
  const goals = items.filter((i) => i.item_type === 'goal' && i.rating !== null);
  const comps = items.filter((i) => i.item_type === 'competency' && i.rating !== null);
  const gw = goals.reduce((s, i) => s + (Number(i.weight) > 0 ? Number(i.weight) : 1), 0);
  const g = goals.length ? goals.reduce((s, i) => s + i.rating! * (Number(i.weight) > 0 ? Number(i.weight) : 1), 0) / gw : null;
  const c = comps.length ? comps.reduce((s, i) => s + i.rating!, 0) / comps.length : null;
  if (g === null && c === null) return null;
  if (g === null) return round2(c!);
  if (c === null) return round2(g);
  const total = goalsWeight + competencyWeight;
  if (total <= 0) return round2((g + c) / 2);
  return round2((g * goalsWeight + c * competencyWeight) / total);
}

export const round2 = (n: number) => Math.round(n * 100) / 100;

/** Plain-text escaping for the printable pages. */
export const esc = (s: unknown) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

/** Once-only guard for reminders: returns true the first time a key is seen. */
export async function firstTime(db: Queryable, key: string): Promise<boolean> {
  const r = await db.query('INSERT INTO perf_reminder_sent (key) VALUES ($1) ON CONFLICT DO NOTHING', [key]);
  return (r.rowCount ?? 0) > 0;
}
