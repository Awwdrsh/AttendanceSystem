import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf } from '@hajiri/attendance-engine';
import { adToBs } from '@hajiri/bs-calendar';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { remindTravelSettlements } from '../src/modules/expenses/jobs.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hr2: string; let sita: string; let ram: string; let stranger: string; let otherHr: string;
let cats: Record<string, string> = {};
const E = (p: string) => `/api/v1/expenses${p}`;
const P = (p: string) => `/api/v1/payroll${p}`;
const PASS = 'correct-horse-battery-staple';
const today = localDateOf(Date.now());
const daysAgo = (n: number) => new Date(new Date(`${today}T00:00:00Z`).getTime() - n * 86_400_000).toISOString().slice(0, 10);
const daysAhead = (n: number) => daysAgo(-n);
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;

async function upload(token: string, purpose = 'expense_receipt'): Promise<string> {
  const r = await request(app).post('/api/v1/files').set(auth(token)).send({ purpose, filename: 'bill.pdf', dataUrl: PDF });
  expect(r.status).toBe(201);
  return r.body.file.id;
}

/** Manager then HR: the two-step chain for claims, trips and settlements. */
async function approveBoth(path: string, body: Record<string, unknown> = {}) {
  const m = await request(app).post(path).set(auth(ram)).send({ decision: 'approve', ...body });
  expect(m.status).toBe(200);
  expect(m.body.state).toBe('pending');
  const h = await request(app).post(path).set(auth(hr)).send({ decision: 'approve', ...body });
  expect(h.status).toBe(200);
  expect(h.body.state).toBe('approved');
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  hr2 = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  stranger = (await loginAs(db, app, ['manager'])).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`UPDATE employee SET grade = 'G3' WHERE id = $1`, [DEMO.sita]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`);
  await createUser(db, { companyId: c.rows[0]!.id, email: 'hr@other.local', name: 'Other HR', password: PASS, roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.local', password: PASS })).body.token;
  const pol = await request(app).get(E('/policy')).set(auth(sita));
  cats = Object.fromEntries(pol.body.categories.map((x: { code: string; id: string }) => [x.code, x.id]));
});
afterAll(async () => { await db.end(); });

describe('policy', () => {
  it('everyone sees the categories and travel rates; only HR changes them', async () => {
    const r = await request(app).get(E('/policy')).set(auth(sita));
    expect(r.status).toBe(200);
    expect(r.body.categories.map((x: { code: string }) => x.code)).toEqual(expect.arrayContaining(['LOCAL_TRAVEL', 'MEALS', 'FUEL', 'MOBILE', 'MEDICAL', 'OFFICE', 'OTHER']));
    expect(r.body.tadaRates).toHaveLength(4);
    expect(r.body.myGrade).toBe('G3');
    expect((await request(app).put(E('/policy/settings')).set(auth(sita)).send({ mileageRatePerKm: '20', limitMode: 'warn', claimWindowDays: 60 })).status).toBe(403);
    expect((await request(app).put(E('/policy/settings')).set(auth(ram)).send({ mileageRatePerKm: '20', limitMode: 'warn', claimWindowDays: 60 })).status).toBe(403);
    const s = await request(app).put(E('/policy/settings')).set(auth(hr)).send({ mileageRatePerKm: '20', limitMode: 'block', claimWindowDays: 60 });
    expect(s.status).toBe(200);
    expect(s.body.settings.mileageRatePerKm).toBe('20.00');
  });
  it('HR sets limits per grade and TADA rates; duplicates are refused', async () => {
    expect((await request(app).put(E(`/categories/${cats.MEALS}/limits`)).set(auth(hr)).send({ limits: [{ grade: 'G3', perClaim: '1500' }, { grade: 'G3', perClaim: '2000' }] })).status).toBe(400);
    const l = await request(app).put(E(`/categories/${cats.MEALS}/limits`)).set(auth(hr)).send({ limits: [{ grade: 'G3', perClaim: '1500', perMonth: '3000' }, { grade: '*', perClaim: '800' }] });
    expect(l.status).toBe(200);
    const rates = await request(app).put(E('/tada-rates')).set(auth(hr)).send({ rates: [
      { grade: '*', destination: 'valley', daRate: '1000', lodgingRate: '2500' }, { grade: '*', destination: 'city', daRate: '1500', lodgingRate: '3000' },
      { grade: 'G3', destination: 'city', daRate: '2000', lodgingRate: '3500' }, { grade: '*', destination: 'international', daRate: '50', lodgingRate: '100' },
    ] });
    expect(rates.status).toBe(200);
    expect((await request(app).put(E('/tada-rates')).set(auth(sita)).send({ rates: [] })).status).toBe(403);
  });
  it('HR adds a category; another company cannot touch it', async () => {
    const c = await request(app).post(E('/categories')).set(auth(hr)).send({ code: 'TRAINING', name: 'Training fees', kind: 'amount', receiptRequiredAbove: '0' });
    expect(c.status).toBe(201);
    expect((await request(app).patch(E(`/categories/${c.body.id}`)).set(auth(otherHr)).send({ name: 'x' })).status).toBe(404);
    expect((await request(app).patch(E(`/categories/${c.body.id}`)).set(auth(hr)).send({ active: false })).status).toBe(200);
  });
});

describe('expense claims', () => {
  let claimId = '';
  it('mileage is km × the company rate; receipts are required above the category threshold', async () => {
    const chk = await request(app).post(E('/claims/check')).set(auth(sita)).send({ items: [
      { expenseDate: daysAgo(2), categoryId: cats.FUEL, description: 'Client visit, Lalitpur', km: 12 },
      { expenseDate: daysAgo(2), categoryId: cats.LOCAL_TRAVEL, description: 'Taxi', amount: '900' },
    ] });
    expect(chk.status).toBe(200);
    expect(chk.body.amounts).toEqual(['240.00', '900.00']);
    expect(chk.body.issues.map((i: { code: string }) => i.code)).toContain('receipt_required');
    const blocked = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Client visit', submit: true, items: [{ expenseDate: daysAgo(2), categoryId: cats.LOCAL_TRAVEL, description: 'Taxi', amount: '900' }] });
    expect(blocked.status).toBe(400);
    expect(blocked.body.details.code).toBe('claim_blocked');
  });
  it('limits per claim follow the grade; future and old dates are refused', async () => {
    const over = await request(app).post(E('/claims/check')).set(auth(sita)).send({ items: [{ expenseDate: daysAgo(1), categoryId: cats.MEALS, description: 'Team lunch', amount: '1600', receiptFileId: null }] });
    const codes = over.body.issues.map((i: { code: string }) => i.code);
    expect(codes).toContain('per_claim_limit');
    expect(over.body.issues.find((i: { code: string }) => i.code === 'per_claim_limit').message).toMatch(/1,500/);
    const fut = await request(app).post(E('/claims/check')).set(auth(sita)).send({ items: [{ expenseDate: daysAhead(2), categoryId: cats.OFFICE, description: 'Pens', amount: '100' }] });
    expect(fut.body.issues.map((i: { code: string }) => i.code)).toContain('future_date');
    const old = await request(app).post(E('/claims/check')).set(auth(sita)).send({ items: [{ expenseDate: daysAgo(80), categoryId: cats.OFFICE, description: 'Pens', amount: '100' }] });
    expect(old.body.issues.map((i: { code: string }) => i.code)).toContain('too_old');
  });
  it('a draft is private, editable, and submits into the manager → HR chain', async () => {
    const receipt = await upload(sita);
    const d = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Client visit', items: [
      { expenseDate: daysAgo(2), categoryId: cats.LOCAL_TRAVEL, description: 'Taxi', amount: '900', receiptFileId: receipt },
      { expenseDate: daysAgo(2), categoryId: cats.FUEL, description: 'Own bike', km: 10 },
    ] });
    expect(d.status).toBe(201);
    expect(d.body.status).toBe('draft');
    claimId = d.body.id;
    expect((await request(app).get(E(`/claims/${claimId}`)).set(auth(ram))).status).toBe(404);
    const teamList = await request(app).get(E('/claims')).set(auth(ram));
    expect(teamList.body.claims.find((c: { id: string }) => c.id === claimId)).toBeUndefined();
    // Another employee cannot reuse the receipt.
    const other = await request(app).post(E('/claims')).set(auth(ram)).send({ title: 'x', items: [{ expenseDate: daysAgo(1), categoryId: cats.LOCAL_TRAVEL, description: 'Taxi', amount: '900', receiptFileId: receipt }] });
    expect([400, 403]).toContain(other.status);
    const sub = await request(app).post(E(`/claims/${claimId}/submit`)).set(auth(sita));
    expect(sub.status).toBe(200);
    expect((await request(app).put(E(`/claims/${claimId}`)).set(auth(sita)).send({ title: 'x', items: [{ expenseDate: daysAgo(1), categoryId: cats.OFFICE, description: 'x', amount: '1' }] })).status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const card = inbox.body.items.find((i: { objectId: string }) => i.objectId === claimId);
    expect(card.objectType).toBe('expense_claim');
    expect(card.summary).toMatchObject({ title: 'Client visit', amount: '1100.00', items: 2, receipts: 1 });
  });
  it('outsiders, other companies and the employee cannot decide; the receipt is shown to the manager', async () => {
    expect((await request(app).get(E(`/claims/${claimId}`)).set(auth(stranger))).status).toBe(404);
    expect((await request(app).get(E(`/claims/${claimId}`)).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).post(E(`/claims/${claimId}/review`)).set(auth(sita)).send({ decision: 'approve' })).status).toBe(403);
    expect((await request(app).post(E(`/claims/${claimId}/review`)).set(auth(stranger)).send({ decision: 'approve' })).status).toBe(403);
    const detail = await request(app).get(E(`/claims/${claimId}`)).set(auth(ram));
    expect(detail.body.canDecide).toBe(true);
    const line = detail.body.items.find((i: { receiptFileId: string | null }) => i.receiptFileId);
    const f = await request(app).get(E(`/claims/${claimId}/items/${line.id}/receipt`)).set(auth(ram));
    expect(f.status).toBe(200);
    expect(f.headers['content-type']).toContain('application/pdf');
    expect((await request(app).get(E(`/claims/${claimId}/items/${line.id}/receipt`)).set(auth(stranger))).status).toBe(404);
  });
  it('HR approves less than claimed only with a reason; the payout waits for payroll', async () => {
    expect((await request(app).post(E(`/claims/${claimId}/review`)).set(auth(ram)).send({ decision: 'approve' })).body.state).toBe('pending');
    const detail = await request(app).get(E(`/claims/${claimId}`)).set(auth(hr));
    const taxi = detail.body.items.find((i: { description: string }) => i.description === 'Taxi');
    expect((await request(app).post(E(`/claims/${claimId}/review`)).set(auth(hr)).send({ decision: 'approve', items: [{ id: taxi.id, approvedAmount: '1000' }] })).status).toBe(400);
    expect((await request(app).post(E(`/claims/${claimId}/review`)).set(auth(hr)).send({ decision: 'approve', items: [{ id: taxi.id, approvedAmount: '700' }] })).status).toBe(400);
    const ok = await request(app).post(E(`/claims/${claimId}/review`)).set(auth(hr)).send({ decision: 'approve', items: [{ id: taxi.id, approvedAmount: '700', reason: 'Fare cap for this route' }] });
    expect(ok.body.state).toBe('approved');
    const after = await request(app).get(E(`/claims/${claimId}`)).set(auth(sita));
    expect(after.body.claim.status).toBe('approved');
    expect(after.body.claim.totalApproved).toBe('900.00');
    expect(after.body.payouts).toEqual([expect.objectContaining({ code: 'EXP_REIMB', amount: '900.00', status: 'pending' })]);
    const pending = await request(app).get(E('/reports/pending-payment')).set(auth(hr));
    expect(pending.body.rows).toHaveLength(1);
    expect((await request(app).get(E('/reports/pending-payment')).set(auth(ram))).status).toBe(403);
  });
  it('rejecting needs a reason; cancelling a pending claim withdraws it', async () => {
    const c = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Stationery', submit: true, items: [{ expenseDate: daysAgo(1), categoryId: cats.OFFICE, description: 'Notebooks', amount: '300', receiptFileId: await upload(sita) }] });
    expect(c.status).toBe(201);
    expect((await request(app).post(E(`/claims/${c.body.id}/review`)).set(auth(ram)).send({ decision: 'reject' })).status).toBe(400);
    const cancel = await request(app).post(E(`/claims/${c.body.id}/cancel`)).set(auth(sita));
    expect(cancel.body.status).toBe('cancelled');
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(inbox.body.items.find((i: { objectId: string }) => i.objectId === c.body.id)).toBeUndefined();
  });
  it('the monthly limit counts other open claims in the same BS month', async () => {
    const day = daysAgo(1);
    const first = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Lunch 1', submit: true, items: [{ expenseDate: day, categoryId: cats.MEALS, description: 'Lunch', amount: '1400', receiptFileId: await upload(sita) }] });
    expect(first.status).toBe(201);
    const second = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Lunch 2', submit: true, items: [{ expenseDate: day, categoryId: cats.MEALS, description: 'Lunch', amount: '1400', receiptFileId: await upload(sita) }] });
    // Same BS month as the first only when both dates fall in it; daysAgo(1) and the other are the same day here.
    expect(second.status).toBe(201);
    const third = await request(app).post(E('/claims')).set(auth(sita)).send({ title: 'Lunch 3', submit: true, items: [{ expenseDate: day, categoryId: cats.MEALS, description: 'Lunch', amount: '400', receiptFileId: await upload(sita) }] });
    expect(third.status).toBe(400);
    expect(third.body.error).toMatch(/monthly limit/);
    await request(app).post(E(`/claims/${first.body.id}/cancel`)).set(auth(sita));
    await request(app).post(E(`/claims/${second.body.id}/cancel`)).set(auth(sita));
  });
  it('HR sees everyone, a manager the team, an employee only themself', async () => {
    expect((await request(app).get(E('/claims')).query({ scope: 'all' }).set(auth(ram))).status).toBe(403);
    const all = await request(app).get(E('/claims')).set(auth(hr));
    expect(all.body.claims.length).toBeGreaterThan(0);
    expect(all.body.claims.every((c: { status: string }) => c.status !== 'draft')).toBe(true);
    const mine = await request(app).get(E('/claims')).set(auth(sita));
    expect(mine.body.claims.every((c: { employeeId: string }) => c.employeeId === DEMO.sita)).toBe(true);
    expect((await request(app).get(E('/claims')).set(auth(otherHr))).body.claims).toHaveLength(0);
  });
});

describe('travel', () => {
  let tripId = '';
  it('quotes daily allowance by grade and lodging per night', async () => {
    const q = await request(app).post(E('/travel/quote')).set(auth(sita)).send({ destinationCategory: 'city', fromDate: daysAhead(3), toDate: daysAhead(5) });
    expect(q.status).toBe(200);
    expect(q.body.quote).toMatchObject({ days: 3, nights: 2, daRate: '2000.00', daAmount: '6000.00', lodgingLimit: '7000.00', grade: 'G3' });
    const intl = await request(app).post(E('/travel/quote')).set(auth(sita)).send({ destinationCategory: 'international', fromDate: daysAhead(3), toDate: daysAhead(4) });
    expect(intl.status).toBe(400);
    const intl2 = await request(app).post(E('/travel/quote')).set(auth(sita)).send({ destinationCategory: 'international', fromDate: daysAhead(3), toDate: daysAhead(4), fxRate: '136.5' });
    expect(intl2.body.quote).toMatchObject({ currency: 'USD', daRateNpr: '6825.00', daAmount: '13650.00', lodgingLimit: '13650.00' });
  });
  it('a request asks for an advance up to the expected cost; overlapping trips are refused', async () => {
    const big = await request(app).post(E('/travel')).set(auth(sita)).send({ destination: 'Pokhara', destinationCategory: 'city', purpose: 'Site survey', fromDate: daysAgo(4), toDate: daysAhead(1), mode: 'bus', estimatedCost: '2000', advanceRequested: '99999' });
    expect(big.status).toBe(400);
    const t = await request(app).post(E('/travel')).set(auth(sita)).send({ destination: 'Pokhara', destinationCategory: 'city', purpose: 'Site survey', fromDate: daysAgo(4), toDate: daysAhead(1), mode: 'bus', estimatedCost: '2000', advanceRequested: '10000' });
    expect(t.status).toBe(201);
    tripId = t.body.id;
    const dup = await request(app).post(E('/travel')).set(auth(sita)).send({ destination: 'Butwal', destinationCategory: 'city', purpose: 'Other', fromDate: daysAgo(1), toDate: daysAhead(2), mode: 'bus' });
    expect(dup.status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(inbox.body.items.find((i: { objectId: string }) => i.objectId === tripId).summary).toMatchObject({ destination: 'Pokhara', days: 6, advance: '10000.00' });
  });
  it('approvers may lower the advance; approval queues it for payroll', async () => {
    expect((await request(app).post(E(`/travel/${tripId}/review`)).set(auth(ram)).send({ decision: 'approve', advanceApproved: '20000' })).status).toBe(400);
    await approveBoth(E(`/travel/${tripId}/review`), { advanceApproved: '8000' });
    const t = await request(app).get(E(`/travel/${tripId}`)).set(auth(sita));
    expect(t.body.trip).toMatchObject({ status: 'approved', advanceApproved: '8000.00', advanceStatus: 'pending' });
    expect(t.body.payouts).toEqual([expect.objectContaining({ code: 'TRAVEL_ADV', amount: '8000.00', status: 'pending' })]);
    expect(t.body.canSettle).toBe(true);
    expect((await request(app).get(E(`/travel/${tripId}`)).set(auth(stranger))).status).toBe(404);
    const adv = await request(app).get(E('/reports/advances')).set(auth(hr));
    expect(adv.body.rows).toHaveLength(1);
  });
  it('HR pays the advance in cash; payroll then never pays it', async () => {
    expect((await request(app).post(E(`/travel/${tripId}/advance-outside`)).set(auth(ram)).send({ note: 'cash' })).status).toBe(403);
    const r = await request(app).post(E(`/travel/${tripId}/advance-outside`)).set(auth(hr)).send({ note: 'Paid from petty cash' });
    expect(r.status).toBe(200);
    const p = await db.query(`SELECT status FROM expense_payout WHERE source_type = 'travel_advance' AND source_id = $1`, [tripId]);
    expect(p.rows[0].status).toBe('cash');
    expect((await request(app).post(E(`/travel/${tripId}/cancel`)).set(auth(sita))).status).toBe(409);
  });
  it('settlement: DA for days travelled + lodging up to the limit + bills − advance; overspend of advance is recovered', async () => {
    expect((await request(app).post(E(`/travel/${tripId}/settlement`)).set(auth(sita)).send({ actualFrom: daysAgo(4), actualTo: daysAhead(1), bills: [] })).status).toBe(400);
    const noFile = await request(app).post(E(`/travel/${tripId}/settlement`)).set(auth(sita)).send({ actualFrom: daysAgo(4), actualTo: daysAgo(3), bills: [{ kind: 'transport', billDate: daysAgo(4), description: 'Bus', amount: '1200' }] });
    expect(noFile.status).toBe(400);
    const bill = await upload(sita, 'travel_bill');
    const s = await request(app).post(E(`/travel/${tripId}/settlement`)).set(auth(sita)).send({
      actualFrom: daysAgo(4), actualTo: daysAgo(4),
      bills: [{ kind: 'transport', billDate: daysAgo(4), description: 'Bus', amount: '1200', fileId: bill }, { kind: 'lodging', billDate: daysAgo(4), description: 'Hotel', amount: '400' }],
    });
    expect(s.status).toBe(201);
    // One day: DA 2000, no nights so lodging allowed 0, transport 1200 → 3200 − 8000 advance = −4800 to recover.
    expect(s.body).toMatchObject({ daAmount: '2000.00', lodgingAllowed: '0.00', entitlement: '3200.00', advance: '8000.00', net: '-4800.00' });
    expect((await request(app).post(E(`/travel/${tripId}/settlement`)).set(auth(sita)).send({ actualFrom: daysAgo(4), actualTo: daysAgo(4), bills: [] })).status).toBe(409);
    await approveBoth(E(`/settlements/${s.body.id}/review`));
    const t = await request(app).get(E(`/travel/${tripId}`)).set(auth(hr));
    expect(t.body.trip.status).toBe('settled');
    expect(t.body.payouts).toEqual(expect.arrayContaining([expect.objectContaining({ code: 'EXP_RECOVERY', amount: '4800.00', status: 'pending' })]));
    const billRow = t.body.settlements[0].bills.find((b: { fileId: string | null }) => b.fileId);
    expect((await request(app).get(E(`/settlements/${s.body.id}/bills/${billRow.id}/file`)).set(auth(ram))).status).toBe(200);
    expect((await request(app).get(E(`/settlements/${s.body.id}/bills/${billRow.id}/file`)).set(auth(otherHr))).status).toBe(404);
  });
  it('an advance payroll has not picked up yet is cancelled at settlement and the full entitlement is paid', async () => {
    const t = await request(app).post(E('/travel')).set(auth(sita)).send({ destination: 'Hetauda', destinationCategory: 'city', purpose: 'Training', fromDate: daysAgo(2), toDate: daysAgo(-0), mode: 'office_vehicle', advanceRequested: '3000' });
    expect(t.status).toBe(201);
    await approveBoth(E(`/travel/${t.body.id}/review`));
    const s = await request(app).post(E(`/travel/${t.body.id}/settlement`)).set(auth(sita)).send({ actualFrom: daysAgo(2), actualTo: daysAgo(2), bills: [] });
    expect(s.body.net).toBe('-1000.00');
    await approveBoth(E(`/settlements/${s.body.id}/review`));
    const p = await db.query<{ component_code: string; status: string; amount: string }>(
      `SELECT component_code, status, amount::text FROM expense_payout WHERE (source_type = 'travel_advance' AND source_id = $1) OR (source_type = 'travel_settlement' AND source_id = $2) ORDER BY created_at`, [t.body.id, s.body.id]);
    expect(p.rows).toEqual([{ component_code: 'TRAVEL_ADV', status: 'cancelled', amount: '3000.00' }, { component_code: 'EXP_REIMB', status: 'pending', amount: '2000.00' }]);
    const st = await db.query('SELECT advance::text, net::text FROM travel_settlement WHERE id = $1', [s.body.id]);
    expect(st.rows[0]).toEqual({ advance: '0.00', net: '2000.00' });
  });
  it('reminds travellers to settle 3 days after the trip', async () => {
    const t = await request(app).post(E('/travel')).set(auth(sita)).send({ destination: 'Dhulikhel', destinationCategory: 'valley', purpose: 'Offsite', fromDate: daysAhead(10), toDate: daysAhead(10), mode: 'bus' });
    await approveBoth(E(`/travel/${t.body.id}/review`));
    const later = new Date(new Date(`${daysAhead(13)}T06:00:00Z`).getTime());
    expect(await remindTravelSettlements(db, later)).toBeGreaterThanOrEqual(1);
  });
});

describe('payroll', () => {
  let runId = '';
  it('pre-flight shows what will be paid and recovered', async () => {
    await db.query(`UPDATE employee SET join_date = '2026-01-01' WHERE id = ANY($1)`, [[DEMO.sita, DEMO.ram]]);
    const s = await request(app).get(P('/structures')).set(auth(hr));
    const versionId = s.body.structures[0].versions[0].id;
    for (const [emp, base] of [[DEMO.sita, { BASIC: '40000.00', ALLOWANCE: '20000.00' }], [DEMO.ram, { BASIC: '30000.00' }]] as const) {
      expect((await request(app).post(P('/assignments')).set(auth(hr)).send({ employeeId: emp, structureVersionId: versionId, from: '2026-01-01', baseAmounts: base })).status).toBe(201);
    }
    const bs = adToBs(today);
    const period = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: bs.year, bsMonth: bs.month });
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: period.body.period.id });
    runId = run.body.id;
    const pf = await request(app).post(P(`/runs/${runId}/preflight`)).set(auth(hr));
    const w = pf.body.warnings.find((x: { code: string }) => x.code === 'expense_payouts');
    expect(w.message).toMatch(/Rs 2,900.*to pay.*Rs 4,800.*to recover/);
  });
  it('a run pays the reimbursement exactly once, untaxed, and recovers the advance', async () => {
    const reason = { overrideReason: 'test run without locked attendance' };
    const c1 = await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr)).send(reason);
    expect(c1.status).toBe(200);
    // Recomputing releases and re-claims: still exactly one line, never doubled.
    expect((await request(app).post(P(`/runs/${runId}/compute`)).set(auth(hr)).send(reason)).status).toBe(200);
    const lines = await db.query<{ component_code: string; amount: string; type: string }>(
      `SELECT l.component_code, l.amount::text, l.type FROM payslip_line l JOIN payslip ps ON ps.id = l.payslip_id
        WHERE ps.run_id = $1 AND ps.employee_id = $2 AND l.component_code IN ('EXP_REIMB', 'EXP_RECOVERY', 'TRAVEL_ADV') ORDER BY l.component_code`, [runId, DEMO.sita]);
    expect(lines.rows).toEqual(expect.arrayContaining([{ component_code: 'EXP_REIMB', amount: '2900.00', type: 'earning' }, { component_code: 'EXP_RECOVERY', amount: '4800.00', type: 'deduction' }]));
    expect(lines.rows).toHaveLength(2);
    // Not taxed: taxable pay is the taxable earnings plus the employer retirement share, without the reimbursement.
    const slip = await db.query<{ taxable: string; gross: string; employer_statutory: string }>(
      'SELECT taxable::text, gross::text, employer_statutory::text FROM payslip WHERE run_id = $1 AND employee_id = $2', [runId, DEMO.sita]);
    const s = slip.rows[0]!;
    expect(Number(s.taxable)).toBeCloseTo(Number(s.gross) - 2900 + Number(s.employer_statutory), 2);
    const held = await db.query(`SELECT count(*)::int AS n FROM expense_payout WHERE payroll_run_id = $1`, [runId]);
    expect(held.rows[0].n).toBe(3);
    // Cash payment is refused while a run being prepared holds the payout.
    const claim = await db.query<{ id: string }>(`SELECT id FROM expense_claim WHERE status = 'approved' LIMIT 1`);
    expect((await request(app).post(E(`/claims/${claim.rows[0]!.id}/pay-outside`)).set(auth(hr)).send({ note: 'cash' })).status).toBe(409);
  });
  it('locking marks payouts and claims paid; a later run does not pay them again', async () => {
    expect((await request(app).post(P(`/runs/${runId}/review`)).set(auth(hr))).status).toBe(200);
    expect((await request(app).post(P(`/runs/${runId}/approve`)).set(auth(hr2))).status).toBe(200);
    expect((await request(app).post(P(`/runs/${runId}/lock`)).set(auth(hr))).status).toBe(200);
    const p = await db.query<{ status: string }>(`SELECT DISTINCT status FROM expense_payout WHERE payroll_run_id = $1`, [runId]);
    expect(p.rows).toEqual([{ status: 'paid' }]);
    const claims = await db.query<{ status: string }>(`SELECT status FROM expense_claim WHERE title = 'Client visit'`);
    expect(claims.rows[0]!.status).toBe('paid');
    const pending = await request(app).get(E('/reports/pending-payment')).set(auth(hr));
    expect(pending.body.rows).toHaveLength(0);
    const run = await db.query<{ period_id: string }>('SELECT period_id FROM payroll_run WHERE id = $1', [runId]);
    const supp = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: run.rows[0]!.period_id, runType: 'supplementary' });
    expect(supp.status).toBe(201);
    const c = await request(app).post(P(`/runs/${supp.body.id}/compute`)).set(auth(hr)).send({ overrideReason: 'nothing pending to pay' });
    expect(c.status).toBe(200);
    expect(c.body.payslips).toBe(0);
  });
  it('a recovery larger than the pay is capped; the balance stays outstanding for the next run', async () => {
    const bs = adToBs(today);
    const next = bs.month === 12 ? { year: bs.year + 1, month: 1 } : { year: bs.year, month: bs.month + 1 };
    const period = await request(app).post(P('/periods')).set(auth(hr)).send({ bsYear: next.year, bsMonth: next.month });
    const run = await request(app).post(P('/runs')).set(auth(hr)).send({ periodId: period.body.period.id });
    await db.query(`INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note) VALUES ($1, $2, 'travel_settlement', uuidv7(), 'EXP_RECOVERY', 900000, 'test')`,
      [DEMO.companyId, DEMO.ram]);
    expect((await request(app).post(P(`/runs/${run.body.id}/compute`)).set(auth(hr)).send({ overrideReason: 'testing the recovery cap' })).status).toBe(200);
    await request(app).post(P(`/runs/${run.body.id}/review`)).set(auth(hr));
    await request(app).post(P(`/runs/${run.body.id}/approve`)).set(auth(hr2));
    expect((await request(app).post(P(`/runs/${run.body.id}/lock`)).set(auth(hr))).status).toBe(200);
    const rows = await db.query<{ status: string; amount: string; paid_amount: string | null; parent_payout_id: string | null }>(
      `SELECT status, amount::text, paid_amount::text, parent_payout_id FROM expense_payout WHERE employee_id = $1 AND component_code = 'EXP_RECOVERY' ORDER BY created_at`, [DEMO.ram]);
    expect(rows.rows).toHaveLength(2);
    const [paid, rest] = rows.rows as [typeof rows.rows[number], typeof rows.rows[number]];
    expect(paid.status).toBe('paid');
    expect(Number(paid.paid_amount)).toBeGreaterThan(0);
    expect(Number(paid.paid_amount)).toBeLessThan(900000);
    expect(rest).toMatchObject({ status: 'pending' });
    expect(Number(rest.amount) + Number(paid.paid_amount)).toBeCloseTo(900000, 2);
    expect(rest.parent_payout_id).not.toBeNull();
  });
  it('spend report groups approved claims and settlements; CSV is HR only', async () => {
    const r = await request(app).get(E('/reports/spend')).query({ from: daysAgo(30), to: today, groupBy: 'category' }).set(auth(hr));
    expect(r.status).toBe(200);
    const groups = Object.fromEntries(r.body.rows.map((x: { group: string; amount: string }) => [x.group, x.amount]));
    expect(groups).toMatchObject({ 'Local travel': '700.00', 'Fuel (own vehicle)': '200.00', Travel: '5200.00' });
    const csv = await request(app).get(E('/reports/spend')).query({ from: daysAgo(30), to: today, groupBy: 'month', format: 'csv' }).set(auth(hr));
    expect(csv.headers['content-type']).toContain('text/csv');
    expect((await request(app).get(E('/reports/spend')).query({ from: daysAgo(30), to: today }).set(auth(sita))).status).toBe(403);
  });
});
