import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { taskReminders } from '../src/modules/tasks/service.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let sita: string; let sitaUser: string; let ram: string; let ramUser: string; let other: string; let otherUser: string;
const OTHER_EMP = '00000000-0000-7000-8000-0000000000e4';
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;
const plus = (n: number) => new Date(Date.parse(`${nptToday()}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  const s = await loginAs(db, app, ['employee'], DEMO.sita); sita = s.token; sitaUser = s.userId;
  const m = await loginAs(db, app, ['manager'], DEMO.ram); ram = m.token; ramUser = m.userId;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`INSERT INTO employee (id, company_id, emp_code, name_en, gender) VALUES ($1, $2, 'E004', 'Gita Rai', 'female')`, [OTHER_EMP, DEMO.companyId]);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2026-01-01', NULL), $2, 'regular')`, [OTHER_EMP, DEMO.branchId]);
  const o = await loginAs(db, app, ['employee'], OTHER_EMP); other = o.token; otherUser = o.userId;
});
afterAll(async () => { await db.end(); });

describe('personal boards', () => {
  let boardId = '';
  it('every user gets a private personal board with To do / In progress / Done', async () => {
    const r = await request(app).get('/api/v1/tasks/boards').set(auth(sita));
    expect(r.status).toBe(200);
    expect(r.body.canCreateTeamBoard).toBe(false);
    const b = r.body.boards.find((x: { kind: string }) => x.kind === 'personal');
    expect(b.columns.map((c: { name: string }) => c.name)).toEqual(['To do', 'In progress', 'Done']);
    boardId = b.id;
    // Asking again does not make a second one.
    const again = await request(app).get('/api/v1/tasks/boards').set(auth(sita));
    expect(again.body.boards.filter((x: { kind: string }) => x.kind === 'personal')).toHaveLength(1);
  });

  it('nobody else can open it, not even HR', async () => {
    expect((await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(hr))).status).toBe(404);
    expect((await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(ram))).status).toBe(404);
  });

  it('tasks are assigned to the owner, moving to Done completes them, and they show in my to-do', async () => {
    const t = await request(app).post(`/api/v1/tasks/boards/${boardId}/tasks`).set(auth(sita)).send({ title: 'Renew passport', dueDate: plus(1), priority: 'high', labels: ['personal'] });
    expect(t.status).toBe(201);
    const bad = await request(app).post(`/api/v1/tasks/boards/${boardId}/tasks`).set(auth(sita)).send({ title: 'x', columnId: 'nope' });
    expect(bad.status).toBe(400);
    const todo = await request(app).get('/api/v1/tasks/my/todo').set(auth(sita));
    expect(todo.body.tasks.map((x: { id: string }) => x.id)).toContain(t.body.task.id);
    const done = await request(app).patch(`/api/v1/tasks/${t.body.task.id}`).set(auth(sita)).send({ columnId: 'done' });
    expect(done.status).toBe(200);
    expect(done.body.task.completed_at).not.toBeNull();
    const todo2 = await request(app).get('/api/v1/tasks/my/todo').set(auth(sita));
    expect(todo2.body.tasks.map((x: { id: string }) => x.id)).not.toContain(t.body.task.id);
    const back = await request(app).patch(`/api/v1/tasks/${t.body.task.id}`).set(auth(sita)).send({ columnId: 'todo' });
    expect(back.body.task.completed_at).toBeNull();
  });

  it('employees cannot set up team boards', async () => {
    expect((await request(app).post('/api/v1/tasks/boards').set(auth(sita)).send({ name: 'Mine' })).status).toBe(403);
  });
});

describe('team boards', () => {
  let boardId = ''; let taskId = '';

  it('a manager sets up a board with their reports; adding a non-report is refused', async () => {
    const bad = await request(app).post('/api/v1/tasks/boards').set(auth(ram)).send({ name: 'Ops', memberUserIds: [otherUser] });
    expect(bad.status).toBe(403);
    const r = await request(app).post('/api/v1/tasks/boards').set(auth(ram)).send({ name: 'Ops', addMyReports: true });
    expect(r.status).toBe(201);
    boardId = r.body.board.id;
    const b = await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(ram));
    expect(b.body.access).toBe('owner');
    expect(b.body.members.map((x: { user_id: string }) => x.user_id)).toEqual([sitaUser]);
  });

  it('tasks can be assigned to members only and the assignee is notified', async () => {
    const out = await request(app).post(`/api/v1/tasks/boards/${boardId}/tasks`).set(auth(ram)).send({ title: 'Count stock', assigneeIds: [otherUser] });
    expect(out.status).toBe(400);
    const t = await request(app).post(`/api/v1/tasks/boards/${boardId}/tasks`).set(auth(ram)).send({ title: 'Count stock', assigneeIds: [sitaUser], dueDate: nptToday() });
    expect(t.status).toBe(201);
    taskId = t.body.task.id;
    const n = await db.query(`SELECT body FROM notification_outbox WHERE user_id = $1 AND template = 'tasks.assigned'`, [sitaUser]);
    expect(n.rows).toHaveLength(1);
  });

  it('members see the board and task; outsiders get 404; HR can open team boards', async () => {
    expect((await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(sita))).body.access).toBe('member');
    expect((await request(app).get(`/api/v1/tasks/${taskId}`).set(auth(other))).status).toBe(404);
    expect((await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(other))).status).toBe(404);
    expect((await request(app).get(`/api/v1/tasks/boards/${boardId}`).set(auth(hr))).body.access).toBe('hr');
    const boards = await request(app).get('/api/v1/tasks/boards').set(auth(sita));
    expect(boards.body.boards.map((x: { id: string }) => x.id)).toContain(boardId);
  });

  it('checklist, comments and attachments', async () => {
    const c = await request(app).post(`/api/v1/tasks/${taskId}/checklist`).set(auth(sita)).send({ text: 'Shelf A' });
    expect(c.status).toBe(201);
    expect((await request(app).patch(`/api/v1/tasks/checklist/${c.body.item.id}`).set(auth(sita)).send({ done: true })).status).toBe(200);
    expect((await request(app).patch(`/api/v1/tasks/checklist/${c.body.item.id}`).set(auth(other)).send({ done: false })).status).toBe(404);
    const cm = await request(app).post(`/api/v1/tasks/${taskId}/comments`).set(auth(sita)).send({ body: 'Half done' });
    expect(cm.status).toBe(201);
    const n = await db.query(`SELECT 1 FROM notification_outbox WHERE user_id = $1 AND template = 'tasks.comment'`, [ramUser]);
    expect(n.rows).toHaveLength(1);
    const up = await request(app).post('/api/v1/files').set(auth(sita)).send({ purpose: 'task_attachment', filename: 'count.pdf', dataUrl: PDF });
    expect(up.status).toBe(201);
    const at = await request(app).post(`/api/v1/tasks/${taskId}/attachments`).set(auth(sita)).send({ fileId: up.body.file.id });
    expect(at.status).toBe(201);
    const file = await request(app).get(`/api/v1/tasks/attachments/${at.body.id}/file`).set(auth(ram));
    expect(file.status).toBe(200);
    expect(file.headers['content-type']).toContain('application/pdf');
    expect((await request(app).get(`/api/v1/tasks/attachments/${at.body.id}/file`).set(auth(other))).status).toBe(404);
    const full = await request(app).get(`/api/v1/tasks/${taskId}`).set(auth(sita));
    expect(full.body.task).toMatchObject({ checklist_total: 1, checklist_done: 1, comments: 1, attachments: 1 });
  });

  it('only the owner changes columns and members; removing a used column needs a target', async () => {
    const cols = [{ id: 'todo', name: 'Backlog', done: false }, { id: 'done', name: 'Finished', done: true }];
    expect((await request(app).patch(`/api/v1/tasks/boards/${boardId}`).set(auth(sita)).send({ columns: cols })).status).toBe(403);
    await request(app).patch(`/api/v1/tasks/${taskId}`).set(auth(sita)).send({ columnId: 'doing' });
    expect((await request(app).patch(`/api/v1/tasks/boards/${boardId}`).set(auth(ram)).send({ columns: cols })).status).toBe(409);
    expect((await request(app).patch(`/api/v1/tasks/boards/${boardId}`).set(auth(ram)).send({ columns: [{ id: 'todo', name: 'A', done: false }] })).status).toBe(400);
    const ok = await request(app).patch(`/api/v1/tasks/boards/${boardId}`).set(auth(ram)).send({ columns: cols, moveTo: 'todo' });
    expect(ok.status).toBe(200);
    const t = await request(app).get(`/api/v1/tasks/${taskId}`).set(auth(sita));
    expect(t.body.task.column_id).toBe('todo');
    expect((await request(app).put(`/api/v1/tasks/boards/${boardId}/members`).set(auth(sita)).send({ userIds: [] })).status).toBe(403);
  });

  it('reports count open, overdue and per assignee; the daily job reminds once', async () => {
    await db.query(`UPDATE task SET due_date = $2 WHERE id = $1`, [taskId, plus(-1)]);
    const r = await request(app).get(`/api/v1/tasks/reports/boards/${boardId}`).set(auth(ram));
    expect(r.status).toBe(200);
    expect(r.body.totals).toMatchObject({ open: 1, overdue: 1 });
    expect(r.body.assignees[0]).toMatchObject({ user_id: sitaUser, open: 1, overdue: 1 });
    const first = await taskReminders(db);
    expect(first.overdue).toBe(1);
    const second = await taskReminders(db);
    expect(second.overdue).toBe(0);
  });

  it('removing a member unassigns them; only the creator or owner can remove a task', async () => {
    expect((await request(app).delete(`/api/v1/tasks/${taskId}`).set(auth(sita))).status).toBe(403);
    const m = await request(app).put(`/api/v1/tasks/boards/${boardId}/members`).set(auth(ram)).send({ userIds: [] });
    expect(m.status).toBe(200);
    expect((await request(app).get(`/api/v1/tasks/${taskId}`).set(auth(sita))).status).toBe(404);
    const a = await db.query('SELECT 1 FROM task_assignee WHERE task_id = $1', [taskId]);
    expect(a.rows).toHaveLength(0);
    expect((await request(app).delete(`/api/v1/tasks/${taskId}`).set(auth(ram))).status).toBe(200);
  });
});
