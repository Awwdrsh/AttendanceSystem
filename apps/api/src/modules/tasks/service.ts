import { z } from 'zod';
import type { Queryable } from '../../db/pool.js';
import { ForbiddenError, NotFoundError, BadRequestError } from '../../errors.js';
import type { AuthUser } from '../auth/service.js';
import { hasRole } from '../auth/middleware.js';
import { notify } from '../notifications/service.js';
import { nptToday } from '../workflow/service.js';
import { bsLabel } from '../../bsdate.js';

/**
 * Tasks (study: "Tasks"): a personal board for every user and team boards that managers and HR set up for their people.
 * A personal board is private to its owner. A team board is shared with its members; its owner manages the name, the
 * columns and who is on it. HR can open any team board (never a personal one).
 */
export interface TaskColumn { id: string; name: string; done: boolean }
export interface BoardRow { id: string; company_id: string; kind: 'personal' | 'team'; name: string; owner_user_id: string; columns: TaskColumn[]; archived_at: Date | null; created_at: Date }
export type BoardAccess = 'owner' | 'member' | 'hr';

export const DEFAULT_COLUMNS: TaskColumn[] = [
  { id: 'todo', name: 'To do', done: false },
  { id: 'doing', name: 'In progress', done: false },
  { id: 'done', name: 'Done', done: true },
];

export const columnsSchema = z.array(z.object({
  id: z.string().regex(/^[a-z0-9-]{1,24}$/, 'column ids are short lowercase words'),
  name: z.string().trim().min(1).max(40),
  done: z.boolean().default(false),
})).min(1).max(8)
  .refine((c) => new Set(c.map((x) => x.id)).size === c.length, 'column ids must be unique')
  .refine((c) => c.some((x) => x.done), 'mark at least one column as done');

export async function personalBoard(db: Queryable, user: AuthUser): Promise<BoardRow> {
  const r = await db.query<BoardRow>(`SELECT * FROM task_board WHERE owner_user_id = $1 AND kind = 'personal'`, [user.id]);
  if (r.rows[0]) return r.rows[0];
  const ins = await db.query<BoardRow>(
    `INSERT INTO task_board (company_id, kind, name, owner_user_id, columns) VALUES ($1, 'personal', 'My tasks', $2, $3)
     ON CONFLICT (owner_user_id) WHERE kind = 'personal' DO UPDATE SET name = task_board.name RETURNING *`,
    [user.companyId, user.id, JSON.stringify(DEFAULT_COLUMNS)]);
  return ins.rows[0]!;
}

/** The board and how the user may use it; 404 for boards they cannot see. */
export async function boardFor(db: Queryable, user: AuthUser, boardId: string): Promise<{ board: BoardRow; access: BoardAccess }> {
  const r = await db.query<BoardRow>('SELECT * FROM task_board WHERE id = $1 AND company_id = $2', [boardId, user.companyId]);
  const board = r.rows[0];
  if (!board) throw new NotFoundError('board not found');
  if (board.owner_user_id === user.id) return { board, access: 'owner' };
  if (board.kind === 'personal') throw new NotFoundError('board not found');
  const m = await db.query('SELECT 1 FROM task_board_member WHERE board_id = $1 AND user_id = $2', [boardId, user.id]);
  if (m.rows.length) return { board, access: 'member' };
  if (hasRole(user, 'hr')) return { board, access: 'hr' };
  throw new NotFoundError('board not found');
}

export function assertOwner(access: BoardAccess): void {
  if (access !== 'owner') throw new ForbiddenError('only the board owner can change the board');
}

/** Users who may be put on a team board: HR any active user of the company; a manager their direct reports. */
export async function assertMembersAllowed(db: Queryable, user: AuthUser, userIds: string[]): Promise<void> {
  if (!userIds.length) return;
  const r = await db.query<{ id: string; employee_id: string | null }>(
    `SELECT id, employee_id FROM app_user WHERE company_id = $1 AND status = 'active' AND id = ANY($2::uuid[])`, [user.companyId, userIds]);
  if (r.rows.length !== new Set(userIds).size) throw new BadRequestError('a chosen person does not exist or cannot sign in');
  if (hasRole(user, 'hr')) return;
  if (!user.employeeId) throw new ForbiddenError('only managers and HR can set up team boards');
  const reports = await db.query<{ employee_id: string }>(
    'SELECT employee_id FROM employee_assignment WHERE manager_id = $1 AND valid @> current_date', [user.employeeId]);
  const ok = new Set(reports.rows.map((x) => x.employee_id));
  const bad = r.rows.filter((u) => u.id !== user.id && (!u.employee_id || !ok.has(u.employee_id)));
  if (bad.length) throw new ForbiddenError('managers can add only their direct reports to a team board');
}

/** Assignees must be able to see the board: the owner and members of a team board, only the owner on a personal one. */
export async function assertAssignable(db: Queryable, board: BoardRow, userIds: string[]): Promise<void> {
  if (!userIds.length) return;
  if (board.kind === 'personal') {
    if (userIds.some((id) => id !== board.owner_user_id)) throw new BadRequestError('tasks on your personal board can only be assigned to you');
    return;
  }
  const m = await db.query<{ user_id: string }>('SELECT user_id FROM task_board_member WHERE board_id = $1', [board.id]);
  const allowed = new Set([board.owner_user_id, ...m.rows.map((x) => x.user_id)]);
  if (userIds.some((id) => !allowed.has(id))) throw new BadRequestError('assign tasks only to people on this board');
}

export interface TaskRow {
  id: string; company_id: string; board_id: string; column_id: string; title: string; description: string | null; priority: string;
  due_date: string | null; labels: string[]; position: number; created_by: string | null; completed_at: Date | null; archived_at: Date | null;
  created_at: Date; updated_at: Date;
}

export async function taskFor(db: Queryable, user: AuthUser, taskId: string): Promise<{ task: TaskRow; board: BoardRow; access: BoardAccess }> {
  const r = await db.query<TaskRow>(`SELECT id, company_id, board_id, column_id, title, description, priority, due_date::text, labels, position, created_by, completed_at, archived_at, created_at, updated_at
    FROM task WHERE id = $1 AND company_id = $2`, [taskId, user.companyId]);
  const task = r.rows[0];
  if (!task) throw new NotFoundError('task not found');
  const { board, access } = await boardFor(db, user, task.board_id);
  return { task, board, access };
}

export async function notifyAssigned(db: Queryable, actor: AuthUser, task: { id: string; title: string; due_date: string | null }, board: BoardRow, userIds: string[]): Promise<void> {
  for (const userId of userIds) {
    if (userId === actor.id) continue;
    await notify(db, { companyId: board.company_id, userId, template: 'tasks.assigned', title: 'New task for you',
      body: `${actor.name} assigned you “${task.title}” on ${board.name}${task.due_date ? `, due ${bsLabel(task.due_date)}` : ''}.`, payload: { taskId: task.id, boardId: board.id } });
  }
}

/**
 * Daily: remind assignees (or the owner of an unassigned personal task) of open tasks due today, and once of tasks that
 * just became overdue. Each reminder goes once per task, person and day.
 */
export async function taskReminders(db: Queryable, now = new Date()): Promise<{ dueToday: number; overdue: number }> {
  const today = nptToday();
  void now;
  const rows = await db.query<{ id: string; company_id: string; title: string; due_date: string; board_name: string; user_id: string }>(
    `SELECT t.id, t.company_id, t.title, t.due_date::text, b.name AS board_name, coalesce(a.user_id, b.owner_user_id) AS user_id
       FROM task t JOIN task_board b ON b.id = t.board_id LEFT JOIN task_assignee a ON a.task_id = t.id
      WHERE t.completed_at IS NULL AND t.archived_at IS NULL AND b.archived_at IS NULL
        AND (t.due_date = $1::date OR t.due_date = $1::date - 1)
        AND (a.user_id IS NOT NULL OR b.kind = 'personal')`, [today]);
  let dueToday = 0; let overdue = 0;
  for (const r of rows.rows) {
    const kind = r.due_date === today ? 'due_today' : 'overdue';
    const ins = await db.query(`INSERT INTO task_reminder_sent (task_id, user_id, kind, local_date) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING task_id`, [r.id, r.user_id, kind, today]);
    if (!ins.rows.length) continue;
    await notify(db, { companyId: r.company_id, userId: r.user_id, template: `tasks.${kind}`, title: kind === 'due_today' ? 'Task due today' : 'Task overdue',
      body: `“${r.title}” on ${r.board_name} ${kind === 'due_today' ? 'is due today' : 'was due yesterday and is still open'}.`, payload: { taskId: r.id } });
    if (kind === 'due_today') dueToday++; else overdue++;
  }
  return { dueToday, overdue };
}
