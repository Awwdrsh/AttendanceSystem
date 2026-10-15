import { adToBs, bsMonthBounds, BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError } from '../../errors.js';

export interface InsightFilters { companyId: string; from?: string | undefined; to?: string | undefined; branchId?: string | null | undefined; orgUnitId?: string | null | undefined; today: string }

export interface Month { key: string; label: string; short: string; startAd: string; endAd: string }

const ym = (s: string) => {
  const m = /^(\d{4})-(\d{1,2})$/.exec(s);
  if (!m) throw new BadRequestError('months are written as YYYY-MM in the Bikram Sambat calendar');
  const y = Number(m[1]); const mo = Number(m[2]);
  if (mo < 1 || mo > 12) throw new BadRequestError('month must be 1 to 12');
  return { y, mo };
};
/** Axis abbreviations that stay distinct (Ashadh and Ashwin both start "Ash"). */
const SHORT_MONTHS = ['Bai', 'Jes', 'Asr', 'Shr', 'Bha', 'Asw', 'Kar', 'Man', 'Pou', 'Mag', 'Fal', 'Cha'];
const keyOf = (y: number, m: number) => `${y}-${String(m).padStart(2, '0')}`;
const nextYm = (y: number, m: number) => (m === 12 ? { y: y + 1, mo: 1 } : { y, mo: m + 1 });
const prevYm = (y: number, m: number) => (m === 1 ? { y: y - 1, mo: 12 } : { y, mo: m - 1 });

/** BS months from `from` to `to` inclusive (default: the last 12 months up to this one), at most 36. */
export function monthsBetween(from: string | undefined, to: string | undefined, today: string): Month[] {
  const now = adToBs(today);
  const end = to ? ym(to) : { y: now.year, mo: now.month };
  let start = from ? ym(from) : { y: end.y, mo: end.mo };
  if (!from) for (let i = 0; i < 11; i++) start = prevYm(start.y, start.mo);
  const out: Month[] = [];
  let cur = start;
  while (cur.y < end.y || (cur.y === end.y && cur.mo <= end.mo)) {
    let b: { startAd: string; endAd: string };
    try { b = bsMonthBounds(cur.y, cur.mo); } catch { throw new BadRequestError('that range is outside the supported calendar'); }
    const name = BS_MONTH_NAMES_EN[cur.mo - 1]!;
    out.push({ key: keyOf(cur.y, cur.mo), label: `${name} ${cur.y}`, short: `${SHORT_MONTHS[cur.mo - 1]} ${String(cur.y).slice(-2)}`, startAd: b.startAd, endAd: b.endAd });
    if (out.length > 36) throw new BadRequestError('pick at most 36 months');
    cur = nextYm(cur.y, cur.mo);
  }
  if (!out.length) throw new BadRequestError('the start month is after the end month');
  return out;
}

interface EmpRow {
  id: string; gender: string | null; dob: string | null; joined: string; exited: string | null; status: string;
  unit: string | null; branch: string | null; exit_kind: string | null;
}

const IDENT = /^[a-z_][a-z0-9_]*$/;

/**
 * Exit reasons are optional data (they may live on the employee or in an offboarding table added later). Look for them
 * at run time and classify each as voluntary or involuntary; without such data the split is simply not shown.
 */
async function exitReasonSource(db: Queryable): Promise<string | null> {
  const col = await db.query<{ column_name: string }>(
    `SELECT column_name FROM information_schema.columns
      WHERE table_schema = current_schema() AND table_name = 'employee' AND column_name IN ('exit_reason', 'exit_type', 'separation_type', 'separation_reason')
      ORDER BY array_position(ARRAY['separation_type', 'exit_type', 'exit_reason', 'separation_reason'], column_name::text) LIMIT 1`);
  if (col.rows[0] && IDENT.test(col.rows[0].column_name)) return `e."${col.rows[0].column_name}"::text`;
  const tbl = await db.query<{ table_name: string; column_name: string }>(
    `SELECT c.table_name, c.column_name FROM information_schema.columns c
      WHERE c.table_schema = current_schema()
        AND (c.table_name LIKE '%exit%' OR c.table_name LIKE '%separation%' OR c.table_name LIKE '%offboarding%' OR c.table_name LIKE '%resignation%')
        AND c.column_name IN ('separation_type', 'exit_type', 'reason_type', 'kind', 'type')
        AND EXISTS (SELECT 1 FROM information_schema.columns x WHERE x.table_schema = c.table_schema AND x.table_name = c.table_name AND x.column_name = 'employee_id')
      ORDER BY c.table_name LIMIT 1`);
  const t = tbl.rows[0];
  if (t && IDENT.test(t.table_name) && IDENT.test(t.column_name)) return `(SELECT x."${t.column_name}"::text FROM "${t.table_name}" x WHERE x.employee_id = e.id LIMIT 1)`;
  return null;
}

export function classifyExit(kind: string | null): 'voluntary' | 'involuntary' | 'other' | null {
  if (!kind) return null;
  const k = kind.toLowerCase();
  if (/involuntary|terminat|dismiss|layoff|lay_off|redundan|fired|misconduct/.test(k)) return 'involuntary';
  if (/resign|voluntary|quit/.test(k)) return 'voluntary';
  return 'other';
}

const DAY_MS = 86_400_000;
const yearsBetween = (a: string, b: string) => (Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / (365.25 * DAY_MS);
const round = (n: number, d = 1) => Math.round(n * 10 ** d) / 10 ** d;

function bands<T>(rows: T[], labels: string[], pick: (r: T) => string) {
  const m = new Map(labels.map((l) => [l, 0]));
  for (const r of rows) m.set(pick(r), (m.get(pick(r)) ?? 0) + 1);
  return [...m.entries()].map(([label, count]) => ({ label, count }));
}

/** Everything the Insights page shows, for one company, filter and BS month range. */
export async function computeInsights(db: Queryable, f: InsightFilters) {
  const months = monthsBetween(f.from, f.to, f.today);
  const first = months[0]!; const last = months[months.length - 1]!;
  const reasonExpr = await exitReasonSource(db);
  const empQ = await db.query<EmpRow>(
    `WITH RECURSIVE units AS (
        SELECT id FROM org_unit WHERE id = $3::uuid AND company_id = $1
        UNION SELECT o.id FROM org_unit o JOIN units ON o.parent_id = units.id)
     SELECT e.id, e.gender, e.dob, coalesce(e.join_date, (e.created_at AT TIME ZONE 'Asia/Kathmandu')::date) AS joined,
            CASE WHEN e.exit_date IS NOT NULL THEN e.exit_date WHEN e.status = 'exited' THEN (e.updated_at AT TIME ZONE 'Asia/Kathmandu')::date END AS exited,
            e.status, o.name_en AS unit, b.name_en AS branch, ${reasonExpr ?? 'NULL::text'} AS exit_kind
       FROM employee e
       LEFT JOIN LATERAL (SELECT a.branch_id, a.org_unit_id FROM employee_assignment a WHERE a.employee_id = e.id
                           ORDER BY (a.valid @> current_date) DESC, lower(a.valid) DESC LIMIT 1) a ON true
       LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN branch b ON b.id = a.branch_id
      WHERE e.company_id = $1 AND ($2::uuid IS NULL OR a.branch_id = $2::uuid) AND ($3::uuid IS NULL OR a.org_unit_id IN (SELECT id FROM units))`,
    [f.companyId, f.branchId ?? null, f.orgUnitId ?? null]);
  const emps = empQ.rows;
  const ids = emps.map((e) => e.id);
  const activeAt = (ad: string) => emps.filter((e) => e.joined <= ad && (!e.exited || e.exited > ad));
  const before = (ad: string) => new Date(Date.parse(`${ad}T00:00:00Z`) - DAY_MS).toISOString().slice(0, 10);

  const headcount = months.map((m) => {
    const start = activeAt(before(m.startAd)).length;
    const end = activeAt(m.endAd).length;
    const joins = emps.filter((e) => e.joined >= m.startAd && e.joined <= m.endAd).length;
    const exitRows = emps.filter((e) => e.exited && e.exited >= m.startAd && e.exited <= m.endAd);
    const avgHc = (start + end) / 2;
    return {
      month: m.key, label: m.label, short: m.short, headcount: end, joins, exits: exitRows.length, net: joins - exitRows.length,
      attritionPct: avgHc > 0 ? round((exitRows.length / avgHc) * 100, 2) : 0,
      voluntary: exitRows.filter((e) => classifyExit(e.exit_kind) === 'voluntary').length,
      involuntary: exitRows.filter((e) => classifyExit(e.exit_kind) === 'involuntary').length,
    };
  });
  const totalExits = headcount.reduce((s, h) => s + h.exits, 0);
  const avgMonthly = headcount.reduce((s, h) => s + h.attritionPct, 0) / headcount.length;
  const exitsWithReason = emps.filter((e) => e.exited && e.exited >= first.startAd && e.exited <= last.endAd && classifyExit(e.exit_kind));
  const attrition = {
    exits: totalExits, monthlyAvgPct: round(avgMonthly, 2), annualisedPct: round(avgMonthly * 12, 1),
    split: reasonExpr && exitsWithReason.length ? {
      voluntary: exitsWithReason.filter((e) => classifyExit(e.exit_kind) === 'voluntary').length,
      involuntary: exitsWithReason.filter((e) => classifyExit(e.exit_kind) === 'involuntary').length,
      other: exitsWithReason.filter((e) => classifyExit(e.exit_kind) === 'other').length,
    } : null,
  };

  const asOf = last.endAd < f.today ? last.endAd : f.today;
  const current = activeAt(asOf);
  const tenure = bands(current, ['Under 1 year', '1–2 years', '2–5 years', '5–10 years', '10+ years'], (e) => {
    const y = yearsBetween(e.joined, asOf);
    return y < 1 ? 'Under 1 year' : y < 2 ? '1–2 years' : y < 5 ? '2–5 years' : y < 10 ? '5–10 years' : '10+ years';
  });
  const age = bands(current, ['Under 25', '25–34', '35–44', '45–54', '55 and over', 'Not recorded'], (e) => {
    if (!e.dob) return 'Not recorded';
    const y = yearsBetween(e.dob, asOf);
    return y < 25 ? 'Under 25' : y < 35 ? '25–34' : y < 45 ? '35–44' : y < 55 ? '45–54' : '55 and over';
  });
  const gender = bands(current, ['Female', 'Male', 'Other', 'Not recorded'], (e) =>
    (e.gender === 'female' ? 'Female' : e.gender === 'male' ? 'Male' : e.gender === 'other' ? 'Other' : 'Not recorded')).filter((g) => g.count > 0 || g.label !== 'Other');
  const groupCount = (pick: (e: EmpRow) => string | null, none: string) => {
    const m = new Map<string, number>();
    for (const e of current) { const k = pick(e) ?? none; m.set(k, (m.get(k) ?? 0) + 1); }
    return [...m.entries()].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  };
  const departments = groupCount((e) => e.unit, 'No department');
  const branches = groupCount((e) => e.branch, 'No branch');

  const keys = months.map((m) => m.key); const starts = months.map((m) => m.startAd); const ends = months.map((m) => m.endAd);
  const monthCte = `m AS (SELECT * FROM unnest($2::text[], $3::date[], $4::date[]) AS m(key, start_ad, end_ad))`;

  const att = await db.query<{ key: string; scheduled: number; present: number; absent: number; late: number; ot_min: number; avg_worked: string | null }>(
    `WITH ${monthCte}
     SELECT m.key,
            count(d.*) FILTER (WHERE d.status IN ('Present', 'HalfDay', 'Absent', 'WFH', 'Incomplete', 'OnLeave', 'HalfDayLeave'))::int AS scheduled,
            count(d.*) FILTER (WHERE d.status IN ('Present', 'HalfDay', 'WFH', 'Incomplete'))::int AS present,
            count(d.*) FILTER (WHERE d.status = 'Absent')::int AS absent,
            count(d.*) FILTER (WHERE d.late_min > 0)::int AS late,
            coalesce(sum(d.ot_min), 0)::int AS ot_min,
            avg(d.worked_min) FILTER (WHERE d.worked_min > 0)::text AS avg_worked
       FROM m LEFT JOIN v_attendance_latest d ON d.local_date BETWEEN m.start_ad AND m.end_ad AND d.employee_id = ANY($1::uuid[])
      GROUP BY m.key`, [ids, keys, starts, ends]);
  const otApproved = await db.query<{ key: string; min: number }>(
    `WITH ${monthCte}
     SELECT m.key, coalesce(sum(s.approved_min), 0)::int AS min FROM m
       LEFT JOIN ot_slip s ON s.local_date BETWEEN m.start_ad AND m.end_ad AND s.status IN ('approved', 'posted') AND s.employee_id = ANY($1::uuid[])
      GROUP BY m.key`, [ids, keys, starts, ends]);
  const attBy = new Map(att.rows.map((r) => [r.key, r]));
  const otBy = new Map(otApproved.rows.map((r) => [r.key, r.min]));
  const attendance = months.map((m) => {
    const a = attBy.get(m.key);
    const scheduled = a?.scheduled ?? 0; const present = a?.present ?? 0;
    return {
      month: m.key, label: m.label, short: m.short, scheduledDays: scheduled, presentDays: present, absentDays: a?.absent ?? 0, lateDays: a?.late ?? 0,
      absenteeismPct: scheduled ? round(((a?.absent ?? 0) / scheduled) * 100, 1) : null,
      latePct: present ? round(((a?.late ?? 0) / present) * 100, 1) : null,
      avgWorkedHours: a?.avg_worked ? round(Number(a.avg_worked) / 60, 2) : null,
    };
  });
  const overtime = months.map((m) => ({
    month: m.key, label: m.label, short: m.short,
    detectedHours: round((attBy.get(m.key)?.ot_min ?? 0) / 60, 1), approvedHours: round((otBy.get(m.key) ?? 0) / 60, 1),
  }));

  const leaveQ = await db.query<{ type: string; code: string; days: string }>(
    `SELECT lt.name_en AS type, lt.code, coalesce(-sum(l.delta_days), 0)::text AS days
       FROM leave_ledger l JOIN leave_type lt ON lt.id = l.leave_type_id
      WHERE lt.company_id = $1 AND l.employee_id = ANY($2::uuid[]) AND l.reason IN ('usage', 'cancellation') AND l.effective_date BETWEEN $3 AND $4
      GROUP BY lt.name_en, lt.code, lt.sort_order ORDER BY lt.sort_order`, [f.companyId, ids, first.startAd, last.endAd]);
  const avgHeadcount = headcount.reduce((s, h) => s + h.headcount, 0) / headcount.length;
  const leave = leaveQ.rows.map((r) => ({ type: r.type, code: r.code, days: round(Number(r.days), 1), perEmployee: avgHeadcount ? round(Number(r.days) / avgHeadcount, 2) : 0 })).filter((r) => r.days > 0);
  const leaveMonthly = await db.query<{ key: string; days: string }>(
    `WITH ${monthCte}
     SELECT m.key, coalesce(-sum(l.delta_days), 0)::text AS days FROM m
       LEFT JOIN leave_ledger l ON l.effective_date BETWEEN m.start_ad AND m.end_ad AND l.reason IN ('usage', 'cancellation') AND l.employee_id = ANY($1::uuid[])
      GROUP BY m.key`, [ids, keys, starts, ends]);
  const leaveBy = new Map(leaveMonthly.rows.map((r) => [r.key, Number(r.days)]));

  // People cost from locked (or later) pay runs only: drafts can still change.
  const costQ = await db.query<{ key: string; gross: string; employer: string; net: string; tds: string; people: number }>(
    `SELECT (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) AS key, sum(p.gross)::text AS gross, sum(p.employer_statutory)::text AS employer,
            sum(p.net)::text AS net, sum(p.tds)::text AS tds, count(DISTINCT p.employee_id)::int AS people
       FROM payslip p JOIN payroll_run r ON r.id = p.run_id JOIN payroll_period pp ON pp.id = r.period_id
      WHERE r.company_id = $1 AND r.status IN ('locked', 'disbursed', 'filed') AND p.employee_id = ANY($2::uuid[])
        AND (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) = ANY($3::text[])
      GROUP BY 1`, [f.companyId, ids, keys]);
  const costBy = new Map(costQ.rows.map((r) => [r.key, r]));
  const cost = months.map((m) => {
    const c = costBy.get(m.key);
    const gross = Number(c?.gross ?? 0); const employer = Number(c?.employer ?? 0);
    return { month: m.key, label: m.label, short: m.short, gross: round(gross, 2), employerContributions: round(employer, 2), totalCost: round(gross + employer, 2),
      net: round(Number(c?.net ?? 0), 2), tds: round(Number(c?.tds ?? 0), 2), people: c?.people ?? 0, locked: !!c };
  });
  const unitOf = new Map(emps.map((e) => [e.id, e.unit ?? 'No department']));
  const costDeptQ = await db.query<{ employee_id: string; gross: string; employer: string }>(
    `SELECT p.employee_id, sum(p.gross)::text AS gross, sum(p.employer_statutory)::text AS employer
       FROM payslip p JOIN payroll_run r ON r.id = p.run_id JOIN payroll_period pp ON pp.id = r.period_id
      WHERE r.company_id = $1 AND r.status IN ('locked', 'disbursed', 'filed') AND p.employee_id = ANY($2::uuid[])
        AND (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) = ANY($3::text[])
      GROUP BY p.employee_id`, [f.companyId, ids, keys]);
  const deptCost = new Map<string, number>();
  for (const r of costDeptQ.rows) { const k = unitOf.get(r.employee_id) ?? 'No department'; deptCost.set(k, (deptCost.get(k) ?? 0) + Number(r.gross) + Number(r.employer)); }
  const costByDepartment = [...deptCost.entries()].map(([label, total]) => ({ label, totalCost: round(total, 2) })).sort((a, b) => b.totalCost - a.totalCost);

  // Headline tiles: the last month of the range against the one before it.
  const lastH = headcount[headcount.length - 1]!; const prevH = headcount[headcount.length - 2];
  // Pay is only final once a run is locked: compare the latest locked month with the locked month before it.
  const lockedIdx = cost.map((x, i) => (x.locked ? i : -1)).filter((i) => i >= 0);
  const lastC = lockedIdx.length ? cost[lockedIdx[lockedIdx.length - 1]!]! : null;
  const prevC = lockedIdx.length > 1 ? cost[lockedIdx[lockedIdx.length - 2]!]! : null;
  const lastA = attendance[attendance.length - 1]!; const prevA = attendance[attendance.length - 2];
  const delta = (a: number | null, b: number | null | undefined) => (a === null || b === null || b === undefined ? null : round(a - b, 2));
  const leaveDays = leaveBy.get(last.key) ?? 0; const prevLeave = prevH ? leaveBy.get(months[months.length - 2]!.key) ?? 0 : null;
  const kpis = [
    { key: 'headcount', label: 'Headcount', value: lastH.headcount, delta: delta(lastH.headcount, prevH?.headcount), unit: '', goodWhen: 'neutral' },
    { key: 'joins', label: 'Joined', value: lastH.joins, delta: delta(lastH.joins, prevH?.joins), unit: '', goodWhen: 'neutral' },
    { key: 'exits', label: 'Left', value: lastH.exits, delta: delta(lastH.exits, prevH?.exits), unit: '', goodWhen: 'down' },
    { key: 'attrition', label: 'Attrition a year', value: attrition.annualisedPct, delta: null, unit: '%', goodWhen: 'down' },
    { key: 'cost', label: lastC ? `People cost, ${lastC.label.split(' ')[0]}` : 'People cost', value: lastC ? lastC.totalCost : null,
      delta: lastC && prevC ? delta(lastC.totalCost, prevC.totalCost) : null, unit: 'NPR', goodWhen: 'neutral', ...(prevC ? { vs: prevC.label.split(' ')[0] } : {}) },
    { key: 'absenteeism', label: 'Absenteeism', value: lastA.absenteeismPct, delta: delta(lastA.absenteeismPct, prevA?.absenteeismPct), unit: '%', goodWhen: 'down' },
    { key: 'late', label: 'Late arrivals', value: lastA.latePct, delta: delta(lastA.latePct, prevA?.latePct), unit: '%', goodWhen: 'down' },
    { key: 'hours', label: 'Average day', value: lastA.avgWorkedHours, delta: delta(lastA.avgWorkedHours, prevA?.avgWorkedHours), unit: 'h', goodWhen: 'neutral' },
    { key: 'leave', label: 'Leave taken', value: round(leaveDays, 1), delta: delta(leaveDays, prevLeave), unit: 'days', goodWhen: 'neutral' },
  ];

  return {
    months: months.map((m) => ({ key: m.key, label: m.label, short: m.short, startAd: m.startAd, endAd: m.endAd })),
    asOf, kpis, headcount, attrition, tenure, age, gender, departments, branches, cost, costByDepartment, overtime, leave,
    leaveMonthly: months.map((m) => ({ month: m.key, label: m.label, short: m.short, days: round(leaveBy.get(m.key) ?? 0, 1) })),
    attendance, exitReasonsAvailable: !!reasonExpr,
  };
}

export type Insights = Awaited<ReturnType<typeof computeInsights>>;

/** Columns for each CSV export. */
export const DATASETS: Record<string, { file: string; rows: (i: Insights) => Array<Record<string, unknown>>; columns: Array<{ key: string; header: string }> }> = {
  headcount: { file: 'headcount', rows: (i) => i.headcount, columns: [
    { key: 'label', header: 'Month' }, { key: 'headcount', header: 'Headcount at month end' }, { key: 'joins', header: 'Joined' }, { key: 'exits', header: 'Left' },
    { key: 'net', header: 'Net change' }, { key: 'attritionPct', header: 'Attrition %' }, { key: 'voluntary', header: 'Voluntary exits' }, { key: 'involuntary', header: 'Involuntary exits' }] },
  tenure: { file: 'tenure', rows: (i) => i.tenure, columns: [{ key: 'label', header: 'Tenure' }, { key: 'count', header: 'Employees' }] },
  age: { file: 'age', rows: (i) => i.age, columns: [{ key: 'label', header: 'Age' }, { key: 'count', header: 'Employees' }] },
  gender: { file: 'gender', rows: (i) => i.gender, columns: [{ key: 'label', header: 'Gender' }, { key: 'count', header: 'Employees' }] },
  departments: { file: 'departments', rows: (i) => i.departments, columns: [{ key: 'label', header: 'Department' }, { key: 'count', header: 'Employees' }] },
  branches: { file: 'branches', rows: (i) => i.branches, columns: [{ key: 'label', header: 'Branch' }, { key: 'count', header: 'Employees' }] },
  cost: { file: 'people-cost', rows: (i) => i.cost, columns: [
    { key: 'label', header: 'Month' }, { key: 'gross', header: 'Gross pay' }, { key: 'employerContributions', header: 'Employer SSF/PF/gratuity' },
    { key: 'totalCost', header: 'Total cost' }, { key: 'net', header: 'Net pay' }, { key: 'tds', header: 'TDS' }, { key: 'people', header: 'People paid' }] },
  costByDepartment: { file: 'people-cost-by-department', rows: (i) => i.costByDepartment, columns: [{ key: 'label', header: 'Department' }, { key: 'totalCost', header: 'Total cost' }] },
  overtime: { file: 'overtime', rows: (i) => i.overtime, columns: [{ key: 'label', header: 'Month' }, { key: 'detectedHours', header: 'Overtime detected (h)' }, { key: 'approvedHours', header: 'Overtime approved (h)' }] },
  leave: { file: 'leave-by-type', rows: (i) => i.leave, columns: [{ key: 'type', header: 'Leave type' }, { key: 'days', header: 'Days taken' }, { key: 'perEmployee', header: 'Days per employee' }] },
  attendance: { file: 'attendance', rows: (i) => i.attendance, columns: [
    { key: 'label', header: 'Month' }, { key: 'scheduledDays', header: 'Scheduled days' }, { key: 'presentDays', header: 'Days worked' }, { key: 'absentDays', header: 'Absent days' },
    { key: 'absenteeismPct', header: 'Absenteeism %' }, { key: 'lateDays', header: 'Late days' }, { key: 'latePct', header: 'Late %' }, { key: 'avgWorkedHours', header: 'Average hours per day' }] },
};
