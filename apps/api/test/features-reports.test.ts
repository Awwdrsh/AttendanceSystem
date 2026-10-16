import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { invalidateFeatures, FEATURE_KEYS } from '../src/modules/features/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** Settings → Features (module switches), People record reports and the ad-hoc report builder. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hr2: string; let sita: string; let ram: string; let foreignHr: string;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  hr2 = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`UPDATE employee SET join_date = '2024-04-13', dob = '1994-10-20', marital_status = 'single',
                    permanent_address = '{"province":"Bagmati","district":"Lalitpur","municipality":"Lalitpur Metropolitan City","ward":5}' WHERE id = $1`, [DEMO.sita]);
  await db.query(`UPDATE employee SET join_date = '2022-01-01', dob = '1985-03-02', passport_expiry = current_date + 20 WHERE id = $1`, [DEMO.ram]);
  await db.query(`INSERT INTO employee_family (employee_id, name, relation, is_nominee, nominee_share) VALUES ($1, 'Gita Sharma', 'mother', true, 100)`, [DEMO.sita]);
  await db.query(`INSERT INTO employee_emergency_contact (employee_id, name, relation, phone) VALUES ($1, 'Hari Sharma', 'father', '9800000000')`, [DEMO.sita]);
  await db.query(`INSERT INTO employee_education (employee_id, level, degree, institution, end_year) VALUES ($1, 'bachelor', 'BBA', 'Tribhuvan University', 2016)`, [DEMO.sita]);
  const c = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERF', 'Other Co') RETURNING id`)).rows[0]!.id;
  await createUser(db, { companyId: c, email: 'hr@other.features', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.features', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

const setFeature = async (tok: string, key: string, on: boolean) =>
  request(app).put('/api/v1/settings/features').set(auth(tok)).send({ enabled: { [key]: on } });

describe('feature switches', () => {
  it('everything is on by default and every signed-in user can read the switches', async () => {
    const r = await request(app).get('/api/v1/settings/features').set(auth(sita));
    expect(r.status).toBe(200);
    expect(Object.keys(r.body.enabled).sort()).toEqual([...FEATURE_KEYS].sort());
    expect(Object.values(r.body.enabled).every((v) => v === true)).toBe(true);
    expect(r.body.features[0]).toMatchObject({ key: expect.any(String), label: expect.any(String), enabled: true });
  });

  it('only HR or admin can change them, and unknown keys are refused', async () => {
    expect((await setFeature(sita, 'time', false)).status).toBe(403);
    expect((await setFeature(ram, 'time', false)).status).toBe(403);
    expect((await setFeature(hr, 'payroll', false)).status).toBe(400);
    expect((await request(app).put('/api/v1/settings/features').set(auth(hr)).send({ enabled: { time: 'no' } })).status).toBe(400);
  });

  // Each switch: [feature, a GET that works while on, the status that GET gives while on].
  const probes: Array<[string, string]> = [
    ['time', '/api/v1/time/settings'],
    ['expenses', '/api/v1/expenses/policy'],
    ['performance', '/api/v1/performance/goals'],
    ['learning', '/api/v1/learning/courses'],
    ['hiring', '/api/v1/hiring/jobs'],
    ['lifecycle', '/api/v1/lifecycle/assets'],
    ['documents', '/api/v1/documents/categories'],
    ['helpdesk', '/api/v1/helpdesk/categories'],
    ['kudos', '/api/v1/engage/leaderboard'],
    ['surveys', '/api/v1/engage/surveys'],
    ['suggestions', '/api/v1/engage/suggestions/public'],
    ['field', '/api/v1/field/kiosks'],
  ];

  for (const [key, path] of probes) {
    it(`switching ${key} off answers 404 "feature off", and on again restores it`, async () => {
      const before = await request(app).get(path).set(auth(hr));
      expect(before.status, `${path} while on`).not.toBe(404);
      expect((await setFeature(hr, key, false)).status).toBe(200);
      const off = await request(app).get(path).set(auth(hr));
      expect(off.status).toBe(404);
      expect(off.body).toMatchObject({ error: 'feature off', feature: key });
      // Another company is not affected.
      const other = await request(app).get(path).set(auth(foreignHr));
      expect(other.body.error).not.toBe('feature off');
      expect((await setFeature(hr, key, true)).status).toBe(200);
      expect((await request(app).get(path).set(auth(hr))).status).toBe(before.status);
    });
  }

  it('field switch leaves locations and rosters working; tasks is gated even before its module exists', async () => {
    await setFeature(hr, 'field', false);
    expect((await request(app).get('/api/v1/field/locations').set(auth(hr))).status).toBe(200);
    expect((await request(app).get('/api/v1/field/visits').set(auth(hr))).body.error).toBe('feature off');
    await setFeature(hr, 'field', true);
    await setFeature(hr, 'tasks', false);
    expect((await request(app).get('/api/v1/tasks').set(auth(hr))).body).toMatchObject({ error: 'feature off' });
    await setFeature(hr, 'tasks', true);
  });

  it('switched-off kudos disappear from the feed and the public careers page closes with hiring', async () => {
    await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [DEMO.ram], message: 'Thanks for the help with the report' });
    const on = await request(app).get('/api/v1/engage/feed').set(auth(sita));
    expect(on.body.items.some((i: { type?: string; kind?: string }) => (i.type ?? i.kind) === 'kudos')).toBe(true);
    await setFeature(hr, 'kudos', false);
    const off = await request(app).get('/api/v1/engage/feed').set(auth(sita));
    expect(off.body.items.some((i: { type?: string; kind?: string }) => (i.type ?? i.kind) === 'kudos')).toBe(false);
    await setFeature(hr, 'kudos', true);

    await setFeature(hr, 'hiring', false);
    expect((await request(app).get('/api/v1/public/careers/jobs?company=DEMO')).status).toBe(404);
    await setFeature(hr, 'hiring', true);
    expect((await request(app).get('/api/v1/public/careers/jobs?company=DEMO')).status).toBe(200);
  });

  it('a saved change is audited and takes effect at once (no stale cache)', async () => {
    await setFeature(hr, 'helpdesk', false);
    const a = await db.query(`SELECT after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'features' ORDER BY id DESC LIMIT 1`);
    expect(a.rows[0].after.off).toContain('helpdesk');
    invalidateFeatures();
    expect((await request(app).get('/api/v1/helpdesk/categories').set(auth(sita))).status).toBe(404);
    await setFeature(hr, 'helpdesk', true);
  });
});

describe('People record reports', () => {
  const people = (name: string, tok: string, q = '') => request(app).get(`/api/v1/reports/people/${name}${q}`).set(auth(tok));

  it('headcount on a date counts only people employed on that date', async () => {
    const now = await people('headcount-on-date', hr);
    expect(now.status).toBe(200);
    const total = now.body.rows.reduce((s: number, r: { headcount: number }) => s + r.headcount, 0);
    expect(total).toBeGreaterThanOrEqual(2);
    const past = await people('headcount-on-date', hr, '?date=2023-01-01');
    expect(past.body.rows.reduce((s: number, r: { headcount: number }) => s + r.headcount, 0)).toBe(total - 1);
  });

  it('statistics, joiners and leavers, birthdays, nominees, contacts, addresses and education', async () => {
    const stats = await people('statistics', hr);
    expect(stats.body.rows.find((r: { dimension: string; bucket: string }) => r.dimension === 'Province (permanent address)' && r.bucket === 'Bagmati')).toBeTruthy();
    const jl = await people('joiners-leavers', hr, '?from=2024-04-01&to=2024-04-30');
    expect(jl.body.rows.map((r: { emp_code: string }) => r.emp_code)).toEqual(['E001']);
    const bd = await people('birthdays-anniversaries', hr, '?from=2026-10-15&days=10');
    expect(bd.body.rows.some((r: { occasion: string; emp_code: string; years: number }) => r.occasion === 'Birthday' && r.emp_code === 'E001' && r.years === 32)).toBe(true);
    expect((await people('nominees', hr)).body.rows[0]).toMatchObject({ nominee: 'Gita Sharma', share: '100.00' });
    expect((await people('emergency-contacts', hr)).body.rows[0]).toMatchObject({ contact: 'Hari Sharma' });
    expect((await people('addresses', hr)).body.rows.find((r: { emp_code: string }) => r.emp_code === 'E001').permanent_address).toContain('Ward 5');
    expect((await people('education', hr)).body.rows[0]).toMatchObject({ level: 'Bachelor', institution: 'Tribhuvan University' });
  });

  it('the licence register never shows ID numbers and flags expiring documents', async () => {
    await db.query(`UPDATE employee SET passport_no_enc = '\\x00'::bytea WHERE id = $1`, [DEMO.ram]);
    const r = await people('licence-register', hr);
    const row = r.body.rows.find((x: { emp_code: string; document: string }) => x.emp_code === 'E002' && x.document === 'Passport');
    expect(row).toMatchObject({ on_file: 'Recorded', state: 'Expiring soon' });
    expect(JSON.stringify(r.body)).not.toMatch(/passport_no|\\x00/);
  });

  it('record completeness lists what is missing, least complete first', async () => {
    const r = await people('record-completeness', hr);
    expect(r.body.rows[0].complete).toBeLessThanOrEqual(r.body.rows[r.body.rows.length - 1].complete);
    expect(r.body.rows.find((x: { emp_code: string }) => x.emp_code === 'E002').missing).toContain('Emergency contact');
  });

  it('CSV shows dates in Bikram Sambat and neutralises formula cells', async () => {
    await db.query(`UPDATE employee_education SET institution = '=HYPERLINK("x")' WHERE employee_id = $1`, [DEMO.sita]);
    const csv = await people('education', hr, '?format=csv');
    expect(csv.headers['content-type']).toContain('text/csv');
    expect(csv.text).toContain(`"'=HYPERLINK(""x"")"`);
    const jl = await people('joiners-leavers', hr, '?from=2024-04-01&to=2024-04-30&format=csv');
    expect(jl.text).toContain('1 Baishakh 2081');
  });

  it('filters by branch and department, and status', async () => {
    const other = (await db.query<{ id: string }>(`INSERT INTO branch (company_id, code, name_en) VALUES ($1, 'BRT', 'Biratnagar') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
    const none = await people('addresses', hr, `?branchId=${other}`);
    expect(none.body.rows).toEqual([]);
    const hq = await people('addresses', hr, `?branchId=${DEMO.branchId}`);
    expect(hq.body.rows.length).toBeGreaterThanOrEqual(2);
    expect((await people('addresses', hr, '?status=exited')).body.rows).toEqual([]);
  });

  it('managers get only the team reports, scoped to their team; employees get none; other companies see nothing', async () => {
    const hc = await people('headcount-on-date', ram);
    expect(hc.status).toBe(200);
    expect(hc.body.rows.reduce((s: number, r: { headcount: number }) => s + r.headcount, 0)).toBe(2); // Ram and Sita
    expect((await people('nominees', ram)).status).toBe(403);
    expect((await people('statistics', ram)).status).toBe(403);
    expect((await people('headcount-on-date', sita)).status).toBe(403);
    const foreign = await people('nominees', foreignHr);
    expect(foreign.body.rows).toEqual([]);
  });

  it('bad input is rejected', async () => {
    expect((await people('joiners-leavers', hr, '?from=2024-05-01&to=2024-04-01')).status).toBe(400);
    expect((await people('birthdays-anniversaries', hr, '?days=500')).status).toBe(400);
    expect((await people('addresses', hr, '?branchId=not-a-uuid')).status).toBe(400);
    expect((await people('nope', hr)).status).toBe(404);
  });
});

describe('ad-hoc report builder', () => {
  const run = (tok: string, body: object, q = '') => request(app).post(`/api/v1/reports/adhoc/run${q}`).set(auth(tok)).send(body);

  it('lists datasets with whitelisted columns', async () => {
    const r = await request(app).get('/api/v1/reports/adhoc/datasets').set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.datasets.map((d: { key: string }) => d.key)).toEqual(expect.arrayContaining(['employees', 'education', 'family', 'experience', 'career_events', 'leave_balances']));
    const emp = r.body.datasets.find((d: { key: string }) => d.key === 'employees');
    expect(emp.columns.map((c: { key: string }) => c.key)).not.toContain('pan');
  });

  it('runs with columns, filters and sort, and previews at most 100 rows', async () => {
    const r = await run(hr, { dataset: 'employees', columns: ['emp_code', 'name', 'age', 'province'], filters: [{ column: 'province', op: 'eq', value: 'bagmati' }], sort: { column: 'name', dir: 'desc' } });
    expect(r.status).toBe(200);
    expect(r.body.rows).toEqual([{ emp_code: 'E001', name: 'Sita Sharma', age: expect.any(Number), province: 'Bagmati' }]);
    expect(r.body.total).toBe(1);
    const all = await run(hr, { dataset: 'employees', columns: ['emp_code'], filters: [{ column: 'join_date', op: 'gte', value: '2023-01-01' }] });
    expect(all.body.rows.map((x: { emp_code: string }) => x.emp_code)).toEqual(['E001']);
    const nom = await run(hr, { dataset: 'family', columns: ['emp_code', 'member', 'nominee'], filters: [{ column: 'nominee', op: 'eq', value: true }] });
    expect(nom.body.rows).toEqual([{ emp_code: 'E001', member: 'Gita Sharma', nominee: true }]);
    const contains = await run(hr, { dataset: 'education', columns: ['institution'], filters: [{ column: 'institution', op: 'contains', value: '%' }] });
    expect(contains.body.rows).toEqual([]); // % is matched literally
  });

  it('refuses anything outside the whitelist (no SQL ever comes from the request)', async () => {
    const attempts = [
      { dataset: 'employees', columns: ['emp_code; DROP TABLE employee'] },
      { dataset: 'employees', columns: ['pan_enc'] },
      { dataset: 'app_user', columns: ['email'] },
      { dataset: 'employees', columns: ['emp_code'], sort: { column: 'name_en desc, (select 1)', dir: 'asc' } },
      { dataset: 'employees', columns: ['emp_code'], sort: { column: 'name', dir: 'desc; drop' } },
      { dataset: 'employees', columns: ['emp_code'], filters: [{ column: 'name', op: 'like', value: 'x' }] },
      { dataset: 'employees', columns: ['emp_code'], filters: [{ column: 'age', op: 'gt', value: "1 OR 1=1" }] },
      { dataset: 'employees', columns: ['emp_code'], filters: [{ column: 'dob', op: 'lt', value: "2020-01-01' OR '1'='1" }] },
      { dataset: 'employees', columns: ['emp_code'], extra: 'x' },
    ];
    for (const a of attempts) {
      const r = await run(hr, a);
      expect(r.status, JSON.stringify(a)).toBe(400);
    }
    const quoted = await run(hr, { dataset: 'employees', columns: ['emp_code'], filters: [{ column: 'name', op: 'eq', value: "x' OR '1'='1" }] });
    expect(quoted.status).toBe(200);
    expect(quoted.body.rows).toEqual([]);
    expect((await db.query('SELECT count(*)::int AS n FROM employee')).rows[0].n).toBeGreaterThan(0);
  });

  it('exports CSV with dates in BS and is HR only', async () => {
    const csv = await run(hr, { dataset: 'employees', columns: ['emp_code', 'join_date', 'pan_on_file'] }, '?format=csv');
    expect(csv.headers['content-type']).toContain('text/csv');
    expect(csv.text).toContain('Join date');
    expect(csv.text).toMatch(/E001,1 Baishakh 2081,No/);
    expect((await run(ram, { dataset: 'employees', columns: ['emp_code'] })).status).toBe(403);
    expect((await run(sita, { dataset: 'employees', columns: ['emp_code'] })).status).toBe(403);
    const foreign = await run(foreignHr, { dataset: 'employees', columns: ['emp_code'] });
    expect(foreign.body.rows).toEqual([]);
  });

  it('saves, shares, runs, updates and deletes named reports', async () => {
    const def = { dataset: 'education', columns: ['emp_code', 'institution'] };
    const mine = await request(app).post('/api/v1/reports/adhoc/saved').set(auth(hr)).send({ name: 'Degrees', definition: def });
    expect(mine.status).toBe(201);
    const shared = await request(app).post('/api/v1/reports/adhoc/saved').set(auth(hr)).send({ name: 'Shared degrees', shared: true, definition: def });
    const bad = await request(app).post('/api/v1/reports/adhoc/saved').set(auth(hr)).send({ name: 'Bad', definition: { dataset: 'employees', columns: ['nope'] } });
    expect(bad.status).toBe(201); // saved definitions are validated again when run
    expect((await request(app).get(`/api/v1/reports/adhoc/saved/${bad.body.report.id}/run`).set(auth(hr))).status).toBe(400);

    const list2 = await request(app).get('/api/v1/reports/adhoc/saved').set(auth(hr2));
    expect(list2.body.reports.map((x: { name: string }) => x.name)).toEqual(['Shared degrees']);
    expect((await request(app).get(`/api/v1/reports/adhoc/saved/${mine.body.report.id}/run`).set(auth(hr2))).status).toBe(404);
    const ranShared = await request(app).get(`/api/v1/reports/adhoc/saved/${shared.body.report.id}/run`).set(auth(hr2));
    expect(ranShared.status).toBe(200);
    expect(ranShared.body.rows.length).toBeGreaterThan(0);
    expect((await request(app).put(`/api/v1/reports/adhoc/saved/${shared.body.report.id}`).set(auth(hr2)).send({ name: 'Mine now', definition: def })).status).toBe(403);
    expect((await request(app).delete(`/api/v1/reports/adhoc/saved/${shared.body.report.id}`).set(auth(hr2))).status).toBe(403);
    expect((await request(app).get(`/api/v1/reports/adhoc/saved/${shared.body.report.id}/run`).set(auth(foreignHr))).status).toBe(404);

    const upd = await request(app).put(`/api/v1/reports/adhoc/saved/${mine.body.report.id}`).set(auth(hr)).send({ name: 'Degrees v2', shared: false, definition: { ...def, columns: ['emp_code'] } });
    expect(upd.body.report).toMatchObject({ name: 'Degrees v2' });
    expect((await request(app).delete(`/api/v1/reports/adhoc/saved/${mine.body.report.id}`).set(auth(hr))).status).toBe(200);
    expect((await request(app).get('/api/v1/reports/adhoc/saved').set(auth(hr))).body.reports.map((x: { name: string }) => x.name)).not.toContain('Degrees v2');
  });

  it('the older attendance reports still answer under /reports', async () => {
    expect((await request(app).get('/api/v1/reports/headcount').set(auth(hr))).status).toBe(200);
    expect((await request(app).get('/api/v1/reports/headcount').set(auth(sita))).status).toBe(403);
  });
});
