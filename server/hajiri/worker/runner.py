"""
The scheduler (startWorker in apps/api/src/worker.ts): one tick every 15 s. Cadence: the recompute queue every tick,
delivery every 30 s, terminal checks every 60 s, auto clock-out and the digest every 5 min, SLA reminders every
10 min, the nightly sweep, daily module jobs, leave accrual and punch partitions at 00:30 Nepal time (and the accrual
catch-up and partitions once at start). Registered periodic jobs run at their own cadence.

Only one process runs ticks at a time: each tick takes a PostgreSQL advisory lock, so several web processes with
RUN_WORKER=true, or a `run_worker` command next to them, never do the same work twice. The nightly run is recorded in
company_setting (key worker.nightly, one row per company), so a restart or a second process after 00:30 does not run
it again the same day.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from django.db import close_old_connections, connection

from hajiri.core.clock import NPT, now as _now
from hajiri.core.db import execute, one

from . import registry
from .tasks import run_once

log = logging.getLogger('hajiri.worker')

TICK_SECONDS = 15
_LOCK_KEY = 0x48414A495249  # 'HAJIRI'
_CADENCE = {'deliver': 30, 'offline': 60, 'sla': 600, 'autoClockOut': 300, 'digest': 300}


class Scheduler:
    def __init__(self, senders: list | None = None):
        self.senders = senders
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
            jobs += ['nightly', 'accrual', 'partitions']
        # Run the idempotent leave catch-up and the partition check once at start, so a fresh or restarted install
        # does not wait for 00:30.
        if self.startup:
            self.startup = False
            jobs += [j for j in ('accrual', 'partitions') if j not in jobs]
        return jobs

    def tick(self, now: datetime | None = None) -> dict | None:
        """One tick, if this process holds the worker lock; None when another process is working."""
        if not _try_lock():
            return None
        try:
            now = now or _now()
            jobs = self.due_jobs(now)
            result = run_once(jobs, now, self.senders)
            if 'nightly' in jobs:
                _mark_nightly(now.astimezone(NPT).date().isoformat())
            q = result.get('queue')
            if isinstance(q, dict) and q.get('processed'):
                log.info('worker tick %s', result)
            return result
        finally:
            _unlock()


def _try_lock() -> bool:
    return bool(one('SELECT pg_try_advisory_lock(%s) AS ok', [_LOCK_KEY])['ok'])


def _unlock() -> None:
    one('SELECT pg_advisory_unlock(%s) AS ok', [_LOCK_KEY])


def _nightly_ran(today: str) -> bool:
    """Has the nightly work already run for this Nepal date (in any process)?"""
    r = one("SELECT 1 AS x FROM company_setting WHERE key = 'worker.nightly' AND value->>'date' = %s LIMIT 1", [today])
    return r is not None


def _mark_nightly(today: str) -> None:
    execute("""INSERT INTO company_setting (company_id, key, value) SELECT id, 'worker.nightly', %s FROM company
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            [json.dumps({'date': today})])


def run_forever(stop: threading.Event | None = None, senders: list | None = None) -> None:
    stop = stop or threading.Event()
    sched = Scheduler(senders)
    log.info('worker started')
    while not stop.is_set():
        started = time.monotonic()
        try:
            close_old_connections()
            sched.tick()
        except Exception:  # noqa: BLE001 - a failed tick is logged and the next one runs
            log.exception('worker tick failed')
        finally:
            close_old_connections()
        stop.wait(max(0.0, TICK_SECONDS - (time.monotonic() - started)))
    connection.close()
    log.info('worker stopped')


_thread: threading.Thread | None = None
_stop = threading.Event()


def maybe_start_in_process() -> bool:
    """Start the worker in a daemon thread when settings.RUN_WORKER is true (the TS server did the same). Call from
    the WSGI/ASGI entry point, not from AppConfig.ready, so management commands and tests never start it."""
    global _thread
    from django.conf import settings
    if not getattr(settings, 'RUN_WORKER', False) or (_thread and _thread.is_alive()):
        return False
    _thread = threading.Thread(target=run_forever, args=(_stop,), name='hajiri-worker', daemon=True)
    _thread.start()
    return True


def stop_in_process() -> None:
    _stop.set()
