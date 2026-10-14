import { D } from '@hajiri/payroll-engine';
import type { Queryable } from '../../db/pool.js';

const csv = (rows: string[][]) => rows.map((r) => r.map((c) => (/[",\n]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c)).join(',')).join('\r\n') + '\r\n';

/** SSF monthly contribution sheet (plan §11.5, §11.10): per contributor basic, 11%, 20%, scheme allocation, 31% total. Official SOSYS layout to be confirmed (Gap G-3). */
export async function ssfSheetCsv(db: Queryable, runId: string): Promise<string> {
  const r = await db.query<{ emp_code: string; name_en: string; ssf_id: string | null; basic: string; ee: string; er: string; alloc: Record<string, string> | null }>(
    `SELECT e.emp_code, e.name_en, e.ssf_id,
            coalesce((SELECT l.amount FROM payslip_line l WHERE l.payslip_id = ps.id AND l.component_code = 'BASIC'), 0)::text AS basic,
            ps.employee_statutory::text AS ee, ps.employer_statutory::text AS er, ps.inputs_snapshot->'result'->'ssfAllocation' AS alloc
       FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 AND e.regime = 'ssf' ORDER BY e.emp_code`, [runId]);
  const rows = [['emp_code', 'ssf_id', 'name', 'basic', 'employee_11pct', 'employer_20pct', 'medical_1pct', 'accident_1_40pct', 'dependent_0_27pct', 'old_age_28_33pct', 'total_31pct']];
  for (const x of r.rows) {
    rows.push([x.emp_code, x.ssf_id ?? '', x.name_en, D(x.basic).toFixed(2), D(x.ee).toFixed(2), D(x.er).toFixed(2), x.alloc?.medical ?? '', x.alloc?.accident ?? '', x.alloc?.dependent ?? '', x.alloc?.old_age ?? '', D(x.ee).plus(x.er).toFixed(2)]);
  }
  return csv(rows);
}
