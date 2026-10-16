import { randomBytes } from 'node:crypto';
import { nptToday } from '../../modules/workflow/service.js';
import { createUser } from '../../modules/auth/service.js';
import type { Db } from '../pool.js';
import type { Actor, Kit } from './kit.js';

export interface DemoEmployee {
  id: string; code: string; name: string; first: string; last: string; gender: string | null; designation: string | null; joinDate: string | null;
  orgUnitId: string | null; orgCode: string | null; managerId: string | null; branchId: string | null; userId: string | null;
}

/** Everything a section needs: the company, today, the employees in code order, and API sessions for HR and any employee. */
export interface DemoContext {
  db: Db; kit: Kit; companyId: string; companyName: string; today: string; domain: string;
  hr: Actor;
  employees: DemoEmployee[];
  byCode: (code: string) => DemoEmployee;
  as: (e: DemoEmployee) => Promise<Actor>;
  /** The manager's session for an employee (falls back to HR when there is no line manager with an account). */
  managerOf: (e: DemoEmployee) => Promise<Actor>;
  log: (msg: string) => void;
}

/**
 * True when a section's data is already there (its last record exists), so the section is skipped. When only its first
 * records exist, an earlier run stopped half-way: refuse rather than add duplicates.
 */
export async function alreadySeeded(ctx: DemoContext, section: string, doneSql: string, startedSql: string): Promise<boolean> {
  if ((await ctx.db.query(doneSql, [ctx.companyId])).rows.length) return true;
  if ((await ctx.db.query(startedSql, [ctx.companyId])).rows.length) {
    throw new Error(`the ${section} data is only partly there from an earlier run; restore the demo database (or remove that data) and run again`);
  }
  return false;
}

export async function buildContext(db: Db, kit: Kit, companyCode: string, log: (m: string) => void): Promise<DemoContext> {
  const c = await db.query<{ id: string; name_en: string }>('SELECT id, name_en FROM company WHERE code = $1', [companyCode]);
  if (!c.rows[0]) throw new Error(`company ${companyCode} not found`);
  const companyId = c.rows[0].id;
  const today = nptToday();
  const hrRow = await db.query<{ id: string; email: string }>(
    `SELECT id, email FROM app_user WHERE company_id = $1 AND status = 'active' AND (roles && ARRAY['hr', 'admin']::text[]) AND NOT must_change_password
      ORDER BY 'hr' = ANY(roles) DESC, created_at LIMIT 1`, [companyId]);
  if (!hrRow.rows[0]) throw new Error('the company needs an active HR or admin user (who has set their password) to run the demo suite');
  const domain = hrRow.rows[0].email.split('@')[1] ?? 'example.com';

  const load = async () => (await db.query<{
    id: string; emp_code: string; name_en: string; gender: string | null; designation: string | null; join_date: string | null;
    org_unit_id: string | null; org_code: string | null; manager_id: string | null; branch_id: string | null; user_id: string | null;
  }>(
    `SELECT e.id, e.emp_code, e.name_en, e.gender, e.designation, e.join_date, a.org_unit_id, o.code AS org_code, a.manager_id, a.branch_id,
            (SELECT u.id FROM app_user u WHERE u.employee_id = e.id AND u.status = 'active' ORDER BY u.created_at LIMIT 1) AS user_id
       FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date LEFT JOIN org_unit o ON o.id = a.org_unit_id
      WHERE e.company_id = $1 AND e.status = 'active' ORDER BY e.emp_code`, [companyId, today])).rows;

  let rows = await load();
  if (rows.length < 8) throw new Error(`the demo suite needs at least 8 active employees (found ${rows.length})`);

  // Every employee gets an account so they can raise requests; the password is random (HR can reset it to hand it over).
  const managers = new Set(rows.map((r) => r.manager_id).filter(Boolean));
  let created = 0;
  for (const r of rows) {
    const wantRoles = managers.has(r.id) ? ['employee', 'manager'] : ['employee'];
    if (r.user_id) {
      if (managers.has(r.id)) await db.query(`UPDATE app_user SET roles = array(SELECT DISTINCT unnest(roles || ARRAY['manager']::text[])) WHERE id = $1 AND NOT ('manager' = ANY(roles))`, [r.user_id]);
      continue;
    }
    const first = r.name_en.split(/\s+/)[0]!.toLowerCase().replace(/[^a-z]/g, '') || 'user';
    let email = `${first}@${domain}`;
    if ((await db.query('SELECT 1 FROM app_user WHERE email = $1', [email])).rows.length) email = `${first}.${r.emp_code.toLowerCase()}@${domain}`;
    await createUser(db, { companyId, employeeId: r.id, email, name: r.name_en, password: randomBytes(18).toString('base64url'), roles: wantRoles as never, mustChangePassword: false, pepper: kit.pepper });
    created++;
  }
  if (created) { log(`created ${created} employee accounts (random passwords; reset one with the CLI to sign in as that person)`); rows = await load(); }

  const employees: DemoEmployee[] = rows.map((r) => {
    const parts = r.name_en.split(/\s+/);
    return {
      id: r.id, code: r.emp_code, name: r.name_en, first: parts[0]!, last: parts.slice(1).join(' ') || parts[0]!, gender: r.gender, designation: r.designation,
      joinDate: r.join_date, orgUnitId: r.org_unit_id, orgCode: r.org_code, managerId: r.manager_id, branchId: r.branch_id, userId: r.user_id,
    };
  });
  const sessions = new Map<string, Actor>();
  const hr = await kit.actor(hrRow.rows[0].id);
  const as = async (e: DemoEmployee) => {
    if (!e.userId) throw new Error(`${e.code} has no account`);
    const hit = sessions.get(e.userId);
    if (hit) return hit;
    const a = await kit.actor(e.userId);
    sessions.set(e.userId, a);
    return a;
  };
  const byId = new Map(employees.map((e) => [e.id, e]));
  return {
    db, kit, companyId, companyName: c.rows[0].name_en, today, domain, hr, employees,
    byCode: (code) => { const e = employees.find((x) => x.code === code); if (!e) throw new Error(`no employee ${code}`); return e; },
    as,
    managerOf: async (e) => { const m = e.managerId ? byId.get(e.managerId) : undefined; return m?.userId ? as(m) : hr; },
    log,
  };
}
