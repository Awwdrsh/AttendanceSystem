import { localDateOf } from '@hajiri/attendance-engine';
import type { Queryable } from '../../db/pool.js';
import { notify, userIdForEmployee, userIdsWithRole } from '../notifications/service.js';

/**
 * Daily: remind travellers to settle a trip 3 and 10 days after it ended (an unsettled advance stays outstanding), and
 * tell HR about trips still unsettled 30 days after the return date.
 */
export async function remindTravelSettlements(db: Queryable, now = new Date()): Promise<number> {
  const today = localDateOf(now.getTime());
  const due = await db.query<{ id: string; company_id: string; employee_id: string; destination: string; age: number; advance_status: string }>(
    `SELECT id, company_id, employee_id, destination, ($1::date - to_date) AS age, advance_status FROM travel_request
      WHERE status = 'approved' AND ($1::date - to_date) IN (3, 10, 30)`, [today]);
  let n = 0;
  for (const t of due.rows) {
    if (t.age === 30) {
      if (t.advance_status === 'none') continue;
      for (const userId of await userIdsWithRole(db, t.company_id, 'hr')) {
        await notify(db, { companyId: t.company_id, userId, template: 'travel_settlement.overdue', title: 'Travel advance not settled',
          body: `A trip to ${t.destination} ended 30 days ago and its advance has not been settled.`, payload: { travelId: t.id }, channels: ['push'] });
        n++;
      }
      continue;
    }
    const userId = await userIdForEmployee(db, t.employee_id);
    if (!userId) continue;
    await notify(db, { companyId: t.company_id, userId, employeeId: t.employee_id, template: 'travel_settlement.reminder', title: 'Settle your trip',
      body: `Your trip to ${t.destination} has ended. Submit the settlement with your bills under Claims & travel.`, payload: { travelId: t.id }, channels: ['push'] });
    n++;
  }
  return n;
}
