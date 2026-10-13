/**
 * Admin CLI:  pnpm --filter @hajiri/api cli <command> [options]
 *   create-admin --email <email> --password <pw> [--name <name>] [--company DEMO]
 *   seed-defaults [--company DEMO]        leave types, workflows, settings, rule versions, salary components
 *   reset-password --email <email> --password <pw>
 */
import { loadConfig, loadDatabaseUrl } from './config.js';
import { createPool } from './db/pool.js';
import { createUser } from './modules/auth/service.js';
import { hashPassword, passwordPolicyError } from './crypto.js';
import { seedDefaults } from './db/seed-defaults.js';

const args = process.argv.slice(2);
const cmd = args[0];
const opt = (name: string, def?: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] ?? def : def; };

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
      const email = opt('email'); const password = opt('password');
      if (!email || !password) throw new Error('--email and --password are required');
      const err = passwordPolicyError(password); if (err) throw new Error(err);
      const cid = await companyId(opt('company', 'DEMO')!);
      const u = await createUser(db, { companyId: cid, email, name: opt('name', 'Administrator')!, password, roles: ['admin'], pepper: pepper() });
      console.log(`admin user ${u.email} created (${u.id})`);
      break;
    }
    case 'reset-password': {
      const email = opt('email'); const password = opt('password');
      if (!email || !password) throw new Error('--email and --password are required');
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
    default:
      console.log('usage: cli create-admin|reset-password|seed-defaults ...');
      process.exitCode = 1;
  }
} catch (e) {
  console.error((e as Error).message);
  process.exitCode = 1;
} finally {
  await db.end();
}
