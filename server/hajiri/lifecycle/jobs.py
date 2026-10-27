"""Worker jobs and hooks of the lifecycle app (module-jobs.ts lifecycle; integrations.ts hire hook)."""
from hajiri.worker.registry import daily_job, register_hook

from .service import auto_start_onboarding, lifecycle_daily


@daily_job('lifecycle')
def lifecycle_job(now):
    return lifecycle_daily(now)


register_hook('lifecycle.auto_start_onboarding', auto_start_onboarding)
