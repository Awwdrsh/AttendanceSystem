"""Daily job 'relations' (module-jobs.ts): end temporary roles, milestone reminders, overdue responses, KYE reminders,
close decided grievances after the appeal window."""
from hajiri.worker.registry import daily_job

from .services import relations_daily


@daily_job('relations')
def relations_job(now):
    return relations_daily(now)
