import { z } from 'zod';
import type { FieldCipher } from '../../crypto.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { isoDate } from '../../validation.js';
import { recordFieldChanges, type FieldChange } from './history.js';

/**
 * The employee master beyond the basics, one schema per profile section. Flat sections are partial (an omitted key
 * is unchanged, null clears it); list sections (emergency, family, education, experience) replace the whole list.
 */
const text = (max: number) => z.string().trim().min(1).max(max);
const optText = (max: number) => z.string().trim().max(max).nullable().optional();
const phone = z.string().trim().regex(/^\+?[0-9 ()-]{6,20}$/, 'phone number');

export const addressSchema = z.object({
  province: optText(60), district: optText(60), municipality: optText(80),
  ward: z.number().int().min(1).max(40).nullable().optional(), street: optText(160),
}).strict();

export const BLOOD_GROUPS = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'] as const;
export const FAMILY_RELATIONS = ['spouse', 'father', 'mother', 'son', 'daughter', 'brother', 'sister', 'father_in_law', 'mother_in_law',
  'grandfather', 'grandmother', 'other'] as const;
export const EDUCATION_LEVELS = ['see', 'plus_two', 'diploma', 'bachelor', 'master', 'mphil', 'phd', 'other'] as const;

const year = z.number().int().min(1950).max(2200);

export const sectionSchemas = {
  personal: z.object({
    gender: z.enum(['male', 'female', 'other']).nullable().optional(),
    dob: isoDate.nullable().optional(),
    maritalStatus: z.enum(['single', 'couple']).nullable().optional(),
    bloodGroup: z.enum(BLOOD_GROUPS).nullable().optional(),
    nationality: optText(60),
  }).strict(),
  contact: z.object({
    personalEmail: z.email().max(160).nullable().optional(),
    personalPhone: phone.nullable().optional(),
    workPhone: phone.nullable().optional(),
    permanentAddress: addressSchema.nullable().optional(),
    temporaryAddress: addressSchema.nullable().optional(),
  }).strict(),
  bank: z.object({
    bankCode: optText(20),
    bankBranch: optText(80),
    bankAccount: z.string().trim().regex(/^[0-9A-Za-z-]{4,40}$/, 'account number').optional(),
  }).strict(),
  ids: z.object({
    pan: z.string().trim().regex(/^\d{9}$/, 'PAN must be 9 digits').nullable().optional(),
    citizenshipNo: z.string().trim().regex(/^[0-9A-Za-z/ -]{3,30}$/, 'citizenship number').nullable().optional(),
    citizenshipDistrict: optText(60),
    citizenshipIssuedOn: isoDate.nullable().optional(),
    passportNo: z.string().trim().regex(/^[0-9A-Za-z]{5,20}$/, 'passport number').nullable().optional(),
    passportExpiry: isoDate.nullable().optional(),
    licenceNo: z.string().trim().regex(/^[0-9A-Za-z/ -]{4,30}$/, 'licence number').nullable().optional(),
    licenceExpiry: isoDate.nullable().optional(),
    ssfId: optText(20),
    citId: optText(40),
    pfId: optText(40),
  }).strict(),
  emergency: z.object({
    contacts: z.array(z.object({ name: text(120), relation: text(40), phone, altPhone: phone.nullable().optional(), address: optText(200) }).strict()).max(5),
  }).strict(),
  family: z.object({
    members: z.array(z.object({
      name: text(120), relation: z.enum(FAMILY_RELATIONS), dob: isoDate.nullable().optional(), occupation: optText(80), phone: phone.nullable().optional(),
      isDependent: z.boolean().default(false), isNominee: z.boolean().default(false), nomineeShare: z.number().gt(0).max(100).nullable().optional(),
    }).strict()).max(20),
  }).strict().superRefine((v, ctx) => {
    const nominees = v.members.filter((m) => m.isNominee);
    if (nominees.some((m) => !m.nomineeShare)) ctx.addIssue({ code: 'custom', path: ['members'], message: 'every nominee needs a share' });
    else if (nominees.length && Math.abs(nominees.reduce((s, m) => s + (m.nomineeShare ?? 0), 0) - 100) > 0.001) {
      ctx.addIssue({ code: 'custom', path: ['members'], message: 'nominee shares must add up to 100%' });
    }
    if (v.members.some((m) => !m.isNominee && m.nomineeShare)) ctx.addIssue({ code: 'custom', path: ['members'], message: 'only nominees have a share' });
  }),
  education: z.object({
    items: z.array(z.object({
      level: z.enum(EDUCATION_LEVELS), degree: optText(120), field: optText(120), institution: text(160), board: optText(160),
      startYear: year.nullable().optional(), endYear: year.nullable().optional(), result: optText(40),
    }).strict().refine((e) => !e.startYear || !e.endYear || e.endYear >= e.startYear, { message: 'end year is before start year' })).max(15),
  }).strict(),
  experience: z.object({
    items: z.array(z.object({
      organisation: text(160), designation: optText(120), fromDate: isoDate.nullable().optional(), toDate: isoDate.nullable().optional(),
      responsibilities: optText(1000), reasonForLeaving: optText(300),
    }).strict().refine((e) => !e.fromDate || !e.toDate || e.toDate >= e.fromDate, { message: 'end date is before start date' })).max(20),
  }).strict(),
  skills: z.object({
    skills: z.array(z.string().trim().min(1).max(60)).max(30),
    languages: z.array(z.string().trim().min(1).max(40)).max(15),
  }).strict(),
} as const;

export type Section = keyof typeof sectionSchemas;
export const SECTIONS = Object.keys(sectionSchemas) as Section[];
export const SECTION_LABEL: Record<Section, string> = {
  personal: 'Personal details', contact: 'Contact and address', bank: 'Bank account', ids: 'ID documents', emergency: 'Emergency contacts',
  family: 'Family and nominees', education: 'Education', experience: 'Previous employment', skills: 'Skills and languages',
};
/** Sections whose direct edits by HR need a second HR's approval when people.policy.makerChecker is on. */
export const SENSITIVE_SECTIONS: Section[] = ['bank', 'ids'];

export function parseSection(section: string, value: unknown): { section: Section; value: Record<string, unknown> } {
  if (!SECTIONS.includes(section as Section)) throw new NotFoundError(`unknown profile section ${section}`);
  const s = section as Section;
  return { section: s, value: (sectionSchemas[s] as z.ZodType).parse(value) as Record<string, unknown> };
}

// ---------------------------------------------------------------- reading

const FIELD_LABEL: Record<string, string> = {
  gender: 'Gender', dob: 'Date of birth', maritalStatus: 'Marital status', bloodGroup: 'Blood group', nationality: 'Nationality',
  personalEmail: 'Personal email', personalPhone: 'Personal phone', workPhone: 'Work phone', permanentAddress: 'Permanent address', temporaryAddress: 'Temporary address',
  bankCode: 'Bank', bankBranch: 'Bank branch', bankAccount: 'Bank account', pan: 'PAN', citizenshipNo: 'Citizenship number',
  citizenshipDistrict: 'Citizenship issue district', citizenshipIssuedOn: 'Citizenship issue date', passportNo: 'Passport number', passportExpiry: 'Passport expiry',
  licenceNo: 'Driving licence number', licenceExpiry: 'Driving licence expiry', ssfId: 'SSF ID', citId: 'CIT ID', pfId: 'PF number',
  contacts: 'Emergency contacts', members: 'Family members', skills: 'Skills', languages: 'Languages',
};
const LIST_LABEL: Partial<Record<Section, string>> = { education: 'Education', experience: 'Previous employment' };
export const fieldLabel = (f: string, section?: Section) => (f === 'items' && section ? LIST_LABEL[section] : undefined) ?? FIELD_LABEL[f] ?? f;

/** Encrypted ID numbers: column and AAD suffix. PAN and bank keep the AADs the employees module already uses. */
const SECRET_COLS = {
  pan: { col: 'pan_enc', aad: 'pan' }, bankAccount: { col: 'bank_account_enc', aad: 'bank' },
  citizenshipNo: { col: 'citizenship_no_enc', aad: 'citizenship' }, passportNo: { col: 'passport_no_enc', aad: 'passport' }, licenceNo: { col: 'licence_no_enc', aad: 'licence' },
} as const;
type SecretField = keyof typeof SECRET_COLS;
export const SECRET_FIELDS = Object.keys(SECRET_COLS) as SecretField[];
const secretAad = (employeeId: string, f: SecretField) => `employee:${employeeId}:${SECRET_COLS[f].aad}`;
const isSecret = (f: string): f is SecretField => (SECRET_FIELDS as string[]).includes(f);

export function mask(v: string | null | undefined): string | null {
  if (!v) return null;
  return v.length <= 4 ? '••••' : `•••• ${v.slice(-4)}`;
}

interface EmpRow {
  id: string; gender: string | null; dob: string | null; marital_status: string | null; blood_group: string | null; nationality: string | null;
  personal_email: string | null; personal_phone: string | null; work_phone: string | null; permanent_address: Record<string, unknown> | null; temporary_address: Record<string, unknown> | null;
  bank_code: string | null; bank_branch: string | null; bank_account_last4: string | null; ssf_id: string | null; cit_id: string | null; pf_id: string | null;
  citizenship_district: string | null; citizenship_issued_on: string | null; passport_expiry: string | null; licence_expiry: string | null;
  skills: string[]; languages: string[];
  pan_enc: Buffer | null; bank_account_enc: Buffer | null; citizenship_no_enc: Buffer | null; passport_no_enc: Buffer | null; licence_no_enc: Buffer | null;
}

async function loadEmp(db: Queryable, employeeId: string): Promise<EmpRow> {
  const r = await db.query<EmpRow>(
    `SELECT id, gender, dob, marital_status, blood_group, nationality, personal_email, personal_phone, work_phone, permanent_address, temporary_address,
            bank_code, bank_branch, bank_account_last4, ssf_id, cit_id, pf_id, citizenship_district, citizenship_issued_on, passport_expiry, licence_expiry,
            skills, languages, pan_enc, bank_account_enc, citizenship_no_enc, passport_no_enc, licence_no_enc
       FROM employee WHERE id = $1`, [employeeId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

export type Profile = Record<Section, Record<string, unknown>>;

/** The whole profile, section by section. Encrypted numbers are masked unless `reveal` (the caller audits that). */
export async function readProfile(db: Queryable, cipher: FieldCipher, employeeId: string, opts: { reveal?: boolean } = {}): Promise<Profile> {
  const e = await loadEmp(db, employeeId);
  const secret = (f: SecretField): string | null => {
    const blob = e[SECRET_COLS[f].col];
    if (!blob) return null;
    const plain = cipher.decrypt(blob, secretAad(employeeId, f));
    return opts.reveal ? plain : mask(plain);
  };
  // One after another: `db` may be a transaction client, which runs one query at a time.
  const contacts = await db.query('SELECT name, relation, phone, alt_phone, address FROM employee_emergency_contact WHERE employee_id = $1 ORDER BY sort, name', [employeeId]);
  const family = await db.query('SELECT name, relation, dob, occupation, phone, is_dependent, is_nominee, nominee_share FROM employee_family WHERE employee_id = $1 ORDER BY sort, name', [employeeId]);
  const education = await db.query('SELECT level, degree, field, institution, board, start_year, end_year, result FROM employee_education WHERE employee_id = $1 ORDER BY sort', [employeeId]);
  const experience = await db.query('SELECT organisation, designation, from_date, to_date, responsibilities, reason_for_leaving FROM employee_experience WHERE employee_id = $1 ORDER BY sort', [employeeId]);
  return {
    personal: { gender: e.gender, dob: e.dob, maritalStatus: e.marital_status, bloodGroup: e.blood_group, nationality: e.nationality },
    contact: { personalEmail: e.personal_email, personalPhone: e.personal_phone, workPhone: e.work_phone, permanentAddress: e.permanent_address, temporaryAddress: e.temporary_address },
    bank: { bankCode: e.bank_code, bankBranch: e.bank_branch, bankAccount: secret('bankAccount') },
    ids: {
      pan: secret('pan'), citizenshipNo: secret('citizenshipNo'), citizenshipDistrict: e.citizenship_district, citizenshipIssuedOn: e.citizenship_issued_on,
      passportNo: secret('passportNo'), passportExpiry: e.passport_expiry, licenceNo: secret('licenceNo'), licenceExpiry: e.licence_expiry,
      ssfId: e.ssf_id, citId: e.cit_id, pfId: e.pf_id,
    },
    emergency: { contacts: contacts.rows.map((c) => ({ name: c.name, relation: c.relation, phone: c.phone, altPhone: c.alt_phone, address: c.address })) },
    family: {
      members: family.rows.map((m) => ({
        name: m.name, relation: m.relation, dob: m.dob, occupation: m.occupation, phone: m.phone, isDependent: m.is_dependent, isNominee: m.is_nominee,
        nomineeShare: m.nominee_share === null ? null : Number(m.nominee_share),
      })),
    },
    education: { items: education.rows.map((x) => ({ level: x.level, degree: x.degree, field: x.field, institution: x.institution, board: x.board, startYear: x.start_year, endYear: x.end_year, result: x.result })) },
    experience: { items: experience.rows.map((x) => ({ organisation: x.organisation, designation: x.designation, fromDate: x.from_date, toDate: x.to_date, responsibilities: x.responsibilities, reasonForLeaving: x.reason_for_leaving })) },
    skills: { skills: e.skills, languages: e.languages },
  };
}

// ---------------------------------------------------------------- diffing

/** A readable one-line form of any section value, for history and the approval inbox. */
export function display(field: string, v: unknown): string | null {
  if (v === null || v === undefined || v === '') return null;
  if (Array.isArray(v)) {
    if (!v.length) return null;
    if (typeof v[0] !== 'object') return v.join(', ');
    return v.map((x) => {
      const o = x as Record<string, unknown>;
      const head = String(o.name ?? o.institution ?? o.organisation ?? '');
      const tail = String(o.relation ?? o.degree ?? o.level ?? o.designation ?? '').replace(/_/g, ' ');
      const share = o.isNominee && o.nomineeShare ? `, nominee ${o.nomineeShare}%` : '';
      return tail ? `${head} (${tail}${share})` : head;
    }).join('; ');
  }
  if (typeof v === 'object') {
    const a = v as Record<string, unknown>;
    return [a.street, a.ward ? `Ward ${a.ward}` : null, a.municipality, a.district, a.province].filter((x) => x !== null && x !== undefined && x !== '').join(', ') || null;
  }
  if (field === 'gender' || field === 'maritalStatus') return String(v).charAt(0).toUpperCase() + String(v).slice(1);
  return String(v);
}

function normalise(v: unknown): unknown {
  if (v === undefined || v === '') return null;
  if (Array.isArray(v)) return v.map(normalise);
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, normalise(x)] as const)
      .filter(([, x]) => x !== null && x !== false).sort(([a], [b]) => a.localeCompare(b)));
  }
  return v;
}

/**
 * Field-by-field differences between the stored section and a proposed value. Secret numbers are compared in clear
 * and shown masked unless `reveal`.
 */
export async function diffSection(db: Queryable, cipher: FieldCipher, employeeId: string, section: Section, value: Record<string, unknown>, opts: { reveal?: boolean } = {}): Promise<FieldChange[]> {
  const current = (await readProfile(db, cipher, employeeId, { reveal: true }))[section];
  const out: FieldChange[] = [];
  for (const [k, next] of Object.entries(value)) {
    if (next === undefined) continue;
    const before = current[k];
    if (JSON.stringify(normalise(before)) === JSON.stringify(normalise(next))) continue;
    const hide = isSecret(k) && !opts.reveal;
    const from = display(k, before); const to = display(k, next);
    out.push({ field: k, label: fieldLabel(k, section), from: hide ? mask(from) : from, to: hide ? mask(to) : to });
  }
  return out;
}

// ---------------------------------------------------------------- writing

export interface ApplyInput {
  companyId: string; employeeId: string; section: Section; value: Record<string, unknown>;
  actorId: string | null; source: 'hr_edit' | 'change_request'; refId?: string | null;
}

/** Write a validated section value and record field history. Call inside a transaction. Returns what changed. */
export async function applySection(db: Queryable, cipher: FieldCipher, a: ApplyInput): Promise<FieldChange[]> {
  const changes = await diffSection(db, cipher, a.employeeId, a.section, a.value);
  if (!changes.length) return [];
  const v = a.value;
  const id = a.employeeId;
  const set = async (cols: Record<string, unknown>) => {
    const entries = Object.entries(cols).filter(([, x]) => x !== undefined);
    if (!entries.length) return;
    await db.query(`UPDATE employee SET ${entries.map(([c], i) => `${c} = $${i + 2}`).join(', ')}, updated_at = now() WHERE id = $1`, [id, ...entries.map(([, x]) => x)]);
  };
  const enc = (f: SecretField, plain: unknown) => plain === null ? null : plain === undefined ? undefined : cipher.encrypt(String(plain), secretAad(id, f));
  const json = (x: unknown) => x === undefined ? undefined : x === null ? null : JSON.stringify(x);
  switch (a.section) {
    case 'personal':
      await set({ gender: v.gender, dob: v.dob, marital_status: v.maritalStatus, blood_group: v.bloodGroup, nationality: v.nationality });
      break;
    case 'contact':
      await set({ personal_email: v.personalEmail, personal_phone: v.personalPhone, work_phone: v.workPhone,
        permanent_address: json(v.permanentAddress), temporary_address: json(v.temporaryAddress) });
      break;
    case 'bank':
      await set({ bank_code: v.bankCode, bank_branch: v.bankBranch, bank_account_enc: enc('bankAccount', v.bankAccount),
        bank_account_last4: v.bankAccount === undefined ? undefined : String(v.bankAccount).slice(-4) });
      break;
    case 'ids': {
      if (typeof v.pan === 'string') {
        const owner = await db.query<{ emp_code: string }>('SELECT emp_code FROM employee WHERE company_id = $1 AND pan_bidx = $2 AND id <> $3 LIMIT 1', [a.companyId, cipher.blindIndex(v.pan), id]);
        if (owner.rows[0]) throw new ConflictError(`this PAN is already recorded for employee ${owner.rows[0].emp_code}`);
      }
      await set({
        pan_enc: enc('pan', v.pan), pan_bidx: v.pan === undefined ? undefined : v.pan === null ? null : cipher.blindIndex(String(v.pan)),
        citizenship_no_enc: enc('citizenshipNo', v.citizenshipNo), citizenship_district: v.citizenshipDistrict, citizenship_issued_on: v.citizenshipIssuedOn,
        passport_no_enc: enc('passportNo', v.passportNo), passport_expiry: v.passportExpiry, licence_no_enc: enc('licenceNo', v.licenceNo), licence_expiry: v.licenceExpiry,
        ssf_id: v.ssfId, cit_id: v.citId, pf_id: v.pfId,
      });
      break;
    }
    case 'emergency':
      await db.query('DELETE FROM employee_emergency_contact WHERE employee_id = $1', [id]);
      for (const [i, c] of (v.contacts as Array<Record<string, unknown>>).entries()) {
        await db.query('INSERT INTO employee_emergency_contact (employee_id, name, relation, phone, alt_phone, address, sort) VALUES ($1, $2, $3, $4, $5, $6, $7)',
          [id, c.name, c.relation, c.phone, c.altPhone ?? null, c.address ?? null, i]);
      }
      break;
    case 'family':
      await db.query('DELETE FROM employee_family WHERE employee_id = $1', [id]);
      for (const [i, m] of (v.members as Array<Record<string, unknown>>).entries()) {
        await db.query(`INSERT INTO employee_family (employee_id, name, relation, dob, occupation, phone, is_dependent, is_nominee, nominee_share, sort)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
          [id, m.name, m.relation, m.dob ?? null, m.occupation ?? null, m.phone ?? null, m.isDependent ?? false, m.isNominee ?? false, m.isNominee ? m.nomineeShare : null, i]);
      }
      break;
    case 'education':
      await db.query('DELETE FROM employee_education WHERE employee_id = $1', [id]);
      for (const [i, x] of (v.items as Array<Record<string, unknown>>).entries()) {
        await db.query(`INSERT INTO employee_education (employee_id, level, degree, field, institution, board, start_year, end_year, result, sort)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
          [id, x.level, x.degree ?? null, x.field ?? null, x.institution, x.board ?? null, x.startYear ?? null, x.endYear ?? null, x.result ?? null, i]);
      }
      break;
    case 'experience':
      await db.query('DELETE FROM employee_experience WHERE employee_id = $1', [id]);
      for (const [i, x] of (v.items as Array<Record<string, unknown>>).entries()) {
        await db.query(`INSERT INTO employee_experience (employee_id, organisation, designation, from_date, to_date, responsibilities, reason_for_leaving, sort)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
          [id, x.organisation, x.designation ?? null, x.fromDate ?? null, x.toDate ?? null, x.responsibilities ?? null, x.reasonForLeaving ?? null, i]);
      }
      break;
    case 'skills':
      await set({ skills: v.skills, languages: v.languages });
      break;
    default:
      throw new BadRequestError('unknown section');
  }
  await recordFieldChanges(db, { companyId: a.companyId, employeeId: id, changes, source: a.source, refId: a.refId ?? null, actorId: a.actorId });
  return changes;
}
