import { del, get, patch, post, put } from './client';

export type Priority = 'low' | 'normal' | 'high' | 'urgent';
export interface TaskColumn { id: string; name: string; done: boolean }
export interface Board {
  id: string; kind: 'personal' | 'team'; name: string; owner_user_id: string; columns: TaskColumn[]; archived_at: string | null; created_at: string;
  open?: number; overdue?: number; owner_name?: string;
}
export interface Member { user_id: string; name: string; employee_id: string | null }
export interface Task {
  id: string; board_id: string; column_id: string; title: string; description: string | null; priority: Priority; due_date: string | null; labels: string[];
  position: number; created_by: string | null; completed_at: string | null; created_at: string; updated_at: string;
  assignees: Array<{ userId: string; name: string }>; checklist_total: number; checklist_done: number; comments: number; attachments: number;
  board_name?: string; board_kind?: 'personal' | 'team'; column_name?: string | null;
}
export interface ChecklistItem { id: string; text: string; done: boolean; position: number }
export interface Comment { id: string; body: string; created_at: string; user_id: string; name: string }
export interface Attachment { id: string; filename: string; mime: string; bytes: number; added_at: string; added_by_name: string | null }
export type Access = 'owner' | 'member' | 'hr';

export interface TaskInput { title: string; description?: string | null; columnId?: string; priority?: Priority; dueDate?: string | null; labels?: string[]; assigneeIds?: string[] }

export const PRIORITY_LABEL: Record<Priority, string> = { low: 'Low', normal: 'Normal', high: 'High', urgent: 'Urgent' };
export const PRIORITY_COLOR: Record<Priority, string> = { low: 'gray', normal: 'blue', high: 'orange', urgent: 'red' };

export const tasks = {
  boards: (all = false) => get<{ boards: Board[]; canCreateTeamBoard: boolean }>('/tasks/boards', all ? { all: 1 } : {}),
  createBoard: (b: { name: string; memberUserIds?: string[]; addMyReports?: boolean }) => post<{ board: Board }>('/tasks/boards', b),
  board: (id: string) => get<{ board: Board; access: Access; ownerName: string | null; members: Member[]; tasks: Task[] }>(`/tasks/boards/${id}`),
  updateBoard: (id: string, b: { name?: string; columns?: TaskColumn[]; moveTo?: string; archived?: boolean }) => patch<{ ok: boolean }>(`/tasks/boards/${id}`, b),
  setMembers: (id: string, userIds: string[]) => put<{ members: Member[] }>(`/tasks/boards/${id}/members`, { userIds }),
  people: () => get<{ people: Array<{ user_id: string; name: string; designation: string | null }> }>('/tasks/people'),
  create: (boardId: string, t: TaskInput) => post<{ task: { id: string } }>(`/tasks/boards/${boardId}/tasks`, t),
  get: (id: string) => get<{ task: Task; board: { id: string; name: string; kind: 'personal' | 'team'; columns: TaskColumn[] }; access: Access; members: Member[]; checklist: ChecklistItem[]; comments: Comment[]; attachments: Attachment[] }>(`/tasks/${id}`),
  update: (id: string, t: Partial<TaskInput> & { position?: number }) => patch<{ task: Task }>(`/tasks/${id}`, t),
  remove: (id: string) => del<{ ok: boolean }>(`/tasks/${id}`),
  addChecklist: (id: string, text: string) => post<{ item: ChecklistItem }>(`/tasks/${id}/checklist`, { text }),
  updateChecklist: (itemId: string, b: { text?: string; done?: boolean }) => patch<{ ok: boolean }>(`/tasks/checklist/${itemId}`, b),
  removeChecklist: (itemId: string) => del<{ ok: boolean }>(`/tasks/checklist/${itemId}`),
  comment: (id: string, body: string) => post<{ comment: Comment }>(`/tasks/${id}/comments`, { body }),
  attach: (id: string, fileId: string) => post<{ id: string }>(`/tasks/${id}/attachments`, { fileId }),
  removeAttachment: (attId: string) => del<{ ok: boolean }>(`/tasks/attachments/${attId}`),
  attachmentUrl: (attId: string) => `/api/v1/tasks/attachments/${attId}/file`,
  todo: () => get<{ today: string; weekEnd: string; tasks: Task[] }>('/tasks/my/todo'),
  report: (boardId: string) => get<{
    board: { id: string; name: string; kind: string }; columns: Array<TaskColumn & { tasks: number }>;
    totals: { open: number; overdue: number; done: number; done_this_week: number; unassigned: number };
    assignees: Array<{ user_id: string; name: string; open: number; overdue: number; done: number }>; overdueTasks: Task[];
  }>(`/tasks/reports/boards/${boardId}`),
};
