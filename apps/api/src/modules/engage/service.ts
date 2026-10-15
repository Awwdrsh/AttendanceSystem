import { adToBs, BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';
import type { Db, Queryable } from '../../db/pool.js';
import type { AuthUser } from '../auth/service.js';
import { hasRole } from '../auth/middleware.js';

export type AudienceKind = 'all' | 'branch' | 'department' | 'reports';

/** Run fn in one transaction on a dedicated client. */
export async function inTx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query('BEGIN');
    const out = await fn(client);
    await client.query('COMMIT');
    return out;
  } catch (e) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw e;
  } finally {
    client.release();
  }
}

/** Where someone sits today: branch, org unit with all its parent units, and line manager. */
export interface Placement { branchId: string | null; unitIds: string[]; managerId: string | null; orgUnitId: string | null }

export async function placementOf(db: Queryable, employeeId: string | null): Promise<Placement> {
  if (!employeeId) return { branchId: null, unitIds: [], managerId: null, orgUnitId: null };
  const r = await db.query<{ branch_id: string | null; org_unit_id: string | null; manager_id: string | null; units: string[] | null }>(
    `WITH a AS (
       SELECT branch_id, org_unit_id, manager_id FROM employee_assignment
        WHERE employee_id = $1 ORDER BY (valid @> current_date) DESC, lower(valid) DESC LIMIT 1)
     SELECT a.branch_id, a.org_unit_id, a.manager_id,
            (WITH RECURSIVE up AS (SELECT id, parent_id FROM org_unit WHERE id = a.org_unit_id
                                   UNION SELECT o.id, o.parent_id FROM org_unit o JOIN up ON o.id = up.parent_id)
             SELECT array_agg(id) FROM up) AS units
       FROM a`, [employeeId]);
  const row = r.rows[0];
  return { branchId: row?.branch_id ?? null, unitIds: row?.units ?? [], managerId: row?.manager_id ?? null, orgUnitId: row?.org_unit_id ?? null };
}

/**
 * SQL condition true when the item (alias with audience_kind / audience_id, and author_employee_id when `withReports`)
 * targets the person at placement `p`. Parameters are appended to `params`.
 */
export function audienceSql(alias: string, p: Placement, params: unknown[], withReports: boolean): string {
  params.push(p.branchId, p.unitIds, p.managerId);
  const b = params.length - 2; const u = params.length - 1; const m = params.length;
  const reports = withReports
    ? `OR (${alias}.audience_kind = 'reports' AND ${alias}.author_employee_id = $${m}::uuid)`
    : `OR ($${m}::uuid IS NOT NULL AND false)`;
  return `(${alias}.audience_kind = 'all'
     OR (${alias}.audience_kind = 'branch' AND ${alias}.audience_id = $${b}::uuid)
     OR (${alias}.audience_kind = 'department' AND ${alias}.audience_id = ANY($${u}::uuid[]))
     ${reports})`;
}

export interface AudienceMember { employee_id: string; user_id: string | null; org_unit_id: string | null; branch_id: string | null }

/** Active employees (with their active user account, if any) an audience reaches. */
export async function audienceMembers(db: Queryable, companyId: string, kind: AudienceKind, audienceId: string | null, authorEmployeeId: string | null): Promise<AudienceMember[]> {
  const r = await db.query<AudienceMember>(
    `WITH RECURSIVE units AS (
        SELECT id FROM org_unit WHERE id = $3::uuid AND company_id = $1
        UNION SELECT o.id FROM org_unit o JOIN units ON o.parent_id = units.id),
      cur AS (
        SELECT DISTINCT ON (a.employee_id) a.employee_id, a.branch_id, a.org_unit_id, a.manager_id
          FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.company_id = $1
         ORDER BY a.employee_id, (a.valid @> current_date) DESC, lower(a.valid) DESC)
     SELECT e.id AS employee_id, u.id AS user_id, cur.org_unit_id, cur.branch_id
       FROM employee e
       LEFT JOIN cur ON cur.employee_id = e.id
       LEFT JOIN app_user u ON u.employee_id = e.id AND u.status = 'active'
      WHERE e.company_id = $1 AND e.status = 'active'
        AND CASE $2::text
              WHEN 'all' THEN true
              WHEN 'branch' THEN cur.branch_id IS NOT DISTINCT FROM $3::uuid
              WHEN 'department' THEN cur.org_unit_id IN (SELECT id FROM units)
              WHEN 'reports' THEN cur.manager_id IS NOT DISTINCT FROM $4::uuid AND $4::uuid IS NOT NULL
              ELSE false END`,
    [companyId, kind, audienceId, authorEmployeeId]);
  return r.rows;
}

/** The audience target (branch or org unit) must belong to the company. */
export async function audienceTargetExists(db: Queryable, companyId: string, kind: AudienceKind, id: string | null | undefined): Promise<boolean> {
  if (kind === 'all' || kind === 'reports') return true;
  if (!id) return false;
  const table = kind === 'branch' ? 'branch' : 'org_unit';
  const r = await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [id, companyId]);
  return r.rows.length > 0;
}

export const isHr = (u: AuthUser) => hasRole(u, 'hr');

const DEFAULT_VALUES: Array<[string, string, string]> = [
  ['Ownership', 'Took responsibility and saw it through', '🎯'],
  ['Teamwork', 'Made the people around them better', '🤝'],
  ['Customer first', 'Went the extra mile for a customer', '💚'],
  ['Innovation', 'Found a better way to do it', '💡'],
  ['Integrity', 'Did the right thing, even when it was hard', '🛡️'],
];

/** Companies set up after the migration get the same starter values the first time anyone opens the kudos form. */
export async function ensureDefaultValues(db: Queryable, companyId: string): Promise<void> {
  const any = await db.query('SELECT 1 FROM engage_value WHERE company_id = $1 LIMIT 1', [companyId]);
  if (any.rows.length) return;
  for (const [i, [name, description, emoji]] of DEFAULT_VALUES.entries()) {
    await db.query('INSERT INTO engage_value (company_id, name, description, emoji, sort_order) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING',
      [companyId, name, description, emoji, i + 1]);
  }
}

// ---------------------------------------------------------------- celebrations

export interface Celebration {
  kind: 'birthday' | 'anniversary' | 'joined';
  targetId: string;
  employeeId: string;
  name: string;
  designation: string | null;
  date: string;            // AD date of the occurrence
  bsLabel: string;
  years: number | null;
  daysAway: number;        // 0 = today; negative for people who joined in the last two weeks
}

const DAY_MS = 86_400_000;
const addAd = (ad: string, days: number) => new Date(Date.parse(`${ad}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
const bsOf = (ad: string) => { try { const b = adToBs(ad); return { ...b, md: `${b.month}-${b.day}` }; } catch { return null; } };
const labelOf = (ad: string) => { const b = bsOf(ad); return b ? `${b.day} ${BS_MONTH_NAMES_EN[b.month - 1]}` : ad; };

/**
 * Birthdays (only people who chose to show theirs), work anniversaries and new joiners from today for `days` days.
 * Birthdays and anniversaries follow the Bikram Sambat calendar: the same BS month and day each year.
 */
export async function celebrations(db: Queryable, companyId: string, today: string, days: number): Promise<Celebration[]> {
  const r = await db.query<{ id: string; name_en: string; designation: string | null; dob: string | null; join_date: string | null; show_birthday: boolean | null }>(
    `SELECT e.id, e.name_en, e.designation, e.dob, e.join_date, p.show_birthday
       FROM employee e LEFT JOIN engage_pref p ON p.employee_id = e.id
      WHERE e.company_id = $1 AND e.status = 'active' AND (e.dob IS NOT NULL OR e.join_date IS NOT NULL)`, [companyId]);
  const window = new Map<string, { ad: string; offset: number; year: number }>();
  for (let i = 0; i < days; i++) {
    const ad = addAd(today, i);
    const b = bsOf(ad);
    if (b && !window.has(b.md)) window.set(b.md, { ad, offset: i, year: b.year });
  }
  const out: Celebration[] = [];
  for (const e of r.rows) {
    if (e.dob && e.show_birthday) {
      const b = bsOf(e.dob);
      const hit = b ? window.get(b.md) : undefined;
      if (hit) out.push({ kind: 'birthday', targetId: `birthday:${e.id}:${hit.year}`, employeeId: e.id, name: e.name_en, designation: e.designation, date: hit.ad, bsLabel: labelOf(hit.ad), years: null, daysAway: hit.offset });
    }
    if (e.join_date) {
      const b = bsOf(e.join_date);
      const hit = b ? window.get(b.md) : undefined;
      if (b && hit && hit.year > b.year) {
        out.push({ kind: 'anniversary', targetId: `anniversary:${e.id}:${hit.year}`, employeeId: e.id, name: e.name_en, designation: e.designation, date: hit.ad, bsLabel: labelOf(hit.ad), years: hit.year - b.year, daysAway: hit.offset });
      }
      const ago = Math.round((Date.parse(`${today}T00:00:00Z`) - Date.parse(`${e.join_date}T00:00:00Z`)) / DAY_MS);
      if (ago >= 0 && ago <= 14) out.push({ kind: 'joined', targetId: `joined:${e.id}`, employeeId: e.id, name: e.name_en, designation: e.designation, date: e.join_date, bsLabel: labelOf(e.join_date), years: null, daysAway: -ago });
    }
  }
  return out.sort((a, b) => a.daysAway - b.daysAway || a.name.localeCompare(b.name));
}

/** A celebration target id refers to an employee of the company (and, for birthdays, one who shows it). */
export async function celebrationTargetOk(db: Queryable, companyId: string, targetId: string): Promise<boolean> {
  const m = /^(birthday|anniversary|joined):([0-9a-f-]{36})(?::(\d{4}))?$/.exec(targetId);
  if (!m) return false;
  const r = await db.query<{ show_birthday: boolean | null }>(
    `SELECT p.show_birthday FROM employee e LEFT JOIN engage_pref p ON p.employee_id = e.id WHERE e.id = $1 AND e.company_id = $2`, [m[2], companyId]);
  if (!r.rows[0]) return false;
  return m[1] !== 'birthday' || r.rows[0].show_birthday === true;
}
