import { amountInWordsEn, formatNpr, D } from '@hajiri/payroll-engine';
import { bsLabel, bsMonthName } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { csvCell as cell } from '../reports/csv.js';

type LineType = 'earning' | 'deduction' | 'employer_contribution';
export interface SheetColumn { code: string; name: string }
export interface SheetRow {
  sn: number; employeeId: string; empCode: string; name: string; designation: string | null; department: string | null;
  paidDays: string; lopDays: string; otHours: string;
  earnings: Record<string, string>; gross: string;
  deductions: Record<string, string>; totalDeductions: string; rounding: string; net: string;
  employer: Record<string, string>; employerTotal: string; costToCompany: string;
  bank: string | null; bankAccount: string | null; flags: string[];
}
export interface SalarySheet {
  company: string;
  period: { label: string; bsYear: number; bsMonth: number; from: string; to: string; days: number };
  run: { id: string; type: string; status: string; final: boolean; preparedBy: string | null; approvedBy: string | null; approvedAt: string | null; lockedAt: string | null };
  columns: { earnings: SheetColumn[]; deductions: SheetColumn[]; employer: SheetColumn[]; rounding: boolean };
  rows: SheetRow[];
  totals: { paidDays: string; lopDays: string; otHours: string; earnings: Record<string, string>; gross: string; deductions: Record<string, string>; totalDeductions: string; rounding: string; net: string; employer: Record<string, string>; employerTotal: string; costToCompany: string };
}

const RUN_TYPE: Record<string, string> = { regular: 'Regular', supplementary: 'Supplementary', arrears: 'Arrears', off_cycle: 'Off-cycle', final_settlement: 'Final settlement', bonus: 'Bonus' };

/**
 * Salary sheet for one pay run: one row per employee with every earning, deduction and employer contribution in its own
 * column, plus totals. It is the HR record of what each person is owed; nothing here talks to a bank or an accounting system.
 */
export async function salarySheet(db: Queryable, companyId: string, runId: string): Promise<SalarySheet | null> {
  const head = await db.query<{ id: string; run_type: string; status: string; locked_at: Date | null; approved_at: Date | null; company: string; bs_year: number; bs_month: number; start_ad: string; end_ad: string; days: number; maker: string | null; checker: string | null }>(
    `SELECT r.id, r.run_type, r.status, r.locked_at, r.approved_at, c.name_en AS company, p.bs_year, p.bs_month, p.start_ad::text, p.end_ad::text, p.days,
            mk.name AS maker, ck.name AS checker
       FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id JOIN company c ON c.id = r.company_id
       LEFT JOIN app_user mk ON mk.id = r.maker_id LEFT JOIN app_user ck ON ck.id = r.checker_id
      WHERE r.id = $1 AND r.company_id = $2`, [runId, companyId]);
  const h = head.rows[0];
  if (!h) return null;
  const slips = await db.query<{ id: string; employee_id: string; emp_code: string; name_en: string; designation: string | null; department: string | null; paid_days: string; lop_days: string; ot_hours: string; gross: string; total_deductions: string; net: string; employer_statutory: string; bank_code: string | null; bank_branch: string | null; bank_account_last4: string | null; flags: string[] }>(
    `SELECT ps.id, ps.employee_id, e.emp_code, e.name_en, e.designation,
            (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> $2::date LIMIT 1) AS department,
            ps.paid_days::text, ps.lop_days::text, ps.ot_hours::text, ps.gross::text, ps.total_deductions::text, ps.net::text, ps.employer_statutory::text,
            e.bank_code, e.bank_branch, e.bank_account_last4,
            -- Pay held in this run (see payroll_hold) is marked so the sheet shows who is not paid this month.
            CASE WHEN EXISTS (SELECT 1 FROM payroll_hold h WHERE h.run_id = ps.run_id AND h.employee_id = ps.employee_id AND h.status <> 'cancelled')
                 THEN ps.flags || '["PayHeld"]'::jsonb ELSE ps.flags END AS flags
       FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 ORDER BY e.emp_code`, [runId, h.end_ad]);
  const lines = await db.query<{ payslip_id: string; component_code: string; type: LineType; name_en: string; amount: string; sort_order: number }>(
    `SELECT l.payslip_id, l.component_code, l.type, l.name_en, l.amount::text, l.sort_order FROM payslip_line l JOIN payslip ps ON ps.id = l.payslip_id
      WHERE ps.run_id = $1 AND l.type <> 'statistical' ORDER BY l.sort_order, l.component_code`, [runId]);

  const cols: Record<LineType, Map<string, { name: string; order: number }>> = { earning: new Map(), deduction: new Map(), employer_contribution: new Map() };
  const byslip = new Map<string, Record<LineType, Record<string, string>>>();
  // Gross and total deductions exclude the whole-rupee rounding line, so it gets its own signed column: gross − deductions ± rounding = net.
  const rounding = new Map<string, string>();
  for (const l of lines.rows) {
    if (l.component_code === 'ROUNDING') { rounding.set(l.payslip_id, (l.type === 'deduction' ? D(l.amount).neg() : D(l.amount)).toFixed(2)); continue; }
    const seen = cols[l.type].get(l.component_code);
    if (!seen || l.sort_order < seen.order) cols[l.type].set(l.component_code, { name: l.name_en, order: l.sort_order });
    const b = byslip.get(l.payslip_id) ?? { earning: {}, deduction: {}, employer_contribution: {} };
    b[l.type][l.component_code] = D(b[l.type][l.component_code] ?? 0).plus(l.amount).toFixed(2);
    byslip.set(l.payslip_id, b);
  }
  const list = (t: LineType): SheetColumn[] => [...cols[t]].sort((a, b) => a[1].order - b[1].order || a[0].localeCompare(b[0])).map(([code, v]) => ({ code, name: v.name }));
  const columns = { earnings: list('earning'), deductions: list('deduction'), employer: list('employer_contribution'), rounding: rounding.size > 0 };

  const sumOf = (rec: Record<string, string>) => Object.values(rec).reduce((a, x) => a.plus(x), D(0));
  const rows: SheetRow[] = slips.rows.map((s, i) => {
    const b = byslip.get(s.id) ?? { earning: {}, deduction: {}, employer_contribution: {} };
    const employerTotal = Object.keys(b.employer_contribution).length ? sumOf(b.employer_contribution) : D(s.employer_statutory);
    return {
      sn: i + 1, employeeId: s.employee_id, empCode: s.emp_code, name: s.name_en, designation: s.designation, department: s.department,
      paidDays: D(s.paid_days).toString(), lopDays: D(s.lop_days).toString(), otHours: D(s.ot_hours).toString(),
      earnings: b.earning, gross: D(s.gross).toFixed(2), deductions: b.deduction, totalDeductions: D(s.total_deductions).toFixed(2), rounding: rounding.get(s.id) ?? '0.00', net: D(s.net).toFixed(2),
      employer: b.employer_contribution, employerTotal: employerTotal.toFixed(2), costToCompany: D(s.gross).plus(employerTotal).toFixed(2),
      bank: [s.bank_code, s.bank_branch].filter(Boolean).join(', ') || null, bankAccount: s.bank_account_last4 ? `****${s.bank_account_last4}` : null,
      flags: Array.isArray(s.flags) ? s.flags : [],
    };
  });
  const total = (pick: (r: SheetRow) => string | undefined) => rows.reduce((a, r) => a.plus(pick(r) ?? 0), D(0));
  const totalsBy = (cs: SheetColumn[], k: 'earnings' | 'deductions' | 'employer') => Object.fromEntries(cs.map((c) => [c.code, total((r) => r[k][c.code]).toFixed(2)]));
  return {
    company: h.company,
    period: { label: bsMonthName(h.bs_year, h.bs_month), bsYear: h.bs_year, bsMonth: h.bs_month, from: h.start_ad, to: h.end_ad, days: h.days },
    run: { id: h.id, type: h.run_type, status: h.status, final: !!h.locked_at, preparedBy: h.maker, approvedBy: h.checker,
      approvedAt: h.approved_at?.toISOString() ?? null, lockedAt: h.locked_at?.toISOString() ?? null },
    columns, rows,
    totals: {
      paidDays: total((r) => r.paidDays).toString(), lopDays: total((r) => r.lopDays).toString(), otHours: total((r) => r.otHours).toString(),
      earnings: totalsBy(columns.earnings, 'earnings'), gross: total((r) => r.gross).toFixed(2),
      deductions: totalsBy(columns.deductions, 'deductions'), totalDeductions: total((r) => r.totalDeductions).toFixed(2), rounding: total((r) => r.rounding).toFixed(2), net: total((r) => r.net).toFixed(2),
      employer: totalsBy(columns.employer, 'employer'), employerTotal: total((r) => r.employerTotal).toFixed(2), costToCompany: total((r) => r.costToCompany).toFixed(2),
    },
  };
}

/** CSV with a byte-order mark so Excel opens it as UTF-8; one header row, one row per employee, a totals row. */
export function salarySheetCsv(s: SalarySheet): string {
  const { earnings, deductions, employer } = s.columns;
  const header = ['S.N.', 'Employee code', 'Name', 'Designation', 'Department', 'Paid days', 'Unpaid days', 'OT hours',
    ...earnings.map((c) => c.name), 'Gross pay', ...deductions.map((c) => c.name), 'Total deductions', ...(s.columns.rounding ? ['Rounding'] : []), 'Net pay',
    ...employer.map((c) => `${c.name} (employer)`), 'Employer contributions', 'Cost to company', 'Bank', 'Account'];
  const body = s.rows.map((r) => [String(r.sn), r.empCode, r.name, r.designation ?? '', r.department ?? '', r.paidDays, r.lopDays, r.otHours,
    ...earnings.map((c) => r.earnings[c.code] ?? '0.00'), r.gross, ...deductions.map((c) => r.deductions[c.code] ?? '0.00'), r.totalDeductions, ...(s.columns.rounding ? [r.rounding] : []), r.net,
    ...employer.map((c) => r.employer[c.code] ?? '0.00'), r.employerTotal, r.costToCompany, r.bank ?? '', r.bankAccount ?? '']);
  const t = s.totals;
  const foot = ['', '', `Total (${s.rows.length})`, '', '', t.paidDays, t.lopDays, t.otHours, ...earnings.map((c) => t.earnings[c.code]!), t.gross,
    ...deductions.map((c) => t.deductions[c.code]!), t.totalDeductions, ...(s.columns.rounding ? [t.rounding] : []), t.net, ...employer.map((c) => t.employer[c.code]!), t.employerTotal, t.costToCompany, '', ''];
  const title = [`${s.company} salary sheet, ${s.period.label} (${bsLabel(s.period.from)} to ${bsLabel(s.period.to)})${s.run.final ? '' : ', DRAFT'}`];
  return '﻿' + [title, header, ...body, foot].map((r) => r.map(cell).join(',')).join('\r\n') + '\r\n';
}

const esc = (s: string | null | undefined) => (s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));

/** Printable salary sheet (A4 landscape) with sign-off lines for the people who prepare, check and approve it. */
export function salarySheetHtml(s: SalarySheet): string {
  const n = (x: string | undefined) => formatNpr(x ?? '0', 'latin');
  const { earnings, deductions } = s.columns;
  const rnd = (x: string) => (s.columns.rounding ? `<td class="amt">${D(x).isZero() ? '' : `${D(x).gt(0) ? '+' : '−'}${n(D(x).abs().toFixed(2))}`}</td>` : '');
  const th = (label: string, cls = 'amt') => `<th class="${cls}">${esc(label)}</th>`;
  const head = `<tr>${th('S.N.', 'c')}${th('Employee', '')}${th('Paid days')}${th('Unpaid')}${earnings.map((c) => th(c.name)).join('')}${th('Gross pay', 'amt strong')}${deductions.map((c) => th(c.name)).join('')}${th('Total deductions', 'amt strong')}${s.columns.rounding ? th('Rounding') : ''}${th('Net pay', 'amt net')}${th('Employer contributions')}${th('Bank', '')}${th('Signature', 'sig')}</tr>`;
  const body = s.rows.map((r) => `<tr><td class="c">${r.sn}</td><td><b>${esc(r.name)}</b><div class="muted">${esc(r.empCode)}${r.designation ? ` · ${esc(r.designation)}` : ''}</div></td><td class="amt">${r.paidDays}</td><td class="amt">${r.lopDays}</td>${earnings.map((c) => `<td class="amt">${n(r.earnings[c.code])}</td>`).join('')}<td class="amt strong">${n(r.gross)}</td>${deductions.map((c) => `<td class="amt">${n(r.deductions[c.code])}</td>`).join('')}<td class="amt strong">${n(r.totalDeductions)}</td>${rnd(r.rounding)}<td class="amt net">${n(r.net)}${r.flags.includes('PayHeld') ? '<div class="muted">On hold</div>' : ''}</td><td class="amt">${n(r.employerTotal)}</td><td>${esc(r.bank) || '—'}${r.bankAccount ? `<div class="muted">${esc(r.bankAccount)}</div>` : ''}</td><td class="sig"></td></tr>`).join('');
  const t = s.totals;
  const foot = `<tr><td></td><td><b>Total (${s.rows.length})</b></td><td class="amt">${t.paidDays}</td><td class="amt">${t.lopDays}</td>${earnings.map((c) => `<td class="amt">${n(t.earnings[c.code])}</td>`).join('')}<td class="amt strong">${n(t.gross)}</td>${deductions.map((c) => `<td class="amt">${n(t.deductions[c.code])}</td>`).join('')}<td class="amt strong">${n(t.totalDeductions)}</td>${rnd(t.rounding)}<td class="amt net">${n(t.net)}</td><td class="amt">${n(t.employerTotal)}</td><td></td><td></td></tr>`;
  const kind = RUN_TYPE[s.run.type] ?? s.run.type;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Salary sheet ${esc(s.period.label)}</title>
<style>
@page{size:A4 landscape;margin:10mm}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:11px;color:#161c26;margin:24px;line-height:1.4}
h1{font-size:18px;margin:0;font-weight:600;letter-spacing:-.01em}.muted{color:#6b707b;font-size:10px}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:12px;border-bottom:2px solid #12773a;margin-bottom:14px}
.right{text-align:right}.right b{font-size:15px}
.draft{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:99px;background:#fff4d6;color:#8a5a00;font-size:10px;font-weight:600;letter-spacing:.04em}
.sum{display:flex;gap:28px;margin:0 0 14px}.sum div span{display:block;color:#6b707b;font-size:10px}.sum div b{font-size:14px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}th{font-weight:600;color:#3b4250;font-size:10px;text-align:left;padding:6px 5px;border-bottom:1px solid #c9ccd2;background:#f4f6f5;vertical-align:bottom}
td{padding:5px;border-bottom:1px solid #eceef0;vertical-align:top}.amt{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.c{text-align:center}
.strong{font-weight:600}.net{font-weight:700;color:#12773a}.sig{width:90px}tfoot td{border-top:2px solid #161c26;border-bottom:none;font-weight:600;padding-top:7px}
.words{margin-top:10px;font-size:11px}.signs{display:grid;grid-template-columns:repeat(3,1fr);gap:48px;margin-top:48px}
.signs div{border-top:1px solid #161c26;padding-top:6px}.signs span{display:block;color:#6b707b;font-size:10px}
.foot{margin-top:18px;font-size:9.5px;color:#6b707b}
@media print{body{margin:0}thead{display:table-header-group}tr{page-break-inside:avoid}}
</style></head><body>
<div class="hdr"><div><h1>${esc(s.company)}</h1><div class="muted">Salary sheet · ${esc(kind)} run</div></div>
<div class="right"><div class="muted">Pay period</div><b>${esc(s.period.label)}</b>${s.run.final ? '' : '<span class="draft">DRAFT</span>'}<div class="muted">${esc(bsLabel(s.period.from))} to ${esc(bsLabel(s.period.to))} · ${s.period.days} days</div></div></div>
<div class="sum"><div><span>Employees</span><b>${s.rows.length}</b></div><div><span>Gross pay</span><b>NPR ${n(t.gross)}</b></div><div><span>Deductions</span><b>NPR ${n(t.totalDeductions)}</b></div><div><span>Net pay</span><b>NPR ${n(t.net)}</b></div><div><span>Employer contributions</span><b>NPR ${n(t.employerTotal)}</b></div><div><span>Cost to company</span><b>NPR ${n(t.costToCompany)}</b></div></div>
<table><thead>${head}</thead><tbody>${body || `<tr><td colspan="99" class="muted">No payslips in this run yet. Calculate the run first.</td></tr>`}</tbody><tfoot>${foot}</tfoot></table>
<div class="words">Total net pay in words: <b>${esc(amountInWordsEn(t.net))}</b></div>
<div class="signs"><div>Prepared by<span>${esc(s.run.preparedBy) || '&nbsp;'}</span></div><div>Checked by<span>&nbsp;</span></div><div>Approved by<span>${esc(s.run.approvedBy) || '&nbsp;'}</span></div></div>
<div class="foot">Generated by Hajiri. Amounts in NPR. ${s.run.final ? `Run locked ${esc(bsLabel(new Date(Date.parse(s.run.lockedAt!) + 345 * 60_000)))}.` : 'Draft: figures can still change until the run is locked.'} Run ${esc(s.run.id)}.</div>
</body></html>`;
}
