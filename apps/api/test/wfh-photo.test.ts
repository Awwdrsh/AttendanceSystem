import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import pino from 'pino';
import type { Db } from '../src/db/pool.js';
import { purgeExpiredPhotos } from '../src/worker.js';
import { createUser } from '../src/modules/auth/service.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher, TEST_PEPPER } from './setup-db.js';

/** Work-from-home clock-in with a photo: required from home, encrypted at rest, visible to self, manager and HR only. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let peer: string; let foreignHr: string;
const home = { lat: 27.80, lng: 85.40, accuracyM: 30, source: 'web' as const };
const office = { lat: 27.7172, lng: 85.3240, accuracyM: 20, source: 'web' as const };
const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.from('a-tiny-selfie')]);
const selfie = `data:image/jpeg;base64,${jpeg.toString('base64')}`;
const nowIso = () => new Date().toISOString();
let n = 0;
const punch = (tok: string, body: Record<string, unknown>) => request(app).post('/api/v1/punch').set(auth(tok)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: `photo-${++n}-key`, ...body });

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  const other = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, 'E099', 'Peer Person') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  peer = (await loginAs(db, app, ['employee'], other)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES ($1, 'Head office', 27.7172, 85.3240, 150)`, [DEMO.companyId]);
  await db.query('UPDATE employee SET work_from_home = true WHERE id = $1', [DEMO.sita]);
  const c = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id`)).rows[0]!.id;
  await createUser(db, { companyId: c, email: 'hr@other.photo', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.photo', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

let photoId: string;

describe('clock-in from home with a photo', () => {
  it('asks for a photo, then stores it encrypted and links it to the punch', async () => {
    const missing = await punch(sita, home);
    expect(missing.status).toBe(400);
    expect(missing.body.details).toMatchObject({ code: 'photo_required' });
    const ok = await punch(sita, { ...home, selfie });
    expect(ok.status).toBe(201);
    expect(ok.body.remote).toBe(true);
    photoId = ok.body.photoId;
    expect(photoId).toMatch(/^[0-9a-f-]{36}$/);
    const row = (await db.query('SELECT image_enc, mime, bytes FROM punch_photo WHERE id = $1', [photoId])).rows[0];
    expect(row).toMatchObject({ mime: 'image/jpeg', bytes: jpeg.length });
    expect(Buffer.from(row.image_enc).includes(jpeg)).toBe(false); // not stored in the clear
    const today = await request(app).get('/api/v1/punch/today').set(auth(sita));
    expect(today.body.punches.at(-1).photo_id).toBe(photoId);
    const team = await request(app).get('/api/v1/attendance/today').set(auth(hr)).query({ date: nptToday() });
    expect(team.body.rows.find((r: { employee_id: string }) => r.employee_id === DEMO.sita).photo_id).toBe(photoId);
  });

  it('serves the photo to the employee, their manager and HR (audited), and to nobody else', async () => {
    const own = await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(sita));
    expect(own.status).toBe(200);
    expect(own.headers['content-type']).toMatch(/^image\/jpeg/);
    expect(own.headers['cache-control']).toBe('private, max-age=300');
    expect(Buffer.from(own.body).equals(jpeg)).toBe(true);
    expect((await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(ram))).status).toBe(200);
    expect((await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(hr))).status).toBe(200);
    expect((await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(peer))).status).toBe(403);
    expect((await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(foreignHr))).status).toBe(404);
    expect((await request(app).get(`/api/v1/punch/photos/${photoId}`))).toHaveProperty('status', 401);
    const views = await db.query(`SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_photo' AND row_id = $1`, [photoId]);
    expect(views.rows[0].n).toBe(2);
  });

  it('lists home check-ins: HR sees everyone, a manager their team, an employee only themselves', async () => {
    const q = { from: nptToday(), to: nptToday() };
    const all = await request(app).get('/api/v1/punch/remote').set(auth(hr)).query(q);
    expect(all.body.punches).toHaveLength(1);
    expect(all.body.punches[0]).toMatchObject({ employee_id: DEMO.sita, photo_id: photoId, location_shared: true, direction: 'in' });
    expect(all.body.punches[0]).not.toHaveProperty('lat');
    expect((await request(app).get('/api/v1/punch/remote').set(auth(ram)).query(q)).body.punches).toHaveLength(1);
    expect((await request(app).get('/api/v1/punch/remote').set(auth(peer)).query(q)).body.punches).toHaveLength(0);
    expect((await request(app).get('/api/v1/punch/remote').set(auth(peer)).query({ ...q, employeeId: DEMO.sita })).status).toBe(403);
    expect((await request(app).get('/api/v1/punch/remote').set(auth(foreignHr)).query(q)).body.punches).toHaveLength(0);
  });

  it('a clock-out from home does not need a photo; office punches never keep one', async () => {
    expect((await punch(sita, { ...home, direction: 'out' })).status).toBe(201);
    await db.query('UPDATE employee SET work_from_home = true WHERE id = $1', [DEMO.ram]);
    const atOffice = await punch(ram, { ...office, selfie });
    expect(atOffice.status).toBe(201);
    expect(atOffice.body.remote).toBe(false);
    expect(atOffice.body.photoId).toBeNull();
    expect((await db.query('SELECT count(*)::int AS n FROM punch_photo WHERE employee_id = $1', [DEMO.ram])).rows[0].n).toBe(0);
  });

  it('rejects anything that is not a real image, and oversized photos', async () => {
    const fake = `data:image/jpeg;base64,${Buffer.from('<svg onload=alert(1)>').toString('base64')}`;
    expect((await punch(sita, { ...home, selfie: fake })).status).toBe(400);
    expect((await punch(sita, { ...home, selfie: 'data:text/html;base64,PGgxPg==' })).status).toBe(400);
    const big = `data:image/jpeg;base64,${Buffer.concat([jpeg, Buffer.alloc(310 * 1024)]).toString('base64')}`;
    expect((await punch(sita, { ...home, selfie: big })).status).toBe(400);
  });

  it('the company can turn the photo off', async () => {
    await db.query(`UPDATE company_setting SET value = value || '{"wfhPhotoRequired": false}' WHERE company_id = $1 AND key = 'attendance.policy'`, [DEMO.companyId]);
    expect((await punch(sita, home)).status).toBe(201);
    await db.query(`UPDATE company_setting SET value = value || '{"wfhPhotoRequired": true}' WHERE company_id = $1 AND key = 'attendance.policy'`, [DEMO.companyId]);
  });

  it('photos are wiped after the retention period and then report as deleted', async () => {
    const later = new Date(Date.now() + 91 * 86_400_000);
    const purged = await purgeExpiredPhotos({ db, cipher: testCipher, logger: pino({ level: 'silent' }) }, later);
    expect(purged).toBeGreaterThan(0);
    const gone = await request(app).get(`/api/v1/punch/photos/${photoId}`).set(auth(hr));
    expect(gone.status).toBe(404);
    expect(gone.body.error).toMatch(/retention/);
    expect((await db.query('SELECT purged_at FROM punch_photo WHERE id = $1', [photoId])).rows[0].purged_at).not.toBeNull();
  });
});
