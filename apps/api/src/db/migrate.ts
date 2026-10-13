import { createHash } from 'node:crypto';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadDatabaseUrl } from '../config.js';
import { createPool, type Db } from './pool.js';

/**
 * Minimal forward-only SQL migration runner.
 * Files: migrations/NNNN_name.sql, applied in lexical order, each inside one transaction.
 * A file's sha256 is recorded; editing an applied file fails loudly instead of silently diverging.
 */
export async function migrate(db: Db, dir: string, log: (m: string) => void = console.log): Promise<string[]> {
  await db.query(`CREATE TABLE IF NOT EXISTS schema_migrations (
    name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())`);
  const files = (await readdir(dir)).filter((f) => /^\d{4}_.+\.sql$/.test(f)).sort();
  const applied = new Map<string, string>(
    (await db.query<{ name: string; checksum: string }>('SELECT name, checksum FROM schema_migrations')).rows.map((r) => [r.name, r.checksum]),
  );
  const ran: string[] = [];
  for (const file of files) {
    const sql = await readFile(path.join(dir, file), 'utf8');
    const checksum = createHash('sha256').update(sql).digest('hex');
    const prev = applied.get(file);
    if (prev) {
      if (prev !== checksum) throw new Error(`Migration ${file} was modified after being applied (checksum mismatch)`);
      continue;
    }
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      await client.query(sql);
      await client.query('INSERT INTO schema_migrations (name, checksum) VALUES ($1, $2)', [file, checksum]);
      await client.query('COMMIT');
      log(`applied ${file}`);
      ran.push(file);
    } catch (e) {
      await client.query('ROLLBACK');
      throw new Error(`Migration ${file} failed: ${(e as Error).message}`);
    } finally {
      client.release();
    }
  }
  return ran;
}

export const MIGRATIONS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../migrations');

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const db = createPool(loadDatabaseUrl());
  migrate(db, MIGRATIONS_DIR)
    .then((ran) => { console.log(ran.length ? `${ran.length} migration(s) applied` : 'up to date'); return db.end(); })
    .catch((e) => { console.error(e.message); process.exit(1); });
}
