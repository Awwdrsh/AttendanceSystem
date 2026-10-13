import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';
import { createUser } from '../src/modules/auth/service.js';
import { hashPassword, verifyPassword, FieldCipher } from '../src/crypto.js';

let db: Db; let app: ReturnType<typeof testApp>;
beforeAll(async () => { db = await freshDb(); app = testApp(db); });
afterAll(async () => { await db.end(); });

describe('auth', () => {
  it('rejects anonymous API calls and accepts a session', async () => {
    expect((await request(app).get('/api/v1/employees')).status).toBe(401);
    const { token } = await loginAs(db, app, ['hr']);
    const me = await request(app).get('/api/v1/auth/me').set(auth(token));
    expect(me.body.user.roles).toEqual(['hr']);
    expect((await request(app).get('/api/v1/employees').set(auth(token))).status).toBe(200);
  });

  it('sets an httpOnly cookie on login and clears it on logout', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'cookie@test.local', name: 'c', password: 'correct-horse-battery-staple', roles: ['employee'], pepper: TEST_PEPPER });
    const res = await request(app).post('/api/v1/auth/login').send({ email: 'cookie@test.local', password: 'correct-horse-battery-staple' });
    const cookie = res.headers['set-cookie']?.[0] ?? '';
    expect(cookie).toMatch(/hajiri_session=.*HttpOnly/);
    const me = await request(app).get('/api/v1/auth/me').set('Cookie', cookie.split(';')[0]!);
    expect(me.status).toBe(200);
    await request(app).post('/api/v1/auth/logout').set('Cookie', cookie.split(';')[0]!);
    expect((await request(app).get('/api/v1/auth/me').set('Cookie', cookie.split(';')[0]!)).status).toBe(401);
  });

  it('wrong password, unknown user and disabled user all fail the same way; lockout after 10 failures', async () => {
    await createUser(db, { companyId: DEMO.companyId, email: 'lock@test.local', name: 'l', password: 'correct-horse-battery-staple', roles: ['employee'], pepper: TEST_PEPPER });
    expect((await request(app).post('/api/v1/auth/login').send({ email: 'nobody@test.local', password: 'x'.repeat(12) })).status).toBe(401);
    for (let i = 0; i < 10; i++) await request(app).post('/api/v1/auth/login').send({ email: 'lock@test.local', password: 'wrong-password-123' });
    const locked = await request(app).post('/api/v1/auth/login').send({ email: 'lock@test.local', password: 'correct-horse-battery-staple' });
    expect(locked.status).toBe(401);
    expect(locked.body.error).toMatch(/locked/);
  });

  it('enforces roles: employee cannot list employees or read others; can read self', async () => {
    const { token } = await loginAs(db, app, ['employee'], DEMO.sita);
    expect((await request(app).get('/api/v1/employees').set(auth(token))).status).toBe(403);
    expect((await request(app).get(`/api/v1/employees/${DEMO.ram}`).set(auth(token))).status).toBe(403);
    expect((await request(app).get(`/api/v1/employees/${DEMO.sita}`).set(auth(token))).status).toBe(200);
    expect((await request(app).post('/api/v1/attendance/recompute').set(auth(token)).send({ employeeId: DEMO.sita, localDate: '2026-09-21' })).status).toBe(403);
  });

  it('password change revokes sessions and enforces the policy', async () => {
    const { token } = await loginAs(db, app, ['employee'], null, 'pw@test.local');
    const weak = await request(app).post('/api/v1/auth/change-password').set(auth(token)).send({ currentPassword: 'correct-horse-battery-staple', newPassword: 'short' });
    expect(weak.status).toBe(400);
    const ok = await request(app).post('/api/v1/auth/change-password').set(auth(token)).send({ currentPassword: 'correct-horse-battery-staple', newPassword: 'another-long-passphrase-42' });
    expect(ok.status).toBe(200);
    expect((await request(app).get('/api/v1/auth/me').set(auth(token))).status).toBe(401);
    expect((await request(app).post('/api/v1/auth/login').send({ email: 'pw@test.local', password: 'another-long-passphrase-42' })).status).toBe(200);
  });

  it('admin can create users; non-admin cannot', async () => {
    const { token: adminTok } = await loginAs(db, app, ['admin']);
    const { token: hrTok } = await loginAs(db, app, ['hr']);
    const body = { email: 'new@test.local', name: 'New', password: 'a-strong-passphrase-99', roles: ['manager'] };
    expect((await request(app).post('/api/v1/auth/users').set(auth(hrTok)).send(body)).status).toBe(403);
    const created = await request(app).post('/api/v1/auth/users').set(auth(adminTok)).send(body);
    expect(created.status).toBe(201);
    expect((await request(app).post('/api/v1/auth/users').set(auth(adminTok)).send(body)).status).toBe(409);
  });
});

describe('crypto helpers', () => {
  it('scrypt hashes verify with the pepper and fail without it', async () => {
    const h = await hashPassword('secret-passphrase-123', 'pepper');
    expect(await verifyPassword('secret-passphrase-123', h, 'pepper')).toBe(true);
    expect(await verifyPassword('secret-passphrase-123', h, 'other')).toBe(false);
    expect(await verifyPassword('secret-passphrase-124', h, 'pepper')).toBe(false);
  });
  it('field cipher round-trips, binds AAD, and gives a stable blind index', () => {
    const c = new FieldCipher(Buffer.alloc(32, 1));
    const blob = c.encrypt('123456789', 'employee:x:pan');
    expect(c.decrypt(blob, 'employee:x:pan')).toBe('123456789');
    expect(() => c.decrypt(blob, 'employee:y:pan')).toThrow();
    expect(c.blindIndex('123456789')).toBe(c.blindIndex(' 123456789 '));
    expect(blob.length).toBe(1 + 12 + 16 + 9);
  });
});
