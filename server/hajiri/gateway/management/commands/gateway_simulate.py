"""Terminal simulator: drives a running gateway like a ZKTeco or Hikvision terminal would.

    manage.py gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
    manage.py gateway_simulate --vendor hikvision --sn HIK-01 --pin 1 --time "2026-09-21 10:17:03"
Options as in the TS simulator: --pin/--time (repeatable), --poll N, --verify, --status, --template.
"""
from __future__ import annotations

import argparse

from django.core.management.base import BaseCommand

from hajiri.gateway.simulate import simulate


class Command(BaseCommand):
    help = 'Simulate a ZKTeco (ADMS) or Hikvision (ISAPI) terminal against a running gateway.'

    def add_arguments(self, parser):
        parser.add_argument('sim_args', nargs=argparse.REMAINDER,
                            help='--url, --sn, --vendor, --pin, --time, --poll, --verify, --status, --template')

    def run_from_argv(self, argv):
        # Pass every option through untouched (repeatable --pin/--time pairs, as the TS script read process.argv).
        self._raw = argv[2:]
        super().run_from_argv(argv[:2])

    def handle(self, *args, **opts):
        simulate(getattr(self, '_raw', None) or opts.get('sim_args') or [], out=self.stdout)
