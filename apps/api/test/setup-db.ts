import 'dotenv/config';
import { migrate, MIGRATIONS_DIR } from '../src/db/migrate.js';
import { createPool, type Db } from '../src/db/pool.js';
import { seedBsCalendar } from '../src/db/seed-bs-calendar.js';
import { seedDemo } from '../src/db/seed-demo.js';

/** Integration tests run against DATABASE_URL_TEST (default: hajiri_test on the local PG18). */
export const TEST_DATABASE_URL = process.env.DATABASE_URL_TEST ?? 'postgres://localhost:5433/hajiri_test';

export async function freshDb(): Promise<Db> {
  const db = createPool(TEST_DATABASE_URL);
  await db.query('DROP SCHEMA public CASCADE; CREATE SCHEMA public;');
  await migrate(db, MIGRATIONS_DIR, () => undefined);
  await seedBsCalendar(db);
  await seedDemo(db);
  return db;
}

export const DEMO = {
  companyId: '00000000-0000-7000-8000-000000000001',
  branchId: '00000000-0000-7000-8000-000000000002',
  shiftId: '00000000-0000-7000-8000-000000000003',
  sita: '00000000-0000-7000-8000-000000000011',
  ram: '00000000-0000-7000-8000-000000000012',
  deviceId: '00000000-0000-7000-8000-000000000021',
};
