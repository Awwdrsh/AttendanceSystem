import { Router } from 'express';
import { localDateOf } from '@hajiri/attendance-engine';
import { adToBs, bsMonthBounds } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import { BadRequestError } from '../../errors.js';
import { requireRole } from '../auth/middleware.js';
import { listDays } from '../attendance/service.js';

/** Executive and employee home dashboards (plan §3, §13.2: ≤ 6 KPI tiles, one trend, one "needs attention" list). */
export function dashboardRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/executive', requireRole('hr', 'finance', 'manager'), async (req, res) => {
    const cid = req.user!.companyId;
    const today = localDateOf(Date.now());
    let monthStart = today;
    try { const bs = adToBs(today); monthStart = bsMonthBounds(bs.year, bs.month).startAd; } catch { monthStart = today.slice(0, 8) + '01'; }
    const [kpi, trend, devices, unmapped, ot, leaveOld, lastRun] = await Promise.all([
      db.query<{ headcount: number; present: number; late: number; absent: number; pending: number; exceptions: number }>(
        `SELECT (SELECT count(*) FROM employee WHERE company_id = $1 AND status = 'active')::int AS headcount,
                (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = $1 AND v.local_date = $2 AND v.status IN ('Present', 'HalfDay', 'HalfDayLeave', 'WFH', 'Incomplete'))::int AS present,
                (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = $1 AND v.local_date = $2 AND v.late_min > 0)::int AS late,
                (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = $1 AND v.local_date = $2 AND v.status = 'Absent')::int AS absent,
                (SELECT count(*) FROM workflow_instance WHERE company_id = $1 AND state = 'pending')::int AS pending,
                (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = $1 AND v.local_date BETWEEN $3 AND $2 AND v.locked_at IS NULL
                   AND (v.status IN ('Incomplete', 'Absent') OR v.flags @> '[{"kind":"OTPreapprovalMissing"}]' OR v.flags @> '[{"kind":"OTCapExceeded"}]'))::int AS exceptions`,
        [cid, today, monthStart]),
      db.query<{ local_date: string; present_pct: number }>(
        `SELECT v.local_date, round(100.0 * count(*) FILTER (WHERE v.status IN ('Present', 'HalfDay', 'HalfDayLeave', 'WFH')) / nullif(count(*) FILTER (WHERE v.status NOT IN ('Holiday', 'WeeklyOff', 'OffShift')), 0), 1)::float8 AS present_pct
           FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
          WHERE e.company_id = $1 AND v.local_date > $2::date - 30 AND v.local_date <= $2 GROUP BY v.local_date ORDER BY v.local_date`, [cid, today]),
      db.query<{ serial_no: string; name: string | null; last_seen_at: Date | null }>(
        `SELECT serial_no, name, last_seen_at FROM device WHERE company_id = $1 AND status = 'active' AND vendor NOT IN ('mobile', 'kiosk', 'manual')
           AND (last_seen_at IS NULL OR last_seen_at < now() - interval '120 seconds') ORDER BY last_seen_at NULLS FIRST`, [cid]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM punch_event p JOIN device d ON d.id = p.device_id WHERE d.company_id = $1 AND p.employee_id IS NULL AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)`, [cid]),
      db.query<{ n: number; minutes: number }>(`SELECT count(*) FILTER (WHERE s.status = 'detected')::int AS n, coalesce(sum(s.detected_min) FILTER (WHERE s.local_date >= $2), 0)::int AS minutes FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE e.company_id = $1`, [cid, monthStart]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM leave_request lr JOIN employee e ON e.id = lr.employee_id WHERE e.company_id = $1 AND lr.status = 'pending' AND lr.created_at < now() - interval '24 hours'`, [cid]),
      db.query<{ id: string; status: string; run_type: string; bs_year: number; bs_month: number; headcount: number; net: string; gross: string }>(
        `SELECT r.id, r.status, r.run_type, p.bs_year, p.bs_month, (SELECT count(*) FROM payslip s WHERE s.run_id = r.id)::int AS headcount,
                coalesce((SELECT sum(net) FROM payslip s WHERE s.run_id = r.id), 0)::text AS net, coalesce((SELECT sum(gross) FROM payslip s WHERE s.run_id = r.id), 0)::text AS gross
           FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.company_id = $1 ORDER BY r.created_at DESC LIMIT 1`, [cid]),
    ]);
    const k = kpi.rows[0]!;
    res.json({
      date: today,
      tiles: [
        { key: 'headcount', label: 'Active employees', value: k.headcount },
        { key: 'present', label: 'Present today', value: k.present },
        { key: 'late', label: 'Late today', value: k.late },
        { key: 'absent', label: 'Absent today', value: k.absent },
        { key: 'pendingApprovals', label: 'Pending approvals', value: k.pending },
        { key: 'exceptions', label: 'Open exceptions this month', value: k.exceptions },
      ],
      trend: trend.rows.map((t) => ({ date: t.local_date, presentPct: t.present_pct })),
      needsAttention: [
        ...devices.rows.map((d) => ({ kind: 'device_offline', label: `${d.name ?? d.serial_no} is offline`, since: d.last_seen_at })),
        ...(unmapped.rows[0]!.n > 0 ? [{ kind: 'unmapped_punches', label: `${unmapped.rows[0]!.n} punches from unknown PINs`, count: unmapped.rows[0]!.n }] : []),
        ...(ot.rows[0]!.n > 0 ? [{ kind: 'ot_unapproved', label: `${ot.rows[0]!.n} overtime slips awaiting review`, count: ot.rows[0]!.n }] : []),
        ...(leaveOld.rows[0]!.n > 0 ? [{ kind: 'leave_overdue', label: `${leaveOld.rows[0]!.n} leave requests waiting over 24 h`, count: leaveOld.rows[0]!.n }] : []),
      ],
      otMinutesThisMonth: ot.rows[0]!.minutes,
      lastPayrollRun: lastRun.rows[0] ?? null,
    });
  });

  r.get('/me', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const today = localDateOf(Date.now());
    let monthStart = today; let monthEnd = today; let bsLabel: string | null = null;
    try { const bs = adToBs(today); const b = bsMonthBounds(bs.year, bs.month); monthStart = b.startAd; monthEnd = b.endAd; bsLabel = `${bs.year}-${String(bs.month).padStart(2, '0')}`; } catch { /* outside calendar table */ }
    const [days, month, balances, unread, pending] = await Promise.all([
      listDays(db, employeeId, today, today),
      db.query<{ status: string; n: number; late: number; ot: number }>(
        `SELECT status, count(*)::int AS n, coalesce(sum(late_min), 0)::int AS late, coalesce(sum(ot_min), 0)::int AS ot FROM v_attendance_latest
          WHERE employee_id = $1 AND local_date BETWEEN $2 AND $3 GROUP BY status`, [employeeId, monthStart, monthEnd]),
      db.query<{ code: string; name_en: string; name_ne: string | null; balance: string }>(
        `SELECT lt.code, lt.name_en, lt.name_ne, coalesce(sum(l.delta_days), 0)::numeric(8,2)::text AS balance
           FROM leave_type lt JOIN employee e ON e.company_id = lt.company_id AND e.id = $1
           LEFT JOIN leave_ledger l ON l.employee_id = e.id AND l.leave_type_id = lt.id
          WHERE lt.accrual_basis <> 'none' OR lt.code = 'SUBSTITUTE' GROUP BY lt.id, lt.code, lt.name_en, lt.name_ne, lt.sort_order ORDER BY lt.sort_order`, [employeeId]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = $1 AND channel = 'in_app' AND read_at IS NULL`, [req.user!.id]),
      db.query<{ kind: string; id: string; created_at: Date; detail: string }>(
        `SELECT 'leave' AS kind, id, created_at, from_date::text || ' to ' || to_date::text AS detail FROM leave_request WHERE employee_id = $1 AND status = 'pending'
         UNION ALL SELECT 'regularisation', id, created_at, local_date::text || ' ' || kind FROM regularisation_request WHERE employee_id = $1 AND status = 'pending'
         UNION ALL SELECT 'ot', id, created_at, local_date::text || ' ' || requested_min || ' min' FROM ot_request WHERE employee_id = $1 AND status = 'pending'
         ORDER BY created_at DESC LIMIT 20`, [employeeId]),
    ]);
    const summary: Record<string, number> = {}; let lateMin = 0; let otMin = 0;
    for (const m of month.rows) { summary[m.status] = m.n; lateMin += m.late; otMin += m.ot; }
    res.json({ date: today, today: days[0] ?? null, month: { bs: bsLabel, from: monthStart, to: monthEnd, byStatus: summary, lateMin, otMin },
      leaveBalances: balances.rows, unreadNotifications: unread.rows[0]!.n, pendingRequests: pending.rows });
  });

  return r;
}
