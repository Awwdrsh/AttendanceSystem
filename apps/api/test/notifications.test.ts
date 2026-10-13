import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { dailyDigest, notify, payslipPublishedNotification } from '../src/modules/notifications/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let tok: string; let userId: string; let mgr: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  ({ token: tok, userId } = await loginAs(db, app, ['employee'], DEMO.sita));
  mgr = (await loginAs(db, app, ['manager'])).userId;
});
afterAll(async () => { await db.end(); });

describe('notifications', () => {
  it('lists own in-app rows with unread count, marks read, read-all', async () => {
    await notify(db, { companyId: DEMO.companyId, userId, template: 'leave.decided', title: 'Leave approved', body: 'Your leave was approved.' });
    await notify(db, { companyId: DEMO.companyId, userId, template: 'x', title: 'Second', body: 'b', channels: ['email'] });
    await notify(db, { companyId: DEMO.companyId, userId: mgr, template: 'x', title: 'Not mine', body: 'b' });
    const r = await request(app).get('/api/v1/notifications').set(auth(tok));
    expect(r.status).toBe(200);
    expect(r.body.notifications.map((n: { title: string }) => n.title)).toEqual(['Second', 'Leave approved']);
    expect(r.body.unread).toBe(2);
    const one = await request(app).post(`/api/v1/notifications/${r.body.notifications[0].id}/read`).set(auth(tok));
    expect(one.status).toBe(200);
    expect((await request(app).get('/api/v1/notifications').set(auth(tok)).query({ unreadOnly: 'true' })).body.notifications).toHaveLength(1);
    expect((await request(app).post('/api/v1/notifications/read-all').set(auth(tok))).body.marked).toBe(1);
    expect((await request(app).get('/api/v1/notifications').set(auth(tok))).body.unread).toBe(0);
  });
  it('cannot mark another user\'s notification', async () => {
    const other = await db.query<{ id: string }>(`SELECT id FROM notification_outbox WHERE user_id = $1 LIMIT 1`, [mgr]);
    expect((await request(app).post(`/api/v1/notifications/${other.rows[0]!.id}/read`).set(auth(tok))).status).toBe(404);
  });
  it('payslip notification carries no amounts and the digest reaches managers', async () => {
    expect(payslipPublishedNotification('Bhadra 2083').body).not.toMatch(/NPR|Rs\.|\d{1,3}(,\d{3})+|\d{5,}/);
    const d = await dailyDigest(db, DEMO.companyId, '2026-09-21');
    expect(d).toEqual({ managers: 1, missing: 2 });
    const n = await db.query(`SELECT title FROM notification_outbox WHERE template = 'attendance.digest' AND user_id = $1`, [mgr]);
    expect(n.rows[0].title).toBe('2 employees have not punched today');
    expect(await dailyDigest(db, DEMO.companyId, '2026-09-26')).toEqual({ managers: 0, missing: 0 }); // Saturday
  });
});
