import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db/pool.js';
import { runHireHooks } from '../src/modules/hiring/service.js';
import { wireModules } from '../src/modules/integrations.js';
import { applyConfirmation, completeExit, ensureLifecycleDefaults, recordExit } from '../src/modules/lifecycle/service.js';
import { DEMO, freshDb } from './setup-db.js';

describe('links between HR modules', () => {
  let db: Db;
  beforeAll(async () => { db = await freshDb(); wireModules(); await ensureLifecycleDefaults(db, DEMO.companyId); });
  afterAll(async () => { await db.end(); });

  async function newEmployee(code: string, joinDate: string): Promise<string> {
    const r = await db.query<{ id: string }>(
      `INSERT INTO employee (company_id, emp_code, name_en, join_date, designation, weekly_off_days, regime) VALUES ($1, $2, $3, $4, 'Engineer', '{6}', 'ssf') RETURNING id`,
      [DEMO.companyId, code, `Person ${code}`, joinDate]);
    await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange($2, NULL), $3, 'regular')`, [r.rows[0]!.id, joinDate, DEMO.branchId]);
    return r.rows[0]!.id;
  }
  const kinds = async (id: string) => (await db.query<{ kind: string }>('SELECT kind FROM career_event WHERE employee_id = $1 ORDER BY created_at', [id])).rows.map((r) => r.kind);

  it('a hired candidate gets a Joined career event and an onboarding checklist', async () => {
    const id = await newEmployee('INT1', '2026-11-01');
    const admin = (await db.query<{ id: string }>(`SELECT id FROM app_user LIMIT 1`)).rows[0]?.id ?? null;
    const c = await db.connect();
    try {
      await c.query('BEGIN');
      await runHireHooks(c, { companyId: DEMO.companyId, employeeId: id, applicationId: id, jobId: id, joinDate: '2026-11-01', actorId: admin as string });
      await c.query('COMMIT');
    } finally { c.release(); }
    expect(await kinds(id)).toEqual(['hire']);
    const cases = await db.query(`SELECT 1 FROM lifecycle_case WHERE employee_id = $1 AND kind = 'onboarding'`, [id]);
    expect(cases.rows.length).toBe(1);
  });

  it('a probation confirmation and a completed exit appear on the career timeline once', async () => {
    const id = await newEmployee('INT2', '2025-01-01');
    await applyConfirmation(db, { companyId: DEMO.companyId, employeeId: id, confirmedOn: '2025-07-01', reviewId: null, actorId: null });
    const exitId = await recordExit(db, { companyId: DEMO.companyId, employeeId: id, kind: 'resignation', voluntary: true, reason: 'Moving abroad', lastWorkingDay: '2026-01-15', actorId: null });
    await completeExit(db, exitId, null, { skipOpenTasks: true });
    expect(await kinds(id)).toEqual(['confirmation', 'separation']);
    const sep = await db.query(`SELECT status, effective_date::text AS d, details FROM career_event WHERE employee_id = $1 AND kind = 'separation'`, [id]);
    expect(sep.rows[0]).toMatchObject({ status: 'applied', d: '2026-01-15', details: { separationType: 'resignation' } });
    const emp = await db.query(`SELECT status FROM employee WHERE id = $1`, [id]);
    expect(emp.rows[0].status).toBe('exited');
  });
});
