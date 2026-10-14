import { allSupportedDays, formatBs } from '@hajiri/bs-calendar';
import { loadDatabaseUrl } from '../config.js';
import { createPool, type Db } from './pool.js';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Upsert the BS calendar table from the package data (plan §8.4: runtime reads the table, not the library). */
export async function seedBsCalendar(db: Db): Promise<number> {
  // Five parallel arrays and unnest(): one round trip for every supported day, well under the bind-parameter limit.
  const y: number[] = []; const m: number[] = []; const d: number[] = []; const label: string[] = []; const ad: string[] = [];
  for (const day of allSupportedDays()) {
    y.push(day.bs.year); m.push(day.bs.month); d.push(day.bs.day); label.push(formatBs(day.bs)); ad.push(day.ad);
  }
  const res = await db.query(
    `INSERT INTO bs_calendar (bs_year, bs_month, bs_day, bs_date, ad_date)
     SELECT * FROM unnest($1::int[], $2::int[], $3::int[], $4::text[], $5::date[])
     ON CONFLICT (ad_date) DO UPDATE SET bs_year = EXCLUDED.bs_year, bs_month = EXCLUDED.bs_month, bs_day = EXCLUDED.bs_day, bs_date = EXCLUDED.bs_date`,
    [y, m, d, label, ad],
  );
  return res.rowCount ?? 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const db = createPool(loadDatabaseUrl());
  seedBsCalendar(db).then((n) => { console.log(`bs_calendar: ${n} rows upserted`); return db.end(); })
    .catch((e) => { console.error(e.message); process.exit(1); });
}
