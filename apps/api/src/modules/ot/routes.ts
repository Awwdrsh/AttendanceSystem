import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { assertAccessEmployee, hasRole, requireRole } from '../auth/middleware.js';
import { enqueueRecompute } from '../attendance/service.js';
import { creditCompOff } from '../leave/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { cancelWorkflow, registerHandler, startWorkflow, type DecisionContext } from '../workflow/service.js';

/** Labour Act 2074 Sec 30(1): 4 h/day, 24 h/week. Never relaxed by settings. */
const CAP_DAY_MIN = 240;
const CAP_WEEK_MIN = 1440;

export async function applyOtDecision(db: Queryable, requestId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ employee_id: string; local_date: string; requested_min: number; status: string; company_id: string }>(
    'SELECT o.employee_id, o.local_date, o.requested_min, o.status, e.company_id FROM ot_request o JOIN employee e ON e.id = o.employee_id WHERE o.id = $1 FOR UPDATE', [requestId]);
  const o = r.rows[0];
  if (!o) throw new NotFoundError('OT request not found');
  if (o.status !== 'pending') throw new ConflictError(`request already ${o.status}`);
  let approved: number | null = null;
  if (state === 'approved') {
    const asked = typeof ctx.payload.approvedMin === 'number' ? ctx.payload.approvedMin : o.requested_min;
    approved = Math.min(o.requested_min, Math.max(0, asked), CAP_DAY_MIN);
  }
  await db.query('UPDATE ot_request SET status = $2, approved_min = $3, decided_at = now() WHERE id = $1', [requestId, state, approved]);
  if (state === 'approved') await enqueueRecompute(db, [{ employeeId: o.employee_id, localDate: o.local_date }], 'ot_preapproved');
  const userId = await userIdForEmployee(db, o.employee_id);
  await notify(db, {
    companyId: o.company_id, userId, employeeId: o.employee_id, template: `ot.${state}`, title: state === 'approved' ? 'Overtime approved' : 'Overtime rejected',
    body: `Overtime on ${o.local_date}: ${state === 'approved' ? `${approved} minutes approved` : 'rejected'}${ctx.reason ? ` (${ctx.reason})` : ''}.`, payload: { requestId }, channels: ['push'],
  });
}
registerHandler('ot_request', applyOtDecision);

export function otRoutes({ db }: AppDeps): Router {
  const r = Router();

  const weekStart = (d: string) => new Date(new Date(`${d}T00:00:00Z`).getTime() - new Date(`${d}T00:00:00Z`).getUTCDay() * 86_400_000).toISOString().slice(0, 10);

  r.post('/requests', async (req, res) => {
    const b = z.object({ employeeId: z.uuid().optional(), localDate: isoDate, requestedMin: z.number().int().min(1), reason: z.string().max(500).optional() }).parse(req.body);
    const u = req.user!;
    const employeeId = b.employeeId ?? u.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    if (employeeId !== u.employeeId && !hasRole(u, 'hr', 'manager')) throw new ForbiddenError('cannot request overtime for another employee');
    const emp = await db.query<{ company_id: string; ot_eligible: boolean }>('SELECT company_id, ot_eligible FROM employee WHERE id = $1', [employeeId]);
    if (!emp.rows[0] || emp.rows[0].company_id !== u.companyId) throw new NotFoundError('employee not found');
    if (!emp.rows[0].ot_eligible) throw new BadRequestError('employee grade is not overtime-eligible (Sec 31(2) basis must be documented)');
    if (b.requestedMin > CAP_DAY_MIN) throw new BadRequestError(`overtime may not exceed ${CAP_DAY_MIN} minutes (4 hours) per day (Sec 30(1))`);
    const ws = weekStart(b.localDate);
    const wk = await db.query<{ done: string; pending: string }>(
      `SELECT (SELECT coalesce(sum(ot_min), 0) FROM v_attendance_latest WHERE employee_id = $1 AND local_date >= $2::date AND local_date < $2::date + 7 AND local_date <> $3::date)::text AS done,
              (SELECT coalesce(sum(coalesce(approved_min, requested_min)), 0) FROM ot_request WHERE employee_id = $1 AND status IN ('pending', 'approved') AND local_date >= $2::date AND local_date < $2::date + 7 AND local_date <> $3::date)::text AS pending`,
      [employeeId, ws, b.localDate]);
    const used = Math.max(Number(wk.rows[0]!.done), Number(wk.rows[0]!.pending));
    if (used + b.requestedMin > CAP_WEEK_MIN) throw new BadRequestError(`weekly overtime cap of 24 hours would be exceeded (${used} minutes already this week)`);
    const pol = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'attendance.policy'`, [u.companyId]);
    const pastWindow = typeof pol.rows[0]?.value.otPastDatedDays === 'number' ? (pol.rows[0].value.otPastDatedDays as number) : 7;
    const today = new Date().toISOString().slice(0, 10);
    const age = Math.floor((new Date(`${today}T00:00:00Z`).getTime() - new Date(`${b.localDate}T00:00:00Z`).getTime()) / 86_400_000);
    if (age > pastWindow && !hasRole(u, 'hr')) throw new BadRequestError(`past-dated overtime requests are limited to ${pastWindow} days`);
    const dup = await db.query(`SELECT 1 FROM ot_request WHERE employee_id = $1 AND local_date = $2 AND status IN ('pending', 'approved')`, [employeeId, b.localDate]);
    if (dup.rows[0]) throw new ConflictError('an overtime request already exists for that day');

    const client = await db.connect();
    let id = '';
    try {
      await client.query('BEGIN');
      const ins = await client.query<{ id: string }>('INSERT INTO ot_request (employee_id, local_date, requested_min, reason, created_by) VALUES ($1, $2, $3, $4, $5) RETURNING id',
        [employeeId, b.localDate, b.requestedMin, b.reason ?? null, u.id]);
      id = ins.rows[0]!.id;
      const inst = await startWorkflow(client, { companyId: u.companyId!, objectType: 'ot_request', objectId: id, employeeId, title: 'Overtime pre-approval', body: `${b.requestedMin} minutes on ${b.localDate}${b.reason ? `: ${b.reason}` : ''}` });
      await client.query('UPDATE ot_request SET workflow_instance_id = $2 WHERE id = $1', [id, inst.id]);
      await audit(client, { actorId: u.id, action: 'create', table: 'ot_request', rowId: id, after: { ...b, employeeId } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.status(201).json({ id });
  });

  r.get('/requests', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), status: z.enum(['pending', 'approved', 'rejected', 'cancelled']).optional(), from: isoDate.optional(), to: isoDate.optional(), scope: z.enum(['own', 'team', 'all']).optional() }).parse(req.query);
    const u = req.user!;
    const scope = q.scope ?? (hasRole(u, 'hr', 'finance') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own');
    if (scope === 'all' && !hasRole(u, 'hr', 'finance')) throw new ForbiddenError('scope all requires HR');
    if (q.employeeId) assertAccessEmployee(u, q.employeeId);
    const rows = await db.query(
      `SELECT o.id, o.employee_id, e.emp_code, e.name_en, o.local_date, o.requested_min, o.approved_min, o.reason, o.status, o.workflow_instance_id, o.created_at, o.decided_at
         FROM ot_request o JOIN employee e ON e.id = o.employee_id LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND ($2::uuid IS NULL OR o.employee_id = $2) AND ($3::text IS NULL OR o.status = $3)
          AND ($4::date IS NULL OR o.local_date >= $4) AND ($5::date IS NULL OR o.local_date <= $5)
          AND CASE $6 WHEN 'own' THEN o.employee_id = $7::uuid WHEN 'team' THEN (a.manager_id = $7::uuid OR o.employee_id = $7::uuid) ELSE true END
        ORDER BY o.local_date DESC LIMIT 500`, [u.companyId, q.employeeId ?? null, q.status ?? null, q.from ?? null, q.to ?? null, scope, u.employeeId]);
    res.json({ requests: rows.rows });
  });

  r.post('/requests/:id/cancel', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const r0 = await db.query<{ employee_id: string; status: string; company_id: string }>('SELECT o.employee_id, o.status, e.company_id FROM ot_request o JOIN employee e ON e.id = o.employee_id WHERE o.id = $1', [id]);
    const o = r0.rows[0];
    if (!o || o.company_id !== req.user!.companyId) throw new NotFoundError('request not found');
    assertAccessEmployee(req.user!, o.employee_id);
    if (o.status !== 'pending') throw new ConflictError(`only pending requests can be cancelled (status ${o.status})`);
    await db.query(`UPDATE ot_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
    await cancelWorkflow(db, 'ot_request', id);
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- slips (detected by the attendance engine; only approved hours reach payroll)
  r.get('/slips', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), from: isoDate.optional(), to: isoDate.optional(), status: z.enum(['detected', 'approved', 'rejected', 'posted', 'comp_off']).optional() }).parse(req.query);
    const u = req.user!;
    const employeeId = hasRole(u, 'hr', 'finance', 'manager') ? q.employeeId ?? null : u.employeeId;
    if (!hasRole(u, 'hr', 'finance', 'manager') && !employeeId) throw new ForbiddenError('no employee linked to this user');
    const rows = await db.query(
      `SELECT s.id, s.employee_id, e.emp_code, e.name_en, s.local_date, s.detected_min, s.approved_min, s.multiplier, s.day_type, s.status, s.posted_payslip_id, s.reviewed_by, s.reviewed_at, s.created_at
         FROM ot_slip s JOIN employee e ON e.id = s.employee_id LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND ($2::uuid IS NULL OR s.employee_id = $2) AND ($3::date IS NULL OR s.local_date >= $3) AND ($4::date IS NULL OR s.local_date <= $4)
          AND ($5::text IS NULL OR s.status = $5) AND ($6 OR a.manager_id = $7::uuid OR s.employee_id = $7::uuid)
        ORDER BY s.local_date DESC LIMIT 1000`, [u.companyId, employeeId, q.from ?? null, q.to ?? null, q.status ?? null, hasRole(u, 'hr', 'finance'), u.employeeId]);
    res.json({ slips: rows.rows });
  });

  const slipFor = async (id: string, companyId: string | null) => {
    const r0 = await db.query<{ id: string; employee_id: string; local_date: string; detected_min: number; approved_min: number | null; status: string; company_id: string; day_type: string }>(
      'SELECT s.*, e.company_id FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE s.id = $1', [id]);
    const s = r0.rows[0];
    if (!s || s.company_id !== companyId) throw new NotFoundError('OT slip not found');
    if (s.status === 'posted') throw new ConflictError('slip already posted to payroll');
    return s;
  };

  r.post('/slips/:id/approve', requireRole('hr', 'manager'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ approvedMin: z.number().int().min(0).max(CAP_DAY_MIN).optional() }).parse(req.body ?? {});
    const s = await slipFor(id, req.user!.companyId);
    if (req.user!.employeeId === s.employee_id) throw new ForbiddenError('cannot approve your own overtime');
    const approved = Math.min(b.approvedMin ?? s.detected_min, s.detected_min, CAP_DAY_MIN);
    await db.query(`UPDATE ot_slip SET status = 'approved', approved_min = $2, reviewed_by = $3, reviewed_at = now() WHERE id = $1`, [id, approved, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'approve', table: 'ot_slip', rowId: id, after: { approvedMin: approved } });
    res.json({ ok: true, approvedMin: approved });
  });

  r.post('/slips/:id/reject', requireRole('hr', 'manager'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ reason: z.string().min(3).max(500) }).parse(req.body);
    const s = await slipFor(id, req.user!.companyId);
    await db.query(`UPDATE ot_slip SET status = 'rejected', approved_min = 0, reviewed_by = $2, reviewed_at = now() WHERE id = $1`, [id, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'reject', table: 'ot_slip', rowId: id, after: { reason: b.reason, employeeId: s.employee_id } });
    res.json({ ok: true });
  });

  /** Convert to substitute leave instead of pay (Sec 42: must be used within 21 days). */
  r.post('/slips/:id/comp-off', requireRole('hr', 'manager'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const s = await slipFor(id, req.user!.companyId);
    if (s.status === 'comp_off') throw new ConflictError('already converted');
    const minutes = s.approved_min ?? s.detected_min;
    const days = Math.round((minutes / 480) * 100) / 100;
    const expires = new Date(new Date(`${s.local_date}T00:00:00Z`).getTime() + 21 * 86_400_000).toISOString().slice(0, 10);
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const ledgerId = await creditCompOff(client, s.employee_id, days, expires, { refType: 'ot_slip', refId: id, note: `comp-off for ${minutes} min OT on ${s.local_date}`, postedBy: req.user!.id });
      await client.query(`UPDATE ot_slip SET status = 'comp_off', approved_min = $2, comp_off_ledger_id = $3, reviewed_by = $4, reviewed_at = now() WHERE id = $1`, [id, minutes, ledgerId, req.user!.id]);
      await audit(client, { actorId: req.user!.id, action: 'comp_off', table: 'ot_slip', rowId: id, after: { days, expires } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true, days, expiresOn: expires });
  });

  /** Inspector-ready OT register (plan §5.10). */
  r.get('/register', requireRole('hr', 'finance'), async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate, format: z.enum(['json', 'csv']).default('json') }).parse(req.query);
    const rows = await db.query<Record<string, unknown>>(
      `SELECT e.emp_code, e.name_en, s.local_date, v.bs_date, s.day_type, s.detected_min, s.approved_min, s.multiplier, s.status
         FROM ot_slip s JOIN employee e ON e.id = s.employee_id LEFT JOIN v_attendance_latest v ON v.employee_id = s.employee_id AND v.local_date = s.local_date
        WHERE e.company_id = $1 AND s.local_date BETWEEN $2 AND $3 ORDER BY s.local_date, e.emp_code`, [req.user!.companyId, q.from, q.to]);
    if (q.format === 'csv') {
      const cols = ['emp_code', 'name_en', 'local_date', 'bs_date', 'day_type', 'detected_min', 'approved_min', 'multiplier', 'status'];
      const esc = (v: unknown) => { const s = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
      res.type('text/csv').send([cols.join(','), ...rows.rows.map((r0) => cols.map((c) => esc(r0[c])).join(','))].join('\n') + '\n');
      return;
    }
    res.json({ register: rows.rows });
  });

  return r;
}
