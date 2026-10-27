"""Daily clean-up of uploads never attached to a record."""
from hajiri.worker.registry import daily_job

from .store import purge_unattached_files


@daily_job('filesPurged')
def files_purged(now):
    return purge_unattached_files(now)
