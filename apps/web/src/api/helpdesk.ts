import { get, patch, post, put } from './client';

export type Priority = 'low' | 'normal' | 'high' | 'urgent';
export type TicketStatus = 'open' | 'in_progress' | 'waiting' | 'resolved' | 'closed';

export interface Category { id: string; name: string; description: string | null; confidential: boolean; sort: number; active: boolean; owner_user_id: string | null; owner_name: string | null }
export interface Ticket {
  id: string; number: number; subject: string; priority: Priority; status: TicketStatus; category_id: string; category_name: string; confidential: boolean;
  requester_user_id: string; requester_name: string; employee_name: string | null; emp_code: string | null; assignee_user_id: string | null; assignee_name: string | null;
  sla_due_at: string; first_response_at: string | null; resolved_at: string | null; closed_at: string | null; breached_at: string | null; rating: number | null; rating_comment: string | null;
  created_at: string; updated_at: string; overdue?: boolean;
}
export interface Message { id: string; body: string; internal: boolean; file_ids: string[]; created_at: string; author_user_id: string; author_name: string; mine: boolean }
export interface TicketFile { id: string; filename: string; mime: string; bytes: number }
export interface HelpdeskPolicy { slaHours: Record<Priority, number>; autoCloseDays: number; reopenDays: number }
export interface Agent { id: string; name: string; email: string; roles: string[]; is_hr: boolean }
export interface Stats { open: number; unassigned: number; overdue: number; breached_90d: number; resolved_30d: number; avg_resolution_hours: string | null; avg_first_response_hours: string | null; avg_rating: string | null; ratings: number }

export const helpdesk = {
  categories: () => get<{ categories: Category[] }>('/helpdesk/categories'),
  createCategory: (b: { name: string; description?: string | null; confidential: boolean; ownerUserId?: string | null; sort?: number; active?: boolean }) => post<{ id: string }>('/helpdesk/categories', b),
  updateCategory: (id: string, b: { name: string; description?: string | null; confidential: boolean; ownerUserId?: string | null; sort?: number; active?: boolean }) => put<{ ok: boolean }>(`/helpdesk/categories/${id}`, b),
  policy: () => get<{ policy: HelpdeskPolicy }>('/helpdesk/settings'),
  savePolicy: (p: HelpdeskPolicy) => put<{ policy: HelpdeskPolicy }>('/helpdesk/settings', p),
  agents: () => get<{ agents: Agent[] }>('/helpdesk/agents'),
  create: (b: { categoryId: string; subject: string; body: string; priority: Priority; fileIds: string[] }) => post<{ id: string; number: number; slaDueAt: string }>('/helpdesk/tickets', b),
  mine: () => get<{ tickets: Ticket[] }>('/helpdesk/tickets/mine'),
  queue: (q: { status?: string; categoryId?: string; priority?: string; assignee?: string; breached?: string; search?: string } = {}) => get<{ tickets: Ticket[] }>('/helpdesk/tickets', q),
  stats: () => get<{ stats: Stats; byCategory: Array<{ name: string; open: number; total: number }> }>('/helpdesk/stats'),
  ticket: (id: string) => get<{ ticket: Ticket; role: 'agent' | 'requester'; messages: Message[]; files: TicketFile[]; canReopen: boolean }>(`/helpdesk/tickets/${id}`),
  reply: (id: string, b: { body: string; internal?: boolean; fileIds?: string[] }) => post<{ id: string }>(`/helpdesk/tickets/${id}/messages`, b),
  update: (id: string, b: { status?: TicketStatus; priority?: Priority; assigneeUserId?: string | null; categoryId?: string }) => patch<{ ok: boolean }>(`/helpdesk/tickets/${id}`, b),
  reopen: (id: string) => post<{ ok: boolean }>(`/helpdesk/tickets/${id}/reopen`),
  close: (id: string) => post<{ ok: boolean }>(`/helpdesk/tickets/${id}/close`),
  rate: (id: string, rating: number, comment?: string) => post<{ ok: boolean }>(`/helpdesk/tickets/${id}/rate`, { rating, comment }),
  fileUrl: (ticketId: string, fileId: string) => `/api/v1/helpdesk/tickets/${ticketId}/files/${fileId}`,
};

export const PRIORITY_LABEL: Record<Priority, string> = { low: 'Low', normal: 'Normal', high: 'High', urgent: 'Urgent' };
export const PRIORITY_COLOR: Record<Priority, string> = { low: 'gray', normal: 'blue', high: 'orange', urgent: 'red' };
export const STATUS_LABEL: Record<TicketStatus, string> = { open: 'Open', in_progress: 'In progress', waiting: 'Waiting on you', resolved: 'Resolved', closed: 'Closed' };
export const STATUS_COLOR: Record<TicketStatus, string> = { open: 'blue', in_progress: 'grape', waiting: 'yellow', resolved: 'teal', closed: 'gray' };
