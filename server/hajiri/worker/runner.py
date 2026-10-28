"""
The scheduler (startWorker in apps/api/src/worker.ts) on django.tasks: one tick every 15 s decides which jobs are due
and ENQUEUES them as `hajiri.worker.tasks.run_job` tasks; task workers (`manage.py db_worker`, or the worker thread of
`manage.py run_worker`) run them.

Cadence: the recompute queue every tick, delivery every 30 s, terminal checks every 60 s, auto clock-out and the
digest every 5 min, SLA reminders every 10 min, the nightly sweep, daily module jobs, leave accrual, punch partitions
and task-result pruning at 00:30 Nepal time (and the accrual catch-up and partitions once at start). Registered
periodic jobs run at their own cadence.

Only one scheduler ticks at a time: each tick takes a PostgreSQL advisory lock, so several web processes with
RUN_WORKER=true, or a `run_scheduler` next to them, never enqueue the same work twice; a job that is still waiting in
the queue is not enqueued again. The nightly run is recorded in company_setting (key worker.nightly, one row per
company), so a restart or a second process after 00:30 does not run it again the same day.

Processes:
- `manage.py run_worker`: scheduler and one task worker in one process (the single-machine default; RUN_WORKER=true
  starts the same two threads inside the web process).
- `manage.py run_scheduler` + any number of `manage.py db_worker`: the same, split for larger installs.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from django.db import close_old_connections, connection

from hajiri.core.clock import NPT, now as _now
from hajiri.core.settings_store import put_settings
from hajiri.db.models import Company, CompanySetting

from . import registry
from .tasks import run_job, run_once

log = logging.getLogger('hajiri.worker')

TICK_SECONDS = 15
_LOCK_KEY = 0x48414A495249  # 'HAJIRI'
_CADENCE = {'deliver': 30, 'offline': 60, 'sla': 600, 'autoClockOut': 300, 'digest': 300}


class Scheduler:
    """Decides what is due. `tick()` enqueues the due jobs as tasks; with inline=True it runs them in this process
    instead (one-off tooling and tests that need the results at once)."""

    def __init__(self, senders: list | None = None, *, inline: bool = False):
        self.senders = senders
        self.inline = inline
        self.last: dict[str, float] = {}
        self.nightly_done_for = ''
        self.startup = True

    def _every(self, job: str, seconds: float, t: float) -> bool:
        if t - self.last.get(job, 0) >= seconds:
            self.last[job] = t
            return True
        return False

    def due_jobs(self, now: datetime) -> list[str]:
        t = now.timestamp()
        jobs = ['queue']
        for job, seconds in _CADENCE.items():
            if self._every(job, seconds, t):
                jobs.append(job)
        for j in registry.periodic_jobs():
            if self._every(f'periodic:{j.name}', j.every_seconds or TICK_SECONDS, t):
                jobs.append(j.name)
        npt = now.astimezone(NPT)
        today = npt.date().isoformat()
        if npt.hour == 0 and npt.minute >= 30 and self.nightly_done_for != today and not _nightly_ran(today):
            self.nightly_done_for = today
            jobs += ['nightly', 'accrual', 'partitions', 'pruneTasks']
        # Run the idempotent leave catch-up and the partition check once at start, so a fresh or restarted install
        # does not wait for 00:30.
        if self.startup:
            self.startup = False
            jobs += [j for j in ('accrual', 'partitions') if j not in jobs]
        return jobs

    def tick(self, now: datetime | None = None) -> dict | None:
        """One tick, if this process holds the scheduler lock (None when another scheduler is ticking). Returns
        {job: task result id} for the enqueued jobs (or {job: summary} inline)."""
        if not _try_lock():
            return None
        try:
            now = now or _now()
            jobs = self.due_jobs(now)
            if self.inline:
                result = run_once(jobs, now, self.senders)
            else:
                waiting = _waiting_jobs()
                result = {}
                for job in jobs:
                    if job in waiting:
                        result[job] = 'already queued'
                        continue
                    result[job] = str(run_job.enqueue(job, now.isoformat()).id)
            if 'nightly' in jobs:
                _mark_nightly(now.astimezone(NPT).date().isoformat())
            return result
        finally:
            _unlock()


def _waiting_jobs() -> set[str]:
    """Job names with a run_job task still waiting to start (so a slow queue is not flooded with copies)."""
    try:
        from django_tasks_db.models import DBTaskResult
    except ImportError:  # pragma: no cover - another task backend
        return set()
    rows = (DBTaskResult.objects.filter(status='READY', task_path=run_job.module_path)
            .values_list('args_kwargs', flat=True))
    return {(r.get('args') or [None])[0] for r in rows if isinstance(r, dict)}


def _try_lock() -> bool:
    with connection.cursor() as cur:
        # Raw SQL: PostgreSQL advisory lock (no ORM API), held for the duration of one tick.
        cur.execute('SELECT pg_try_advisory_lock(%s)', [_LOCK_KEY])
        return bool(cur.fetchone()[0])


def _unlock() -> None:
    with connection.cursor() as cur:
        # Raw SQL: releases the advisory lock taken by _try_lock.
        cur.execute('SELECT pg_advisory_unlock(%s)', [_LOCK_KEY])


def _nightly_ran(today: str) -> bool:
    """Has the nightly work already run for this Nepal date (in any process)?"""
    return CompanySetting.objects.filter(key='worker.nightly', value__date=today).exists()


def _mark_nightly(today: str) -> None:
    put_settings(list(Company.objects.values_list('pk', flat=True)), 'worker.nightly', {'date': today})


# ---------------------------------------------------------------- long-running loops

def run_scheduler_forever(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    sched = Scheduler()
    log.info('scheduler started')
    while not stop.is_set():
        started = time.monotonic()
        try:
            close_old_connections()
            sched.tick()
        except Exception:  # noqa: BLE001 - a failed tick is logged and the next one runs
            log.exception('scheduler tick failed')
        finally:
            close_old_connections()
        stop.wait(max(0.0, TICK_SECONDS - (time.monotonic() - started)))
    connection.close()
    log.info('scheduler stopped')


def make_task_worker(*, interval: float = 1.0, worker_id: str | None = None):
    """A django_tasks_db worker for the default backend and every queue (what `manage.py db_worker` runs)."""
    from django.utils.crypto import get_random_string
    from django_tasks_db.management.commands.db_worker import Worker
    return Worker(queue_names=['*'], interval=interval, batch=False, backend_name='default', startup_delay=False,
                  max_tasks=None, worker_id=worker_id or get_random_string(32), excluded_queue_names=[])


def run_forever(stop: threading.Event | None = None, senders: list | None = None) -> None:
    """Scheduler and a task worker together until `stop` is set (run_worker, RUN_WORKER=true). `senders` is kept for
    the old signature; delivery tasks build theirs from the environment."""
    stop = stop or threading.Event()
    worker = make_task_worker()
    t = threading.Thread(target=_run_worker_until, args=(worker, stop), name='hajiri-task-worker', daemon=True)
    t.start()
    try:
        run_scheduler_forever(stop)
    finally:
        worker.running = False
        t.join(timeout=30)


def _run_worker_until(worker, stop: threading.Event) -> None:
    def watch():
        stop.wait()
        worker.running = False
    threading.Thread(target=watch, daemon=True).start()
    try:
        worker.run()
    except Exception:  # noqa: BLE001
        log.exception('task worker stopped with an error')
    finally:
        connection.close()


_thread: threading.Thread | None = None
_stop = threading.Event()


def maybe_start_in_process() -> bool:
    """Start the scheduler and a task worker in a daemon thread when settings.RUN_WORKER is true (the TS server did the
    same). Call from the WSGI/ASGI entry point, not from AppConfig.ready, so management commands and tests never
    start it."""
    global _thread
    from django.conf import settings
    if not getattr(settings, 'RUN_WORKER', False) or (_thread and _thread.is_alive()):
        return False
    _thread = threading.Thread(target=run_forever, args=(_stop,), name='hajiri-worker', daemon=True)
    _thread.start()
    return True


def stop_in_process() -> None:
    _stop.set()
