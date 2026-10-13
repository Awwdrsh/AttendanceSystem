import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { D, Decimal } from '@hajiri/payroll-engine';
import type { Db } from '../src/db/pool.js';
import { testCipher } from './setup-db.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let finance: string; let financeUserId: string; let sitaTok: string; let ramTok: string;
let stdVersionId: string; let periodId: string; let runId: string;
const P = (path: string) => `/api/v1/payroll${path}`;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  const f = await loginAs(db, app, ['finance']); finance = f.token; financeUserId = f.userId;
  sitaTok = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ramTok = (await loginAs(db, app, ['employee'], DEMO.ram)).token;
  // Master data needed by pre-flight: bank, PAN, SSF ids.
  for (const [id, code] of [[DEMO.sita, 'E001'], [DEMO.ram, 'E002']] as const) {
    await db.query(`UPDATE employee SET pan_enc = $2, pan_bidx = 'x', bank_account_enc = $3, bank_account_last4 = '0012', bank_code = 'NMB', bank_branch = '7', ssf_id = '10000000001', join_date = '2026-01-01' WHERE id = $1`,
      [id, testCipher.encrypt('123456789', `employee:${id}:pan`), testCipher.encrypt(`0012345600${code.slice(-2)}12`, `employee:${id}:bank`)]);
  }
  await db.query(`UPDATE employee SET regime = 'pf', ssf_id = NULL, pf_id = 'PF-22' WHERE id = $1`, [DEMO.ram]);
});
afterAll(async () => { await db.end(); });

async function attendanceFor(employeeId: string, from: string, to: string, absentDates: string[] = [], locked = true) {
  for (let t = new Date(`${from}T00:00:00Z`).getTime(); t <= new Date(`${to}T00:00:00Z`).getTime(); t += 86_400_000) {
    const d = new Date(t).toISOString().slice(0, 10);
    const dow = new Date(t).getUTCDay();
    const status = absentDates.includes(d) ? 'Absent' : dow === 6 ? 'WeeklyOff' : 'Present';
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at) VALUES ($1, $2, 1, $3, $4, 'h', $5) ON CONFLICT DO NOTHING`,
      [employeeId, d, status, status === 'Present' ? 480 : 0, locked ? new Date() : null]);
  }
}

describe('payroll setup', () => {
  it('seeded rule versions resolve by date and statutory keys are locked', async () => {
    const r = await request(app).get(P('/rules')).set(auth(hr)).query({ key: 'tax.schedule', asOf: '2026-08-01' });
    expect(r.status).toBe(200);
    expect(r.body.rules).toHaveLength(1);
    expect(r.body.rules[0]).toMatchObject({ locked: true, valid_from_ad: '2026-07-17' });
    expect(r.body.rules[0].value.bands.all).toHaveLength(5);
    const old = await request(app).get(P('/rules')).set(auth(hr)).query({ key: 'tax.schedule', asOf: '2026-01-01' });
    expect(old.body.rules[0].value.bands.single).toHaveLength(6);
    const ssf = await request(app).get(P('/rules')).set(auth(hr)).query({ key: 'ssf.deposit_days', asOf: '2026-01-01' });
    expect(ssf.body.rules[0].value.days).toBe(25);
  });
  it('components validate formulas; statutory components cannot be rewritten', async () => {
    const bad = await request(app).post(P('/components')).set(auth(hr)).send({ code: 'TRANSPORT', nameEn: 'Transport', type: 'earning', formula: 'BASIC * HRA_X' });
    expect(bad.status).toBe(400);
    expect(bad.body.error).toMatch(/unknown identifier HRA_X/);
    const ok = await request(app).post(P('/components')).set(auth(hr)).send({ code: 'TRANSPORT', nameEn: 'Transport', type: 'earning', formula: 'min(BASIC * 0.05, 2000)', dependsOnPaymentDays: true });
    expect(ok.status).toBe(201);
    const locked = await request(app).patch(P('/components/SSF_EE')).set(auth(hr)).send({ formula: 'BASIC * 0.05' });
    expect(locked.status).toBe(403);
    expect((await request(app).get(P('/components')).set(auth(sitaTok))).status).toBe(403);
  });
  it('assigns the standard structure with base amounts (effective-dated) and freezes the version', async () => {
    const s = await request(app).get(P('/structures')).set(auth(hr));
    stdVersionId = s.body.structures[0].versions[0].id;
    for (const [emp, base] of [[DEMO.sita, { BASIC: '40000.00', ALLOWANCE: '20000.00' }], [DEMO.ram, { BASIC: '30000.00', DA: '7380.00' }]] as const) {
      const a = await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: emp, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: base });
      expect(a.status).toBe(201);
    }
    const again = await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.ram, structureVersionId: stdVersionId, from: '2026-10-01', baseAmounts: { BASIC: '32000.00', DA: '7380.00' } });
    expect(again.status).toBe(201);
    const list = await request(app).get(P('/assignments')).set(auth(hr)).query({ employeeId: DEMO.ram });
    expect(list.body.assignments.map((x: { valid: string }) => x.valid)).toEqual(['[2026-10-01,)', '[2026-01-01,2026-10-01)']);
    const frozen = await db.query('SELECT frozen_at FROM salary_structure_version WHERE id = $1', [stdVersionId]);
    expect(frozen.rows[0].frozen_at).not.toBeNull();
  });
  it('employee submits a tax declaration; HR verifies it', async () => {
    const put = await request(app).put(P('/declarations')).set(auth(sitaTok)).send({ fiscalYearStart: 2083, lifePremium: '30000.00' });
    expect(put.status).toBe(200);
    expect((await request(app).put(P('/declarations')).set(auth(sitaTok)).send({ employeeId: DEMO.ram, fiscalYearStart: 2083 })).status).toBe(403);
    expect((await request(app).post(P(`/declarations/${DEMO.sita}/verify`)).set(auth(hr)).send({ fiscalYearStart: 2083, decision: 'verified' })).status).toBe(200);
    const get = await request(app).get(P('/declarations')).set(auth(sitaTok)).query({ fiscalYearStart: 2083 });
    expect(get.body.declaration).toMatchObject({ status: 'verified', life_premium: '30000.00' });
  });
  it('creates a loan for Ram', async () => {
    const l = await request(app).post(P('/loans')).set(auth(hr)).send({ employeeId: DEMO.ram, principal: '12000.00', emi: '5000.00', startBsYear: 2083, startBsMonth: 5 });
    expect(l.status).toBe(201);
    expect(l.body.installments).toBe(3);
  });
});

describe('payroll run lifecycle (Bhadra 2083)', () => {
  it('creates the period from the BS calendar', async () => {
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 5 });
    expect(p.status).toBe(201);
    expect(p.body.period).toMatchObject({ start_ad: '2026-08-17', end_ad: '2026-09-16', days: 31 });
    periodId = p.body.period.id;
  });
  it('pre-flight blocks on unlocked attendance and missing master data, then passes', async () => {
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId });
    expect(run.status).toBe(201);
    runId = run.body.id;
    await attendanceFor(DEMO.sita, '2026-08-17', '2026-09-16', ['2026-09-01', '2026-09-02', '2026-09-03'], false);
    await attendanceFor(DEMO.ram, '2026-08-17', '2026-09-16', [], false);
    let pf = await request(app).post(P(`/runs/${runId}/preflight`)).set(auth(hr));
    expect(pf.body.ok).toBe(false);
    expect(pf.body.blockers.map((b: { code: string }) => b.code)).toContain('attendance_not_locked');
    expect((await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr))).status).toBe(409);
    const lock = await request(app).post(P(`/runs/${runId}/lock-attendance`)).set(auth(hr));
    expect(lock.body.locked).toBe(62);
    pf = await request(app).post(P(`/runs/${runId}/preflight`)).set(auth(hr));
    expect(pf.body.ok).toBe(true);
    expect(pf.body.info.employees).toBe(2);
  });
  it('one-time inputs, compute: lines and totals match the engine', async () => {
    const ot = await db.query(`INSERT INTO ot_slip (employee_id, local_date, detected_min, approved_min, status, day_type) VALUES ($1, '2026-08-20', 600, 600, 'approved', 'working')`, [DEMO.ram]);
    expect(ot.rowCount).toBe(1);
    expect((await request(app).post(P(`/runs/${runId}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'BONUS', amount: '5000.00', note: 'spot bonus' }] })).status).toBe(201);
    const c = await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr));
    expect(c.status).toBe(200);
    expect(c.body.payslips).toBe(2);
    const run = await request(app).get(P(`/runs/${runId}`)).set(auth(hr));
    const sita = run.body.payslips.find((p: { employee_id: string }) => p.employee_id === DEMO.sita);
    const ram = run.body.payslips.find((p: { employee_id: string }) => p.employee_id === DEMO.ram);
    // Sita: 3 absent days of 31 → BASIC 40,000 × 28/31 = 36,129.03; ALLOWANCE 18,064.52; BONUS 5,000; SSF 11% of 36,129.03 = 3,974.19 → 3,974.
    expect(sita).toMatchObject({ paid_days: '28.00', lop_days: '3.00', gross: '59193.55', employee_statutory: '3974.00' }); // gross excludes the rounding adjustment
    const detail = await request(app).get(P(`/payslips/${sita.id}`)).set(auth(hr));
    const line = (code: string) => detail.body.lines.find((l: { code: string }) => l.code === code)?.amount;
    expect(line('BASIC')).toBe('36129.03');
    expect(line('BONUS')).toBe('5000.00');
    expect(line('SSF_ER')).toBe('7226.00');
    expect(D(detail.body.payslip.net).mod(1).isZero()).toBe(true);
    expect(detail.body.tax.fiscalYearStart).toBe(2083);
    expect(detail.body.tax.insuranceDeduction).toBe('30000.00');
    // Ram: PF regime, OT 10 h at 30,000/208 × 1.5 = 2,163; loan EMI 5,000.
    expect(ram).toMatchObject({ paid_days: '31.00', ot_hours: '10.00' });
    const ramDetail = await request(app).get(P(`/payslips/${ram.id}`)).set(auth(hr));
    const rl = (code: string) => ramDetail.body.lines.find((l: { code: string }) => l.code === code)?.amount;
    expect(rl('OT')).toBe('2163.00');
    expect(rl('PF_EE')).toBe('3000.00');
    expect(rl('GRATUITY')).toBe('2499.00');
    expect(rl('LOAN')).toBe('5000.00');
    expect(ram.flags).toContain('FirstPayslip');
    expect(run.body.run.totals.headcount).toBe('2');
  });
  it('maker-checker: maker cannot approve, HR cannot approve, finance (different user) can; then lock is immutable', async () => {
    expect((await request(app).post(P(`/runs/${runId}/approve`)).set(auth(finance))).status).toBe(409); // not reviewed yet
    expect((await request(app).post(P(`/runs/${runId}/review`)).set(auth(hr)).send({ notes: 'checked variances' })).status).toBe(200);
    expect((await request(app).post(P(`/runs/${runId}/approve`)).set(auth(hr))).status).toBe(403);
    // A finance user who is also the maker cannot approve.
    const makerFinance = await loginAs(db, app, ['hr', 'finance']);
    await db.query('UPDATE payroll_run SET maker_id = $2 WHERE id = $1', [runId, makerFinance.userId]);
    expect((await request(app).post(P(`/runs/${runId}/approve`)).set(auth(makerFinance.token))).status).toBe(403);
    const ap = await request(app).post(P(`/runs/${runId}/approve`)).set(auth(finance));
    expect(ap.status).toBe(200);
    expect(ap.body).toMatchObject({ approved: true, pendingSecondApproval: false });
    expect(ap.body.signature).toMatch(/^sha256:[0-9a-f]{64}$/);
    const lock = await request(app).post(P(`/runs/${runId}/lock`)).set(auth(hr));
    expect(lock.status).toBe(200);
    const run = await db.query('SELECT status, locked_at, checker_id FROM payroll_run WHERE id = $1', [runId]);
    expect(run.rows[0]).toMatchObject({ status: 'locked', checker_id: financeUserId });
    await expect(db.query(`UPDATE payslip SET net = 1 WHERE run_id = $1`, [runId])).rejects.toThrow(/locked/);
    await expect(db.query(`DELETE FROM payslip_line WHERE payslip_id IN (SELECT id FROM payslip WHERE run_id = $1)`, [runId])).rejects.toThrow(/locked/);
    await expect(db.query(`UPDATE payroll_run SET status = 'draft' WHERE id = $1`, [runId])).rejects.toThrow(/immutable/);
    expect((await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr))).status).toBe(409);
    const slip = await db.query(`SELECT status FROM ot_slip WHERE employee_id = $1 AND local_date = '2026-08-20'`, [DEMO.ram]);
    expect(slip.rows[0].status).toBe('posted');
    const inst = await db.query('SELECT amount FROM loan_installment');
    expect(inst.rows).toHaveLength(1);
  });
  it('bank files in each format, SSF sheet, ETDS and journal export', async () => {
    const nmb = await request(app).post(P(`/runs/${runId}/bank-file`)).set(auth(finance)).send({ bankCode: 'NMB' });
    expect(nmb.status).toBe(201);
    expect(nmb.body.rows).toBe(2);
    const dl = await request(app).get(P(`/runs/${runId}/bank-files/${nmb.body.id}`)).set(auth(finance));
    expect(dl.text).toMatch(/^001234560001\d*,Sita Sharma,\d+\.\d{2},007,Salary 2083-05\r\n/);
    const everest = await request(app).post(P(`/runs/${runId}/bank-file`)).set(auth(finance)).send({ bankCode: 'EVEREST' });
    expect(everest.status).toBe(201);
    expect(everest.body.rows).toBe(0); // nobody banks with Everest in the demo data
    const generic = await request(app).post(P(`/runs/${runId}/bank-file`)).set(auth(finance)).send({ bankCode: 'GENERIC' });
    expect(generic.status).toBe(201);
    expect((await request(app).post(P(`/runs/${runId}/bank-file`)).set(auth(hr)).send({ bankCode: 'NMB' })).status).toBe(403);
    const ssf = await request(app).get(P(`/runs/${runId}/ssf-sheet.csv`)).set(auth(hr));
    expect(ssf.text.split('\r\n')[1]).toMatch(/^E001,10000000001,Sita Sharma,36129\.03,3974\.00,7226\.00,361\.29,505\.81,97\.55,10235\.35,11200\.00$/);
    const etds = await request(app).get(P(`/runs/${runId}/etds.csv`)).set(auth(finance));
    expect(etds.text).toMatch(/E001,123456789,Sita Sharma,/);
    const j = await request(app).get(P(`/runs/${runId}/journal.json`)).set(auth(finance));
    const debit = j.body.journal.reduce((a: Decimal, r: { debit: string }) => a.plus(r.debit), D(0));
    const credit = j.body.journal.reduce((a: Decimal, r: { credit: string }) => a.plus(r.credit), D(0));
    expect(debit.toFixed(2)).toBe(credit.toFixed(2));
    expect((await request(app).post(P(`/runs/${runId}/disburse`)).set(auth(finance))).status).toBe(200);
    expect((await request(app).post(P(`/runs/${runId}/file`)).set(auth(finance))).status).toBe(200);
  });
  it('publish notifies employees; employees see only their own published payslips; HTML renders bilingual', async () => {
    expect((await request(app).get(P('/payslips')).set(auth(sitaTok))).body.payslips).toHaveLength(0);
    const pub = await request(app).post(P(`/runs/${runId}/publish`)).set(auth(hr));
    expect(pub.body.published).toBe(2);
    const mine = await request(app).get(P('/payslips')).set(auth(sitaTok));
    expect(mine.body.payslips).toHaveLength(1);
    const id = mine.body.payslips[0].id;
    expect((await request(app).get(P(`/payslips/${id}`)).set(auth(ramTok))).status).toBe(403);
    expect((await request(app).get(P('/payslips')).set(auth(ramTok)).query({ employeeId: DEMO.sita })).status).toBe(403);
    const html = await request(app).get(P(`/payslips/${id}/html`)).set(auth(sitaTok));
    expect(html.status).toBe(200);
    expect(html.text).toContain('तलब विवरण / Payslip');
    expect(html.text).toContain('भदौ २०८३ / Bhadra 2083');
    expect(html.text).toContain('*****6789');
    expect(html.text).toMatch(/Rupees Only/);
    expect(html.text).toMatch(/रुपैयाँ मात्र/);
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'payslip.published' AND employee_id = $1`, [DEMO.sita]);
    expect(n.rows[0].n).toBeGreaterThan(0);
  });
  it('a second regular run for a locked period is refused; a supplementary run with inputs works', async () => {
    expect((await request(app).post(P('/runs')).set(auth(hr)).send({ periodId })).status).toBe(409);
    const sup = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId, runType: 'supplementary', employeeIds: [DEMO.sita] });
    expect(sup.status).toBe(201);
    await request(app).post(P(`/runs/${sup.body.id}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'ARREARS', amount: '1500.00' }] });
    const c = await request(app).post(P(`/runs/${sup.body.id}/compute`)).set(auth(hr));
    expect(c.status).toBe(200);
    expect(c.body.payslips).toBe(1);
    // A supplementary run pays the one-time line only: no salary, no SSF, no loan; its projection reuses the regular run's recurring figures.
    const sr = await request(app).get(P(`/runs/${sup.body.id}`)).set(auth(hr));
    const sd = await request(app).get(P(`/payslips/${sr.body.payslips[0].id}`)).set(auth(hr));
    const codes = sd.body.lines.map((l: { code: string }) => l.code);
    expect(codes).toContain('ARREARS');
    expect(codes).not.toContain('BASIC');
    expect(codes).not.toContain('SSF_EE');
    expect(codes).not.toContain('LOAN');
    expect(sd.body.payslip.gross).toBe('1500.00');
    expect(sd.body.tax.recurringTaxable).toBe('68000.00');   // full-month 40,000 + 20,000 + employer SSF 8,000, borrowed from the locked regular payslip
    expect(sr.body.payslips[0].flags).not.toContain('NoRecurringReference');
  });
  it('arrears run: raised salary effective in the locked period pays the difference', async () => {
    await db.query(`UPDATE salary_assignment SET base_amounts = '{"BASIC":"45000.00","ALLOWANCE":"20000.00"}' WHERE employee_id = $1`, [DEMO.sita]);
    const ar = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId, runType: 'arrears', referencesRunId: runId, employeeIds: [DEMO.sita] });
    expect(ar.status).toBe(201);
    const c = await request(app).post(P(`/runs/${ar.body.id}/compute`)).set(auth(hr));
    expect(c.status).toBe(200);
    const run = await request(app).get(P(`/runs/${ar.body.id}`)).set(auth(hr));
    const d = await request(app).get(P(`/payslips/${run.body.payslips[0].id}`)).set(auth(hr));
    // 5,000 raise × 28/31 paid days = 4,516.13
    expect(d.body.lines.find((l: { code: string }) => l.code === 'ARREARS').amount).toBe('4516.13');
  });
  it('final settlement run for an exiting employee encashes leave and recovers loans in full', async () => {
    await db.query(`UPDATE employee SET exit_date = '2026-10-10' WHERE id = $1`, [DEMO.ram]);
    const lt = await db.query<{ id: string }>(`SELECT id FROM leave_type WHERE company_id = $1 AND code = 'HOME'`, [DEMO.companyId]);
    await db.query(`INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES ($1, $2, '2026-09-30', 12, 'allocation')`, [DEMO.ram, lt.rows[0]!.id]);
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 6 });
    await attendanceFor(DEMO.ram, '2026-09-17', '2026-10-10');
    const fs = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: p.body.period.id, runType: 'final_settlement', employeeIds: [DEMO.ram], noticePay: '-3000.00' });
    expect(fs.status).toBe(201);
    const c = await request(app).post(P(`/runs/${fs.body.id}/compute`)).set(auth(hr));
    expect(c.status).toBe(200);
    const run = await request(app).get(P(`/runs/${fs.body.id}`)).set(auth(hr));
    const d = await request(app).get(P(`/payslips/${run.body.payslips[0].id}`)).set(auth(hr));
    const line = (code: string) => d.body.lines.find((l: { code: string }) => l.code === code)?.amount;
    expect(d.body.payslip.paid_days).toBe('24.00');
    expect(line('LEAVE_ENCASH')).toBe('12387.10');       // 12 days × 32,000 / 31
    expect(line('NOTICE_DEDUCTION')).toBe('3000.00');
    expect(line('LOAN')).toBe('7000.00');                // remaining after one 5,000 instalment
    expect(line('FESTIVAL')).toBeDefined();              // pro-rated festival allowance (Sec 37(3))
    expect(run.body.payslips[0].flags).toContain('LastMonth');
  });
  it('bonus run allocates the 10% pool by wages with Bonus Act caps', async () => {
    const p = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 7 });
    const b = await request(app).post(P('/runs/bonus')).set(auth(hr)).send({ periodId: p.body.period.id, fiscalYearStart: 2083, netProfit: '1000000.00' });
    expect(b.status).toBe(201);
    expect(b.body.pool).toBe('100000.00');
    expect(b.body.allocation.length).toBe(2);
    const c = await request(app).post(P(`/runs/${b.body.id}/compute`)).set(auth(hr));
    expect(c.status).toBe(200);
    expect(c.body.payslips).toBe(1); // Ram exited before Kartik, so only Sita is paid in this period
  });
});
