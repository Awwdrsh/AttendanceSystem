"""
Daily reminders (port of apps/api/src/modules/performance/jobs.ts): 3 days, 1 day and on the day a review phase is
due, to whoever still has something to submit; the day before a 1-on-1; and the learning reminders. Each is sent once
(perf_reminder_sent).
"""
from __future__ import annotations

from datetime import date

from hajiri.core.bsdate import bs_label
from hajiri.core.db import rows
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.worker.registry import daily_job
from hajiri.workflow.service import npt_today

from .common import first_time

REMIND_DAYS = (3, 1, 0)


def _days_between(a: str, b) -> int:
    return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(a)).days


def performance_reminders(now=None) -> dict:
    from hajiri.learning.services import learning_reminders
    today = npt_today()
    reviews = 0
    cycles = rows("""SELECT id, company_id, name, status, self_due, manager_due, peer_due, calibration_due FROM perf_cycle
                      WHERE status IN ('self_review', 'manager_review', 'peer_review', 'calibration')""")
    for c in cycles:
        cid, co = str(c['id']), str(c['company_id'])
        due = {'self_review': c['self_due'], 'manager_review': c['manager_due'], 'peer_review': c['peer_due'],
               'calibration': c['calibration_due']}[c['status']]
        if not due:
            continue
        left = _days_between(today, due)
        if left not in REMIND_DAYS:
            continue
        when = 'today' if left == 0 else f'on {bs_label(due)}'
        targets: list[str] = []
        if c['status'] == 'self_review':
            targets = [str(x['employee_id']) for x in rows('SELECT employee_id FROM perf_review WHERE cycle_id = %s AND self_submitted_at IS NULL', [cid])]
        elif c['status'] == 'manager_review':
            targets = [str(x['m']) for x in rows("""SELECT DISTINCT manager_employee_id AS m FROM perf_review WHERE cycle_id = %s
                                                     AND manager_submitted_at IS NULL AND manager_employee_id IS NOT NULL""", [cid])]
        elif c['status'] == 'peer_review':
            targets = [str(x['p']) for x in rows("""SELECT DISTINCT p.reviewer_employee_id AS p FROM perf_peer p JOIN perf_review r ON r.id = p.review_id
                                                     WHERE r.cycle_id = %s AND p.status = 'approved'""", [cid])]
        elif first_time(f'cycle:{cid}:calibration:{left}'):
            notify_role(co, 'hr', template='review.reminder', title=f"{c['name']}: calibration due {when}",
                        body='Set final ratings and release the reviews.', payload={'cycleId': cid})
            reviews += 1
        what = {'self_review': 'your self review', 'manager_review': "your team's reviews",
                'peer_review': 'the peer feedback you were asked for'}.get(c['status'])
        for emp in targets:
            if not first_time(f"cycle:{cid}:{c['status']}:{emp}:{left}"):
                continue
            notify(company_id=co, user_id=user_id_for_employee(emp), employee_id=emp, template='review.reminder',
                   title=f"{c['name']}: due {when}", body=f'Please complete {what}.', payload={'cycleId': cid})
            reviews += 1

    one_on_ones = 0
    meetings = rows("""SELECT o.id, o.company_id, o.manager_employee_id, o.employee_id, m.name_en AS mn, e.name_en AS en FROM perf_one_on_one o
                         JOIN employee m ON m.id = o.manager_employee_id JOIN employee e ON e.id = o.employee_id
                        WHERE o.status = 'scheduled' AND (o.scheduled_at AT TIME ZONE 'Asia/Kathmandu')::date = %s::date + 1""", [today])
    for m in meetings:
        if not first_time(f"1on1:{m['id']}:{today}"):
            continue
        for emp, other in ((str(m['manager_employee_id']), m['en']), (str(m['employee_id']), m['mn'])):
            notify(company_id=str(m['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template='one_on_one.reminder',
                   title='1-on-1 tomorrow', body=f'With {other}. Add agenda items under Growth, 1-on-1s.', payload={'meetingId': str(m['id'])})
        one_on_ones += 1
    return {'reviews': reviews, 'oneOnOnes': one_on_ones, 'learning': learning_reminders(now)}


@daily_job('performanceReminders')
def _daily(now):
    return performance_reminders(now)
