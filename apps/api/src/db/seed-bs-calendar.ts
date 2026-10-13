import { allSupportedDays, formatBs } from '@hajiri/bs-calendar';
import { loadConfig } from '../config.js';
import { createPool, type Db } from './pool.js';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Upsert the BS calendar table from the package data (plan §8.4: runtime reads the table, not the library). */
export async function seedBsCalendar(db: Db): Promise<number> {
  const rows: string[] = [];
  const params: unknown[] = [];
  for (const { bs, ad } of allSupportedDays()) {
    const i = params.length;
    rows.push(`($${i + 1}, $${i + 2}, $${i + 3}, $${i + 4}, $${i + 5})`);
    params.push(bs.year, bs.month, bs.day, formatBs(bs), ad);
  }
  const res = await db.query(
    `INSERT INTO bs_calendar (bs_year, bs_month, bs_day, bs_date, ad_date) VALUES ${rows.join(',')}
     ON CONFLICT (ad_date) DO UPDATE SET bs_year = EXCLUDED.bs_year, bs_month = EXCLUDED.bs_month, bs_day = EXCLUDED.bs_day, bs_date = EXCLUDED.bs_date`,
    params,
  );
  return res.rowCount ?? 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const db = createPool(loadConfig().DATABASE_URL);
  seedBsCalendar(db).then((n) => { console.log(`bs_calendar: ${n} rows upserted`); return db.end(); })
    .catch((e) => { console.error(e.message); process.exit(1); });
}
