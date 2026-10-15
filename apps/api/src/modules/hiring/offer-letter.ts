import { amountInWordsEn, formatNpr } from '@hajiri/payroll-engine';
import { bsLabel } from '../../bsdate.js';

export interface OfferLetterView {
  company: string;
  candidate: string;
  designation: string;
  department: string | null;
  location: string | null;
  employmentType: string;
  salary: string;
  joinDate: string;
  expiresOn: string | null;
  issuedOn: string;
  notes: string | null;
  reference: string;
  draft: boolean;
}

const esc = (s: string | null | undefined) => (s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

const TYPE: Record<string, string> = { full_time: 'full-time', part_time: 'part-time', contract: 'contract', internship: 'internship', temporary: 'temporary' };

/** Printable A4 offer letter in the CoreValley style; marked DRAFT until the offer is approved. */
export function renderOfferLetter(v: OfferLetterView): string {
  const paras = (v.notes ?? '').split(/\n{2,}/).map((p) => p.trim()).filter(Boolean).map((p) => `<p>${esc(p).replace(/\n/g, '<br>')}</p>`).join('');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offer letter: ${esc(v.candidate)}</title>
<style>
@page{size:A4;margin:18mm}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13.5px;color:#17191e;margin:32px auto;max-width:760px;line-height:1.65;padding:0 24px}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:14px;border-bottom:2px solid #12773a}
h1{font-size:20px;margin:0;font-weight:700;letter-spacing:-.01em}.muted{color:#6b707b}.ref{text-align:right;font-size:12px}
h2{font-size:16px;margin:28px 0 12px}table{border-collapse:collapse;margin:12px 0 18px;width:100%}
td{padding:7px 0;border-bottom:1px solid #eef0f2;vertical-align:top}td:first-child{color:#6b707b;width:38%}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:48px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;font-size:12px;color:#6b707b}
.draft{position:fixed;top:40%;left:0;right:0;text-align:center;font-size:96px;color:rgba(200,30,30,.08);transform:rotate(-24deg);font-weight:800;pointer-events:none}
@media print{body{margin:0 auto}}
</style></head><body>
${v.draft ? '<div class="draft">DRAFT</div>' : ''}
<div class="hdr"><div><h1>${esc(v.company)}</h1><div class="muted">Offer of employment</div></div>
<div class="ref"><div>Ref. ${esc(v.reference)}</div><div>${esc(bsLabel(v.issuedOn))}</div></div></div>
<p style="margin-top:24px">Dear ${esc(v.candidate)},</p>
<p>We are pleased to offer you the ${esc(TYPE[v.employmentType] ?? 'full-time')} position of <b>${esc(v.designation)}</b>${v.department ? ` in ${esc(v.department)}` : ''} at ${esc(v.company)}. The main terms of this offer are below.</p>
<table>
<tr><td>Position</td><td>${esc(v.designation)}</td></tr>
${v.department ? `<tr><td>Department</td><td>${esc(v.department)}</td></tr>` : ''}
${v.location ? `<tr><td>Work location</td><td>${esc(v.location)}</td></tr>` : ''}
<tr><td>Monthly gross salary</td><td>NPR ${esc(formatNpr(v.salary, 'latin'))}<div class="muted" style="font-size:12px">${esc(amountInWordsEn(v.salary))}</div></td></tr>
<tr><td>Joining date</td><td>${esc(bsLabel(v.joinDate))}</td></tr>
${v.expiresOn ? `<tr><td>Please accept by</td><td>${esc(bsLabel(v.expiresOn))}</td></tr>` : ''}
</table>
${paras}
<p>Your employment will be governed by the Labour Act 2074, the company's policies and the appointment letter you will receive on joining. Salary is subject to tax and Social Security Fund deductions as required by law.</p>
<p>Please sign and return a copy of this letter to confirm that you accept the offer. We look forward to welcoming you.</p>
<div class="sign"><div>For ${esc(v.company)}<br>Authorised signatory</div><div>Accepted by ${esc(v.candidate)}<br>Signature and date</div></div>
</body></html>`;
}
