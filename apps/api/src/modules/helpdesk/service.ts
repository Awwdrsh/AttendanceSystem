import { z } from 'zod';
import type { Db, Queryable } from '../../db/pool.js';
import { hasRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { notify, userIdsWithRole } from '../notifications/service.js';

export const PRIORITIES = ['low', 'normal', 'high', 'urgent'] as const;
export type Priority = typeof PRIORITIES[number];
export const STATUSES = ['open', 'in_progress', 'waiting', 'resolved', 'closed'] as const;

export const helpdeskPolicySchema = z.object({
  /** Hours from raising a ticket to resolving it, by priority. */
  slaHours: z.object({ low: z.number().int().min(1).max(720), normal: z.number().int().min(1).max(720), high: z.number().int().min(1).max(720), urgent: z.number().int().min(1).max(720) })
    .default({ low: 72, normal: 48, high: 24, urgent: 8 }),
  /** Resolved tickets close by themselves after this many days. */
  autoCloseDays: z.number().int().min(1).max(60).default(7),
  /** The requester can reopen a resolved ticket within this many days. */
  reopenDays: z.number().int().min(0).max(60).default(7),
});
export type HelpdeskPolicy = z.infer<typeof helpdeskPolicySchema>;

export async function helpdeskPolicy(db: Queryable, companyId: string): Promise<HelpdeskPolicy> {
  const r = await db.query<{ value: unknown }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'helpdesk.policy'`, [companyId]);
  const parsed = helpdeskPolicySchema.safeParse(r.rows[0]?.value ?? {});
  return parsed.success ? parsed.data : helpdeskPolicySchema.parse({});
}

const CATEGORIES: Array<[string, string, boolean]> = [
  ['Payroll & tax', 'Salary, payslips, deductions, TDS and SSF questions', false],
  ['Leave & attendance', 'Balances, missed punches, shifts and holidays', false],
  ['Documents & letters', 'Experience letters, salary certificates and other paperwork', false],
  ['IT & equipment', 'Laptop, email, access and other equipment', false],
  ['Other', 'Anything else for HR', false],
];

export async function ensureHelpdeskDefaults(db: Queryable, companyId: string): Promise<boolean> {
  const marker = await db.query(`INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'helpdesk.seeded', '{"version":1}') ON CONFLICT DO NOTHING`, [companyId]);
  if ((marker.rowCount ?? 0) === 0) return false;
  for (const [i, [name, description, confidential]] of CATEGORIES.entries()) {
    await db.query('INSERT INTO helpdesk_category (company_id, name, description, confidential, sort) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING', [companyId, name, description, confidential, i]);
  }
  return true;
}

export interface TicketRow {
  id: string; company_id: string; number: number; requester_user_id: string; employee_id: string | null; category_id: string; subject: string;
  priority: Priority; status: typeof STATUSES[number]; assignee_user_id: string | null; sla_due_at: Date; first_response_at: Date | null;
  resolved_at: Date | null; closed_at: Date | null; breached_at: Date | null; rating: number | null; rating_comment: string | null;
  created_at: Date; updated_at: Date; confidential: boolean; owner_user_id: string | null; category_name: string;
}

/**
 * Who someone is on a ticket. HR handles every ticket; outside HR, the assignee and the category owner handle
 * non-confidential tickets. Confidential tickets (grievances) are only ever visible to HR and the person who raised them.
 */
export function roleOnTicket(user: AuthUser, t: Pick<TicketRow, 'company_id' | 'requester_user_id' | 'assignee_user_id' | 'owner_user_id' | 'confidential'>): 'agent' | 'requester' | null {
  if (user.companyId !== t.company_id) return null;
  if (hasRole(user, 'hr')) return 'agent';
  if (!t.confidential && (t.assignee_user_id === user.id || t.owner_user_id === user.id)) return 'agent';
  if (t.requester_user_id === user.id) return 'requester';
  return null;
}

/** People to tell about something new on a ticket: the assignee, else the category owner, else HR. */
export async function handlers(db: Queryable, t: Pick<TicketRow, 'company_id' | 'assignee_user_id' | 'owner_user_id' | 'confidential'>): Promise<string[]> {
  if (t.assignee_user_id) return [t.assignee_user_id];
  if (t.owner_user_id && !t.confidential) return [t.owner_user_id];
  return userIdsWithRole(db, t.company_id, 'hr');
}

/** Daily: close tickets resolved long enough ago, and escalate tickets that passed their SLA without being resolved. */
export async function helpdeskDaily(db: Db, now = new Date()): Promise<Record<string, number>> {
  const closed = await db.query(
    `UPDATE helpdesk_ticket t SET status = 'closed', closed_at = $1, updated_at = $1
       FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'helpdesk.policy'
      WHERE t.company_id = c.id AND t.status = 'resolved'
        AND t.resolved_at < $1::timestamptz - make_interval(days => coalesce((s.value->>'autoCloseDays')::int, 7))`, [now]);
  const breached = await db.query<TicketRow>(
    `UPDATE helpdesk_ticket t SET breached_at = $1 FROM helpdesk_category c
      WHERE c.id = t.category_id AND t.status IN ('open', 'in_progress', 'waiting') AND t.breached_at IS NULL AND t.sla_due_at < $1
      RETURNING t.*, c.confidential, c.owner_user_id, c.name AS category_name`, [now]);
  for (const t of breached.rows) {
    const targets = new Set([...(await handlers(db, t)), ...(await userIdsWithRole(db, t.company_id, 'hr'))]);
    for (const userId of targets) {
      await notify(db, { companyId: t.company_id, userId, template: 'helpdesk.sla_breached', title: `Ticket #${t.number} is past its due time`,
        body: `${t.confidential ? 'A confidential ticket' : `"${t.subject}"`} (${t.priority} priority) has not been resolved in time.`, payload: { ticketId: t.id }, channels: ['push'] });
    }
  }
  return { ticketsClosed: closed.rowCount ?? 0, ticketsBreached: breached.rows.length };
}
