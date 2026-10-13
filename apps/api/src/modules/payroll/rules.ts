import type { RuleSet } from '@hajiri/payroll-engine';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError } from '../../errors.js';

const REQUIRED = ['tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance', 'min_wage'];

/** Resolve every rule as of a date (plan §6.13): newest version whose validity covers the date. */
export async function resolveRules(db: Queryable, asOfAd: string): Promise<{ rules: RuleSet; ids: string[] }> {
  const r = await db.query<{ id: string; rule_key: string; value: unknown }>(
    `SELECT DISTINCT ON (rule_key) id, rule_key, value FROM rule_version
      WHERE jurisdiction = 'NP' AND valid_from_ad <= $1 AND (valid_to_ad IS NULL OR valid_to_ad >= $1)
      ORDER BY rule_key, valid_from_ad DESC, created_at DESC`, [asOfAd]);
  const rules: Record<string, unknown> = {};
  const ids: string[] = [];
  for (const row of r.rows) { rules[row.rule_key] = row.value; ids.push(row.id); }
  const missing = REQUIRED.filter((k) => !(k in rules));
  if (missing.length) throw new BadRequestError(`statutory rules missing for ${asOfAd}: ${missing.join(', ')} (run db:seed:defaults or add rule versions)`);
  return { rules: rules as unknown as RuleSet, ids };
}
