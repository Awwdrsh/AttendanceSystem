import { weekdayOf } from '@hajiri/bs-calendar';
import { bsLabel } from '../../bsdate.js';
import type { Queryable } from '../../db/pool.js';
import { ConflictError } from '../../errors.js';
import { enqueueRecompute, isAttendanceLocked } from '../attendance/service.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { registerHandler, registerSummariser } from '../workflow/service.js';

export interface EffectiveShift { shiftId: string | null; source: 'roster' | 'default' | 'weekly_off' | 'roster_off' | 'none' }

/** What someone works on a date: a roster row wins (NULL = off), then the weekly off, then the default shift assignment. */
export async function effectiveShift(db: Queryable, employeeId: string, localDate: string): Promise<EffectiveShift> {
  const r = await db.query<{ has_roster: boolean; roster_shift: string | null; default_shift: string | null; weekly_off_days: number[] }>(
    `SELECT EXISTS (SELECT 1 FROM roster WHERE employee_id = e.id AND local_date = $2) AS has_roster,
            (SELECT shift_id FROM roster WHERE employee_id = e.id AND local_date = $2) AS roster_shift,
            (SELECT shift_id FROM shift_assignment WHERE employee_id = e.id AND valid @> $2::date) AS default_shift,
            e.weekly_off_days
       FROM employee e WHERE e.id = $1`, [employeeId, localDate]);
  const row = r.rows[0];
  if (!row) return { shiftId: null, source: 'none' };
  if (row.has_roster) return row.roster_shift ? { shiftId: row.roster_shift, source: 'roster' } : { shiftId: null, source: 'roster_off' };
  if ((row.weekly_off_days ?? []).includes(weekdayOf(localDate))) return { shiftId: null, source: 'weekly_off' };
  return row.default_shift ? { shiftId: row.default_shift, source: 'default' } : { shiftId: null, source: 'none' };
}

interface SwapRow { id: string; company_id: string; requester_id: string; colleague_id: string; local_date: string; status: string }

/**
 * Approved swap: each person takes the other's current shift for that day (written as roster rows, so it shows on the
 * roster and drives attendance), both are told, and both days are recomputed.
 */
async function applySwapDecision(db: Queryable, objectId: string, state: 'approved' | 'rejected'): Promise<void> {
  const r = await db.query<SwapRow>('SELECT id, company_id, requester_id, colleague_id, local_date, status FROM shift_swap WHERE id = $1 FOR UPDATE', [objectId]);
  const s = r.rows[0];
  if (!s || s.status !== 'pending') return;
  if (state === 'approved') {
    for (const id of [s.requester_id, s.colleague_id]) {
      if (await isAttendanceLocked(db, id, s.local_date)) throw new ConflictError('attendance for that day is locked by payroll');
    }
    const a = await effectiveShift(db, s.requester_id, s.local_date);
    const b = await effectiveShift(db, s.colleague_id, s.local_date);
    const upsert = 'INSERT INTO roster (employee_id, local_date, shift_id) VALUES ($1, $2, $3) ON CONFLICT (employee_id, local_date) DO UPDATE SET shift_id = EXCLUDED.shift_id';
    await db.query(upsert, [s.requester_id, s.local_date, b.shiftId]);
    await db.query(upsert, [s.colleague_id, s.local_date, a.shiftId]);
    await enqueueRecompute(db, [{ employeeId: s.requester_id, localDate: s.local_date }, { employeeId: s.colleague_id, localDate: s.local_date }], 'shift_swap');
  }
  await db.query('UPDATE shift_swap SET status = $2, decided_at = now() WHERE id = $1', [s.id, state]);
  for (const emp of [s.requester_id, s.colleague_id]) {
    const userId = await userIdForEmployee(db, emp);
    if (!userId) continue;
    await notify(db, {
      companyId: s.company_id, userId, employeeId: emp, template: `shift_swap.${state}`,
      title: state === 'approved' ? 'Shift swap approved' : 'Shift swap not approved',
      body: state === 'approved' ? `The shift swap on ${bsLabel(s.local_date)} is approved. Check your roster.` : `The shift swap on ${bsLabel(s.local_date)} was not approved. Your shifts stay as they were.`,
      payload: { swapId: s.id, localDate: s.local_date },
    });
  }
}

registerHandler('shift_swap', applySwapDecision);

registerSummariser('shift_swap', async (db, inst) => {
  const r = await db.query(
    `SELECT s.local_date, s.reason, s.colleague_note, c.name_en AS colleague_name,
            rs.code AS requester_shift_code, rs.name_en AS requester_shift_name, cs.code AS colleague_shift_code, cs.name_en AS colleague_shift_name
       FROM shift_swap s JOIN employee c ON c.id = s.colleague_id
       LEFT JOIN shift rs ON rs.id = s.requester_shift_id LEFT JOIN shift cs ON cs.id = s.colleague_shift_id
      WHERE s.id = $1`, [inst.object_id]);
  return r.rows[0] ?? {};
});
