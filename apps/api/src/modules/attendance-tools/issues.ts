import type { Queryable } from '../../db/pool.js';
import { approversFor, type WorkflowStep } from '../workflow/service.js';

/**
 * Attendance issues for a period (study: "Issues and notifications"): missing punches, absences nobody has applied leave
 * or a time request for, and leave and time requests still waiting, with who they are waiting on. HR sees the company,
 * a manager their direct reports.
 */
export interface PendingItem {
  instanceId: string; type: 'leave' | 'time'; requestId: string; from: string; to: string; label: string; createdAt: string; ageDays: number;
  waitingOn: Array<{ userId: string; name: string }>;
}
export interface IssueRow {
  employeeId: string; empCode: string; name: string; missing: string[]; absent: string[]; pending: PendingItem[];
}

export async function attendanceIssues(db: Queryable, companyId: string, from: string, to: string, managerEmployeeId: string | null): Promise<IssueRow[]> {
  const scope = [companyId, from, to, managerEmployeeId];
  const team = `($4::uuid IS NULL OR e.id IN (SELECT a.employee_id FROM employee_assignment a WHERE a.manager_id = $4 AND a.valid @> current_date))`;
  const days = await db.query<{ employee_id: string; emp_code: string; name_en: string; local_date: string; status: string; issue: 'missing' | 'absent' }>(
    `SELECT e.id AS employee_id, e.emp_code, e.name_en, v.local_date::text, v.status,
            CASE WHEN v.status = 'Incomplete' OR v.flags @> '[{"kind":"MissingOut"}]' OR v.flags @> '[{"kind":"UnpairedPunch"}]' THEN 'missing' ELSE 'absent' END AS issue
       FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
      WHERE e.company_id = $1 AND e.status = 'active' AND v.local_date BETWEEN $2 AND $3 AND ${team}
        -- today is still in progress: nobody has "missed" anything yet
        AND v.local_date < (now() AT TIME ZONE 'Asia/Kathmandu')::date
        AND (v.status = 'Incomplete' OR v.flags @> '[{"kind":"MissingOut"}]' OR v.flags @> '[{"kind":"UnpairedPunch"}]'
             OR (v.status = 'Absent' AND NOT v.flags @> '[{"kind":"HalfDayLeave"}]'))
        -- an absence someone has already asked to cover is not an open issue
        AND NOT EXISTS (SELECT 1 FROM leave_request lr WHERE lr.employee_id = e.id AND lr.status = 'pending' AND v.local_date BETWEEN lr.from_date AND lr.to_date)
        AND NOT EXISTS (SELECT 1 FROM regularisation_request rr WHERE rr.employee_id = e.id AND rr.status = 'pending' AND rr.local_date = v.local_date)
      ORDER BY e.emp_code, v.local_date`, scope);

  const pend = await db.query<{ instance_id: string; object_type: string; object_id: string; employee_id: string; emp_code: string; name_en: string; steps: WorkflowStep[]; current_step: number;
    created_at: Date; from_date: string; to_date: string; label: string }>(
    `SELECT wi.id AS instance_id, wi.object_type, wi.object_id, e.id AS employee_id, e.emp_code, e.name_en, wi.steps, wi.current_step, wi.created_at,
            coalesce(lr.from_date, rr.local_date)::text AS from_date, coalesce(lr.to_date, rr.local_date)::text AS to_date,
            CASE WHEN lr.id IS NOT NULL THEN lt.name_en ELSE replace(initcap(rr.kind), '_', ' ') END AS label
       FROM workflow_instance wi JOIN employee e ON e.id = wi.employee_id
       LEFT JOIN leave_request lr ON wi.object_type = 'leave_request' AND lr.id = wi.object_id
       LEFT JOIN leave_type lt ON lt.id = lr.leave_type_id
       LEFT JOIN regularisation_request rr ON wi.object_type = 'regularisation_request' AND rr.id = wi.object_id
      WHERE wi.company_id = $1 AND wi.state = 'pending' AND wi.object_type IN ('leave_request', 'regularisation_request') AND ${team}
        AND coalesce(lr.to_date, rr.local_date) >= $2 AND coalesce(lr.from_date, rr.local_date) <= $3
      ORDER BY wi.created_at`, scope);

  const cache = new Map<string, string[]>();
  const names = new Map<string, string>();
  const byEmp = new Map<string, IssueRow>();
  const row = (id: string, code: string, name: string): IssueRow => {
    let r = byEmp.get(id);
    if (!r) { r = { employeeId: id, empCode: code, name, missing: [], absent: [], pending: [] }; byEmp.set(id, r); }
    return r;
  };
  for (const d of days.rows) row(d.employee_id, d.emp_code, d.name_en)[d.issue].push(d.local_date);
  const now = Date.now();
  for (const p of pend.rows) {
    const step = p.steps[p.current_step];
    const ids = step ? await approversFor(db, companyId, p.employee_id, step.role, cache) : [];
    const missingNames = ids.filter((i) => !names.has(i));
    if (missingNames.length) {
      const u = await db.query<{ id: string; name: string }>('SELECT id, name FROM app_user WHERE id = ANY($1::uuid[])', [missingNames]);
      for (const x of u.rows) names.set(x.id, x.name);
    }
    row(p.employee_id, p.emp_code, p.name_en).pending.push({
      instanceId: p.instance_id, type: p.object_type === 'leave_request' ? 'leave' : 'time', requestId: p.object_id, from: p.from_date, to: p.to_date, label: p.label,
      createdAt: p.created_at.toISOString(), ageDays: Math.floor((now - p.created_at.getTime()) / 86_400_000),
      waitingOn: ids.map((id) => ({ userId: id, name: names.get(id) ?? 'Approver' })),
    });
  }
  return [...byEmp.values()].sort((a, b) => a.empCode.localeCompare(b.empCode));
}

export function summarise(rows: IssueRow[]): { employees: number; missing: number; absent: number; pendingLeave: number; pendingTime: number; oldestPendingDays: number } {
  const pending = rows.flatMap((r) => r.pending);
  return {
    employees: rows.length,
    missing: rows.reduce((s, r) => s + r.missing.length, 0),
    absent: rows.reduce((s, r) => s + r.absent.length, 0),
    pendingLeave: pending.filter((p) => p.type === 'leave').length,
    pendingTime: pending.filter((p) => p.type === 'time').length,
    oldestPendingDays: pending.reduce((m, p) => Math.max(m, p.ageDays), 0),
  };
}
