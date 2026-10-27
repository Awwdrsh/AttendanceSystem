"""Daily time-tracking work (module-jobs.ts `timesheets`): stop forgotten timers, remind on Fridays."""
from hajiri.worker.registry import daily_job

from .services import daily_time_jobs


@daily_job('timesheets')
def timesheets_daily(now):
    return daily_time_jobs(now)
