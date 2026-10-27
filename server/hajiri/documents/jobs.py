"""Daily documents job (module-jobs.ts `documents`): expiry alerts and weekly policy reminders."""
from hajiri.worker.registry import daily_job

from .services import documents_daily_job


@daily_job('documents')
def documents_daily(now):
    return documents_daily_job(now)
