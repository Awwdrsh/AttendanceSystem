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

/** ETDS annex: PAN, taxable this month, TDS withheld (plan §6.6; official IRD annex layout to be obtained, Gap G-3). PAN is decrypted by the caller. */
export async function etdsRows(db: Queryable, runId: string): Promise<Array<{ employee_id: string; emp_code: string; name_en: string; taxable: string; tds: string; pan_enc: Buffer | null }>> {
  const r = await db.query<{ employee_id: string; emp_code: string; name_en: string; taxable: string; tds: string; pan_enc: Buffer | null }>(
    `SELECT e.id AS employee_id, e.emp_code, e.name_en, ps.taxable::text, ps.tds::text, e.pan_enc FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 ORDER BY e.emp_code`, [runId]);
  return r.rows;
}
export const etdsCsv = (rows: Array<{ emp_code: string; name_en: string; pan: string; taxable: string; tds: string }>) =>
  csv([['emp_code', 'pan', 'name', 'taxable_remuneration', 'tds'], ...rows.map((x) => [x.emp_code, x.pan, x.name_en, D(x.taxable).toFixed(2), D(x.tds).toFixed(2)])]);

const DEFAULT_GL: Record<string, string> = {
  earning: '5100 Salaries and wages', employer_contribution: '5150 Employer social security', SSF_EE: '2310 SSF payable', PF_EE: '2320 PF payable', CIT: '2330 CIT payable',
  TDS: '2300 TDS payable', LOAN: '1400 Staff loans', ADVANCE: '1410 Staff advances', INSURANCE_LIFE: '2340 Insurance payable', INSURANCE_HEALTH: '2340 Insurance payable',
  SSF_ER: '2310 SSF payable', PF_ER: '2320 PF payable', GRATUITY: '2350 Gratuity provision', NET: '2200 Salaries payable', deduction: '2390 Other payroll deductions',
};

/** Journal export: expense debits for earnings and employer contributions, liability credits for deductions, employer contributions and net pay (plan §11.10). */
export async function journal(db: Queryable, companyId: string, runId: string): Promise<Array<{ account: string; debit: string; credit: string; memo: string }>> {
  const map = { ...DEFAULT_GL, ...((await db.query<{ value: Record<string, string> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payroll.gl_map'`, [companyId])).rows[0]?.value ?? {}) };
  const lines = await db.query<{ component_code: string; type: string; total: string }>(
    `SELECT l.component_code, l.type, sum(l.amount)::text AS total FROM payslip_line l JOIN payslip ps ON ps.id = l.payslip_id WHERE ps.run_id = $1 AND l.type <> 'statistical' GROUP BY 1, 2 ORDER BY 2, 1`, [runId]);
  const net = await db.query<{ total: string }>('SELECT coalesce(sum(net), 0)::text AS total FROM payslip WHERE run_id = $1', [runId]);
  const out: Array<{ account: string; debit: string; credit: string; memo: string }> = [];
  for (const l of lines.rows) {
    const acct = map[l.component_code] ?? map[l.type] ?? 'Unmapped';
    if (l.type === 'earning') out.push({ account: acct, debit: D(l.total).toFixed(2), credit: '0.00', memo: l.component_code });
    else if (l.type === 'employer_contribution') { out.push({ account: map.employer_contribution!, debit: D(l.total).toFixed(2), credit: '0.00', memo: l.component_code }); out.push({ account: acct, debit: '0.00', credit: D(l.total).toFixed(2), memo: l.component_code }); }
    else out.push({ account: acct, debit: '0.00', credit: D(l.total).toFixed(2), memo: l.component_code });
  }
  out.push({ account: map.NET!, debit: '0.00', credit: D(net.rows[0]!.total).toFixed(2), memo: 'Net salaries payable' });
  return out;
}
export const journalCsv = (rows: Array<{ account: string; debit: string; credit: string; memo: string }>) => csv([['account', 'debit', 'credit', 'memo'], ...rows.map((r) => [r.account, r.debit, r.credit, r.memo])]);
