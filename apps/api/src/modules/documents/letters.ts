import { readFileSync } from 'node:fs';
import { adToBs } from '@hajiri/bs-calendar';
import { amountInWordsEn, D, formatNpr } from '@hajiri/payroll-engine';
import { bsLabel } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { nptToday } from '../workflow/service.js';

/**
 * Letter templates: plain text with {{placeholders}}, blank lines between paragraphs, "- " bullet lines and **bold**.
 * Nothing else is interpreted; every value and every piece of template text is HTML-escaped, so a template or an
 * employee's name can never inject markup into a printed letter.
 */
export const PLACEHOLDERS: Array<{ key: string; label: string }> = [
  { key: 'employee.name', label: 'Employee full name' },
  { key: 'employee.first_name', label: 'Employee first name' },
  { key: 'employee.code', label: 'Employee code' },
  { key: 'employee.title', label: 'Mr. / Ms. (from gender)' },
  { key: 'employee.email', label: 'Work email' },
  { key: 'employee.phone', label: 'Phone' },
  { key: 'designation', label: 'Designation' },
  { key: 'grade', label: 'Grade' },
  { key: 'department', label: 'Department' },
  { key: 'branch', label: 'Branch' },
  { key: 'manager.name', label: 'Line manager' },
  { key: 'employment_type', label: 'Employment type' },
  { key: 'join_date', label: 'Joining date (BS)' },
  { key: 'exit_date', label: 'Last working date (BS)' },
  { key: 'probation_end', label: 'Probation end date (BS)' },
  { key: 'service_period', label: 'Length of service' },
  { key: 'salary.gross', label: 'Monthly salary (NPR)' },
  { key: 'salary.gross_words', label: 'Monthly salary in words' },
  { key: 'salary.annual', label: 'Annual salary (NPR)' },
  { key: 'today', label: "Today's date (BS)" },
  { key: 'company.name', label: 'Company name' },
  { key: 'company.address', label: 'Company address' },
  { key: 'company.pan', label: 'Company PAN' },
  { key: 'signatory.name', label: 'Signatory name' },
  { key: 'signatory.title', label: 'Signatory title' },
  { key: 'ref_no', label: 'Letter reference number' },
  { key: 'purpose', label: 'Purpose (from the request)' },
  { key: 'addressed_to', label: 'Addressed to (from the request)' },
];
const KEYS = new Set(PLACEHOLDERS.map((p) => p.key));
const TOKEN = /\{\{\s*([a-z_.]+)\s*\}\}/g;

/** Placeholders used in a template that are not on the whitelist (a typo would otherwise print blank). */
export function unknownPlaceholders(...texts: string[]): string[] {
  const bad = new Set<string>();
  for (const t of texts) {
    for (const m of t.matchAll(TOKEN)) if (!KEYS.has(m[1]!)) bad.add(m[1]!);
    // Stray braces such as "{{name" or "{{ Name }}" are almost always mistakes.
    const stripped = t.replace(TOKEN, '');
    const stray = /\{\{[^}\n]{0,30}|[^{\n]{0,30}\}\}/.exec(stripped);
    if (stray) bad.add(stray[0].trim());
  }
  return [...bad];
}

export const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

export type LetterValues = Partial<Record<string, string | null>>;

const inline = (s: string) => s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');

/** Fill placeholders into escaped text; values have their markdown characters neutralised so they stay literal. */
function fill(text: string, values: LetterValues, missing: Set<string>, mark: boolean): string {
  // Bold markers are resolved on the template text first, with placeholders swapped for inert sentinels.
  const tokens: string[] = [];
  const withSentinels = text.replace(TOKEN, (_m, key: string) => { tokens.push(key); return `\u0000${tokens.length - 1}\u0000`; });
  const html = inline(esc(withSentinels));
  return html.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => {
    const key = tokens[Number(i)]!;
    const v = values[key];
    if (v === undefined || v === null || v === '') {
      missing.add(key);
      return mark ? `<span class="missing">[${esc(key)}]</span>` : '';
    }
    return esc(v).replace(/\*/g, '&#42;');
  });
}

/** Render the body to HTML paragraphs and lists. `mark` highlights missing values (preview only). */
export function renderBody(body: string, values: LetterValues, opts: { mark?: boolean } = {}): { html: string; missing: string[] } {
  const missing = new Set<string>();
  const clean = body.replace(/\r\n?/g, '\n').replace(/\u0000/g, '');
  const blocks = clean.split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  const html = blocks.map((b) => {
    const lines = b.split('\n');
    const bullet = /^\s*[-•]\s+/;
    if (lines.some((l) => bullet.test(l))) {
      // Leading text lines become a paragraph, bullet lines a list.
      const out: string[] = [];
      let list: string[] = [];
      const flush = () => { if (list.length) { out.push(`<ul>${list.join('')}</ul>`); list = []; } };
      let para: string[] = [];
      const flushPara = () => { if (para.length) { out.push(`<p>${para.join('<br>')}</p>`); para = []; } };
      for (const l of lines) {
        if (bullet.test(l)) { flushPara(); list.push(`<li>${fill(l.replace(bullet, ''), values, missing, !!opts.mark)}</li>`); }
        else { flush(); para.push(fill(l, values, missing, !!opts.mark)); }
      }
      flushPara(); flush();
      return out.join('\n');
    }
    return `<p>${lines.map((l) => fill(l, values, missing, !!opts.mark)).join('<br>')}</p>`;
  }).join('\n');
  return { html, missing: [...missing] };
}

export function renderSubject(subject: string, values: LetterValues, missing: Set<string>, mark = false): string {
  return fill(subject.replace(/\u0000/g, ''), values, missing, mark);
}

// ---------------------------------------------------------------- letterhead
let defaultLogo: string | null | undefined;
function brandLogo(): string | null {
  if (defaultLogo !== undefined) return defaultLogo;
  try {
    const svg = readFileSync(new URL('../../../../web/public/brand/cv-wordmark-carbon.svg', import.meta.url));
    defaultLogo = `data:image/svg+xml;base64,${svg.toString('base64')}`;
  } catch { defaultLogo = null; }
  return defaultLogo;
}

export interface LetterLayout {
  company: { name: string; address: string | null; pan: string | null; logoDataUrl: string | null };
  refNo: string;
  dateBs: string;
  subjectHtml: string;
  bodyHtml: string;
  signatory: { name: string | null; title: string | null };
  draft?: boolean;
  voided?: string | null;
}

/** A4 printable letter with the company letterhead, reference and date, subject, body and signature block. */
export function renderLetterHtml(l: LetterLayout): string {
  const logo = l.company.logoDataUrl && /^data:image\/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(l.company.logoDataUrl) ? l.company.logoDataUrl : brandLogo();
  const wm = l.voided ? 'VOID' : l.draft ? 'DRAFT' : null;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${esc(l.refNo)}</title>
<style>
@page{size:A4;margin:18mm 20mm}
body{font-family:Manrope,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13.5px;color:#17191e;margin:32px auto;max-width:760px;line-height:1.65;padding:0 28px;background:#fff}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:14px;border-bottom:2px solid #12773a}
.head img{height:34px;display:block}.co{text-align:right;font-size:12px;color:#4b505a}.co b{display:block;font-size:15px;color:#17191e}
.meta{display:flex;justify-content:space-between;margin:22px 0 18px;font-size:12.5px;color:#4b505a}.meta b{color:#17191e;font-weight:600}
h1{font-size:15px;text-align:center;text-decoration:underline;text-underline-offset:4px;margin:8px 0 18px;font-weight:700}
p{margin:0 0 12px;text-align:justify}ul{margin:0 0 12px 18px;padding:0}li{margin:2px 0}
.sign{margin-top:46px}.sign .line{width:200px;border-top:1px solid #17191e;margin-bottom:6px}.sign b{display:block}
.foot{margin-top:40px;font-size:10.5px;color:#8a8f99;border-top:1px solid #e6e7ea;padding-top:8px}
.missing{background:#fff1c2;color:#8a5a00;border-radius:3px;padding:0 3px;font-family:ui-monospace,monospace;font-size:11.5px}
.wm{position:fixed;top:38%;left:0;right:0;text-align:center;font-size:110px;font-weight:800;color:rgba(200,30,30,.10);transform:rotate(-24deg);pointer-events:none;letter-spacing:.1em}
@media print{body{margin:0 auto;padding:0}}
</style></head><body>
${wm ? `<div class="wm">${wm}</div>` : ''}
<div class="head">${logo ? `<img src="${esc(logo)}" alt="">` : '<span></span>'}<div class="co"><b>${esc(l.company.name)}</b>${l.company.address ? esc(l.company.address) : ''}${l.company.pan ? `<br>PAN ${esc(l.company.pan)}` : ''}</div></div>
<div class="meta"><div>Ref: <b>${esc(l.refNo)}</b></div><div>Date: <b>${esc(l.dateBs)}</b></div></div>
<h1>${l.subjectHtml}</h1>
${l.bodyHtml}
<div class="sign"><div class="line"></div><b>${esc(l.signatory.name ?? '')}</b>${esc(l.signatory.title ?? '')}<br>${esc(l.company.name)}</div>
<div class="foot">Reference ${esc(l.refNo)}. Issued from Hajiri; the reference can be verified with ${esc(l.company.name)} HR.${l.voided ? ` This letter was voided: ${esc(l.voided)}.` : ''}</div>
</body></html>`;
}

// ---------------------------------------------------------------- values for an employee
export interface LetterSettings { signatoryName: string | null; signatoryTitle: string | null; refPrefix: string }
export const DEFAULT_LETTER_SETTINGS: LetterSettings = { signatoryName: null, signatoryTitle: 'Human Resources', refPrefix: 'HR' };

export async function letterSettings(db: Queryable, companyId: string): Promise<LetterSettings> {
  const r = await db.query<{ value: Partial<LetterSettings> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'documents.letters'`, [companyId]);
  return { ...DEFAULT_LETTER_SETTINGS, ...(r.rows[0]?.value ?? {}) };
}

export interface CompanyHead { name: string; address: string | null; pan: string | null; logoDataUrl: string | null }
export async function companyHead(db: Queryable, companyId: string): Promise<CompanyHead> {
  const c = await db.query<{ name_en: string; value: Record<string, string | null> | null }>(
    `SELECT c.name_en, s.value FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'company.profile' WHERE c.id = $1`, [companyId]);
  const row = c.rows[0];
  const p = row?.value ?? {};
  return { name: p.nameEn || row?.name_en || 'Company', address: p.address ?? null, pan: p.pan ?? null, logoDataUrl: p.logoDataUrl ?? null };
}

const TYPE_LABEL: Record<string, string> = { regular: 'regular', work_based: 'work-based', time_based: 'time-based', casual: 'casual', part_time: 'part-time' };

export function servicePeriod(from: string | null, to: string): string | null {
  if (!from) return null;
  const a = new Date(`${from}T00:00:00Z`); const b = new Date(`${to}T00:00:00Z`);
  if (b < a) return null;
  let months = (b.getUTCFullYear() - a.getUTCFullYear()) * 12 + (b.getUTCMonth() - a.getUTCMonth());
  if (b.getUTCDate() < a.getUTCDate()) months -= 1;
  const y = Math.floor(months / 12); const m = months % 12;
  const parts = [y ? `${y} year${y === 1 ? '' : 's'}` : '', m ? `${m} month${m === 1 ? '' : 's'}` : ''].filter(Boolean);
  return parts.length ? parts.join(' and ') : 'less than a month';
}

export function bsYearOf(ad: string): number {
  try { return adToBs(ad).year; } catch { return Number(ad.slice(0, 4)) + 57; }
}

/** Every placeholder value for an employee (company-scoped; returns null if the employee is not in the company). */
export async function employeeLetterValues(db: Queryable, companyId: string, employeeId: string, extra: { refNo?: string; purpose?: string | null; addressedTo?: string | null } = {}): Promise<LetterValues | null> {
  const today = nptToday();
  const r = await db.query<{
    name_en: string; emp_code: string; gender: string | null; email: string | null; phone: string | null; designation: string | null; grade: string | null;
    join_date: string | null; exit_date: string | null; probation_end: string | null; dept: string | null; branch: string | null; manager: string | null; employment_type: string | null;
  }>(
    `SELECT e.name_en, e.emp_code, e.gender, e.email, e.phone, e.designation, e.grade, e.join_date::text AS join_date, e.exit_date::text AS exit_date,
            coalesce(a.probation_end, e.probation_end)::text AS probation_end, ou.name_en AS dept, b.name_en AS branch, m.name_en AS manager, a.employment_type
       FROM employee e
       LEFT JOIN LATERAL (SELECT * FROM employee_assignment x WHERE x.employee_id = e.id
                           ORDER BY (x.valid @> $3::date) DESC, upper(x.valid) DESC NULLS FIRST LIMIT 1) a ON true
       LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
       LEFT JOIN branch b ON b.id = a.branch_id
       LEFT JOIN employee m ON m.id = a.manager_id
      WHERE e.id = $1 AND e.company_id = $2`, [employeeId, companyId, today]);
  const e = r.rows[0];
  if (!e) return null;
  const sal = await db.query<{ base_amounts: Record<string, string> }>(
    `SELECT base_amounts FROM salary_assignment WHERE employee_id = $1 ORDER BY (valid @> $2::date) DESC, lower(valid) DESC LIMIT 1`, [employeeId, today]);
  let gross: string | null = null;
  if (sal.rows[0]) {
    let t = D(0);
    for (const v of Object.values(sal.rows[0].base_amounts ?? {})) { try { t = t.plus(D(v)); } catch { /* skip non-numeric */ } }
    if (t.gt(0)) gross = t.toFixed(2);
  }
  const head = await companyHead(db, companyId);
  const s = await letterSettings(db, companyId);
  return {
    'employee.name': e.name_en,
    'employee.first_name': e.name_en.split(/\s+/)[0] ?? e.name_en,
    'employee.code': e.emp_code,
    'employee.title': e.gender === 'male' ? 'Mr.' : e.gender === 'female' ? 'Ms.' : null,
    'employee.email': e.email,
    'employee.phone': e.phone,
    designation: e.designation,
    grade: e.grade,
    department: e.dept,
    branch: e.branch,
    'manager.name': e.manager,
    employment_type: e.employment_type ? TYPE_LABEL[e.employment_type] ?? e.employment_type : null,
    join_date: e.join_date ? bsLabel(e.join_date) : null,
    exit_date: e.exit_date ? bsLabel(e.exit_date) : null,
    probation_end: e.probation_end ? bsLabel(e.probation_end) : null,
    service_period: servicePeriod(e.join_date, e.exit_date && e.exit_date < today ? e.exit_date : today),
    'salary.gross': gross ? `NPR ${formatNpr(gross, 'latin')}` : null,
    'salary.gross_words': gross ? amountInWordsEn(gross) : null,
    'salary.annual': gross ? `NPR ${formatNpr(D(gross).times(12).toFixed(2), 'latin')}` : null,
    today: bsLabel(today),
    'company.name': head.name,
    'company.address': head.address,
    'company.pan': head.pan,
    'signatory.name': s.signatoryName,
    'signatory.title': s.signatoryTitle,
    ref_no: extra.refNo ?? null,
    purpose: extra.purpose ?? null,
    addressed_to: extra.addressedTo ?? null,
  };
}

// ---------------------------------------------------------------- built-in templates
export interface BuiltinTemplate { code: string; name: string; subject: string; body: string; requestable: boolean }

export const BUILTIN_TEMPLATES: BuiltinTemplate[] = [
  { code: 'appointment', name: 'Appointment letter', requestable: false, subject: 'Letter of appointment', body:
`Dear {{employee.title}} {{employee.name}},

We are pleased to appoint you as **{{designation}}** in the {{department}} department of {{company.name}}, effective from {{join_date}}.

The main terms of your appointment are:
- Employment type: {{employment_type}}
- Place of work: {{branch}}
- Reporting to: {{manager.name}}
- Monthly salary: {{salary.gross}} ({{salary.gross_words}})
- Probation period ending on {{probation_end}}

Your employment is governed by the Labour Act 2074, the Labour Rules 2075 and the company's policies as amended from time to time.

Please sign and return a copy of this letter as your acceptance. We look forward to working with you.` },
  { code: 'confirmation', name: 'Confirmation letter', requestable: false, subject: 'Confirmation of employment', body:
`Dear {{employee.title}} {{employee.name}},

Following a review of your performance during the probation period, we are pleased to confirm your employment as **{{designation}}** with {{company.name}}.

All other terms and conditions of your employment remain unchanged.

Congratulations, and thank you for your contribution so far.` },
  { code: 'promotion', name: 'Promotion letter', requestable: false, subject: 'Promotion', body:
`Dear {{employee.title}} {{employee.name}},

In recognition of your performance and contribution, we are pleased to promote you to the position of **{{designation}}** (grade {{grade}}) in the {{department}} department.

Your revised monthly salary will be {{salary.gross}}. All other terms of your employment remain unchanged.

Congratulations on this well-deserved promotion.` },
  { code: 'transfer', name: 'Transfer letter', requestable: false, subject: 'Transfer', body:
`Dear {{employee.title}} {{employee.name}},

You are hereby transferred to the {{department}} department at our {{branch}} office as **{{designation}}**, reporting to {{manager.name}}.

Please complete the handover of your current responsibilities and report to your new place of work as advised by HR. All other terms of your employment remain unchanged.` },
  { code: 'salary_increment', name: 'Salary increment letter', requestable: false, subject: 'Revision of salary', body:
`Dear {{employee.title}} {{employee.name}},

We are pleased to inform you that your monthly salary has been revised to **{{salary.gross}}** ({{salary.gross_words}}).

This revision reflects your contribution to {{company.name}}. All other terms of your employment remain unchanged.` },
  { code: 'experience', name: 'Experience letter', requestable: true, subject: 'Experience certificate', body:
`To whom it may concern,

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) worked with {{company.name}} as **{{designation}}** in the {{department}} department from {{join_date}} to {{exit_date}}, a period of {{service_period}}.

During this period we found {{employee.first_name}} sincere, hardworking and professional. We wish {{employee.first_name}} every success in the future.` },
  { code: 'relieving', name: 'Relieving letter', requestable: false, subject: 'Relieving letter', body:
`Dear {{employee.title}} {{employee.name}},

This is to confirm that you have been relieved of your duties as **{{designation}}** at {{company.name}} with effect from the close of business on {{exit_date}}, after completing the handover of your responsibilities.

We thank you for your service and wish you the very best.` },
  { code: 'salary_certificate', name: 'Salary certificate', requestable: true, subject: 'Salary certificate', body:
`To {{addressed_to}},

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) has been working with {{company.name}} as **{{designation}}** since {{join_date}}.

{{employee.first_name}}'s current monthly salary is {{salary.gross}} ({{salary.gross_words}}), an annual salary of {{salary.annual}}.

This certificate is issued on request for the purpose of {{purpose}}.` },
  { code: 'to_whom_it_may_concern', name: 'To whom it may concern', requestable: true, subject: 'To whom it may concern', body:
`To whom it may concern,

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) is a {{employment_type}} employee of {{company.name}}, working as **{{designation}}** in the {{department}} department since {{join_date}}.

This letter is issued on request for the purpose of {{purpose}}.` },
  { code: 'noc', name: 'No objection certificate', requestable: true, subject: 'No objection certificate', body:
`To {{addressed_to}},

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) is working with {{company.name}} as **{{designation}}** since {{join_date}}.

The company has no objection to {{employee.first_name}}'s request for the purpose of {{purpose}}.` },
  { code: 'warning', name: 'Warning letter', requestable: false, subject: 'Warning', body:
`Dear {{employee.title}} {{employee.name}},

This letter is a formal warning regarding {{purpose}}.

Such conduct is not consistent with the company's code of conduct. You are advised to correct this immediately. Any repetition may lead to further disciplinary action under the Labour Act 2074 and the company's policies.

Please acknowledge receipt of this letter.` },
];

export const DEFAULT_CATEGORIES: Array<{ code: string; name: string; employeeCanUpload: boolean; expiryRequired: boolean; visible: boolean }> = [
  { code: 'contract', name: 'Employment contract', employeeCanUpload: false, expiryRequired: false, visible: true },
  { code: 'citizenship', name: 'Citizenship certificate', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'pan_card', name: 'PAN card', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'academic', name: 'Academic certificate', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'experience', name: 'Experience letter', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'medical', name: 'Medical certificate', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'photo', name: 'Photograph', employeeCanUpload: true, expiryRequired: false, visible: true },
  { code: 'passport', name: 'Passport', employeeCanUpload: true, expiryRequired: true, visible: true },
  { code: 'driving_licence', name: 'Driving licence', employeeCanUpload: true, expiryRequired: true, visible: true },
  { code: 'other', name: 'Other', employeeCanUpload: true, expiryRequired: false, visible: true },
];

/** Idempotent per-company defaults: document categories, built-in letter templates and the letter-request workflow. */
export async function seedDocumentDefaults(db: Queryable, companyId: string): Promise<{ categories: number; templates: number }> {
  let categories = 0; let templates = 0;
  for (const [i, c] of DEFAULT_CATEGORIES.entries()) {
    const r = await db.query(
      `INSERT INTO document_category (company_id, code, name, employee_can_upload, expiry_required, visible_to_employee, sort_order)
       VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (company_id, code) DO NOTHING`, [companyId, c.code, c.name, c.employeeCanUpload, c.expiryRequired, c.visible, i]);
    categories += r.rowCount ?? 0;
  }
  for (const t of BUILTIN_TEMPLATES) {
    const r = await db.query(
      `INSERT INTO letter_template (company_id, code, name, subject, body, requestable, is_builtin) VALUES ($1, $2, $3, $4, $5, $6, true)
       ON CONFLICT (company_id, code) DO NOTHING`, [companyId, t.code, t.name, t.subject, t.body, t.requestable]);
    templates += r.rowCount ?? 0;
  }
  await db.query(`INSERT INTO workflow_definition (company_id, object_type, steps) VALUES ($1, 'letter_request', '[{"role":"hr"}]') ON CONFLICT (company_id, object_type) DO NOTHING`, [companyId]);
  return { categories, templates };
}

/** Next reference number for the company and BS year, e.g. HR/2083/0001. Call inside the issuing transaction. */
export async function nextRefNo(db: Queryable, companyId: string, prefix: string, today = nptToday()): Promise<string> {
  const year = bsYearOf(today);
  const r = await db.query<{ last_no: number }>(
    `INSERT INTO letter_sequence (company_id, bs_year, last_no) VALUES ($1, $2, 1)
     ON CONFLICT (company_id, bs_year) DO UPDATE SET last_no = letter_sequence.last_no + 1 RETURNING last_no`, [companyId, year]);
  const clean = prefix.replace(/[^A-Za-z0-9/_-]/g, '').replace(/\/+$/, '') || 'HR';
  return `${clean}/${year}/${String(r.rows[0]!.last_no).padStart(4, '0')}`;
}
