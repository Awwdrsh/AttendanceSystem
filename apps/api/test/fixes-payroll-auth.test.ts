import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** Regression tests for payroll, sign-in, employee master and settings bugs found by the fake-data run. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hrUserId: string; let sita: string; let stdVersionId: string;
const P = (path: string) => `/api/v1/payroll${path}`;
const PASS = 'correct-horse-battery-staple';
const yesterday = () => localDateOf(Date.now() - 86_400_000);

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  const h = await loginAs(db, app, ['hr']); hr = h.token; hrUserId = h.userId;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  stdVersionId = (await request(app).get(P('/structures')).set(auth(hr))).body.structures[0].versions[0].id;
});
afterAll(async () => { await db.end(); });

describe('payroll', () => {
  it('a salary revision inside the period is day-weighted (69,500 for 15 days, 60,000 for 16 days of Bhadra 2083)', async () => {
    expect((await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: { BASIC: '69500.00' } })).status).toBe(201);
    expect((await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: stdVersionId, from: '2026-09-01', baseAmounts: { BASIC: '60000.00' } })).status).toBe(201);
    for (let t = Date.parse('2026-08-17T00:00:00Z'); t <= Date.parse('2026-09-16T00:00:00Z'); t += 86_400_000) {
      await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash, locked_at) VALUES ($1, $2, 1, 'Present', 480, 'h', now())`, [DEMO.sita, new Date(t).toISOString().slice(0, 10)]);
    }
    const period = (await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: 2083, bsMonth: 5 })).body.period;
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: period.id, runType: 'supplementary', employeeIds: [DEMO.sita] });
    // A regular run scoped to one employee keeps this test independent of the other demo employee's data.
    await db.query(`UPDATE payroll_run SET run_type = 'regular' WHERE id = $1`, [run.body.id]);
    expect((await request(app).post(P(`/runs/${run.body.id}/compute`)).set(auth(hr)).send({ overrideReason: 'master data not filled in this test' })).status).toBe(200);
    const slip = (await request(app).get(P(`/runs/${run.body.id}`)).set(auth(hr))).body.payslips[0];
    const detail = await request(app).get(P(`/payslips/${slip.id}`)).set(auth(hr));
    // (69,500 × 15 + 60,000 × 16) / 31 = 64,596.77
    expect(detail.body.lines.find((l: { code: string }) => l.code === 'BASIC').amount).toBe('64596.77');
    expect(slip.flags).toContain('SalaryRevised');
    expect(detail.body.payslip.explain[0]).toMatchObject({ code: 'SALARY_REVISION' });
    expect(detail.body.payslip.explain[0].text).toMatch(/BASIC 69500\.00 for 15 days from 2026-08-17; BASIC 60000\.00 for 16 days from 2026-09-01/);

    // Inputs for someone outside a scoped run are refused instead of silently dropped.
    const out = await request(app).post(P(`/runs/${run.body.id}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.ram, componentCode: 'BONUS', amount: '100.00' }] });
    expect(out.status).toBe(400);
    expect(out.body.error).toMatch(/not in it/);
    expect((await request(app).post(P(`/runs/${run.body.id}/inputs`)).set(auth(hr)).send({ inputs: [{ employeeId: DEMO.sita, componentCode: 'BONUS', amount: '100.00' }] })).status).toBe(201);
  });

  it('salary amounts for components outside the structure are refused, listing them', async () => {
    const r = await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.ram, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: { BASIC: '30000.00', NOTACOMP: '100.00' } });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatch(/NOTACOMP/);
    expect((await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: DEMO.ram, structureVersionId: stdVersionId, from: '2026-01-01', baseAmounts: {} })).status).toBe(400);
  });

  it('formulas that refer back to themselves, directly or through another component, are refused', async () => {
    const self = await request(app).post(P('/components')).set(auth(hr)).send({ code: 'LOOP', nameEn: 'Loop', type: 'earning', formula: 'LOOP + 1' });
    expect(self.status).toBe(400);
    expect(self.body.error).toMatch(/LOOP → LOOP/);
    expect((await request(app).post(P('/components')).set(auth(hr)).send({ code: 'CYA', nameEn: 'A', type: 'earning', formula: 'BASIC * 0.1' })).status).toBe(201);
    expect((await request(app).post(P('/components')).set(auth(hr)).send({ code: 'CYB', nameEn: 'B', type: 'earning', formula: 'CYA + 1' })).status).toBe(201);
    const cyc = await request(app).patch(P('/components/CYA')).set(auth(hr)).send({ formula: 'CYB + 1' });
    expect(cyc.status).toBe(400);
    expect(cyc.body.error).toMatch(/CYA → CYB → CYA/);
    expect((await request(app).patch(P('/components/CYA')).set(auth(hr)).send({ formula: 'BASIC * 0.2' })).status).toBe(200);
  });

  it('a NUL byte in a path or body is a 400, not a server error', async () => {
    expect((await request(app).patch(P('/components/%00')).set(auth(hr)).send({ nameEn: 'x' })).status).toBe(400);
    expect((await request(app).patch(P('/components/CYB')).set(auth(hr)).send({ nameEn: 'bad\u0000name' })).status).toBe(400);
  });
});

describe('overtime', () => {
  it('approving an overtime slip twice is a conflict', async () => {
    const id = (await db.query<{ id: string }>(`INSERT INTO ot_slip (employee_id, local_date, detected_min, status, day_type) VALUES ($1, '2026-10-01', 90, 'detected', 'working') RETURNING id`, [DEMO.ram])).rows[0]!.id;
    expect((await request(app).post(`/api/v1/ot/slips/${id}/approve`).set(auth(hr)).send({})).status).toBe(200);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/approve`).set(auth(hr)).send({})).status).toBe(409);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/reject`).set(auth(hr)).send({ reason: 'not pre-approved' })).status).toBe(200);
    expect((await request(app).post(`/api/v1/ot/slips/${id}/reject`).set(auth(hr)).send({ reason: 'not pre-approved' })).status).toBe(409);
  });
});

describe('employee master', () => {
  it('a PAN already recorded for another employee is refused on create, update and import', async () => {
    expect((await request(app).patch(`/api/v1/employees/${DEMO.sita}`).set(auth(hr)).send({ pan: '123456789' })).status).toBe(200);
    expect((await request(app).patch(`/api/v1/employees/${DEMO.sita}`).set(auth(hr)).send({ pan: '123456789' })).status).toBe(200); // same person, same PAN
    const upd = await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ pan: '123456789' });
    expect(upd.status).toBe(409);
    expect(upd.body.error).toMatch(/E001/);
    expect((await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'E900', nameEn: 'Copy Cat', pan: '123456789' })).status).toBe(409);
    const imp = await request(app).post('/api/v1/employees/import').set(auth(hr)).set('content-type', 'text/csv').send('emp_code,name_en,pan\nE901,Dup One,987654321\nE902,Dup Two,987654321\nE903,Dup Three,123456789\n');
    expect(imp.status).toBe(400);
    expect(imp.body.errors.map((e: { message: string }) => e.message).join(' ')).toMatch(/duplicate PAN in file.*already recorded for E001/s);
  });

  it('search text is literal: % and _ are not wildcards', async () => {
    const all = await request(app).get('/api/v1/employees').set(auth(hr));
    expect(all.body.employees.length).toBeGreaterThan(1);
    for (const s of ['%', '_', '\\']) expect((await request(app).get('/api/v1/employees').query({ search: s }).set(auth(hr))).body.employees).toHaveLength(0);
    expect((await request(app).get('/api/v1/employees').query({ search: 'sita' }).set(auth(hr))).body.employees).toHaveLength(1);
  });

  it('an employee can read only their own shift calendar', async () => {
    const q = { from: '2026-10-01', to: '2026-10-07' };
    expect((await request(app).get('/api/v1/shifts/calendar').query({ ...q, employeeId: DEMO.sita }).set(auth(sita))).status).toBe(200);
    expect((await request(app).get('/api/v1/shifts/calendar').query({ ...q, employeeId: DEMO.ram }).set(auth(sita))).status).toBe(403);
    expect((await request(app).get('/api/v1/shifts/calendar').query({ ...q, employeeId: DEMO.ram }).set(auth(hr))).status).toBe(200);
  });
});

describe('settings', () => {
  it('an unsaved company profile defaults to the company name and saves back unchanged', async () => {
    const s = await request(app).get('/api/v1/settings').set(auth(hr));
    const profile = s.body.settings.find((x: { key: string }) => x.key === 'company.profile');
    expect(profile.value).toMatchObject({ nameEn: expect.any(String) });
    expect(profile.value.nameEn.length).toBeGreaterThan(0);
    expect((await request(app).put('/api/v1/settings/company.profile').set(auth(hr)).send(profile.value)).status).toBe(200);
    expect((await request(app).put('/api/v1/settings/company.profile').set(auth(hr)).send({ ...profile.value, address: null, pan: null })).status).toBe(200);
  });
});

describe('sign-in', () => {
  it('someone who has left cannot sign in, and their open session stops working', async () => {
    const emp = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E950', 'Leaving Soon') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    await createUser(db, { companyId: DEMO.companyId, employeeId: emp, email: 'leaver@test.local', name: 'Leaver', password: PASS, roles: ['employee'], pepper: TEST_PEPPER });
    const login = await request(app).post('/api/v1/auth/login').send({ email: 'leaver@test.local', password: PASS });
    expect(login.status).toBe(200);
    await db.query(`UPDATE employee SET exit_date = $2, status = 'exited' WHERE id = $1`, [emp, localDateOf(Date.now())]);
    expect((await request(app).get('/api/v1/auth/me').set(auth(login.body.token))).status).toBe(200); // last working day: still allowed
    await db.query(`UPDATE employee SET exit_date = $2 WHERE id = $1`, [emp, yesterday()]);
    expect((await request(app).get('/api/v1/auth/me').set(auth(login.body.token))).status).toBe(401);
    const again = await request(app).post('/api/v1/auth/login').send({ email: 'leaver@test.local', password: PASS });
    expect(again.status).toBe(401);
    expect(again.body.error).toMatch(/left the company/);
    // Users without an employee link (HR, admin) are unaffected.
    expect((await request(app).get('/api/v1/auth/me').set(auth(hr))).body.user.id).toBe(hrUserId);
  });

  it('the per-address throttle counts only failed sign-ins, so a whole office can sign in from one address', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'office@test.local', name: 'Office', password: PASS, roles: ['employee'], pepper: TEST_PEPPER });
    for (let i = 0; i < 40; i++) expect((await request(app).post('/api/v1/auth/login').send({ email: 'office@test.local', password: PASS })).status).toBe(200);
    // Failures go to unknown addresses so no account lockout interferes; the 31st failure from the same address is throttled.
    // (Earlier tests in this file already failed one sign-in from this address, so the limit is reached one attempt sooner.)
    let throttledAt = -1;
    for (let i = 0; i < 31 && throttledAt < 0; i++) {
      const st = (await request(app).post('/api/v1/auth/login').send({ email: `nobody${i}@test.local`, password: 'wrong-password-1' })).status;
      if (st === 429) throttledAt = i; else expect(st).toBe(401);
    }
    expect(throttledAt).toBeGreaterThanOrEqual(28);
    expect(throttledAt).toBeLessThanOrEqual(30);
  });
});
