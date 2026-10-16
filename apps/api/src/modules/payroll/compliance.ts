import { readFileSync } from 'node:fs';
import { Router, type Response } from 'express';
import { z } from 'zod';
import { amountInWordsEn, D, Decimal, formatNpr, optimumCit, type OptimumCit } from '@hajiri/payroll-engine';
import { adToBs, BS_MONTH_NAMES_EN, bsMonthBounds, fiscalYear, fiscalYearOf } from '@hajiri/bs-calendar';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { isoDate, money } from '../../validation.js';
import { companyIdOf, hasModule, requireModule } from '../auth/middleware.js';
import { parseCsv } from '../employees/routes.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { sendReport } from '../reports/csv.js';
import { nptToday } from '../workflow/service.js';
import { effectiveCit } from './compliance-service.js';
import { currentFiscalYearStart, estimateInput } from './tax-savings.js';
import { getRun } from './service.js';

/**
 * Payroll compliance extras for HR (who runs payroll alone; there is no bank sheet or accounting export):
 * opening balances, optimum CIT, held pay, tax and salary certificates, annual tax summary and the monthly statutory
 * reports (tax deposit, retirement fund deposit, pay variance) plus the leave liability estimate.
 */
const amount = money.refine((v) => !v.startsWith('-'), 'must not be negative');
const fyParam = z.coerce.number().int().min(2070).max(2200);
const hr = requireModule('payroll');
const fyLabel = (fy: number) => `${fy}/${String(fy + 1).slice(-2)}`;
const npr = (x: string | number | Decimal) => formatNpr(D(x).toFixed(2), 'latin');
const esc = (s: unknown) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));
/** Fiscal-year month index (Shrawan = 1) of a BS month. */
const fyMonth = (bsMonth: number) => ((bsMonth - 4 + 12) % 12) + 1;

async function assertEmployee(db: Queryable, companyId: string, employeeId: string): Promise<{ id: string; emp_code: string; name_en: string }> {
  const r = await db.query<{ id: string; emp_code: string; name_en: string }>('SELECT id, emp_code, name_en FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

// ---------------------------------------------------------------- annual tax summary (certificates and report)

export interface MonthFigures { bsYear: number; bsMonth: number; gross: string; taxable: string; tds: string; sst: string; ssfEe: string; ssfEr: string; pfEe: string; pfEr: string; gratuity: string; cit: string }
export interface AnnualSummary {
  employee: { id: string; code: string; name: string; designation: string | null; department: string | null; joinDate: string | null; exitDate: string | null; pan: string | null; ssfId: string | null; pfId: string | null; citId: string | null };
  fiscalYearStart: number;
  months: MonthFigures[];
  incomeHeads: Array<{ code: string; name: string; amount: string; taxable: boolean }>;
  opening: { gross: string; taxable: string; tds: string; retirementEmployee: string; retirementEmployer: string; source: string } | null;
  totals: { gross: string; taxable: string; tds: string; sst: string; retirementEmployee: string; retirementEmployer: string; cit: string };
  tax: { complete: boolean; asOfMonth: string | null; projectedAnnualTaxable: string; retirementDeduction: string; insuranceDeduction: string; remoteAreaDeduction: string; disabilityDeduction: string;
    donationDeduction: string; taxableAfterDeductions: string; grossTax: string; femaleRebate: string; medicalCredit: string; annualTax: string } | null;
  balance: string | null;
}

type TaxSnap = Record<string, string | number | Array<Record<string, unknown>>> & { bands?: Array<{ rate: string; tax: string; waived: boolean; upTo: string | null }>; annualTax?: string };

/** Share of a month's TDS that is social security tax (the 1% band), from that payslip's own tax computation. */
function sstShare(tds: string, tax: TaxSnap | null | undefined): Decimal {
  if (!tax?.bands || !tax.annualTax || D(tax.annualTax).isZero()) return D(0);
  const first = tax.bands[0];
  if (!first || first.waived || first.rate !== '0.01') return D(0);
  return D(tds).times(D(first.tax)).div(D(tax.annualTax)).toDecimalPlaces(2);
}

export async function annualSummaries(db: Queryable, cipher: AppDeps['cipher'], companyId: string, fiscalYearStart: number, employeeIds: string[] | null, opts: { revealIds?: boolean } = {}): Promise<AnnualSummary[]> {
  const fy = fiscalYear(fiscalYearStart);
  const emps = await db.query<{ id: string; emp_code: string; name_en: string; designation: string | null; department: string | null; join_date: string | null; exit_date: string | null; pan_enc: Buffer | null; ssf_id: string | null; pf_id: string | null; cit_id: string | null }>(
    `SELECT e.id, e.emp_code, e.name_en, e.designation, e.join_date::text, e.exit_date::text, e.pan_enc, e.ssf_id, e.pf_id, e.cit_id,
            (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id ORDER BY lower(a.valid) DESC LIMIT 1) AS department
       FROM employee e
      WHERE e.company_id = $1 AND ($2::uuid[] IS NULL OR e.id = ANY($2))
        AND (EXISTS (SELECT 1 FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                      WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND p.start_ad BETWEEN $3 AND $4)
             OR EXISTS (SELECT 1 FROM payroll_opening_balance o WHERE o.employee_id = e.id AND o.fiscal_year_start = $5)
             OR $2::uuid[] IS NOT NULL)
      ORDER BY e.emp_code`, [companyId, employeeIds, fy.startAd, fy.endAd, fiscalYearStart]);
  if (!emps.rows.length) return [];
  const ids = emps.rows.map((e) => e.id);
  const slips = await db.query<{ id: string; employee_id: string; bs_year: number; bs_month: number; gross: string; taxable: string; tds: string; tax: TaxSnap | null; start_ad: string }>(
    `SELECT ps.id, ps.employee_id, p.bs_year, p.bs_month, ps.gross::text, ps.taxable::text, ps.tds::text, ps.inputs_snapshot->'result'->'tax' AS tax, p.start_ad::text
       FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
      WHERE ps.employee_id = ANY($1) AND r.company_id = $2 AND r.locked_at IS NOT NULL AND p.start_ad BETWEEN $3 AND $4
      ORDER BY p.start_ad, r.locked_at`, [ids, companyId, fy.startAd, fy.endAd]);
  const lines = await db.query<{ payslip_id: string; component_code: string; type: string; name_en: string; amount: string; sort_order: number }>(
    `SELECT l.payslip_id, l.component_code, l.type, l.name_en, l.amount::text, l.sort_order FROM payslip_line l WHERE l.payslip_id = ANY($1) AND l.type IN ('earning', 'deduction', 'employer_contribution')`,
    [slips.rows.map((s) => s.id)]);
  const comps = await db.query<{ code: string; taxable: boolean }>('SELECT code, taxable FROM salary_component WHERE company_id = $1', [companyId]);
  const taxableCode = new Map(comps.rows.map((c) => [c.code, c.taxable]));
  const byslip = new Map<string, typeof lines.rows>();
  for (const l of lines.rows) byslip.set(l.payslip_id, [...(byslip.get(l.payslip_id) ?? []), l]);
  const openings = await db.query<{ employee_id: string; gross: string; taxable: string; tds_paid: string; retirement_employee: string; retirement_employer: string; source: string }>(
    `SELECT employee_id, gross::text, taxable::text, tds_paid::text, retirement_employee::text, retirement_employer::text, source FROM payroll_opening_balance WHERE employee_id = ANY($1) AND fiscal_year_start = $2`, [ids, fiscalYearStart]);
  const opening = new Map(openings.rows.map((o) => [o.employee_id, o]));
  const lastPeriod = fy.endAd;

  return emps.rows.map((e) => {
    const mine = slips.rows.filter((s) => s.employee_id === e.id);
    const months = new Map<string, MonthFigures>();
    const heads = new Map<string, { code: string; name: string; amount: Decimal; taxable: boolean; order: number }>();
    const z = '0.00';
    for (const s of mine) {
      const key = `${s.bs_year}-${s.bs_month}`;
      const m = months.get(key) ?? { bsYear: s.bs_year, bsMonth: s.bs_month, gross: z, taxable: z, tds: z, sst: z, ssfEe: z, ssfEr: z, pfEe: z, pfEr: z, gratuity: z, cit: z };
      const add = (k: keyof Omit<MonthFigures, 'bsYear' | 'bsMonth'>, v: Decimal | string) => { m[k] = D(m[k]).plus(v).toFixed(2); };
      add('gross', s.gross); add('taxable', s.taxable); add('tds', s.tds); add('sst', sstShare(s.tds, s.tax));
      for (const l of byslip.get(s.id) ?? []) {
        const map: Record<string, keyof MonthFigures> = { SSF_EE: 'ssfEe', SSF_ER: 'ssfEr', PF_EE: 'pfEe', PF_ER: 'pfEr', GRATUITY: 'gratuity', CIT: 'cit' };
        const k = map[l.component_code];
        if (k) add(k as 'ssfEe', l.amount);
        if (l.type === 'earning' && l.component_code !== 'ROUNDING') {
          const h = heads.get(l.component_code) ?? { code: l.component_code, name: l.name_en, amount: D(0), taxable: taxableCode.get(l.component_code) ?? true, order: l.sort_order };
          h.amount = h.amount.plus(l.amount);
          heads.set(l.component_code, h);
        }
      }
      months.set(key, m);
    }
    const monthList = [...months.values()];
    const sum = (k: keyof Omit<MonthFigures, 'bsYear' | 'bsMonth'>) => monthList.reduce((a, m) => a.plus(m[k]), D(0));
    const ob = opening.get(e.id);
    const last = mine.at(-1);
    const lastTax = last?.tax ?? null;
    const complete = !!last && last.start_ad >= bsMonthBounds(fiscalYearStart + 1, 3).startAd;
    const tax: AnnualSummary['tax'] = lastTax ? {
      complete, asOfMonth: last ? `${BS_MONTH_NAMES_EN[last.bs_month - 1]} ${last.bs_year}` : null,
      projectedAnnualTaxable: String(lastTax.projectedAnnualTaxable ?? '0'), retirementDeduction: String(lastTax.retirementDeduction ?? '0'), insuranceDeduction: String(lastTax.insuranceDeduction ?? '0'),
      remoteAreaDeduction: String(lastTax.remoteAreaDeduction ?? '0'), disabilityDeduction: String(lastTax.disabilityDeduction ?? '0'), donationDeduction: String(lastTax.donationDeduction ?? '0.00'),
      taxableAfterDeductions: String(lastTax.taxableAfterDeductions ?? '0'), grossTax: String(lastTax.grossTax ?? '0'), femaleRebate: String(lastTax.femaleRebate ?? '0'),
      medicalCredit: String(lastTax.medicalCredit ?? '0'), annualTax: String(lastTax.annualTax ?? '0'),
    } : null;
    const tdsTotal = sum('tds').plus(ob?.tds_paid ?? 0);
    void lastPeriod;
    let pan: string | null = null;
    if (e.pan_enc) { const full = cipher.decrypt(e.pan_enc, `employee:${e.id}:pan`); pan = opts.revealIds ? full : `*****${full.slice(-4)}`; }
    return {
      employee: { id: e.id, code: e.emp_code, name: e.name_en, designation: e.designation, department: e.department, joinDate: e.join_date, exitDate: e.exit_date, pan, ssfId: e.ssf_id, pfId: e.pf_id, citId: e.cit_id },
      fiscalYearStart, months: monthList,
      incomeHeads: [...heads.values()].sort((a, b) => a.order - b.order).map((h) => ({ code: h.code, name: h.name, amount: h.amount.toFixed(2), taxable: h.taxable })),
      opening: ob ? { gross: ob.gross, taxable: ob.taxable, tds: ob.tds_paid, retirementEmployee: ob.retirement_employee, retirementEmployer: ob.retirement_employer, source: ob.source } : null,
      totals: {
        gross: sum('gross').plus(ob?.gross ?? 0).toFixed(2), taxable: sum('taxable').plus(ob?.taxable ?? 0).toFixed(2), tds: tdsTotal.toFixed(2), sst: sum('sst').toFixed(2),
        retirementEmployee: sum('ssfEe').plus(sum('pfEe')).plus(sum('cit')).plus(ob?.retirement_employee ?? 0).toFixed(2),
        retirementEmployer: sum('ssfEr').plus(sum('pfEr')).plus(sum('gratuity')).plus(ob?.retirement_employer ?? 0).toFixed(2), cit: sum('cit').toFixed(2),
      },
      tax, balance: tax ? D(tax.annualTax).minus(tdsTotal).toFixed(2) : null,
    };
  });
}

// ---------------------------------------------------------------- certificate pages

let brand: string | null | undefined;
function brandLogo(): string | null {
  if (brand !== undefined) return brand;
  try { brand = `data:image/svg+xml;base64,${readFileSync(new URL('../../../../web/public/brand/cv-wordmark-carbon.svg', import.meta.url)).toString('base64')}`; } catch { brand = null; }
  return brand;
}

async function companyProfile(db: Queryable, companyId: string): Promise<{ name: string; address: string | null; pan: string | null; logo: string | null }> {
  const c = await db.query<{ name_en: string; value: { nameEn?: string; address?: string; pan?: string; logoDataUrl?: string } | null }>(
    `SELECT c.name_en, s.value FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'company.profile' WHERE c.id = $1`, [companyId]);
  const v = c.rows[0]?.value ?? {};
  const logo = v.logoDataUrl && /^data:image\/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(v.logoDataUrl) ? v.logoDataUrl : brandLogo();
  return { name: v.nameEn || c.rows[0]?.name_en || 'Company', address: v.address ?? null, pan: v.pan ?? null, logo };
}

const PAGE_CSS = `body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:12.5px;color:#17191e;margin:24px auto;max-width:820px;line-height:1.5;padding:0 24px}
.head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #12773a;padding-bottom:12px;margin-bottom:16px}.head img{height:34px}.co{text-align:right;color:#4b5060}.co b{color:#17191e;font-size:15px}
h1{font-size:18px;margin:6px 0 2px;letter-spacing:-.01em}.sub{color:#6b707b}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px 20px;margin:14px 0}.meta span{display:block;color:#6b707b;font-size:11px}
table{width:100%;border-collapse:collapse;margin:8px 0 14px}th{text-align:left;font-weight:600;color:#4b5060;font-size:11.5px;border-bottom:1px solid #d9dbe0;padding:5px 4px}
td{border-bottom:1px solid #eeeff2;padding:5px 4px}.n{text-align:right;font-variant-numeric:tabular-nums}tr.t td{font-weight:600;border-top:1px solid #9aa0aa}
h2{font-size:13.5px;margin:18px 0 4px}.note{font-size:11px;color:#6b707b}.sign{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:48px}.sign div{border-top:1px solid #9aa0aa;padding-top:4px;color:#4b5060}
.draft{background:#fff4e5;color:#8a4b00;padding:8px 12px;border-radius:8px;margin-bottom:10px}@media print{body{margin:8mm auto}}`;

function pageHead(co: Awaited<ReturnType<typeof companyProfile>>, title: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${esc(title)}</title><style>${PAGE_CSS}</style></head><body>
<div class="head">${co.logo ? `<img src="${esc(co.logo)}" alt="">` : '<span></span>'}<div class="co"><b>${esc(co.name)}</b>${co.address ? `<br>${esc(co.address)}` : ''}${co.pan ? `<br>PAN ${esc(co.pan)}` : ''}</div></div>`;
}

export function taxCertificateHtml(co: Awaited<ReturnType<typeof companyProfile>>, s: AnnualSummary, issuedOn: string): string {
  const e = s.employee;
  const monthRows = s.months.map((m) => `<tr><td>${esc(BS_MONTH_NAMES_EN[m.bsMonth - 1])} ${m.bsYear}</td><td class="n">${npr(m.gross)}</td><td class="n">${npr(m.taxable)}</td><td class="n">${npr(D(m.ssfEe).plus(m.pfEe))}</td><td class="n">${npr(m.cit)}</td><td class="n">${npr(m.sst)}</td><td class="n">${npr(D(m.tds).minus(m.sst))}</td><td class="n">${npr(m.tds)}</td></tr>`).join('');
  const t = s.tax;
  const row = (label: string, v: string | undefined | null, strong = false) => `<tr${strong ? ' class="t"' : ''}><td>${esc(label)}</td><td class="n">${npr(v ?? '0')}</td></tr>`;
  return `${pageHead(co, `Tax certificate ${e.code} FY ${fyLabel(s.fiscalYearStart)}`)}
${t && !t.complete ? `<div class="draft">The fiscal year is not finished: the tax figures are the projection as of ${esc(t.asOfMonth)} and will change with later payslips.</div>` : ''}
<h1>Tax deduction certificate</h1><div class="sub">Remuneration and tax deducted at source, fiscal year ${fyLabel(s.fiscalYearStart)} (Shrawan ${s.fiscalYearStart} to Ashadh ${s.fiscalYearStart + 1})</div>
<div class="meta"><div><span>Employee</span>${esc(e.name)} (${esc(e.code)})</div><div><span>Designation</span>${esc(e.designation) || '—'}</div><div><span>Department</span>${esc(e.department) || '—'}</div>
<div><span>Employee PAN</span>${esc(e.pan) || '—'}</div><div><span>SSF number</span>${esc(e.ssfId) || '—'}</div><div><span>Service in the year</span>${esc(e.joinDate && e.joinDate > fiscalYear(s.fiscalYearStart).startAd ? `from ${bsLabel(e.joinDate)}` : 'whole year')}${e.exitDate && e.exitDate <= fiscalYear(s.fiscalYearStart).endAd ? `, until ${esc(bsLabel(e.exitDate))}` : ''}</div></div>
<h2>Monthly remuneration and tax deposited</h2>
<table><thead><tr><th>Month</th><th class="n">Gross</th><th class="n">Taxable</th><th class="n">SSF/PF (employee)</th><th class="n">CIT</th><th class="n">SST</th><th class="n">Income tax</th><th class="n">Total TDS</th></tr></thead><tbody>
${monthRows || '<tr><td colspan="8" class="sub">No locked payslips in this fiscal year.</td></tr>'}
${s.opening ? `<tr><td>Before Hajiri (${esc(s.opening.source === 'previous_employer' ? 'previous employer' : 'earlier payroll')})</td><td class="n">${npr(s.opening.gross)}</td><td class="n">${npr(s.opening.taxable)}</td><td class="n" colspan="2">${npr(s.opening.retirementEmployee)}</td><td class="n" colspan="2"></td><td class="n">${npr(s.opening.tds)}</td></tr>` : ''}
<tr class="t"><td>Total</td><td class="n">${npr(s.totals.gross)}</td><td class="n">${npr(s.totals.taxable)}</td><td class="n" colspan="2">${npr(s.totals.retirementEmployee)}</td><td class="n">${npr(s.totals.sst)}</td><td class="n">${npr(D(s.totals.tds).minus(s.totals.sst))}</td><td class="n">${npr(s.totals.tds)}</td></tr>
</tbody></table>
<h2>Income by head</h2><table><thead><tr><th>Head</th><th>Taxable</th><th class="n">Amount</th></tr></thead><tbody>
${s.incomeHeads.map((h) => `<tr><td>${esc(h.name)}</td><td>${h.taxable ? 'Yes' : 'No'}</td><td class="n">${npr(h.amount)}</td></tr>`).join('') || '<tr><td colspan="3" class="sub">None</td></tr>'}</tbody></table>
${t ? `<h2>Tax computation</h2><table><tbody>
${row('Assessable remuneration for the year', t.projectedAnnualTaxable)}
${row('Less retirement contributions (SSF, PF, CIT; Sec 63)', t.retirementDeduction)}
${row('Less insurance premiums', t.insuranceDeduction)}
${D(t.remoteAreaDeduction).gt(0) ? row('Less remote area allowance', t.remoteAreaDeduction) : ''}
${D(t.disabilityDeduction).gt(0) ? row('Less disability allowance', t.disabilityDeduction) : ''}
${D(t.donationDeduction).gt(0) ? row('Less approved donations (Sec 12)', t.donationDeduction) : ''}
${row('Taxable income', t.taxableAfterDeductions, true)}
${row('Tax on taxable income', t.grossTax)}
${D(t.femaleRebate).gt(0) ? row('Less female rebate', t.femaleRebate) : ''}
${D(t.medicalCredit).gt(0) ? row('Less medical tax credit (Sec 51)', t.medicalCredit) : ''}
${row('Tax liability for the year', t.annualTax, true)}
${row('Tax deducted and deposited', s.totals.tds)}
${row(D(s.balance ?? 0).isNegative() ? 'Tax deducted in excess' : 'Balance still to be deducted', D(s.balance ?? 0).abs().toFixed(2), true)}
</tbody></table>` : ''}
<p class="note">Issued on ${esc(issuedOn)}. Amounts in NPR. Computer-generated from the locked payroll records of ${esc(co.name)}. Figures depend on the tax rules configured in Hajiri; confirm them with your accountant.</p>
<div class="sign"><div>Prepared by</div><div>Authorised signatory</div></div></body></html>`;
}

export function salaryCertificateHtml(co: Awaited<ReturnType<typeof companyProfile>>, s: AnnualSummary, current: { lines: Array<{ name: string; amount: string }>; gross: string; deductions: Array<{ name: string; amount: string }>; net: string; asOf: string } | null, issuedOn: string, purpose: string | null): string {
  const e = s.employee;
  return `${pageHead(co, `Salary certificate ${e.code}`)}
<h1>Salary certificate</h1><div class="sub">Issued on ${esc(issuedOn)}${purpose ? ` · for ${esc(purpose)}` : ''}</div>
<p style="margin-top:14px">This is to certify that <b>${esc(e.name)}</b> (employee code ${esc(e.code)}) ${e.exitDate && e.exitDate < nptToday() ? 'was' : 'is'} employed with ${esc(co.name)}${e.designation ? ` as <b>${esc(e.designation)}</b>` : ''}${e.department ? ` in ${esc(e.department)}` : ''} since ${esc(bsLabel(e.joinDate))}${e.exitDate && e.exitDate < nptToday() ? ` until ${esc(bsLabel(e.exitDate))}` : ''}.</p>
${current ? `<h2>Current monthly salary (as of ${esc(current.asOf)})</h2><table><tbody>
${current.lines.map((l) => `<tr><td>${esc(l.name)}</td><td class="n">${npr(l.amount)}</td></tr>`).join('')}
<tr class="t"><td>Gross monthly salary</td><td class="n">${npr(current.gross)}</td></tr>
${current.deductions.map((l) => `<tr><td>Less ${esc(l.name)}</td><td class="n">${npr(l.amount)}</td></tr>`).join('')}
<tr class="t"><td>Net monthly salary (last payslip)</td><td class="n">${npr(current.net)}</td></tr></tbody></table>
<p class="note">${esc(amountInWordsEn(current.gross))} gross per month.</p>` : ''}
<h2>Fiscal year ${fyLabel(s.fiscalYearStart)} so far</h2><table><tbody>
<tr><td>Gross remuneration</td><td class="n">${npr(s.totals.gross)}</td></tr><tr><td>Retirement contributions (employee)</td><td class="n">${npr(s.totals.retirementEmployee)}</td></tr>
<tr><td>Tax deducted at source</td><td class="n">${npr(s.totals.tds)}</td></tr></tbody></table>
<p class="note">Amounts in NPR, from the locked payroll records. This certificate is issued at the employee's request without any liability on the part of the company.</p>
<div class="sign"><div>Prepared by</div><div>Authorised signatory</div></div></body></html>`;
}

/** The latest locked regular payslip (for the salary certificate's current salary). */
async function lastPayslip(db: Queryable, employeeId: string): Promise<{ lines: Array<{ name: string; amount: string; type: string }>; gross: string; net: string; label: string } | null> {
  const r = await db.query<{ id: string; gross: string; net: string; bs_year: number; bs_month: number }>(
    `SELECT ps.id, ps.gross::text, ps.net::text, p.bs_year, p.bs_month FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
      WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.run_type = 'regular' ORDER BY p.start_ad DESC LIMIT 1`, [employeeId]);
  const ps = r.rows[0];
  if (!ps) return null;
  const lines = await db.query<{ name_en: string; amount: string; type: string }>(
    `SELECT name_en, amount::text, type FROM payslip_line WHERE payslip_id = $1 AND type IN ('earning', 'deduction') AND component_code <> 'ROUNDING' ORDER BY sort_order`, [ps.id]);
  return { lines: lines.rows.map((l) => ({ name: l.name_en, amount: l.amount, type: l.type })), gross: ps.gross, net: ps.net, label: `${BS_MONTH_NAMES_EN[ps.bs_month - 1]} ${ps.bs_year}` };
}

// ---------------------------------------------------------------- routes

const openingBody = z.object({
  employeeId: z.uuid(), fiscalYearStart: fyParam,
  gross: amount.default('0'), taxable: amount.default('0'), tdsPaid: amount.default('0'), retirementEmployee: amount.default('0'), retirementEmployer: amount.default('0'),
  festivalPaid: z.boolean().default(false), months: z.number().int().min(0).max(12).default(0),
  source: z.enum(['previous_employer', 'go_live', 'other']).default('previous_employer'), note: z.string().trim().max(500).nullish(),
}).refine((b) => D(b.taxable).lte(D(b.gross).plus(b.retirementEmployer)), { message: 'taxable income cannot be more than gross plus the employer contribution', path: ['taxable'] });

async function saveOpening(db: Queryable, companyId: string, actorId: string, b: z.infer<typeof openingBody>): Promise<void> {
  await db.query(
    `INSERT INTO payroll_opening_balance (company_id, employee_id, fiscal_year_start, gross, taxable, tds_paid, retirement_employee, retirement_employer, festival_paid, months, source, note, updated_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
     ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET gross = EXCLUDED.gross, taxable = EXCLUDED.taxable, tds_paid = EXCLUDED.tds_paid, retirement_employee = EXCLUDED.retirement_employee,
       retirement_employer = EXCLUDED.retirement_employer, festival_paid = EXCLUDED.festival_paid, months = EXCLUDED.months, source = EXCLUDED.source, note = EXCLUDED.note,
       updated_by = EXCLUDED.updated_by, updated_at = now()`,
    [companyId, b.employeeId, b.fiscalYearStart, b.gross, b.taxable, b.tdsPaid, b.retirementEmployee, b.retirementEmployer, b.festivalPaid, b.months, b.source, b.note ?? null, actorId]);
  await audit(db, { actorId, action: 'upsert', table: 'payroll_opening_balance', rowId: b.employeeId, after: { fiscalYearStart: b.fiscalYearStart, gross: b.gross, taxable: b.taxable, tdsPaid: b.tdsPaid } });
}

function sendHtml(res: Response, html: string): void {
  res.setHeader('Cache-Control', 'private, no-store');
  res.type('text/html').send(html);
}

export function complianceRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  // ------------------------------------------------------------ opening balances
  r.get('/opening-balances', hr, async (req, res) => {
    const q = z.object({ fiscalYearStart: fyParam.optional() }).parse(req.query);
    const fy = q.fiscalYearStart ?? currentFiscalYearStart();
    const rows = await db.query(
      `SELECT o.id, o.employee_id, e.emp_code, e.name_en, o.fiscal_year_start, o.gross::text, o.taxable::text, o.tds_paid::text, o.retirement_employee::text, o.retirement_employer::text,
              o.festival_paid, o.months, o.source, o.note, o.updated_at, u.name AS updated_by_name
         FROM payroll_opening_balance o JOIN employee e ON e.id = o.employee_id LEFT JOIN app_user u ON u.id = o.updated_by
        WHERE o.company_id = $1 AND o.fiscal_year_start = $2 ORDER BY e.emp_code`, [companyIdOf(req), fy]);
    res.json({ fiscalYearStart: fy, balances: rows.rows });
  });

  r.put('/opening-balances', hr, async (req, res) => {
    const b = openingBody.parse(req.body);
    const companyId = companyIdOf(req);
    await assertEmployee(db, companyId, b.employeeId);
    await saveOpening(db, companyId, req.user!.id, b);
    res.json({ ok: true });
  });

  r.delete('/opening-balances/:id', hr, async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const d = await db.query('DELETE FROM payroll_opening_balance WHERE id = $1 AND company_id = $2 RETURNING employee_id, fiscal_year_start', [id, companyIdOf(req)]);
    if (!d.rows[0]) throw new NotFoundError('opening balance not found');
    await audit(db, { actorId: req.user!.id, action: 'delete', table: 'payroll_opening_balance', rowId: id, before: d.rows[0] });
    res.json({ ok: true });
  });

  /** CSV: emp_code, fiscal_year_start, gross, taxable, tds_paid, retirement_employee, retirement_employer, festival_paid, months, source, note. All rows or none. */
  r.post('/opening-balances/import', hr, async (req, res) => {
    const text = typeof req.body === 'string' ? req.body : (req.body?.csv as string | undefined);
    if (!text) throw new BadRequestError('send CSV as text/csv body or {"csv": "..."}');
    const rows = parseCsv(text.replace(/^﻿/, ''));
    if (rows.length < 2) throw new BadRequestError('CSV needs a header row and at least one data row');
    if (rows.length > 5001) throw new BadRequestError('CSV has more than 5,000 data rows; split the file');
    const header = rows[0]!.map((h) => h.trim().toLowerCase());
    for (const h of ['emp_code', 'fiscal_year_start']) if (!header.includes(h)) throw new BadRequestError(`missing column ${h}`);
    const companyId = companyIdOf(req);
    const codes = await db.query<{ id: string; emp_code: string }>('SELECT id, emp_code FROM employee WHERE company_id = $1', [companyId]);
    const byCode = new Map(codes.rows.map((c) => [c.emp_code.toLowerCase(), c.id]));
    const errors: Array<{ row: number; message: string }> = [];
    const valid: Array<z.infer<typeof openingBody>> = [];
    for (let i = 1; i < rows.length; i++) {
      const rec = Object.fromEntries(header.map((h, j) => [h, rows[i]![j]?.trim() ?? '']));
      if (Object.values(rec).every((v) => !v)) continue;
      const employeeId = byCode.get(String(rec.emp_code).toLowerCase());
      if (!employeeId) { errors.push({ row: i + 1, message: `no employee with code ${rec.emp_code}` }); continue; }
      const num = (k: string) => (rec[k] ? String(rec[k]).replace(/,/g, '') : '0');
      const parsed = openingBody.safeParse({
        employeeId, fiscalYearStart: rec.fiscal_year_start, gross: num('gross'), taxable: num('taxable'), tdsPaid: num('tds_paid'), retirementEmployee: num('retirement_employee'),
        retirementEmployer: num('retirement_employer'), festivalPaid: ['true', '1', 'yes'].includes(String(rec.festival_paid ?? '').toLowerCase()),
        months: rec.months ? Number(rec.months) : 0, source: rec.source || undefined, note: rec.note || undefined,
      });
      if (!parsed.success) { for (const is of parsed.error.issues) errors.push({ row: i + 1, message: `${is.path.join('.') || 'row'}: ${is.message}` }); continue; }
      valid.push(parsed.data);
    }
    if (errors.length) return res.status(400).json({ error: 'some rows have errors; nothing was imported', details: { errors } });
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      for (const b of valid) await saveOpening(client, companyId, req.user!.id, b);
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    return res.json({ imported: valid.length });
  });

  // ------------------------------------------------------------ optimum CIT
  const optimumFor = async (companyId: string, employeeId: string, fy: number): Promise<(OptimumCit & { available: true }) | { available: false; reason: string }> => {
    const built = await estimateInput(db, companyId, employeeId, fy);
    if ('reason' in built) return { available: false, reason: built.reason };
    return { available: true, ...optimumCit(built.input) };
  };

  r.get('/optimum-cit/me', async (req, res) => {
    const employeeId = req.user!.employeeId;
    if (!employeeId) throw new BadRequestError('this account is not linked to an employee');
    res.json(await optimumFor(companyIdOf(req), employeeId, currentFiscalYearStart()));
  });

  r.get('/optimum-cit', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const fy = currentFiscalYearStart();
    const emps = await db.query<{ id: string; emp_code: string; name_en: string }>(
      `SELECT e.id, e.emp_code, e.name_en FROM employee e WHERE e.company_id = $1 AND e.status = 'active' AND (e.exit_date IS NULL OR e.exit_date >= $2)
          AND EXISTS (SELECT 1 FROM salary_assignment a WHERE a.employee_id = e.id) ORDER BY e.emp_code LIMIT 2000`, [companyId, nptToday()]);
    const out = [];
    for (const e of emps.rows) out.push({ employeeId: e.id, empCode: e.emp_code, name: e.name_en, ...(await optimumFor(companyId, e.id, fy)) });
    res.json({ fiscalYearStart: fy, employees: out });
  });

  /** HR applies a monthly CIT amount from a month of the current fiscal year; earlier months keep the amount they had. */
  r.post('/optimum-cit/apply', hr, async (req, res) => {
    const b = z.object({ employeeId: z.uuid(), monthlyCit: amount, fromBsMonth: z.number().int().min(1).max(12).optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const emp = await assertEmployee(db, companyId, b.employeeId);
    const fy = currentFiscalYearStart();
    const today = adToBs(nptToday());
    const fromIndex = fyMonth(b.fromBsMonth ?? today.month);
    if (fromIndex < fyMonth(today.month)) throw new BadRequestError('choose this month or a later month of the fiscal year');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const cur = await client.query<{ cit_monthly: string; cit_from_month: number | null; cit_prev_monthly: string }>(
        `SELECT cit_monthly::text, cit_from_month, cit_prev_monthly::text FROM tax_declaration WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'verified' FOR UPDATE`, [b.employeeId, fy]);
      // The amount in force just before the new start month.
      const before = cur.rows[0] ? effectiveCit(cur.rows[0], Math.max(1, fromIndex - 1)) : '0.00';
      await client.query(
        `INSERT INTO tax_declaration (employee_id, fiscal_year_start, cit_monthly, cit_from_month, cit_prev_monthly, status, verified_by, verified_at)
         VALUES ($1, $2, $3, $4, $5, 'verified', $6, now())
         ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET cit_monthly = EXCLUDED.cit_monthly, cit_from_month = EXCLUDED.cit_from_month, cit_prev_monthly = EXCLUDED.cit_prev_monthly,
           status = 'verified', verified_by = EXCLUDED.verified_by, verified_at = now(), updated_at = now()`,
        [b.employeeId, fy, b.monthlyCit, fromIndex === 1 ? null : fromIndex, before, req.user!.id]);
      await audit(client, { actorId: req.user!.id, action: 'apply_cit', table: 'tax_declaration', rowId: b.employeeId, after: { fiscalYearStart: fy, monthlyCit: b.monthlyCit, fromMonthIndex: fromIndex, before } });
      const userId = await userIdForEmployee(client, b.employeeId);
      const monthName = BS_MONTH_NAMES_EN[((fromIndex + 2) % 12)];
      await notify(client, { companyId, userId, employeeId: b.employeeId, template: 'payroll.cit_applied', title: 'Your CIT contribution changed',
        body: `HR set your Citizen Investment Trust contribution to NPR ${npr(b.monthlyCit)} a month from ${monthName}. It lowers your income tax this year.`, payload: { fiscalYearStart: fy }, channels: ['push'] });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true, employee: emp.emp_code, fiscalYearStart: fy, fromMonthIndex: fromIndex });
  });

  // ------------------------------------------------------------ held pay
  r.get('/holds', hr, async (req, res) => {
    const q = z.object({ status: z.enum(['held', 'releasing', 'released', 'cancelled', 'open']).default('open') }).parse(req.query);
    const rows = await db.query(
      `SELECT h.id, h.employee_id, e.emp_code, e.name_en, h.run_id, r.run_type, r.status AS run_status, p.bs_year, p.bs_month, h.amount::text, h.reason, h.status, h.release_mode,
              h.release_run_id, rp.bs_year AS release_bs_year, rp.bs_month AS release_bs_month, h.release_note, h.held_at, hb.name AS held_by_name, h.released_at
         FROM payroll_hold h JOIN employee e ON e.id = h.employee_id JOIN payroll_run r ON r.id = h.run_id JOIN payroll_period p ON p.id = r.period_id
         LEFT JOIN payroll_run rr ON rr.id = h.release_run_id LEFT JOIN payroll_period rp ON rp.id = rr.period_id LEFT JOIN app_user hb ON hb.id = h.held_by
        WHERE h.company_id = $1 AND ($2 = 'open' AND h.status IN ('held', 'releasing') OR h.status = $2)
        ORDER BY h.held_at DESC LIMIT 500`, [companyIdOf(req), q.status]);
    res.json({ holds: rows.rows });
  });

  r.post('/runs/:id/holds', hr, async (req, res) => {
    const runId = z.uuid().parse(req.params.id);
    const b = z.object({ employeeId: z.uuid(), reason: z.string().trim().min(3).max(500) }).parse(req.body);
    const companyId = companyIdOf(req);
    const run = await getRun(db, companyId, runId);
    if (!['computed', 'reviewed', 'approved', 'locked'].includes(run.status)) throw new ConflictError('calculate the run before holding anyone\'s pay');
    const ps = await db.query<{ net: string }>('SELECT net::text FROM payslip WHERE run_id = $1 AND employee_id = $2', [runId, b.employeeId]);
    if (!ps.rows[0]) throw new NotFoundError('this employee has no payslip in the run');
    if (!D(ps.rows[0].net).gt(0)) throw new ConflictError('there is no net pay to hold');
    const existing = await db.query<{ id: string; status: string }>('SELECT id, status FROM payroll_hold WHERE run_id = $1 AND employee_id = $2', [runId, b.employeeId]);
    if (existing.rows[0] && existing.rows[0].status !== 'cancelled') throw new ConflictError('pay is already held for this employee in this run');
    const row = await db.query<{ id: string }>(
      `INSERT INTO payroll_hold (company_id, employee_id, run_id, amount, reason, held_by) VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (run_id, employee_id) DO UPDATE SET amount = EXCLUDED.amount, reason = EXCLUDED.reason, held_by = EXCLUDED.held_by, held_at = now(), status = 'held',
         release_mode = NULL, release_run_id = NULL, release_note = NULL, released_by = NULL, released_at = NULL
       RETURNING id`, [companyId, b.employeeId, runId, ps.rows[0].net, b.reason, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'hold', table: 'payroll_hold', rowId: row.rows[0]!.id, after: { runId, employeeId: b.employeeId, amount: ps.rows[0].net, reason: b.reason } });
    res.status(201).json({ id: row.rows[0]!.id, amount: ps.rows[0].net });
  });

  /** Release held pay: through the next pay run as a non-taxable line, or record that it was paid outside payroll. */
  r.post('/holds/:id/release', hr, async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ mode: z.enum(['payroll', 'outside']), note: z.string().trim().max(500).optional() }).parse(req.body);
    const companyId = companyIdOf(req);
    const h = await db.query<{ status: string; run_id: string; employee_id: string; amount: string }>('SELECT status, run_id, employee_id, amount::text FROM payroll_hold WHERE id = $1 AND company_id = $2', [id, companyId]);
    const hold = h.rows[0];
    if (!hold) throw new NotFoundError('hold not found');
    if (hold.status !== 'held') throw new ConflictError(`this pay is already ${hold.status}`);
    const run = await getRun(db, companyId, hold.run_id);
    if (!run.locked_at) throw new ConflictError('lock the pay run first, or cancel the hold to pay normally in this run');
    if (b.mode === 'outside') {
      await db.query(`UPDATE payroll_hold SET status = 'released', release_mode = 'outside', release_note = $2, released_by = $3, released_at = now() WHERE id = $1`, [id, b.note ?? null, req.user!.id]);
    } else {
      await db.query(`UPDATE payroll_hold SET status = 'releasing', release_mode = 'payroll', release_note = $2, released_by = $3 WHERE id = $1`, [id, b.note ?? null, req.user!.id]);
    }
    await audit(db, { actorId: req.user!.id, action: 'release_hold', table: 'payroll_hold', rowId: id, after: { mode: b.mode, amount: hold.amount } });
    const userId = await userIdForEmployee(db, hold.employee_id);
    await notify(db, { companyId, userId, employeeId: hold.employee_id, template: 'payroll.hold_released', title: 'Held pay released',
      body: b.mode === 'payroll' ? `NPR ${npr(hold.amount)} that was on hold will be paid with your next salary.` : `NPR ${npr(hold.amount)} that was on hold has been paid.`, payload: { holdId: id }, channels: ['push'] });
    res.json({ ok: true });
  });

  /** Undo a hold (made by mistake) while its run is not locked: the employee is paid normally in that run. */
  r.post('/holds/:id/cancel', hr, async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const companyId = companyIdOf(req);
    const h = await db.query<{ status: string; run_id: string }>('SELECT status, run_id FROM payroll_hold WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!h.rows[0]) throw new NotFoundError('hold not found');
    if (h.rows[0].status !== 'held') throw new ConflictError(`this pay is already ${h.rows[0].status}`);
    const run = await getRun(db, companyId, h.rows[0].run_id);
    if (run.locked_at) throw new ConflictError('the run is locked: release the pay instead');
    await db.query(`UPDATE payroll_hold SET status = 'cancelled' WHERE id = $1`, [id]);
    await audit(db, { actorId: req.user!.id, action: 'cancel_hold', table: 'payroll_hold', rowId: id });
    res.json({ ok: true });
  });

  // ------------------------------------------------------------ certificates and annual summary
  r.get('/certificates/:employeeId', async (req, res) => {
    const q = z.object({ fiscalYearStart: fyParam.optional(), kind: z.enum(['tax', 'salary']).default('tax'), purpose: z.string().trim().max(120).optional() }).parse(req.query);
    const companyId = companyIdOf(req);
    const employeeId = req.params.employeeId === 'me' ? req.user!.employeeId : z.uuid().parse(req.params.employeeId);
    if (!employeeId) throw new BadRequestError('this account is not linked to an employee');
    const own = employeeId === req.user!.employeeId;
    if (!own && !hasModule(req.user!, 'payroll')) throw new NotFoundError('employee not found');
    await assertEmployee(db, companyId, employeeId);
    const fy = q.fiscalYearStart ?? currentFiscalYearStart();
    const [s] = await annualSummaries(db, cipher, companyId, fy, [employeeId], { revealIds: true });
    if (!s) throw new NotFoundError('employee not found');
    const co = await companyProfile(db, companyId);
    const issuedOn = bsLabel(nptToday());
    if (!own) await audit(db, { actorId: req.user!.id, action: 'certificate', table: 'employee', rowId: employeeId, after: { kind: q.kind, fiscalYearStart: fy } });
    if (q.kind === 'tax') return sendHtml(res, taxCertificateHtml(co, s, issuedOn));
    const last = await lastPayslip(db, employeeId);
    const current = last ? { lines: last.lines.filter((l) => l.type === 'earning').map((l) => ({ name: l.name, amount: l.amount })), gross: last.gross,
      deductions: last.lines.filter((l) => l.type === 'deduction').map((l) => ({ name: l.name, amount: l.amount })), net: last.net, asOf: last.label } : null;
    return sendHtml(res, salaryCertificateHtml(co, s, current, issuedOn, q.purpose ?? null));
  });

  r.get('/reports/annual-tax', hr, async (req, res) => {
    const q = z.object({ fiscalYearStart: fyParam.optional(), format: z.enum(['json', 'csv']).default('json') }).parse(req.query);
    const fy = q.fiscalYearStart ?? currentFiscalYearStart();
    const list = await annualSummaries(db, cipher, companyIdOf(req), fy, null, { revealIds: q.format === 'csv' });
    if (q.format === 'csv') await audit(db, { actorId: req.user!.id, action: 'export', table: 'payroll_annual_tax', after: { fiscalYearStart: fy, rows: list.length } });
    const rows = list.map((s) => ({
      employeeId: s.employee.id, empCode: s.employee.code, name: s.employee.name, pan: s.employee.pan, gross: s.totals.gross, taxable: s.totals.taxable,
      retirementEmployee: s.totals.retirementEmployee, retirementEmployer: s.totals.retirementEmployer, taxableAfterDeductions: s.tax?.taxableAfterDeductions ?? '',
      annualTax: s.tax?.annualTax ?? '', sst: s.totals.sst, tds: s.totals.tds, balance: s.balance ?? '', complete: s.tax?.complete ? 'yes' : 'no', opening: s.opening ? 'yes' : 'no',
    }));
    sendReport(res, q.format, `annual-tax-${fy}`, [
      { key: 'empCode', header: 'Employee code' }, { key: 'name', header: 'Name' }, { key: 'pan', header: 'PAN' }, { key: 'gross', header: 'Gross' },
      { key: 'taxable', header: 'Taxable pay' }, { key: 'retirementEmployee', header: 'Retirement (employee)' }, { key: 'retirementEmployer', header: 'Retirement (employer)' },
      { key: 'taxableAfterDeductions', header: 'Taxable income' }, { key: 'annualTax', header: 'Tax liability' }, { key: 'sst', header: 'SST deducted' },
      { key: 'tds', header: 'TDS deducted' }, { key: 'balance', header: 'Balance' }, { key: 'complete', header: 'Year complete' }, { key: 'opening', header: 'Opening balance' },
    ], rows);
  });

  // ------------------------------------------------------------ monthly statutory reports
  const monthQuery = z.object({ bsYear: z.coerce.number().int().min(2070).max(2200), bsMonth: z.coerce.number().int().min(1).max(12), format: z.enum(['json', 'csv']).default('json') });

  /** TDS and SST withheld per employee in a BS month (all locked runs of the period), to deposit with the tax office. */
  r.get('/reports/tax-deposit', hr, async (req, res) => {
    const q = monthQuery.parse(req.query);
    const companyId = companyIdOf(req);
    const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; pan_enc: Buffer | null; taxable: string; tds: string; tax: TaxSnap | null }>(
      `SELECT ps.employee_id, e.emp_code, e.name_en, e.pan_enc, ps.taxable::text, ps.tds::text, ps.inputs_snapshot->'result'->'tax' AS tax
         FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
        WHERE r.company_id = $1 AND p.bs_year = $2 AND p.bs_month = $3 AND r.locked_at IS NOT NULL ORDER BY e.emp_code, r.locked_at`, [companyId, q.bsYear, q.bsMonth]);
    const by = new Map<string, { empCode: string; name: string; pan: string | null; taxable: Decimal; sst: Decimal; tds: Decimal }>();
    for (const x of rows.rows) {
      const cur = by.get(x.employee_id) ?? { empCode: x.emp_code, name: x.name_en, pan: x.pan_enc ? cipher.decrypt(x.pan_enc, `employee:${x.employee_id}:pan`) : null, taxable: D(0), sst: D(0), tds: D(0) };
      cur.taxable = cur.taxable.plus(x.taxable); cur.tds = cur.tds.plus(x.tds); cur.sst = cur.sst.plus(sstShare(x.tds, x.tax));
      by.set(x.employee_id, cur);
    }
    const out = [...by.values()].map((v) => ({ empCode: v.empCode, name: v.name, pan: q.format === 'csv' ? v.pan : v.pan ? `*****${v.pan.slice(-4)}` : null,
      taxable: v.taxable.toFixed(2), sst: v.sst.toFixed(2), incomeTax: v.tds.minus(v.sst).toFixed(2), tds: v.tds.toFixed(2) }));
    if (q.format === 'csv') await audit(db, { actorId: req.user!.id, action: 'export', table: 'payroll_tax_deposit', after: { bsYear: q.bsYear, bsMonth: q.bsMonth } });
    sendReport(res, q.format, `tax-deposit-${q.bsYear}-${String(q.bsMonth).padStart(2, '0')}`, [
      { key: 'empCode', header: 'Employee code' }, { key: 'name', header: 'Name' }, { key: 'pan', header: 'PAN' }, { key: 'taxable', header: 'Taxable pay' },
      { key: 'sst', header: 'Social security tax (1%)' }, { key: 'incomeTax', header: 'Income tax' }, { key: 'tds', header: 'Total TDS' },
    ], out);
  });

  /** SSF, PF, gratuity and CIT per employee in a BS month, to deposit with each fund. */
  r.get('/reports/retirement-deposit', hr, async (req, res) => {
    const q = monthQuery.parse(req.query);
    const rows = await db.query<{ emp_code: string; name_en: string; ssf_id: string | null; pf_id: string | null; cit_id: string | null; code: string; amount: string }>(
      `SELECT e.emp_code, e.name_en, e.ssf_id, e.pf_id, e.cit_id, l.component_code AS code, sum(l.amount)::text AS amount
         FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
         JOIN payslip_line l ON l.payslip_id = ps.id AND l.component_code IN ('SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT')
        WHERE r.company_id = $1 AND p.bs_year = $2 AND p.bs_month = $3 AND r.locked_at IS NOT NULL
        GROUP BY e.emp_code, e.name_en, e.ssf_id, e.pf_id, e.cit_id, l.component_code ORDER BY e.emp_code`, [companyIdOf(req), q.bsYear, q.bsMonth]);
    const by = new Map<string, Record<string, string>>();
    for (const x of rows.rows) {
      const cur = by.get(x.emp_code) ?? { empCode: x.emp_code, name: x.name_en, ssfId: x.ssf_id ?? '', pfId: x.pf_id ?? '', citId: x.cit_id ?? '', SSF_EE: '0.00', SSF_ER: '0.00', PF_EE: '0.00', PF_ER: '0.00', GRATUITY: '0.00', CIT: '0.00' };
      cur[x.code] = D(cur[x.code] ?? 0).plus(x.amount).toFixed(2);
      by.set(x.emp_code, cur);
    }
    const out = [...by.values()].map((v) => ({ ...v, ssfTotal: D(v.SSF_EE!).plus(v.SSF_ER!).toFixed(2), pfTotal: D(v.PF_EE!).plus(v.PF_ER!).plus(v.GRATUITY!).toFixed(2) }));
    sendReport(res, q.format, `retirement-deposit-${q.bsYear}-${String(q.bsMonth).padStart(2, '0')}`, [
      { key: 'empCode', header: 'Employee code' }, { key: 'name', header: 'Name' }, { key: 'ssfId', header: 'SSF number' }, { key: 'SSF_EE', header: 'SSF employee (11%)' },
      { key: 'SSF_ER', header: 'SSF employer (20%)' }, { key: 'ssfTotal', header: 'SSF total' }, { key: 'pfId', header: 'PF number' }, { key: 'PF_EE', header: 'PF employee' },
      { key: 'PF_ER', header: 'PF employer' }, { key: 'GRATUITY', header: 'Gratuity' }, { key: 'pfTotal', header: 'PF + gratuity total' }, { key: 'citId', header: 'CIT number' }, { key: 'CIT', header: 'CIT' },
    ], out);
  });

  /** Per employee: this regular run's net against the previous locked regular run, with the reasons it changed. */
  r.get('/reports/pay-variance', hr, async (req, res) => {
    const q = z.object({ runId: z.uuid(), format: z.enum(['json', 'csv']).default('json') }).parse(req.query);
    const companyId = companyIdOf(req);
    const run = await getRun(db, companyId, q.runId);
    if (run.run_type !== 'regular') throw new BadRequestError('pay variance compares regular monthly runs');
    const prevRun = await db.query<{ id: string; bs_year: number; bs_month: number }>(
      `SELECT r.id, p.bs_year, p.bs_month FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
        WHERE r.company_id = $1 AND r.run_type = 'regular' AND r.locked_at IS NOT NULL AND r.id <> $2
          AND (p.bs_year, p.bs_month) < (SELECT bs_year, bs_month FROM payroll_period WHERE id = $3)
        ORDER BY p.bs_year DESC, p.bs_month DESC LIMIT 1`, [companyId, run.id, run.period_id]);
    type Slip = { employee_id: string; emp_code: string; name_en: string; net: string; gross: string; tds: string; lop_days: string; ot_hours: string; basic: string | null; inputs: number; flags: string[] };
    const load = async (runId: string) => (await db.query<Slip>(
      `SELECT ps.employee_id, e.emp_code, e.name_en, ps.net::text, ps.gross::text, ps.tds::text, ps.lop_days::text, ps.ot_hours::text,
              ps.inputs_snapshot->'input'->'baseAmounts'->>'BASIC' AS basic, jsonb_array_length(coalesce(ps.inputs_snapshot->'input'->'inputs', '[]'::jsonb)) AS inputs, ps.flags
         FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1`, [runId])).rows;
    const cur = await load(run.id);
    const prev = prevRun.rows[0] ? new Map((await load(prevRun.rows[0].id)).map((s) => [s.employee_id, s])) : new Map<string, Slip>();
    const seen = new Set<string>();
    const out = cur.map((c) => {
      seen.add(c.employee_id);
      const p = prev.get(c.employee_id);
      const reasons: string[] = [];
      if (!p) reasons.push('New in payroll');
      else {
        if (c.basic !== p.basic || (Array.isArray(c.flags) && c.flags.includes('SalaryRevised'))) reasons.push('Salary revision');
        if (!D(c.lop_days).eq(p.lop_days)) reasons.push(`Unpaid days ${D(p.lop_days).toString()} → ${D(c.lop_days).toString()}`);
        if (!D(c.ot_hours).eq(p.ot_hours)) reasons.push(`Overtime ${D(p.ot_hours).toString()} h → ${D(c.ot_hours).toString()} h`);
        if (c.inputs > 0 || p.inputs > 0) reasons.push('One-time pay or deductions');
        if (!D(c.tds).eq(p.tds)) reasons.push('Tax changed');
        if (Array.isArray(c.flags) && c.flags.includes('LastMonth')) reasons.push('Leaving this month');
      }
      const diff = p ? D(c.net).minus(p.net) : D(c.net);
      return { empCode: c.emp_code, name: c.name_en, previousNet: p?.net ?? '', net: c.net, difference: diff.toFixed(2),
        percent: p && D(p.net).gt(0) ? diff.div(p.net).times(100).toDecimalPlaces(1).toString() : '', reasons: reasons.join('; ') || 'No change' };
    });
    for (const [id, p] of prev) if (!seen.has(id)) out.push({ empCode: p.emp_code, name: p.name_en, previousNet: p.net, net: '0.00', difference: D(p.net).neg().toFixed(2), percent: '-100', reasons: 'Not in this run (left or not paid)' });
    out.sort((a, b) => D(b.difference).abs().comparedTo(D(a.difference).abs()));
    if (q.format === 'json') return res.json({ previous: prevRun.rows[0] ? { runId: prevRun.rows[0].id, bsYear: prevRun.rows[0].bs_year, bsMonth: prevRun.rows[0].bs_month } : null, rows: out });
    return sendReport(res, 'csv', `pay-variance-${run.id}`, [
      { key: 'empCode', header: 'Employee code' }, { key: 'name', header: 'Name' }, { key: 'previousNet', header: 'Previous net' }, { key: 'net', header: 'Net' },
      { key: 'difference', header: 'Difference' }, { key: 'percent', header: 'Change %' }, { key: 'reasons', header: 'Reasons' },
    ], out);
  });

  /** What the encashable leave balances are worth as of a date: balance × last basic ÷ the encashment divisor. */
  r.get('/reports/leave-liability', hr, async (req, res) => {
    const q = z.object({ asOf: isoDate.optional(), format: z.enum(['json', 'csv']).default('json') }).parse(req.query);
    const companyId = companyIdOf(req);
    const asOf = q.asOf ?? nptToday();
    const policy = await db.query<{ value: { encashmentDivisor?: string } }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payroll.policy'`, [companyId]);
    const bs = adToBs(asOf);
    const divisor = policy.rows[0]?.value.encashmentDivisor === 'fixed_30' ? 30 : bsMonthBounds(bs.year, bs.month).days;
    const rows = await db.query<{ employee_id: string; emp_code: string; name_en: string; department: string | null; leave_code: string; leave_name: string; balance: string; basic: string | null }>(
      `SELECT e.id AS employee_id, e.emp_code, e.name_en,
              (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> $2::date LIMIT 1) AS department,
              lt.code AS leave_code, lt.name_en AS leave_name, sum(ll.delta_days)::text AS balance,
              (SELECT sa.base_amounts->>'BASIC' FROM salary_assignment sa WHERE sa.employee_id = e.id AND sa.valid @> $2::date LIMIT 1) AS basic
         FROM employee e JOIN leave_ledger ll ON ll.employee_id = e.id AND ll.effective_date <= $2 JOIN leave_type lt ON lt.id = ll.leave_type_id AND lt.encashable
        WHERE e.company_id = $1 AND e.status = 'active' AND (e.exit_date IS NULL OR e.exit_date >= $2)
        GROUP BY e.id, e.emp_code, e.name_en, lt.code, lt.name_en, lt.sort_order HAVING sum(ll.delta_days) > 0 ORDER BY e.emp_code, lt.sort_order`, [companyId, asOf]);
    const out = rows.rows.map((x) => {
      const rate = x.basic ? D(x.basic).div(divisor) : null;
      return { empCode: x.emp_code, name: x.name_en, department: x.department ?? 'No department', leaveType: x.leave_name, balance: D(x.balance).toFixed(2),
        dailyRate: rate ? rate.toDecimalPlaces(2).toFixed(2) : '', amount: rate ? rate.times(x.balance).toDecimalPlaces(2).toFixed(2) : '', note: x.basic ? '' : 'no salary on this date' };
    });
    const total = out.reduce((a, x) => a.plus(x.amount || 0), D(0));
    const byDepartment = [...out.reduce((m, x) => m.set(x.department, (m.get(x.department) ?? D(0)).plus(x.amount || 0)), new Map<string, Decimal>())]
      .map(([department, amount2]) => ({ department, amount: amount2.toFixed(2) })).sort((a, b) => D(b.amount).comparedTo(a.amount));
    if (q.format === 'json') return res.json({ asOf, divisor, total: total.toFixed(2), byDepartment, rows: out });
    return sendReport(res, 'csv', `leave-liability-${asOf}`, [
      { key: 'empCode', header: 'Employee code' }, { key: 'name', header: 'Name' }, { key: 'department', header: 'Department' }, { key: 'leaveType', header: 'Leave type' },
      { key: 'balance', header: 'Balance (days)' }, { key: 'dailyRate', header: 'Daily rate' }, { key: 'amount', header: 'Amount' }, { key: 'note', header: 'Note' },
    ], out);
  });

  /** Fiscal years with data, for the pickers. */
  r.get('/compliance/years', hr, async (req, res) => {
    const r2 = await db.query<{ fy: number }>(
      `SELECT DISTINCT CASE WHEN p.bs_month >= 4 THEN p.bs_year ELSE p.bs_year - 1 END AS fy FROM payroll_period p WHERE p.company_id = $1 ORDER BY fy DESC`, [companyIdOf(req)]);
    const cur = currentFiscalYearStart();
    const years = [...new Set([cur, ...r2.rows.map((x) => x.fy)])].sort((a, b) => b - a);
    res.json({ current: cur, years, currentMonth: adToBs(nptToday()) , fiscalYearOf: fiscalYearOf(adToBs(nptToday())).startYear });
  });

  return r;
}
