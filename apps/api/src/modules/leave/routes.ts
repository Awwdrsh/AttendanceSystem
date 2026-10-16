import { Router } from 'express';
import { bsRange } from '../../bsdate.js';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { hhmm, isoDate } from '../../validation.js';
import { adToBs, fiscalYear, fiscalYearOf } from '@hajiri/bs-calendar';
import { hasModule, hasRole, requireModule, requireRole } from '../auth/middleware.js';
import { dateRange, enqueueRecompute } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { assertManages, cancelWorkflow, nptToday, registerHandler, startWorkflow, type DecisionContext } from '../workflow/service.js';
import {
  assertNoOverlap, balances, bridgeAcross, computeHourlyLeave, computeLeaveDays, eventDaysUsed, getEmployeeLite, getLeaveType, hasLockedDay, leaveTypeByCode, ledgerBalance, pendingDays, type LeaveTypeRow,
} from './service.js';
import { allocateAnnualLeave, runLeaveCatchUp } from './accrual.js';

export { creditCompOff, encashableBalances, postEncashment } from './service.js';
export { runMonthlyAccrual, expireSubstituteLeave, allocateAnnualLeave, runLeaveCatchUp } from './accrual.js';

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
    await postBridge(db, requestId, lr.employee_id, ctx.actorId);
    await enqueueRecompute(db, dateRange(lr.from_date, lr.to_date).map((d) => ({ employeeId: lr.employee_id, localDate: d })), 'leave_approved');
  }
  const userId = await userIdForEmployee(db, lr.employee_id);
  await notify(db, {
    companyId: lr.company_id, userId, employeeId: lr.employee_id, template: `leave.${state}`,
    title: state === 'approved' ? 'Leave approved' : 'Leave rejected',
    body: `${lr.type_name} ${bsRange(String(lr.from_date).slice(0, 10), String(lr.to_date).slice(0, 10))} was ${state}${ctx.reason ? `: ${ctx.reason}` : ''}.`,
    payload: { requestId }, channels: ['push', 'sms'],
  });
}
registerHandler('leave_request', applyLeaveDecision);

/**
 * On approval, charge the non-working days this request bridges (sandwich across requests) to the type that pays for
 * them, if the leave on the other side is still pending or approved; otherwise the bridge is dropped.
 */
async function postBridge(db: Queryable, requestId: string, employeeId: string, actorId: string): Promise<void> {
  const b = await db.query<{ bridge_days: string; bridge_type_id: string | null; bridge_from: string | null; bridge_to: string | null }>(
    'SELECT bridge_days, bridge_type_id, bridge_from::text, bridge_to::text FROM leave_request WHERE id = $1', [requestId]);
  const x = b.rows[0];
  if (!x || !x.bridge_type_id || Number(x.bridge_days) <= 0 || !x.bridge_from || !x.bridge_to) return;
  const other = await db.query(
    `SELECT 1 FROM leave_request WHERE employee_id = $1 AND id <> $2 AND status IN ('pending', 'approved')
        AND (to_date = $3::date - 1 OR from_date = $4::date + 1) LIMIT 1`, [employeeId, requestId, x.bridge_from, x.bridge_to]);
  if (!other.rows.length) {
    await db.query('UPDATE leave_request SET bridge_days = 0, bridge_type_id = NULL, bridge_from = NULL, bridge_to = NULL WHERE id = $1', [requestId]);
    return;
  }
  await db.query(
    `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note)
     VALUES ($1, $2, $3, $4, 'usage', 'leave_bridge', $5, $6, $7)`,
    [employeeId, x.bridge_type_id, x.bridge_from, -Number(x.bridge_days), requestId, actorId, 'Weekly-off/holiday days between two leaves (sandwich rule)']);
}

const typeBody = z.object({
  code: z.string().min(1).max(20), nameEn: z.string().min(1).max(80), nameNe: z.string().max(80).optional(), paid: z.boolean().default(true),
  accrualBasis: z.enum(['none', 'worked_days', 'monthly', 'annual']).default('none'), accrualDays: z.number().min(0).default(0),
  accrualPerWorkedDays: z.number().int().min(1).nullable().optional(), maxBalance: z.number().min(0).nullable().optional(), carryForward: z.boolean().default(true),
  encashable: z.boolean().default(false), certificateAfterDays: z.number().int().min(0).nullable().optional(), allowHalfDay: z.boolean().default(true),
  maxConsecutiveDays: z.number().int().min(1).nullable().optional(), minServiceDays: z.number().int().min(0).default(0), negativeBalanceLimit: z.number().min(0).default(0),
  genderScope: z.enum(['all', 'female', 'male']).default('all'), sandwichRule: z.boolean().default(false), expiryDays: z.number().int().min(1).nullable().optional(),
  eventDays: z.number().min(0.5).max(365).nullable().optional(), allowHourly: z.boolean().default(false),
  /** Follow another type's place in the leave order and its sandwich rule. */
  clubWithId: z.uuid().nullable().optional(), sortOrder: z.number().int().min(0).max(9999).optional(),
});

/** Columns that define a statutory entitlement (Labour Act Ch. 9) — not editable through the API once statutory. */
const STATUTORY_LOCKED = new Set(['accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays', 'eventDays']);


/** An hourly request names both times, a single date and no half day; returns null for day-based requests. */
function hourlyWindow(b: { from: string; to: string; halfDay?: string | undefined; fromTime?: string | undefined; toTime?: string | undefined }): { fromTime: string; toTime: string } | null {
  if (b.fromTime === undefined && b.toTime === undefined) return null;
  if (!b.fromTime || !b.toTime) throw new BadRequestError('give both a start and an end time for hourly leave');
  if (b.from !== b.to) throw new BadRequestError('hourly leave must be on a single date');
  if (b.halfDay) throw new BadRequestError('choose either a half day or hours, not both');
  return { fromTime: b.fromTime, toTime: b.toTime };
}

const hoursOf = (calc: object): number => ('hours' in calc ? Number(calc.hours) : 0);

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
  r.post('/types', requireModule('attendance'), async (req, res) => {
    const b = typeBody.parse(req.body);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO leave_type (company_id, code, name_en, name_ne, paid, statutory, accrual_basis, accrual_days, accrual_per_worked_days, max_balance, carry_forward, encashable,
         certificate_after_days, allow_half_day, max_consecutive_days, min_service_days, negative_balance_limit, gender_scope, sandwich_rule, expiry_days, event_days, sort_order, allow_hourly)
       VALUES ($1, $2, $3, $4, $5, false, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, 100, $21) RETURNING id`,
      [companyOf(req), b.code, b.nameEn, b.nameNe ?? null, b.paid, b.accrualBasis, b.accrualDays, b.accrualPerWorkedDays ?? null, b.maxBalance ?? null, b.carryForward, b.encashable,
        b.certificateAfterDays ?? null, b.allowHalfDay, b.maxConsecutiveDays ?? null, b.minServiceDays, b.negativeBalanceLimit, b.genderScope, b.sandwichRule, b.expiryDays ?? null, b.eventDays ?? null,
        b.allowHourly && b.paid && (b.eventDays ?? null) === null]);
    if (b.clubWithId) {
      await assertClubTarget(db, companyOf(req), ins.rows[0]!.id, b.clubWithId);
      await db.query('UPDATE leave_type SET club_with_id = $2 WHERE id = $1', [ins.rows[0]!.id, b.clubWithId]);
    }
    if (b.sortOrder !== undefined) await db.query('UPDATE leave_type SET sort_order = $2 WHERE id = $1', [ins.rows[0]!.id, b.sortOrder]);
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'leave_type', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });
  r.patch('/types/:id', requireModule('attendance'), async (req, res) => {
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
      ['negativeBalanceLimit', 'negative_balance_limit'], ['genderScope', 'gender_scope'], ['sandwichRule', 'sandwich_rule'], ['expiryDays', 'expiry_days'], ['eventDays', 'event_days'],
      ['allowHourly', 'allow_hourly'], ['clubWithId', 'club_with_id'], ['sortOrder', 'sort_order'],
    ];
    if (b.clubWithId) await assertClubTarget(db, cur.rows[0].company_id, id, b.clubWithId);
    if (b.allowHourly && (!(b.paid ?? cur.rows[0].paid) || (b.eventDays !== undefined ? b.eventDays : cur.rows[0].event_days) !== null)) {
      throw new BadRequestError('only paid leave with a balance can be taken by the hour');
    }
    const sets: string[] = []; const vals: unknown[] = [id];
    for (const [k, col] of map) if (b[k] !== undefined) { vals.push(b[k]); sets.push(`${col} = $${vals.length}`); }
    if (sets.length) await db.query(`UPDATE leave_type SET ${sets.join(', ')} WHERE id = $1`, vals);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'leave_type', rowId: id, before: cur.rows[0], after: b });
    res.json({ ok: true });
  });

  /** Set the leave order in one go: the listed types get sort orders 10, 20, 30... */
  r.post('/types/order', requireRole('hr'), async (req, res) => {
    const b = z.object({ ids: z.array(z.uuid()).min(1).max(100) }).parse(req.body);
    const own = await db.query<{ id: string }>('SELECT id FROM leave_type WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyOf(req), b.ids]);
    if (own.rows.length !== new Set(b.ids).size) throw new BadRequestError('a leave type in the list does not exist');
    for (const [i, id] of b.ids.entries()) await db.query('UPDATE leave_type SET sort_order = $2 WHERE id = $1', [id, (i + 1) * 10]);
    await audit(db, { actorId: req.user!.id, action: 'reorder', table: 'leave_type', after: { ids: b.ids } });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- balances
  r.get('/balances', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), asOf: isoDate.optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    if (!emp || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee not found');
    await assertManages(db, req.user!, employeeId, 'attendance');
    await allocateAnnualLeave(db, emp.company_id, nptToday(), employeeId);
    res.json({ employeeId, balances: await balances(db, emp, q.asOf ?? nptToday()) });
  });

  /** Preview the deduction before submitting (plan §13.5 computed line). */
  r.get('/preview', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), leaveTypeId: z.uuid(), from: isoDate, to: isoDate, halfDay: z.enum(['first_half', 'second_half']).optional(),
      fromTime: hhmm.optional(), toTime: hhmm.optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    const type = await getLeaveType(db, q.leaveTypeId);
    if (!emp || !type || emp.company_id !== type.company_id || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee or leave type not found');
    await assertManages(db, req.user!, employeeId, 'attendance');
    const policy = await leavePolicy(emp.company_id);
    const hourly = hourlyWindow(q);
    const calc = hourly ? await computeHourlyLeave(db, emp, type, q.from, hourly.fromTime, hourly.toTime)
      : await computeLeaveDays(db, emp, type, q.from, q.to, q.halfDay ?? null, policy.sandwichRule);
    const bridge = !hourly && !q.halfDay ? await bridgeAcross(db, emp, type, q.from, q.to, policy.sandwichRule) : null;
    if (type.event_days !== null) {
      const left = Number(type.event_days) - (await eventDaysUsed(db, employeeId, type.id, q.from, q.to));
      return res.json({ ...calc, balance: Number(type.event_days), available: left, balanceAfter: left - calc.days });
    }
    await allocateAnnualLeave(db, emp.company_id, nptToday(), employeeId);
    const bal = await ledgerBalance(db, employeeId, type.id);
    const pend = await pendingDays(db, employeeId, type.id);
    const ownBridge = bridge && bridge.typeId === type.id ? bridge.days : 0;
    return res.json({ ...calc, bridge, balance: bal, available: bal - pend, balanceAfter: bal - pend - calc.days - ownBridge });
  });

  // ---------------------------------------------------------------- requests
  r.post('/requests', async (req, res) => {
    const b = z.object({
      employeeId: z.uuid().optional(), leaveTypeId: z.uuid(), from: isoDate, to: isoDate, halfDay: z.enum(['first_half', 'second_half']).optional(),
      /** Hourly (short) leave: a window on a single day, e.g. 10:00–12:00. */
      fromTime: hhmm.optional(), toTime: hhmm.optional(),
      reason: z.string().max(1000).optional(), attachmentKey: z.string().max(200).optional(),
      /** HR only: record full-day leave on a past day that already shows as worked (e.g. a mis-attributed punch). */
      override: z.boolean().optional(),
    }).parse(req.body);
    if (b.override && !hasModule(req.user!, 'attendance')) throw new ForbiddenError('only HR can override the worked-day check');
    if (b.to < b.from) throw new BadRequestError('to must be on or after from');
    const employeeId = b.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const emp = await getEmployeeLite(db, employeeId);
    const type = await getLeaveType(db, b.leaveTypeId);
    if (!emp || !type || emp.company_id !== type.company_id || emp.company_id !== req.user!.companyId) throw new NotFoundError('employee or leave type not found');
    await assertManages(db, req.user!, employeeId, 'attendance');
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
    const hourly = hourlyWindow(b);
    const calc = hourly ? await computeHourlyLeave(db, emp, type, b.from, hourly.fromTime, hourly.toTime)
      : await computeLeaveDays(db, emp, type, b.from, b.to, b.halfDay ?? null, policy.sandwichRule);
    const bridge = !hourly && !b.halfDay ? await bridgeAcross(db, emp, type, b.from, b.to, policy.sandwichRule) : null;
    const ownBridge = bridge && bridge.typeId === type.id ? bridge.days : 0;
    if (bridge && bridge.typeId !== type.id) {
      const avail = (await ledgerBalance(db, employeeId, bridge.typeId)) - (await pendingDays(db, employeeId, bridge.typeId));
      if (bridge.days > avail) throw new BadRequestError(`${bridge.explanation}, but only ${Math.max(0, avail).toFixed(1)} day(s) of ${bridge.typeName} are left. Change the dates or ask HR.`);
    }
    // The company's maxContinuousDays policy limits discretionary leave only; a statutory entitlement cannot be capped by policy.
    const maxRun = type.max_consecutive_days ?? (type.statutory ? null : policy.maxContinuousDays);
    if (maxRun !== null && calc.days > maxRun) throw new BadRequestError(`${type.name_en} cannot exceed ${maxRun} consecutive days`);
    if (type.event_days !== null) {
      const perEvent = Number(type.event_days);
      const used = await eventDaysUsed(db, employeeId, type.id, b.from, b.to);
      if (used + calc.days > perEvent) {
        const left = Math.max(0, perEvent - used);
        const unpaid = await leaveTypeByCode(db, emp.company_id, 'SPECIAL_UNPAID');
        const rest = type.code === 'MATERNITY' && unpaid ? ` Take the remaining days (up to 98 in total) as ${unpaid.name_en}.` : '';
        return res.status(400).json({
          error: `${type.name_en} allows ${perEvent} day(s) per event${used ? ` and ${used} are already requested for this event` : ''}; this request needs ${calc.days}.${rest}`,
          suggestion: { paidDays: left, remainingDays: calc.days - left, ...(rest ? { remainderLeaveTypeId: unpaid!.id } : {}) },
        });
      }
    }
    // Full-day leave cannot replace a past day the employee is already recorded as having worked; that would turn a worked day
    // into leave (and unpaid leave into a pay cut). Half days are fine; HR can override for a mis-attributed punch.
    if (!b.halfDay && !hourly && !b.override) {
      const worked = await db.query<{ local_date: string }>(
        `SELECT local_date::text FROM v_attendance_latest WHERE employee_id = $1 AND local_date = ANY($2::date[]) AND local_date < $3::date AND status IN ('Present', 'WFH') ORDER BY local_date`,
        [employeeId, calc.workingDays, nptToday()]);
      if (worked.rows.length) {
        throw new ConflictError(`${employeeId === req.user!.employeeId ? 'You were' : 'The employee was'} recorded as working on ${bsRange(worked.rows[0]!.local_date, worked.rows[worked.rows.length - 1]!.local_date)} (${worked.rows.length} day(s)). Apply for a half day, or ask HR to correct the attendance first.`);
      }
    }
    if (type.certificate_after_days !== null && calc.days > type.certificate_after_days && !b.attachmentKey) {
      throw new BadRequestError(`a medical certificate is required for more than ${type.certificate_after_days} consecutive days of ${type.name_en} (Sec 44(2))`);
    }
    if (type.paid && type.event_days === null) {
      await allocateAnnualLeave(db, emp.company_id, nptToday(), employeeId);
      const available = (await ledgerBalance(db, employeeId, type.id)) - (await pendingDays(db, employeeId, type.id));
      if (calc.days + ownBridge > available + Number(type.negative_balance_limit)) {
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
        `INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, half_day, days_requested, reason, attachment_key, created_by, from_time, to_time, hours,
           bridge_days, bridge_type_id, bridge_from, bridge_to)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id`,
        [employeeId, type.id, b.from, b.to, b.halfDay ?? null, calc.days, b.reason ?? null, b.attachmentKey ?? null, req.user!.id,
          hourly?.fromTime ?? null, hourly?.toTime ?? null, hourly ? hoursOf(calc) : null,
          bridge?.days ?? 0, bridge?.typeId ?? null, bridge?.from ?? null, bridge?.to ?? null]);
      requestId = ins.rows[0]!.id;
      const when = hourly ? `${bsRange(b.from, b.to)} ${hourly.fromTime}–${hourly.toTime}` : bsRange(b.from, b.to);
      const inst = await startWorkflow(client, {
        companyId: emp.company_id, objectType: 'leave_request', objectId: requestId, employeeId,
        title: 'Leave request to approve', body: `${type.name_en} ${when} (${hourly ? `${hoursOf(calc)} hour(s), ` : ''}${calc.days} day(s))${b.reason ? `: ${b.reason}` : ''}`,
      });
      await client.query('UPDATE leave_request SET workflow_instance_id = $2 WHERE id = $1', [requestId, inst.id]);
      await audit(client, { actorId: req.user!.id, action: 'create', table: 'leave_request', rowId: requestId, after: { ...b, days: calc.days } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    return res.status(201).json({ id: requestId, days: calc.days, explanation: calc.explanation, sandwichApplied: calc.sandwichApplied, bridge, teamOverlap: overlap.rows.map((x) => x.name_en) });
  });

  r.get('/requests', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), status: z.enum(['pending', 'approved', 'rejected', 'cancelled']).optional(), from: isoDate.optional(), to: isoDate.optional(), scope: z.enum(['own', 'team', 'all']).optional() }).parse(req.query);
    const u = req.user!;
    const scope = q.scope ?? (hasModule(u, 'attendance') ? 'all' : hasRole(u, 'manager') ? 'team' : 'own');
    if (scope === 'all' && !hasModule(u, 'attendance')) throw new ForbiddenError('scope all requires HR');
    if (q.employeeId) await assertManages(db, u, q.employeeId, 'attendance');
    const rows = await db.query(
      `SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, e.name_ne, lr.leave_type_id, lt.code AS type_code, lt.name_en AS type_name, lt.paid,
              lr.from_date, lr.to_date, lr.half_day, lr.days_requested, lr.reason, lr.attachment_key, lr.status, lr.workflow_instance_id, lr.created_at, lr.decided_at,
              to_char(lr.from_time, 'HH24:MI') AS from_time, to_char(lr.to_time, 'HH24:MI') AS to_time, lr.hours, lr.bridge_days, lr.bridge_type_id
         FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1
          AND ($2::uuid IS NULL OR lr.employee_id = $2)
          AND ($3::text IS NULL OR lr.status = $3)
          AND ($4::date IS NULL OR lr.to_date >= $4) AND ($5::date IS NULL OR lr.from_date <= $5)
          AND CASE $6 WHEN 'own' THEN lr.employee_id = $7::uuid WHEN 'team' THEN (hj_manages($7::uuid, lr.employee_id, current_date) OR lr.employee_id = $7::uuid) ELSE true END
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
        // Give back the bridged weekly-off/holiday days, if any were charged.
        await client.query(
          `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, posted_by, note)
           SELECT employee_id, leave_type_id, effective_date, -delta_days, 'cancellation', 'leave_bridge', ref_id, $2, $3 FROM leave_ledger
            WHERE ref_type = 'leave_bridge' AND ref_id = $1 AND reason = 'usage'`, [id, req.user!.id, b.reason ?? null]);
        await enqueueRecompute(client, dateRange(lr.from_date, lr.to_date).map((d) => ({ employeeId: lr.employee_id, localDate: d })), 'leave_cancelled');
      }
      await client.query(`UPDATE leave_request SET status = 'cancelled', decided_at = now() WHERE id = $1`, [id]);
      await cancelWorkflow(client, 'leave_request', id);
      await audit(client, { actorId: req.user!.id, action: 'cancel', table: 'leave_request', rowId: id, before: lr, after: b });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true });
  });

  /** Run leave accrual catch-up now: missed months of the current fiscal year and missing annual allocations (idempotent). */
  r.post('/accrual/run', requireModule('attendance'), async (req, res) => {
    const summary = await runLeaveCatchUp(db, companyOf(req), nptToday());
    const postings = summary.months.reduce((a, m) => a + m.postings, 0) + summary.annualPostings;
    await audit(db, { actorId: req.user!.id, action: 'accrual_catch_up', table: 'leave_ledger', rowId: companyOf(req), after: { postings, months: summary.months.length } });
    res.json({ postings, months: summary.months.map((m) => ({ bsYear: m.bsYear, bsMonth: m.bsMonth, postings: m.postings })), annualPostings: summary.annualPostings });
  });

  /** HR ledger adjustments and allocations (audited). */
  r.post('/adjust', requireModule('attendance'), async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), leaveTypeId: z.uuid(), deltaDays: z.number().min(-365).max(365).refine((n) => n !== 0, 'deltaDays cannot be 0'), reason: z.string().min(3).max(500), effectiveDate: isoDate.optional(), kind: z.enum(['adjustment', 'allocation', 'carry_forward']).default('adjustment') }).parse(req.body);
    const emp = await getEmployeeLite(db, b.employeeId);
    const type = await getLeaveType(db, b.leaveTypeId);
    if (!emp || !type || emp.company_id !== req.user!.companyId || type.company_id !== emp.company_id) throw new NotFoundError('employee or leave type not found');
    const effective = b.effectiveDate ?? nptToday();
    const client = await db.connect();
    let id: string; let replacedAutomatic: string | null = null;
    try {
      await client.query('BEGIN');
      // An HR allocation for a fiscal year replaces the automatic annual grant for that year instead of stacking on it.
      if (b.kind === 'allocation' && type.accrual_basis === 'annual') {
        const fy = fiscalYearOf(adToBs(effective));
        const auto = await client.query<{ total: string }>(
          `SELECT coalesce(sum(delta_days), 0)::text AS total FROM leave_ledger WHERE employee_id = $1 AND leave_type_id = $2
              AND effective_date >= $3::date AND effective_date < $4::date AND ((reason = 'allocation' AND ref_type = 'accrual') OR ref_type = 'accrual_replaced')`,
          [b.employeeId, b.leaveTypeId, fy.startAd, fiscalYear(fy.startYear + 1).startAd]);
        const net = Number(auto.rows[0]!.total);
        if (net > 0) {
          await client.query(
            `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note, posted_by) VALUES ($1, $2, $3, $4, 'adjustment', 'accrual_replaced', $5, $6)`,
            [b.employeeId, b.leaveTypeId, effective, -net, `Automatic FY${fy.startYear} grant replaced by HR allocation`, req.user!.id]);
          replacedAutomatic = net.toFixed(2);
        }
      }
      const ins = await client.query<{ id: string }>(
        `INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note, posted_by) VALUES ($1, $2, $3, $4, $5, 'hr_adjustment', $6, $7) RETURNING id`,
        [b.employeeId, b.leaveTypeId, effective, b.deltaDays, b.kind, b.reason, req.user!.id]);
      id = ins.rows[0]!.id;
      await audit(client, { actorId: req.user!.id, action: 'adjust', table: 'leave_ledger', rowId: id, after: { ...b, replacedAutomatic } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.status(201).json({ id, balance: await ledgerBalance(db, b.employeeId, b.leaveTypeId), replacedAutomatic });
  });

  r.get('/ledger', async (req, res) => {
    const q = z.object({ employeeId: z.uuid().optional(), leaveTypeId: z.uuid().optional() }).parse(req.query);
    const employeeId = q.employeeId ?? req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('employeeId required');
    const own = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, req.user!.companyId]);
    if (!own.rows[0]) throw new NotFoundError('employee not found');
    await assertManages(db, req.user!, employeeId, 'attendance');
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
    const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; from_date: string; to_date: string; half_day: string | null; type_code: string; status: string; from_time: string | null; to_time: string | null }>(
      `SELECT lr.employee_id, e.emp_code, e.name_en, lr.from_date, lr.to_date, lr.half_day, lt.code AS type_code, lr.status,
              to_char(lr.from_time, 'HH24:MI') AS from_time, to_char(lr.to_time, 'HH24:MI') AS to_time
         FROM leave_request lr JOIN employee e ON e.id = lr.employee_id JOIN leave_type lt ON lt.id = lr.leave_type_id
         LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND lr.status IN ('pending', 'approved') AND lr.from_date <= $3::date AND lr.to_date >= $2::date
          AND ($4 OR hj_manages($5::uuid, lr.employee_id, current_date) OR lr.employee_id = $5::uuid
               OR a.manager_id = (SELECT manager_id FROM employee_assignment m WHERE m.employee_id = $5::uuid AND m.valid @> current_date))
        ORDER BY lr.from_date`, [u.companyId, q.from, q.to, hasModule(u, 'attendance'), u.employeeId]);
    const days = new Map<string, Array<Record<string, unknown>>>();
    for (const lr of rows.rows) {
      for (const d of dateRange(lr.from_date < q.from ? q.from : lr.from_date, lr.to_date > q.to ? q.to : lr.to_date)) {
        days.set(d, [...(days.get(d) ?? []), { employeeId: lr.employee_id, empCode: lr.emp_code, nameEn: lr.name_en, type: lr.type_code, status: lr.status, halfDay: lr.half_day, ...(lr.from_time ? { fromTime: lr.from_time, toTime: lr.to_time } : {}) }]);
      }
    }
    res.json({ days: [...days.entries()].sort().map(([date, off]) => ({ date, off })) });
  });

  return r;
}

/** A type can be clubbed with another type of the same company that is not itself clubbed (no chains or loops). */
async function assertClubTarget(db: Queryable, companyId: string, typeId: string, targetId: string): Promise<void> {
  if (typeId === targetId) throw new BadRequestError('a leave type cannot be clubbed with itself');
  const t = await db.query<{ club_with_id: string | null }>('SELECT club_with_id FROM leave_type WHERE id = $1 AND company_id = $2', [targetId, companyId]);
  if (!t.rows[0]) throw new BadRequestError('the leave type to club with does not exist');
  if (t.rows[0].club_with_id) throw new BadRequestError('that leave type is itself clubbed with another; club with the main type instead');
  const followers = await db.query('SELECT 1 FROM leave_type WHERE club_with_id = $1 LIMIT 1', [typeId]);
  if (followers.rows.length) throw new BadRequestError('other leave types are clubbed with this one, so it cannot follow another type');
}
