import { Router, type Request } from 'express';
import { z } from 'zod';
import { adToBs, bsMonthBounds } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import type { Db, Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { bsRange } from '../../bsdate.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { companyIdOf, hasModule, hasRole, MODULE_ROLES, requireModule, requireRole } from '../auth/middleware.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { approversFor, assertManages, getInstance, nptToday } from '../workflow/service.js';
import { attendanceIssues, summarise } from './issues.js';
import { applyPenalties, monthLocked, penaltyPreview, reversePenalty } from './penalties.js';
import { extrasPolicy, extrasPolicySchema, saveExtrasPolicy } from './policy.js';
import { checkUpload, importUpload, UPLOAD_TEMPLATE } from './upload.js';
import { scopeEmployeeIds, whosOut } from './whosout.js';

/** The acting person's own employee id, so nobody but an administrator edits their own attendance or penalties. */
const selfOf = (req: Request): string | null => (req.user && !hasRole(req.user, 'admin') ? (req.user.employeeId ?? null) : null);

const addDays = (iso: string, n: number): string => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const month = z.object({ bsYear: z.coerce.number().int().min(2000).max(2100), bsMonth: z.coerce.number().int().min(1).max(12) });
const range = z.object({ from: isoDate, to: isoDate }).refine((r) => r.from <= r.to, 'from must be on or before to')
  .refine((r) => (Date.parse(r.to) - Date.parse(r.from)) / 86_400_000 <= 92, 'pick at most 92 days');

async function tx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const c = await db.connect();
  try { await c.query('BEGIN'); const out = await fn(c); await c.query('COMMIT'); return out; }
  catch (e) { await c.query('ROLLBACK'); throw e; } finally { c.release(); }
}

/** Leave & attendance extras: rules, late penalties, attendance issues and nudges, manual upload, who's out. */
export function attendanceToolsRoutes({ db }: AppDeps): Router {
  const r = Router();
  const staff = requireRole(...MODULE_ROLES.attendance, 'manager');

  // ------------------------------------------------------------ rules
  r.get('/policy', async (req, res) => {
    res.json({ policy: await extrasPolicy(db, companyIdOf(req)) });
  });

  r.put('/policy', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = extrasPolicySchema.parse(req.body);
    if (b.latePenalty.leaveTypeIds.length) {
      const t = await db.query<{ id: string; paid: boolean; event_days: string | null }>(
        'SELECT id, paid, event_days FROM leave_type WHERE company_id = $1 AND id = ANY($2::uuid[])', [companyId, b.latePenalty.leaveTypeIds]);
      if (t.rows.length !== new Set(b.latePenalty.leaveTypeIds).size) throw new BadRequestError('a chosen leave type does not exist');
      if (t.rows.some((x) => !x.paid || x.event_days !== null)) throw new BadRequestError('penalties can only be taken from paid leave with a balance');
    }
    const before = await extrasPolicy(db, companyId);
    await saveExtrasPolicy(db, companyId, b, req.user!.id);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: companyId, before: { 'attendance.extras': before }, after: { 'attendance.extras': b } });
    res.json({ policy: b });
  });

  // ------------------------------------------------------------ late-arrival penalties
  r.get('/penalties', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const q = month.parse(req.query);
    const policy = await extrasPolicy(db, companyId);
    const scope = hasModule(req.user!, 'attendance') ? {} : { managerEmployeeId: req.user!.employeeId ?? '00000000-0000-0000-0000-000000000000' };
    const lines = await penaltyPreview(db, companyId, q.bsYear, q.bsMonth, policy.latePenalty, scope);
    res.json({ rule: policy.latePenalty, locked: await monthLocked(db, companyId, q.bsYear, q.bsMonth), lines });
  });

  r.get('/penalties/me', async (req, res) => {
    const companyId = companyIdOf(req);
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('your account is not linked to an employee');
    const today = adToBs(nptToday());
    const q = month.partial().parse(req.query);
    const y = q.bsYear ?? today.year; const m = q.bsMonth ?? today.month;
    const policy = await extrasPolicy(db, companyId);
    const line = (await penaltyPreview(db, companyId, y, m, policy.latePenalty, { employeeId }))[0] ?? null;
    res.json({ bsYear: y, bsMonth: m, enabled: policy.latePenalty.enabled, rule: { lateCount: policy.latePenalty.lateCount, penaltyDays: policy.latePenalty.penaltyDays, minLateMinutes: policy.latePenalty.minLateMinutes },
      lateDays: line?.lateDays ?? 0, lateDates: line?.lateDates ?? [], projectedDays: policy.latePenalty.enabled ? (line?.penaltyDays ?? 0) : 0, applied: line?.applied ?? null });
  });

  r.post('/penalties/apply', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = month.extend({ employeeIds: z.array(z.uuid()).max(2000).optional() }).parse(req.body);
    const policy = await extrasPolicy(db, companyId);
    if (!policy.latePenalty.enabled) throw new ConflictError('turn the late-arrival penalty on first');
    if (b.employeeIds && selfOf(req) && b.employeeIds.includes(selfOf(req)!)) throw new ForbiddenError('someone else must apply your own late penalty');
    const out = await tx(db, (c) => applyPenalties(c, companyId, b.bsYear, b.bsMonth, policy.latePenalty, b.employeeIds ?? null, req.user!.id));
    res.json(out);
  });

  r.post('/penalties/:id/reverse', requireModule('attendance'), async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ reason: z.string().trim().min(3).max(300) }).parse(req.body);
    await tx(db, (c) => reversePenalty(c, companyIdOf(req), id, b.reason, req.user!.id, selfOf(req)));
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ attendance issues and nudges
  r.get('/issues', staff, async (req, res) => {
    const q = range.parse(req.query);
    const manager = hasModule(req.user!, 'attendance') ? null : (req.user!.employeeId ?? '00000000-0000-0000-0000-000000000000');
    const rows = await attendanceIssues(db, companyIdOf(req), q.from, q.to, manager);
    res.json({ summary: summarise(rows), rows });
  });

  /** Pre-payroll check: the open issues of a BS month in numbers (the payroll pre-flight links here). */
  r.get('/issues/summary', requireModule('attendance'), async (req, res) => {
    const q = month.parse(req.query);
    const { startAd, endAd } = bsMonthBounds(q.bsYear, q.bsMonth);
    const rows = await attendanceIssues(db, companyIdOf(req), startAd, endAd, null);
    res.json({ bsYear: q.bsYear, bsMonth: q.bsMonth, from: startAd, to: endAd, ...summarise(rows) });
  });

  r.post('/issues/nudge', staff, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.discriminatedUnion('target', [
      z.object({ target: z.literal('employee'), employeeId: z.uuid(), from: isoDate, to: isoDate }),
      z.object({ target: z.literal('approver'), instanceId: z.uuid() }),
    ]).parse(req.body);
    const today = nptToday();
    if (b.target === 'employee') {
      const emp = await db.query<{ id: string }>('SELECT id FROM employee WHERE id = $1 AND company_id = $2', [b.employeeId, companyId]);
      if (!emp.rows[0]) throw new NotFoundError('employee not found');
      await assertManages(db, req.user!, b.employeeId);
      if (b.employeeId === req.user!.employeeId) throw new BadRequestError('you cannot nudge yourself');
      const rows = (await attendanceIssues(db, companyId, b.from, b.to, null)).find((x) => x.employeeId === b.employeeId);
      if (!rows || (!rows.missing.length && !rows.absent.length)) throw new ConflictError('there is nothing for this employee to fix in this period');
      const userId = await userIdForEmployee(db, b.employeeId);
      if (!userId) throw new ConflictError('this employee has no sign-in to notify');
      const ins = await db.query(
        `INSERT INTO attendance_nudge (company_id, target_user_id, kind, ref, local_date, nudged_by) VALUES ($1, $2, 'employee', $3, $4, $5) ON CONFLICT DO NOTHING RETURNING id`,
        [companyId, userId, `${b.from}:${b.to}`, today, req.user!.id]);
      if (!ins.rows.length) return res.json({ sent: 0, already: true });
      const parts = [rows.missing.length ? `${rows.missing.length} day(s) with a missing punch` : '', rows.absent.length ? `${rows.absent.length} absent day(s) with no leave or time request` : ''].filter(Boolean).join(' and ');
      await notify(db, { companyId, userId, employeeId: b.employeeId, template: 'attendance.nudge', title: 'Please fix your attendance',
        body: `You have ${parts} between ${bsRange(b.from, b.to)}. Apply for leave or raise a time request before payroll.`, payload: { from: b.from, to: b.to }, channels: ['push'] });
      return res.json({ sent: 1, already: false });
    }
    const inst = await getInstance(db, b.instanceId);
    if (!inst || inst.company_id !== companyId || !['leave_request', 'regularisation_request'].includes(inst.object_type)) throw new NotFoundError('request not found');
    if (inst.state !== 'pending') throw new ConflictError('this request is no longer waiting');
    if (inst.employee_id) await assertManages(db, req.user!, inst.employee_id);
    const step = inst.steps[inst.current_step];
    const ids = step ? [...new Set(await approversFor(db, companyId, inst.employee_id, step.role))].filter((id) => id !== req.user!.id) : [];
    if (!ids.length) throw new ConflictError('there is nobody else to remind about this request');
    const emp = inst.employee_id ? (await db.query<{ name_en: string }>('SELECT name_en FROM employee WHERE id = $1', [inst.employee_id])).rows[0] : null;
    let sent = 0;
    for (const id of ids) {
      const ins = await db.query(
        `INSERT INTO attendance_nudge (company_id, target_user_id, kind, ref, local_date, nudged_by) VALUES ($1, $2, 'approver', $3, $4, $5) ON CONFLICT DO NOTHING RETURNING id`,
        [companyId, id, inst.id, today, req.user!.id]);
      if (!ins.rows.length) continue;
      await notify(db, { companyId, userId: id, template: 'attendance.nudge_approver', title: 'A request is waiting for you',
        body: `${emp?.name_en ?? 'An employee'}'s ${inst.object_type === 'leave_request' ? 'leave' : 'time'} request has been waiting since ${bsRange(inst.created_at.toISOString().slice(0, 10), inst.created_at.toISOString().slice(0, 10))}. Please decide it before payroll.`,
        payload: { instanceId: inst.id }, channels: ['push'] });
      sent++;
    }
    return res.json({ sent, already: sent === 0 });
  });

  // ------------------------------------------------------------ manual attendance upload
  r.get('/upload/template', requireModule('attendance'), (_req, res) => {
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="attendance-upload-template.csv"');
    res.send(`﻿${UPLOAD_TEMPLATE}`);
  });

  const uploadBody = z.object({ csv: z.string().min(1).max(1_000_000), filename: z.string().max(200).optional() });
  r.post('/upload/check', requireModule('attendance'), async (req, res) => {
    const b = uploadBody.parse(req.body);
    const out = await checkUpload(db, companyIdOf(req), b.csv, selfOf(req));
    res.json({ errors: out.errors, rows: out.rows, valid: out.rows.filter((x) => !x.errors.length).length, invalid: out.rows.filter((x) => x.errors.length).length });
  });

  r.post('/upload/import', requireModule('attendance'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = uploadBody.extend({ reason: z.string().trim().min(5).max(300) }).parse(req.body);
    const checked = await checkUpload(db, companyId, b.csv, selfOf(req));
    if (checked.errors.length) throw new BadRequestError(checked.errors.join(' '));
    const bad = checked.rows.filter((x) => x.errors.length);
    if (bad.length) throw new BadRequestError(`${bad.length} row(s) have errors; fix them or remove them and upload again`, { rows: bad.map((x) => ({ line: x.line, errors: x.errors })) });
    if (!checked.rows.length) throw new BadRequestError('there are no rows to import');
    const out = await importUpload(db, companyId, checked.rows, b.reason, req.user!.id);
    const ins = await db.query<{ id: string }>(
      `INSERT INTO attendance_upload (company_id, filename, reason, rows_total, punches, duplicates, uploaded_by) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
      [companyId, b.filename ?? null, b.reason, checked.rows.length, out.punches, out.duplicates, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'attendance_upload', table: 'attendance_upload', rowId: ins.rows[0]!.id, after: { rows: checked.rows.length, ...out, reason: b.reason } });
    res.status(201).json({ id: ins.rows[0]!.id, rows: checked.rows.length, ...out });
  });

  r.get('/uploads', requireModule('attendance'), async (req, res) => {
    const rows = await db.query(
      `SELECT u.id, u.filename, u.reason, u.rows_total, u.punches, u.duplicates, u.created_at, a.name AS uploaded_by_name
         FROM attendance_upload u LEFT JOIN app_user a ON a.id = u.uploaded_by WHERE u.company_id = $1 ORDER BY u.created_at DESC LIMIT 50`, [companyIdOf(req)]);
    res.json({ uploads: rows.rows });
  });

  // ------------------------------------------------------------ who's out
  r.get('/whos-out', async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({ days: z.coerce.number().int().min(1).max(14).default(7) }).parse(req.query);
    const policy = await extrasPolicy(db, companyId);
    const ids = await scopeEmployeeIds(db, req.user!, policy.whosOutScope);
    if (ids && !ids.length) return res.json({ scope: policy.whosOutScope, days: [] });
    const from = nptToday();
    const to = addDays(from, q.days - 1);
    return res.json({ scope: policy.whosOutScope, days: await whosOut(db, companyId, ids, from, to) });
  });

  return r;
}
