import type { Queryable } from '../../db/pool.js';

export interface FieldChange { field: string; label: string; from: string | null; to: string | null }

/** Append field-level history rows (values already masked and readable). Call inside the transaction that made the change. */
export async function recordFieldChanges(db: Queryable, h: {
  companyId: string; employeeId: string; changes: FieldChange[]; source: 'hr_edit' | 'change_request' | 'career_event' | 'import' | 'system';
  refId?: string | null; actorId: string | null;
}): Promise<void> {
  for (const c of h.changes) {
    await db.query(
      `INSERT INTO employee_field_history (company_id, employee_id, field, old_value, new_value, source, ref_id, changed_by)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
      [h.companyId, h.employeeId, c.label, c.from?.slice(0, 500) ?? null, c.to?.slice(0, 500) ?? null, h.source, h.refId ?? null, h.actorId]);
  }
}

/** Plain employee columns edited on the HR employee form, with the label used in history. */
const EMPLOYEE_FIELDS: Array<[string, string]> = [
  ['emp_code', 'Employee code'], ['name_en', 'Name'], ['gender', 'Gender'], ['dob', 'Date of birth'], ['marital_status', 'Marital status'],
  ['join_date', 'Join date'], ['exit_date', 'Exit date'], ['designation', 'Designation'], ['grade', 'Grade'], ['phone', 'Phone'], ['email', 'Work email'],
  ['ot_eligible', 'Overtime eligible'], ['work_from_home', 'Work from home'], ['regime', 'Retirement scheme'], ['ssf_id', 'SSF ID'], ['pf_id', 'PF number'],
  ['cit_id', 'CIT ID'], ['bank_code', 'Bank'], ['bank_branch', 'Bank branch'], ['bank_account_last4', 'Bank account'], ['is_resident', 'Resident'],
  ['remote_area_class', 'Remote area class'], ['disability', 'Disability'], ['festival_month', 'Festival month'], ['probation_end', 'Probation end'],
  ['has_pan', 'PAN'], ['branch_id', 'Branch'], ['org_unit_id', 'Department'], ['manager_id', 'Manager'], ['employment_type', 'Employment type'], ['status', 'Status'],
];

const show = (v: unknown): string | null => {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (Array.isArray(v)) return v.join(', ');
  return String(v);
};

/** Compare two employee rows (as the employees module returns them) and return readable changes. */
export async function diffEmployeeRows(db: Queryable, before: Record<string, unknown>, after: Record<string, unknown>): Promise<FieldChange[]> {
  const out: FieldChange[] = [];
  const name = async (table: 'branch' | 'org_unit' | 'employee', id: unknown) => {
    if (!id) return null;
    const r = await db.query<{ n: string }>(`SELECT name_en AS n FROM ${table} WHERE id = $1`, [id]);
    return r.rows[0]?.n ?? String(id);
  };
  for (const [col, label] of EMPLOYEE_FIELDS) {
    const b = before[col]; const a = after[col];
    if (show(b) === show(a)) continue;
    let from = show(b); let to = show(a);
    if (col === 'branch_id') { from = await name('branch', b); to = await name('branch', a); }
    if (col === 'org_unit_id') { from = await name('org_unit', b); to = await name('org_unit', a); }
    if (col === 'manager_id') { from = await name('employee', b); to = await name('employee', a); }
    if (col === 'bank_account_last4') { from = from ? `•••• ${from}` : null; to = to ? `•••• ${to}` : null; }
    if (col === 'has_pan') { from = b ? 'Recorded' : null; to = a ? 'Recorded' : null; }
    out.push({ field: col, label, from, to });
  }
  return out;
}
