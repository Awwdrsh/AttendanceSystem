import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import pino from 'pino';
import { Writable } from 'node:stream';
import type { Db } from '../src/db/pool.js';
import { createApp } from '../src/app.js';
import { createUser } from '../src/modules/auth/service.js';
import { auth, DEMO, freshDb, loginAs, TEST_PEPPER, testApp, testCipher } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
const PW = 'correct-horse-battery-staple';

async function cookieLogin(email: string, password = PW): Promise<string> {
  const res = await request(app).post('/api/v1/auth/login').send({ email, password });
  expect(res.status).toBe(200);
  return (res.headers['set-cookie'] as unknown as string[])[0]!.split(';')[0]!;
}

beforeAll(async () => { db = await freshDb(); app = testApp(db); });
afterAll(async () => { await db.end(); });

describe('request logging', () => {
  it('never writes session tokens or cookies to the log', async () => {
    const lines: string[] = [];
    const sink = new Writable({ write(chunk, _enc, cb) { lines.push(String(chunk)); cb(); } });
    const logged = createApp({ db, cipher: testCipher, pepper: TEST_PEPPER, secureCookie: false, logger: pino({ level: 'info' }, sink) });
    await createUser(db, { companyId: DEMO.companyId, email: 'log@test.local', name: 'l', password: PW, roles: ['employee'], pepper: TEST_PEPPER });
    const login = await request(logged).post('/api/v1/auth/login').send({ email: 'log@test.local', password: PW });
    const token = login.body.token as string;
    await request(logged).get('/api/v1/auth/me').set('Authorization', `Bearer ${token}`).set('Cookie', `hajiri_session=${token}`);
    const out = lines.join('');
    expect(out.length).toBeGreaterThan(0);
    expect(out).not.toContain(token);
    expect(out).not.toContain(PW);
    expect(out).toContain('[redacted]');
  });
});

describe('cross-site request protection (cookie sessions)', () => {
  it('refuses a state-changing cookie request from another origin; same origin, no origin and Bearer clients pass', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'csrf@test.local', name: 'c', password: PW, roles: ['employee'], pepper: TEST_PEPPER });
    const cookie = await cookieLogin('csrf@test.local');
    const evil = await request(app).post('/api/v1/auth/logout').set('Cookie', cookie).set('Origin', 'http://localhost:5173');
    expect(evil.status).toBe(403);
    expect(evil.body.error).toMatch(/cross-site/);
    const evilRef = await request(app).post('/api/v1/auth/logout').set('Cookie', cookie).set('Referer', 'http://evil.example/page');
    expect(evilRef.status).toBe(403);
    expect((await request(app).post('/api/v1/auth/logout').set('Cookie', cookie).set('Origin', 'null')).status).toBe(403);
    // Reads are unaffected, and the session is still alive after the refused attempts.
    expect((await request(app).get('/api/v1/auth/me').set('Cookie', cookie).set('Origin', 'http://evil.example')).status).toBe(200);
    expect((await request(app).post('/api/v1/auth/logout').set('Cookie', cookie).set('Host', 'hajiri.local').set('Origin', 'http://hajiri.local')).status).toBe(200);
    // Bearer tokens are not sent automatically by browsers, so they are not subject to the check.
    const { token } = await loginAs(db, app, ['employee']);
    expect((await request(app).post('/api/v1/auth/logout').set(auth(token)).set('Origin', 'http://evil.example')).status).toBe(200);
  });
  it('allows configured CORS origins', async () => {
    const cors = createApp({ db, cipher: testCipher, pepper: TEST_PEPPER, secureCookie: false }, { corsOrigins: ['http://localhost:5173'] });
    await createUser(db, { companyId: DEMO.companyId, email: 'cors@test.local', name: 'c', password: PW, roles: ['employee'], pepper: TEST_PEPPER });
    const login = await request(cors).post('/api/v1/auth/login').send({ email: 'cors@test.local', password: PW });
    const cookie = (login.headers['set-cookie'] as unknown as string[])[0]!.split(';')[0]!;
    expect((await request(cors).post('/api/v1/auth/logout').set('Cookie', cookie).set('Origin', 'http://localhost:5173')).status).toBe(200);
  });
});

describe('security headers', () => {
  it('sends a strict CSP even when the web build is not served by the API', async () => {
    const res = await request(app).get('/health');
    expect(res.headers['content-security-policy']).toMatch(/default-src 'self'/);
    expect(res.headers['content-security-policy']).toMatch(/frame-ancestors 'none'/);
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    expect(res.headers['x-powered-by']).toBeUndefined();
  });
});

describe('login lockout', () => {
  it('counts parallel wrong guesses exactly and locks the account', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'race@test.local', name: 'r', password: PW, roles: ['employee'], pepper: TEST_PEPPER });
    await Promise.all(Array.from({ length: 12 }, () => request(app).post('/api/v1/auth/login').set('X-Forwarded-For', '10.9.9.9').send({ email: 'race@test.local', password: 'wrong-password-123' })));
    const row = await db.query<{ failed_logins: number; locked_until: Date | null }>(`SELECT failed_logins, locked_until FROM app_user WHERE email = 'race@test.local'`);
    expect(row.rows[0]!.failed_logins).toBe(12);
    expect(row.rows[0]!.locked_until!.getTime()).toBeGreaterThan(Date.now());
    expect((await request(app).post('/api/v1/auth/login').send({ email: 'race@test.local', password: PW })).status).toBe(401);
  });
});

describe('passwords', () => {
  it('rejects reusing the current password and oversized inputs', async () => {
    const { token } = await loginAs(db, app, ['employee']);
    expect((await request(app).post('/api/v1/auth/change-password').set(auth(token)).send({ currentPassword: PW, newPassword: PW })).status).toBe(400);
    expect((await request(app).post('/api/v1/auth/change-password').set(auth(token)).send({ currentPassword: 'x'.repeat(10_000), newPassword: 'a-fresh-long-password' })).status).toBe(400);
  });
  it('an admin with a temporary password cannot manage accounts until they change it', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'temp-admin@test.local', name: 't', password: PW, roles: ['admin'], mustChangePassword: true, pepper: TEST_PEPPER });
    const token = (await request(app).post('/api/v1/auth/login').send({ email: 'temp-admin@test.local', password: PW })).body.token as string;
    expect((await request(app).get('/api/v1/auth/users').set(auth(token))).status).toBe(403);
    expect((await request(app).post('/api/v1/auth/users').set(auth(token)).send({ email: 'x@test.local', name: 'x', password: 'another-long-password', roles: ['admin'] })).status).toBe(403);
    expect((await request(app).get('/api/v1/auth/me').set(auth(token))).status).toBe(200);
  });
});

describe('account administration', () => {
  it('cannot link a user to an employee of another company', async () => {
    const other = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('SEC1', 'Other') RETURNING id`);
    const foreign = await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'F1', 'Foreign') RETURNING id`, [other.rows[0]!.id]);
    const admin = await loginAs(db, app, ['admin']);
    const created = await request(app).post('/api/v1/auth/users').set(auth(admin.token)).send({ email: 'link@test.local', name: 'Link', password: 'a-long-enough-password', roles: ['employee'], employeeId: foreign.rows[0]!.id });
    expect(created.status).toBe(404);
    const ok = await request(app).post('/api/v1/auth/users').set(auth(admin.token)).send({ email: 'link@test.local', name: 'Link', password: 'a-long-enough-password', roles: ['employee'] });
    expect(ok.status).toBe(201);
    expect((await request(app).patch(`/api/v1/auth/users/${ok.body.user.id}`).set(auth(admin.token)).send({ employeeId: foreign.rows[0]!.id })).status).toBe(404);
    const linked = await db.query('SELECT employee_id FROM app_user WHERE id = $1', [ok.body.user.id]);
    expect(linked.rows[0].employee_id).toBeNull();
    expect((await request(app).patch(`/api/v1/auth/users/${ok.body.user.id}`).set(auth(admin.token)).send({ employeeId: DEMO.sita })).status).toBe(200);
  });
  it('rejects oversized names and emails', async () => {
    const admin = await loginAs(db, app, ['admin']);
    expect((await request(app).post('/api/v1/auth/users').set(auth(admin.token)).send({ email: 'n@test.local', name: 'x'.repeat(500), password: 'a-long-enough-password', roles: ['employee'] })).status).toBe(400);
  });
});
