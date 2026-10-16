import { amountInWordsEn, formatNpr, D } from '@hajiri/payroll-engine';
import { BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';
import { bsLabel } from '../../bsdate.js';

export interface PayslipView {
  company: { nameEn: string; nameNe: string | null };
  employee: { code: string; nameEn: string; nameNe: string | null; designation: string | null; department: string | null; panMasked: string | null; ssfMasked: string | null; bankMasked: string | null };
  period: { bsYear: number; bsMonth: number; startAd: string; endAd: string };
  paidDays: string; lopDays: string; otHours: string;
  lines: Array<{ code: string; type: string; nameEn: string; nameNe: string | null; amount: string }>;
  gross: string; totalDeductions: string; employerStatutory: string; net: string;
  ytd: { gross: string; tds: string; statutory: string };
  explain: Array<{ code: string; text: string }>;
  numerals: 'latin' | 'devanagari';
  payslipId: string;
  /** HR has held this net pay; it is paid later. */
  held?: boolean;
}

const esc = (s: string | null | undefined) => (s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));

/** Printable payslip in English. Employer contributions are shown apart from deductions. */
export function renderPayslipHtml(v: PayslipView): string {
  const n = (x: string) => formatNpr(x, 'latin');
  const period = `${BS_MONTH_NAMES_EN[v.period.bsMonth - 1]} ${v.period.bsYear}`;
  const block = (title: string, type: string) => {
    const rows = v.lines.filter((l) => l.type === type);
    return `<table class="blk"><thead><tr><th>${title}</th><th class="amt">NPR</th></tr></thead><tbody>${rows.map((l) => `<tr><td>${esc(l.nameEn)}</td><td class="amt">${n(l.amount)}</td></tr>`).join('')}${rows.length ? '' : '<tr><td colspan="2" class="muted">None</td></tr>'}</tbody></table>`;
  };
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Payslip ${esc(v.employee.code)} ${esc(period)}</title>
<style>
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px;color:#17191e;margin:32px auto;max-width:860px;line-height:1.55;padding:0 24px}
h1{font-size:20px;margin:0;font-weight:600;letter-spacing:-.01em}.sub{color:#6b707b}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:16px;border-bottom:1px solid #e6e7ea}
.period{text-align:right}.period b{font-size:16px;font-weight:600}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:12px 24px;margin:20px 0}.meta div span{color:#6b707b;display:block;font-size:11.5px}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}table.blk{width:100%;border-collapse:collapse}
table.blk th{text-align:left;font-weight:500;color:#6b707b;font-size:12px;padding:6px 0;border-bottom:1px solid #e6e7ea}
table.blk td{padding:6px 0;border-bottom:1px solid #f0f0f2}.amt{text-align:right;font-variant-numeric:tabular-nums}.muted{color:#9aa0aa}
.totals{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:8px;font-weight:600}
.net{margin-top:24px;padding:18px 20px;border:1px solid #e6e7ea;border-radius:12px;display:flex;justify-content:space-between;align-items:center}
.net .label{color:#6b707b}.net b{font-size:24px;font-weight:600;letter-spacing:-.02em;color:#12773a;font-variant-numeric:tabular-nums}.words{font-size:12px;color:#6b707b;margin-top:2px}
.held{margin-top:20px;padding:10px 14px;border-radius:10px;background:#fff4e5;color:#8a4b00;font-weight:500}
.foot{margin-top:24px;font-size:11px;color:#6b707b;border-top:1px solid #e6e7ea;padding-top:10px}.explain{margin-top:16px;font-size:12px}
@media print{body{margin:10mm auto}.net{border-color:#bbb}}
</style></head><body>
<div class="hdr"><div><h1>${esc(v.company.nameEn)}</h1><div class="sub">Payslip</div></div>
<div class="period"><div class="sub">Pay period</div><b>${esc(period)}</b><div class="muted">${esc(bsLabel(v.period.startAd))} to ${esc(bsLabel(v.period.endAd))}</div></div></div>
<div class="meta">
<div><span>Employee</span>${esc(v.employee.nameEn)} (${esc(v.employee.code)})</div>
<div><span>Designation</span>${esc(v.employee.designation) || '—'}</div><div><span>Department</span>${esc(v.employee.department) || '—'}</div>
<div><span>PAN</span>${esc(v.employee.panMasked) || '—'}</div><div><span>SSF</span>${esc(v.employee.ssfMasked) || '—'}</div><div><span>Bank account</span>${esc(v.employee.bankMasked) || '—'}</div>
<div><span>Paid days</span>${v.paidDays}</div><div><span>Unpaid days</span>${v.lopDays}</div><div><span>Overtime hours</span>${v.otHours}</div>
</div>
<div class="cols">${block('Earnings', 'earning')}${block('Deductions', 'deduction')}${block('Employer contributions', 'employer_contribution')}</div>
<div class="totals"><div class="amt">Gross ${n(v.gross)}</div><div class="amt">Total deductions ${n(v.totalDeductions)}</div><div class="amt">${n(v.employerStatutory)}</div></div>
${v.held ? '<div class="held">Payment on hold: this net pay has not been paid yet. HR will release it in a later month.</div>' : ''}
<div class="net"><div><div class="label">Net pay</div><div class="words">${esc(amountInWordsEn(v.net))}</div></div><b>NPR ${n(v.net)}</b></div>
<div class="meta"><div><span>Year-to-date gross</span>${n(v.ytd.gross)}</div><div><span>Year-to-date tax (TDS)</span>${n(v.ytd.tds)}</div><div><span>Year-to-date SSF/PF</span>${n(v.ytd.statutory)}</div></div>
<details class="explain"><summary>How was this amount calculated?</summary><ul>${v.explain.map((e) => `<li><b>${esc(e.code)}</b>: ${esc(e.text)}</li>`).join('')}</ul></details>
<div class="foot">Computer-generated payslip. Verification code ${esc(v.payslipId)}. Figures in NPR.${D(v.net).isNegative() ? ' Net pay is negative.' : ''} Contact HR if anything on this payslip looks wrong.</div>
</body></html>`;
}
