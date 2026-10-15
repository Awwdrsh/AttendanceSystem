import { bsLabel } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { ConflictError, NotFoundError } from '../../errors.js';
import { notify, notifyRole, userIdForEmployee } from '../notifications/service.js';
import { firstTime } from '../performance/service.js';
import { nptToday, registerHandler, type DecisionContext } from '../workflow/service.js';

/** 'YYYY-MM-DD' plus whole months, clamped to the end of a shorter month (31 Jan + 1 month = 28/29 Feb). */
export function addMonths(iso: string, months: number): string {
  const [y, m, d] = iso.split('-').map(Number) as [number, number, number];
  const target = new Date(Date.UTC(y, m - 1 + months, 1));
  const last = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  target.setUTCDate(Math.min(d, last));
  return target.toISOString().slice(0, 10);
}

/** Mark an enrolment complete and issue the certificate dates from the course's validity. */
export async function completeEnrollment(db: Queryable, enrollmentId: string, opts: { attended?: boolean } = {}): Promise<void> {
  const r = await db.query<{ status: string; validity_months: number | null; company_id: string; employee_id: string; title: string }>(
    `SELECT e.status, c.validity_months, e.company_id, e.employee_id, c.title FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id WHERE e.id = $1 FOR UPDATE OF e`, [enrollmentId]);
  const e = r.rows[0];
  if (!e) throw new NotFoundError('enrolment not found');
  if (e.status === 'completed') return;
  if (e.status !== 'enrolled') throw new ConflictError(`this enrolment is ${e.status.replace('_', ' ')}`);
  const today = nptToday();
  await db.query(
    `UPDATE lrn_enrollment SET status = 'completed', completed_at = now(), attended = coalesce($2, attended),
            certificate_issued_on = $3, certificate_expires_on = $4, expiry_reminded_at = NULL WHERE id = $1`,
    [enrollmentId, opts.attended ?? null, today, e.validity_months ? addMonths(today, e.validity_months) : null]);
  await notify(db, { companyId: e.company_id, userId: await userIdForEmployee(db, e.employee_id), employeeId: e.employee_id, template: 'learning.completed',
    title: 'Training completed', body: `You completed ${e.title}. Your certificate is under Growth, Learning.`, payload: { enrollmentId } });
}

/** Training needs from a released review become enrolments the employee can see. */
export async function createReviewNominations(db: Queryable, companyId: string, employeeId: string, reviewId: string, courseIds: string[], actorId: string): Promise<number> {
  let n = 0;
  for (const courseId of courseIds) {
    const c = await db.query<{ cost: string; title: string }>('SELECT cost, title FROM lrn_course WHERE id = $1 AND company_id = $2 AND active', [courseId, companyId]);
    if (!c.rows[0]) continue;
    const ins = await db.query(
      `INSERT INTO lrn_enrollment (company_id, course_id, employee_id, source, status, review_id, assigned_by, cost) VALUES ($1, $2, $3, 'review', 'enrolled', $4, $5, $6) ON CONFLICT DO NOTHING`,
      [companyId, courseId, employeeId, reviewId, actorId, c.rows[0].cost]);
    if (ins.rowCount) {
      n++;
      await notify(db, { companyId, userId: await userIdForEmployee(db, employeeId), employeeId, template: 'learning.assigned', title: 'Training assigned', body: `From your review: ${c.rows[0].title}.`, payload: { courseId } });
    }
  }
  return n;
}

export async function applyNominationDecision(db: Queryable, enrollmentId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ status: string; company_id: string; employee_id: string; title: string }>(
    'SELECT e.status, e.company_id, e.employee_id, c.title FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id WHERE e.id = $1 FOR UPDATE OF e', [enrollmentId]);
  const e = r.rows[0];
  if (!e) throw new NotFoundError('enrolment not found');
  if (e.status !== 'pending_approval') throw new ConflictError(`nomination already ${e.status}`);
  await db.query('UPDATE lrn_enrollment SET status = $2 WHERE id = $1', [enrollmentId, state === 'approved' ? 'enrolled' : 'rejected']);
  await notify(db, { companyId: e.company_id, userId: await userIdForEmployee(db, e.employee_id), employeeId: e.employee_id, template: `learning.nomination_${state}`,
    title: state === 'approved' ? 'Training approved' : 'Training not approved', body: `${e.title}${ctx.reason ? ` (${ctx.reason})` : ''}.`, payload: { enrollmentId } });
}
registerHandler('training_nomination', applyNominationDecision);

/**
 * Daily: certificates of mandatory or expiring courses that run out within 30 days (employee and HR are told once per
 * certificate), and reminders the day before a scheduled session.
 */
export async function learningReminders(db: Queryable, now = new Date()): Promise<{ expiring: number; sessions: number }> {
  const today = nptToday();
  void now;
  const exp = await db.query<{ id: string; company_id: string; employee_id: string; title: string; certificate_expires_on: string; name_en: string }>(
    `SELECT e.id, e.company_id, e.employee_id, c.title, e.certificate_expires_on, em.name_en
       FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id JOIN employee em ON em.id = e.employee_id
      WHERE e.status = 'completed' AND e.certificate_expires_on IS NOT NULL AND e.expiry_reminded_at IS NULL AND em.status = 'active'
        AND e.certificate_expires_on <= $1::date + 30
        AND NOT EXISTS (SELECT 1 FROM lrn_enrollment n WHERE n.course_id = e.course_id AND n.employee_id = e.employee_id AND n.created_at > e.created_at)`, [today]);
  for (const e of exp.rows) {
    const past = e.certificate_expires_on < today;
    const body = `${e.title} certificate ${past ? 'expired' : 'expires'} on ${bsLabel(e.certificate_expires_on)}.`;
    await notify(db, { companyId: e.company_id, userId: await userIdForEmployee(db, e.employee_id), employeeId: e.employee_id, template: 'learning.certificate_expiring', title: past ? 'Certificate expired' : 'Certificate expiring', body: `${body} Ask HR for a refresher session.`, payload: { enrollmentId: e.id } });
    await notifyRole(db, e.company_id, 'hr', { employeeId: e.employee_id, template: 'learning.certificate_expiring', title: past ? 'Certificate expired' : 'Certificate expiring', body: `${e.name_en}: ${body}`, payload: { enrollmentId: e.id } });
    await db.query('UPDATE lrn_enrollment SET expiry_reminded_at = now() WHERE id = $1', [e.id]);
  }
  const sess = await db.query<{ id: string; company_id: string; employee_id: string; title: string; starts_at: Date; venue: string | null }>(
    `SELECT e.id, e.company_id, e.employee_id, c.title, s.starts_at, s.venue FROM lrn_enrollment e JOIN lrn_session s ON s.id = e.session_id JOIN lrn_course c ON c.id = e.course_id
      WHERE e.status = 'enrolled' AND s.status = 'scheduled' AND (s.starts_at AT TIME ZONE 'Asia/Kathmandu')::date = $1::date + 1`, [today]);
  let sessions = 0;
  for (const e of sess.rows) {
    if (!(await firstTime(db, `session:${e.id}:${today}`))) continue;
    sessions++;
    await notify(db, { companyId: e.company_id, userId: await userIdForEmployee(db, e.employee_id), employeeId: e.employee_id, template: 'learning.session_tomorrow', title: 'Training tomorrow', body: `${e.title}${e.venue ? ` at ${e.venue}` : ''}.`, payload: { enrollmentId: e.id } });
  }
  return { expiring: exp.rows.length, sessions };
}
