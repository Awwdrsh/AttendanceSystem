import { bsLabel } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { learningReminders } from '../learning/service.js';
import { notify, notifyRole, userIdForEmployee } from '../notifications/service.js';
import { nptToday } from '../workflow/service.js';
import { firstTime } from './service.js';

const REMIND_DAYS = [3, 1, 0];

const daysBetween = (from: string, to: string) => Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);

/**
 * Daily: reminders 3 days, 1 day and on the day a review phase is due, to whoever still has something to submit;
 * a reminder the day before a 1-on-1; and the learning reminders (certificates expiring, sessions tomorrow).
 * Each reminder is sent once (perf_reminder_sent).
 */
export async function performanceReminders(db: Queryable, now = new Date()): Promise<{ reviews: number; oneOnOnes: number; learning: { expiring: number; sessions: number } }> {
  const today = nptToday();
  let reviews = 0;
  const cycles = await db.query<{ id: string; company_id: string; name: string; status: string; self_due: string | null; manager_due: string | null; peer_due: string | null; calibration_due: string | null }>(
    `SELECT id, company_id, name, status, self_due, manager_due, peer_due, calibration_due FROM perf_cycle WHERE status IN ('self_review', 'manager_review', 'peer_review', 'calibration')`);
  for (const c of cycles.rows) {
    const due = { self_review: c.self_due, manager_review: c.manager_due, peer_review: c.peer_due, calibration: c.calibration_due }[c.status as 'self_review'];
    if (!due) continue;
    const left = daysBetween(today, due);
    if (!REMIND_DAYS.includes(left)) continue;
    const when = left === 0 ? 'today' : `on ${bsLabel(due)}`;
    let targets: string[] = [];
    if (c.status === 'self_review') {
      targets = (await db.query<{ employee_id: string }>('SELECT employee_id FROM perf_review WHERE cycle_id = $1 AND self_submitted_at IS NULL', [c.id])).rows.map((x) => x.employee_id);
    } else if (c.status === 'manager_review') {
      targets = (await db.query<{ m: string }>('SELECT DISTINCT manager_employee_id AS m FROM perf_review WHERE cycle_id = $1 AND manager_submitted_at IS NULL AND manager_employee_id IS NOT NULL', [c.id])).rows.map((x) => x.m);
    } else if (c.status === 'peer_review') {
      targets = (await db.query<{ p: string }>(`SELECT DISTINCT p.reviewer_employee_id AS p FROM perf_peer p JOIN perf_review r ON r.id = p.review_id WHERE r.cycle_id = $1 AND p.status = 'approved'`, [c.id])).rows.map((x) => x.p);
    } else if (await firstTime(db, `cycle:${c.id}:calibration:${left}`)) {
      await notifyRole(db, c.company_id, 'hr', { template: 'review.reminder', title: `${c.name}: calibration due ${when}`, body: 'Set final ratings and release the reviews.', payload: { cycleId: c.id } });
      reviews++;
    }
    const what = { self_review: 'your self review', manager_review: 'your team\'s reviews', peer_review: 'the peer feedback you were asked for' }[c.status as 'self_review'];
    for (const emp of targets) {
      if (!(await firstTime(db, `cycle:${c.id}:${c.status}:${emp}:${left}`))) continue;
      await notify(db, { companyId: c.company_id, userId: await userIdForEmployee(db, emp), employeeId: emp, template: 'review.reminder', title: `${c.name}: due ${when}`, body: `Please complete ${what}.`, payload: { cycleId: c.id } });
      reviews++;
    }
  }

  let oneOnOnes = 0;
  const meetings = await db.query<{ id: string; company_id: string; manager_employee_id: string; employee_id: string; mn: string; en: string }>(
    `SELECT o.id, o.company_id, o.manager_employee_id, o.employee_id, m.name_en AS mn, e.name_en AS en FROM perf_one_on_one o
       JOIN employee m ON m.id = o.manager_employee_id JOIN employee e ON e.id = o.employee_id
      WHERE o.status = 'scheduled' AND (o.scheduled_at AT TIME ZONE 'Asia/Kathmandu')::date = $1::date + 1`, [today]);
  for (const m of meetings.rows) {
    if (!(await firstTime(db, `1on1:${m.id}:${today}`))) continue;
    for (const [emp, other] of [[m.manager_employee_id, m.en], [m.employee_id, m.mn]] as const) {
      await notify(db, { companyId: m.company_id, userId: await userIdForEmployee(db, emp), employeeId: emp, template: 'one_on_one.reminder', title: '1-on-1 tomorrow', body: `With ${other}. Add agenda items under Growth, 1-on-1s.`, payload: { meetingId: m.id } });
    }
    oneOnOnes++;
  }
  return { reviews, oneOnOnes, learning: await learningReminders(db, now) };
}
