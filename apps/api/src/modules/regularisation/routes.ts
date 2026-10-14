import { createHash } from 'node:crypto';
import { bsLabel } from '../../bsdate.js';
import { Router } from 'express';
import { z } from 'zod';
import { localDateOf, NPT_OFFSET_MIN, MIN_MS } from '@hajiri/attendance-engine';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { hasRole } from '../auth/middleware.js';
import { enqueueRecompute } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { act, assertManages, cancelWorkflow, nptToday, registerHandler, startWorkflow, type DecisionContext } from '../workflow/service.js';

const KINDS = ['missed_punch', 'wfh', 'on_duty', 'half_day', 'shift_change'] as const;

/** Device row used for HR-entered and regularised punches (one per company; vendor/protocol 'manual'). */
export async function manualDeviceId(db: Queryable, companyId: string): Promise<string> {
  const c = await db.query<{ code: string }>('SELECT code FROM company WHERE id = $1', [companyId]);
  const sn = `MANUAL-${c.rows[0]?.code ?? companyId.slice(0, 8)}`;
  const r = await db.query<{ id: string }>(
    `INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES ($1, $2, 'manual', 'manual', 'Manual entries', 'active')
     ON CONFLICT (serial_no) DO UPDATE SET status = 'active' RETURNING id`, [companyId, sn]);
  return r.rows[0]!.id;
}

function deviceLocalTime(at: Date): string {
  const d = new Date(at.getTime() + NPT_OFFSET_MIN * MIN_MS);
  return d.toISOString().slice(0, 19).replace('T', ' ');
}

/** Applies an approved regularisation (plan §5.4): a missed punch becomes a real, attributable punch_event row. */
export async function applyRegularisationDecision(db: Queryable, requestId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ employee_id: string; local_date: string; kind: string; punch_time: Date | null; punch_direction: 'in' | 'out' | null; status: string; company_id: string; emp_code: string }>(
    `SELECT rr.employee_id, rr.local_date, rr.kind, rr.punch_time, rr.punch_direction, rr.status, e.company_id, e.emp_code
       FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id WHERE rr.id = $1 FOR UPDATE`, [requestId]);
  const rr = r.rows[0];
  if (!rr) throw new NotFoundError('regularisation request not found');
  if (rr.status !== 'pending') throw new ConflictError(`request already ${rr.status}`);
  await db.query('UPDATE regularisation_request SET status = $2, decided_at = now() WHERE id = $1', [requestId, state]);
  if (state === 'approved') {
    if (rr.kind === 'missed_punch' && rr.punch_time) {
      const deviceId = await manualDeviceId(db, rr.company_id);
      const key = createHash('sha256').update(`regularisation|${requestId}`).digest('hex');
      const p = await db.query<{ id: string }>(
        `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
         SELECT $1, d.serial_no, 'manual', $2, $3, $4, $5, $6, $7, 'manual', 'regularisation', $8, $9 FROM device d WHERE d.id = $1
         ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id`,
        [deviceId, rr.emp_code, rr.employee_id, rr.punch_time, deviceLocalTime(rr.punch_time), localDateOf(rr.punch_time.getTime()), rr.punch_direction ?? 'unknown',
          JSON.stringify({ requestId, approvedBy: ctx.actorId }), key]);
      if (p.rows[0]) await db.query('UPDATE regularisation_request SET resulting_punch_id = $2 WHERE id = $1', [requestId, p.rows[0].id]);
    }
    await enqueueRecompute(db, [{ employeeId: rr.employee_id, localDate: rr.local_date }], 'regularisation_approved');
  }
  const userId = await userIdForEmployee(db, rr.employee_id);
  await notify(db, {
    companyId: rr.company_id, userId, employeeId: rr.employee_id, template: `regularisation.${state}`,
    title: state === 'approved' ? 'Attendance correction approved' : 'Attendance correction rejected',
    body: `${rr.kind === 'wfh' ? 'Work from home' : rr.kind.replace('_', ' ')} for ${bsLabel(String(rr.local_date).slice(0, 10))} was ${state}${ctx.reason ? `: ${ctx.reason}` : ''}.`, payload: { requestId }, channels: ['push'],
  });
}
registerHandler('regularisation_request', applyRegularisationDecision);

export function regularisationRoutes({ db }: AppDeps): Router {
  const r = Router();

  const policy = async (companyId: string): Promise<{ backdateDays: number; monthlyCap: number }> => {
    const s = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'attendance.policy'`, [companyId]);
    const v = s.rows[0]?.value ?? {};
    return { backdateDays: typeof v.regularisationBackdateDays === 'number' ? v.regularisationBackdateDays : 30, monthlyCap: typeof v.regularisationMonthlyCap === 'number' ? v.regularisationMonthlyCap : 5 };
  };

  r.post('/', async (req, res) => {
    const b = z.object({
      employeeId: z.uuid().optional(), localDate: isoDate, kind: z.enum(KINDS), punchTime: z.string().datetime({ offset: true }).optional(),
      punchDirection: z.enum(['in', 'out']).optional(), shiftId: z.uuid().optional(), reason: z.string().min(3).max(1000), attachmentKey: z.string().max(200).optional(),
    }).parse(req.body);
    const u = req.user!;
    const employeeId = b.employeeId ?? u.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const onBehalf = employeeId !== u.employeeId;
    const emp = await db.query<{ company_id: string; status: string }>('SELECT company_id, status FROM employee WHERE id = $1', [employeeId]);
    if (!emp.rows[0] || emp.rows[0].company_id !== u.companyId) throw new NotFoundError('employee not found');
    if (onBehalf) await assertManages(db, u, employeeId);
    if (b.kind === 'missed_punch' && (!b.punchTime || !b.punchDirection)) throw new BadRequestError('punchTime and punchDirection are required for a missed punch');
    if (b.kind === 'shift_change') {
      if (!b.shiftId) throw new BadRequestError('shiftId is required for a shift change');
      const sh = await db.query('SELECT 1 FROM shift WHERE id = $1 AND company_id = $2', [b.shiftId, u.companyId]);
      if (!sh.rows[0]) throw new NotFoundError('shift not found');
    }
    if (b.punchTime) {
      // Night shifts: the OUT punch may fall on the next calendar day (plan §10.1 attribution to the start date); never earlier.
      const punchDay = localDateOf(new Date(b.punchTime).getTime());
      const diff = (new Date(`${punchDay}T00:00:00Z`).getTime() - new Date(`${b.localDate}T00:00:00Z`).getTime()) / 86_400_000;
      if (diff < 0 || diff > 1) throw new BadRequestError('punchTime must be on the attendance date or the following day');
    }
    const today = nptToday();
    const daysAhead = Math.floor((new Date(`${b.localDate}T00:00:00Z`).getTime() - new Date(`${today}T00:00:00Z`).getTime()) / 86_400_000);
    // Work-from-home days are planned ahead (up to 60 days); every other correction is for a day that has happened.
    if (b.kind === 'wfh' ? daysAhead > 60 : daysAhead > 0) throw new BadRequestError(b.kind === 'wfh' ? 'work from home can be requested up to 60 days ahead' : 'cannot regularise a future date');
    const pol = await policy(emp.rows[0].company_id);
    const isHr = hasRole(u, 'hr');
    if (!isHr) {
      const ageDays = Math.floor((new Date(`${today}T00:00:00Z`).getTime() - new Date(`${b.localDate}T00:00:00Z`).getTime()) / 86_400_000);
      if (ageDays > pol.backdateDays) throw new BadRequestError(`corrections can only be requested within ${pol.backdateDays} days`);
      const month = b.localDate.slice(0, 7);
      if (b.kind !== 'wfh') {
      // The cap limits what an employee files for themself; HR/manager corrections (device outages) do not count against it.
      const cnt = await db.query<{ n: string }>(`SELECT count(*)::text AS n FROM regularisation_request WHERE employee_id = $1 AND to_char(local_date, 'YYYY-MM') = $2 AND status IN ('pending', 'approved') AND created_by = $3 AND kind <> 'wfh'`, [employeeId, month, u.id]);
      if (Number(cnt.rows[0]!.n) >= pol.monthlyCap) throw new BadRequestError(`monthly limit of ${pol.monthlyCap} corrections reached; contact HR`);
      }
    }
    const locked = await db.query('SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date = $2 AND locked_at IS NOT NULL', [employeeId, b.localDate]);
    if (locked.rows[0]) throw new ConflictError('this day is locked by payroll; HR must raise a supplementary correction');
    const dup = await db.query('SELECT 1 FROM regularisation_request WHERE employee_id = $1 AND local_date = $2 AND kind = $3 AND status = $4', [employeeId, b.localDate, b.kind, 'pending']);
    if (dup.rows[0]) throw new ConflictError('a pending request of this kind already exists for that day');

    const client = await db.connect();
    let id = ''; let state = 'pending';
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>(
        `INSERT INTO regularisation_request (employee_id, local_date, kind, punch_time, punch_direction, shift_id, reason, attachment_key, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
        [employeeId, b.localDate, b.kind, b.punchTime ? new Date(b.punchTime) : null, b.punchDirection ?? null, b.shiftId ?? null, b.reason, b.attachmentKey ?? null, u.id]);
      id = ins.rows[0]!.id;
      const inst = await startWorkflow(client, {
        companyId: emp.rows[0].company_id, objectType: 'regularisation_request', objectId: id, employeeId,
        title: 'Attendance correction to approve', body: `${b.kind === 'wfh' ? 'Work from home' : b.kind.replace('_', ' ')} on ${bsLabel(b.localDate)}: ${b.reason}`,
      });
      await client.query('UPDATE regularisation_request SET workflow_instance_id = $2 WHERE id = $1', [id, inst.id]);
      await audit(client, { actorId: u.id, action: 'create', table: 'regularisation_request', rowId: id, after: { ...b, employeeId, onBehalf } });
      // HR fixing a punch on behalf of a worker is approved immediately but still leaves a signed action (plan §3 "Fix punch (audited)").
      if (isHr && onBehalf && b.kind === 'missed_punch') {
        state = await act(client, { instanceId: inst.id, user: u, decision: 'approve', reason: 'fixed by HR on behalf of the employee' });
      }
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.status(201).json({ id, status: state === 'approved' ? 'approved' : 'pending' });
  });

  r.get('/', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), status: z.enum(['pending', 'approved', 'rejected', 'cancelled']).optional(), from: isoDate.optional(), to: isoDate.optional(), scope: z.enum(['own', 'team', 'all']).optional() }).parse(req.query);
    const u = req.user!;
    const scope = q.scope ?? (hasRole(u, 'hr') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own');
    if (scope === 'all' && !hasRole(u, 'hr')) throw new ForbiddenError('scope all requires HR');
    if (q.employeeId) await assertManages(db, u, q.employeeId);
    const rows = await db.query(
      `SELECT rr.id, rr.employee_id, e.emp_code, e.name_en, rr.local_date, rr.kind, rr.punch_time, rr.punch_direction, rr.shift_id, rr.reason, rr.attachment_key, rr.status,
              rr.workflow_instance_id, rr.resulting_punch_id, rr.created_by, rr.created_at, rr.decided_at
         FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND ($2::uuid IS NULL OR rr.employee_id = $2) AND ($3::text IS NULL OR rr.status = $3)
          AND ($4::date IS NULL OR rr.local_date >= $4) AND ($5::date IS NULL OR rr.local_date <= $5)
          AND CASE $6 WHEN 'own' THEN rr.employee_id = $7::uuid WHEN 'team' THEN (a.manager_id = $7::uuid OR rr.employee_id = $7::uuid) ELSE true END
        ORDER BY rr.created_at DESC LIMIT 500`, [u.companyId, q.employeeId ?? null, q.status ?? null, q.from ?? null, q.to ?? null, scope, u.employeeId]);
    res.json({ requests: rows.rows });
  });

  r.post('/:id/cancel', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const r0 = await db.query<{ employee_id: string; status: string; company_id: string }>('SELECT rr.employee_id, rr.status, e.company_id FROM regularisation_request rr JOIN employee e ON e.id = rr.employee_id WHERE rr.id = $1', [id]);
    const rr = r0.rows[0];
    if (!rr || rr.company_id !== req.user!.companyId) throw new NotFoundError('request not found');
    await assertManages(db, req.user!, rr.employee_id);
    // Approved corrections already produced a punch/decision; they cannot be undone here (a new request or HR adjustment supersedes).
    if (rr.status !== 'pending') throw new ConflictError(`only pending requests can be cancelled (status ${rr.status})`);
    await db.query(`UPDATE regularisation_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
    await cancelWorkflow(db, 'regularisation_request', id);
    await audit(db, { actorId: req.user!.id, action: 'cancel', table: 'regularisation_request', rowId: id });
    res.json({ ok: true });
  });

  return r;
}
