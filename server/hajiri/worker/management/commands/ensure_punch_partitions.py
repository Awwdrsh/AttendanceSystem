"""Create the monthly punch_event partitions for this month and the next ones (the worker also does this nightly)."""
from django.core.management.base import BaseCommand

from hajiri.worker.tasks import ensure_punch_partitions


class Command(BaseCommand):
    help = 'Create missing monthly punch_event partitions.'

    def add_arguments(self, parser):
        parser.add_argument('--months-ahead', type=int, default=3)

    def handle(self, *args, **opts):
        made = ensure_punch_partitions(months_ahead=opts['months_ahead'])
        self.stdout.write(f'created: {", ".join(made)}' if made else 'all partitions exist')
