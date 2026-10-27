"""Background work (port of apps/api/src/modules/expenses/jobs.ts): travel settlement reminders, daily."""
from __future__ import annotations

from datetime import datetime

from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today
from hajiri.core.db import rows
from hajiri.notifications.service import notify, user_id_for_employee, user_ids_with_role
from hajiri.worker.registry import daily_job


def remind_travel_settlements(now: datetime | None = None) -> int:
    """
    Remind travellers to settle a trip 3 and 10 days after it ended (an unsettled advance stays outstanding), and tell
    HR about trips still unsettled 30 days after the return date. Returns the number of notifications.
    """
    today = npt_today(now or _now()).isoformat()
    due = rows("""SELECT id::text, company_id::text, employee_id::text, destination, (%s::date - to_date) AS age, advance_status
                    FROM travel_request WHERE status = 'approved' AND (%s::date - to_date) IN (3, 10, 30)""", [today, today])
    n = 0
    for t in due:
        if t['age'] == 30:
            if t['advance_status'] == 'none':
                continue
            for user_id in user_ids_with_role(t['company_id'], 'hr'):
                notify(company_id=t['company_id'], user_id=user_id, template='travel_settlement.overdue', title='Travel advance not settled',
                       body=f"A trip to {t['destination']} ended 30 days ago and its advance has not been settled.",
                       payload={'travelId': t['id']}, channels=('push',))
                n += 1
            continue
        user_id = user_id_for_employee(t['employee_id'])
        if not user_id:
            continue
        notify(company_id=t['company_id'], user_id=user_id, employee_id=t['employee_id'], template='travel_settlement.reminder',
               title='Settle your trip',
               body=f"Your trip to {t['destination']} has ended. Submit the settlement with your bills under Claims & travel.",
               payload={'travelId': t['id']}, channels=('push',))
        n += 1
    return n


@daily_job('travelSettlementReminders')
def travel_settlement_reminders(now):
    return remind_travel_settlements(now)
