import { Router } from 'express';
import { z } from 'zod';
import { bsMonthBounds } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import { isoDate } from '../../validation.js';
import { assertManages, hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import { hours, nptHHMM, sendReport } from './csv.js';

const fmt = z.object({ format: z.enum(['json', 'csv']).optional() });

/** Row filter for employee-level reports: HR sees everyone; a manager sees direct reports and themselves. `h` and `u` are the placeholder numbers. */
const team = (h: number, u: number) => `($${h} OR e.id = $${u}::uuid OR hj_manages($${u}::uuid, e.id, current_date))`;
const range = fmt.extend({ from: isoDate, to: isoDate });

/** Reports (plan §5.10): JSON by default, `?format=csv` for Excel. HR sees the company; managers see their direct reports; salary-based and device reports are HR only. */
export function reportsRoutes({ db }: AppDeps): Router {
  const r = Router();
  r.use(requireRole('hr', 'manager'));
  const scope = (req: { user?: AuthUser }) => [hasRole(req.user!, 'hr'), req.user!.employeeId] as const;

  r.get('/daily', async (req, res) => {
    const q = fmt.extend({ date: isoDate }).parse(req.query);
    const rows = await db.query(
      `SELECT e.emp_code, e.name_en, e.name_ne, b.name_en AS branch, v.status, v.first_in, v.last_out, v.worked_min, v.late_min, v.early_min, v.ot_min, v.flags
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date LEFT JOIN branch b ON b.id = a.branch_id
         LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = $2
        WHERE e.company_id = $1 AND e.status = 'active' AND ${team(3, 4)} ORDER BY e.emp_code`, [req.user!.companyId, q.date, ...scope(req)]);
    const out = rows.rows.map((x) => ({ ...x, status: x.status ?? 'NotComputed', arrival: nptHHMM(x.first_in), departure: nptHHMM(x.last_out), worked_hours: hours(x.worked_min), ot_hours: hours(x.ot_min), flags: JSON.stringify(x.flags ?? []) }));
    sendReport(res, q.format, `daily-${q.date}`, [
      { key: 'emp_code', header: 'Emp code' }, { key: 'name_en', header: 'Name' }, { key: 'branch', header: 'Branch' },
      { key: 'status', header: 'Status' }, { key: 'arrival', header: 'Arrival' }, { key: 'departure', header: 'Departure' }, { key: 'worked_hours', header: 'Worked (h)' },
      { key: 'late_min', header: 'Late (min)' }, { key: 'early_min', header: 'Early out (min)' }, { key: 'ot_hours', header: 'OT (h)' }, { key: 'flags', header: 'Flags' }], out);
  });

  /** Monthly attendance register, inspector-ready (plan §5.10, §6.9): one row per employee per calendar day of the BS month. */
  r.get('/register', async (req, res) => {
    const q = fmt.extend({ bsYear: z.coerce.number().int(), bsMonth: z.coerce.number().int().min(1).max(12), employeeId: z.uuid().optional() }).parse(req.query);
    if (q.employeeId) await assertManages(db, req.user!, q.employeeId);
    const { startAd, endAd } = bsMonthBounds(q.bsYear, q.bsMonth);
    const rows = await db.query(
      `SELECT e.emp_code, e.name_en, e.name_ne, d.day::date AS ad_date, c.bs_date, v.status, v.first_in, v.last_out, v.worked_min, v.break_min, v.ot_min, v.late_min,
              (SELECT lt.code FROM leave_request lr JOIN leave_type lt ON lt.id = lr.leave_type_id
                WHERE lr.employee_id = e.id AND lr.status = 'approved' AND d.day BETWEEN lr.from_date AND lr.to_date LIMIT 1) AS leave_code
         FROM employee e
         CROSS JOIN generate_series($2::date, $3::date, interval '1 day') AS d(day)
         LEFT JOIN bs_calendar c ON c.ad_date = d.day::date
         LEFT JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date = d.day::date
        WHERE e.company_id = $1 AND ($4::uuid IS NULL OR e.id = $4)
          AND (e.join_date IS NULL OR e.join_date <= $3::date) AND (e.exit_date IS NULL OR e.exit_date >= $2::date) AND ${team(5, 6)}
        ORDER BY e.emp_code, d.day`, [req.user!.companyId, startAd, endAd, q.employeeId ?? null, ...scope(req)]);
    const out = rows.rows.map((x) => ({
      emp_code: x.emp_code, name_en: x.name_en, name_ne: x.name_ne, bs_date: x.bs_date, ad_date: String(x.ad_date).slice(0, 10),
      arrival: nptHHMM(x.first_in), departure: nptHHMM(x.last_out), worked_hours: hours(x.worked_min), ot_hours: hours(x.ot_min), break_min: x.break_min ?? 0,
      late_min: x.late_min ?? 0, leave: x.leave_code ?? '', status: x.status ?? '',
    }));
    sendReport(res, q.format, `register-${q.bsYear}-${String(q.bsMonth).padStart(2, '0')}`, [
      { key: 'emp_code', header: 'Emp code' }, { key: 'name_en', header: 'Name' }, { key: 'bs_date', header: 'Date (BS)' },
      { key: 'ad_date', header: 'Date (AD)' }, { key: 'arrival', header: 'Arrival' }, { key: 'departure', header: 'Departure' }, { key: 'worked_hours', header: 'Worked (h)' },
      { key: 'break_min', header: 'Break (min)' }, { key: 'ot_hours', header: 'OT (h)' }, { key: 'late_min', header: 'Late (min)' }, { key: 'leave', header: 'Leave' }, { key: 'status', header: 'Status' }], out);
  });

  r.get('/ot', async (req, res) => {
    const q = range.parse(req.query);
    const rows = await db.query(
      `SELECT e.emp_code, e.name_en, s.local_date, s.day_type, s.detected_min, s.approved_min, s.multiplier, s.status
         FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE e.company_id = $1 AND s.local_date BETWEEN $2 AND $3 AND ${team(4, 5)} ORDER BY s.local_date, e.emp_code`,
      [req.user!.companyId, q.from, q.to, ...scope(req)]);
    const out = rows.rows.map((x) => ({ ...x, detected_hours: hours(x.detected_min), approved_hours: hours(x.approved_min) }));
    sendReport(res, q.format, `ot-${q.from}_${q.to}`, [
      { key: 'emp_code', header: 'Emp code' }, { key: 'name_en', header: 'Name' }, { key: 'local_date', header: 'Date' }, { key: 'day_type', header: 'Day type' },
      { key: 'detected_hours', header: 'Detected (h)' }, { key: 'approved_hours', header: 'Approved (h)' }, { key: 'multiplier', header: 'Rate' }, { key: 'status', header: 'Status' }], out);
  });

  r.get('/late-early', async (req, res) => {
    const q = range.parse(req.query);
    const rows = await db.query(
      `SELECT e.emp_code, e.name_en, count(*) FILTER (WHERE v.late_min > 0)::int AS late_days, coalesce(sum(v.late_min), 0)::int AS late_min,
              count(*) FILTER (WHERE v.early_min > 0)::int AS early_days, coalesce(sum(v.early_min), 0)::int AS early_min,
              count(*) FILTER (WHERE v.status = 'Absent')::int AS absent_days, count(*) FILTER (WHERE v.status = 'HalfDay')::int AS half_days
         FROM employee e JOIN v_attendance_latest v ON v.employee_id = e.id AND v.local_date BETWEEN $2 AND $3
        WHERE e.company_id = $1 AND ${team(4, 5)} GROUP BY e.id, e.emp_code, e.name_en ORDER BY late_days DESC, e.emp_code`, [req.user!.companyId, q.from, q.to, ...scope(req)]);
    sendReport(res, q.format, `late-early-${q.from}_${q.to}`, [
      { key: 'emp_code', header: 'Emp code' }, { key: 'name_en', header: 'Name' }, { key: 'late_days', header: 'Late days' }, { key: 'late_min', header: 'Late (min)' },
      { key: 'early_days', header: 'Early-out days' }, { key: 'early_min', header: 'Early (min)' }, { key: 'absent_days', header: 'Absent' }, { key: 'half_days', header: 'Half days' }], rows.rows);
  });

  /** Leave balances per employee and type; liability = balance × daily basic for encashable types (plan §5.10). */
  r.get('/leave-balances', requireRole('hr'), async (req, res) => {
    const q = fmt.parse(req.query);
    const rows = await db.query(
      `SELECT e.emp_code, e.name_en, lt.code AS leave_type, lt.encashable, coalesce(sum(l.delta_days), 0)::numeric(8,2)::text AS balance,
              (SELECT (sa.base_amounts->>'BASIC') FROM salary_assignment sa WHERE sa.employee_id = e.id AND sa.valid @> current_date) AS basic
         FROM employee e JOIN leave_type lt ON lt.company_id = e.company_id
         LEFT JOIN leave_ledger l ON l.employee_id = e.id AND l.leave_type_id = lt.id
        WHERE e.company_id = $1 AND e.status = 'active' AND lt.accrual_basis <> 'none'
        GROUP BY e.id, e.emp_code, e.name_en, lt.code, lt.encashable, lt.sort_order ORDER BY e.emp_code, lt.sort_order`, [req.user!.companyId]);
    const days = (await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM bs_calendar c WHERE (c.bs_year, c.bs_month) = (SELECT bs_year, bs_month FROM bs_calendar WHERE ad_date = current_date)`)).rows[0]?.n || 30;
    const out = rows.rows.map((x) => {
      const basic = x.basic ? Number(x.basic) : null;
      const liability = x.encashable && basic !== null ? ((basic / days) * Number(x.balance)).toFixed(2) : '';
      return { emp_code: x.emp_code, name_en: x.name_en, leave_type: x.leave_type, balance: x.balance, encashable: x.encashable, liability_npr: liability };
    });
    sendReport(res, q.format, 'leave-balances', [
      { key: 'emp_code', header: 'Emp code' }, { key: 'name_en', header: 'Name' }, { key: 'leave_type', header: 'Leave type' }, { key: 'balance', header: 'Balance (days)' },
      { key: 'encashable', header: 'Encashable' }, { key: 'liability_npr', header: 'Liability (NPR)' }], out);
  });

  r.get('/headcount', async (req, res) => {
    const q = fmt.parse(req.query);
    const rows = await db.query(
      `SELECT coalesce(b.name_en, '(no branch)') AS branch, coalesce(o.name_en, '(no unit)') AS org_unit, a.employment_type,
              count(*)::int AS headcount, count(*) FILTER (WHERE e.gender = 'female')::int AS female, count(*) FILTER (WHERE e.gender = 'male')::int AS male
         FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
         LEFT JOIN branch b ON b.id = a.branch_id LEFT JOIN org_unit o ON o.id = a.org_unit_id
        WHERE e.company_id = $1 AND e.status = 'active' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`, [req.user!.companyId]);
    sendReport(res, q.format, 'headcount', [
      { key: 'branch', header: 'Branch' }, { key: 'org_unit', header: 'Unit' }, { key: 'employment_type', header: 'Type' }, { key: 'headcount', header: 'Headcount' },
      { key: 'female', header: 'Female' }, { key: 'male', header: 'Male' }], rows.rows);
  });

  r.get('/exceptions-summary', async (req, res) => {
    const q = range.parse(req.query);
    const rows = await db.query(
      `SELECT v.local_date, count(*) FILTER (WHERE v.status = 'Incomplete')::int AS incomplete, count(*) FILTER (WHERE v.status = 'Absent')::int AS absent,
              count(*) FILTER (WHERE v.flags @> '[{"kind":"OTPreapprovalMissing"}]')::int AS ot_unapproved, count(*) FILTER (WHERE v.flags @> '[{"kind":"OTCapExceeded"}]')::int AS ot_cap,
              count(*) FILTER (WHERE v.late_min > 0)::int AS late
         FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
        WHERE e.company_id = $1 AND v.local_date BETWEEN $2 AND $3 AND ${team(4, 5)} GROUP BY v.local_date ORDER BY v.local_date`, [req.user!.companyId, q.from, q.to, ...scope(req)]);
    sendReport(res, q.format, `exceptions-${q.from}_${q.to}`, [
      { key: 'local_date', header: 'Date' }, { key: 'incomplete', header: 'Incomplete' }, { key: 'absent', header: 'Absent' }, { key: 'late', header: 'Late' },
      { key: 'ot_unapproved', header: 'OT without approval' }, { key: 'ot_cap', header: 'OT cap exceeded' }], rows.rows);
  });

  r.get('/devices', requireRole('hr'), async (req, res) => {
    const q = fmt.parse(req.query);
    const rows = await db.query(
      `SELECT d.serial_no, d.name, d.vendor, d.status, d.fw_version, d.last_handshake_at, d.last_seen_at,
              (d.status = 'active' AND d.vendor NOT IN ('mobile', 'kiosk', 'manual') AND (d.last_seen_at IS NULL OR d.last_seen_at < now() - interval '120 seconds')) AS offline,
              (SELECT count(*) FROM punch_event p WHERE p.device_id = d.id AND p.local_date = current_date)::int AS punches_today
         FROM device d WHERE d.company_id = $1 ORDER BY d.status, d.serial_no`, [req.user!.companyId]);
    sendReport(res, q.format, 'device-health', [
      { key: 'serial_no', header: 'Serial' }, { key: 'name', header: 'Name' }, { key: 'vendor', header: 'Vendor' }, { key: 'status', header: 'Status' },
      { key: 'fw_version', header: 'Firmware' }, { key: 'last_seen_at', header: 'Last seen' }, { key: 'offline', header: 'Offline' }, { key: 'punches_today', header: 'Punches today' }], rows.rows);
  });

  r.get('/punch-audit', async (req, res) => {
    const q = range.extend({ employeeId: z.uuid() }).parse(req.query);
    await assertManages(db, req.user!, q.employeeId);
    const rows = await db.query(
      `SELECT p.id, p.local_date, p.punched_at, p.direction, p.verify_mode, p.source, p.device_sn, p.ingested_at, p.supersedes_id,
              (SELECT count(*) FROM punch_event s WHERE s.supersedes_id = p.id) > 0 AS superseded, p.raw->>'reason' AS reason
         FROM punch_event p WHERE p.employee_id = $1 AND p.local_date BETWEEN $2 AND $3 ORDER BY p.punched_at`, [q.employeeId, q.from, q.to]);
    const out = rows.rows.map((x) => ({ ...x, time: nptHHMM(x.punched_at) }));
    sendReport(res, q.format, `punches-${q.employeeId}`, [
      { key: 'local_date', header: 'Date' }, { key: 'time', header: 'Time' }, { key: 'direction', header: 'Direction' }, { key: 'verify_mode', header: 'Verify' },
      { key: 'source', header: 'Source' }, { key: 'device_sn', header: 'Device' }, { key: 'superseded', header: 'Superseded' }, { key: 'reason', header: 'Reason' }], out);
  });

  return r;
}
