"""Run the background worker.

    manage.py run_worker                 scheduler + one task worker in this process (single-machine default)
    manage.py run_worker --once          run the given jobs now, in this process, and print their results
    manage.py run_worker --once --jobs queue,digest

Larger installs run `manage.py run_scheduler` once and `manage.py db_worker` (django_tasks_db) as often as needed.
"""
import json
import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from hajiri.worker.runner import run_forever
from hajiri.worker.tasks import JOB_NAMES, run_once


class Command(BaseCommand):
    help = 'Run the background worker (scheduler and task worker: recompute queue, delivery, reminders, nightly jobs).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='run the jobs once, in this process, and exit')
        parser.add_argument('--jobs', default=','.join(JOB_NAMES), help=f'comma-separated, for --once ({",".join(JOB_NAMES)})')

    def handle(self, *args, **opts):
        if opts['once']:
            jobs = [j.strip() for j in opts['jobs'].split(',') if j.strip()]
            if not jobs:
                raise CommandError('no jobs given')
            self.stdout.write(json.dumps(run_once(jobs), default=str, indent=2))
            return
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        run_forever(stop)
