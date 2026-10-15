import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { documentsDailyJob } from '../src/modules/documents/routes.js';
import { renderBody, unknownPlaceholders } from '../src/modules/documents/letters.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** Documents & letters: employee documents, expiry alerts, letter templates and issuing, letter requests, policies. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hr2: string; let sita: string; let ram: string; let peer: string; let foreignHr: string;
let peerId: string; let hrUserId: string;
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;
const addDays = (d: string, n: number) => new Date(new Date(`${d}T00:00:00Z`).getTime() + n * 86_400_000).toISOString().slice(0, 10);

async function upload(tok: string, purpose = 'employee_document'): Promise<string> {
  const r = await request(app).post('/api/v1/files').set(auth(tok)).send({ purpose, filename: 'doc.pdf', dataUrl: PDF });
  expect(r.status).toBe(201);
  return r.body.file.id as string;
}
async function category(tok: string, code: string): Promise<string> {
  const r = await request(app).get('/api/v1/documents/categories').set(auth(tok));
  return r.body.categories.find((c: { code: string }) => c.code === code).id as string;
}
async function template(code: string): Promise<string> {
  const r = await request(app).get('/api/v1/documents/templates').set(auth(hr));
  return r.body.templates.find((t: { code: string }) => t.code === code).id as string;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  const h = await loginAs(db, app, ['hr']); hr = h.token; hrUserId = h.userId;
  hr2 = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  peerId = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E099', 'Peer Person') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2020-01-01', NULL), $2, 'regular')`, [peerId, DEMO.branchId]);
  peer = (await loginAs(db, app, ['employee'], peerId)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`UPDATE employee SET designation = 'Software Engineer', join_date = '2024-04-14', email = 'sita@demo.test' WHERE id = $1`, [DEMO.sita]);
  const c = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`)).rows[0]!.id;
  await createUser(db, { companyId: c, email: 'hr@other.docs', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.docs', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('letter engine', () => {
  it('escapes values and template text, keeps bold and lists', () => {
    const out = renderBody('Dear **{{employee.name}}**,\n\n- one <b>\n- {{designation}}', { 'employee.name': '<script>x</script>', designation: 'a **b**' });
    expect(out.html).toContain('<b>&lt;script&gt;x&lt;/script&gt;</b>');
    expect(out.html).toContain('<li>one &lt;b&gt;</li>');
    expect(out.html).toContain('a &#42;&#42;b&#42;&#42;');
    expect(out.html).not.toContain('<script>');
  });
  it('flags unknown placeholders and stray braces', () => {
    expect(unknownPlaceholders('Hi {{employee.name}}')).toEqual([]);
    expect(unknownPlaceholders('Hi {{employee.salary}}')).toEqual(['employee.salary']);
    expect(unknownPlaceholders('Hi {{employee.name')).toHaveLength(1);
  });
  it('reports missing values', () => {
    expect(renderBody('{{designation}} {{grade}}', { designation: 'X' }).missing).toEqual(['grade']);
  });
});

describe('employee documents', () => {
  let docId: string;
  it('seeds categories for the company and hides other companies', async () => {
    const r = await request(app).get('/api/v1/documents/categories').set(auth(sita));
    expect(r.status).toBe(200);
    expect(r.body.categories.map((c: { code: string }) => c.code)).toEqual(expect.arrayContaining(['contract', 'citizenship', 'passport', 'other']));
  });

  it('lets an employee upload their own document, pending HR verification', async () => {
    const fileId = await upload(sita);
    const r = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'citizenship'), title: 'Citizenship', fileId });
    expect(r.status).toBe(201);
    docId = r.body.id;
    const mine = await request(app).get('/api/v1/documents/mine').set(auth(sita));
    expect(mine.body.documents).toHaveLength(1);
    expect(mine.body.documents[0]).toMatchObject({ status: 'pending', title: 'Citizenship', version: 1 });
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'document.uploaded'`);
    expect(n.rows[0].n).toBeGreaterThan(0);
  });

  it('refuses categories employees may not upload, missing expiry dates and other employees', async () => {
    const contract = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'contract'), title: 'Contract', fileId: await upload(sita) });
    expect(contract.status).toBe(403);
    const passport = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'passport'), title: 'Passport', fileId: await upload(sita) });
    expect(passport.status).toBe(400);
    const other = await request(app).post(`/api/v1/documents/employee/${peerId}`).set(auth(sita)).send({ categoryId: await category(sita, 'other'), title: 'X', fileId: await upload(sita) });
    expect(other.status).toBe(403);
    // A file uploaded by someone else cannot be attached.
    const stolen = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'other'), title: 'X', fileId: await upload(peer) });
    expect(stolen.status).toBe(403);
  });

  it('serves files to the employee and HR (audited), never to managers, peers or other companies', async () => {
    expect((await request(app).get(`/api/v1/documents/${docId}/file`).set(auth(sita))).status).toBe(200);
    const h = await request(app).get(`/api/v1/documents/${docId}/file`).set(auth(hr));
    expect(h.status).toBe(200);
    expect(h.headers['content-type']).toContain('application/pdf');
    expect((await request(app).get(`/api/v1/documents/${docId}/file`).set(auth(ram))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/${docId}/file`).set(auth(peer))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/${docId}/file`).set(auth(foreignHr))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(ram))).status).toBe(403);
    expect((await request(app).get(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(foreignHr))).status).toBe(404);
    const views = await db.query(`SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_document' AND row_id = $1`, [docId]);
    expect(views.rows[0].n).toBe(1);
  });

  it('HR verifies or rejects with a reason; the employee cannot verify', async () => {
    expect((await request(app).post(`/api/v1/documents/${docId}/verify`).set(auth(sita)).send({ decision: 'verify' })).status).toBe(403);
    expect((await request(app).post(`/api/v1/documents/${docId}/verify`).set(auth(hr)).send({ decision: 'reject' })).status).toBe(400);
    const q = await request(app).get('/api/v1/documents/queue').set(auth(hr));
    expect(q.body.pending.map((d: { id: string }) => d.id)).toContain(docId);
    expect((await request(app).post(`/api/v1/documents/${docId}/verify`).set(auth(hr)).send({ decision: 'verify' })).status).toBe(200);
    expect((await request(app).post(`/api/v1/documents/${docId}/verify`).set(auth(hr)).send({ decision: 'verify' })).status).toBe(409);
    // Verified documents can no longer be deleted by the employee.
    expect((await request(app).delete(`/api/v1/documents/${docId}`).set(auth(sita))).status).toBe(403);
  });

  it('keeps versions when a document is replaced, and restores the previous one when the new one is deleted', async () => {
    const r = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'citizenship'), title: 'Citizenship', fileId: await upload(sita), replacesId: docId });
    expect(r.status).toBe(201);
    const mine = await request(app).get('/api/v1/documents/mine').set(auth(sita));
    expect(mine.body.documents).toHaveLength(1);
    expect(mine.body.documents[0].version).toBe(2);
    const hist = await request(app).get(`/api/v1/documents/employee/${DEMO.sita}?history=1`).set(auth(hr));
    expect(hist.body.documents.map((d: { version: number }) => d.version)).toEqual([2, 1]);
    // Replacing an old version is refused.
    expect((await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(sita)).send({ categoryId: await category(sita, 'citizenship'), title: 'C', fileId: await upload(sita), replacesId: docId })).status).toBe(409);
    expect((await request(app).delete(`/api/v1/documents/${r.body.id}`).set(auth(sita))).status).toBe(200);
    const after = await request(app).get('/api/v1/documents/mine').set(auth(sita));
    expect(after.body.documents[0]).toMatchObject({ id: docId, version: 1 });
    const wiped = await db.query('SELECT f.data_enc FROM employee_document d JOIN stored_file f ON f.id = d.file_id WHERE d.id = $1', [r.body.id]);
    expect(wiped.rows[0].data_enc).toBeNull();
  });

  it('HR-only documents stay hidden from the employee', async () => {
    const r = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(hr)).send({ categoryId: await category(hr, 'contract'), title: 'Signed contract', fileId: await upload(hr), visibleToEmployee: false });
    expect(r.status).toBe(201);
    const mine = await request(app).get('/api/v1/documents/mine').set(auth(sita));
    expect(mine.body.documents.map((d: { id: string }) => d.id)).not.toContain(r.body.id);
    expect((await request(app).get(`/api/v1/documents/${r.body.id}/file`).set(auth(sita))).status).toBe(404);
  });

  it('sends expiry alerts once per threshold, to the employee and a summary to HR', async () => {
    const today = nptToday();
    const r = await request(app).post(`/api/v1/documents/employee/${DEMO.sita}`).set(auth(hr)).send({ categoryId: await category(hr, 'passport'), title: 'Passport', fileId: await upload(hr), expiryDate: addDays(today, 20) });
    expect(r.status).toBe(201);
    const first = await documentsDailyJob(db);
    expect(first.expiryAlerts).toBe(1);
    expect((await documentsDailyJob(db)).expiryAlerts).toBe(0);
    // Twelve days on, it is within 7 days: one more alert.
    expect((await documentsDailyJob(db, new Date(Date.now() + 14 * 86_400_000))).expiryAlerts).toBe(1);
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'document.expiring' AND channel = 'in_app'`);
    expect(n.rows[0].n).toBe(2);
    const exp = await request(app).get('/api/v1/documents/queue?days=30').set(auth(hr));
    expect(exp.body.expiring.map((d: { id: string }) => d.id)).toContain(r.body.id);
    expect((await request(app).get('/api/v1/documents/queue').set(auth(ram))).status).toBe(403);
  });
});

describe('letters', () => {
  it('rejects templates with unknown placeholders and saves valid ones', async () => {
    const bad = await request(app).post('/api/v1/documents/templates').set(auth(hr)).send({ code: 'bad_one', name: 'Bad', subject: 'S', body: 'Hello {{employee.nmae}}' });
    expect(bad.status).toBe(400);
    expect(bad.body.details.unknown).toEqual(['employee.nmae']);
    const ok = await request(app).post('/api/v1/documents/templates').set(auth(hr)).send({ code: 'welcome', name: 'Welcome note', subject: 'Welcome {{employee.first_name}}', body: 'Welcome to {{company.name}}.' });
    expect(ok.status).toBe(201);
    expect((await request(app).post('/api/v1/documents/templates').set(auth(sita)).send({ code: 'x_y', name: 'X', subject: 'S', body: 'B' })).status).toBe(403);
  });

  it('previews with missing values marked and a draft watermark', async () => {
    const r = await request(app).post('/api/v1/documents/templates/preview').set(auth(hr)).send({ employeeId: DEMO.sita, templateId: await template('appointment') });
    expect(r.status).toBe(200);
    expect(r.body.html).toContain('Sita');
    expect(r.body.html).toContain('DRAFT');
    expect(r.body.missing).toEqual(expect.arrayContaining(['department', 'salary.gross']));
    expect((await request(app).post('/api/v1/documents/templates/preview').set(auth(foreignHr)).send({ employeeId: DEMO.sita, subject: 'S', body: 'B' })).status).toBe(404);
  });

  it('refuses to issue with missing values unless allowed, then numbers letters per BS year', async () => {
    const tpl = await template('confirmation');
    const missing = await request(app).post('/api/v1/documents/letters').set(auth(hr)).send({ templateId: await template('promotion'), employeeId: DEMO.sita });
    expect(missing.status).toBe(400);
    expect(missing.body.details.code).toBe('missing_values');
    await request(app).put('/api/v1/documents/letter-settings').set(auth(hr)).send({ signatoryName: 'Gita HR', signatoryTitle: 'HR Manager', refPrefix: 'CV/HR' });
    const one = await request(app).post('/api/v1/documents/letters').set(auth(hr)).send({ templateId: tpl, employeeId: DEMO.sita });
    expect(one.status).toBe(201);
    expect(one.body.refNo).toMatch(/^CV\/HR\/20\d\d\/0001$/);
    const two = await request(app).post('/api/v1/documents/letters').set(auth(hr)).send({ templateId: await template('welcome'), employeeId: DEMO.sita });
    expect(two.body.refNo).toMatch(/\/0002$/);
    const html = await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(sita));
    expect(html.status).toBe(200);
    expect(html.text).toContain(one.body.refNo);
    expect(html.text).toContain('Gita HR');
    expect(html.text).not.toContain('DRAFT');
    expect((await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(peer))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(ram))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(foreignHr))).status).toBe(404);
    // Issued letters cannot be edited in the database either.
    await expect(db.query(`UPDATE issued_letter SET html = 'x' WHERE id = $1`, [one.body.id])).rejects.toThrow(/cannot be changed/);
    // Voiding hides it from the employee and marks it for HR.
    expect((await request(app).post(`/api/v1/documents/letters/${one.body.id}/void`).set(auth(hr)).send({ reason: 'wrong date' })).status).toBe(200);
    expect((await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(sita))).status).toBe(404);
    expect((await request(app).get(`/api/v1/documents/letters/${one.body.id}/html`).set(auth(hr))).text).toContain('VOID');
    const mine = await request(app).get('/api/v1/documents/letters').set(auth(sita));
    expect(mine.body.letters.map((l: { id: string }) => l.id)).toEqual([two.body.id]);
    expect((await request(app).get(`/api/v1/documents/letters?employeeId=${peerId}`).set(auth(sita))).status).toBe(403);
  });

  it('runs a letter request through HR: inbox, issue completes it and notifies', async () => {
    const reqs = await request(app).get('/api/v1/documents/templates').set(auth(sita));
    expect(reqs.body.templates.map((t: { code: string }) => t.code)).toEqual(expect.arrayContaining(['salary_certificate', 'experience']));
    expect(reqs.body.templates[0]).not.toHaveProperty('body');
    const toWhom = reqs.body.templates.find((t: { code: string }) => t.code === 'to_whom_it_may_concern').id;
    expect((await request(app).post('/api/v1/documents/letter-requests').set(auth(sita)).send({ templateId: await template('appointment'), purpose: 'bank loan' })).status).toBe(400);
    const r = await request(app).post('/api/v1/documents/letter-requests').set(auth(sita)).send({ templateId: toWhom, purpose: 'visa application' });
    expect(r.status).toBe(201);
    expect((await request(app).post('/api/v1/documents/letter-requests').set(auth(sita)).send({ templateId: toWhom, purpose: 'again' })).status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === r.body.id);
    expect(item.summary).toMatchObject({ letter: 'To whom it may concern', purpose: 'visa application' });
    const ramInbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(ramInbox.body.items.find((i: { objectId: string }) => i.objectId === r.body.id)).toBeUndefined();
    const issued = await request(app).post('/api/v1/documents/letters').set(auth(hr)).send({ templateId: toWhom, employeeId: DEMO.sita, requestId: r.body.id, allowMissing: true });
    expect(issued.status).toBe(201);
    const list = await request(app).get('/api/v1/documents/letter-requests').set(auth(sita));
    expect(list.body.requests.find((x: { id: string }) => x.id === r.body.id)).toMatchObject({ status: 'issued', issued_letter_id: issued.body.id });
    const html = await request(app).get(`/api/v1/documents/letters/${issued.body.id}/html`).set(auth(sita));
    expect(html.text).toContain('visa application');
    const wf = await db.query(`SELECT state FROM workflow_instance WHERE object_id = $1`, [r.body.id]);
    expect(wf.rows[0].state).toBe('approved');
    const note = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'letter.issued' AND employee_id = $1`, [DEMO.sita]);
    expect(note.rows[0].n).toBeGreaterThan(0);
  });

  it('a rejected request notifies the employee; cancel works only for open requests', async () => {
    const tpl = await template('salary_certificate');
    const r = await request(app).post('/api/v1/documents/letter-requests').set(auth(sita)).send({ templateId: tpl, purpose: 'bank loan', addressedTo: 'The Manager, ABC Bank' });
    const wf = (await db.query<{ id: string }>(`SELECT id FROM workflow_instance WHERE object_id = $1`, [r.body.id])).rows[0]!.id;
    const rej = await request(app).post(`/api/v1/workflow/${wf}/reject`).set(auth(hr)).send({ reason: 'no salary set yet' });
    expect([200, 201]).toContain(rej.status);
    const list = await request(app).get('/api/v1/documents/letter-requests').set(auth(sita));
    expect(list.body.requests.find((x: { id: string }) => x.id === r.body.id).status).toBe('rejected');
    expect((await request(app).post(`/api/v1/documents/letter-requests/${r.body.id}/cancel`).set(auth(sita))).status).toBe(409);
    const r2 = await request(app).post('/api/v1/documents/letter-requests').set(auth(sita)).send({ templateId: tpl, purpose: 'bank loan' });
    expect((await request(app).post(`/api/v1/documents/letter-requests/${r2.body.id}/cancel`).set(auth(peer))).status).toBe(404);
    expect((await request(app).post(`/api/v1/documents/letter-requests/${r2.body.id}/cancel`).set(auth(sita))).status).toBe(200);
  });
});

describe('company policies', () => {
  let policyId: string;
  it('publishes to an audience, notifies, and needs a company_document upload', async () => {
    const bad = await request(app).post('/api/v1/documents/policies').set(auth(hr)).send({ title: 'Handbook', fileId: await upload(hr, 'expense_receipt') });
    expect(bad.status).toBe(400);
    const r = await request(app).post('/api/v1/documents/policies').set(auth(hr)).send({ title: 'Employee handbook', description: 'Read it', fileId: await upload(hr, 'company_document') });
    expect(r.status).toBe(201);
    policyId = r.body.id;
    expect((await request(app).post('/api/v1/documents/policies').set(auth(sita)).send({ title: 'X', fileId: await upload(sita, 'company_document') })).status).toBe(403);
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'policy.published' AND channel = 'in_app'`);
    expect(n.rows[0].n).toBeGreaterThanOrEqual(3);
  });

  it('branch audiences only reach that branch', async () => {
    const b2 = (await db.query<{ id: string }>(`INSERT INTO branch (company_id, code, name_en) VALUES ($1, 'PKR', 'Pokhara') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    const r = await request(app).post('/api/v1/documents/policies').set(auth(hr)).send({ title: 'Pokhara rules', fileId: await upload(hr, 'company_document'), audience: 'branch', audienceIds: [b2] });
    expect(r.status).toBe(201);
    const mine = await request(app).get('/api/v1/documents/policies').set(auth(sita));
    expect(mine.body.policies.map((p: { id: string }) => p.id)).toEqual([policyId]);
    expect((await request(app).get(`/api/v1/documents/policies/${r.body.id}/file`).set(auth(sita))).status).toBe(404);
    expect((await request(app).post(`/api/v1/documents/policies/${r.body.id}/acknowledge`).set(auth(sita))).status).toBe(404);
    expect((await request(app).post('/api/v1/documents/policies').set(auth(hr)).send({ title: 'x', fileId: await upload(hr, 'company_document'), audience: 'branch', audienceIds: ['00000000-0000-7000-8000-00000000ffff'] })).status).toBe(400);
  });

  it('employees read and acknowledge; HR sees who has and reminds the rest', async () => {
    expect((await request(app).get(`/api/v1/documents/policies/${policyId}/file`).set(auth(sita))).status).toBe(200);
    expect((await request(app).post(`/api/v1/documents/policies/${policyId}/acknowledge`).set(auth(sita))).status).toBe(200);
    expect((await request(app).post(`/api/v1/documents/policies/${policyId}/acknowledge`).set(auth(sita))).status).toBe(200);
    const acks = await request(app).get(`/api/v1/documents/policies/${policyId}/acks`).set(auth(hr));
    const sitaRow = acks.body.employees.find((e: { employee_id: string }) => e.employee_id === DEMO.sita);
    expect(sitaRow.acknowledged_at).not.toBeNull();
    expect((await request(app).get(`/api/v1/documents/policies/${policyId}/acks`).set(auth(sita))).status).toBe(403);
    const list = await request(app).get('/api/v1/documents/policies').set(auth(hr));
    const p = list.body.policies.find((x: { id: string }) => x.id === policyId);
    expect(p.acks).toBe(1);
    expect(p.audience_size).toBeGreaterThanOrEqual(3);
    const remind = await request(app).post(`/api/v1/documents/policies/${policyId}/remind`).set(auth(hr));
    expect(remind.body.sent).toBe(2);
    expect((await request(app).get(`/api/v1/documents/policies/${policyId}/file`).set(auth(foreignHr))).status).toBe(404);
  });

  it('nudges weekly from the daily job and stops once acknowledged or archived', async () => {
    const later = new Date(Date.now() + 9 * 86_400_000);
    const first = await documentsDailyJob(db, later);
    expect(first.policyNudges).toBe(2);
    expect((await documentsDailyJob(db, later)).policyNudges).toBe(0);
    await request(app).post(`/api/v1/documents/policies/${policyId}/acknowledge`).set(auth(ram));
    expect((await documentsDailyJob(db, new Date(later.getTime() + 8 * 86_400_000))).policyNudges).toBe(1);
    expect((await request(app).patch(`/api/v1/documents/policies/${policyId}`).set(auth(hr)).send({ archived: true })).status).toBe(200);
    expect((await documentsDailyJob(db, new Date(later.getTime() + 30 * 86_400_000))).policyNudges).toBe(0);
    const mine = await request(app).get('/api/v1/documents/policies').set(auth(peer));
    expect(mine.body.policies).toHaveLength(0);
    void hr2; void hrUserId;
  });
});
