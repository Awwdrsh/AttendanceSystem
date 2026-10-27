"""Worker job (module-jobs.ts candidatesPurged): the nightly purge of candidates who were not selected."""
from hajiri.worker.registry import daily_job

from .services import purge_rejected_candidates


@daily_job('candidatesPurged')
def candidates_purged(now):
    return purge_rejected_candidates(now)
