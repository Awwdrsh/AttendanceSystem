import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { closeStaleBreaks } from '../src/modules/field/jobs.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** Field force, breaks, geofence policy, kiosks with PIN/QR, and shift swaps. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let hari: string; let foreignHr: string; let hariId: string;
const office = { lat: 27.7172, lng: 85.3240, accuracyM: 20, source: 'web' as const };
const far = { lat: 27.80, lng: 85.40, accuracyM: 20, source: 'web' as const };
const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.from('a-tiny-selfie')]);
const selfie = `data:image/jpeg;base64,${jpeg.toString('base64')}`;
const nowIso = () => new Date().toISOString();
const today = () => localDateOf(Date.now());
const addDays = (d: string, n: number) => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
let n = 0;
const punch = (tok: string, body: Record<string, unknown>) => request(app).post('/api/v1/punch').set(auth(tok)).send({ direction: 'in', clientTime: nowIso(), idempotencyKey: `field-${++n}-key`, ...body });
const get = (tok: string, url: string) => request(app).get(`/api/v1${url}`).set(auth(tok));
const post = (tok: string, url: string, body: unknown = {}) => request(app).post(`/api/v1${url}`).set(auth(tok)).send(body as object);
const put = (tok: string, url: string, body: unknown = {}) => request(app).put(`/api/v1${url}`).set(auth(tok)).send(body as object);

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  hariId = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, weekly_off_days) VALUES ($1, 'E003', 'Hari Karki', '{6}') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2026-01-01', NULL), $2, 'regular')`, [hariId, DEMO.branchId]);
  await db.query(`INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange('2026-01-01', NULL))`, [hariId, DEMO.shiftId]);
  hari = (await loginAs(db, app, ['employee'], hariId)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('FLD2', 'Other Co') RETURNING id`);
  await createUser(db, { companyId: c.rows[0]!.id, email: 'hr@fld2.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@fld2.local', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

/** A fresh employee in the demo branch on the general shift (punches are append-only, so each scenario gets its own person). */
async function newEmployee(code: string): Promise<{ id: string; token: string }> {
  const id = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) VALUES ($1, $2, $3) RETURNING id`, [DEMO.companyId, code, `Person ${code}`])).rows[0]!.id;
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2026-01-01', NULL), $2, 'regular')`, [id, DEMO.branchId]);
  await db.query(`INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES ($1, $2, daterange('2026-01-01', NULL))`, [id, DEMO.shiftId]);
  return { id, token: (await loginAs(db, app, ['employee'], id)).token };
}

describe('locations and geofence policy', () => {
  let hq: string;
  it('HR manages locations; employees cannot', async () => {
    expect((await post(sita, '/field/locations', { name: 'X', lat: 1, lng: 1 })).status).toBe(403);
    const c = await post(hr, '/field/locations', { name: 'Head office', address: 'Kathmandu', lat: 27.7172, lng: 85.3240, radiusM: 150 });
    expect(c.status).toBe(201);
    hq = c.body.id;
    const bad = await post(hr, '/field/locations', { name: 'Bad', lat: 200, lng: 1 });
    expect(bad.status).toBe(400);
    const list = await get(hr, '/field/locations');
    expect(list.body.locations).toHaveLength(1);
    expect(list.body.locations[0]).toMatchObject({ name: 'Head office', address: 'Kathmandu', active: true });
    expect((await get(foreignHr, '/field/locations')).body.locations).toHaveLength(0);
    expect((await put(foreignHr, `/field/locations/${hq}`, { name: 'Hijack', lat: 1, lng: 1 })).status).toBe(404);
  });

  it('policy defaults to flag; only HR may change it', async () => {
    expect((await get(sita, '/field/policy')).body.policy.geofenceMode).toBe('flag');
    expect((await put(sita, '/field/policy', { geofenceMode: 'off' })).status).toBe(403);
    const r = await put(hr, '/field/policy', { geofenceMode: 'block' });
    expect(r.status).toBe(200);
    expect(r.body.policy.breaks.lunch).toMatchObject({ paid: false, maxMin: 60 });
  });

  it('block refuses a punch outside; off ignores locations; flag asks for a reason', async () => {
    const g = await newEmployee('G001');
    const blocked = await punch(g.token, far);
    expect(blocked.status).toBe(400);
    expect(blocked.body.details).toEqual({ code: 'outside_location' });
    await put(hr, '/field/policy', { geofenceMode: 'flag' });
    const flagged = await punch(g.token, far);
    expect(flagged.status).toBe(400);
    expect(flagged.body.error).toMatch(/reason/);
    await put(hr, '/field/policy', { geofenceMode: 'off' });
    const off = await punch(g.token, far);
    expect(off.status).toBe(201);
    expect(off.body.location.inside).toBeNull();
    await put(hr, '/field/policy', { geofenceMode: 'block' });
  });

  it('locations tied to a person replace the branch locations', async () => {
    const g = await newEmployee('G002');
    const site = await post(hr, '/field/locations', { name: 'Site B', lat: 27.80, lng: 85.40, radiusM: 200 });
    expect((await put(hr, `/field/staff/${g.id}`, { locationIds: [site.body.id] })).status).toBe(200);
    const atHq = await punch(g.token, office);
    expect(atHq.status).toBe(400); // HQ is not one of this person's locations
    const atSite = await punch(g.token, far);
    expect(atSite.status).toBe(201);
    expect(atSite.body.location).toMatchObject({ inside: true, name: 'Site B' });
    expect((await put(hr, `/field/staff/${g.id}`, { locationIds: ['00000000-0000-7000-8000-00000000dead'] })).status).toBe(404);
    expect((await put(foreignHr, `/field/staff/${g.id}`, { fieldWork: true })).status).toBe(404);
  });
});

describe('breaks', () => {
  it('needs a clock-in; one open break at a time; ends with minutes', async () => {
    const early = await post(sita, '/field/breaks/start', { kind: 'tea' });
    expect(early.status).toBe(400);
    expect((await punch(sita, office)).status).toBe(201);
    const s = await post(sita, '/field/breaks/start', { kind: 'tea' });
    expect(s.status).toBe(201);
    expect(s.body.break).toMatchObject({ kind: 'tea', paid: true, max_min: 15 });
    expect((await post(sita, '/field/breaks/start', { kind: 'lunch' })).status).toBe(409);
    expect((await post(sita, '/field/breaks/start', { kind: 'nap' })).status).toBe(400);
    const me = await get(sita, '/field/me');
    expect(me.body.clockedIn).toBe(true);
    expect(me.body.openBreak).toMatchObject({ kind: 'tea' });
    const team = await get(ram, `/attendance/today?date=${today()}`);
    expect(team.body.rows.find((r: { employee_id: string }) => r.employee_id === DEMO.sita).on_break).toBe('tea');
    const e = await post(sita, '/field/breaks/end');
    expect(e.status).toBe(200);
    expect(e.body.break.over_limit).toBe(false);
    expect((await post(sita, '/field/breaks/end')).status).toBe(409);
  });

  it('an over-long break is flagged and the manager is told', async () => {
    await post(sita, '/field/breaks/start', { kind: 'lunch' });
    await db.query(`UPDATE work_break SET started_at = now() - interval '75 minutes' WHERE employee_id = $1 AND ended_at IS NULL`, [DEMO.sita]);
    const e = await post(sita, '/field/breaks/end');
    expect(e.body.break).toMatchObject({ over_limit: true, minutes: 75 });
    const note = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'break.over_limit' AND user_id = (SELECT id FROM app_user WHERE employee_id = $1)`, [DEMO.ram]);
    expect(note.rows[0].n).toBe(1);
  });

  it('break lists are scoped: self, manager, HR; CSV export works', async () => {
    const range = `from=${today()}&to=${today()}`;
    expect((await get(sita, `/field/breaks?${range}`)).body.breaks).toHaveLength(2);
    expect((await get(ram, `/field/breaks?${range}&overOnly=1`)).body.breaks).toHaveLength(1);
    expect((await get(hari, `/field/breaks?${range}`)).body.breaks).toHaveLength(0);
    expect((await get(hari, `/field/breaks?${range}&employeeId=${DEMO.sita}`)).status).toBe(403);
    expect((await get(hr, `/field/breaks?${range}`)).body.breaks).toHaveLength(2);
    expect((await get(foreignHr, `/field/breaks?${range}`)).body.breaks).toHaveLength(0);
    const csv = await get(hr, `/field/breaks?${range}&format=csv`);
    expect(csv.headers['content-type']).toContain('text/csv');
    expect(csv.text).toContain('Over limit');
    expect((await get(hr, `/field/breaks?from=2026-01-01&to=2026-12-31`)).status).toBe(400);
  });

  it('the nightly job closes a break left open from an earlier day', async () => {
    await db.query(`INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at) VALUES ($1, $2, $3, 'tea', true, 15, $4)`,
      [DEMO.companyId, hariId, addDays(today(), -2), new Date(Date.now() - 2 * 86_400_000)]);
    expect(await closeStaleBreaks(db)).toBe(1);
    const b = await db.query(`SELECT minutes FROM work_break WHERE employee_id = $1`, [hariId]);
    expect(b.rows[0].minutes).toBe(15);
    await db.query('DELETE FROM work_break WHERE employee_id = $1', [hariId]);
  });
});

describe('field visits', () => {
  let visitId: string;
  it('only field staff record visits', async () => {
    expect((await post(hari, '/field/visits/start', { place: 'Client A' })).status).toBe(403);
    expect((await put(sita, `/field/staff/${hariId}`, { fieldWork: true })).status).toBe(403);
    expect((await put(hr, `/field/staff/${hariId}`, { fieldWork: true })).status).toBe(200);
  });

  it('the first visit of the day opens the day with a field punch; photo stored encrypted', async () => {
    const s = await post(hari, '/field/visits/start', { place: 'Client A', purpose: 'Demo', photo: selfie, ...far });
    expect(s.status).toBe(201);
    expect(s.body.clockedIn).toBe(true);
    visitId = s.body.visit.id;
    const p = await db.query(`SELECT source, direction, raw->'flags' AS flags FROM punch_event WHERE employee_id = $1`, [hariId]);
    expect(p.rows).toEqual([{ source: 'field', direction: 'in', flags: ['Field'] }]);
    const f = await db.query(`SELECT f.data_enc FROM field_visit v JOIN stored_file f ON f.id = v.photo_file_id WHERE v.id = $1`, [visitId]);
    expect(f.rows[0].data_enc.includes(Buffer.from('a-tiny-selfie'))).toBe(false);
    expect((await post(hari, '/field/visits/start', { place: 'Client B' })).status).toBe(409);
  });

  it('field staff punching away from the office are tagged Field, not refused', async () => {
    const r = await punch(hari, { lat: 27.95, lng: 85.55, accuracyM: 20, source: 'web', direction: 'out' });
    expect(r.status).toBe(201);
    expect(r.body.flags).toContain('Field');
  });

  it('photo access: self and HR yes, colleagues no, other company no', async () => {
    expect((await get(hari, `/field/visits/${visitId}/photo`)).status).toBe(200);
    expect((await get(hr, `/field/visits/${visitId}/photo`)).status).toBe(200);
    expect((await get(sita, `/field/visits/${visitId}/photo`)).status).toBe(403);
    expect((await get(foreignHr, `/field/visits/${visitId}/photo`)).status).toBe(404);
  });

  it('end, list and export', async () => {
    expect((await post(sita, `/field/visits/${visitId}/end`, { outcome: 'x' })).status).toBe(404);
    const e = await post(hari, `/field/visits/${visitId}/end`, { outcome: 'Order placed', ...far });
    expect(e.status).toBe(200);
    expect(e.body.visit.outcome).toBe('Order placed');
    const list = await get(hr, `/field/visits?from=${today()}&to=${today()}`);
    expect(list.body.visits[0]).toMatchObject({ place: 'Client A', has_photo: true, opened_day: true, start_lat: 27.8 });
    expect((await get(sita, `/field/visits?from=${today()}&to=${today()}`)).body.visits).toHaveLength(0);
    const csv = await get(hr, `/field/visits?from=${today()}&to=${today()}&format=csv`);
    expect(csv.text).toContain('Client A');
  });

  it('a required photo is enforced', async () => {
    await put(hr, '/field/policy', { geofenceMode: 'block', fieldPhotoRequired: true });
    const r = await post(hari, '/field/visits/start', { place: 'Client C' });
    expect(r.status).toBe(400);
    expect(r.body.details).toEqual({ code: 'photo_required' });
    await put(hr, '/field/policy', { geofenceMode: 'flag' });
  });
});

describe('kiosks', () => {
  let kioskId: string; let key: string; let code: string;
  const kiosk = (url: string, body: unknown = {}) => request(app).post(`/api/v1/kiosk${url}`).set('x-kiosk-key', key).send(body as object);

  it('HR creates a kiosk with a one-time pairing code', async () => {
    expect((await post(sita, '/field/kiosks', { name: 'Lobby' })).status).toBe(403);
    const c = await post(hr, '/field/kiosks', { name: 'Lobby tablet' });
    expect(c.status).toBe(201);
    expect(c.body.pairingCode).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
    kioskId = c.body.id; code = c.body.pairingCode;
    expect((await get(foreignHr, '/field/kiosks')).body.kiosks).toHaveLength(0);
    expect((await get(hr, '/field/kiosks')).body.kiosks[0]).toMatchObject({ name: 'Lobby tablet', status: 'pairing', pairing_open: true });
  });

  it('pairs once; the key authenticates the tablet; no key, no access', async () => {
    expect((await request(app).post('/api/v1/kiosk/pair').send({ code: 'AAAA-BBBB' })).status).toBe(400);
    const p = await request(app).post('/api/v1/kiosk/pair').send({ code: code.toLowerCase() });
    expect(p.status).toBe(200);
    key = p.body.key;
    expect((await request(app).post('/api/v1/kiosk/pair').send({ code })).status).toBe(400);
    expect((await request(app).get('/api/v1/kiosk/session')).status).toBe(401);
    const s = await request(app).get('/api/v1/kiosk/session').set('x-kiosk-key', key);
    expect(s.body.kiosk).toMatchObject({ name: 'Lobby tablet', selfieRequired: true });
    const raw = await db.query('SELECT key_hash FROM kiosk WHERE id = $1', [kioskId]);
    expect(raw.rows[0].key_hash).not.toContain(key);
  });

  it('PIN: weak ones refused; wrong PINs counted and locked; HR resets', async () => {
    expect((await kiosk('/identify', { empCode: 'E002', pin: '4826' })).status).toBe(400); // no PIN set yet
    expect((await put(ram, '/field/kiosk-pin', { pin: '1234' })).status).toBe(400);
    expect((await put(ram, '/field/kiosk-pin', { pin: '12a4' })).status).toBe(400);
    expect((await put(ram, '/field/kiosk-pin', { pin: '4826' })).status).toBe(200);
    for (let i = 1; i <= 4; i++) {
      const w = await kiosk('/identify', { empCode: 'E002', pin: '1111' });
      expect(w.status).toBe(400);
      expect(w.body.error).toContain(`${5 - i} tr`);
    }
    expect((await kiosk('/identify', { empCode: 'E002', pin: '1111' })).status).toBe(429);
    expect((await kiosk('/identify', { empCode: 'E002', pin: '4826' })).status).toBe(429);
    expect((await post(ram, `/field/staff/${DEMO.ram}/kiosk-reset`)).status).toBe(403);
    expect((await post(hr, `/field/staff/${DEMO.ram}/kiosk-reset`)).status).toBe(200);
    expect((await put(ram, '/field/kiosk-pin', { pin: '4826' })).status).toBe(200);
  });

  it('identify → selfie → punch at server time; tickets are bound to the kiosk and single-use', async () => {
    const id = await kiosk('/identify', { empCode: 'e002', pin: '4826' });
    expect(id.status).toBe(200);
    expect(id.body).toMatchObject({ employee: { name: 'Ram Thapa', empCode: 'E002' }, nextDirection: 'in', selfieRequired: true });
    const noPhoto = await kiosk('/punch', { ticket: id.body.ticket });
    expect(noPhoto.body.details).toEqual({ code: 'photo_required' });
    const ok = await kiosk('/punch', { ticket: id.body.ticket, selfie });
    expect(ok.status).toBe(201);
    expect(ok.body.direction).toBe('in');
    const again = await kiosk('/punch', { ticket: id.body.ticket, selfie });
    expect(again.body.duplicate).toBe(true);
    const tampered = id.body.ticket.replace(/.$/, (c: string) => (c === 'a' ? 'b' : 'a'));
    expect((await kiosk('/punch', { ticket: tampered, selfie })).status).toBe(400);
    const p = await db.query(`SELECT source, raw->>'kioskId' AS kiosk FROM punch_event WHERE employee_id = $1`, [DEMO.ram]);
    expect(p.rows).toEqual([{ source: 'kiosk', kiosk: kioskId }]);
    const photo = await db.query('SELECT count(*)::int AS n FROM punch_photo WHERE employee_id = $1', [DEMO.ram]);
    expect(photo.rows[0].n).toBe(1);
  });

  it('QR badge: valid once, tampered refused', async () => {
    const qr = await get(ram, '/field/kiosk-qr');
    expect(qr.body.token).toMatch(/^HQ1\./);
    const id = await kiosk('/identify', { qr: qr.body.token });
    expect(id.status).toBe(200);
    expect(id.body.nextDirection).toBe('out');
    expect((await kiosk('/identify', { qr: qr.body.token })).body.error).toMatch(/already used/);
    const forged = qr.body.token.replace(DEMO.ram, DEMO.sita);
    expect((await kiosk('/identify', { qr: forged })).status).toBe(400);
  });

  it('revoked kiosks stop working', async () => {
    expect((await post(foreignHr, `/field/kiosks/${kioskId}/revoke`)).status).toBe(404);
    expect((await post(hr, `/field/kiosks/${kioskId}/revoke`)).status).toBe(200);
    expect((await request(app).get('/api/v1/kiosk/session').set('x-kiosk-key', key)).status).toBe(401);
    expect((await post(hr, `/field/kiosks/${kioskId}/pairing-code`)).status).toBe(409);
  });
});

describe('shift swaps', () => {
  let night: string; const day = addDays(today(), 3);
  beforeAll(async () => {
    night = (await db.query<{ id: string }>(`INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES ($1, 'NIGHT', 'Night', '22:00', '06:00') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    await db.query('DELETE FROM roster');
  });

  it('same shift cannot be swapped; colleagues list shows shifts', async () => {
    // Make sure the date is a working day for both.
    await db.query(`INSERT INTO roster (employee_id, local_date, shift_id) VALUES ($1, $3, $4), ($2, $3, $4)`, [DEMO.sita, hariId, day, DEMO.shiftId]);
    expect((await post(sita, '/field/swaps', { colleagueId: hariId, localDate: day })).body.error).toMatch(/same shift/);
    await db.query(`UPDATE roster SET shift_id = $3 WHERE employee_id = $1 AND local_date = $2`, [hariId, day, night]);
    const c = await get(sita, `/field/swap-colleagues?date=${day}`);
    expect(c.body.colleagues.find((x: { id: string }) => x.id === hariId).shift.code).toBe('NIGHT');
  });

  it('request → colleague accepts → manager approves → roster swapped', async () => {
    const r = await post(sita, '/field/swaps', { colleagueId: hariId, localDate: day, reason: 'Family event' });
    expect(r.status).toBe(201);
    expect((await post(sita, '/field/swaps', { colleagueId: hariId, localDate: day })).status).toBe(409);
    expect((await post(sita, `/field/swaps/${r.body.id}/accept`)).status).toBe(404); // only the colleague accepts
    expect((await post(hari, `/field/swaps/${r.body.id}/accept`, { note: 'Fine' })).status).toBe(200);
    const inbox = await get(ram, '/workflow/inbox');
    const item = inbox.body.items.find((i: { objectType: string }) => i.objectType === 'shift_swap');
    expect(item.summary).toMatchObject({ colleague_name: 'Hari Karki', requester_shift_code: 'GEN', colleague_shift_code: 'NIGHT' });
    expect((await post(ram, `/workflow/${item.instanceId}/approve`)).status).toBe(200);
    const roster = await db.query(`SELECT employee_id, shift_id FROM roster WHERE local_date = $1`, [day]);
    expect(new Map(roster.rows.map((x) => [x.employee_id, x.shift_id]))).toEqual(new Map([[DEMO.sita, night], [hariId, DEMO.shiftId]]));
    expect((await get(sita, '/field/swaps')).body.swaps[0].status).toBe('approved');
    const mine = await get(sita, `/field/my-roster?from=${day}&to=${day}`);
    expect(mine.body.days[0].shift.code).toBe('NIGHT');
    expect((await get(ram, '/field/swaps?scope=team')).body.swaps).toHaveLength(1);
    expect((await get(sita, '/field/swaps?scope=team')).status).toBe(403);
  });

  it('decline and cancel close the request; other companies and past days refused', async () => {
    const d2 = addDays(today(), 4);
    await db.query(`INSERT INTO roster (employee_id, local_date, shift_id) VALUES ($1, $3, $4), ($2, $3, $5)`, [DEMO.sita, hariId, d2, DEMO.shiftId, night]);
    const a = await post(sita, '/field/swaps', { colleagueId: hariId, localDate: d2 });
    expect((await post(hari, `/field/swaps/${a.body.id}/decline`, { note: 'Busy' })).body.status).toBe('declined');
    const b = await post(sita, '/field/swaps', { colleagueId: hariId, localDate: d2 });
    expect(b.status).toBe(201);
    expect((await post(hari, `/field/swaps/${b.body.id}/cancel`)).status).toBe(404);
    expect((await post(sita, `/field/swaps/${b.body.id}/cancel`)).body.status).toBe('cancelled');
    expect((await post(sita, '/field/swaps', { colleagueId: hariId, localDate: addDays(today(), -1) })).status).toBe(400);
    const foreign = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en) SELECT id, 'Z1', 'Zed' FROM company WHERE code = 'FLD2' RETURNING id`)).rows[0]!.id;
    expect((await post(sita, '/field/swaps', { colleagueId: foreign, localDate: d2 })).status).toBe(404);
  });
});
