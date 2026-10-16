/**
 * Admin CLI:  pnpm --filter @hajiri/api cli <command> [options]
 *   create-admin --email <email> --password <pw> [--name <name>] [--company DEMO]
 *     (the password may come from HAJIRI_PASSWORD instead of --password, which keeps it out of the process list;
 *      the new admin must change it at first sign-in)
 *   seed-defaults [--company DEMO]        leave types, workflows, settings, rule versions, salary components
 *   reset-password --email <email> --password <pw>
 *   seed-demo-suite --yes [--company DEMO] [--only a,b]   sample data for every HR module (DATABASE_URL must be set)
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
      // Sample data for every HR module, through the real API. DATABASE_URL must name the target database explicitly.
      if (!process.env.DATABASE_URL || !args.includes('--yes')) {
        throw new Error('seed-demo-suite adds sample people data. Set DATABASE_URL to the demo database and add --yes to confirm.');
      }
      const cfg = loadConfig();
      const { seedDemoSuite } = await import('./db/demo-suite/index.js');
      const only = opt('only')?.split(',').map((s) => s.trim()).filter(Boolean);
      const out = await seedDemoSuite(db, FieldCipher.fromBase64(cfg.DATA_ENCRYPTION_KEY), cfg.PASSWORD_PEPPER, { company: opt('company', 'DEMO')!, ...(only ? { only } : {}) });
      console.log(`done: ${out.done.join(', ') || 'nothing new'}; already there: ${out.skipped.join(', ') || 'none'}`);
      if (out.failed.length) { console.log(`failed: ${out.failed.map((f) => f.section).join(', ')}`); process.exitCode = 1; }
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
