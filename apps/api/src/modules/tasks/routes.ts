import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Db, Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { companyIdOf, hasRole } from '../auth/middleware.js';
import { attachFile, deleteFile, readFile, sendStoredFile } from '../files/store.js';
import { notify } from '../notifications/service.js';
import { nptToday } from '../workflow/service.js';
import {
  assertAssignable, assertMembersAllowed, assertOwner, boardFor, columnsSchema, DEFAULT_COLUMNS, notifyAssigned, personalBoard, taskFor,
  type BoardRow, type TaskRow,
} from './service.js';

async function tx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const c = await db.connect();
  try { await c.query('BEGIN'); const out = await fn(c); await c.query('COMMIT'); return out; }
  catch (e) { await c.query('ROLLBACK'); throw e; } finally { c.release(); }
}

const addDays = (iso: string, n: number): string => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const label = z.string().trim().min(1).max(24);
const taskBody = z.object({
  title: z.string().trim().min(1).max(200),
  description: z.string().max(5000).nullable().optional(),
  columnId: z.string().max(24).optional(),
  priority: z.enum(['low', 'normal', 'high', 'urgent']).default('normal'),
  dueDate: isoDate.nullable().optional(),
  labels: z.array(label).max(8).default([]),
  assigneeIds: z.array(z.uuid()).max(10).default([]),
});

const TASK_COLS = `t.id, t.board_id, t.column_id, t.title, t.description, t.priority, t.due_date::text AS due_date, t.labels, t.position, t.created_by,
  t.completed_at, t.created_at, t.updated_at,
  coalesce((SELECT json_agg(json_build_object('userId', u.id, 'name', u.name) ORDER BY u.name) FROM task_assignee a JOIN app_user u ON u.id = a.user_id WHERE a.task_id = t.id), '[]') AS assignees,
  (SELECT count(*)::int FROM task_checklist_item c WHERE c.task_id = t.id) AS checklist_total,
  (SELECT count(*)::int FROM task_checklist_item c WHERE c.task_id = t.id AND c.done) AS checklist_done,
  (SELECT count(*)::int FROM task_comment c WHERE c.task_id = t.id) AS comments,
  (SELECT count(*)::int FROM task_attachment c WHERE c.task_id = t.id) AS attachments`;

/** Personal and team task boards, tasks with checklists, comments and attachments, my to-do and board reports. */
export function tasksRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  const members = async (boardId: string) => (await db.query<{ user_id: string; name: string; employee_id: string | null }>(
    `SELECT m.user_id, u.name, u.employee_id FROM task_board_member m JOIN app_user u ON u.id = m.user_id WHERE m.board_id = $1 ORDER BY u.name`, [boardId])).rows;

  // ------------------------------------------------------------ boards
  r.get('/boards', async (req, res) => {
    companyIdOf(req);
    const mine = await personalBoard(db, req.user!);
    const rows = await db.query<BoardRow & { open: number; overdue: number; owner_name: string }>(
      `SELECT b.*, u.name AS owner_name,
              (SELECT count(*)::int FROM task t WHERE t.board_id = b.id AND t.archived_at IS NULL AND t.completed_at IS NULL) AS open,
              (SELECT count(*)::int FROM task t WHERE t.board_id = b.id AND t.archived_at IS NULL AND t.completed_at IS NULL AND t.due_date < $3::date) AS overdue
         FROM task_board b JOIN app_user u ON u.id = b.owner_user_id
        WHERE b.company_id = $1 AND b.archived_at IS NULL
          AND (b.id = $4 OR (b.kind = 'team' AND (b.owner_user_id = $2 OR EXISTS (SELECT 1 FROM task_board_member m WHERE m.board_id = b.id AND m.user_id = $2) OR $5)))
        ORDER BY (b.kind = 'personal') DESC, b.name`, [req.user!.companyId, req.user!.id, nptToday(), mine.id, hasRole(req.user!, 'hr') && req.query.all === '1']);
    res.json({ boards: rows.rows, canCreateTeamBoard: hasRole(req.user!, 'hr', 'manager') });
  });

  r.post('/boards', async (req, res) => {
    if (!hasRole(req.user!, 'hr', 'manager')) throw new ForbiddenError('only managers and HR can set up team boards');
    const b = z.object({ name: z.string().trim().min(1).max(80), memberUserIds: z.array(z.uuid()).max(200).default([]), addMyReports: z.boolean().default(false) }).parse(req.body);
    let ids = b.memberUserIds;
    if (b.addMyReports && req.user!.employeeId) {
      const rep = await db.query<{ id: string }>(
        `SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.employee_id AND u.status = 'active'
          WHERE a.manager_id = $1 AND a.valid @> current_date`, [req.user!.employeeId]);
      ids = [...new Set([...ids, ...rep.rows.map((x) => x.id)])];
    }
    ids = ids.filter((id) => id !== req.user!.id);
    await assertMembersAllowed(db, req.user!, ids);
    const board = await tx(db, async (c) => {
      const ins = await c.query<BoardRow>(
        `INSERT INTO task_board (company_id, kind, name, owner_user_id, columns) VALUES ($1, 'team', $2, $3, $4) RETURNING *`,
        [companyIdOf(req), b.name, req.user!.id, JSON.stringify(DEFAULT_COLUMNS)]);
      for (const id of ids) await c.query('INSERT INTO task_board_member (board_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [ins.rows[0]!.id, id]);
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'task_board', rowId: ins.rows[0]!.id, after: { name: b.name, members: ids.length } });
      return ins.rows[0]!;
    });
    res.status(201).json({ board });
  });

  r.get('/boards/:id', async (req, res) => {
    const { board, access } = await boardFor(db, req.user!, z.uuid().parse(req.params.id));
    const tasks = await db.query(`SELECT ${TASK_COLS} FROM task t WHERE t.board_id = $1 AND t.archived_at IS NULL ORDER BY t.column_id, t.position, t.created_at`, [board.id]);
    const owner = await db.query<{ name: string }>('SELECT name FROM app_user WHERE id = $1', [board.owner_user_id]);
    res.json({ board, access, ownerName: owner.rows[0]?.name ?? null, members: board.kind === 'team' ? await members(board.id) : [], tasks: tasks.rows });
  });

  r.patch('/boards/:id', async (req, res) => {
    const { board, access } = await boardFor(db, req.user!, z.uuid().parse(req.params.id));
    assertOwner(access);
    const b = z.object({ name: z.string().trim().min(1).max(80).optional(), columns: columnsSchema.optional(), moveTo: z.string().max(24).optional(), archived: z.boolean().optional() }).parse(req.body);
    if (b.archived && board.kind === 'personal') throw new BadRequestError('your personal board cannot be archived');
    await tx(db, async (c) => {
      if (b.columns) {
        const keep = new Set(b.columns.map((x) => x.id));
        const removed = board.columns.filter((x) => !keep.has(x.id)).map((x) => x.id);
        if (removed.length) {
          const used = await c.query<{ n: number }>('SELECT count(*)::int AS n FROM task WHERE board_id = $1 AND column_id = ANY($2::text[]) AND archived_at IS NULL', [board.id, removed]);
          if (used.rows[0]!.n > 0) {
            if (!b.moveTo || !keep.has(b.moveTo)) throw new ConflictError(`${used.rows[0]!.n} task(s) are in the columns you removed; choose a column to move them to`);
            await c.query('UPDATE task SET column_id = $3, updated_at = now() WHERE board_id = $1 AND column_id = ANY($2::text[])', [board.id, removed, b.moveTo]);
          }
        }
        await c.query('UPDATE task_board SET columns = $2 WHERE id = $1', [board.id, JSON.stringify(b.columns)]);
        // Done-ness follows the column a task sits in.
        const done = b.columns.filter((x) => x.done).map((x) => x.id);
        await c.query(`UPDATE task SET completed_at = CASE WHEN column_id = ANY($2::text[]) THEN coalesce(completed_at, now()) ELSE NULL END WHERE board_id = $1`, [board.id, done]);
      }
      if (b.name) await c.query('UPDATE task_board SET name = $2 WHERE id = $1', [board.id, b.name]);
      if (b.archived !== undefined) await c.query('UPDATE task_board SET archived_at = CASE WHEN $2 THEN now() ELSE NULL END WHERE id = $1', [board.id, b.archived]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'task_board', rowId: board.id, after: b });
    });
    res.json({ ok: true });
  });

  r.put('/boards/:id/members', async (req, res) => {
    const { board, access } = await boardFor(db, req.user!, z.uuid().parse(req.params.id));
    assertOwner(access);
    if (board.kind !== 'team') throw new BadRequestError('a personal board has no members');
    const b = z.object({ userIds: z.array(z.uuid()).max(200) }).parse(req.body);
    const ids = [...new Set(b.userIds)].filter((id) => id !== board.owner_user_id);
    const current = (await members(board.id)).map((m) => m.user_id);
    const added = ids.filter((id) => !current.includes(id));
    await assertMembersAllowed(db, req.user!, added);
    await tx(db, async (c) => {
      const removed = current.filter((id) => !ids.includes(id));
      if (removed.length) {
        await c.query('DELETE FROM task_board_member WHERE board_id = $1 AND user_id = ANY($2::uuid[])', [board.id, removed]);
        await c.query('DELETE FROM task_assignee WHERE user_id = ANY($2::uuid[]) AND task_id IN (SELECT id FROM task WHERE board_id = $1)', [board.id, removed]);
      }
      for (const id of added) await c.query('INSERT INTO task_board_member (board_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [board.id, id]);
      await audit(c, { actorId: req.user!.id, action: 'members', table: 'task_board', rowId: board.id, after: { added, removed } });
    });
    res.json({ members: await members(board.id) });
  });

  /** People the current user may add to a team board. */
  r.get('/people', async (req, res) => {
    const companyId = companyIdOf(req);
    if (!hasRole(req.user!, 'hr', 'manager')) return res.json({ people: [] });
    const rows = hasRole(req.user!, 'hr')
      ? await db.query(`SELECT u.id AS user_id, u.name, e.designation FROM app_user u LEFT JOIN employee e ON e.id = u.employee_id
          WHERE u.company_id = $1 AND u.status = 'active' ORDER BY u.name LIMIT 1000`, [companyId])
      : await db.query(`SELECT u.id AS user_id, u.name, e.designation FROM employee_assignment a JOIN app_user u ON u.employee_id = a.employee_id AND u.status = 'active'
          JOIN employee e ON e.id = a.employee_id WHERE a.manager_id = $1 AND a.valid @> current_date ORDER BY u.name`, [req.user!.employeeId]);
    return res.json({ people: rows.rows });
  });

  // ------------------------------------------------------------ tasks
  r.post('/boards/:id/tasks', async (req, res) => {
    const { board } = await boardFor(db, req.user!, z.uuid().parse(req.params.id));
    const b = taskBody.parse(req.body);
    const column = b.columnId ?? board.columns[0]!.id;
    const col = board.columns.find((c) => c.id === column);
    if (!col) throw new BadRequestError('that column does not exist on this board');
    const assignees = board.kind === 'personal' ? [board.owner_user_id] : [...new Set(b.assigneeIds)];
    await assertAssignable(db, board, assignees);
    const task = await tx(db, async (c) => {
      const pos = await c.query<{ p: number }>('SELECT coalesce(max(position), 0) + 1 AS p FROM task WHERE board_id = $1 AND column_id = $2', [board.id, column]);
      const ins = await c.query<TaskRow>(
        `INSERT INTO task (company_id, board_id, column_id, title, description, priority, due_date, labels, position, created_by, completed_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CASE WHEN $11 THEN now() END) RETURNING id, title, due_date::text`,
        [board.company_id, board.id, column, b.title, b.description ?? null, b.priority, b.dueDate ?? null, b.labels, pos.rows[0]!.p, req.user!.id, col.done]);
      for (const id of assignees) await c.query('INSERT INTO task_assignee (task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [ins.rows[0]!.id, id]);
      await notifyAssigned(c, req.user!, ins.rows[0]!, board, assignees);
      return ins.rows[0]!;
    });
    res.status(201).json({ task });
  });

  r.get('/my/todo', async (req, res) => {
    const today = nptToday();
    // The week runs Sunday to Saturday; overdue tasks always show.
    const dow = new Date(`${today}T00:00:00Z`).getUTCDay();
    const weekEnd = addDays(today, 6 - dow);
    const rows = await db.query(
      `SELECT ${TASK_COLS}, b.name AS board_name, b.kind AS board_kind, (SELECT x->>'name' FROM jsonb_array_elements(b.columns) x WHERE x->>'id' = t.column_id) AS column_name
         FROM task t JOIN task_board b ON b.id = t.board_id
        WHERE t.company_id = $1 AND t.archived_at IS NULL AND t.completed_at IS NULL AND b.archived_at IS NULL
          AND (EXISTS (SELECT 1 FROM task_assignee a WHERE a.task_id = t.id AND a.user_id = $2) OR (b.kind = 'personal' AND b.owner_user_id = $2))
          AND (t.due_date IS NULL OR t.due_date <= $3::date)
        ORDER BY t.due_date NULLS LAST, CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.created_at
        LIMIT 100`, [companyIdOf(req), req.user!.id, weekEnd]);
    res.json({ today, weekEnd, tasks: rows.rows });
  });

  r.get('/:id', async (req, res) => {
    const { task, board, access } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    const [full, checklist, comments, attachments] = await Promise.all([
      db.query(`SELECT ${TASK_COLS} FROM task t WHERE t.id = $1`, [task.id]),
      db.query('SELECT id, text, done, position FROM task_checklist_item WHERE task_id = $1 ORDER BY position, id', [task.id]),
      db.query(`SELECT c.id, c.body, c.created_at, c.user_id, u.name FROM task_comment c JOIN app_user u ON u.id = c.user_id WHERE c.task_id = $1 ORDER BY c.created_at`, [task.id]),
      db.query(`SELECT a.id, f.filename, f.mime, f.bytes, a.added_at, u.name AS added_by_name FROM task_attachment a JOIN stored_file f ON f.id = a.file_id
                 LEFT JOIN app_user u ON u.id = a.added_by WHERE a.task_id = $1 AND f.deleted_at IS NULL ORDER BY a.added_at`, [task.id]),
    ]);
    res.json({ task: full.rows[0], board: { id: board.id, name: board.name, kind: board.kind, columns: board.columns }, access,
      members: board.kind === 'team' ? await members(board.id) : [], checklist: checklist.rows, comments: comments.rows, attachments: attachments.rows });
  });

  r.patch('/:id', async (req, res) => {
    const { task, board } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    const b = taskBody.partial().extend({ position: z.number().min(0).max(1e9).optional() }).parse(req.body);
    const keys = new Set(Object.keys(req.body ?? {}));
    const col = b.columnId !== undefined ? board.columns.find((c) => c.id === b.columnId) : undefined;
    if (b.columnId !== undefined && !col) throw new BadRequestError('that column does not exist on this board');
    let newAssignees: string[] = [];
    await tx(db, async (c) => {
      const sets: string[] = []; const vals: unknown[] = [task.id];
      const set = (sql: string, v: unknown) => { vals.push(v); sets.push(`${sql} = $${vals.length}`); };
      if (b.title !== undefined) set('title', b.title);
      if (keys.has('description')) set('description', b.description ?? null);
      if (keys.has('priority') && b.priority) set('priority', b.priority);
      if (keys.has('dueDate')) set('due_date', b.dueDate ?? null);
      if (keys.has('labels') && b.labels) set('labels', b.labels);
      if (b.position !== undefined) set('position', b.position);
      if (col) {
        set('column_id', col.id);
        sets.push(col.done ? 'completed_at = coalesce(completed_at, now())' : 'completed_at = NULL');
      }
      if (sets.length) await c.query(`UPDATE task SET ${sets.join(', ')}, updated_at = now() WHERE id = $1`, vals);
      if (keys.has('assigneeIds') && b.assigneeIds && board.kind === 'team') {
        const ids = [...new Set(b.assigneeIds)];
        await assertAssignable(c, board, ids);
        const cur = (await c.query<{ user_id: string }>('SELECT user_id FROM task_assignee WHERE task_id = $1', [task.id])).rows.map((x) => x.user_id);
        newAssignees = ids.filter((id) => !cur.includes(id));
        await c.query('DELETE FROM task_assignee WHERE task_id = $1 AND NOT (user_id = ANY($2::uuid[]))', [task.id, ids]);
        for (const id of newAssignees) await c.query('INSERT INTO task_assignee (task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [task.id, id]);
        const t = (await c.query<{ id: string; title: string; due_date: string | null }>('SELECT id, title, due_date::text FROM task WHERE id = $1', [task.id])).rows[0]!;
        await notifyAssigned(c, req.user!, t, board, newAssignees);
      }
    });
    const out = await db.query(`SELECT ${TASK_COLS} FROM task t WHERE t.id = $1`, [task.id]);
    res.json({ task: out.rows[0] });
  });

  r.delete('/:id', async (req, res) => {
    const { task, access } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    if (access !== 'owner' && task.created_by !== req.user!.id && access !== 'hr') throw new ForbiddenError('only the person who created the task or the board owner can remove it');
    await db.query('UPDATE task SET archived_at = now(), updated_at = now() WHERE id = $1', [task.id]);
    await audit(db, { actorId: req.user!.id, action: 'archive', table: 'task', rowId: task.id });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ checklist, comments, attachments
  r.post('/:id/checklist', async (req, res) => {
    const { task } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    const b = z.object({ text: z.string().trim().min(1).max(300) }).parse(req.body);
    const n = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM task_checklist_item WHERE task_id = $1', [task.id]);
    if (n.rows[0]!.n >= 50) throw new BadRequestError('a task can have at most 50 checklist items');
    const ins = await db.query('INSERT INTO task_checklist_item (task_id, text, position) VALUES ($1, $2, $3) RETURNING id, text, done, position', [task.id, b.text, n.rows[0]!.n]);
    res.status(201).json({ item: ins.rows[0] });
  });

  const checklistItem = async (req: Request): Promise<string> => {
    const id = z.uuid().parse(req.params.itemId);
    const it = await db.query<{ task_id: string }>('SELECT task_id FROM task_checklist_item WHERE id = $1', [id]);
    if (!it.rows[0]) throw new NotFoundError('checklist item not found');
    await taskFor(db, req.user!, it.rows[0].task_id);
    return id;
  };

  r.patch('/checklist/:itemId', async (req, res) => {
    const id = await checklistItem(req);
    const b = z.object({ text: z.string().trim().min(1).max(300).optional(), done: z.boolean().optional() }).parse(req.body);
    await db.query('UPDATE task_checklist_item SET text = coalesce($2, text), done = coalesce($3, done) WHERE id = $1', [id, b.text ?? null, b.done ?? null]);
    res.json({ ok: true });
  });

  r.delete('/checklist/:itemId', async (req, res) => {
    const id = await checklistItem(req);
    await db.query('DELETE FROM task_checklist_item WHERE id = $1', [id]);
    res.json({ ok: true });
  });

  r.post('/:id/comments', async (req, res) => {
    const { task, board } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    const b = z.object({ body: z.string().trim().min(1).max(2000) }).parse(req.body);
    const ins = await db.query('INSERT INTO task_comment (task_id, user_id, body) VALUES ($1, $2, $3) RETURNING id, body, created_at', [task.id, req.user!.id, b.body]);
    // Tell the assignees (and the creator) that someone commented.
    const to = await db.query<{ user_id: string }>(
      `SELECT user_id FROM task_assignee WHERE task_id = $1 UNION SELECT created_by FROM task WHERE id = $1 AND created_by IS NOT NULL`, [task.id]);
    for (const u of to.rows) {
      if (u.user_id === req.user!.id) continue;
      await notify(db, { companyId: board.company_id, userId: u.user_id, template: 'tasks.comment', title: 'New comment on a task',
        body: `${req.user!.name} commented on “${task.title}”.`, payload: { taskId: task.id } });
    }
    res.status(201).json({ comment: { ...ins.rows[0], user_id: req.user!.id, name: req.user!.name } });
  });

  r.post('/:id/attachments', async (req, res) => {
    const { task } = await taskFor(db, req.user!, z.uuid().parse(req.params.id));
    const b = z.object({ fileId: z.uuid() }).parse(req.body);
    const n = await db.query<{ n: number }>('SELECT count(*)::int AS n FROM task_attachment WHERE task_id = $1', [task.id]);
    if (n.rows[0]!.n >= 10) throw new BadRequestError('a task can have at most 10 attachments');
    const out = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>('INSERT INTO task_attachment (task_id, file_id, added_by) VALUES ($1, $2, $3) RETURNING id', [task.id, b.fileId, req.user!.id]);
      await attachFile(c, { companyId: companyIdOf(req), fileId: b.fileId, userId: req.user!.id, purpose: 'task_attachment', attachedTo: `task_attachment:${ins.rows[0]!.id}` });
      return ins.rows[0]!;
    });
    res.status(201).json({ id: out.id });
  });

  r.get('/attachments/:attId/file', async (req, res) => {
    const id = z.uuid().parse(req.params.attId);
    const a = await db.query<{ task_id: string; file_id: string }>('SELECT task_id, file_id FROM task_attachment WHERE id = $1', [id]);
    if (!a.rows[0]) throw new NotFoundError('attachment not found');
    await taskFor(db, req.user!, a.rows[0].task_id);
    sendStoredFile(res, await readFile(db, cipher, companyIdOf(req), a.rows[0].file_id), { download: req.query.download === '1' });
  });

  r.delete('/attachments/:attId', async (req, res) => {
    const id = z.uuid().parse(req.params.attId);
    const a = await db.query<{ task_id: string; file_id: string; added_by: string | null }>('SELECT task_id, file_id, added_by FROM task_attachment WHERE id = $1', [id]);
    if (!a.rows[0]) throw new NotFoundError('attachment not found');
    const { access } = await taskFor(db, req.user!, a.rows[0].task_id);
    if (a.rows[0].added_by !== req.user!.id && access !== 'owner') throw new ForbiddenError('only the person who added it or the board owner can remove it');
    await db.query('DELETE FROM task_attachment WHERE id = $1', [id]);
    await deleteFile(db, companyIdOf(req), a.rows[0].file_id);
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ reports
  r.get('/reports/boards/:id', async (req, res) => {
    const { board } = await boardFor(db, req.user!, z.uuid().parse(req.params.id));
    const today = nptToday();
    const [byColumn, byAssignee, totals] = await Promise.all([
      db.query<{ column_id: string; n: number }>('SELECT column_id, count(*)::int AS n FROM task WHERE board_id = $1 AND archived_at IS NULL GROUP BY column_id', [board.id]),
      db.query(`SELECT u.id AS user_id, u.name, count(*) FILTER (WHERE t.completed_at IS NULL)::int AS open,
                       count(*) FILTER (WHERE t.completed_at IS NULL AND t.due_date < $2::date)::int AS overdue,
                       count(*) FILTER (WHERE t.completed_at IS NOT NULL)::int AS done
                  FROM task t JOIN task_assignee a ON a.task_id = t.id JOIN app_user u ON u.id = a.user_id
                 WHERE t.board_id = $1 AND t.archived_at IS NULL GROUP BY u.id, u.name ORDER BY open DESC, u.name`, [board.id, today]),
      db.query<{ open: number; overdue: number; done: number; done_this_week: number; unassigned: number }>(
        `SELECT count(*) FILTER (WHERE completed_at IS NULL)::int AS open, count(*) FILTER (WHERE completed_at IS NULL AND due_date < $2::date)::int AS overdue,
                count(*) FILTER (WHERE completed_at IS NOT NULL)::int AS done, count(*) FILTER (WHERE completed_at >= now() - interval '7 days')::int AS done_this_week,
                count(*) FILTER (WHERE completed_at IS NULL AND NOT EXISTS (SELECT 1 FROM task_assignee a WHERE a.task_id = task.id))::int AS unassigned
           FROM task WHERE board_id = $1 AND archived_at IS NULL`, [board.id, today]),
    ]);
    res.json({
      board: { id: board.id, name: board.name, kind: board.kind },
      columns: board.columns.map((c) => ({ ...c, tasks: byColumn.rows.find((x) => x.column_id === c.id)?.n ?? 0 })),
      totals: totals.rows[0], assignees: byAssignee.rows,
      overdueTasks: (await db.query(`SELECT ${TASK_COLS} FROM task t WHERE t.board_id = $1 AND t.archived_at IS NULL AND t.completed_at IS NULL AND t.due_date < $2::date ORDER BY t.due_date LIMIT 50`, [board.id, today])).rows,
    });
  });

  return r;
}
