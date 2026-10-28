"""Run only the scheduler: every 15 s it enqueues the due jobs as tasks for `manage.py db_worker` processes.

    manage.py run_scheduler              forever
    manage.py run_scheduler --once       one tick: enqueue what is due now and print the task ids
"""
import json
import signal
import threading

from django.core.management.base import BaseCommand

from hajiri.worker.runner import Scheduler, run_scheduler_forever


class Command(BaseCommand):
    help = 'Enqueue the periodic and daily background jobs as tasks (run db_worker to execute them).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='one tick, then exit')

    def handle(self, *args, **opts):
        if opts['once']:
            out = Scheduler().tick()
            self.stdout.write(json.dumps(out, indent=2) if out is not None else 'another scheduler holds the lock')
            return
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        run_scheduler_forever(stop)
