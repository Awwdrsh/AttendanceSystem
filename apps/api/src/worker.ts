import type { Logger } from 'pino';
import { localDateOf, localTimeMs, NPT_OFFSET_MIN, MIN_MS, DAY_MIN, parseHHMM } from '@hajiri/attendance-engine';
import { adToBs } from '@hajiri/bs-calendar';
import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';
import { sha256Hex } from './crypto.js';
import { dateRange, enqueueRecompute, recomputeDay } from './modules/attendance/service.js';
import { runLeaveCatchUp, runMonthlyAccrual } from './modules/leave/accrual.js';
import { dailyDigest, deliverQueued, LogSender, notify, notifyRole, userIdForEmployee, userIdsWithRole, WebhookSender, type Sender } from './modules/notifications/service.js';
import { ensurePseudoDevice, nptLocalTime } from './modules/punch/devices.js';

export interface WorkerDeps { db: Db; logger: Logger; cipher: FieldCipher; senders?: Sender[] }

export type JobName = 'queue' | 'nightly' | 'accrual' | 'offline' | 'deliver' | 'sla' | 'autoClockOut' | 'digest';
export const ALL_JOBS: JobName[] = ['queue', 'deliver', 'offline', 'sla', 'autoClockOut', 'nightly', 'accrual', 'digest'];

const OFFLINE_AFTER_MS = 120_000;
const OFFLINE_ESCALATE_MS = 15 * MIN_MS;

/** Senders from the environment (see modules/notifications/README.md); unset channels log only. */
export function sendersFromEnv(env: NodeJS.ProcessEnv, logger: Logger): Sender[] {
  const log = (msg: string, data: unknown) => logger.info(data, msg);
  return [
    env.SMS_WEBHOOK_URL ? new WebhookSender('sms', env.SMS_WEBHOOK_URL) : new LogSender('sms', log),
    env.EMAIL_WEBHOOK_URL ? new WebhookSender('email', env.EMAIL_WEBHOOK_URL) : new LogSender('email', log),
    env.PUSH_WEBHOOK_URL ? new WebhookSender('push', env.PUSH_WEBHOOK_URL) : new LogSender('push', log),
    env.WEBHOOK_URL ? new WebhookSender('webhook', env.WEBHOOK_URL) : new LogSender('webhook', log),
  ];
}

// ---------------------------------------------------------------- jobs
export const MAX_RECOMPUTE_ATTEMPTS = 5;

/**
 * (1) Drain recompute_queue (plan §10.3) in batches until it is empty or the time budget is spent, so a device backlog
 * clears within a tick or two instead of 50 rows every 15 s. A row that fails is re-queued with attempts + 1 (and sorts
 * after fresh work) and is dropped after MAX_RECOMPUTE_ATTEMPTS with an HR notification, so one poisoned row never blocks the queue.
 */
export async function drainRecomputeQueue(deps: WorkerDeps, opts: { batch?: number; budgetMs?: number } = {}): Promise<{ processed: number; failed: number; dropped: number }> {
  const batch = opts.batch ?? 200;
  const deadline = Date.now() + (opts.budgetMs ?? 10_000);
  const total = { processed: 0, failed: 0, dropped: 0 };
  do {
    const r = await drainBatch(deps, batch);
    total.processed += r.processed; total.failed += r.failed; total.dropped += r.dropped;
    if (r.taken < batch) break;
  } while (Date.now() < deadline);
  return total;
}

async function drainBatch(deps: WorkerDeps, batch: number): Promise<{ processed: number; failed: number; dropped: number; taken: number }> {
  const { db, logger } = deps;
  const client = await db.connect();
  let rows: Array<{ employee_id: string; local_date: string; attempts: number }> = [];
  try {
    await client.query('BEGIN');
    const r = await client.query<{ employee_id: string; local_date: string; attempts: number }>(
      `DELETE FROM recompute_queue WHERE (employee_id, local_date) IN (
         SELECT employee_id, local_date FROM recompute_queue WHERE enqueued_at <= now() ORDER BY enqueued_at LIMIT $1 FOR UPDATE SKIP LOCKED) RETURNING employee_id, local_date, attempts`, [batch]);
    rows = r.rows;
    await client.query('COMMIT');
  } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  let processed = 0, failed = 0, dropped = 0;
  for (const row of rows) {
    try { await recomputeDay(db, row.employee_id, row.local_date); processed++; }
    catch (e) {
      const attempts = row.attempts + 1;
      const message = String((e as Error).message).slice(0, 500);
      if (attempts >= MAX_RECOMPUTE_ATTEMPTS) {
        dropped++;
        logger.error({ err: e, employeeId: row.employee_id, localDate: row.local_date, attempts }, 'recompute failed repeatedly; dropped');
        const c = await db.query<{ company_id: string }>('SELECT company_id FROM employee WHERE id = $1', [row.employee_id]).catch(() => ({ rows: [] as Array<{ company_id: string }> }));
        if (c.rows[0]) {
          await notifyRole(db, c.rows[0].company_id, 'hr', { template: 'attendance.recompute_failed', title: `Attendance for ${row.local_date} could not be computed`,
            body: `Recomputing one employee's attendance for ${row.local_date} failed ${attempts} times (${message}). Check the shift and punches, then recompute from the exceptions queue.`,
            payload: { employeeId: row.employee_id, localDate: row.local_date } }).catch(() => undefined);
        }
      } else {
        failed++;
        logger.warn({ err: e, employeeId: row.employee_id, localDate: row.local_date, attempts }, 'recompute failed; re-queued');
        // Backoff: the row sorts after fresh work for `attempts` minutes.
        const retryAt = new Date(Date.now() + attempts * MIN_MS);
        await db.query(
          `INSERT INTO recompute_queue (employee_id, local_date, reason, attempts, last_error, enqueued_at) VALUES ($1, $2, 'retry', $3::int, $4, $5::timestamptz)
           ON CONFLICT (employee_id, local_date) DO UPDATE SET attempts = EXCLUDED.attempts, last_error = EXCLUDED.last_error, enqueued_at = EXCLUDED.enqueued_at`,
          [row.employee_id, row.local_date, attempts, message, retryAt]).catch((err: unknown) => logger.error({ err }, 'could not re-queue recompute'));
      }
    }
  }
  return { processed, failed, dropped, taken: rows.length };
}

/** (2) Nightly sweep: last 3 days for every active employee (plan §10.3). */
export async function nightlySweep(deps: WorkerDeps, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  const from = new Date(localTimeMs(today, '00:00') - 2 * DAY_MIN * MIN_MS);
  const dates = dateRange(localDateOf(from.getTime()), today);
  const emps = await deps.db.query<{ id: string }>(`SELECT id FROM employee WHERE status = 'active'`);
  await enqueueRecompute(deps.db, emps.rows.flatMap((e) => dates.map((d) => ({ employeeId: e.id, localDate: d }))), 'nightly');
  return emps.rows.length * dates.length;
}

/** (2b) Leave accrual for the previous BS month, once per company (guarded by company_setting leave.accrual.last). */
export async function monthlyAccrualIfDue(deps: WorkerDeps, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  let bs: { year: number; month: number };
  try { bs = adToBs(today); } catch { return 0; }
  const prev = bs.month === 1 ? { year: bs.year - 1, month: 12 } : { year: bs.year, month: bs.month - 1 };
  const key = `${prev.year}-${String(prev.month).padStart(2, '0')}`;
  const companies = await deps.db.query<{ id: string; last: string | null }>(
    `SELECT c.id, (SELECT value->>'month' FROM company_setting s WHERE s.company_id = c.id AND s.key = 'leave.accrual.last') AS last FROM company c`);
  let ran = 0;
  for (const c of companies.rows) {
    // Idempotent catch-up: missed months of this fiscal year and annual grants for people who joined after Shrawan.
    try { await runLeaveCatchUp(deps.db, c.id, today); } catch (e) { deps.logger.error({ err: e, companyId: c.id }, 'leave catch-up failed'); }
    if (c.last && c.last >= key) continue;
    try {
      const summary = await runMonthlyAccrual(deps.db, c.id, prev.year, prev.month);
      await deps.db.query(`INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'leave.accrual.last', $2) ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`,
        [c.id, JSON.stringify({ month: key, ranAt: now.toISOString(), ...summary })]);
      ran++;
    } catch (e) { deps.logger.error({ err: e, companyId: c.id, month: key }, 'leave accrual failed'); }
  }
  return ran;
}

/** (3) Terminal offline detection with dedupe and recovery (plan §7.7: offline after 120 s, page at 15 min). */
export async function detectOfflineDevices(deps: WorkerDeps, now = new Date()): Promise<{ offline: number; escalated: number; recovered: number }> {
  const { db } = deps;
  const devices = await db.query<{ id: string; company_id: string; serial_no: string; name: string | null; last_seen_at: Date | null; last_offline: Date | null; last_offline_stage: string | null; last_online: Date | null }>(
    `SELECT d.id, d.company_id, d.serial_no, d.name, d.last_seen_at,
            (SELECT max(created_at) FROM notification_outbox n WHERE n.template = 'device.offline' AND n.payload->>'deviceId' = d.id::text) AS last_offline,
            (SELECT payload->>'stage' FROM notification_outbox n WHERE n.template = 'device.offline' AND n.payload->>'deviceId' = d.id::text ORDER BY created_at DESC LIMIT 1) AS last_offline_stage,
            (SELECT max(created_at) FROM notification_outbox n WHERE n.template = 'device.online' AND n.payload->>'deviceId' = d.id::text) AS last_online
       FROM device d WHERE d.status = 'active' AND d.company_id IS NOT NULL AND d.vendor NOT IN ('mobile', 'kiosk', 'manual') AND d.last_seen_at IS NOT NULL`);
  let offline = 0, escalated = 0, recovered = 0;
  for (const d of devices.rows) {
    const silentMs = now.getTime() - d.last_seen_at!.getTime();
    const label = d.name ?? d.serial_no;
    const isOffline = silentMs > OFFLINE_AFTER_MS;
    const outageOpen = d.last_offline !== null && (d.last_online === null || d.last_online < d.last_offline);
    if (isOffline) {
      const minutes = Math.floor(silentMs / MIN_MS);
      // A new outage: no open offline notice, or the last one predates the device's most recent contact.
      if (!outageOpen || d.last_offline! < d.last_seen_at!) {
        await notifyRole(db, d.company_id, 'hr', { template: 'device.offline', title: `Terminal ${label} is offline`,
          body: `${label} has not contacted the server for ${minutes} minutes. Punches are being saved on the device.`, payload: { deviceId: d.id, stage: '2min', minutes }, channels: ['push'] });
        offline++;
      } else if (silentMs > OFFLINE_ESCALATE_MS && d.last_offline_stage !== '15min') {
        await notifyRole(db, d.company_id, 'hr', { template: 'device.offline', title: `Terminal ${label} offline for ${minutes} minutes`,
          body: `${label} is still offline. Check power and network at the terminal.`, payload: { deviceId: d.id, stage: '15min', minutes }, channels: ['push', 'sms'] });
        escalated++;
      }
    } else if (outageOpen) {
      await notifyRole(db, d.company_id, 'hr', { template: 'device.online', title: `Terminal ${label} is back online`,
        body: `${label} reconnected and is sending its buffered punches.`, payload: { deviceId: d.id } });
      recovered++;
    }
  }
  return { offline, escalated, recovered };
}

/** (5) Approval SLA reminders (24 h) and escalation to HR (48 h), once per instance per day (plan §5.8, §13.5). */
export async function slaReminders(deps: WorkerDeps, now = new Date()): Promise<{ reminded: number; escalated: number }> {
  const { db } = deps;
  const rows = await db.query<{ id: string; company_id: string; object_type: string; employee_id: string | null; steps: Array<{ role: string }>; current_step: number; created_at: Date; name_en: string | null; manager_user: string | null; sla_hours: number; escalation_hours: number; reminded_today: boolean; escalated_today: boolean }>(
    `SELECT w.id, w.company_id, w.object_type, w.employee_id, w.steps, w.current_step, w.created_at, e.name_en,
            (SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id WHERE a.employee_id = w.employee_id AND a.valid @> current_date AND u.status = 'active' LIMIT 1) AS manager_user,
            coalesce((p.value->>'slaHours')::int, 24) AS sla_hours, coalesce((p.value->>'escalationHours')::int, 48) AS escalation_hours,
            EXISTS (SELECT 1 FROM notification_outbox n WHERE n.template = 'workflow.sla' AND n.payload->>'instanceId' = w.id::text AND n.created_at > $1::timestamptz - interval '24 hours') AS reminded_today,
            EXISTS (SELECT 1 FROM notification_outbox n WHERE n.template = 'workflow.escalation' AND n.payload->>'instanceId' = w.id::text AND n.created_at > $1::timestamptz - interval '24 hours') AS escalated_today
       FROM workflow_instance w LEFT JOIN employee e ON e.id = w.employee_id
       LEFT JOIN company_setting p ON p.company_id = w.company_id AND p.key = 'notifications.policy'
      WHERE w.state = 'pending' AND w.created_at < $1::timestamptz - interval '24 hours'`, [now]);
  let reminded = 0, escalated = 0;
  for (const w of rows.rows) {
    const ageH = (now.getTime() - w.created_at.getTime()) / 3_600_000;
    const step = w.steps[w.current_step];
    const subject = `${w.object_type.replace(/_/g, ' ')}${w.name_en ? ` from ${w.name_en}` : ''}`;
    if (ageH >= w.sla_hours && !w.reminded_today) {
      let targets = step?.role === 'manager' && w.manager_user ? [w.manager_user] : await userIdsWithRole(db, w.company_id, step?.role ?? 'hr');
      if (targets.length === 0) targets = await userIdsWithRole(db, w.company_id, 'hr');   // nobody holds the role: HR must act
      for (const userId of targets) {
        await notify(db, { companyId: w.company_id, userId, template: 'workflow.sla', title: `Approval waiting ${Math.floor(ageH)} h: ${subject}`,
          body: `A ${subject} has been waiting for your decision for ${Math.floor(ageH)} hours.`, payload: { instanceId: w.id, objectType: w.object_type }, channels: ['push', 'email'] });
      }
      if (targets.length > 0) reminded++;
    }
    if (ageH >= w.escalation_hours && !w.escalated_today) {
      await notifyRole(db, w.company_id, 'hr', { template: 'workflow.escalation', title: `Escalation: ${subject} unanswered for ${Math.floor(ageH)} h`,
        body: `No decision has been made on a ${subject} for ${Math.floor(ageH)} hours. Please follow up or decide on behalf of the approver.`, payload: { instanceId: w.id, objectType: w.object_type }, channels: ['email'] });
      escalated++;
    }
  }
  return { reminded, escalated };
}

/** (6) Auto clock-out at shift end + N minutes for Incomplete days (company policy; plan §13.8), with a regularisation notice. */
export async function autoClockOut(deps: WorkerDeps, now = new Date()): Promise<number> {
  const { db } = deps;
  const companies = await db.query<{ id: string; after: number }>(
    `SELECT c.id, coalesce((s.value->>'autoClockOutAfterMin')::int, 0) AS after FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'attendance.policy'`);
  let done = 0;
  for (const c of companies.rows) {
    if (c.after <= 0) continue;
    const days = await db.query<{ employee_id: string; local_date: string; emp_code: string; start_time: string; end_time: string }>(
      `SELECT v.employee_id, v.local_date, e.emp_code, s.start_time::text, s.end_time::text
         FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id JOIN shift s ON s.id = v.shift_id
        WHERE e.company_id = $1 AND v.status = 'Incomplete' AND v.locked_at IS NULL AND v.local_date >= ($2::date - 3)
          AND NOT EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = v.employee_id AND p.local_date = v.local_date AND p.source = 'auto_clock_out')`,
      [c.id, localDateOf(now.getTime())]);
    if (days.rows.length === 0) continue;
    const device = await ensurePseudoDevice(db, c.id, 'MANUAL');
    for (const d of days.rows) {
      const start = parseHHMM(d.start_time.slice(0, 5)); const end = parseHHMM(d.end_time.slice(0, 5));
      const endMs = localTimeMs(d.local_date, d.end_time.slice(0, 5), end <= start ? 1 : 0);
      if (now.getTime() < endMs + c.after * MIN_MS) continue;
      const key = sha256Hex(`auto|${d.employee_id}|${d.local_date}`);
      const ins = await db.query<{ id: string }>(
        `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'out', 'auto', 'auto_clock_out', $9, $10) ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id`,
        [device.id, device.serialNo, device.vendor, d.emp_code, d.employee_id, new Date(endMs), nptLocalTime(endMs), d.local_date, JSON.stringify({ auto: true, afterMin: c.after, regularisationRequired: true }), key]);
      if (!ins.rows[0]) continue;
      await enqueueRecompute(db, [{ employeeId: d.employee_id, localDate: d.local_date }], 'auto_clock_out');
      const userId = await userIdForEmployee(db, d.employee_id);
      await notify(db, { companyId: c.id, userId, employeeId: d.employee_id, template: 'attendance.auto_clock_out', title: `Missing OUT punch on ${d.local_date}`,
        body: `No OUT punch was recorded on ${d.local_date}, so the shift end time was used. If you left at a different time, submit a correction.`, payload: { localDate: d.local_date }, channels: ['push'] });
      done++;
    }
  }
  return done;
}

/** (7) Manager digest of employees without a punch, once per company per day at the configured digest time. */
export async function digestIfDue(deps: WorkerDeps, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  const nptMin = (() => { const d = new Date(now.getTime() + NPT_OFFSET_MIN * MIN_MS); return d.getUTCHours() * 60 + d.getUTCMinutes(); })();
  const companies = await deps.db.query<{ id: string; digest: string | null; last: string | null }>(
    `SELECT c.id, (SELECT value->>'digestTime' FROM company_setting s WHERE s.company_id = c.id AND s.key = 'notifications.policy') AS digest,
            (SELECT max(created_at)::text FROM notification_outbox n WHERE n.company_id = c.id AND n.template = 'attendance.digest' AND n.payload->>'date' = $1) AS last FROM company c`, [today]);
  let ran = 0;
  for (const c of companies.rows) {
    if (c.last) continue;
    const [h, m] = (c.digest ?? '10:45').split(':').map(Number);
    if (nptMin < (h ?? 10) * 60 + (m ?? 45)) continue;
    await dailyDigest(deps.db, c.id, today);
    ran++;
  }
  return ran;
}

// ---------------------------------------------------------------- scheduler
/** Run the given jobs once, now, regardless of schedule (tests and admin tooling). */
export async function runOnce(deps: WorkerDeps, jobs: JobName[] = ALL_JOBS, now = new Date()): Promise<Record<string, unknown>> {
  const out: Record<string, unknown> = {};
  const senders = deps.senders ?? sendersFromEnv(process.env, deps.logger);
  for (const job of jobs) {
    try {
      switch (job) {
        case 'queue': out.queue = await drainRecomputeQueue(deps); break;
        case 'deliver': out.deliver = await deliverQueued(deps.db, senders, now); break;
        case 'offline': out.offline = await detectOfflineDevices(deps, now); break;
        case 'sla': out.sla = await slaReminders(deps, now); break;
        case 'autoClockOut': out.autoClockOut = await autoClockOut(deps, now); break;
        case 'nightly': out.nightly = await nightlySweep(deps, now); break;
        case 'accrual': out.accrual = await monthlyAccrualIfDue(deps, now); break;
        case 'digest': out.digest = await digestIfDue(deps, now); break;
      }
    } catch (e) {
      deps.logger.error({ err: e, job }, 'worker job failed');
      out[job] = { error: (e as Error).message };
    }
  }
  return out;
}

/**
 * Background worker: one 15 s tick with overlap protection. Per-job cadence: queue every tick, deliver every 30 s,
 * offline every 60 s, SLA every 10 min, auto clock-out every 5 min, digest every 5 min, nightly sweep + accrual at 00:30 NPT.
 */
export function startWorker(deps: WorkerDeps): { stop: () => void } {
  const senders = deps.senders ?? sendersFromEnv(process.env, deps.logger);
  const d = { ...deps, senders };
  const last: Partial<Record<JobName, number>> = {};
  let nightlyDoneFor = '';
  let startup = true;
  let running = false;
  const every = (job: JobName, ms: number, now: number) => (now - (last[job] ?? 0)) >= ms && (last[job] = now, true);

  const tick = async () => {
    if (running) return;
    running = true;
    try {
      const now = new Date(); const t = now.getTime();
      const jobs: JobName[] = ['queue'];
      if (every('deliver', 30_000, t)) jobs.push('deliver');
      if (every('offline', 60_000, t)) jobs.push('offline');
      if (every('sla', 10 * MIN_MS, t)) jobs.push('sla');
      if (every('autoClockOut', 5 * MIN_MS, t)) jobs.push('autoClockOut');
      if (every('digest', 5 * MIN_MS, t)) jobs.push('digest');
      const npt = new Date(t + NPT_OFFSET_MIN * MIN_MS);
      const today = npt.toISOString().slice(0, 10);
      if (npt.getUTCHours() === 0 && npt.getUTCMinutes() >= 30 && nightlyDoneFor !== today) { nightlyDoneFor = today; jobs.push('nightly', 'accrual'); }
      // Run the idempotent leave catch-up once at start so a fresh or restarted install does not wait for 00:30.
      if (startup) { startup = false; if (!jobs.includes('accrual')) jobs.push('accrual'); }
      const result = await runOnce(d, jobs, now);
      const q = result.queue as { processed: number } | undefined;
      if (q && q.processed > 0) deps.logger.info(result, 'worker tick');
    } catch (e) {
      deps.logger.error({ err: e }, 'worker tick failed');
    } finally {
      running = false;
    }
  };
  const timer = setInterval(() => { void tick(); }, 15_000);
  void tick();
  deps.logger.info('worker started');
  return { stop: () => clearInterval(timer) };
}
