import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf, localTimeMs } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { base32Encode, hotp, matchTotp, otpauthUri, stepAt, totpAt } from '../src/modules/auth/totp.js';
import { recomputeDay } from '../src/modules/attendance/service.js';
import { leaveTypeByCode, ledgerBalance } from '../src/modules/leave/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

const PASS = 'correct-horse-battery-staple';
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let admin: string; let sita: string; let ram: string;
let otherAdmin: string; let otherCompanyId: string;

async function makeUser(email: string, roles: Array<'admin' | 'hr' | 'manager' | 'employee'>, companyId = DEMO.companyId, employeeId: string | null = null) {
  const u = await createUser(db, { companyId, employeeId, email, name: email, password: PASS, roles, pepper: TEST_PEPPER });
  return u.id;
}
const signIn = (email: string, password = PASS) => request(app).post('/api/v1/auth/login').send({ email, password });

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  admin = (await loginAs(db, app, ['admin'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERJ', 'Other Co') RETURNING id`);
  otherCompanyId = c.rows[0]!.id;
  await makeUser('admin@other-j.local', ['admin'], otherCompanyId);
  otherAdmin = (await signIn('admin@other-j.local')).body.token;
});
afterAll(async () => { await db.end(); });

describe('TOTP primitives (RFC 6238)', () => {
  it('matches the RFC 6238 SHA-1 test vectors', () => {
    const secret = Buffer.from('12345678901234567890');
    // RFC 6238 Appendix B: T = 59 s -> 94287082 (8 digits); the 6-digit code is the last six digits.
    expect(hotp(secret, Math.floor(59 / 30))).toBe('287082');
    expect(hotp(secret, Math.floor(1111111109 / 30))).toBe('081804');
    expect(hotp(secret, Math.floor(1234567890 / 30))).toBe('005924');
  });
  it('accepts one step of drift either way and nothing else', () => {
    const s = base32Encode(Buffer.from('12345678901234567890'));
    const now = 1_700_000_000_000;
    expect(matchTotp(s, totpAt(s, now), now)).toBe(stepAt(now));
    expect(matchTotp(s, totpAt(s, now - 30_000), now)).toBe(stepAt(now) - 1);
    expect(matchTotp(s, totpAt(s, now + 30_000), now)).toBe(stepAt(now) + 1);
    expect(matchTotp(s, totpAt(s, now - 90_000), now)).toBeNull();
    expect(matchTotp(s, 'abcdef', now)).toBeNull();
  });
  it('builds an otpauth address for the QR code', () => {
    expect(otpauthUri('ABC', 'a@b.c')).toBe('otpauth://totp/Hajiri:a%40b.c?secret=ABC&issuer=Hajiri&algorithm=SHA1&digits=6&period=30');
  });
});

describe('two-step sign-in', () => {
  const email = 'twofa@test.local';
  let token = ''; let secret = ''; let userId = ''; let recovery: string[] = [];

  beforeAll(async () => {
    userId = await makeUser(email, ['employee']);
    token = (await signIn(email)).body.token;
  });

  it('is off by default and can be set up with a code from the app', async () => {
    const st = await request(app).get('/api/v1/auth/2fa').set(auth(token));
    expect(st.body).toMatchObject({ enabled: false, required: false });
    const setup = await request(app).post('/api/v1/auth/2fa/setup').set(auth(token));
    expect(setup.status).toBe(200);
    secret = setup.body.secret;
    expect(setup.body.otpauthUri).toContain(`secret=${secret}`);
    const raw = await db.query('SELECT totp_pending_enc, totp_secret_enc FROM app_user WHERE id = $1', [userId]);
    expect(raw.rows[0].totp_pending_enc.toString('latin1')).not.toContain(secret);
    expect(raw.rows[0].totp_secret_enc).toBeNull();
    expect((await request(app).post('/api/v1/auth/2fa/enable').set(auth(token)).send({ code: '000000' })).status).toBe(400);
    const en = await request(app).post('/api/v1/auth/2fa/enable').set(auth(token)).send({ code: totpAt(secret, Date.now()) });
    expect(en.status).toBe(200);
    recovery = en.body.recoveryCodes;
    expect(recovery).toHaveLength(10);
    expect(new Set(recovery).size).toBe(10);
    const me = await request(app).get('/api/v1/auth/me').set(auth(token));
    expect(me.body.user).toMatchObject({ twoFactorEnabled: true, mustSetUpTwoFactor: false });
    expect((await request(app).post('/api/v1/auth/2fa/setup').set(auth(token))).status).toBe(409);
  });

  it('a password alone no longer signs in: it returns a challenge and no session', async () => {
    const r = await signIn(email);
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ twoFactorRequired: true, challenge: expect.any(String) });
    expect(r.headers['set-cookie']).toBeUndefined();
    expect(r.body.token).toBeUndefined();
  });

  it('rejects a replayed code and accepts the next one', async () => {
    const used = (await db.query<{ totp_last_step: string }>('SELECT totp_last_step FROM app_user WHERE id = $1', [userId])).rows[0]!.totp_last_step;
    const replay = await signIn(email);
    const bad = await request(app).post('/api/v1/auth/login/2fa').send({ challenge: replay.body.challenge, code: hotp(Buffer.from(secretBytes()), Number(used)) });
    expect(bad.status).toBe(401);
    const next = await request(app).post('/api/v1/auth/login/2fa').send({ challenge: replay.body.challenge, code: totpAt(secret, Date.now() + 30_000) });
    expect(next.status).toBe(200);
    expect(next.body.token).toEqual(expect.any(String));
    expect(next.headers['set-cookie']?.[0]).toMatch(/hajiri_session=/);
    // The challenge is single-use.
    const again = await request(app).post('/api/v1/auth/login/2fa').send({ challenge: replay.body.challenge, code: totpAt(secret, Date.now() + 30_000) });
    expect(again.status).toBe(401);
    const s = await db.query('SELECT two_factor FROM session ORDER BY created_at DESC LIMIT 1');
    expect(s.rows[0].two_factor).toBe(true);
  });

  it('a recovery code works exactly once', async () => {
    const c1 = (await signIn(email)).body.challenge;
    const ok = await request(app).post('/api/v1/auth/login/2fa').send({ challenge: c1, code: recovery[0]!.toLowerCase() });
    expect(ok.status).toBe(200);
    expect(ok.body.usedRecoveryCode).toBe(true);
    const c2 = (await signIn(email)).body.challenge;
    expect((await request(app).post('/api/v1/auth/login/2fa').send({ challenge: c2, code: recovery[0] })).status).toBe(401);
    const st = await request(app).get('/api/v1/auth/2fa').set(auth(token));
    expect(st.body.recoveryCodesLeft).toBe(9);
  });

  it('a challenge allows five tries, and wrong codes count toward the account lockout', async () => {
    await db.query('UPDATE app_user SET failed_logins = 0 WHERE id = $1', [userId]);
    const c = (await signIn(email)).body.challenge;
    for (let i = 0; i < 5; i++) expect((await request(app).post('/api/v1/auth/login/2fa').send({ challenge: c, code: '111111' })).status).toBe(401);
    const sixth = await request(app).post('/api/v1/auth/login/2fa').send({ challenge: c, code: recovery[1] });
    expect(sixth.status).toBe(401);
    expect(sixth.body.error).toMatch(/expired/);
    const f = await db.query<{ failed_logins: number }>('SELECT failed_logins FROM app_user WHERE id = $1', [userId]);
    expect(f.rows[0]!.failed_logins).toBe(5);
    await db.query('UPDATE app_user SET failed_logins = 0 WHERE id = $1', [userId]);
  });

  it('an expired or unknown challenge is refused', async () => {
    const c = (await signIn(email)).body.challenge;
    await db.query(`UPDATE login_challenge SET expires_at = now() - interval '1 second'`);
    expect((await request(app).post('/api/v1/auth/login/2fa').send({ challenge: c, code: recovery[2] })).status).toBe(401);
    expect((await request(app).post('/api/v1/auth/login/2fa').send({ challenge: 'x'.repeat(43), code: recovery[2] })).status).toBe(401);
  });

  it('new recovery codes need a current code; turning it off needs the password and a code', async () => {
    expect((await request(app).post('/api/v1/auth/2fa/recovery-codes').set(auth(token)).send({ code: '123456' })).status).toBe(400);
    const rc = await request(app).post('/api/v1/auth/2fa/recovery-codes').set(auth(token)).send({ code: recovery[3] });
    expect(rc.status).toBe(200);
    recovery = rc.body.recoveryCodes;
    expect((await request(app).post('/api/v1/auth/2fa/disable').set(auth(token)).send({ password: 'wrong-password-123', code: recovery[0] })).status).toBe(401);
    expect((await request(app).post('/api/v1/auth/2fa/disable').set(auth(token)).send({ password: PASS, code: '000000' })).status).toBe(400);
    const off = await request(app).post('/api/v1/auth/2fa/disable').set(auth(token)).send({ password: PASS, code: recovery[1] });
    expect(off.status).toBe(200);
    const r = await signIn(email);
    expect(r.body.token).toEqual(expect.any(String));
    expect((await db.query('SELECT count(*)::int AS n FROM user_recovery_code WHERE user_id = $1', [userId])).rows[0].n).toBe(0);
  });

  function secretBytes(): Buffer {
    // Decode via the shared helper by round-tripping through totpAt's base32 handling.
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    let bits = 0; let value = 0; const out: number[] = [];
    for (const ch of secret) { value = (value << 5) | alphabet.indexOf(ch); bits += 5; if (bits >= 8) { out.push((value >>> (bits - 8)) & 255); bits -= 8; } }
    return Buffer.from(out);
  }
});

describe('company policy: required two-step sign-in, and admin reset', () => {
  let hrToken = ''; let hrId = '';
  beforeAll(async () => {
    hrId = await makeUser('needs2fa@test.local', ['hr']);
    hrToken = (await signIn('needs2fa@test.local')).body.token;
  });

  it('HR can require it for roles; such users can only use /auth until they set it up', async () => {
    const put = await request(app).put('/api/v1/settings/security.policy').set(auth(admin)).send({ require2faRoles: ['hr', 'hr'] });
    expect(put.status).toBe(200);
    expect(put.body.value.require2faRoles).toEqual(['hr']);
    expect((await request(app).put('/api/v1/settings/security.policy').set(auth(admin)).send({ require2faRoles: ['boss'] })).status).toBe(400);
    const me = await request(app).get('/api/v1/auth/me').set(auth(hrToken));
    expect(me.body.user.mustSetUpTwoFactor).toBe(true);
    const blocked = await request(app).get('/api/v1/employees').set(auth(hrToken));
    expect(blocked.status).toBe(403);
    expect(blocked.body.error).toBe('two-step sign-in setup required');
    // Employees are not affected.
    expect((await request(app).get('/api/v1/punch/today').set(auth(sita))).status).toBe(200);
    const setup = await request(app).post('/api/v1/auth/2fa/setup').set(auth(hrToken));
    expect(setup.status).toBe(200);
    const en = await request(app).post('/api/v1/auth/2fa/enable').set(auth(hrToken)).send({ code: totpAt(setup.body.secret, Date.now()) });
    expect(en.status).toBe(200);
    expect((await request(app).get('/api/v1/employees').set(auth(hrToken))).status).toBe(200);
    // It cannot be turned off while required.
    const off = await request(app).post('/api/v1/auth/2fa/disable').set(auth(hrToken)).send({ password: PASS, code: en.body.recoveryCodes[0] });
    expect(off.status).toBe(403);
  });

  it('only an admin of the same company can reset it; the person is signed out everywhere', async () => {
    expect((await request(app).post(`/api/v1/auth/users/${hrId}/2fa/reset`).set(auth(hr)).send({ reason: 'lost phone' })).status).toBe(403);
    expect((await request(app).post(`/api/v1/auth/users/${hrId}/2fa/reset`).set(auth(otherAdmin)).send({ reason: 'lost phone' })).status).toBe(404);
    expect((await request(app).post(`/api/v1/auth/users/${hrId}/2fa/reset`).set(auth(admin)).send({ reason: '' })).status).toBe(400);
    const r = await request(app).post(`/api/v1/auth/users/${hrId}/2fa/reset`).set(auth(admin)).send({ reason: 'lost phone' });
    expect(r.status).toBe(200);
    expect((await request(app).get('/api/v1/auth/me').set(auth(hrToken))).status).toBe(401);
    const a = await db.query(`SELECT after FROM audit_log WHERE action = 'two_factor_reset' AND row_id = $1`, [hrId]);
    expect(a.rows[0].after).toEqual({ reason: 'lost phone' });
    const users = await request(app).get('/api/v1/auth/users').set(auth(admin));
    expect(users.body.users.find((u: { id: string }) => u.id === hrId).two_factor).toBe(false);
    await request(app).put('/api/v1/settings/security.policy').set(auth(admin)).send({ require2faRoles: [] });
  });
});

describe('active sessions', () => {
  const email = 'sessions@test.local';
  it('lists my sessions, signs out one or all others, never someone else\'s', async () => {
    await makeUser(email, ['employee']);
    const a = (await request(app).post('/api/v1/auth/login').set('User-Agent', 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36').send({ email, password: PASS })).body.token;
    const b = (await signIn(email)).body.token;
    const c = (await signIn(email)).body.token;
    const list = await request(app).get('/api/v1/auth/sessions').set(auth(a));
    expect(list.body.sessions).toHaveLength(3);
    expect(list.body.sessions[0]).toMatchObject({ current: true, device: 'Chrome on Android' });
    expect(JSON.stringify(list.body)).not.toMatch(/token_hash|[a-f0-9]{64}/);
    const other = list.body.sessions.find((s: { current: boolean }) => !s.current).id;
    expect((await request(app).delete(`/api/v1/auth/sessions/${list.body.sessions[0].id}`).set(auth(a))).status).toBe(400);
    expect((await request(app).delete(`/api/v1/auth/sessions/${other}`).set(auth(sita))).status).toBe(404);
    expect((await request(app).delete(`/api/v1/auth/sessions/${other}`).set(auth(a))).status).toBe(200);
    const left = await request(app).post('/api/v1/auth/sessions/revoke-others').set(auth(a));
    expect(left.body.revoked).toBe(1);
    expect((await request(app).get('/api/v1/auth/me').set(auth(b))).status).toBe(401);
    expect((await request(app).get('/api/v1/auth/me').set(auth(c))).status).toBe(401);
    expect((await request(app).get('/api/v1/auth/me').set(auth(a))).status).toBe(200);
  });
});

describe('hourly leave', () => {
  let home = ''; let casual = ''; let requestId = '';
  const DAY = '2026-09-21'; // Monday, shift 10:00-18:00 (8 h)
  let seq = 0;
  const punch = async (hhmm: string) => {
    const at = new Date(localTimeMs(DAY, hhmm));
    await db.query(
      `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
       VALUES ($1, 'DEMO-01', 'zkteco', '1', $2, $3, $4, $5, 'unknown', 'fingerprint', 'zkteco', $6)`,
      [DEMO.deviceId, DEMO.sita, at, `${DAY} ${hhmm}:00`, localDateOf(at.getTime()), `hl-${++seq}`]);
  };

  beforeAll(async () => {
    home = (await leaveTypeByCode(db, DEMO.companyId, 'HOME'))!.id;
    casual = (await leaveTypeByCode(db, DEMO.companyId, 'CASUAL'))!.id;
    await request(app).post('/api/v1/leave/adjust').set(auth(hr)).send({ employeeId: DEMO.sita, leaveTypeId: home, deltaDays: 5, reason: 'opening balance', kind: 'allocation' });
  });

  it('home and sick leave allow hours by default; unpaid or event leave cannot', async () => {
    const types = await request(app).get('/api/v1/leave/types').set(auth(hr));
    const byCode = Object.fromEntries(types.body.types.map((t: { code: string; allow_hourly: boolean }) => [t.code, t.allow_hourly]));
    expect(byCode).toMatchObject({ HOME: true, SICK: true, CASUAL: false, LWP: false });
    const lwp = types.body.types.find((t: { code: string }) => t.code === 'LWP').id;
    expect((await request(app).patch(`/api/v1/leave/types/${lwp}`).set(auth(hr)).send({ allowHourly: true })).status).toBe(400);
    expect((await request(app).patch(`/api/v1/leave/types/${casual}`).set(auth(sita)).send({ allowHourly: true })).status).toBe(403);
  });

  it('previews hours as a fraction of the working day', async () => {
    const p = await request(app).get('/api/v1/leave/preview').set(auth(sita)).query({ leaveTypeId: home, from: DAY, to: DAY, fromTime: '10:00', toTime: '12:00' });
    expect(p.status).toBe(200);
    expect(p.body).toMatchObject({ days: 0.25, hours: 2 });
    expect(p.body.explanation).toMatch(/2 hour/);
  });

  it('validates the window', async () => {
    const send = (b: Record<string, unknown>) => request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: home, from: DAY, to: DAY, ...b });
    expect((await send({ fromTime: '12:00', toTime: '11:00' })).status).toBe(400);
    expect((await send({ fromTime: '09:00', toTime: '11:00' })).body.error).toMatch(/within your shift/);
    expect((await send({ fromTime: '10:00', toTime: '10:15' })).body.error).toMatch(/at least 30/);
    expect((await send({ fromTime: '10:00', toTime: '18:00' })).body.error).toMatch(/full day/);
    expect((await send({ fromTime: '10:00' })).status).toBe(400);
    expect((await send({ fromTime: '10:00', toTime: '11:00', halfDay: 'first_half' })).status).toBe(400);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: home, from: DAY, to: '2026-09-22', fromTime: '10:00', toTime: '11:00' })).status).toBe(400);
    expect((await send({ leaveTypeId: casual, fromTime: '10:00', toTime: '11:00' })).body.error).toMatch(/by the hour/);
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: home, from: '2026-09-26', to: '2026-09-26', fromTime: '10:00', toTime: '11:00' })).body.error).toMatch(/working day/);
  });

  it('is approved like any leave, deducts the fraction and excuses the late arrival', async () => {
    await punch('12:05'); await punch('18:00');
    await recomputeDay(db, DEMO.sita, DAY);
    const before = await db.query(`SELECT late_min, status FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2`, [DEMO.sita, DAY]);
    expect(before.rows[0].late_min).toBe(110);

    const r = await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: home, from: DAY, to: DAY, fromTime: '10:00', toTime: '12:00', reason: 'bank' });
    expect(r.status).toBe(201);
    expect(r.body.days).toBe(0.25);
    requestId = r.body.id;
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === requestId);
    expect(item.summary).toMatchObject({ fromTime: '10:00', toTime: '12:00', hours: 2, days: 0.25 });
    // A second request overlapping the same day is refused.
    expect((await request(app).post('/api/v1/leave/requests').set(auth(sita)).send({ leaveTypeId: home, from: DAY, to: DAY, fromTime: '14:00', toTime: '15:00' })).status).toBe(409);
    const act = await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(ram)).send({});
    expect(act.status).toBe(200);
    expect(await ledgerBalance(db, DEMO.sita, home)).toBeCloseTo(4.75, 5);
    await recomputeDay(db, DEMO.sita, DAY);
    const after = await db.query(`SELECT late_min, status, flags FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2`, [DEMO.sita, DAY]);
    expect(after.rows[0]).toMatchObject({ late_min: 0, status: 'Present' });
    expect(after.rows[0].flags).toContainEqual({ kind: 'ShortLeave', minutes: 120 });
    const list = await request(app).get('/api/v1/leave/requests').set(auth(sita)).query({ scope: 'own' });
    expect(list.body.requests.find((x: { id: string }) => x.id === requestId)).toMatchObject({ from_time: '10:00', to_time: '12:00', hours: '2.00' });
    const cal = await request(app).get('/api/v1/leave/team-calendar').set(auth(sita)).query({ from: DAY, to: DAY });
    expect(cal.body.days[0].off[0]).toMatchObject({ fromTime: '10:00', toTime: '12:00' });
  });

  it('cancelling an approved hourly leave gives the fraction back', async () => {
    const c = await request(app).post(`/api/v1/leave/requests/${requestId}/cancel`).set(auth(sita)).send({});
    expect(c.status).toBe(200);
    expect(await ledgerBalance(db, DEMO.sita, home)).toBeCloseTo(5, 5);
  });
});

describe('tax savings self-service', () => {
  const FY = 2083;
  let requestId = ''; let proofId = '';

  beforeAll(async () => {
    const s = await request(app).get('/api/v1/payroll/structures').set(auth(hr));
    const ver = s.body.structures[0].versions[0].id;
    const a = await request(app).post('/api/v1/payroll/assignments').set(auth(hr)).send({ employeeId: DEMO.sita, structureVersionId: ver, from: '2026-07-17', baseAmounts: { BASIC: '150000.00' } });
    expect(a.status).toBe(201);
  });

  it('estimates tax before and after the proposed savings using the payroll engine', async () => {
    const e = await request(app).post('/api/v1/payroll/tax-savings/estimate').set(auth(sita)).send({ fiscalYearStart: FY, proposed: { lifePremium: '40000.00', healthPremium: '20000.00' } });
    expect(e.status).toBe(200);
    expect(e.body.available).toBe(true);
    expect(Number(e.body.current.annualTax)).toBeGreaterThan(0);
    expect(e.body.proposed.insuranceDeduction).toBe('60000.00');
    expect(Number(e.body.annualSaving)).toBeGreaterThan(0);
    expect(Number(e.body.proposed.annualTax)).toBeLessThan(Number(e.body.current.annualTax));
    // Without a salary there is nothing to estimate; another person's estimate is HR only.
    const ramEst = await request(app).post('/api/v1/payroll/tax-savings/estimate').set(auth(ram)).send({});
    expect(ramEst.body).toMatchObject({ available: false });
    expect((await request(app).post('/api/v1/payroll/tax-savings/estimate').set(auth(ram)).send({ employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).post('/api/v1/payroll/tax-savings/estimate').set(auth(hr)).send({ employeeId: DEMO.sita })).body.available).toBe(true);
  });

  it('submitting with a proof creates a pending request that does not touch payroll', async () => {
    const pdf = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n%%EOF').toString('base64')}`;
    const up = await request(app).post('/api/v1/files').set(auth(sita)).send({ purpose: 'tax_proof', filename: 'premium-receipt.pdf', dataUrl: pdf });
    expect(up.status).toBe(201);
    proofId = up.body.file.id;
    const neg = await request(app).post('/api/v1/payroll/tax-savings').set(auth(sita)).send({ fiscalYearStart: FY, lifePremium: '-1.00' });
    expect(neg.status).toBe(400);
    const r = await request(app).post('/api/v1/payroll/tax-savings').set(auth(sita)).send({ fiscalYearStart: FY, lifePremium: '40000.00', healthPremium: '20000.00', note: 'policy renewed', proofFileIds: [proofId] });
    expect(r.status).toBe(201);
    requestId = r.body.request.id;
    const decl = await db.query(`SELECT * FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2`, [DEMO.sita, FY]);
    expect(decl.rows).toHaveLength(0);
    const mine = await request(app).get('/api/v1/payroll/tax-savings').set(auth(sita)).query({ fiscalYearStart: FY });
    expect(mine.body.declaration).toBeNull();
    expect(mine.body.pending).toMatchObject({ id: requestId, life_premium: '40000.00' });
    expect(mine.body.history[0].proofs[0]).toMatchObject({ id: proofId, filename: 'premium-receipt.pdf' });
    // A proof already attached cannot be reused by someone else.
    const steal = await request(app).post('/api/v1/payroll/tax-savings').set(auth(ram)).send({ fiscalYearStart: FY, proofFileIds: [proofId] });
    expect(steal.status).toBe(400);
  });

  it('proof files: the employee and HR (audited) only', async () => {
    expect((await request(app).get(`/api/v1/payroll/tax-savings/${requestId}/files/${proofId}`).set(auth(sita))).status).toBe(200);
    expect((await request(app).get(`/api/v1/payroll/tax-savings/${requestId}/files/${proofId}`).set(auth(ram))).status).toBe(404);
    const h = await request(app).get(`/api/v1/payroll/tax-savings/${requestId}/files/${proofId}`).set(auth(hr));
    expect(h.status).toBe(200);
    expect(h.headers['content-type']).toContain('application/pdf');
    expect((await db.query(`SELECT 1 FROM audit_log WHERE action = 'view_file' AND row_id = $1`, [requestId])).rows).toHaveLength(1);
    expect((await request(app).get(`/api/v1/payroll/tax-savings/${requestId}/files/${proofId}`).set(auth(otherAdmin))).status).toBe(404);
  });

  it('a new submission replaces the pending one; the manager cannot approve, HR can', async () => {
    const r2 = await request(app).post('/api/v1/payroll/tax-savings').set(auth(sita)).send({ fiscalYearStart: FY, lifePremium: '35000.00', healthPremium: '20000.00' });
    expect(r2.status).toBe(201);
    const old = await db.query('SELECT status FROM tax_declaration_request WHERE id = $1', [requestId]);
    expect(old.rows[0].status).toBe('cancelled');
    requestId = r2.body.request.id;
    const ramInbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(ramInbox.body.items.find((i: { objectId: string }) => i.objectId === requestId)).toBeUndefined();
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === requestId);
    expect(item.objectType).toBe('tax_declaration');
    expect(item.summary).toMatchObject({ fiscalYearStart: FY, lifePremium: '35000.00', current: null });
    const act = await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(hr)).send({});
    expect(act.status).toBe(200);
    const decl = await db.query(`SELECT status, life_premium FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2`, [DEMO.sita, FY]);
    expect(decl.rows[0]).toMatchObject({ status: 'verified', life_premium: '35000.00' });
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE template = 'tax_declaration.approved'`);
    expect(n.rows.length).toBeGreaterThan(0);
    const est = await request(app).post('/api/v1/payroll/tax-savings/estimate').set(auth(sita)).send({ fiscalYearStart: FY });
    expect(est.body.current.insuranceDeduction).toBe('55000.00');
  });

  it('the old declarations endpoint: own submissions become requests; HR entry for others is verified directly', async () => {
    const own = await request(app).put('/api/v1/payroll/declarations').set(auth(sita)).send({ fiscalYearStart: FY, lifePremium: '10000.00' });
    expect(own.body).toMatchObject({ ok: true, status: 'pending' });
    const still = await db.query(`SELECT life_premium FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2`, [DEMO.sita, FY]);
    expect(still.rows[0].life_premium).toBe('35000.00');
    const get = await request(app).get('/api/v1/payroll/declarations').set(auth(hr)).query({ employeeId: DEMO.sita, fiscalYearStart: FY });
    expect(get.body.pending).toMatchObject({ life_premium: '10000.00' });
    const rej = await request(app).post(`/api/v1/payroll/declarations/${DEMO.sita}/verify`).set(auth(hr)).send({ fiscalYearStart: FY, decision: 'rejected', reason: 'no receipt' });
    expect(rej.status).toBe(200);
    expect((await db.query(`SELECT life_premium FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2`, [DEMO.sita, FY])).rows[0].life_premium).toBe('35000.00');
    const hrEntry = await request(app).put('/api/v1/payroll/declarations').set(auth(hr)).send({ employeeId: DEMO.ram, fiscalYearStart: FY, healthPremium: '15000.00' });
    expect(hrEntry.body).toMatchObject({ ok: true, status: 'verified' });
  });

  it('cannot cancel someone else\'s request; can cancel my own pending one', async () => {
    const r = await request(app).post('/api/v1/payroll/tax-savings').set(auth(sita)).send({ fiscalYearStart: FY, medicalCost: '5000.00' });
    expect((await request(app).post(`/api/v1/payroll/tax-savings/${r.body.request.id}/cancel`).set(auth(ram))).status).toBe(404);
    expect((await request(app).post(`/api/v1/payroll/tax-savings/${r.body.request.id}/cancel`).set(auth(sita))).status).toBe(200);
    const inst = await db.query(`SELECT state FROM workflow_instance WHERE object_id = $1`, [r.body.request.id]);
    expect(inst.rows[0].state).toBe('cancelled');
    expect((await request(app).get('/api/v1/payroll/tax-savings').set(auth(ram)).query({ employeeId: DEMO.sita })).status).toBe(403);
  });
});
