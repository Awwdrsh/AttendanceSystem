"""Daily help desk upkeep (module-jobs.ts `helpdesk`): close old resolved tickets, escalate tickets past their SLA."""
from hajiri.worker.registry import daily_job

from .services import helpdesk_daily


@daily_job('helpdesk')
def helpdesk_job(now):
    return helpdesk_daily(now)
