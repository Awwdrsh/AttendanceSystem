/**
 * Admin CLI:  pnpm --filter @hajiri/api cli <command> [options]
 *   create-admin --email <email> --password <pw> [--name <name>] [--company DEMO]
 *     (the password may come from HAJIRI_PASSWORD instead of --password, which keeps it out of the process list;
 *      the new admin must change it at first sign-in)
 *   seed-defaults [--company DEMO]        leave types, workflows, settings, rule versions, salary components
 *   reset-password --email <email> --password <pw>
 *   seed-demo-suite --database <url> --yes [--company DEMO] [--only a,b]   sample data for every HR module (never the .env database)
 */
import { loadConfig, loadDatabaseUrl } from './config.js';
import { createPool } from './db/pool.js';
import { createUser } from './modules/auth/service.js';
import { FieldCipher, hashPassword, passwordPolicyError } from './crypto.js';
import { seedDefaults } from './db/seed-defaults.js';

const args = process.argv.slice(2);
const cmd = args[0];
const opt = (name: string, def?: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] ?? def : def; };
const passwordArg = () => opt('password') ?? process.env.HAJIRI_PASSWORD;

const db = createPool(loadDatabaseUrl());
// Secrets are only required for password operations.
const pepper = () => loadConfig().PASSWORD_PEPPER;

async function companyId(code: string): Promise<string> {
  const r = await db.query<{ id: string }>('SELECT id FROM company WHERE code = $1', [code]);
  if (!r.rows[0]) throw new Error(`company ${code} not found (run db:seed:demo or create one first)`);
  return r.rows[0].id;
}

try {
  switch (cmd) {
    case 'create-admin': {
      const email = opt('email'); const password = passwordArg();
      if (!email || !password) throw new Error('--email and --password (or HAJIRI_PASSWORD) are required');
      const err = passwordPolicyError(password); if (err) throw new Error(err);
      const cid = await companyId(opt('company', 'DEMO')!);
      const u = await createUser(db, { companyId: cid, email, name: opt('name', 'Administrator')!, password, roles: ['admin'], mustChangePassword: true, pepper: pepper() });
      console.log(`admin user ${u.email} created (${u.id})`);
      break;
    }
    case 'reset-password': {
      const email = opt('email'); const password = passwordArg();
      if (!email || !password) throw new Error('--email and --password (or HAJIRI_PASSWORD) are required');
      const err = passwordPolicyError(password); if (err) throw new Error(err);
      const r = await db.query('UPDATE app_user SET password_hash = $2, failed_logins = 0, locked_until = NULL WHERE email = lower($1) RETURNING id', [email, await hashPassword(password, pepper())]);
      if (!r.rowCount) throw new Error('user not found');
      await db.query('UPDATE session SET revoked_at = now() WHERE user_id = $1', [r.rows[0].id]);
      console.log('password reset; all sessions revoked');
      break;
    }
    case 'seed-defaults': {
      const cid = await companyId(opt('company', 'DEMO')!);
      const summary = await seedDefaults(db, cid);
      console.log('defaults seeded:', summary);
      break;
    }
    case 'seed-demo-suite': {
      // Sample data for every HR module, through the real API. The target must be named on the command line and must not
      // be the database in apps/api/.env (the live one): a mistyped command can never fill production with demo people.
      const target = opt('database');
      if (!target || !args.includes('--yes')) {
        throw new Error('seed-demo-suite adds sample people data. Name the demo database with --database <url> and add --yes to confirm.');
      }
      const live = (await import('dotenv')).parse(await import('node:fs').then((fs) => { try { return fs.readFileSync(new URL('../.env', import.meta.url)); } catch { return ''; } })).DATABASE_URL;
      const norm = (u: string) => u.replace(/\/+$/, '').toLowerCase();
      if (live && norm(live) === norm(target)) throw new Error('refusing to add demo data to the database in apps/api/.env (the live database)');
      const demoDb = createPool(target);
      const cfg = loadConfig();
      const { seedDemoSuite } = await import('./db/demo-suite/index.js');
      const only = opt('only')?.split(',').map((s) => s.trim()).filter(Boolean);
      const out = await seedDemoSuite(demoDb, FieldCipher.fromBase64(cfg.DATA_ENCRYPTION_KEY), cfg.PASSWORD_PEPPER, { company: opt('company', 'DEMO')!, ...(only ? { only } : {}) });
      console.log(`done: ${out.done.join(', ') || 'nothing new'}; already there: ${out.skipped.join(', ') || 'none'}`);
      if (out.failed.length) { console.log(`failed: ${out.failed.map((f) => f.section).join(', ')}`); process.exitCode = 1; }
      await demoDb.end();
      break;
    }
    default:
      console.log('usage: cli create-admin|reset-password|seed-defaults|seed-demo-suite ...');
      process.exitCode = 1;
  }
} catch (e) {
  console.error((e as Error).message);
  process.exitCode = 1;
} finally {
  await db.end();
}
