"""
Daily reminders (port of apps/api/src/modules/performance/jobs.ts): 3 days, 1 day and on the day a review phase is
due, to whoever still has something to submit; the day before a 1-on-1; and the learning reminders. Each is sent once
(perf_reminder_sent). Background jobs have no active company: every row carries its own company id.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import F

from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.db.models import PerfCycle, PerfOneOnOne, PerfPeer, PerfReview
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.worker.registry import daily_job

from .common import first_time, npt_day_bounds

REMIND_DAYS = (3, 1, 0)
Phase = PerfCycle.Status
DUE_FIELD = {Phase.SELF_REVIEW: 'self_due', Phase.MANAGER_REVIEW: 'manager_due', Phase.PEER_REVIEW: 'peer_due',
             Phase.CALIBRATION: 'calibration_due'}
WHAT = {Phase.SELF_REVIEW: 'your self review', Phase.MANAGER_REVIEW: "your team's reviews",
        Phase.PEER_REVIEW: 'the peer feedback you were asked for'}


def _owing(c: PerfCycle) -> list[str]:
    """Employees who still have to submit something in the cycle's current phase."""
    reviews = PerfReview.objects.filter(cycle_id=c.id)
    if c.status == Phase.SELF_REVIEW:
        ids = reviews.filter(self_submitted_at__isnull=True).values_list('employee_id', flat=True)
    elif c.status == Phase.MANAGER_REVIEW:
        ids = (reviews.filter(manager_submitted_at__isnull=True, manager_employee_id__isnull=False)
               .values_list('manager_employee_id', flat=True).distinct())
    else:
        ids = (PerfPeer.objects.filter(review__cycle_id=c.id, status=PerfPeer.Status.APPROVED)
               .values_list('reviewer_employee_id', flat=True).distinct())
    return [str(x) for x in ids]


def performance_reminders(now=None) -> dict:
    from hajiri.learning.services import learning_reminders
    today = npt_today()
    reviews = 0
    for c in PerfCycle.objects.filter(status__in=tuple(DUE_FIELD)):
        due = getattr(c, DUE_FIELD[c.status])
        if not due:
            continue
        left = (due - today).days
        if left not in REMIND_DAYS:
            continue
        cid, co = str(c.id), str(c.company_id)
        when = 'today' if left == 0 else f'on {bs_label(due)}'
        if c.status == Phase.CALIBRATION:
            if first_time(f'cycle:{cid}:calibration:{left}'):
                notify_role(co, 'hr', template='review.reminder', title=f'{c.name}: calibration due {when}',
                            body='Set final ratings and release the reviews.', payload={'cycleId': cid})
                reviews += 1
            continue
        for emp in _owing(c):
            if not first_time(f'cycle:{cid}:{c.status}:{emp}:{left}'):
                continue
            notify(company_id=co, user_id=user_id_for_employee(emp), employee_id=emp, template='review.reminder',
                   title=f'{c.name}: due {when}', body=f'Please complete {WHAT[c.status]}.', payload={'cycleId': cid})
            reviews += 1

    one_on_ones = 0
    start, end = npt_day_bounds(today + timedelta(days=1))
    meetings = (PerfOneOnOne.objects.filter(status=PerfOneOnOne.Status.SCHEDULED, scheduled_at__gte=start, scheduled_at__lt=end)
                .values('id', 'company_id', 'manager_employee_id', 'employee_id', mn=F('manager_employee__name_en'),
                        en=F('employee__name_en')))
    for m in meetings:
        if not first_time(f"1on1:{m['id']}:{today.isoformat()}"):
            continue
        for emp, other in ((str(m['manager_employee_id']), m['en']), (str(m['employee_id']), m['mn'])):
            notify(company_id=str(m['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template='one_on_one.reminder',
                   title='1-on-1 tomorrow', body=f'With {other}. Add agenda items under Growth, 1-on-1s.',
                   payload={'meetingId': str(m['id'])})
        one_on_ones += 1
    return {'reviews': reviews, 'oneOnOnes': one_on_ones, 'learning': learning_reminders(now)}


@daily_job('performanceReminders')
def _daily(now):
    return performance_reminders(now)
