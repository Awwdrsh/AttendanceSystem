import type { Queryable } from '../../db/pool.js';

export interface Notification {
  companyId: string | null;
  userId?: string | null;
  employeeId?: string | null;
  template: string;          // e.g. leave.requested, leave.decided, device.offline, payslip.published
  title: string;
  body: string;
  payload?: Record<string, unknown>;
  /** Extra channels besides in_app; delivery is handled by the worker. */
  channels?: Array<'email' | 'sms' | 'push' | 'webhook'>;
}

/**
 * Write to the transactional outbox (plan §13.10). Always creates an in_app row; extra channels are queued for the
 * worker to deliver. Call inside the same transaction as the business change so a rollback drops the notification too.
 */
export async function notify(db: Queryable, n: Notification): Promise<void> {
  const channels: string[] = ['in_app', ...(n.channels ?? [])];
  for (const channel of channels) {
    await db.query(
      `INSERT INTO notification_outbox (company_id, user_id, employee_id, channel, template, title, body, payload, status)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
      [n.companyId, n.userId ?? null, n.employeeId ?? null, channel, n.template, n.title, n.body, JSON.stringify(n.payload ?? {}),
        channel === 'in_app' ? 'sent' : 'queued']);
  }
}

/** Resolve the app_user for an employee, if one is linked. */
export async function userIdForEmployee(db: Queryable, employeeId: string): Promise<string | null> {
  const r = await db.query<{ id: string }>('SELECT id FROM app_user WHERE employee_id = $1 AND status = $2', [employeeId, 'active']);
  return r.rows[0]?.id ?? null;
}

/** All active users holding a role in a company (for HR/finance broadcasts). */
export async function userIdsWithRole(db: Queryable, companyId: string, role: string): Promise<string[]> {
  const r = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE company_id = $1 AND status = 'active' AND ($2 = ANY(roles) OR 'admin' = ANY(roles))`, [companyId, role]);
  return r.rows.map((x) => x.id);
}
