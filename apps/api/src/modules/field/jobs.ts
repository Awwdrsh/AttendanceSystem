import type { Queryable } from '../../db/pool.js';

/**
 * Nightly: close breaks left open from an earlier day (someone forgot to end one and went home). The break ends at the
 * day's last clock-out after it started, capped at its limit, so a forgotten break never runs for hours.
 */
export async function closeStaleBreaks(db: Queryable, now = new Date()): Promise<number> {
  const r = await db.query(
    `UPDATE work_break b SET ended_at = x.ended_at, minutes = floor(extract(epoch FROM x.ended_at - b.started_at) / 60)::int,
            over_limit = floor(extract(epoch FROM x.ended_at - b.started_at) / 60) > b.max_min
       FROM (SELECT wb.id, least(coalesce((SELECT min(p.punched_at) FROM punch_event p WHERE p.employee_id = wb.employee_id
                     AND p.local_date = wb.local_date AND p.direction = 'out' AND p.punched_at > wb.started_at), wb.started_at + make_interval(mins => wb.max_min)),
                     wb.started_at + make_interval(mins => wb.max_min)) AS ended_at
               FROM work_break wb WHERE wb.ended_at IS NULL AND wb.local_date < ($1::timestamptz AT TIME ZONE 'Asia/Kathmandu')::date) x
      WHERE b.id = x.id`, [now]);
  return r.rowCount ?? 0;
}
