import 'dotenv/config';
import { randomBytes } from 'node:crypto';
import request from 'supertest';
import type { Express } from 'express';
import { createApp } from '../src/app.js';
import { FieldCipher } from '../src/crypto.js';
import { migrate, MIGRATIONS_DIR } from '../src/db/migrate.js';
import { createPool, type Db } from '../src/db/pool.js';
import { seedBsCalendar } from '../src/db/seed-bs-calendar.js';
import { seedDefaults } from '../src/db/seed-defaults.js';
import { seedDemo } from '../src/db/seed-demo.js';
import type { Role } from '../src/modules/auth/service.js';
import { createUser } from '../src/modules/auth/service.js';

/** Integration tests run against DATABASE_URL_TEST (default: hajiri_test on the local PG18). */
export const TEST_DATABASE_URL = process.env.DATABASE_URL_TEST ?? 'postgres://localhost:5433/hajiri_test';
export const TEST_PEPPER = 'test-pepper-not-secret';
export const testCipher = new FieldCipher(Buffer.alloc(32, 7));

export const DEMO = {
  companyId: '00000000-0000-7000-8000-000000000001',
  branchId: '00000000-0000-7000-8000-000000000002',
  shiftId: '00000000-0000-7000-8000-000000000003',
  sita: '00000000-0000-7000-8000-000000000011',
  ram: '00000000-0000-7000-8000-000000000012',
  deviceId: '00000000-0000-7000-8000-000000000021',
};

/**
 * Empty the test database. One `DROP SCHEMA public CASCADE` takes a lock on every object in a single transaction; with
 * the full schema that can exhaust the server's shared lock table (max_locks_per_transaction x connections), more so
 * when several suites share one server. Dropping foreign keys, views, then tables one statement at a time keeps each
 * transaction to a handful of locks.
 */
async function dropEverything(db: Db): Promise<void> {
  const fks = await db.query<{ tbl: string; name: string }>(
    `SELECT c.relname AS tbl, k.conname AS name FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND k.contype = 'f' AND k.conparentid = 0`);
  for (const f of fks.rows) await db.query(`ALTER TABLE IF EXISTS public."${f.tbl}" DROP CONSTRAINT IF EXISTS "${f.name}"`);
  const views = await db.query<{ name: string; kind: string }>(
    `SELECT c.relname AS name, c.relkind AS kind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')`);
  for (const v of views.rows) await db.query(`DROP ${v.kind === 'm' ? 'MATERIALIZED VIEW' : 'VIEW'} IF EXISTS public."${v.name}" CASCADE`);
  const tables = await db.query<{ name: string }>(
    `SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relispartition DESC, c.relkind, c.relname`);
  for (const t of tables.rows) await db.query(`DROP TABLE IF EXISTS public."${t.name}" CASCADE`);
  await db.query('DROP SCHEMA public CASCADE; CREATE SCHEMA public;');
}

export async function freshDb(): Promise<Db> {
  const db = createPool(TEST_DATABASE_URL);
  // Rebuilding a schema of a few hundred tables takes many locks from the server-wide lock table; when several test
  // databases on the same server are rebuilt at once it can run out ("out of shared memory"). Wait and retry.
  for (let attempt = 1; ; attempt++) {
    try { await dropEverything(db); break; }
    catch (e) {
      if ((e as { code?: string }).code !== '53200' || attempt >= 6) throw e;
      await new Promise((r) => setTimeout(r, 1000 * attempt));
    }
  }
  await migrate(db, MIGRATIONS_DIR, () => undefined);
  await seedBsCalendar(db);
  await seedDemo(db);
  await seedDefaults(db, DEMO.companyId);
  return db;
}

export function testApp(db: Db): Express {
  return createApp({ db, cipher: testCipher, pepper: TEST_PEPPER, secureCookie: false });
}

/** Create a user with the given roles and return a Bearer token for it. */
export async function loginAs(db: Db, app: Express, roles: Role[], employeeId: string | null = null, email?: string): Promise<{ token: string; userId: string }> {
  const addr = email ?? `${roles.join('-')}-${randomBytes(4).toString('hex')}@test.local`;
  const password = 'correct-horse-battery-staple';
  const u = await createUser(db, { companyId: DEMO.companyId, employeeId, email: addr, name: addr, password, roles, pepper: TEST_PEPPER });
  const res = await request(app).post('/api/v1/auth/login').send({ email: addr, password });
  if (res.status !== 200) throw new Error(`login failed: ${res.status} ${res.text}`);
  return { token: res.body.token as string, userId: u.id };
}

export const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
