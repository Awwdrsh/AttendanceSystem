import type { Queryable } from '../../db/pool.js';
import type { AuthUser } from '../auth/service.js';
import { hasRole } from '../auth/middleware.js';
import { dateRange } from '../attendance/service.js';

/**
 * Who's out (study: "Who is Out"): people away today and over the next days — on leave, travelling, working from home or
 * out on field visits. The leave type is never shown (sick leave is private); only "On leave", with half day or hours.
 * Scope comes from the company setting: the viewer's team, department or the whole company.
 */
export type OutKind = 'leave' | 'travel' | 'wfh' | 'field';
export interface OutPerson { employeeId: string; name: string; designation: string | null; kind: OutKind; detail: string | null }

export async function scopeEmployeeIds(db: Queryable, user: AuthUser, scope: 'team' | 'department' | 'company'): Promise<string[] | null> {
  if (scope === 'company') return null;
  if (!user.employeeId) return hasRole(user, 'hr') ? null : [];
  if (scope === 'department') {
    const r = await db.query<{ id: string }>(
      `SELECT a.employee_id AS id FROM employee_assignment a JOIN employee_assignment me ON me.employee_id = $1 AND me.valid @> current_date
        WHERE a.valid @> current_date AND me.org_unit_id IS NOT NULL AND a.org_unit_id = me.org_unit_id`, [user.employeeId]);
    return r.rows.length ? r.rows.map((x) => x.id) : [user.employeeId];
  }
  // Team: a manager's direct reports, plus the people who share the viewer's manager, plus that manager.
  const r = await db.query<{ id: string }>(
    `SELECT a.employee_id AS id FROM employee_assignment a WHERE a.valid @> current_date AND a.manager_id = $1
     UNION SELECT a.employee_id FROM employee_assignment a JOIN employee_assignment me ON me.employee_id = $1 AND me.valid @> current_date
      WHERE a.valid @> current_date AND me.manager_id IS NOT NULL AND (a.manager_id = me.manager_id OR a.employee_id = me.manager_id)
     UNION SELECT $1::uuid`, [user.employeeId]);
  return r.rows.map((x) => x.id);
}

export async function whosOut(db: Queryable, companyId: string, ids: string[] | null, from: string, to: string): Promise<Array<{ date: string; people: OutPerson[] }>> {
  const p = [companyId, from, to, ids];
  const inScope = `e.company_id = $1 AND e.status = 'active' AND ($4::uuid[] IS NULL OR e.id = ANY($4::uuid[]))`;
  const [leave, travel, wfh, field] = await Promise.all([
    db.query<{ employee_id: string; name_en: string; designation: string | null; from_date: string; to_date: string; half_day: string | null; hours: string | null }>(
      `SELECT e.id AS employee_id, e.name_en, e.designation, lr.from_date::text, lr.to_date::text, lr.half_day, lr.hours::text
         FROM leave_request lr JOIN employee e ON e.id = lr.employee_id
        WHERE ${inScope} AND lr.status = 'approved' AND lr.to_date >= $2 AND lr.from_date <= $3`, p),
    db.query<{ employee_id: string; name_en: string; designation: string | null; from_date: string; to_date: string; destination: string | null }>(
      `SELECT e.id AS employee_id, e.name_en, e.designation, t.from_date::text, t.to_date::text, t.destination
         FROM travel_request t JOIN employee e ON e.id = t.employee_id
        WHERE ${inScope} AND t.status IN ('approved', 'settling', 'settled') AND t.to_date >= $2 AND t.from_date <= $3`, p),
    db.query<{ employee_id: string; name_en: string; designation: string | null; local_date: string }>(
      `SELECT DISTINCT e.id AS employee_id, e.name_en, e.designation, x.local_date::text FROM employee e
         JOIN (SELECT employee_id, local_date FROM regularisation_request WHERE kind = 'wfh' AND status = 'approved' AND local_date BETWEEN $2 AND $3
               UNION SELECT employee_id, local_date FROM punch_event WHERE local_date BETWEEN $2 AND $3 AND raw->'flags' ? 'Remote') x ON x.employee_id = e.id
        WHERE ${inScope}`, p),
    db.query<{ employee_id: string; name_en: string; designation: string | null; local_date: string; place: string }>(
      `SELECT DISTINCT ON (e.id, v.local_date) e.id AS employee_id, e.name_en, e.designation, v.local_date::text, v.place
         FROM field_visit v JOIN employee e ON e.id = v.employee_id
        WHERE ${inScope} AND v.local_date BETWEEN $2 AND $3 ORDER BY e.id, v.local_date, v.started_at DESC`, p),
  ]);
  const days = dateRange(from, to).map((date) => ({ date, people: [] as OutPerson[] }));
  const add = (date: string, x: OutPerson) => {
    const d = days.find((y) => y.date === date);
    if (d && !d.people.some((q) => q.employeeId === x.employeeId)) d.people.push(x);
  };
  for (const l of leave.rows) for (const d of dateRange(l.from_date < from ? from : l.from_date, l.to_date > to ? to : l.to_date)) {
    add(d, { employeeId: l.employee_id, name: l.name_en, designation: l.designation, kind: 'leave',
      detail: l.hours ? `${Number(l.hours)} h` : l.half_day ? (l.half_day === 'first_half' ? 'First half' : 'Second half') : null });
  }
  for (const t of travel.rows) for (const d of dateRange(t.from_date < from ? from : t.from_date, t.to_date > to ? to : t.to_date)) {
    add(d, { employeeId: t.employee_id, name: t.name_en, designation: t.designation, kind: 'travel', detail: t.destination });
  }
  for (const w of wfh.rows) add(w.local_date, { employeeId: w.employee_id, name: w.name_en, designation: w.designation, kind: 'wfh', detail: null });
  for (const f of field.rows) add(f.local_date, { employeeId: f.employee_id, name: f.name_en, designation: f.designation, kind: 'field', detail: f.place });
  for (const d of days) d.people.sort((a, b) => a.name.localeCompare(b.name));
  return days;
}
