import { z } from 'zod';
import { BadRequestError } from '../../errors.js';

/**
 * Ad-hoc report builder: HR picks a dataset, columns, filters and a sort. Nothing the user sends ever becomes SQL text:
 * datasets, columns and operators are looked up in the whitelist below and every value is a bound parameter.
 */
type ColType = 'text' | 'number' | 'date' | 'bool';
interface AdhocColumn { label: string; sql: string; type: ColType }
interface AdhocDataset { label: string; description: string; from: string; columns: Record<string, AdhocColumn>; defaultColumns: string[] }

/** Joins giving `e` its placement today ($2) — branch b, department o, manager m. `$1` is the company. */
const PLACED = `employee e
  LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> $2::date
  LEFT JOIN branch b ON b.id = a.branch_id
  LEFT JOIN org_unit o ON o.id = a.org_unit_id
  LEFT JOIN employee m ON m.id = a.manager_id`;

const who: Record<string, AdhocColumn> = {
  emp_code: { label: 'Emp code', sql: 'e.emp_code', type: 'text' },
  name: { label: 'Name', sql: 'e.name_en', type: 'text' },
  department: { label: 'Department', sql: 'o.name_en', type: 'text' },
  branch: { label: 'Branch', sql: 'b.name_en', type: 'text' },
  employee_status: { label: 'Employee status', sql: 'e.status', type: 'text' },
};

export const ADHOC_DATASETS: Record<string, AdhocDataset> = {
  employees: {
    label: 'Employees', description: 'One row per employee with job, placement and personal details (no ID or bank numbers).',
    from: PLACED,
    defaultColumns: ['emp_code', 'name', 'designation', 'department', 'branch', 'join_date'],
    columns: {
      ...who,
      designation: { label: 'Designation', sql: 'e.designation', type: 'text' },
      grade: { label: 'Grade', sql: 'e.grade', type: 'text' },
      employment_type: { label: 'Employment type', sql: 'a.employment_type', type: 'text' },
      manager: { label: 'Manager', sql: 'm.name_en', type: 'text' },
      gender: { label: 'Gender', sql: 'e.gender', type: 'text' },
      dob: { label: 'Date of birth', sql: 'e.dob', type: 'date' },
      age: { label: 'Age', sql: 'extract(year FROM age($2::date, e.dob))::int', type: 'number' },
      marital_status: { label: 'Marital status', sql: `CASE e.marital_status WHEN 'couple' THEN 'married' ELSE e.marital_status END`, type: 'text' },
      blood_group: { label: 'Blood group', sql: 'e.blood_group', type: 'text' },
      nationality: { label: 'Nationality', sql: 'e.nationality', type: 'text' },
      join_date: { label: 'Join date', sql: 'e.join_date', type: 'date' },
      service_years: { label: 'Years of service', sql: `round((($2::date - e.join_date) / 365.25)::numeric, 1)`, type: 'number' },
      confirmed_on: { label: 'Confirmed on', sql: 'e.confirmed_on', type: 'date' },
      probation_end: { label: 'Probation ends', sql: 'e.probation_end', type: 'date' },
      exit_date: { label: 'Exit date', sql: 'e.exit_date', type: 'date' },
      work_email: { label: 'Work email', sql: 'e.email', type: 'text' },
      work_phone: { label: 'Work phone', sql: 'coalesce(e.work_phone, e.phone)', type: 'text' },
      personal_email: { label: 'Personal email', sql: 'e.personal_email', type: 'text' },
      province: { label: 'Province', sql: `e.permanent_address->>'province'`, type: 'text' },
      district: { label: 'District', sql: `e.permanent_address->>'district'`, type: 'text' },
      regime: { label: 'Retirement fund', sql: 'upper(e.regime)', type: 'text' },
      pan_on_file: { label: 'PAN on file', sql: 'e.pan_enc IS NOT NULL', type: 'bool' },
      bank_on_file: { label: 'Bank account on file', sql: 'e.bank_account_enc IS NOT NULL', type: 'bool' },
      bank_code: { label: 'Bank', sql: 'e.bank_code', type: 'text' },
      ot_eligible: { label: 'Overtime eligible', sql: 'e.ot_eligible', type: 'bool' },
      skills: { label: 'Skills', sql: `array_to_string(e.skills, ', ')`, type: 'text' },
    },
  },
  education: {
    label: 'Education', description: 'One row per qualification.',
    from: `${PLACED} JOIN employee_education ed ON ed.employee_id = e.id`,
    defaultColumns: ['emp_code', 'name', 'level', 'degree', 'institution', 'end_year'],
    columns: {
      ...who,
      level: { label: 'Level', sql: 'ed.level', type: 'text' },
      degree: { label: 'Degree', sql: 'ed.degree', type: 'text' },
      field: { label: 'Field', sql: 'ed.field', type: 'text' },
      institution: { label: 'Institution', sql: 'ed.institution', type: 'text' },
      board: { label: 'Board or university', sql: 'ed.board', type: 'text' },
      start_year: { label: 'From year', sql: 'ed.start_year', type: 'number' },
      end_year: { label: 'To year', sql: 'ed.end_year', type: 'number' },
      result: { label: 'Result', sql: 'ed.result', type: 'text' },
    },
  },
  family: {
    label: 'Family and nominees', description: 'One row per family member.',
    from: `${PLACED} JOIN employee_family fm ON fm.employee_id = e.id`,
    defaultColumns: ['emp_code', 'name', 'member', 'relation', 'nominee', 'share'],
    columns: {
      ...who,
      member: { label: 'Family member', sql: 'fm.name', type: 'text' },
      relation: { label: 'Relation', sql: `replace(fm.relation, '_', ' ')`, type: 'text' },
      member_dob: { label: 'Member date of birth', sql: 'fm.dob', type: 'date' },
      occupation: { label: 'Occupation', sql: 'fm.occupation', type: 'text' },
      dependent: { label: 'Dependent', sql: 'fm.is_dependent', type: 'bool' },
      nominee: { label: 'Nominee', sql: 'fm.is_nominee', type: 'bool' },
      share: { label: 'Nominee share (%)', sql: 'fm.nominee_share', type: 'number' },
    },
  },
  experience: {
    label: 'Previous employment', description: 'One row per earlier job.',
    from: `${PLACED} JOIN employee_experience ex ON ex.employee_id = e.id`,
    defaultColumns: ['emp_code', 'name', 'organisation', 'job_title', 'from_date', 'to_date'],
    columns: {
      ...who,
      organisation: { label: 'Organisation', sql: 'ex.organisation', type: 'text' },
      job_title: { label: 'Designation there', sql: 'ex.designation', type: 'text' },
      from_date: { label: 'From', sql: 'ex.from_date', type: 'date' },
      to_date: { label: 'To', sql: 'ex.to_date', type: 'date' },
      years: { label: 'Years', sql: 'round(((coalesce(ex.to_date, $2::date) - ex.from_date) / 365.25)::numeric, 1)', type: 'number' },
      reason_for_leaving: { label: 'Reason for leaving', sql: 'ex.reason_for_leaving', type: 'text' },
    },
  },
  career_events: {
    label: 'Career events', description: 'Hires, promotions, transfers, confirmations and separations.',
    from: `${PLACED} JOIN career_event ce ON ce.employee_id = e.id AND ce.status <> 'cancelled'`,
    defaultColumns: ['emp_code', 'name', 'event', 'effective_date', 'new_designation'],
    columns: {
      ...who,
      event: { label: 'Event', sql: `replace(ce.kind, '_', ' ')`, type: 'text' },
      effective_date: { label: 'Effective date', sql: 'ce.effective_date', type: 'date' },
      event_status: { label: 'Event status', sql: 'ce.status', type: 'text' },
      new_designation: { label: 'New designation', sql: `ce.details->>'designation'`, type: 'text' },
      new_grade: { label: 'New grade', sql: `ce.details->>'grade'`, type: 'text' },
      separation_type: { label: 'Separation type', sql: `replace(ce.details->>'separationType', '_', ' ')`, type: 'text' },
      note: { label: 'Note', sql: 'ce.note', type: 'text' },
    },
  },
  leave_balances: {
    label: 'Leave balances', description: 'One row per employee and leave type, with the balance today.',
    from: `${PLACED} JOIN leave_type lt ON lt.company_id = e.company_id
      LEFT JOIN LATERAL (SELECT coalesce(sum(l.delta_days), 0) AS balance, coalesce(sum(-l.delta_days) FILTER (WHERE l.reason = 'usage'), 0) AS used
                           FROM leave_ledger l WHERE l.employee_id = e.id AND l.leave_type_id = lt.id AND l.effective_date <= $2::date) lb ON true`,
    defaultColumns: ['emp_code', 'name', 'leave_type', 'balance'],
    columns: {
      ...who,
      leave_type: { label: 'Leave type', sql: 'lt.name_en', type: 'text' },
      leave_code: { label: 'Leave code', sql: 'lt.code', type: 'text' },
      balance: { label: 'Balance (days)', sql: 'lb.balance', type: 'number' },
      used: { label: 'Used (days)', sql: 'lb.used', type: 'number' },
      paid: { label: 'Paid leave', sql: 'lt.paid', type: 'bool' },
    },
  },
};

const OPS = ['eq', 'neq', 'contains', 'starts', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'] as const;
const key = z.string().regex(/^[a-z_]{1,40}$/);

export const adhocDefinition = z.object({
  dataset: key,
  columns: z.array(key).min(1).max(30),
  filters: z.array(z.object({ column: key, op: z.enum(OPS), value: z.union([z.string().max(200), z.number(), z.boolean()]).nullable().optional() }).strict()).max(15).default([]),
  sort: z.object({ column: key, dir: z.enum(['asc', 'desc']).default('asc') }).strict().nullable().optional(),
  includeExited: z.boolean().default(false),
}).strict();

export type AdhocDefinition = z.infer<typeof adhocDefinition>;

function coerce(type: ColType, v: unknown, column: string): unknown {
  if (v === null || v === undefined || v === '') throw new BadRequestError(`enter a value for the filter on ${column}`);
  if (type === 'number') { const n = Number(v); if (!Number.isFinite(n)) throw new BadRequestError(`${column} needs a number`); return n; }
  if (type === 'date') { const s = String(v); if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) throw new BadRequestError(`${column} needs a date`); return s; }
  if (type === 'bool') { if (v === true || v === 'true' || v === 'yes') return true; if (v === false || v === 'false' || v === 'no') return false; throw new BadRequestError(`${column} needs yes or no`); }
  return String(v);
}

/** Build the parameterised SQL for a definition. `$1` is the company, `$2` today; user values follow. */
export function buildAdhocQuery(def: AdhocDefinition, companyId: string, today: string, limit: number) {
  const ds = Object.hasOwn(ADHOC_DATASETS, def.dataset) ? ADHOC_DATASETS[def.dataset] : undefined;
  if (!ds) throw new BadRequestError('unknown dataset');
  const col = (k: string) => {
    const c = Object.hasOwn(ds.columns, k) ? ds.columns[k] : undefined;
    if (!c) throw new BadRequestError(`unknown column "${k}" for ${ds.label}`);
    return c;
  };
  const cols = [...new Set(def.columns)];
  const select = cols.map((k) => `${col(k).sql} AS "${k}"`).join(', ');
  const params: unknown[] = [companyId, today];
  const p = (v: unknown) => { params.push(v); return `$${params.length}`; };
  const where = ['e.company_id = $1'];
  if (!def.includeExited) where.push(`e.status = 'active'`);
  for (const f of def.filters) {
    const c = col(f.column);
    const expr = c.sql;
    switch (f.op) {
      case 'empty': where.push(c.type === 'text' ? `nullif(${expr}::text, '') IS NULL` : `${expr} IS NULL`); break;
      case 'not_empty': where.push(c.type === 'text' ? `nullif(${expr}::text, '') IS NOT NULL` : `${expr} IS NOT NULL`); break;
      case 'contains': case 'starts': {
        const s = String(coerce('text', f.value, c.label)).replace(/[\\%_]/g, (m) => `\\${m}`);
        where.push(`${expr}::text ILIKE ${p(f.op === 'contains' ? `%${s}%` : `${s}%`)}`);
        break;
      }
      default: {
        const v = coerce(c.type, f.value, c.label);
        const op = { eq: '=', neq: '<>', gt: '>', gte: '>=', lt: '<', lte: '<=' }[f.op];
        if (c.type === 'text' && (f.op === 'eq' || f.op === 'neq')) where.push(`lower(${expr}::text) ${op} lower(${p(v)})`);
        else if (c.type === 'text') where.push(`${expr}::text ${op} ${p(v)}`);
        else if (c.type === 'date') where.push(`${expr} ${op} ${p(v)}::date`);
        else if (c.type === 'bool') { if (f.op !== 'eq' && f.op !== 'neq') throw new BadRequestError(`${c.label} can only be compared with "is" or "is not"`); where.push(`${expr} ${op} ${p(v)}::boolean`); }
        else where.push(`${expr} ${op} ${p(v)}::numeric`);
      }
    }
  }
  const order = def.sort ? `${col(def.sort.column).sql} ${def.sort.dir === 'desc' ? 'DESC' : 'ASC'} NULLS LAST, e.emp_code` : 'e.emp_code';
  const whereSql = where.join(' AND ');
  return {
    sql: `SELECT ${select} FROM ${ds.from} WHERE ${whereSql} ORDER BY ${order} LIMIT ${Math.max(1, Math.min(limit, 10_000))}`,
    params,
    countSql: `SELECT count(*)::int AS n FROM ${ds.from} WHERE ${whereSql}`,
    countParams: params,
    columns: cols.map((k) => ({ key: k, header: col(k).label, type: col(k).type })),
  };
}
