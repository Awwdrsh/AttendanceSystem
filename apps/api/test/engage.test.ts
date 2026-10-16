import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { adToBs, bsToAd } from '@hajiri/bs-calendar';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { runSurveyJobs } from '../src/modules/engage/surveys.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher, TEST_PEPPER } from './setup-db.js';

/** Engagement: announcements, kudos, celebrations, surveys and polls, suggestion box; and HR insights. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hrUserId: string; let sita: string; let ram: string; let foreignHr: string;
const extra: Array<{ id: string; token: string }> = [];
let deptA: string; let deptB: string;
const today = nptToday();
const addDays = (ad: string, n: number) => new Date(Date.parse(`${ad}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);

async function employee(code: string, name: string, unit: string | null, managerId: string | null = null, extraCols: Record<string, unknown> = {}) {
  const cols = Object.keys(extraCols); const vals = Object.values(extraCols);
  const e = await db.query<{ id: string }>(
    `INSERT INTO employee (company_id, emp_code, name_en${cols.map((c) => `, ${c}`).join('')}) VALUES ($1, $2, $3${cols.map((_, i) => `, $${i + 4}`).join('')}) RETURNING id`,
    [DEMO.companyId, code, name, ...vals]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type) VALUES ($1, daterange('2020-01-01', NULL), $2, $3, $4, 'regular')`,
    [e.rows[0]!.id, DEMO.branchId, unit, managerId]);
  return e.rows[0]!.id;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  deptA = (await db.query<{ id: string }>(`INSERT INTO org_unit (company_id, kind, code, name_en) VALUES ($1, 'department', 'ENG', 'Engineering') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  deptB = (await db.query<{ id: string }>(`INSERT INTO org_unit (company_id, kind, code, name_en) VALUES ($1, 'department', 'OPS', 'Operations') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  const h = await loginAs(db, app, ['hr']); hr = h.token; hrUserId = h.userId;
  await db.query('UPDATE employee_assignment SET org_unit_id = $2, manager_id = $3 WHERE employee_id = $1', [DEMO.sita, deptA, DEMO.ram]);
  await db.query('UPDATE employee_assignment SET org_unit_id = $2 WHERE employee_id = $1', [DEMO.ram, deptA]);
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  for (let i = 0; i < 5; i++) {
    const id = await employee(`E10${i}`, `Person ${i}`, i < 3 ? deptA : deptB, i === 0 ? DEMO.ram : null);
    extra.push({ id, token: (await loginAs(db, app, ['employee'], id)).token });
  }
  const c = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERENG', 'Other Co') RETURNING id`)).rows[0]!.id;
  await createUser(db, { companyId: c, email: 'hr@other.engage', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.engage', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('announcements', () => {
  let annId: string;
  it('HR posts to everyone; people are notified and see it in their feed', async () => {
    const r = await request(app).post('/api/v1/engage/announcements').set(auth(hr)).send({ title: 'Dashain holidays', body: 'Office closed from 1 to 5 Kartik.', pinned: true, requiresAck: true });
    expect(r.status).toBe(201);
    annId = r.body.id;
    expect(r.body.notified).toBeGreaterThanOrEqual(7);
    const feed = await request(app).get('/api/v1/engage/feed').set(auth(sita));
    expect(feed.body.items.find((i: { id: string }) => i.id === annId)).toMatchObject({ type: 'announcement', pinned: true, requiresAck: true, acked: false, canEdit: false });
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'engage.announcement' AND payload->>'announcementId' = $1`, [annId]);
    expect(n.rows[0].n).toBeGreaterThanOrEqual(7);
    const home = await request(app).get('/api/v1/engage/home').set(auth(sita));
    expect(home.body.announcement.id).toBe(annId);
  });

  it('tracks who confirmed reading; only the author or HR sees the list', async () => {
    expect((await request(app).post(`/api/v1/engage/announcements/${annId}/ack`).set(auth(sita))).status).toBe(200);
    const acks = await request(app).get(`/api/v1/engage/announcements/${annId}/acks`).set(auth(hr));
    expect(acks.status).toBe(200);
    expect(acks.body.acked).toBe(1);
    expect(acks.body.people[acks.body.people.length - 1]).toMatchObject({ employeeId: DEMO.sita });
    expect((await request(app).get(`/api/v1/engage/announcements/${annId}/acks`).set(auth(ram))).status).toBe(403);
    expect((await request(app).patch(`/api/v1/engage/announcements/${annId}`).set(auth(ram)).send({ title: 'x' })).status).toBe(403);
  });

  it('managers post only to their own team, and only that team sees it', async () => {
    expect((await request(app).post('/api/v1/engage/announcements').set(auth(ram)).send({ title: 'All hands', body: 'x' })).status).toBe(403);
    expect((await request(app).post('/api/v1/engage/announcements').set(auth(sita)).send({ title: 'Hi', body: 'x', audienceKind: 'reports' })).status).toBe(403);
    const r = await request(app).post('/api/v1/engage/announcements').set(auth(ram)).send({ title: 'Team lunch', body: 'Friday 1 pm', audienceKind: 'reports', pinned: true });
    expect(r.status).toBe(201);
    expect(r.body.notified).toBe(2); // Sita and Person 0
    const mine = await request(app).get('/api/v1/engage/announcements').set(auth(extra[0]!.token));
    expect(mine.body.announcements.map((a: { id: string }) => a.id)).toContain(r.body.id);
    expect(mine.body.announcements.find((a: { id: string }) => a.id === r.body.id).pinned).toBe(false); // only HR pins
    const other = await request(app).get('/api/v1/engage/announcements').set(auth(extra[1]!.token));
    expect(other.body.announcements.map((a: { id: string }) => a.id)).not.toContain(r.body.id);
    expect((await request(app).post(`/api/v1/engage/announcements/${r.body.id}/ack`).set(auth(extra[1]!.token))).status).toBe(404);
  });

  it('department audiences reach that department only; other companies never see it', async () => {
    const r = await request(app).post('/api/v1/engage/announcements').set(auth(hr)).send({ title: 'Ops drill', body: 'Fire drill at 3', audienceKind: 'department', audienceId: deptB });
    expect(r.status).toBe(201);
    expect(r.body.notified).toBe(2);
    const inB = await request(app).get('/api/v1/engage/announcements').set(auth(extra[4]!.token));
    expect(inB.body.announcements.map((a: { id: string }) => a.id)).toContain(r.body.id);
    const inA = await request(app).get('/api/v1/engage/announcements').set(auth(sita));
    expect(inA.body.announcements.map((a: { id: string }) => a.id)).not.toContain(r.body.id);
    const foreign = await request(app).get('/api/v1/engage/announcements').set(auth(foreignHr));
    expect(foreign.body.announcements).toHaveLength(0);
    expect((await request(app).post('/api/v1/engage/announcements').set(auth(foreignHr)).send({ title: 'x', body: 'y', audienceKind: 'department', audienceId: deptB })).status).toBe(400);
  });
});

describe('kudos', () => {
  let kudosId: string;
  it('needs a colleague, never yourself, and notifies the recipients', async () => {
    const values = await request(app).get('/api/v1/engage/values').set(auth(sita));
    expect(values.body.values.length).toBeGreaterThanOrEqual(5);
    const self = await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [DEMO.sita], message: 'Me!' });
    expect(self.status).toBe(400);
    const ok = await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [DEMO.ram, extra[0]!.id], valueId: values.body.values[0].id, message: 'Thanks for the release help' });
    expect(ok.status).toBe(201);
    kudosId = ok.body.id;
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'engage.kudos' AND channel = 'in_app'`);
    expect(n.rows[0].n).toBe(2);
    const feed = await request(app).get('/api/v1/engage/kudos').set(auth(extra[3]!.token));
    expect(feed.body.kudos[0]).toMatchObject({ id: kudosId, canDelete: false, giver: { employeeId: DEMO.sita } });
    expect(feed.body.kudos[0].recipients).toHaveLength(2);
    const foreign = await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [DEMO.ram, '00000000-0000-7000-8000-00000000ffff'], message: 'Hello there' });
    expect(foreign.status).toBe(400);
    expect((await request(app).post('/api/v1/engage/kudos').set(auth(hr)).send({ recipientIds: [DEMO.ram], message: 'From HR acct' })).status).toBe(403);
  });

  it('reactions toggle and comments notify the people involved', async () => {
    const on = await request(app).post('/api/v1/engage/reactions').set(auth(ram)).send({ targetType: 'kudos', targetId: kudosId, emoji: 'clap' });
    expect(on.body).toMatchObject({ reacted: true, reactions: { clap: 1 }, mine: ['clap'] });
    const off = await request(app).post('/api/v1/engage/reactions').set(auth(ram)).send({ targetType: 'kudos', targetId: kudosId, emoji: 'clap' });
    expect(off.body).toMatchObject({ reacted: false, reactions: {} });
    expect((await request(app).post('/api/v1/engage/reactions').set(auth(ram)).send({ targetType: 'kudos', targetId: kudosId, emoji: 'poop' })).status).toBe(400);
    const c = await request(app).post('/api/v1/engage/comments').set(auth(extra[2]!.token)).send({ targetType: 'kudos', targetId: kudosId, body: 'Well deserved!' });
    expect(c.status).toBe(201);
    const list = await request(app).get('/api/v1/engage/comments').query({ targetType: 'kudos', targetId: kudosId }).set(auth(sita));
    expect(list.body.comments[0]).toMatchObject({ body: 'Well deserved!', canDelete: false });
    expect((await request(app).delete(`/api/v1/engage/comments/${c.body.id}`).set(auth(sita))).status).toBe(403);
    expect((await request(app).delete(`/api/v1/engage/comments/${c.body.id}`).set(auth(extra[2]!.token))).status).toBe(200);
    expect((await request(app).get('/api/v1/engage/comments').query({ targetType: 'kudos', targetId: kudosId }).set(auth(foreignHr))).status).toBe(404);
  });

  it('enforces the monthly limit, and the leaderboard counts the month', async () => {
    expect((await request(app).put('/api/v1/engage/settings').set(auth(sita)).send({ kudosMonthlyLimit: 2 })).status).toBe(403);
    expect((await request(app).put('/api/v1/engage/settings').set(auth(hr)).send({ kudosMonthlyLimit: 2 })).status).toBe(200);
    expect((await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [extra[1]!.id], message: 'Second one' })).status).toBe(201);
    const third = await request(app).post('/api/v1/engage/kudos').set(auth(sita)).send({ recipientIds: [extra[1]!.id], message: 'Third one' });
    expect(third.status).toBe(409);
    const lb = await request(app).get('/api/v1/engage/leaderboard').set(auth(ram));
    expect(lb.body.total).toBe(2);
    expect(lb.body.given[0]).toMatchObject({ employeeId: DEMO.sita, count: 2 });
    await request(app).put('/api/v1/engage/settings').set(auth(hr)).send({ kudosMonthlyLimit: null });
  });

  it('only the giver or HR removes kudos', async () => {
    expect((await request(app).delete(`/api/v1/engage/kudos/${kudosId}`).set(auth(ram))).status).toBe(403);
    expect((await request(app).delete(`/api/v1/engage/kudos/${kudosId}`).set(auth(hr))).status).toBe(200);
  });

  it('values are managed by HR', async () => {
    expect((await request(app).post('/api/v1/engage/values').set(auth(sita)).send({ name: 'Speed' })).status).toBe(403);
    const v = await request(app).post('/api/v1/engage/values').set(auth(hr)).send({ name: 'Speed', emoji: '⚡' });
    expect(v.status).toBe(201);
    expect((await request(app).patch(`/api/v1/engage/values/${v.body.id}`).set(auth(hr)).send({ active: false })).status).toBe(200);
    expect((await request(app).post('/api/v1/engage/kudos').set(auth(ram)).send({ recipientIds: [DEMO.sita], valueId: v.body.id, message: 'Fast work' })).status).toBe(400);
  });
});

describe('celebrations', () => {
  it('shows birthdays only for people who opted in; anniversaries and joiners always', async () => {
    const bs = adToBs(today);
    const dob = bsToAd({ year: bs.year - 30, month: bs.month, day: bs.day });
    const joined = bsToAd({ year: bs.year - 3, month: bs.month, day: bs.day });
    await db.query('UPDATE employee SET dob = $2, join_date = $3 WHERE id = $1', [DEMO.sita, dob, joined]);
    await db.query('UPDATE employee SET join_date = $2 WHERE id = $1', [extra[4]!.id, addDays(today, -3)]);
    let c = await request(app).get('/api/v1/engage/celebrations').set(auth(ram));
    expect(c.body.celebrations.find((x: { kind: string }) => x.kind === 'birthday')).toBeUndefined();
    expect(c.body.celebrations.find((x: { kind: string; employeeId: string }) => x.kind === 'anniversary' && x.employeeId === DEMO.sita)).toMatchObject({ years: 3, daysAway: 0 });
    expect(c.body.celebrations.find((x: { kind: string; employeeId: string }) => x.kind === 'joined' && x.employeeId === extra[4]!.id)).toBeTruthy();
    const hidden = await request(app).post('/api/v1/engage/comments').set(auth(ram)).send({ targetType: 'celebration', targetId: `birthday:${DEMO.sita}:${bs.year}`, body: 'Happy birthday' });
    expect(hidden.status).toBe(404);

    expect((await request(app).get('/api/v1/engage/me/prefs').set(auth(sita))).body).toEqual({ showBirthday: false, hasBirthday: true });
    expect((await request(app).put('/api/v1/engage/me/prefs').set(auth(sita)).send({ showBirthday: true })).status).toBe(200);
    c = await request(app).get('/api/v1/engage/celebrations').set(auth(ram));
    const bday = c.body.celebrations.find((x: { kind: string }) => x.kind === 'birthday');
    expect(bday).toMatchObject({ employeeId: DEMO.sita, daysAway: 0 });
    expect(bday.years).toBeNull(); // age is never shown
    const wish = await request(app).post('/api/v1/engage/comments').set(auth(ram)).send({ targetType: 'celebration', targetId: bday.targetId, body: 'Happy birthday!' });
    expect(wish.status).toBe(201);
    const home = await request(app).get('/api/v1/engage/home').set(auth(extra[2]!.token));
    expect(home.body.celebrations.map((x: { kind: string }) => x.kind)).toEqual(expect.arrayContaining(['birthday', 'anniversary']));
  });
});

describe('surveys and polls', () => {
  let surveyId: string; let pollId: string;
  const questions = [
    { kind: 'enps', prompt: 'How likely are you to recommend CoreValley as a place to work?' },
    { kind: 'rating', prompt: 'I have what I need to do my job well.' },
    { kind: 'multi', prompt: 'What would help most?', options: ['Training', 'Tools', 'Flexibility'] },
    { kind: 'text', prompt: 'Anything else?', required: false },
  ];

  it('only HR creates surveys, and the shape is validated', async () => {
    expect((await request(app).post('/api/v1/engage/surveys').set(auth(sita)).send({ title: 'x', opensOn: today, closesOn: today, questions })).status).toBe(403);
    const badPoll = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({ kind: 'poll', title: 'x', opensOn: today, closesOn: today, questions });
    expect(badPoll.status).toBe(400);
    const badChoice = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({ title: 'x', opensOn: today, closesOn: today, questions: [{ kind: 'single', prompt: 'Pick', options: ['Only one'] }] });
    expect(badChoice.status).toBe(400);
    const r = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({ title: 'Pulse', anonymous: true, minGroup: 3, opensOn: today, closesOn: addDays(today, 7), questions });
    expect(r.status).toBe(201);
    surveyId = r.body.id;
    // A draft is invisible to employees.
    expect((await request(app).get(`/api/v1/engage/surveys/${surveyId}`).set(auth(sita))).status).toBe(404);
    const pub = await request(app).post(`/api/v1/engage/surveys/${surveyId}/publish`).set(auth(hr));
    expect(pub.status).toBe(200);
    expect(pub.body.notified).toBeGreaterThanOrEqual(7);
    expect((await request(app).put(`/api/v1/engage/surveys/${surveyId}`).set(auth(hr)).send({ title: 'Changed', opensOn: today, closesOn: today, questions })).status).toBe(400);
  });

  it('anonymous answers are not linked to anyone, and results stay hidden below the minimum group', async () => {
    const s = await request(app).get(`/api/v1/engage/surveys/${surveyId}`).set(auth(sita));
    expect(s.status).toBe(200);
    const q = s.body.questions as Array<{ id: string; kind: string }>;
    const answer = (enps: number, rating: number, text?: string) => ({ answers: [
      { questionId: q[0]!.id, value: enps }, { questionId: q[1]!.id, value: rating }, { questionId: q[2]!.id, choices: ['Tools'] },
      ...(text ? [{ questionId: q[3]!.id, text }] : []),
    ] });
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(sita)).send({ answers: [{ questionId: q[0]!.id, value: 11 }] })).status).toBe(400);
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(sita)).send({ answers: [{ questionId: q[0]!.id, value: 9 }] })).status).toBe(400); // required missing
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(sita)).send(answer(10, 5, 'Great team'))).status).toBe(201);
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(sita)).send(answer(10, 5))).status).toBe(409);
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(ram)).send(answer(3, 2, 'More tools please'))).status).toBe(201);
    let res = await request(app).get(`/api/v1/engage/surveys/${surveyId}/results`).set(auth(hr));
    expect(res.body.results).toMatchObject({ responses: 2, hidden: true, questions: [] });
    expect((await request(app).get(`/api/v1/engage/surveys/${surveyId}/results`).set(auth(sita))).status).toBe(404);
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/respond`).set(auth(extra[0]!.token)).send(answer(8, 4))).status).toBe(201);
    res = await request(app).get(`/api/v1/engage/surveys/${surveyId}/results`).set(auth(hr));
    expect(res.body.results).toMatchObject({ responses: 3, hidden: true, hiddenUntilClose: true, questions: [] });
    await db.query(`UPDATE survey SET status = 'closed' WHERE id = $1`, [surveyId]);   // results of an anonymous survey show once it has closed
    res = await request(app).get(`/api/v1/engage/surveys/${surveyId}/results`).set(auth(hr));
    expect(res.body.results.hidden).toBe(false);
    const enps = res.body.results.questions[0];
    expect(enps.enps).toEqual({ score: 0, promoters: 1, passives: 1, detractors: 1 });
    expect(res.body.results.questions[1].average).toBeCloseTo(3.67, 2);
    expect(res.body.results.questions[2].distribution).toEqual([{ label: 'Training', count: 0 }, { label: 'Tools', count: 3 }, { label: 'Flexibility', count: 0 }]);
    const texts = res.body.results.questions[3].texts as Array<Record<string, unknown>>;
    expect(texts).toHaveLength(2);
    for (const t of texts) expect(Object.keys(t)).toEqual(['text']);
    // All three are in Engineering, which reaches the minimum; a single group gives no breakdown.
    expect(enps.breakdown).toEqual([]);
    const stored = await db.query('SELECT employee_id, respondent_key FROM survey_response WHERE survey_id = $1', [surveyId]);
    expect(stored.rows.every((r) => r.employee_id === null)).toBe(true);
    expect(stored.rows.some((r) => r.respondent_key.includes(DEMO.sita))).toBe(false);
    const csv = await request(app).get(`/api/v1/engage/surveys/${surveyId}/results.csv`).set(auth(hr));
    expect(csv.status).toBe(200);
    expect(csv.text).toContain('More tools please');
    expect(csv.text).not.toContain('Sita');
    await db.query(`UPDATE survey SET status = 'published' WHERE id = $1`, [surveyId]);
    const list = await request(app).get('/api/v1/engage/surveys').set(auth(sita));
    expect(list.body.surveys.find((x: { id: string }) => x.id === surveyId).answered).toBe(true);
    expect((await request(app).get(`/api/v1/engage/surveys/${surveyId}/results`).set(auth(foreignHr))).status).toBe(404);
  });

  it('a poll shows results to people who voted, and audiences are respected', async () => {
    const p = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({
      kind: 'poll', title: 'Friday snacks', anonymous: false, audienceKind: 'department', audienceId: deptB, opensOn: today, closesOn: today,
      questions: [{ kind: 'single', prompt: 'Which snack?', options: ['Momo', 'Samosa'] }] });
    pollId = p.body.id;
    await request(app).post(`/api/v1/engage/surveys/${pollId}/publish`).set(auth(hr));
    const q = (await request(app).get(`/api/v1/engage/surveys/${pollId}`).set(auth(extra[3]!.token))).body.questions[0];
    expect((await request(app).get(`/api/v1/engage/surveys/${pollId}`).set(auth(sita))).status).toBe(404);
    expect((await request(app).post(`/api/v1/engage/surveys/${pollId}/respond`).set(auth(sita)).send({ answers: [{ questionId: q.id, choices: ['Momo'] }] })).status).toBe(404);
    expect((await request(app).get(`/api/v1/engage/surveys/${pollId}/results`).set(auth(extra[3]!.token))).status).toBe(404);
    const v = await request(app).post(`/api/v1/engage/surveys/${pollId}/respond`).set(auth(extra[3]!.token)).send({ answers: [{ questionId: q.id, choices: ['Momo'] }] });
    expect(v.status).toBe(201);
    expect(v.body.results.questions[0].distribution[0]).toEqual({ label: 'Momo', count: 1 });
    expect((await request(app).post(`/api/v1/engage/surveys/${pollId}/respond`).set(auth(extra[4]!.token)).send({ answers: [{ questionId: q.id, choices: ['Pizza'] }] })).status).toBe(400);
    const stored = await db.query('SELECT employee_id FROM survey_response WHERE survey_id = $1', [pollId]);
    expect(stored.rows[0].employee_id).toBe(extra[3]!.id); // not anonymous: the name is kept
  });

  it('the daily job closes, repeats a recurring pulse, and reminds people who have not answered', async () => {
    const r = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({
      title: 'Weekly pulse', recurrence: 'weekly', opensOn: addDays(today, -6), closesOn: addDays(today, 1), questions: [{ kind: 'rating', prompt: 'How was your week?' }] });
    await request(app).post(`/api/v1/engage/surveys/${r.body.id}/publish`).set(auth(hr));
    const q = (await request(app).get(`/api/v1/engage/surveys/${r.body.id}`).set(auth(sita))).body.questions[0];
    await request(app).post(`/api/v1/engage/surveys/${r.body.id}/respond`).set(auth(sita)).send({ answers: [{ questionId: q.id, value: 4 }] });
    const first = await runSurveyJobs(db, testCipher, today);
    expect(first.reminded).toBe(6); // 7 in the audience, Sita already answered
    const again = await runSurveyJobs(db, testCipher, today);
    expect(again.reminded).toBe(0);
    const later = await runSurveyJobs(db, testCipher, addDays(today, 2));
    expect(later.closed).toBeGreaterThanOrEqual(1);
    expect(later.started).toBe(1);
    const next = await db.query<{ id: string; opens_on: string; closes_on: string; status: string; series_id: string }>(
      `SELECT id, opens_on, closes_on, status, series_id FROM survey WHERE series_id = $1`, [r.body.id]);
    expect(next.rows[0]).toMatchObject({ status: 'published', opens_on: addDays(today, 1), closes_on: addDays(today, 8) });
    expect((await db.query('SELECT count(*)::int AS n FROM survey_question WHERE survey_id = $1', [next.rows[0]!.id])).rows[0].n).toBe(1);
    expect(later.opened).toBeGreaterThanOrEqual(1);
  });

  it('closing by hand stops the series; drafts can be deleted', async () => {
    const d = await request(app).post('/api/v1/engage/surveys').set(auth(hr)).send({ title: 'Draft', opensOn: today, closesOn: today, questions: [{ kind: 'text', prompt: 'Hi?' }] });
    expect((await request(app).delete(`/api/v1/engage/surveys/${d.body.id}`).set(auth(hr))).status).toBe(200);
    expect((await request(app).post(`/api/v1/engage/surveys/${surveyId}/close`).set(auth(hr))).status).toBe(200);
    expect((await request(app).delete(`/api/v1/engage/surveys/${surveyId}`).set(auth(hr))).status).toBe(409);
    const manage = await request(app).get('/api/v1/engage/surveys/manage').set(auth(hr));
    expect(manage.body.surveys.find((s: { id: string }) => s.id === surveyId)).toMatchObject({ status: 'closed', responses: 3 });
    expect((await request(app).get('/api/v1/engage/surveys/manage').set(auth(ram))).status).toBe(403);
  });
});

describe('suggestion box', () => {
  it('stores no author, HR replies and can share the answer', async () => {
    expect((await request(app).post('/api/v1/engage/suggestions').set(auth(sita)).send({ category: 'workplace', body: 'Could we get a water filter on the second floor?' })).status).toBe(201);
    const cols = await db.query(`SELECT column_name FROM information_schema.columns WHERE table_name = 'suggestion'`);
    const names = cols.rows.map((c) => c.column_name);
    expect(names).not.toContain('employee_id');
    expect(names).not.toContain('created_at');
    expect((await request(app).get('/api/v1/engage/suggestions').set(auth(sita))).status).toBe(403);
    const list = await request(app).get('/api/v1/engage/suggestions').set(auth(hr));
    expect(list.body.suggestions).toHaveLength(1);
    const id = list.body.suggestions[0].id;
    expect((await request(app).patch(`/api/v1/engage/suggestions/${id}`).set(auth(hr)).send({ reply: 'Ordered, arriving next week.', isPublic: true })).status).toBe(200);
    const pub = await request(app).get('/api/v1/engage/suggestions/public').set(auth(ram));
    expect(pub.body.suggestions[0]).toMatchObject({ reply: 'Ordered, arriving next week.' });
    for (let i = 0; i < 5; i++) await request(app).post('/api/v1/engage/suggestions').set(auth(ram)).send({ body: `Idea number ${i}` });
    expect((await request(app).post('/api/v1/engage/suggestions').set(auth(ram)).send({ body: 'One idea too many' })).status).toBe(409);
    expect((await request(app).get('/api/v1/engage/suggestions/public').set(auth(foreignHr))).body.suggestions).toHaveLength(0);
    void hrUserId;
  });
});

describe('insights', () => {
  it('is for HR only', async () => {
    expect((await request(app).get('/api/v1/insights').set(auth(ram))).status).toBe(403);
    expect((await request(app).get('/api/v1/insights/filters').set(auth(sita))).status).toBe(403);
  });

  it('counts headcount, joins and exits by BS month and filters by department', async () => {
    const bs = adToBs(today);
    const lastMonthDay = bsToAd({ year: bs.month === 1 ? bs.year - 1 : bs.year, month: bs.month === 1 ? 12 : bs.month - 1, day: 10 });
    await db.query(`UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL`);
    await db.query(`UPDATE employee SET status = 'exited', exit_date = $2 WHERE id = $1`, [extra[1]!.id, today]);
    await db.query(`UPDATE employee SET join_date = $2, gender = 'female', dob = '1995-01-01' WHERE id = $1`, [extra[2]!.id, lastMonthDay]);
    const r = await request(app).get('/api/v1/insights').set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.body.months).toHaveLength(12);
    const cur = r.body.headcount[r.body.headcount.length - 1];
    const prev = r.body.headcount[r.body.headcount.length - 2];
    expect(cur.exits).toBe(1);
    expect(cur.joins).toBe(1); // Person 4 joined three days ago
    expect(prev.joins).toBe(1);
    expect(r.body.kpis.find((k: { key: string }) => k.key === 'headcount').value).toBe(cur.headcount);
    expect(r.body.gender.find((g: { label: string }) => g.label === 'Female').count).toBeGreaterThanOrEqual(1);
    expect(r.body.attrition.split).toBeNull();
    const eng = await request(app).get('/api/v1/insights').query({ orgUnitId: deptA }).set(auth(hr));
    expect(eng.body.departments.map((d: { label: string }) => d.label)).toEqual(['Engineering']);
    expect((await request(app).get('/api/v1/insights').query({ orgUnitId: deptA }).set(auth(foreignHr))).status).toBe(400);
    expect((await request(app).get('/api/v1/insights').query({ from: '2083-13' }).set(auth(hr))).status).toBe(400);
    expect((await request(app).get('/api/v1/insights').query({ from: '2070-01', to: '2083-12' }).set(auth(hr))).status).toBe(400);
  });

  it('exports each dataset as CSV and refuses unknown names', async () => {
    const csv = await request(app).get('/api/v1/insights/export/headcount').set(auth(hr));
    expect(csv.status).toBe(200);
    expect(csv.headers['content-type']).toContain('text/csv');
    expect(csv.text).toContain('Headcount at month end');
    expect((await request(app).get('/api/v1/insights/export/constructor').set(auth(hr))).status).toBe(400);
    expect((await request(app).get('/api/v1/insights/export/attendance').set(auth(hr))).status).toBe(200);
  });

  it('splits voluntary and involuntary exits when exit reasons exist', async () => {
    await db.query(`ALTER TABLE employee ADD COLUMN IF NOT EXISTS separation_type text`);
    await db.query(`UPDATE employee SET separation_type = 'resignation' WHERE id = $1`, [extra[1]!.id]);
    const r = await request(app).get('/api/v1/insights').set(auth(hr));
    expect(r.body.attrition.split).toEqual({ voluntary: 1, involuntary: 0, other: 0 });
    expect(r.body.headcount[r.body.headcount.length - 1].voluntary).toBe(1);
    await db.query('ALTER TABLE employee DROP COLUMN separation_type');
  });
});
