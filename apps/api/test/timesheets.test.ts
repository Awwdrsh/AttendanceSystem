import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { autoStopStaleTimers, fridayTimesheetReminders, roundEntry, weekStartOf } from '../src/modules/timesheets/service.js';
import { DAILY_MODULE_JOBS } from '../src/module-jobs.js';
import { auth, DEMO, freshDb, loginAs, TEST_PEPPER, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let ram: string; let other: string; let outsider: string;
let hari: string;
const HARI = '00000000-0000-7000-8000-0000000000a1';
const OTHER_CO = '00000000-0000-7000-8000-0000000000c1';
const OTHER_EMP = '00000000-0000-7000-8000-0000000000c2';

const todayNpt = () => new Date(Date.now() + 345 * 60_000).toISOString().slice(0, 10);
const addDays = (iso: string, n: number) => new Date(new Date(`${iso}T00:00:00Z`).getTime() + n * 86_400_000).toISOString().slice(0, 10);
const WEEK = weekStartOf(addDays(todayNpt(), -14));   // a past Sunday
const MON = addDays(WEEK, 1); const TUE = addDays(WEEK, 2);

let projA = ''; let projPrivate = ''; let actGlobal = ''; let actA = ''; let client = '';

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['manager'], DEMO.ram)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  // Hari works in the same company but not for Ram.
  await db.query(`INSERT INTO employee (id, company_id, emp_code, name_en) VALUES ($1, $2, 'E099', 'Hari Karki')`, [HARI, DEMO.companyId]);
  hari = (await loginAs(db, app, ['employee'], HARI)).token;
  other = (await loginAs(db, app, ['manager'])).token; // a manager with no employee record and no team
  // Another company with its own HR user.
  await db.query(`INSERT INTO company (id, code, name_en) VALUES ($1, 'OTH', 'Other Co')`, [OTHER_CO]);
  await db.query(`INSERT INTO employee (id, company_id, emp_code, name_en) VALUES ($1, $2, 'X001', 'Outsider')`, [OTHER_EMP, OTHER_CO]);
  await createUser(db, { companyId: OTHER_CO, employeeId: OTHER_EMP, email: 'hr@other.test', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  outsider = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.test', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('clients, projects and activities', () => {
  it('only HR manages the catalogue', async () => {
    expect((await request(app).post('/api/v1/time/clients').set(auth(sita)).send({ name: 'Acme' })).status).toBe(403);
    expect((await request(app).post('/api/v1/time/projects').set(auth(ram)).send({ code: 'X', name: 'X' })).status).toBe(403);
    const c = await request(app).post('/api/v1/time/clients').set(auth(hr)).send({ name: 'Acme Hydro' });
    expect(c.status).toBe(201);
    client = c.body.client.id;
    expect((await request(app).post('/api/v1/time/clients').set(auth(hr)).send({ name: 'acme hydro' })).status).toBe(409);
  });

  it('creates projects with members, rates and budgets; validates codes and clients', async () => {
    const a = await request(app).post('/api/v1/time/projects').set(auth(hr)).send({ code: 'hydro-1', name: 'Hydro survey', clientId: client, billRate: 2000, budgetHours: 10 });
    expect(a.status).toBe(201);
    projA = a.body.id;
    const p = await request(app).post('/api/v1/time/projects').set(auth(hr)).send({ code: 'SECRET', name: 'Internal R&D', allMembers: false, memberIds: [DEMO.ram], billable: false });
    expect(p.status).toBe(201);
    projPrivate = p.body.id;
    expect((await request(app).post('/api/v1/time/projects').set(auth(hr)).send({ code: 'bad code!', name: 'x' })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/projects').set(auth(hr)).send({ code: 'HYDRO-1', name: 'dup' })).status).toBe(409);
    expect((await request(app).post('/api/v1/time/projects').set(auth(hr)).send({ code: 'NOPE', name: 'x', memberIds: [OTHER_EMP], allMembers: false })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/projects').set(auth(outsider)).send({ code: 'Z', name: 'z', clientId: client })).status).toBe(400);
    const g = await request(app).post('/api/v1/time/activities').set(auth(hr)).send({ name: 'Meetings' });
    actGlobal = g.body.activity.id;
    const s = await request(app).post('/api/v1/time/activities').set(auth(hr)).send({ name: 'Field work', projectId: projA });
    actA = s.body.activity.id;
    expect(s.status).toBe(201);
  });

  it('employees see only projects they may use; other companies see nothing', async () => {
    const mine = await request(app).get('/api/v1/time/projects').set(auth(sita));
    expect(mine.body.projects.map((p: { code: string }) => p.code)).toEqual(['HYDRO-1']);
    expect(mine.body.projects[0].members).toEqual([]);
    const ramP = await request(app).get('/api/v1/time/projects').set(auth(ram));
    expect(ramP.body.projects).toHaveLength(2);
    const hrAll = await request(app).get('/api/v1/time/projects?all=1').set(auth(hr));
    expect(hrAll.body.projects.find((p: { id: string }) => p.id === projPrivate).members).toHaveLength(1);
    expect((await request(app).get('/api/v1/time/projects').set(auth(outsider))).body.projects).toHaveLength(0);
    expect((await request(app).patch(`/api/v1/time/projects/${projA}`).set(auth(outsider)).send({ name: 'hijack' })).status).toBe(404);
    const acts = await request(app).get(`/api/v1/time/activities?projectId=${projA}`).set(auth(sita));
    expect(acts.body.activities).toHaveLength(2);
  });
});

describe('manual entries', () => {
  it('logs time with start/end or duration and defaults billable from the project', async () => {
    const a = await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, start: '09:00', end: '12:30', projectId: projA, activityId: actA, note: 'Site visit' });
    expect(a.status).toBe(201);
    expect(a.body.entry).toMatchObject({ duration_min: 210, billable: true, project_code: 'HYDRO-1', activity_name: 'Field work', source: 'manual' });
    const b = await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, durationMin: 95, activityId: actGlobal });
    expect(b.status).toBe(201);
    expect(b.body.entry).toMatchObject({ duration_min: 95, billable: false, started_at: null });
  });

  it('rejects overlaps, future time, bad ranges, more than 24 h a day and projects you are not on', async () => {
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, start: '12:00', end: '13:00' })).status).toBe(409);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: addDays(todayNpt(), 1), durationMin: 30 })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, start: '15:00', end: '14:00' })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, start: '15:00' })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, durationMin: 1400 })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, durationMin: 30, projectId: projPrivate })).status).toBe(403);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, durationMin: 30, projectId: projA, activityId: '00000000-0000-7000-8000-00000000dead' })).status).toBe(400);
  });

  it('requires a project when the company says so', async () => {
    const put = await request(app).put('/api/v1/time/settings').set(auth(hr)).send({ requireProject: true, roundingStep: 15, roundingMode: 'up' });
    expect(put.status).toBe(200);
    expect((await request(app).put('/api/v1/time/settings').set(auth(sita)).send({ requireProject: false })).status).toBe(403);
    expect((await request(app).put('/api/v1/time/settings').set(auth(hr)).send({ roundingStep: 7 })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, durationMin: 10 })).status).toBe(400);
    expect((await request(app).get('/api/v1/time/settings').set(auth(sita))).body.policy).toMatchObject({ requireProject: true, roundingStep: 15 });
  });

  it('edits and deletes only your own entries', async () => {
    const e = await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, start: '14:00', end: '15:00', projectId: projA });
    const id = e.body.entry.id;
    expect((await request(app).patch(`/api/v1/time/entries/${id}`).set(auth(ram)).send({ note: 'x' })).status).toBe(404);
    expect((await request(app).delete(`/api/v1/time/entries/${id}`).set(auth(hr)).send()).status).toBe(403);
    const p = await request(app).patch(`/api/v1/time/entries/${id}`).set(auth(sita)).send({ start: '14:00', end: '16:00', note: 'longer' });
    expect(p.status).toBe(200);
    expect(p.body.entry).toMatchObject({ duration_min: 120, note: 'longer' });
    expect((await request(app).patch(`/api/v1/time/entries/${id}`).set(auth(sita)).send({ projectId: projPrivate })).status).toBe(403);
    expect((await request(app).delete(`/api/v1/time/entries/${id}`).set(auth(sita)).send()).status).toBe(200);
  });

  it('lets a manager read their team and HR everyone, but not others', async () => {
    expect((await request(app).get(`/api/v1/time/entries?from=${WEEK}&to=${addDays(WEEK, 6)}&employeeId=${DEMO.sita}`).set(auth(ram))).body.entries.length).toBeGreaterThan(0);
    expect((await request(app).get(`/api/v1/time/entries?from=${WEEK}&to=${addDays(WEEK, 6)}&employeeId=${DEMO.sita}`).set(auth(hr))).status).toBe(200);
    expect((await request(app).get(`/api/v1/time/entries?from=${WEEK}&to=${addDays(WEEK, 6)}&employeeId=${DEMO.sita}`).set(auth(hari))).status).toBe(403);
    expect((await request(app).get(`/api/v1/time/entries?from=${WEEK}&to=${addDays(WEEK, 6)}&employeeId=${DEMO.sita}`).set(auth(outsider))).status).toBe(404);
    expect((await request(app).get(`/api/v1/time/week?weekStart=${WEEK}&employeeId=${DEMO.sita}`).set(auth(other))).status).toBe(403);
    expect((await request(app).get(`/api/v1/time/entries?from=${WEEK}&to=${WEEK}`).set(auth(hr))).status).toBe(403); // HR user without an employee record
  });
});

describe('timer', () => {
  it('starts, refuses a second timer, stops on the server clock and resumes', async () => {
    await request(app).put('/api/v1/time/settings').set(auth(hr)).send({ requireProject: false });
    const s = await request(app).post('/api/v1/time/timer/start').set(auth(hari)).send({ note: 'Inbox' });
    expect(s.status).toBe(201);
    expect(s.body.running).toMatchObject({ source: 'timer', duration_min: null, note: 'Inbox' });
    expect((await request(app).post('/api/v1/time/timer/start').set(auth(hari)).send({})).status).toBe(409);
    expect((await request(app).get('/api/v1/time/timer').set(auth(hari))).body.running.id).toBe(s.body.running.id);
    // Nothing may be logged after a running timer started.
    const now = new Date(Date.now() + 345 * 60_000);
    if (now.getUTCHours() >= 1) {
      const hhmm = (d: Date) => d.toISOString().slice(11, 16);
      const start = new Date(now.getTime() - 50 * 60_000); const end = new Date(now.getTime() - 1 * 60_000);
      if (start.toISOString().slice(0, 10) === end.toISOString().slice(0, 10)) {
        await db.query(`UPDATE time_entry SET started_at = now() - interval '55 minutes' WHERE id = $1`, [s.body.running.id]);
        expect((await request(app).post('/api/v1/time/entries').set(auth(hari)).send({ date: todayNpt(), start: hhmm(start), end: hhmm(end) })).status).toBe(409);
      }
    }
    const stop = await request(app).post('/api/v1/time/timer/stop').set(auth(hari)).send({ note: 'Inbox zero' });
    expect(stop.status).toBe(200);
    expect(stop.body.entry.duration_min).toBeGreaterThanOrEqual(1);
    expect(stop.body.entry.note).toBe('Inbox zero');
    expect((await request(app).post('/api/v1/time/timer/stop').set(auth(hari)).send({})).status).toBe(409);
    const r = await request(app).post('/api/v1/time/timer/resume').set(auth(hari)).send({});
    expect(r.status).toBe(201);
    expect(r.body.running.note).toBe('Inbox zero');
    await request(app).post('/api/v1/time/timer/stop').set(auth(hari)).send({});
  });

  it('auto-stops a forgotten timer at the company limit and tells the employee', async () => {
    const s = await request(app).post('/api/v1/time/timer/start').set(auth(hari)).send({});
    await db.query('DELETE FROM time_entry WHERE employee_id = $1 AND id <> $2', [HARI, s.body.running.id]);
    await db.query(`UPDATE time_entry SET started_at = now() - interval '13 hours' WHERE id = $1`, [s.body.running.id]);
    const t = await request(app).get('/api/v1/time/timer').set(auth(hari));
    expect(t.body.running).toBeNull();
    const row = await db.query('SELECT duration_min, auto_stopped FROM time_entry WHERE id = $1', [s.body.running.id]);
    expect(row.rows[0]).toMatchObject({ duration_min: 720, auto_stopped: true });
    const n = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'time.timer_auto_stopped' AND employee_id = $1`, [HARI]);
    expect(n.rows[0].n).toBe(1);
    expect(await autoStopStaleTimers(db)).toBe(0);
  });

  it('employees without an employee record cannot track time; turning tracking off blocks it', async () => {
    expect((await request(app).post('/api/v1/time/timer/start').set(auth(hr)).send({})).status).toBe(403);
    await request(app).put('/api/v1/time/settings').set(auth(hr)).send({ enabled: false });
    expect((await request(app).post('/api/v1/time/timer/start').set(auth(hari)).send({})).status).toBe(403);
    expect((await request(app).post('/api/v1/time/entries').set(auth(hari)).send({ date: MON, durationMin: 5 })).status).toBe(403);
    await request(app).put('/api/v1/time/settings').set(auth(hr)).send({ enabled: true, roundingStep: 15, roundingMode: 'up' });
  });
});

describe('weekly timesheets', () => {
  let tsId = '';

  it('shows the week with rounding and the attendance comparison', async () => {
    await db.query(`INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, inputs_hash) VALUES ($1, $2, 1, 'Present', 480, 'h')`, [DEMO.sita, MON]);
    const w = await request(app).get(`/api/v1/time/week?weekStart=${MON}`).set(auth(sita));
    expect(w.status).toBe(200);
    expect(w.body.weekStart).toBe(WEEK);
    const mon = w.body.days.find((d: { date: string }) => d.date === MON);
    expect(mon).toMatchObject({ trackedMin: 210, roundedMin: 210, attendanceMin: 480, diffMin: -270, flag: 'under' });
    const tue = w.body.days.find((d: { date: string }) => d.date === TUE);
    expect(tue).toMatchObject({ trackedMin: 95, roundedMin: 105, flag: 'no_attendance' });
    expect(w.body.totals.billableMin).toBe(210);
    expect(roundEntry(95, { roundingStep: 15, roundingMode: 'up' })).toBe(105);
  });

  it('submits a week, locks it, and lets the employee withdraw', async () => {
    expect((await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: MON })).status).toBe(400);
    expect((await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: addDays(WEEK, -7) })).status).toBe(400); // empty week
    const s = await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: WEEK, note: 'Site week' });
    expect(s.status).toBe(201);
    tsId = s.body.id;
    expect((await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: WEEK })).status).toBe(409);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, durationMin: 10, projectId: projA })).status).toBe(409);
    const w = await request(app).post(`/api/v1/time/timesheets/${tsId}/withdraw`).set(auth(sita)).send();
    expect(w.status).toBe(200);
    expect((await request(app).post(`/api/v1/time/timesheets/${tsId}/withdraw`).set(auth(sita)).send()).status).toBe(409);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, start: '17:00', end: '17:30', projectId: projA })).status).toBe(201);
  });

  it('the manager rejects with a reason, the employee fixes and resubmits, the manager approves', async () => {
    const s = await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: WEEK });
    expect(s.status).toBe(201);
    tsId = s.body.id;
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === tsId);
    expect(item.objectType).toBe('timesheet');
    expect(item.summary).toMatchObject({ weekStart: WEEK, totalMin: 335, attendanceMin: 480 });
    expect((await request(app).get('/api/v1/workflow/inbox').set(auth(hari))).body.items.find((i: { objectId: string }) => i.objectId === tsId)).toBeUndefined();
    const rej = await request(app).post(`/api/v1/workflow/${item.instanceId}/reject`).set(auth(ram)).send({ reason: 'Add Tuesday project' });
    expect(rej.status).toBe(200);
    const after = await request(app).get(`/api/v1/time/week?weekStart=${WEEK}`).set(auth(sita));
    expect(after.body.timesheet).toMatchObject({ status: 'rejected', decision_comment: 'Add Tuesday project' });
    const note = await db.query(`SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'timesheet.rejected' AND employee_id = $1`, [DEMO.sita]);
    expect(note.rows[0].n).toBeGreaterThan(0);
    // Returned weeks are open again.
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: TUE, start: '18:00', end: '18:30', projectId: projA })).status).toBe(201);
    const again = await request(app).post('/api/v1/time/timesheets').set(auth(sita)).send({ weekStart: WEEK });
    expect(again.status).toBe(201);
    const item2 = (await request(app).get('/api/v1/workflow/inbox').set(auth(ram))).body.items.find((i: { objectId: string }) => i.objectId === again.body.id);
    expect((await request(app).post(`/api/v1/workflow/${item2.instanceId}/approve`).set(auth(ram)).send({})).status).toBe(200);
    const final = await request(app).get(`/api/v1/time/week?weekStart=${WEEK}`).set(auth(sita));
    expect(final.body.timesheet.status).toBe('approved');
    expect(final.body.history).toHaveLength(2);
    expect((await request(app).post('/api/v1/time/entries').set(auth(sita)).send({ date: MON, durationMin: 5, projectId: projA })).status).toBe(409);
    expect((await request(app).post(`/api/v1/time/timesheets/${again.body.id}/withdraw`).set(auth(sita)).send()).status).toBe(409);
  });

  it('lists timesheets by scope and shows details only to those who manage the person', async () => {
    const team = await request(app).get('/api/v1/time/timesheets').set(auth(ram));
    expect(team.body.timesheets.length).toBeGreaterThanOrEqual(3);
    expect(team.body.timesheets[0]).toHaveProperty('diff_min');
    expect((await request(app).get('/api/v1/time/timesheets?scope=all').set(auth(ram))).status).toBe(403);
    expect((await request(app).get('/api/v1/time/timesheets').set(auth(hari))).body.timesheets).toHaveLength(0);
    expect((await request(app).get('/api/v1/time/timesheets?scope=all').set(auth(hr))).body.timesheets.length).toBeGreaterThanOrEqual(3);
    expect((await request(app).get('/api/v1/time/timesheets?scope=all').set(auth(outsider))).body.timesheets).toHaveLength(0);
    const id = team.body.timesheets[0].id;
    const d = await request(app).get(`/api/v1/time/timesheets/${id}`).set(auth(ram));
    expect(d.status).toBe(200);
    expect(d.body.employee.name).toBe('Sita Sharma');
    expect(d.body.days).toHaveLength(7);
    expect((await request(app).get(`/api/v1/time/timesheets/${id}`).set(auth(hari))).status).toBe(403);
    expect((await request(app).get(`/api/v1/time/timesheets/${id}`).set(auth(outsider))).status).toBe(404);
    const tw = await request(app).get(`/api/v1/time/team-week?weekStart=${WEEK}`).set(auth(ram));
    expect(tw.body.people.map((p: { name_en: string }) => p.name_en)).toEqual(['Sita Sharma']);
    expect(tw.body.people[0].status).toBe('approved');
  });
});

describe('reports', () => {
  it('summarises by project with rounding, billable amount and budget burn', async () => {
    const r = await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${addDays(WEEK, 6)}&groupBy=project`).set(auth(hr));
    expect(r.status).toBe(200);
    const hydro = r.body.rows.find((x: { code: string }) => x.code === 'HYDRO-1');
    // 210 + 30 + 30 minutes, each already on a 15-minute boundary.
    expect(hydro).toMatchObject({ hours: 4.5, billable_hours: 4.5, billable_amount: 9000, budget_hours: 10, budget_used_pct: 45 });
    const none = r.body.rows.find((x: { key: string }) => x.key === 'none');
    expect(none).toMatchObject({ hours: 1.75, billable_hours: 0 });
  });

  it('groups by employee, client, activity and day and is scoped', async () => {
    for (const g of ['employee', 'client', 'activity', 'day']) {
      expect((await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${todayNpt()}&groupBy=${g}`).set(auth(hr))).status).toBe(200);
    }
    const own = await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${todayNpt()}&groupBy=employee`).set(auth(hari));
    expect(own.body.rows.map((x: { label: string }) => x.label)).toEqual(['Hari Karki']);
    const team = await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${todayNpt()}&groupBy=employee`).set(auth(ram));
    expect(team.body.rows.map((x: { label: string }) => x.label)).toEqual(['Sita Sharma']);
    expect((await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${todayNpt()}&scope=all`).set(auth(ram))).status).toBe(403);
    expect((await request(app).get(`/api/v1/time/reports/summary?from=${todayNpt()}&to=${WEEK}`).set(auth(hr))).status).toBe(400);
    expect((await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${todayNpt()}&scope=all`).set(auth(outsider))).body.rows).toHaveLength(0);
  });

  it('exports CSV with formula cells neutralised', async () => {
    await request(app).post('/api/v1/time/entries').set(auth(hari)).send({ date: MON, durationMin: 15, note: '=HYPERLINK("x")' });
    const r = await request(app).get(`/api/v1/time/reports/entries?from=${WEEK}&to=${addDays(WEEK, 6)}&format=csv`).set(auth(hr));
    expect(r.status).toBe(200);
    expect(r.headers['content-type']).toContain('text/csv');
    expect(r.text).toContain(`"'=HYPERLINK(""x"")"`);
    expect(r.text).toContain('Hours (rounded)');
    const s = await request(app).get(`/api/v1/time/reports/summary?from=${WEEK}&to=${addDays(WEEK, 6)}&format=csv`).set(auth(hr));
    expect(s.text).toContain('Budget used %');
  });
});

describe('daily jobs', () => {
  it('sends a Friday reminder once per week to people who track time and have not submitted', async () => {
    const thisWeek = weekStartOf(todayNpt());
    const friday = addDays(thisWeek, 5);
    const at = new Date(`${friday}T06:00:00Z`); // noon in Nepal
    await db.query(`INSERT INTO time_entry (company_id, employee_id, local_date, duration_min, source) VALUES ($1, $2, $3, 30, 'manual')`, [DEMO.companyId, HARI, thisWeek]);
    const n1 = await fridayTimesheetReminders(db, at);
    expect(n1).toBeGreaterThanOrEqual(1);
    expect(await fridayTimesheetReminders(db, at)).toBe(0);
    expect(await fridayTimesheetReminders(db, new Date(`${addDays(thisWeek, 4)}T06:00:00Z`))).toBe(0);
    const jobs = await DAILY_MODULE_JOBS.timesheets!({ db, cipher: undefined as never }, at);
    expect(jobs).toMatchObject({ autoStopped: 0, reminded: 0 });
  });
});
