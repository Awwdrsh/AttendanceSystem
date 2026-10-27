"""Daily job 'staleBreaksClosed' (module-jobs.ts): close breaks left open from an earlier day."""
from hajiri.worker.registry import daily_job

from .services import close_stale_breaks


@daily_job('staleBreaksClosed')
def stale_breaks_closed(now):
    return close_stale_breaks(now)
