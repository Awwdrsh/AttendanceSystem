"""Daily survey job (module-jobs.ts `surveys`): close, repeat recurring pulses, announce openings, remind."""
from hajiri.core.clock import npt_today
from hajiri.worker.registry import daily_job

from .surveys import run_survey_jobs


@daily_job('surveys')
def surveys_daily(now):
    return run_survey_jobs(npt_today(now).isoformat())
