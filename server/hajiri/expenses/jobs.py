"""Background work (port of apps/api/src/modules/expenses/jobs.ts): travel settlement reminders, daily."""
from __future__ import annotations

from datetime import datetime, timedelta

from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today
from hajiri.db.models import TravelRequest
from hajiri.notifications.service import notify, user_id_for_employee, user_ids_with_role
from hajiri.worker.registry import daily_job


def remind_travel_settlements(now: datetime | None = None) -> int:
    """
    Remind travellers to settle a trip 3 and 10 days after it ended (an unsettled advance stays outstanding), and tell
    HR about trips still unsettled 30 days after the return date. Returns the number of notifications.
    """
    today = npt_today(now or _now())
    # Every company's trips (a job has no active company); each notification goes to that trip's company.
    due = (TravelRequest.objects.filter(status=TravelRequest.Status.APPROVED,
                                        to_date__in=[today - timedelta(days=d) for d in (3, 10, 30)])
           .order_by('company_id', 'to_date', 'id').values('id', 'company_id', 'employee_id', 'destination', 'to_date',
                                                            'advance_status'))
    n = 0
    for t in due:
        company_id, travel_id = str(t['company_id']), str(t['id'])
        if (today - t['to_date']).days == 30:
            if t['advance_status'] == TravelRequest.AdvanceStatus.NONE:
                continue
            for user_id in user_ids_with_role(company_id, 'hr'):
                notify(company_id=company_id, user_id=user_id, template='travel_settlement.overdue', title='Travel advance not settled',
                       body=f"A trip to {t['destination']} ended 30 days ago and its advance has not been settled.",
                       payload={'travelId': travel_id}, channels=('push',))
                n += 1
            continue
        user_id = user_id_for_employee(str(t['employee_id']))
        if not user_id:
            continue
        notify(company_id=company_id, user_id=user_id, employee_id=str(t['employee_id']), template='travel_settlement.reminder',
               title='Settle your trip',
               body=f"Your trip to {t['destination']} has ended. Submit the settlement with your bills under Claims & travel.",
               payload={'travelId': travel_id}, channels=('push',))
        n += 1
    return n


@daily_job('travelSettlementReminders')
def travel_settlement_reminders(now):
    return remind_travel_settlements(now)
