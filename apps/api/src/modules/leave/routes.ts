import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { hasRole, requireRole } from '../auth/middleware.js';
import { dateRange, enqueueRecompute } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { assertManages, cancelWorkflow, nptToday, registerHandler, startWorkflow, type DecisionContext } from '../workflow/service.js';
import {
  assertNoOverlap, balances, computeLeaveDays, getEmployeeLite, getLeaveType, hasLockedDay, ledgerBalance, pendingDays, type LeaveTypeRow,
} from './service.js';

export { creditCompOff, encashableBalances, postEncashment } from './service.js';
export { runMonthlyAccrual, expireSubstituteLeave } from './accrual.js';

/** Approval/rejection effect for leave requests (registered with the workflow engine). */
export async function applyLeaveDecision(db: Queryable, requestId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ id: string; employee_id: string; leave_type_id: string; from_date: string; to_date: string; days_requested: string; status: string; company_id: string; type_name: string }>(
    `SELECT lr.*, lt.company_id, lt.name_en AS type_name FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id WHERE lr.id = $1 FOR UPDATE`, [requestId]);
  const lr = r.rows[0];
  if (!lr) throw new NotFoundError('leave request not found');
  if (lr.status !== 'pending') throw new ConflictError(`leave request already ${lr.status}`);
  if (state === 'approved' && (await hasLockedDay(db, lr.employee_id, lr.from_date, lr.to_date))) {
    throw new ConflictError('the leave falls in a payroll-locked period; reject it and let HR raise an adjustment');
  }
  await db.query('UPDATE leave_request SET status = $2, decided_at = now() WHERE id = $1', [requestId, state]);
  if (state === 'approved') {
    await db.query(
      `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by)
       VALUES ($1, $2, $3, $4, 'usage', 'leave_request', $5, $6)`,
      [lr.employee_id, lr.leave_type_id, lr.from_date, -Number(lr.days_requested), requestId, ctx.actorId]);
    await enqueueRecompute(db, dateRange(lr.from_date, lr.to_date).map((d) => ({ employeeId: lr.employee_id, localDate: d })), 'leave_approved');
  }
  const userId = await userIdForEmployee(db, lr.employee_id);
  await notify(db, {
    companyId: lr.company_id, userId, employeeId: lr.employee_id, template: `leave.${state}`,
    title: state === 'approved' ? 'Leave approved' : 'Leave rejected',
    body: `${lr.type_name} ${lr.from_date}${lr.to_date !== lr.from_date ? ` to ${lr.to_date}` : ''} was ${state}${ctx.reason ? `: ${ctx.reason}` : ''}.`,
    payload: { requestId }, channels: ['push', 'sms'],
  });
}
registerHandler('leave_request', applyLeaveDecision);

const typeBody = z.object({
  code: z.string().min(1).max(20), nameEn: z.string().min(1).max(80), nameNe: z.string().max(80).optional(), paid: z.boolean().default(true),
  accrualBasis: z.enum(['none', 'worked_days', 'monthly', 'annual']).default('none'), accrualDays: z.number().min(0).default(0),
  accrualPerWorkedDays: z.number().int().min(1).nullable().optional(), maxBalance: z.number().min(0).nullable().optional(), carryForward: z.boolean().default(true),
  encashable: z.boolean().default(false), certificateAfterDays: z.number().int().min(0).nullable().optional(), allowHalfDay: z.boolean().default(true),
  maxConsecutiveDays: z.number().int().min(1).nullable().optional(), minServiceDays: z.number().int().min(0).default(0), negativeBalanceLimit: z.number().min(0).default(0),
  genderScope: z.enum(['all', 'female', 'male']).default('all'), sandwichRule: z.boolean().default(false), expiryDays: z.number().int().min(1).nullable().optional(),
});

/** Columns that define a statutory entitlement (Labour Act Ch. 9) — not editable through the API once statutory. */
const STATUTORY_LOCKED = new Set(['accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays']);


/** Keys actually present in the request body: PATCH must not apply schema defaults to untouched fields. */
function provided<T extends Record<string, unknown>>(parsed: T, body: unknown): Partial<T> {
  const keys = body && typeof body === 'object' ? new Set(Object.keys(body as object)) : new Set<string>();
  return Object.fromEntries(Object.entries(parsed).filter(([k]) => keys.has(k))) as Partial<T>;
}

export function leaveRoutes({ db }: AppDeps): Router {
  const r = Router();
  const companyOf = (req: { user?: { companyId: string | null } }): string => {
    if (!req.user?.companyId) throw new ForbiddenError('user is not attached to a company');
    return req.user.companyId;
  };
  const leavePolicy = async (companyId: string): Promise<{ sandwichRule: boolean; probationRestriction: boolean; maxContinuousDays: number }> => {
    const s = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'leave.policy'`, [companyId]);
    const v = s.rows[0]?.value ?? {};
    return { sandwichRule: v.sandwichRule === true, probationRestriction: v.probationRestriction !== false, maxContinuousDays: typeof v.maxContinuousDays === 'number' ? v.maxContinuousDays : 30 };
  };

  // ---------------------------------------------------------------- types
  r.get('/types', async (req, res) => {
    const rows = await db.query('SELECT * FROM leave_type WHERE company_id = $1 ORDER BY sort_order, code', [req.user!.companyId]);
    res.json({ types: rows.rows });
  });
  r.post('/types', requireRole('hr'), async (req, res) => {
    const b = typeBody.parse(req.body);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO leave_type (company_id, code, name_en, name_ne, paid, statutory, accrual_basis, accrual_days, accrual_per_worked_days, max_balance, carry_forward, encashable,
         certificate_after_days, allow_half_day, max_consecutive_days, min_service_days, negative_balance_limit, gender_scope, sandwich_rule, expiry_days, sort_order)
       VALUES ($1, $2, $3, $4, $5, false, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, 100) RETURNING id`,
      [companyOf(req), b.code, b.nameEn, b.nameNe ?? null, b.paid, b.accrualBasis, b.accrualDays, b.accrualPerWorkedDays ?? null, b.maxBalance ?? null, b.carryForward, b.encashable,
        b.certificateAfterDays ?? null, b.allowHalfDay, b.maxConsecutiveDays ?? null, b.minServiceDays, b.negativeBalanceLimit, b.genderScope, b.sandwichRule, b.expiryDays ?? null]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'leave_type', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });
  r.patch('/types/:id', requireRole('hr'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = provided(typeBody.partial().parse(req.body), req.body);
    const cur = await db.query<LeaveTypeRow>('SELECT * FROM leave_type WHERE id = $1 AND company_id = $2', [id, req.user!.companyId]);
    if (!cur.rows[0]) throw new NotFoundError('leave type not found');
    if (cur.rows[0].statutory) {
      const locked = Object.keys(b).filter((k) => STATUTORY_LOCKED.has(k) && b[k as keyof typeof b] !== undefined);
      if (locked.length) throw new BadRequestError(`statutory leave entitlement fields are locked: ${locked.join(', ')} (Labour Act 2074 Ch. 9)`);
    }
    const map: Array<[keyof typeof b, string]> = [
      ['code', 'code'], ['nameEn', 'name_en'], ['nameNe', 'name_ne'], ['paid', 'paid'], ['accrualBasis', 'accrual_basis'], ['accrualDays', 'accrual_days'],
      ['accrualPerWorkedDays', 'accrual_per_worked_days'], ['maxBalance', 'max_balance'], ['carryForward', 'carry_forward'], ['encashable', 'encashable'],
      ['certificateAfterDays', 'certificate_after_days'], ['allowHalfDay', 'allow_half_day'], ['maxConsecutiveDays', 'max_consecutive_days'], ['minServiceDays', 'min_service_days'],
      ['negativeBalanceLimit', 'negative_balance_limit'], ['genderScope', 'gender_scope'], ['sandwichRule', 'sandwich_rule'], ['expiryDays', 'expiry_days'],
    ];
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, col] of map) if (b[k] !== undefined) { vals.push(b[k]); sets.push(`${col} = $${vals.length}`); }
    if (sets.length) await db.query(`UPDATE leave_type SET ${sets.join(', ')} WHERE id = $1`, vals);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'leave_type', rowId: id, before: cur.rows[0], after: b });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- balances
  r.get('/balances', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), asOf: isoDate.optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    if (!emp || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee not found');
    await assertManages(db, req.user!, employeeId);
    res.json({ employeeId, balances: await balances(db, emp, q.asOf ?? nptToday()) });
  });

  /** Preview the deduction before submitting (plan §13.5 computed line). */
  r.get('/preview', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), leaveTypeId: z.uuid(), from: isoDate, to: isoDate, halfDay: z.enum(['first_half', 'second_half']).optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    const type = await getLeaveType(db, q.leaveTypeId);
    if (!emp || !type || emp.company_id !== type.company_id || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee or leave type not found');
    await assertManages(db, req.user!, employeeId);
    const policy = await leavePolicy(emp.company_id);
    const calc = await computeLeaveDays(db, emp, type, q.from, q.to, q.halfDay ?? null, policy.sandwichRule);
    const bal = await ledgerBalance(db, employeeId, type.id);
    const pend = await pendingDays(db, employeeId, type.id);
    res.json({ ...calc, balance: bal, available: bal - pend, balanceAfter: bal - pend - calc.days });
  });

  // ---------------------------------------------------------------- requests
  r.post('/requests', async (req, res) => {
    const b = z.object({
      employeeId: z.uuid().optional(), leaveTypeId: z.uuid(), from: isoDate, to: isoDate, halfDay: z.enum(['first_half', 'second_half']).optional(),
      reason: z.string().max(1000).optional(), attachmentKey: z.string().max(200).optional(),
    }).parse(req.body);
    if (b.to < b.from) throw new BadRequestError('to must be on or after from');
    const employeeId = b.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    const type = await getLeaveType(db, b.leaveTypeId);
    if (!emp || !type || emp.company_id !== type.company_id || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee or leave type not found');
    await assertManages(db, req.user!, employeeId);
    if (emp.status !== 'active') throw new BadRequestError('employee is not active');
    if (emp.join_date && b.from < emp.join_date) throw new BadRequestError(`leave cannot start before the joining date ${emp.join_date}`);
    if (emp.exit_date && b.to > emp.exit_date) throw new BadRequestError(`leave cannot end after the exit date ${emp.exit_date}`);
    if (await hasLockedDay(db, employeeId, b.from, b.to)) throw new ConflictError('part of this range is locked by payroll; ask HR for an adjustment');
    if (type.gender_scope !== 'all' && type.gender_scope !== emp.gender) throw new BadRequestError(`${type.name_en} is not available for this employee`);
    const policy = await leavePolicy(emp.company_id);
    if (emp.join_date) {
      const serviceDays = Math.floor((new Date(`${b.from}T00:00:00Z`).getTime() - new Date(`${emp.join_date}T00:00:00Z`).getTime()) / 86_400_000);
      if (serviceDays < type.min_service_days) throw new BadRequestError(`${type.name_en} needs ${type.min_service_days} days of service`);
    }
    if (policy.probationRestriction && emp.probation_end && b.from <= emp.probation_end && !type.statutory && type.paid) {
      throw new BadRequestError(`${type.name_en} cannot be taken during probation (ends ${emp.probation_end})`);
    }
    const calc = await computeLeaveDays(db, emp, type, b.from, b.to, b.halfDay ?? null, policy.sandwichRule);
    const maxRun = type.max_consecutive_days ?? policy.maxContinuousDays;
    if (calc.days > maxRun) throw new BadRequestError(`${type.name_en} cannot exceed ${maxRun} consecutive days`);
    if (type.certificate_after_days !== null && calc.days > type.certificate_after_days && !b.attachmentKey) {
      throw new BadRequestError(`a medical certificate is required for more than ${type.certificate_after_days} consecutive days of ${type.name_en} (Sec 44(2))`);
    }
    if (type.paid) {
      const available = (await ledgerBalance(db, employeeId, type.id)) - (await pendingDays(db, employeeId, type.id));
      if (calc.days > available + Number(type.negative_balance_limit)) {
        const paidDays = Math.max(0, Math.floor(available * 2) / 2);
        return res.status(400).json({
          error: `You have ${available.toFixed(1)} days of ${type.name_en} left but this request needs ${calc.days}. Change the dates, or apply ${(calc.days - paidDays).toFixed(1)} day(s) as unpaid leave.`,
          suggestion: { paidDays, lwpDays: calc.days - paidDays },
        });
      }
    }
    const overlap = await db.query<{ name_en: string }>(
      `SELECT e.name_en FROM leave_request o JOIN employee e ON e.id = o.employee_id WHERE o.status IN ('pending', 'approved') AND o.employee_id <> $1
         AND o.from_date <= $3::date AND o.to_date >= $2::date AND e.company_id = $4 LIMIT 10`, [employeeId, b.from, b.to, emp.company_id]);

    const client = await db.connect();
    let requestId = '';
    try {
      await client.query('BEGIN');
      // Serialise per employee so two concurrent requests for the same dates cannot both pass the overlap check.
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`leave:${employeeId}`]);
      await assertNoOverlap(client, employeeId, b.from, b.to);
      const ins = await client.query<{ id: string }>(
        `INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, half_day, days_requested, reason, attachment_key, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id`,
        [employeeId, type.id, b.from, b.to, b.halfDay ?? null, calc.days, b.reason ?? null, b.attachmentKey ?? null, req.user!.id]);
      requestId = ins.rows[0]!.id;
      const inst = await startWorkflow(client, {
        companyId: emp.company_id, objectType: 'leave_request', objectId: requestId, employeeId,
        title: 'Leave request to approve', body: `${type.name_en} ${b.from}${b.to !== b.from ? ` to ${b.to}` : ''} (${calc.days} day(s))${b.reason ? `: ${b.reason}` : ''}`,
      });
      await client.query('UPDATE leave_request SET workflow_instance_id = $2 WHERE id = $1', [requestId, inst.id]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'leave_request', rowId: requestId, after: { ...b, days: calc.days } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    return res.status(201).json({ id: requestId, days: calc.days, explanation: calc.explanation, sandwichApplied: calc.sandwichApplied, teamOverlap: overlap.rows.map((x) => x.name_en) });
  });

  r.get('/requests', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), status: z.enum(['pending', 'approved', 'rejected', 'cancelled']).optional(), from: isoDate.optional(), to: isoDate.optional(), scope: z.enum(['own', 'team', 'all']).optional() }).parse(req.query);
    const u = req.user!;
    const scope = q.scope ?? (hasRole(u, 'hr', 'finance') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own');
    if (scope === 'all' && !hasRole(u, 'hr', 'finance')) throw new ForbiddenError('scope all requires HR');
    if (q.employeeId) await assertManages(db, u, q.employeeId);
    const rows = await db.query(
      `SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, e.name_ne, lr.leave_type_id, lt.code AS type_code, lt.name_en AS type_name, lt.paid,
              lr.from_date, lr.to_date, lr.half_day, lr.days_requested, lr.reason, lr.attachment_key, lr.status, lr.workflow_instance_id, lr.created_at, lr.decided_at
         FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1
          AND ($2::uuid IS NULL OR lr.employee_id = $2)
          AND ($3::text IS NULL OR lr.status = $3)
          AND ($4::date IS NULL OR lr.to_date >= $4) AND ($5::date IS NULL OR lr.from_date <= $5)
          AND CASE $6 WHEN 'own' THEN lr.employee_id = $7::uuid WHEN 'team' THEN (a.manager_id = $7::uuid OR lr.employee_id = $7::uuid) ELSE true END
        ORDER BY lr.created_at DESC LIMIT 500`,
      [u.companyId, q.employeeId ?? null, q.status ?? null, q.from ?? null, q.to ?? null, scope, u.employeeId]);
    res.json({ requests: rows.rows });
  });

  r.post('/requests/:id/cancel', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ reason: z.string().max(500).optional() }).parse(req.body ?? {});
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const r0 = await client.query<{ employee_id: string; leave_type_id: string; from_date: string; to_date: string; days_requested: string; status: string; company_id: string }>(
        `SELECT lr.employee_id, lr.leave_type_id, lr.from_date, lr.to_date, lr.days_requested, lr.status, e.company_id FROM leave_request lr JOIN employee e ON e.id = lr.employee_id WHERE lr.id = $1 FOR UPDATE`, [id]);
      const lr = r0.rows[0];
      if (!lr || lr.company_id !== req.user!.companyId) throw new NotFoundError('leave request not found');
      await assertManages(client, req.user!, lr.employee_id);
      if (lr.status === 'cancelled' || lr.status === 'rejected') throw new ConflictError(`request is already ${lr.status}`);
      if (lr.status === 'approved') {
        const locked = await client.query('SELECT 1 FROM v_attendance_latest WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3 AND locked_at IS NOT NULL LIMIT 1', [lr.employee_id, lr.from_date, lr.to_date]);
        if (locked.rows[0]) throw new ConflictError('leave falls in a payroll-locked period; ask HR for an adjustment');
        await client.query(
          `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note) VALUES ($1, $2, $3, $4, 'cancellation', 'leave_request', $5, $6, $7)`,
          [lr.employee_id, lr.leave_type_id, lr.from_date, Number(lr.days_requested), id, req.user!.id, b.reason ?? null]);
        await enqueueRecompute(client, dateRange(lr.from_date, lr.to_date).map((d) => ({ employeeId: lr.employee_id, localDate: d })), 'leave_cancelled');
      }
      await client.query(`UPDATE leave_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
      await cancelWorkflow(client, 'leave_request', id);
      await audit(client, { actorId: req.user!.id, action: 'cancel', table: 'leave_request', rowId: id, before: lr, after: b });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true });
  });

  /** HR ledger adjustments and allocations (audited). */
  r.post('/adjust', requireRole('hr'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), leaveTypeId: z.uuid(), deltaDays: z.number().min(-365).max(365).refine((n) => n !== 0, 'deltaDays cannot be 0'), reason: z.string().min(3).max(500), effectiveDate: isoDate.optional(), kind: z.enum(['adjustment', 'allocation', 'carry_forward']).default('adjustment') }).parse(req.body);
    const emp = await getEmployeeLite(db, b.employeeId);
    const type = await getLeaveType(db, b.leaveTypeId);
    if (!emp || !type || emp.company_id !== req.user!.companyId || type.company_id !== emp.company_id) throw new NotFoundError('employee or leave type not found');
    const ins = await db.query<{ id: string }>(
      `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note, posted_by) VALUES ($1, $2, $3, $4, $5, 'hr_adjustment', $6, $7) RETURNING id`,
      [b.employeeId, b.leaveTypeId, b.effectiveDate ?? nptToday(), b.deltaDays, b.kind, b.reason, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'adjust', table: 'leave_ledger', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id, balance: await ledgerBalance(db, b.employeeId, b.leaveTypeId) });
  });

  r.get('/ledger', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), leaveTypeId: z.uuid().optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const own = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, req.user!.companyId]);
    if (!own.rows[0]) throw new NotFoundError('employee not found');
    await assertManages(db, req.user!, employeeId);
    const rows = await db.query(
      `SELECT l.id, l.leave_type_id, t.code, l.posted_at, l.effective_date, l.delta_days, l.reason, l.ref_type, l.ref_id, l.expires_on, l.note
         FROM leave_ledger l JOIN leave_type t ON t.id = l.leave_type_id WHERE l.employee_id = $1 AND ($2::uuid IS NULL OR l.leave_type_id = $2) ORDER BY l.effective_date DESC, l.posted_at DESC LIMIT 500`,
      [employeeId, q.leaveTypeId ?? null]);
    res.json({ ledger: rows.rows });
  });

  /** Who is off when (plan §5.6 team calendar). Employees see their own team; HR sees everyone. */
  r.get('/team-calendar', async (req, res) => {
    const q = z.object({ from: isoDate, to: isoDate }).parse(req.query);
    const u = req.user!;
    const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; from_date: string; to_date: string; half_day: string | null; type_code: string; status: string }>(
      `SELECT lr.employee_id, e.emp_code, e.name_en, lr.from_date, lr.to_date, lr.half_day, lt.code AS type_code, lr.status
         FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND lr.status IN ('pending', 'approved') AND lr.from_date <= $3::date AND lr.to_date >= $2::date
          AND ($4 OR a.manager_id = $5::uuid OR lr.employee_id = $5::uuid
               OR a.manager_id = (SELECT manager_id FROM employee_assignment m WHERE m.employee_id = $5::uuid AND m.valid @> current_date))
        ORDER BY lr.from_date`, [u.companyId, q.from, q.to, hasRole(u, 'hr', 'finance'), u.employeeId]);
    const days = new Map<string, Array<Record<string, unknown>>>();
    for (const lr of rows.rows) {
      for (const d of dateRange(lr.from_date < q.from ? q.from : lr.from_date, lr.to_date > q.to ? q.to : lr.to_date)) {
        days.set(d, [...(days.get(d) ?? []), { employeeId: lr.employee_id, empCode: lr.emp_code, nameEn: lr.name_en, type: lr.type_code, status: lr.status, halfDay: lr.half_day }]);
      }
    }
    res.json({ days: [...days.entries()].sort().map(([date, off]) => ({ date, off })) });
  });

  return r;
}
