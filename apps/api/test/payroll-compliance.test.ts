import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { D } from '@hajiri/payroll-engine';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sitaTok: string; let ramTok: string; let mgr: string;
let bhadraRun: string; let ashwinRun: string;
const P = (path: string) => `/api/v1/payroll${path}`;

async function attendanceFor(employeeId: string, from: string, to: string) {
  for (let t = new Date(`${from}T00:00:00Z`).getTime(); t <= new Date(`${to}T00:00:00Z`).getTime(); t += 86_400_000) {
    const d = new Date(t).toISOString().slice(0, 10);
    const status = new Date(t).getUTCDay() === 6 ? 'WeeklyOff' : 'Present';
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at) VALUES ($1, $2, 1, $3, $4, 'h', now()) ON CONFLICT DO NOTHING`,
      [employeeId, d, status, status === 'Present' ? 480 : 0]);
  }
}

async function runThrough(periodBs: { bsYear: number; bsMonth: number }, beforeLock?: (runId: string) => Promise<void>): Promise<string> {
  const p = await request(app).post(P('/periods')).set(auth(hr)).send(periodBs);
  const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: p.body.period.id });
  expect(run.status).toBe(201);
  const c = await request(app).post(P(`/runs/${run.body.id}/compute`)).set(auth(hr));
  expect(c.status).toBe(200);
  if (beforeLock) await beforeLock(run.body.id);
  expect((await request(app).post(P(`/runs/${run.body.id}/review`)).set(auth(hr)).send({})).status).toBe(200);
  expect((await request(app).post(P(`/runs/${run.body.id}/approve`)).set(auth(hr))).status).toBe(200);
  expect((await request(app).post(P(`/runs/${run.body.id}/lock`)).set(auth(hr))).status).toBe(200);
  return run.body.id as string;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  mgr = (await loginAs(db, app, ['manager'])).token;
  sitaTok = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ramTok = (await loginAs(db, app, ['employee'], DEMO.ram)).token;
  for (const [id, code] of [[DEMO.sita, '01'], [DEMO.ram, '02']] as const) {
    await db.query(`UPDATE employee SET pan_enc = $2, pan_bidx = 'x', bank_account_enc = $3, bank_account_last4 = '0012', bank_code = 'NMB', bank_branch = '7', ssf_id = '1000000000${code}', join_date = '2026-01-01' WHERE id = $1`,
      [id, testCipher.encrypt(`1234567${code}`, `employee:${id}:pan`), testCipher.encrypt(`00123456${code}`, `employee:${id}:bank`)]);
  }
  const s = await request(app).get(P('/structures')).set(auth(hr));
  const version = s.body.structures[0].versions[0].id;
  await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: version, from: '2026-01-01', baseAmounts: { BASIC: '60000.00', ALLOWANCE: '140000.00' } });
  await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.ram, structureVersionId: version, from: '2026-01-01', baseAmounts: { BASIC: '30000.00', DA: '7380.00' } });
  await attendanceFor(DEMO.sita, '2026-08-17', '2026-10-17');
  await attendanceFor(DEMO.ram, '2026-08-17', '2026-10-17');
});
afterAll(async () => { await db.end(); });

describe('opening balances', () => {
  it('HR records prior income for a mid-year joiner; employees and managers cannot', async () => {
    const body = { employeeId: DEMO.ram, fiscalYearStart: 2083, gross: '75000.00', taxable: '75000.00', tdsPaid: '750.00', retirementEmployee: '6600.00', retirementEmployer: '0', months: 1, source: 'previous_employer' };
    expect((await request(app).put(P('/opening-balances')).set(auth(ramTok)).send(body)).status).toBe(403);
    expect((await request(app).put(P('/opening-balances')).set(auth(mgr)).send(body)).status).toBe(403);
    expect((await request(app).put(P('/opening-balances')).set(auth(hr)).send({ ...body, taxable: '999999.00' })).status).toBe(400);
    expect((await request(app).put(P('/opening-balances')).set(auth(hr)).send({ ...body, employeeId: '00000000-0000-7000-8000-0000000000ff' })).status).toBe(404);
    const ok = await request(app).put(P('/opening-balances')).set(auth(hr)).send(body);
    expect(ok.status).toBe(200);
    const list = await request(app).get(P('/opening-balances')).set(auth(hr)).query({ fiscalYearStart: 2083 });
    expect(list.body.balances).toHaveLength(1);
    expect(list.body.balances[0]).toMatchObject({ emp_code: expect.any(String), taxable: '75000.00', tds_paid: '750.00' });
  });

  it('CSV import is all or nothing and reports row errors', async () => {
    const code = (await db.query<{ emp_code: string }>('SELECT emp_code FROM employee WHERE id = $1', [DEMO.sita])).rows[0]!.emp_code;
    const bad = `emp_code,fiscal_year_start,gross,taxable,tds_paid\n${code},2082,10000,10000,100\nNOPE,2082,1,1,0\n`;
    const r1 = await request(app).post(P('/opening-balances/import')).set(auth(hr)).set('Content-Type', 'text/csv').send(bad);
    expect(r1.status).toBe(400);
    expect(r1.body.details.errors[0]).toMatchObject({ row: 3 });
    expect((await db.query('SELECT 1 FROM payroll_opening_balance WHERE fiscal_year_start = 2082')).rows).toHaveLength(0);
    const good = `emp_code,fiscal_year_start,gross,taxable,tds_paid,festival_paid\n${code},2082,"1,20,000",120000,1200,yes\n`;
    const r2 = await request(app).post(P('/opening-balances/import')).set(auth(hr)).set('Content-Type', 'text/csv').send(good);
    expect(r2.status).toBe(200);
    expect(r2.body.imported).toBe(1);
    const row = await db.query('SELECT gross::text, festival_paid FROM payroll_opening_balance WHERE employee_id = $1 AND fiscal_year_start = 2082', [DEMO.sita]);
    expect(row.rows[0]).toMatchObject({ gross: '120000.00', festival_paid: true });
  });
});

describe('held pay and the Bhadra run', () => {
  it('opening balances flow into year-to-date; a hold is listed in pre-flight; release needs a locked run', async () => {
    bhadraRun = await runThrough({ bsYear: 2083, bsMonth: 5 }, async (runId) => {
      const slip = await db.query(`SELECT inputs_snapshot->'input'->'ytd' AS ytd FROM payslip WHERE run_id = $1 AND employee_id = $2`, [runId, DEMO.ram]);
      expect(slip.rows[0].ytd).toMatchObject({ taxable: '75000.00', tdsPaid: '750.00' });
      expect((await request(app).post(P(`/runs/${runId}/holds`)).set(auth(sitaTok)).send({ employeeId: DEMO.sita, reason: 'documents pending' })).status).toBe(403);
      const h = await request(app).post(P(`/runs/${runId}/holds`)).set(auth(hr)).send({ employeeId: DEMO.sita, reason: 'documents pending' });
      expect(h.status).toBe(201);
      expect((await request(app).post(P(`/runs/${runId}/holds`)).set(auth(hr)).send({ employeeId: DEMO.sita, reason: 'again' })).status).toBe(409);
      expect((await request(app).post(P(`/holds/${h.body.id}/release`)).set(auth(hr)).send({ mode: 'payroll' })).status).toBe(409);
      const pf = await request(app).post(P(`/runs/${runId}/preflight`)).set(auth(hr));
      expect(pf.body.warnings.map((w: { code: string }) => w.code)).toContain('pay_held');
      // Recomputing keeps the hold and its amount follows the net.
      expect((await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr))).status).toBe(200);
      const after = await db.query(`SELECT h.amount::text, ps.net::text FROM payroll_hold h JOIN payslip ps ON ps.run_id = h.run_id AND ps.employee_id = h.employee_id WHERE h.id = $1`, [h.body.id]);
      expect(after.rows[0].amount).toBe(after.rows[0].net);
    });
    const holds = await request(app).get(P('/holds')).set(auth(hr));
    expect(holds.body.holds).toHaveLength(1);
    expect(holds.body.holds[0]).toMatchObject({ status: 'held', reason: 'documents pending' });
  });

  it('the employee sees the payslip marked on hold, without HR\'s reason', async () => {
    await request(app).post(P(`/runs/${bhadraRun}/publish`)).set(auth(hr));
    const list = await request(app).get(P('/payslips')).set(auth(sitaTok));
    const id = list.body.payslips[0].id;
    const json = await request(app).get(P(`/payslips/${id}`)).set(auth(sitaTok));
    expect(json.body.payslip.held).toMatchObject({ status: 'held', reason: null });
    const html = await request(app).get(P(`/payslips/${id}/html`)).set(auth(sitaTok));
    expect(html.text).toMatch(/Payment on hold/);
  });

  it('a locked run\'s hold cannot be cancelled; releasing through payroll pays it once in the next run, untaxed', async () => {
    const hold = (await request(app).get(P('/holds')).set(auth(hr))).body.holds[0];
    expect((await request(app).post(P(`/holds/${hold.id}/cancel`)).set(auth(hr))).status).toBe(409);
    expect((await request(app).post(P(`/holds/${hold.id}/release`)).set(auth(hr)).send({ mode: 'payroll', note: 'documents received' })).status).toBe(200);
    expect((await request(app).post(P(`/holds/${hold.id}/release`)).set(auth(hr)).send({ mode: 'payroll' })).status).toBe(409);
    ashwinRun = await runThrough({ bsYear: 2083, bsMonth: 6 }, async (runId) => {
      const pf = await request(app).post(P(`/runs/${runId}/preflight`)).set(auth(hr));
      expect(pf.body.warnings.map((w: { code: string }) => w.code)).toContain('held_pay_release');
      const ps = await db.query(`SELECT ps.id, ps.taxable::text FROM payslip ps WHERE ps.run_id = $1 AND ps.employee_id = $2`, [runId, DEMO.sita]);
      const line = await db.query(`SELECT amount::text FROM payslip_line WHERE payslip_id = $1 AND component_code = 'HELD_RELEASE'`, [ps.rows[0].id]);
      expect(line.rows[0].amount).toBe(hold.amount);
      const earnings = await db.query(`SELECT coalesce(sum(amount), 0)::text AS t FROM payslip_line WHERE payslip_id = $1 AND type = 'earning' AND component_code NOT IN ('HELD_RELEASE', 'ROUNDING')`, [ps.rows[0].id]);
      expect(D(ps.rows[0].taxable).lte(D(earnings.rows[0].t).plus(40000))).toBe(true); // released pay is not taxable again
    });
    const done = await db.query('SELECT status, release_run_id FROM payroll_hold WHERE id = $1', [hold.id]);
    expect(done.rows[0]).toMatchObject({ status: 'released', release_run_id: ashwinRun });
  });

  it('an unlocked run\'s hold can be cancelled; releasing outside payroll needs no run', async () => {
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 7 });
    const sup = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: p.body.period.id, runType: 'off_cycle', employeeIds: [DEMO.ram] });
    await request(app).post(P(`/runs/${sup.body.id}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.ram, componentCode: 'BONUS', amount: '5000.00' }] });
    expect((await request(app).post(P(`/runs/${sup.body.id}/compute`)).set(auth(hr))).status).toBe(200);
    const h = await request(app).post(P(`/runs/${sup.body.id}/holds`)).set(auth(hr)).send({ employeeId: DEMO.ram, reason: 'check bank' });
    expect((await request(app).post(P(`/holds/${h.body.id}/cancel`)).set(auth(hr))).status).toBe(200);
    expect((await db.query('SELECT status FROM payroll_hold WHERE id = $1', [h.body.id])).rows[0].status).toBe('cancelled');
  });
});

describe('optimum CIT', () => {
  it('suggests the CIT that lowers the employee\'s tax most; employees see only their own', async () => {
    const me = await request(app).get(P('/optimum-cit/me')).set(auth(sitaTok));
    expect(me.status).toBe(200);
    expect(me.body.available).toBe(true);
    expect(D(me.body.optimumMonthly).gt(0)).toBe(true);
    expect(D(me.body.annualSaving).gt(0)).toBe(true);
    expect((await request(app).get(P('/optimum-cit')).set(auth(sitaTok))).status).toBe(403);
    const all = await request(app).get(P('/optimum-cit')).set(auth(hr));
    expect(all.body.employees.map((e: { employeeId: string }) => e.employeeId)).toEqual(expect.arrayContaining([DEMO.sita, DEMO.ram]));
  });

  it('HR applies it from this month; the employee is told and the estimate changes', async () => {
    const me = await request(app).get(P('/optimum-cit/me')).set(auth(sitaTok));
    expect((await request(app).post(P('/optimum-cit/apply')).set(auth(sitaTok)).send({ employeeId: DEMO.sita, monthlyCit: me.body.optimumMonthly })).status).toBe(403);
    const ap = await request(app).post(P('/optimum-cit/apply')).set(auth(hr)).send({ employeeId: DEMO.sita, monthlyCit: me.body.optimumMonthly });
    expect(ap.status).toBe(200);
    const d = await db.query(`SELECT cit_monthly::text, status FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = 2083`, [DEMO.sita]);
    expect(d.rows[0]).toMatchObject({ cit_monthly: D(me.body.optimumMonthly).toFixed(2), status: 'verified' });
    const again = await request(app).get(P('/optimum-cit/me')).set(auth(sitaTok));
    expect(again.body.reason).toBe('already_optimal');
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE template = 'payroll.cit_applied' AND employee_id = $1`, [DEMO.sita]);
    expect(n.rows.length).toBeGreaterThan(0);
  });
});

describe('donations in tax savings', () => {
  it('an employee declares a donation; it counts only after HR approves', async () => {
    const sub = await request(app).post(P('/tax-savings')).set(auth(ramTok)).send({ fiscalYearStart: 2083, donation: '10000.00' });
    expect(sub.status).toBe(201);
    expect(sub.body.request.donation).toBe('10000.00');
    let decl = await db.query(`SELECT donation::text FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = 2083`, [DEMO.ram]);
    expect(decl.rows[0]?.donation ?? '0.00').toBe('0.00');
    expect((await request(app).post(P(`/declarations/${DEMO.ram}/verify`)).set(auth(hr)).send({ fiscalYearStart: 2083, decision: 'verified' })).status).toBe(200);
    decl = await db.query(`SELECT donation::text FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = 2083`, [DEMO.ram]);
    expect(decl.rows[0].donation).toBe('10000.00');
    const est = await request(app).post(P('/tax-savings/estimate')).set(auth(ramTok)).send({});
    expect(est.body.current.declaration.donation).toBe('10000.00');
    expect(est.body.current).toHaveProperty('donationDeduction');
  });
});

describe('certificates and reports', () => {
  it('an employee opens their own tax and salary certificates, not a colleague\'s', async () => {
    const tax = await request(app).get(P('/certificates/me')).set(auth(sitaTok)).query({ kind: 'tax', fiscalYearStart: 2083 });
    expect(tax.status).toBe(200);
    expect(tax.text).toMatch(/Tax deduction certificate/);
    expect(tax.text).toMatch(/123456701/);
    expect(tax.text).toMatch(/Bhadra 2083/);
    expect(tax.text).not.toMatch(/<script/i);
    const sal = await request(app).get(P('/certificates/me')).set(auth(sitaTok)).query({ kind: 'salary', purpose: 'Bank loan' });
    expect(sal.status).toBe(200);
    expect(sal.text).toMatch(/Salary certificate/);
    expect(sal.text).toMatch(/Bank loan/);
    expect((await request(app).get(P(`/certificates/${DEMO.sita}`)).set(auth(ramTok))).status).toBe(404);
    expect((await request(app).get(P(`/certificates/${DEMO.sita}`)).set(auth(mgr))).status).toBe(404);
    const byHr = await request(app).get(P(`/certificates/${DEMO.sita}`)).set(auth(hr)).query({ fiscalYearStart: 2083 });
    expect(byHr.status).toBe(200);
    const a = await db.query(`SELECT 1 FROM audit_log WHERE action = 'certificate' AND row_id = $1`, [DEMO.sita]);
    expect(a.rows.length).toBeGreaterThan(0);
  });

  it('annual tax summary includes opening balances and masks PAN on screen', async () => {
    const r = await request(app).get(P('/reports/annual-tax')).set(auth(hr)).query({ fiscalYearStart: 2083 });
    expect(r.status).toBe(200);
    const ram = r.body.rows.find((x: { employeeId: string }) => x.employeeId === DEMO.ram);
    expect(ram.opening).toBe('yes');
    expect(ram.pan).toMatch(/^\*+/);
    const csv = await request(app).get(P('/reports/annual-tax')).set(auth(hr)).query({ fiscalYearStart: 2083, format: 'csv' });
    expect(csv.text.charCodeAt(0)).toBe(0xfeff);
    expect(csv.text).toMatch(/123456702/);
    expect((await request(app).get(P('/reports/annual-tax')).set(auth(sitaTok))).status).toBe(403);
  });

  it('tax and retirement deposit reports for a month', async () => {
    const tx = await request(app).get(P('/reports/tax-deposit')).set(auth(hr)).query({ bsYear: 2083, bsMonth: 5 });
    expect(tx.status).toBe(200);
    expect(tx.body.rows).toHaveLength(2);
    for (const row of tx.body.rows) expect(D(row.sst).plus(row.incomeTax).toFixed(2)).toBe(row.tds);
    const rf = await request(app).get(P('/reports/retirement-deposit')).set(auth(hr)).query({ bsYear: 2083, bsMonth: 5, format: 'csv' });
    expect(rf.status).toBe(200);
    expect(rf.text).toMatch(/SSF employee/);
    expect((await request(app).get(P('/reports/tax-deposit')).set(auth(mgr)).query({ bsYear: 2083, bsMonth: 5 })).status).toBe(403);
  });

  it('pay variance explains the change against the previous month', async () => {
    const v = await request(app).get(P('/reports/pay-variance')).set(auth(hr)).query({ runId: ashwinRun });
    expect(v.status).toBe(200);
    expect(v.body.previous).toMatchObject({ bsMonth: 5 });
    const sita = v.body.rows.find((x: { name: string }) => x.name);
    expect(sita).toHaveProperty('reasons');
    expect(v.body.rows.some((x: { reasons: string }) => /One-time|Tax changed/.test(x.reasons))).toBe(true);
  });

  it('leave liability values encashable balances at basic ÷ divisor', async () => {
    const home = await db.query<{ id: string }>(`SELECT id FROM leave_type WHERE company_id = $1 AND code = 'HOME'`, [DEMO.companyId]);
    await db.query(`INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES ($1, $2, '2026-09-01', 10, 'allocation')`, [DEMO.ram, home.rows[0]!.id]);
    const r = await request(app).get(P('/reports/leave-liability')).set(auth(hr)).query({ asOf: '2026-10-01' });
    expect(r.status).toBe(200);
    const row = r.body.rows.find((x: { leaveType: string; balance: string }) => x.leaveType === 'Home leave' && x.balance === '10.00');
    expect(row).toBeTruthy();
    expect(row.amount).toBe(D(30000).div(r.body.divisor).times(10).toDecimalPlaces(2).toFixed(2));
    expect(D(r.body.total).gt(0)).toBe(true);
  });
});

describe('publishing on lock', () => {
  it('with the setting on, locking a run publishes its payslips', async () => {
    await db.query(`UPDATE company_setting SET value = value || '{"publishOnLock": true}' WHERE company_id = $1 AND key = 'payroll.policy'`, [DEMO.companyId]);
    const run = await runThrough({ bsYear: 2083, bsMonth: 7 });
    const ps = await db.query('SELECT count(*)::int AS n, count(published_at)::int AS p FROM payslip WHERE run_id = $1', [run]);
    expect(ps.rows[0].n).toBeGreaterThan(0);
    expect(ps.rows[0].p).toBe(ps.rows[0].n);
  });
});
