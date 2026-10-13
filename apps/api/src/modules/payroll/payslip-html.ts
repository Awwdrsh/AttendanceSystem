import { amountInWordsEn, amountInWordsNe, formatNpr, toDevanagariDigits, D } from '@hajiri/payroll-engine';
import { BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE } from '@hajiri/bs-calendar';

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
}

const esc = (s: string | null | undefined) => (s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));

/** Bilingual printable payslip (plan §13.7). Employer contributions are shown apart from deductions. */
export function renderPayslipHtml(v: PayslipView): string {
  const n = (x: string) => formatNpr(x, v.numerals);
  const bsLabel = `${BS_MONTH_NAMES_NE[v.period.bsMonth - 1]} ${toDevanagariDigits(String(v.period.bsYear))} / ${BS_MONTH_NAMES_EN[v.period.bsMonth - 1]} ${v.period.bsYear}`;
  const block = (title: string, type: string) => {
    const rows = v.lines.filter((l) => l.type === type);
    return `<table class="blk"><thead><tr><th colspan="2">${title}</th></tr></thead><tbody>${rows.map((l) => `<tr><td>${esc(l.nameNe)}${l.nameNe ? ' / ' : ''}${esc(l.nameEn)}</td><td class="amt">${n(l.amount)}</td></tr>`).join('')}${rows.length ? '' : '<tr><td colspan="2" class="muted">—</td></tr>'}</tbody></table>`;
  };
  return `<!doctype html><html lang="ne"><head><meta charset="utf-8"><title>Payslip ${esc(v.employee.code)} ${esc(bsLabel)}</title>
<style>
body{font-family:"Noto Sans Devanagari","Mukta",Inter,system-ui,sans-serif;font-size:13px;color:#111;margin:24px;line-height:1.7}
h1{font-size:18px;margin:0}.hdr{display:flex;justify-content:space-between;border-bottom:2px solid #222;padding-bottom:8px}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 16px;margin:12px 0}.meta div span{color:#555;display:block;font-size:11px}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}table.blk{width:100%;border-collapse:collapse}table.blk th{text-align:left;background:#f1f1f1;padding:4px 6px}
table.blk td{padding:3px 6px;border-bottom:1px solid #eee}.amt{text-align:right;font-variant-numeric:tabular-nums}.muted{color:#888}
.net{margin-top:14px;padding:10px;border:2px solid #222;font-size:15px}.net b{font-size:20px}.words{font-size:12px;color:#333}
.foot{margin-top:16px;font-size:11px;color:#555;border-top:1px solid #ccc;padding-top:6px}.explain{margin-top:12px;font-size:11px}
@media print{body{margin:8mm}}
</style></head><body>
<div class="hdr"><div><h1>${esc(v.company.nameNe ?? '')}${v.company.nameNe ? ' / ' : ''}${esc(v.company.nameEn)}</h1><div>तलब विवरण / Payslip</div></div>
<div><div>अवधि / Period</div><b>${esc(bsLabel)}</b><div class="muted">${esc(v.period.startAd)} – ${esc(v.period.endAd)}</div></div></div>
<div class="meta">
<div><span>कर्मचारी / Employee</span>${esc(v.employee.nameNe ?? '')}${v.employee.nameNe ? ' / ' : ''}${esc(v.employee.nameEn)} (${esc(v.employee.code)})</div>
<div><span>पद / Designation</span>${esc(v.employee.designation) || '—'}</div><div><span>विभाग / Department</span>${esc(v.employee.department) || '—'}</div>
<div><span>PAN</span>${esc(v.employee.panMasked) || '—'}</div><div><span>SSF</span>${esc(v.employee.ssfMasked) || '—'}</div><div><span>बैंक / Bank</span>${esc(v.employee.bankMasked) || '—'}</div>
<div><span>भुक्तानी दिन / Paid days</span>${v.paidDays}</div><div><span>बेतलबी / LOP</span>${v.lopDays}</div><div><span>ओभरटाइम / OT hours</span>${v.otHours}</div>
</div>
<div class="cols">${block('आम्दानी / Earnings', 'earning')}${block('कट्टी / Deductions', 'deduction')}${block('रोजगारदाता योगदान / Employer contributions', 'employer_contribution')}</div>
<div class="cols" style="margin-top:6px"><div class="amt"><b>कुल / Gross: ${n(v.gross)}</b></div><div class="amt"><b>कुल कट्टी / Total deductions: ${n(v.totalDeductions)}</b></div><div class="amt"><b>${n(v.employerStatutory)}</b></div></div>
<div class="net">खुद तलब / Net pay: <b>रु ${n(v.net)}</b><div class="words">${esc(amountInWordsNe(v.net))}</div><div class="words">${esc(amountInWordsEn(v.net))}</div></div>
<div class="meta"><div><span>YTD gross</span>${n(v.ytd.gross)}</div><div><span>YTD TDS</span>${n(v.ytd.tds)}</div><div><span>YTD SSF/PF</span>${n(v.ytd.statutory)}</div></div>
<details class="explain"><summary>यो तलब किन? / Why is my salary this amount?</summary><ul>${v.explain.map((e) => `<li><b>${esc(e.code)}</b>: ${esc(e.text)}</li>`).join('')}</ul></details>
<div class="foot">Computer-generated payslip · verification ${esc(v.payslipId)} · Figures in NPR ${D(v.net).isNegative() ? '· negative net' : ''} · आफ्नो तलब विवरणमा प्रश्न भए मानव संसाधनलाई सम्पर्क गर्नुहोस् / Contact HR to dispute this payslip.</div>
</body></html>`;
}
