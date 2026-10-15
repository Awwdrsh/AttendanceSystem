import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { helpdeskDaily } from '../src/modules/helpdesk/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hrUser: string; let sita: string; let sitaUser: string; let ram: string; let ramUser: string; let it_: string; let itUser: string; let otherHr: string;
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;
let cats: Array<{ id: string; name: string; confidential: boolean }>;
const cat = (prefix: string) => cats.find((c) => c.name.startsWith(prefix))!.id;
const upload = async (token: string) => (await request(app).post('/api/v1/files').set(auth(token)).send({ purpose: 'helpdesk_attachment', filename: 'slip.pdf', dataUrl: PDF })).body.file.id as string;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  ({ token: hr, userId: hrUser } = await loginAs(db, app, ['hr']));
  ({ token: sita, userId: sitaUser } = await loginAs(db, app, ['employee'], DEMO.sita));
  ({ token: ram, userId: ramUser } = await loginAs(db, app, ['employee', 'manager'], DEMO.ram));
  ({ token: it_, userId: itUser } = await loginAs(db, app, ['employee']));
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERHD', 'Other Co') RETURNING id`);
  await createUser(db, { companyId: c.rows[0]!.id, email: 'hr@other-hd.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other-hd.local', password: 'correct-horse-battery-staple' })).body.token;
  cats = (await request(app).get('/api/v1/helpdesk/categories').set(auth(sita))).body.categories;
});
afterAll(async () => { await db.end(); });

describe('help desk', () => {
  let ticket: { id: string; number: number }; let grievance: string;

  it('seeds categories, with grievances marked confidential and owners hidden from employees', () => {
    expect(cats.length).toBe(6);
    expect(cats.find((c) => c.name === 'Grievance')!.confidential).toBe(true);
    expect((cats[0] as unknown as { owner_user_id: unknown }).owner_user_id).toBeNull();
  });

  it('an employee raises a ticket with an attachment; numbers are sequential with an SLA due time', async () => {
    const fileId = await upload(sita);
    const r = await request(app).post('/api/v1/helpdesk/tickets').set(auth(sita)).send({ categoryId: cat('Payroll'), subject: 'SSF missing on payslip', body: 'My Bhadra payslip has no SSF line.', priority: 'high', fileIds: [fileId] });
    expect(r.status).toBe(201);
    expect(r.body.number).toBe(1);
    expect(new Date(r.body.slaDueAt).getTime() - Date.now()).toBeGreaterThan(23 * 3_600_000);
    ticket = r.body;
    const r2 = await request(app).post('/api/v1/helpdesk/tickets').set(auth(ram)).send({ categoryId: cat('Leave'), subject: 'Balance looks wrong', body: 'Please check' });
    expect(r2.body.number).toBe(2);
    // Someone else's upload cannot be attached.
    const other = await upload(ram);
    expect((await request(app).post('/api/v1/helpdesk/tickets').set(auth(sita)).send({ categoryId: cat('Other'), subject: 'Steal', body: 'x', fileIds: [other] })).status).toBe(403);
    const hrN = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = $1 AND template = 'helpdesk.ticket.new' AND channel = 'in_app'`, [hrUser]);
    expect(hrN.rows[0].n).toBeGreaterThanOrEqual(2);
  });

  it('only the requester and the help desk team can see a ticket', async () => {
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(sita))).status).toBe(200);
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(ram))).status).toBe(404);
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(otherHr))).status).toBe(404);
    expect((await request(app).get('/api/v1/helpdesk/tickets').set(auth(ram))).body.tickets).toEqual([]);
    expect((await request(app).get('/api/v1/helpdesk/tickets').set(auth(hr))).body.tickets).toHaveLength(2);
    const mine = await request(app).get('/api/v1/helpdesk/tickets/mine').set(auth(sita));
    expect(mine.body.tickets).toHaveLength(1);
  });

  it('internal notes and their files stay inside the team; a public reply counts as first response', async () => {
    const noteFile = await upload(hr);
    const note = await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/messages`).set(auth(hr)).send({ body: 'Check the SSF registration date.', internal: true, fileIds: [noteFile] });
    expect(note.status).toBe(201);
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/messages`).set(auth(sita)).send({ body: 'sneaky', internal: true })).status).toBe(403);
    const asSita = await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(sita));
    expect(asSita.body.messages).toHaveLength(1);
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}/files/${noteFile}`).set(auth(sita))).status).toBe(404);
    const own = asSita.body.messages[0].file_ids[0];
    const dl = await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}/files/${own}`).set(auth(sita));
    expect(dl.status).toBe(200);
    expect(dl.headers['content-type']).toContain('application/pdf');
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}/files/${own}`).set(auth(ram))).status).toBe(404);
    await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/messages`).set(auth(hr)).send({ body: 'Fixed in the supplementary run.' });
    const t = (await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(hr))).body.ticket;
    expect(t.status).toBe('in_progress');
    expect(t.first_response_at).toBeTruthy();
  });

  it('only HR reassigns; the assignee then works the ticket', async () => {
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(sita)).send({ status: 'resolved' })).status).toBe(403);
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(hr)).send({ assigneeUserId: sitaUser })).status).toBe(400);
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(hr)).send({ assigneeUserId: itUser })).status).toBe(200);
    const q = await request(app).get('/api/v1/helpdesk/tickets?assignee=me').set(auth(it_));
    expect(q.body.tickets.map((x: { id: string }) => x.id)).toEqual([ticket.id]);
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(it_)).send({ assigneeUserId: ramUser })).status).toBe(403);
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(it_)).send({ status: 'resolved' })).status).toBe(200);
  });

  it('the requester rates once, can reopen within the window, and close', async () => {
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/rate`).set(auth(hr)).send({ rating: 5 })).status).toBe(403);
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/rate`).set(auth(sita)).send({ rating: 2, comment: 'Slow' })).status).toBe(200);
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/rate`).set(auth(sita)).send({ rating: 5 })).status).toBe(409);
    const d = await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(sita));
    expect(d.body.canReopen).toBe(true);
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/reopen`).set(auth(sita))).status).toBe(200);
    const t = (await request(app).get(`/api/v1/helpdesk/tickets/${ticket.id}`).set(auth(sita))).body.ticket;
    expect(t.status).toBe('in_progress');
    expect(t.rating).toBeNull();
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/close`).set(auth(sita))).status).toBe(200);
    expect((await request(app).post(`/api/v1/helpdesk/tickets/${ticket.id}/messages`).set(auth(sita)).send({ body: 'one more thing' })).status).toBe(409);
  });

  it('grievances are never visible to managers or non-HR owners, and only HR can be assigned', async () => {
    const r = await request(app).post('/api/v1/helpdesk/tickets').set(auth(sita)).send({ categoryId: cat('Grievance'), subject: 'Concern about my manager', body: 'Confidential matter' });
    expect(r.status).toBe(201);
    grievance = r.body.id;
    expect((await request(app).patch(`/api/v1/helpdesk/tickets/${grievance}`).set(auth(hr)).send({ assigneeUserId: ramUser })).status).toBe(400);
    expect((await request(app).put(`/api/v1/helpdesk/categories/${cat('Grievance')}`).set(auth(hr)).send({ name: 'Grievance', confidential: true, ownerUserId: ramUser })).status).toBe(400);
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${grievance}`).set(auth(ram))).status).toBe(404);
    const n = await db.query(`SELECT body FROM notification_outbox WHERE template = 'helpdesk.ticket.new' AND payload->>'ticketId' = $1`, [grievance]);
    expect(n.rows.every((x: { body: string }) => !x.body.includes('manager'))).toBe(true);
  });

  it('a non-HR category owner sees tickets in their category only', async () => {
    expect((await request(app).put(`/api/v1/helpdesk/categories/${cat('IT')}`).set(auth(hr)).send({ name: 'IT & equipment', ownerUserId: itUser })).status).toBe(200);
    const r = await request(app).post('/api/v1/helpdesk/tickets').set(auth(sita)).send({ categoryId: cat('IT'), subject: 'Laptop fan noisy', body: 'Very loud' });
    const q = await request(app).get('/api/v1/helpdesk/tickets?status=active').set(auth(it_));
    expect(q.body.tickets.map((x: { id: string }) => x.id)).toContain(r.body.id);
    expect(q.body.tickets.map((x: { id: string }) => x.id)).not.toContain(grievance);
    expect((await request(app).get(`/api/v1/helpdesk/tickets/${r.body.id}`).set(auth(it_))).body.role).toBe('agent');
  });

  it('the daily job escalates overdue tickets once and closes old resolved ones', async () => {
    await db.query(`UPDATE helpdesk_ticket SET sla_due_at = now() - interval '1 hour' WHERE id = $1`, [grievance]);
    const a = await helpdeskDaily(db);
    const b = await helpdeskDaily(db);
    expect(a.ticketsBreached).toBeGreaterThanOrEqual(1);
    expect(b.ticketsBreached).toBe(0);
    const overdue = await request(app).get('/api/v1/helpdesk/tickets?breached=1').set(auth(hr));
    expect(overdue.body.tickets.map((x: { id: string }) => x.id)).toContain(grievance);
    await request(app).patch(`/api/v1/helpdesk/tickets/${grievance}`).set(auth(hr)).send({ status: 'resolved' });
    const c = await helpdeskDaily(db, new Date(Date.now() + 8 * 86_400_000));
    expect(c.ticketsClosed).toBeGreaterThanOrEqual(1);
    const stats = await request(app).get('/api/v1/helpdesk/stats').set(auth(hr));
    expect(stats.status).toBe(200);
    expect(stats.body.stats.breached_90d).toBeGreaterThanOrEqual(1);
    expect((await request(app).get('/api/v1/helpdesk/stats').set(auth(sita))).status).toBe(403);
  });

  it('validates input and settings', async () => {
    expect((await request(app).post('/api/v1/helpdesk/tickets').set(auth(sita)).send({ categoryId: cat('Other'), subject: 'x', body: '' })).status).toBe(400);
    expect((await request(app).put('/api/v1/helpdesk/settings').set(auth(sita)).send({})).status).toBe(403);
    const s = await request(app).put('/api/v1/helpdesk/settings').set(auth(hr)).send({ slaHours: { low: 96, normal: 48, high: 12, urgent: 4 }, autoCloseDays: 5, reopenDays: 3 });
    expect(s.status).toBe(200);
    expect((await request(app).get('/api/v1/helpdesk/settings').set(auth(sita))).body.policy.slaHours.urgent).toBe(4);
  });
});
