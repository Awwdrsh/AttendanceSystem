import { localDateOf, localTimeMs } from '@hajiri/attendance-engine';
import { bsToAd, parseBs } from '@hajiri/bs-calendar';
import type { Db, Queryable } from '../../db/pool.js';
import { sha256Hex } from '../../crypto.js';
import { isAttendanceLocked, recomputeForPunch } from '../attendance/service.js';
import { parseCsv } from '../employees/routes.js';
import { ensurePseudoDevice, nptLocalTime } from '../punch/devices.js';
import { nptToday } from '../workflow/service.js';

/**
 * Manual attendance upload (study: "Manual attendance upload"): HR uploads a CSV of emp_code, date_bs (or date_ad),
 * in_time, out_time, note. Every row is checked first and shown with its errors; importing creates manual punches through
 * the same idempotency key as a single manual punch, so uploading the same file twice adds nothing.
 */
export interface UploadRow {
  line: number; empCode: string; date: string | null; dateBs: string | null; inTime: string | null; outTime: string | null; note: string | null;
  employeeId: string | null; name: string | null; overnight: boolean; errors: string[];
}

const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/;
const norm = (t: string): string | null => { const m = HHMM.exec(t.trim()); return m ? `${m[1]!.padStart(2, '0')}:${m[2]}` : null; };

export const UPLOAD_TEMPLATE = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-29,09:05,17:10,Device was offline\n';
export const MAX_UPLOAD_ROWS = 2000;

export async function checkUpload(db: Queryable, companyId: string, csv: string, selfEmployeeId: string | null = null): Promise<{ rows: UploadRow[]; errors: string[] }> {
  const table = parseCsv(csv.replace(/^﻿/, ''));
  if (!table.length) return { rows: [], errors: ['The file is empty.'] };
  const header = table[0]!.map((h) => h.trim().toLowerCase().replace(/\s+/g, '_'));
  const col = (name: string) => header.indexOf(name);
  const iCode = col('emp_code'); const iBs = col('date_bs') >= 0 ? col('date_bs') : col('date'); const iAd = col('date_ad');
  const iIn = col('in_time'); const iOut = col('out_time'); const iNote = col('note');
  const errors: string[] = [];
  if (iCode < 0) errors.push('The first row must name the columns: emp_code, date_bs (or date_ad), in_time, out_time, note.');
  if (iBs < 0 && iAd < 0) errors.push('Add a date_bs (BS, YYYY-MM-DD) or date_ad column.');
  if (iIn < 0 && iOut < 0) errors.push('Add in_time and/or out_time columns (HH:MM, 24-hour).');
  if (errors.length) return { rows: [], errors };
  const body = table.slice(1);
  if (body.length > MAX_UPLOAD_ROWS) return { rows: [], errors: [`Upload at most ${MAX_UPLOAD_ROWS} rows at a time.`] };

  const codes = [...new Set(body.map((r) => (r[iCode] ?? '').trim()).filter(Boolean))];
  const emps = await db.query<{ id: string; emp_code: string; name_en: string; status: string; join_date: string | null; exit_date: string | null }>(
    `SELECT id, emp_code, name_en, status, join_date::text, exit_date::text FROM employee WHERE company_id = $1 AND emp_code = ANY($2::text[])`, [companyId, codes]);
  const byCode = new Map(emps.rows.map((e) => [e.emp_code, e]));
  const today = nptToday();
  const seen = new Set<string>();
  const rows: UploadRow[] = [];
  for (const [i, r] of body.entries()) {
    const cell = (k: number) => (k >= 0 ? (r[k] ?? '').trim() : '');
    const row: UploadRow = { line: i + 2, empCode: cell(iCode), date: null, dateBs: null, inTime: null, outTime: null, note: cell(iNote) || null, employeeId: null, name: null, overnight: false, errors: [] };
    const e = byCode.get(row.empCode);
    if (!row.empCode) row.errors.push('Employee code is missing.');
    else if (!e) row.errors.push(`No employee with code ${row.empCode}.`);
    else if (e.status !== 'active') row.errors.push(`${e.name_en} is not active.`);
    else if (selfEmployeeId && e.id === selfEmployeeId) row.errors.push('You cannot upload your own attendance; ask another HR person.');
    else { row.employeeId = e.id; row.name = e.name_en; }
    try {
      if (iBs >= 0 && cell(iBs)) { row.dateBs = cell(iBs); row.date = bsToAd(parseBs(row.dateBs)); }
      else if (iAd >= 0 && cell(iAd) && /^\d{4}-\d{2}-\d{2}$/.test(cell(iAd)) && !Number.isNaN(Date.parse(`${cell(iAd)}T00:00:00Z`))) row.date = cell(iAd);
      else row.errors.push('The date is missing or not YYYY-MM-DD.');
    } catch { row.errors.push(`${cell(iBs)} is not a valid BS date.`); }
    if (row.date && row.date > today) row.errors.push('The date is in the future.');
    if (row.date && e && e.join_date && row.date < e.join_date) row.errors.push('The date is before the employee joined.');
    if (row.date && e && e.exit_date && row.date > e.exit_date) row.errors.push('The date is after the employee left.');
    const rawIn = cell(iIn); const rawOut = cell(iOut);
    row.inTime = rawIn ? norm(rawIn) : null; row.outTime = rawOut ? norm(rawOut) : null;
    if (rawIn && !row.inTime) row.errors.push(`In time ${rawIn} is not HH:MM.`);
    if (rawOut && !row.outTime) row.errors.push(`Out time ${rawOut} is not HH:MM.`);
    if (!rawIn && !rawOut) row.errors.push('Give an in time, an out time or both.');
    if (row.inTime && row.outTime && row.outTime <= row.inTime) row.overnight = true;
    if (row.note && row.note.length > 200) row.errors.push('The note is longer than 200 characters.');
    if (row.employeeId && row.date) {
      const k = `${row.employeeId}|${row.date}`;
      if (seen.has(k)) row.errors.push('This employee and date appear more than once in the file.');
      seen.add(k);
      if (!row.errors.length && (await isAttendanceLocked(db, row.employeeId, row.date))) row.errors.push('This day is locked by payroll.');
    }
    rows.push(row);
  }
  return { rows, errors: [] };
}

/** Insert one manual punch with the same idempotency key as POST /punch/manual; returns the id, or null for a duplicate. */
async function manualPunch(db: Queryable, p: { companyId: string; employeeId: string; empCode: string; localDate: string; time: string; dayOffset: number; direction: 'in' | 'out'; reason: string; actorId: string }): Promise<{ id: string; at: number } | null> {
  const device = await ensurePseudoDevice(db, p.companyId, 'MANUAL');
  const at = localTimeMs(p.localDate, p.time, p.dayOffset);
  const punchDate = localDateOf(at);
  const key = sha256Hex(`manual|${p.employeeId}|${punchDate}|${p.time}|${p.direction}`);
  const ins = await db.query<{ id: string }>(
    `INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, raw, idempotency_key)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'manual', 'manual', $10, $11) ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id`,
    [device.id, device.serialNo, device.vendor, p.empCode, p.employeeId, new Date(at), nptLocalTime(at), punchDate, p.direction,
      JSON.stringify({ reason: p.reason, enteredBy: p.actorId, upload: true }), key]);
  return ins.rows[0] ? { id: ins.rows[0].id, at } : null;
}

export async function importUpload(db: Db, companyId: string, rows: UploadRow[], reason: string, actorId: string): Promise<{ punches: number; duplicates: number; days: number }> {
  let punches = 0; let duplicates = 0; let days = 0;
  for (const r of rows) {
    if (r.errors.length || !r.employeeId || !r.date) continue;
    const why = `CSV upload: ${reason}${r.note ? ` (${r.note})` : ''}`;
    let last: { id: string; at: number } | null = null;
    for (const [direction, time, offset] of [['in', r.inTime, 0], ['out', r.outTime, r.overnight ? 1 : 0]] as const) {
      if (!time) continue;
      const p = await manualPunch(db, { companyId, employeeId: r.employeeId, empCode: r.empCode, localDate: r.date, time, dayOffset: offset, direction, reason: why, actorId });
      if (p) { punches++; last = p; } else duplicates++;
    }
    if (last) { await recomputeForPunch(db, r.employeeId, last.at, last.id); days++; }
  }
  return { punches, duplicates, days };
}
