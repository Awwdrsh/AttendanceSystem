import type { FieldCipher } from '../../crypto.js';
import type { Db } from '../pool.js';
import { buildContext, type DemoContext } from './context.js';
import { ApiError, Kit } from './kit.js';
import { seedCareer, seedProfiles, seedStructure } from './people.js';
import { seedAssets, seedDocuments, seedHelpdesk, seedLifecycle } from './records.js';
import { seedField, seedRoster, seedTime } from './work.js';
import { seedExpenses, seedTaxSavings } from './money.js';
import { seedHiring, seedLearning, seedPerformance } from './talent.js';
import { seedEngage } from './engage.js';

/** Sections run in order; each runs once per company (recorded under company_setting 'demo.suite'). */
const SECTIONS: Array<[string, (ctx: DemoContext) => Promise<void>]> = [
  ['structure', seedStructure],
  ['profiles', seedProfiles],
  ['career', seedCareer],
  ['documents', seedDocuments],
  ['assets', seedAssets],
  ['lifecycle', seedLifecycle],
  ['helpdesk', seedHelpdesk],
  ['time', seedTime],
  ['field', seedField],
  ['roster', seedRoster],
  ['expenses', seedExpenses],
  ['tax-savings', seedTaxSavings],
  ['performance', seedPerformance],
  ['learning', seedLearning],
  ['hiring', seedHiring],
  ['engage', seedEngage],
];
export const DEMO_SUITE_SECTIONS = SECTIONS.map(([name]) => name);

export interface SuiteResult { done: string[]; skipped: string[]; failed: Array<{ section: string; error: string }> }

/**
 * Fill every HR module with realistic sample data for one company, through the real API. Idempotent: a section that
 * finished before is skipped, so running it again only retries what failed. Never touches payroll runs.
 */
export async function seedDemoSuite(db: Db, cipher: FieldCipher, pepper: string, opts: { company: string; only?: string[]; log?: (m: string) => void }): Promise<SuiteResult> {
  const log = opts.log ?? ((m: string) => console.log(m));
  const kit = new Kit(db, cipher, pepper);
  await kit.start();
  const out: SuiteResult = { done: [], skipped: [], failed: [] };
  try {
    const ctx = await buildContext(db, kit, opts.company, log);
    const mark = await db.query<{ value: { done?: string[] } }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'demo.suite'`, [ctx.companyId]);
    const finished = new Set(mark.rows[0]?.value?.done ?? []);
    for (const [name, fn] of SECTIONS) {
      if (opts.only && !opts.only.includes(name)) continue;
      if (finished.has(name)) { out.skipped.push(name); continue; }
      try {
        await fn(ctx);
        finished.add(name);
        out.done.push(name);
        await db.query(
          `INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'demo.suite', $2)
           ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`, [ctx.companyId, JSON.stringify({ done: [...finished] })]);
        log(`  ✓ ${name}`);
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : (e as Error).stack ?? String(e);
        out.failed.push({ section: name, error: msg });
        log(`  ✗ ${name}: ${msg}`);
      }
    }
  } finally {
    await kit.stop();
  }
  return out;
}
