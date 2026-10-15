import type { Queryable } from '../../db/pool.js';
import { NPT_OFFSET_MIN, MIN_MS } from '@hajiri/attendance-engine';

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
      [n.companyId, n.userId ?? null, n.employeeId ?? null, channel, n.template, n.title, channel === 'sms' ? stripAmounts(n.body) : n.body,
        JSON.stringify(n.payload ?? {}), channel === 'in_app' ? 'sent' : 'queued']);
  }
}

/** Resolve the app_user for an employee, if one is linked. */
export async function userIdForEmployee(db: Queryable, employeeId: string): Promise<string | null> {
  const r = await db.query<{ id: string }>('SELECT id FROM app_user WHERE employee_id = $1 AND status = $2', [employeeId, 'active']);
  return r.rows[0]?.id ?? null;
}

/** All active users holding a role in a company (for HR broadcasts). */
export async function userIdsWithRole(db: Queryable, companyId: string, role: string): Promise<string[]> {
  const r = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE company_id = $1 AND status = 'active' AND ($2 = ANY(roles) OR 'admin' = ANY(roles))`, [companyId, role]);
  return r.rows.map((x) => x.id);
}

/** Notify every user with a role (one in_app row each). */
export async function notifyRole(db: Queryable, companyId: string, role: string, n: Omit<Notification, 'companyId' | 'userId'>): Promise<number> {
  const ids = await userIdsWithRole(db, companyId, role);
  for (const userId of ids) await notify(db, { ...n, companyId, userId });
  return ids.length;
}

/** Salary figures never travel over SMS (plan §13.10): strip currency amounts and long digit groups. */
export function stripAmounts(text: string): string {
  return text
    .replace(/(NPR|Rs\.?|रु\.?)\s?[\d,]+(\.\d+)?/gi, '[amount]')
    .replace(/\b\d{1,3}(,\d{2,3})+(\.\d+)?\b/g, '[amount]')
    .replace(/\b\d{5,}(\.\d+)?\b/g, '[amount]');
}

/** Body for a payslip-published notification: period only, no amounts. */
export function payslipPublishedNotification(periodLabel: string): Pick<Notification, 'template' | 'title' | 'body' | 'channels'> {
  return { template: 'payslip.published', title: `Payslip for ${periodLabel} is ready`, body: `Your payslip for ${periodLabel} is ready to view in Hajiri.`, channels: ['push', 'sms'] };
}

// ---------------------------------------------------------------- delivery (worker)
export interface OutboxRow {
  id: string; company_id: string | null; user_id: string | null; employee_id: string | null; channel: string; template: string;
  title: string; body: string; payload: Record<string, unknown>; attempts: number;
  recipient_email: string | null; recipient_phone: string | null;
}

export interface Sender {
  channel: 'email' | 'sms' | 'push' | 'webhook';
  send(row: OutboxRow): Promise<void>;
}

/** Development sender: logs instead of sending. */
export class LogSender implements Sender {
  constructor(readonly channel: Sender['channel'], private readonly log: (msg: string, data: unknown) => void = () => undefined) {}
  async send(row: OutboxRow): Promise<void> {
    this.log(`notification ${this.channel}`, { id: row.id, to: row.recipient_email ?? row.recipient_phone, title: row.title });
  }
}

/** Generic HTTP sender: POSTs JSON to an SMS/email/push gateway (Sparrow SMS, an SMTP bridge, etc.). */
export class WebhookSender implements Sender {
  constructor(readonly channel: Sender['channel'], private readonly url: string, private readonly fetchFn: typeof fetch = fetch) {}
  async send(row: OutboxRow): Promise<void> {
    const to = this.channel === 'sms' || this.channel === 'push' ? row.recipient_phone : row.recipient_email;
    if (!to && this.channel !== 'webhook') throw new Error(`no recipient for ${this.channel}`);
    const res = await this.fetchFn(this.url, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ channel: this.channel, to, title: row.title, body: row.body, template: row.template, payload: row.payload, id: row.id }),
    });
    if (!res.ok) throw new Error(`${this.channel} gateway responded ${res.status}`);
  }
}

export interface QuietHours { start: string; end: string }
const DEFAULT_QUIET: QuietHours = { start: '21:00', end: '07:00' };

function nptMinutes(now: Date): number {
  const d = new Date(now.getTime() + NPT_OFFSET_MIN * MIN_MS);
  return d.getUTCHours() * 60 + d.getUTCMinutes();
}
function hm(s: string): number { const [h, m] = s.split(':').map(Number); return (h ?? 0) * 60 + (m ?? 0); }

/** True when `now` (NPT) is inside the quiet window; handles windows crossing midnight. */
export function inQuietHours(now: Date, q: QuietHours = DEFAULT_QUIET): boolean {
  const n = nptMinutes(now); const s = hm(q.start); const e = hm(q.end);
  return s <= e ? n >= s && n < e : n >= s || n < e;
}

/** Next instant at which quiet hours end (NPT). */
export function quietHoursEnd(now: Date, q: QuietHours = DEFAULT_QUIET): Date {
  const n = nptMinutes(now); const e = hm(q.end);
  const minutesAhead = e > n ? e - n : 24 * 60 - n + e;
  return new Date(now.getTime() + minutesAhead * MIN_MS);
}

const MAX_ATTEMPTS = 5;

/**
 * Deliver queued outbox rows through the configured senders. Retries with quadratic backoff up to 5 attempts;
 * sms/push are deferred (not dropped) during quiet hours (plan §13.10).
 */
export async function deliverQueued(db: Queryable, senders: Sender[], now = new Date(), limit = 100): Promise<{ sent: number; failed: number; deferred: number }> {
  const byChannel = new Map(senders.map((s) => [s.channel, s]));
  const rows = await db.query<OutboxRow>(
    `SELECT n.id, n.company_id, n.user_id, n.employee_id, n.channel, n.template, n.title, n.body, n.payload, n.attempts,
            coalesce(n.to_email, u.email, e.email) AS recipient_email, e.phone AS recipient_phone
       FROM notification_outbox n
       LEFT JOIN app_user u ON u.id = n.user_id
       LEFT JOIN employee e ON e.id = coalesce(n.employee_id, u.employee_id)
      WHERE n.status = 'queued' AND n.channel <> 'in_app' AND n.next_attempt_at <= $1
      ORDER BY n.created_at LIMIT $2`, [now, limit]);
  let sent = 0, failed = 0, deferred = 0;
  const quietCache = new Map<string, QuietHours>();
  for (const row of rows.rows) {
    if (row.channel === 'sms' || row.channel === 'push') {
      let q = quietCache.get(row.company_id ?? '');
      if (!q) {
        const s = await db.query<{ value: { quietHoursStart?: string; quietHoursEnd?: string } }>(
          `SELECT value FROM company_setting WHERE company_id = $1 AND key = 'notifications.policy'`, [row.company_id]);
        q = { start: s.rows[0]?.value.quietHoursStart ?? DEFAULT_QUIET.start, end: s.rows[0]?.value.quietHoursEnd ?? DEFAULT_QUIET.end };
        quietCache.set(row.company_id ?? '', q);
      }
      if (inQuietHours(now, q)) {
        await db.query('UPDATE notification_outbox SET next_attempt_at = $2 WHERE id = $1', [row.id, quietHoursEnd(now, q)]);
        deferred++;
        continue;
      }
    }
    const sender = byChannel.get(row.channel as Sender['channel']);
    try {
      if (!sender) throw new Error(`no sender configured for ${row.channel}`);
      await sender.send(row);
      await db.query(`UPDATE notification_outbox SET status = 'sent', sent_at = $2, attempts = attempts + 1 WHERE id = $1`, [row.id, now]);
      sent++;
    } catch (e) {
      const attempts = row.attempts + 1;
      const giveUp = attempts >= MAX_ATTEMPTS;
      const backoffMin = attempts * attempts;
      await db.query(
        `UPDATE notification_outbox SET attempts = $2, last_error = $3, status = $4, next_attempt_at = $5 WHERE id = $1`,
        [row.id, attempts, String((e as Error).message).slice(0, 500), giveUp ? 'failed' : 'queued', new Date(now.getTime() + backoffMin * MIN_MS)]);
      failed++;
    }
  }
  return { sent, failed, deferred };
}

/** Manager digest: employees with no punch yet on a working day (plan §13.10 "missing punch" daily digest). */
export async function dailyDigest(db: Queryable, companyId: string, localDate: string): Promise<{ managers: number; missing: number }> {
  const missing = await db.query<{ emp_code: string; name_en: string }>(
    `SELECT e.emp_code, e.name_en FROM employee e
      WHERE e.company_id = $1 AND e.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = e.id AND p.local_date = $2)
        AND NOT EXISTS (SELECT 1 FROM leave_request l WHERE l.employee_id = e.id AND l.status = 'approved' AND $2::date BETWEEN l.from_date AND l.to_date AND l.from_time IS NULL)
        AND NOT EXISTS (SELECT 1 FROM holiday h WHERE h.company_id = e.company_id AND h.local_date = $2 AND h.branch_id IS NULL AND NOT h.optional)
        AND NOT (extract(dow from $2::date)::int = ANY(e.weekly_off_days))
      ORDER BY e.emp_code`, [companyId, localDate]);
  if (missing.rows.length === 0) return { managers: 0, missing: 0 };
  const names = missing.rows.slice(0, 30).map((m) => `${m.name_en} (${m.emp_code})`).join(', ');
  const more = missing.rows.length > 30 ? ` and ${missing.rows.length - 30} more` : '';
  const managers = await notifyRole(db, companyId, 'manager', {
    template: 'attendance.digest', title: `${missing.rows.length} employees have not punched today`,
    body: `No punch recorded yet for: ${names}${more}.`, payload: { date: localDate, count: missing.rows.length },
  });
  return { managers, missing: missing.rows.length };
}
