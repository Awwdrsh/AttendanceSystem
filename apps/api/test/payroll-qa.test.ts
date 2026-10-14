import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { D } from '@hajiri/payroll-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hrB: string; let hrBId: string; let admin: string;
let otherHr: string; let otherCompanyId: string;
let stdVersionId: string; let periodId: string; let runId: string;
const P = (path: string) => `/api/v1/payroll${path}`;
const PASS = 'correct-horse-battery-staple';

async function attendanceFor(employeeId: string, from: string, to: string, locked = true) {
  for (let t = new Date(`${from}T00:00:00Z`).getTime(); t <= new Date(`${to}T00:00:00Z`).getTime(); t += 86_400_000) {
    const d = new Date(t).toISOString().slice(0, 10);
    const status = new Date(t).getUTCDay() === 6 ? 'WeeklyOff' : 'Present';
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at) VALUES ($1, $2, 1, $3, $4, 'h', $5) ON CONFLICT DO NOTHING`,
      [employeeId, d, status, status === 'Present' ? 480 : 0, locked ? new Date() : null]);
  }
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  const b = await loginAs(db, app, ['hr']); hrB = b.token; hrBId = b.userId;
  admin = (await loginAs(db, app, ['admin'])).token;
  for (const [id, code] of [[DEMO.sita, 'E001'], [DEMO.ram, 'E002']] as const) {
    await db.query(`UPDATE employee SET pan_enc = $2, pan_bidx = 'x', bank_account_enc = $3, bank_account_last4 = '0012', bank_code = 'NMB', bank_branch = '7', ssf_id = '10000000001', join_date = '2026-01-01' WHERE id = $1`,
      [id, testCipher.encrypt('123456789', `employee:${id}:pan`), testCipher.encrypt(`0012345600${code.slice(-2)}12`, `employee:${id}:bank`)]);
  }
  // A second company with its own HR user, for cross-tenant checks.
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`);
  otherCompanyId = c.rows[0]!.id;
  await createUser(db, { companyId: otherCompanyId, email: 'hr@other.local', name: 'Other HR', password: PASS, roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.local', password: PASS })).body.token;
  const s = await request(app).get(P('/structures')).set(auth(hr));
  stdVersionId = s.body.structures[0].versions[0].id;
  for (const [emp, base] of [[DEMO.sita, { BASIC: '40000.00', ALLOWANCE: '20000.00' }], [DEMO.ram, { BASIC: '30000.00', DA: '7380.00' }]] as const) {
    await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: emp, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: base });
  }
});
afterAll(async () => { await db.end(); });

describe('tenant isolation and authorisation', () => {
  it('HR of another company cannot read, verify or write declarations, assignments or payslips of this company', async () => {
    await request(app).put(P('/declarations')).set(auth(hr)).send({ employeeId: DEMO.sita, fiscalYearStart: 2083, lifePremium: '30000.00' });
    expect((await request(app).get(P('/declarations')).set(auth(otherHr)).query({ employeeId: DEMO.sita, fiscalYearStart: 2083 })).status).toBe(404);
    expect((await request(app).post(P(`/declarations/${DEMO.sita}/verify`)).set(auth(otherHr)).send({ fiscalYearStart: 2083, decision: 'verified' })).status).toBe(404);
    expect((await request(app).put(P('/declarations')).set(auth(otherHr)).send({ employeeId: DEMO.sita, fiscalYearStart: 2083 })).status).toBe(404);
    expect((await request(app).post(P('/assignments')).set(auth(otherHr)).send({ employeeId: DEMO.sita, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: { BASIC: '1.00' } })).status).toBe(404);
    // Our HR cannot assign a structure that belongs to another company.
    const foreign = await db.query<{ id: string }>(`INSERT INTO salary_structure (company_id, code, name_en) VALUES ($1, 'X', 'X') RETURNING id`, [otherCompanyId]);
    const fv = await db.query<{ id: string }>(`INSERT INTO salary_structure_version (structure_id, version, components) VALUES ($1, 1, '[{"code":"BASIC"}]') RETURNING id`, [foreign.rows[0]!.id]);
    expect((await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: fv.rows[0]!.id, from: '2026-02-01', baseAmounts: { BASIC: '1.00' } })).status).toBe(404);
  });
  it('rule versions are jurisdiction-wide: HR cannot add them, admin can; HR signs them off', async () => {
    const body = { ruleKey: 'test.rule', validFromAd: '2026-01-01', validFromBs: '2082-09-17', value: { x: 1 } };
    expect((await request(app).post(P('/rules')).set(auth(hr)).send(body)).status).toBe(403);
    const created = await request(app).post(P('/rules')).set(auth(admin)).send(body);
    expect(created.status).toBe(201);
    const emp = (await loginAs(db, app, ['employee'])).token;
    expect((await request(app).post(P(`/rules/${created.body.id}/approve`)).set(auth(emp))).status).toBe(403);
    expect((await request(app).post(P(`/rules/${created.body.id}/approve`)).set(auth(hr))).status).toBe(200);
    const row = await db.query('SELECT approved_by, approved_at FROM rule_version WHERE id = $1', [created.body.id]);
    expect(row.rows[0].approved_at).not.toBeNull();
    expect((await request(app).post(P('/rules/00000000-0000-7000-8000-00000000ffff/approve')).set(auth(hr))).status).toBe(404);
  });
  it('period creation outside the calendar table is a 400, not a crash', async () => {
    const r = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2099, bsMonth: 1 });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatch(/calendar/);
  });
});

describe('run lifecycle guards', () => {
  it('only one regular run per period; a second one is refused while the first is still open', async () => {
    periodId = (await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 5 })).body.period.id;
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId });
    expect(run.status).toBe(201);
    runId = run.body.id;
    const again = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId });
    expect(again.status).toBe(409);
    expect(again.body.error).toMatch(/continue that run/);
  });
  it('final settlement needs an exit date inside the period; active employees cannot be settled', async () => {
    const r = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId, runType: 'final_settlement', employeeIds: [DEMO.ram] });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatch(/exit date/);
  });
  it('inputs reject unknown component codes and non-positive amounts', async () => {
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'NOPE', amount: '10.00' }] })).status).toBe(400);
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'BONUS', amount: '0.00' }] })).status).toBe(400);
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'GROSS', amount: '10.00' }] })).status).toBe(400);
  });
  it('HR can override pre-flight blockers with a reason (audited); compute without a reason is refused', async () => {
    await attendanceFor(DEMO.sita, '2026-08-17', '2026-09-16', false);
    await attendanceFor(DEMO.ram, '2026-08-17', '2026-09-16', false);
    expect((await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr))).status).toBe(409);
    const ok = await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr)).send({ overrideReason: 'month-end rush, lock follows' });
    expect(ok.status).toBe(200);
    expect(ok.body.payslips).toBe(2);
    const a = await db.query(`SELECT 1 FROM audit_log WHERE action = 'preflight_override' AND row_id = $1`, [runId]);
    expect(a.rows).toHaveLength(1);
  });
  it('adding inputs after review sends the run back to computed; inputs after approval are refused', async () => {
    expect((await request(app).post(P(`/runs/${runId}/review`)).set(auth(hr))).status).toBe(200);
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'BONUS', amount: '1000.00' }] })).status).toBe(201);
    expect((await db.query('SELECT status FROM payroll_run WHERE id = $1', [runId])).rows[0].status).toBe('computed');
    expect((await request(app).post(P(`/runs/${runId}/approve`)).set(auth(hr))).status).toBe(409);
    await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr)).send({ overrideReason: 'month-end rush, lock follows' });
    await request(app).post(P(`/runs/${runId}/review`)).set(auth(hr));
  });
  it('separate-approver policy: when on, the HR user who prepared the run cannot approve it; another HR user can', async () => {
    await db.query(`UPDATE company_setting SET value = value || '{"separateApprover": true}' WHERE company_id = $1 AND key = 'payroll.policy'`, [DEMO.companyId]);
    const own = await request(app).post(P(`/runs/${runId}/approve`)).set(auth(hr));
    expect(own.status).toBe(403);
    expect(own.body.error).toMatch(/different HR user/);
    expect((await db.query('SELECT status FROM payroll_run WHERE id = $1', [runId])).rows[0].status).toBe('reviewed');
    const other = await request(app).post(P(`/runs/${runId}/approve`)).set(auth(hrB));
    expect(other.status).toBe(200);
    expect(other.body).toMatchObject({ approved: true });
    const run = await db.query('SELECT status, checker_id FROM payroll_run WHERE id = $1', [runId]);
    expect(run.rows[0]).toMatchObject({ status: 'approved', checker_id: hrBId });
    await db.query(`UPDATE company_setting SET value = value || '{"separateApprover": false}' WHERE company_id = $1 AND key = 'payroll.policy'`, [DEMO.companyId]);
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'BONUS', amount: '1.00' }] })).status).toBe(409);
  });
  it('locking a regular run locks the period; cross-company run access is a 404', async () => {
    expect((await request(app).post(P(`/runs/${runId}/lock`)).set(auth(hr))).status).toBe(200);
    expect((await request(app).get(P(`/runs/${runId}`)).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get(P(`/runs/${runId}/salary-sheet`)).set(auth(otherHr))).status).toBe(404);
    const ps = await db.query<{ id: string }>('SELECT id FROM payslip WHERE run_id = $1 LIMIT 1', [runId]);
    expect((await request(app).get(P(`/payslips/${ps.rows[0]!.id}`)).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get(P(`/payslips/${ps.rows[0]!.id}/html`)).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get(P(`/runs/${runId}/salary-sheet`)).query({ format: 'csv' }).set(auth(otherHr))).status).toBe(404);
  });
});

describe('payslip visibility', () => {
  it('an employee of another company gets 404; a manager cannot read a direct report\'s payslips; self works', async () => {
    const foreignEmp = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'X1', 'Foreign') RETURNING id`, [otherCompanyId]);
    await createUser(db, { companyId: otherCompanyId, employeeId: foreignEmp.rows[0]!.id, email: 'emp@other.local', name: 'Foreign', password: PASS, roles: ['employee'], pepper: TEST_PEPPER });
    const foreign = (await request(app).post('/api/v1/auth/login').send({ email: 'emp@other.local', password: PASS })).body.token;
    expect((await request(app).get(P('/payslips')).set(auth(foreign)).query({ employeeId: DEMO.sita })).status).toBe(404);
    expect((await request(app).get(P('/declarations')).set(auth(foreign)).query({ employeeId: DEMO.sita, fiscalYearStart: 2083 })).status).toBe(404);
    // Ram manages Sita: he may see her attendance, but pay is HR/self only (plan §13.2 cost figures are role-gated).
    await db.query(`UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1 AND upper_inf(valid)`, [DEMO.sita, DEMO.ram]);
    const manager = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
    expect((await request(app).get(P('/payslips')).set(auth(manager)).query({ employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get(P('/payslips')).set(auth(manager)).query({ employeeId: DEMO.ram })).status).toBe(200);
    const unrelated = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
    expect((await request(app).get(P('/payslips')).set(auth(unrelated)).query({ employeeId: DEMO.ram })).status).toBe(403);
  });
});

describe('loss of pay from attendance flags', () => {
  it('counts half-day leave without punches as 0.5 (1.0 if unpaid) using jsonb flag containment', async () => {
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 7 });
    const rows: Array<[string, string, string]> = [
      ['2026-10-18', 'Absent', '[{"kind":"HalfDayLeave","half":"first_half"}]'],
      ['2026-10-19', 'Absent', '[{"kind":"HalfDayLeave","half":"second_half"},{"kind":"UnpaidLeave"}]'],
      ['2026-10-20', 'OnLeave', '[{"kind":"UnpaidLeave"}]'],
      ['2026-10-21', 'HalfDayLeave', '[{"kind":"UnpaidLeave"}]'],
      ['2026-10-22', 'OnLeave', '[]'],
      ['2026-10-25', 'Present', '[{"kind":"AutoClockOut"},{"kind":"Late","minutes":5}]'],
      ['2026-10-26', 'Incomplete', '[{"kind":"MissingOut"}]'],
    ];
    for (const [d, status, flags] of rows) {
      await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, flags, locked_at) VALUES ($1, $2, 1, $3, 'h', $4::jsonb, now())`, [DEMO.sita, d, status, flags]);
    }
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: p.body.period.id, employeeIds: [DEMO.sita] });
    expect(run.status).toBe(201);
    const c = await request(app).post(P(`/runs/${run.body.id}/compute`)).set(auth(hr)).send({ overrideReason: 'attendance partially entered for the LOP test' });
    expect(c.status).toBe(200);
    const r = await request(app).get(P(`/runs/${run.body.id}`)).set(auth(hr));
    // 0.5 + 1 + 1 + 0.5 + 0 + 0 + 0 = 3 days of LOP: the Incomplete day (missing punch-out) was worked, so it is paid
    expect(r.body.payslips[0].lop_days).toBe('3.00');
    expect(r.body.payslips[0].paid_days).toBe('27.00');
    const pf = (await db.query('SELECT preflight FROM payroll_run WHERE id = $1', [run.body.id])).rows[0].preflight;
    expect(pf.warnings.find((w: { code: string }) => w.code === 'incomplete_days')).toMatchObject({ count: 1, employees: ['E001: 1 day'] });
  });
});

describe('outputs', () => {
  it('the SSF sheet allocation sums to 31% of basic', async () => {
    const ssf = await request(app).get(P(`/runs/${runId}/ssf-sheet.csv`)).set(auth(hr));
    const row = ssf.text.split('\r\n')[1]!.split(',');
    const basic = D(row[3]!);
    const alloc = D(row[6]!).plus(row[7]!).plus(row[8]!).plus(row[9]!);
    expect(alloc.minus(basic.times('0.31')).abs().lte('0.05')).toBe(true);
  });
  it('payslip and salary sheet HTML escape user-controlled strings; CSV quotes commas', async () => {
    await db.query(`UPDATE employee SET name_en = $2, designation = $3 WHERE id = $1`, [DEMO.sita, 'Sita <script>alert(1)</script>', '"><img src=x onerror=alert(1)>']);
    const ps = await db.query<{ id: string }>('SELECT id FROM payslip WHERE run_id = $1 AND employee_id = $2', [runId, DEMO.sita]);
    const html = await request(app).get(P(`/payslips/${ps.rows[0]!.id}/html`)).set(auth(hr));
    expect(html.status).toBe(200);
    expect(html.text).not.toContain('<script>alert(1)</script>');
    expect(html.text).toContain('&lt;script&gt;');
    expect(html.text).not.toContain('<img src=x');
    const sheet = await request(app).get(P(`/runs/${runId}/salary-sheet`)).query({ format: 'html' }).set(auth(hr));
    expect(sheet.status).toBe(200);
    expect(sheet.text).not.toContain('<script>alert(1)</script>');
    expect(sheet.text).toContain('&lt;script&gt;');
    expect(sheet.text).not.toContain('<img src=x');
    await db.query(`UPDATE employee SET name_en = 'Sharma, Sita' WHERE id = $1`, [DEMO.sita]);
    const csv = await request(app).get(P(`/runs/${runId}/salary-sheet`)).query({ format: 'csv' }).set(auth(hr));
    expect(csv.text).toContain(',"Sharma, Sita",');
    await db.query(`UPDATE employee SET name_en = 'Sita Sharma', designation = NULL WHERE id = $1`, [DEMO.sita]);
  });
});

describe('final settlement scoping', () => {
  it('locks only the settled employee\'s attendance and pro-rates the festival allowance to months served', async () => {
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 6 });
    await db.query(`UPDATE employee SET exit_date = '2026-10-10' WHERE id = $1`, [DEMO.ram]);
    await attendanceFor(DEMO.ram, '2026-09-17', '2026-10-10', false);
    await attendanceFor(DEMO.sita, '2026-09-17', '2026-10-16', false);
    const fs = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: p.body.period.id, runType: 'final_settlement', employeeIds: [DEMO.ram] });
    expect(fs.status).toBe(201);
    expect((await request(app).post(P(`/runs/${fs.body.id}/compute`)).set(auth(hr))).status).toBe(200);
    const run = await request(app).get(P(`/runs/${fs.body.id}`)).set(auth(hr));
    const d = await request(app).get(P(`/payslips/${run.body.payslips[0].id}`)).set(auth(hr));
    // Joined before the FY (17 Jul 2026) and left 10 Oct 2026: about 3 months of service → 30,000 × 3/12 = 7,500, not a full month's basic.
    const festival = d.body.lines.find((l: { code: string }) => l.code === 'FESTIVAL');
    expect(festival.amount).toBe('7500.00');
    await request(app).post(P(`/runs/${fs.body.id}/review`)).set(auth(hr));
    expect((await request(app).post(P(`/runs/${fs.body.id}/approve`)).set(auth(hr))).status).toBe(200);
    expect((await request(app).post(P(`/runs/${fs.body.id}/lock`)).set(auth(hr))).status).toBe(200);
    const sitaLocked = await db.query(`SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = $1 AND local_date BETWEEN '2026-09-17' AND '2026-10-16' AND locked_at IS NOT NULL`, [DEMO.sita]);
    expect(sitaLocked.rows[0].n).toBe(0);
    const ramLocked = await db.query(`SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = $1 AND local_date BETWEEN '2026-09-17' AND '2026-10-10' AND locked_at IS NULL`, [DEMO.ram]);
    expect(ramLocked.rows[0].n).toBe(0);
    const period = await db.query('SELECT attendance_locked_at FROM payroll_period WHERE id = $1', [p.body.period.id]);
    expect(period.rows[0].attendance_locked_at).toBeNull();
  });
});
